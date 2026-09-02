#!/usr/bin/env python3
"""Fail-closed, exhaustive queue scan for exclusive JIT assignment classes."""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from bounded_subprocess import ObservationError, run_bounded


PER_PAGE = 100


class ScanError(RuntimeError):
    """The queue could not be observed completely and authoritatively."""


def _object_list(payload: Any, key: str, path: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ScanError(f"GitHub API returned non-object for {path}")
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ScanError(f"GitHub API returned invalid {key} for {path}")
    return value


class StaleDemandClassifier:
    """Refuses to count demand from a merge_group run that can no longer run.

    A dequeued merge queue entry can leave its workflow run reporting `queued`
    indefinitely: the queue branch is deleted, the run carries no jobs, a normal
    cancel reports the run already completed, a force-cancel reports it is not
    queued, and deleting it is forbidden. Nothing in the run's own status
    distinguishes that from a live entry, so demand derived from status alone
    never drains and the class it belongs to outranks every lower tier forever.

    Classification is POSITIVE DETERMINATION ONLY. An API error, a timeout, or
    any indeterminate answer leaves the run counted. Treating uncertainty as
    staleness would turn this guard into the silent demand-suppressor it exists
    to prevent, which is the more dangerous of the two failures: a run wrongly
    counted wastes a boot, a run wrongly discarded strands real work.

    A run is excluded from demand when EITHER its queue branch is confirmed
    absent OR it is confirmed to carry no queued job. It is additionally
    remembered — so later passes skip its job fetch entirely — only when BOTH
    are confirmed, which is the exact signature above. The narrower rule governs
    persistence because a remembered verdict outlives the observation it came
    from.
    """

    CLASSIFIED_EVENT = "merge_group"

    def __init__(self, scanner: "AssignmentScanner", args: argparse.Namespace) -> None:
        self.scanner = scanner
        self.enabled = args.stale_demand_classifier
        self.ttl = args.stale_demand_ttl_seconds
        self.min_age = args.stale_demand_min_age_seconds
        self.path = Path(args.stale_demand_quarantine_file)
        self.lock = threading.Lock()
        self.evidence: list[dict[str, Any]] = []
        self.quarantined: dict[str, dict[str, Any]] = {}
        self.dirty = False
        if self.enabled:
            self.quarantined = self._load()

    # ── persistence ──
    def _load(self) -> dict[str, dict[str, Any]]:
        # A quarantine we cannot read is an empty quarantine: the only cost is
        # re-doing work. Refusing to scan because a cache file is corrupt would
        # make a cosmetic problem fail the fleet.
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        runs = payload.get("runs") if isinstance(payload, dict) else None
        if not isinstance(runs, dict):
            return {}
        now = time.time()
        live: dict[str, dict[str, Any]] = {}
        for run_id, record in runs.items():
            if not isinstance(record, dict):
                continue
            at = record.get("quarantined_at")
            if not isinstance(at, (int, float)):
                continue
            if now - at >= self.ttl:
                self.dirty = True
                continue
            live[str(run_id)] = record
        return live

    def persist(self) -> None:
        if not self.enabled or not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"runs": self.quarantined}, indent=2, sort_keys=True) + "\n"
            )
            os.replace(tmp, self.path)
        except OSError:
            # Losing the memo costs an API call next pass and nothing else.
            pass

    # ── evidence ──
    def note(self, kind: str, run: dict[str, Any], **fields: Any) -> None:
        record = {
            "evidence": kind,
            "run_id": run.get("id"),
            "event": run.get("event"),
            "head_branch": run.get("head_branch"),
            **fields,
        }
        with self.lock:
            self.evidence.append(record)

    def emit(self) -> None:
        # stderr only. stdout carries the demand count the caller parses.
        for record in self.evidence:
            print("stale-demand: " + json.dumps(record, sort_keys=True), file=sys.stderr)

    # ── classification ──
    def considers(self, run: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        if str(run.get("event", "")).lower() != self.CLASSIFIED_EVENT:
            return False
        # A merge_group run that is genuinely in flight is the normal case and
        # must cost nothing. Only a run that has been queued long enough to be
        # anomalous is worth an extra call.
        stamp = str(run.get("created_at") or run.get("updated_at") or "")
        try:
            created = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return False
        age = (dt.datetime.now(dt.timezone.utc) - created).total_seconds()
        return age >= self.min_age

    def remembered(self, run: dict[str, Any]) -> bool:
        """True when a previous pass proved this run stale — skip its jobs fetch."""
        if not self.considers(run):
            return False
        record = self.quarantined.get(str(run.get("id")))
        if record is None:
            return False
        self.note("quarantine_hit", run, reason=record.get("reason"))
        return True

    def classify(self, run: dict[str, Any], queued_jobs: int) -> bool:
        """Return True when this run's demand must not be counted."""
        if not self.considers(run):
            return False
        has_job = queued_jobs > 0
        branch_live = self._branch_live(run)
        if branch_live is None:
            # Indeterminate. Count the run; say so, so a persistent
            # indeterminate answer is visible rather than silently benign.
            self.note("indeterminate", run, queued_jobs=queued_jobs)
            return False
        if branch_live and has_job:
            return False
        reason = (
            "queue_branch_absent_and_no_queued_job"
            if not branch_live and not has_job
            else "queue_branch_absent"
            if not branch_live
            else "no_queued_job"
        )
        self.note("excluded", run, reason=reason, queued_jobs=queued_jobs)
        if not branch_live and not has_job:
            with self.lock:
                self.quarantined[str(run.get("id"))] = {
                    "quarantined_at": time.time(),
                    "reason": reason,
                    "head_branch": run.get("head_branch"),
                }
                self.dirty = True
        return True

    def _branch_live(self, run: dict[str, Any]) -> bool | None:
        branch = run.get("head_branch")
        if not isinstance(branch, str) or not branch.strip():
            return None
        return self.scanner.ref_exists(f"heads/{branch.strip()}")


class AssignmentScanner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.workflows = set(args.workflow)
        self.runner_labels = {
            label.strip().lower() for label in args.labels.split(",") if label.strip()
        }
        self.required_label = args.require_label.strip().lower()
        if not self.workflows:
            raise ScanError("at least one workflow is required")
        if not self.required_label:
            raise ScanError("the required assignment-class label is empty")
        if self.required_label not in self.runner_labels:
            raise ScanError("required assignment-class label is absent from runner labels")
        self.deadline = time.monotonic() + args.scan_timeout
        self.observation_lock_path = Path(args.observation_lock_file)
        self.api_calls = 0
        self.api_calls_lock = threading.Lock()
        self.observation_lock_fd: int | None = None
        self.stale_demand = StaleDemandClassifier(self, args)

    @contextlib.contextmanager
    def _observation_lock(self) -> Any:
        self.observation_lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_deadline = min(
            self.deadline,
            time.monotonic() + self.args.observation_lock_timeout,
        )
        with self.observation_lock_path.open("a+", encoding="utf-8") as handle:
            while True:
                try:
                    fcntl.flock(
                        handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                    break
                except BlockingIOError:
                    if time.monotonic() >= lock_deadline:
                        raise ScanError(
                            "host queue observation lock timed out after "
                            f"{self.args.observation_lock_timeout}s"
                        )
                    time.sleep(0.05)
            try:
                self.observation_lock_fd = os.dup(handle.fileno())
                os.set_inheritable(self.observation_lock_fd, True)
                try:
                    yield
                finally:
                    os.close(self.observation_lock_fd)
                    self.observation_lock_fd = None
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _gh(self, path: str) -> dict[str, Any]:
        with self.api_calls_lock:
            if self.api_calls >= self.args.max_api_calls:
                raise ScanError(
                    f"assignment scan API budget exhausted ({self.args.max_api_calls})"
                )
            self.api_calls += 1
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ScanError("assignment scan exceeded its overall deadline")
        try:
            result = run_bounded(
                [self.args.gh_cli, "api", path],
                timeout=min(self.args.gh_timeout, remaining),
                operation="assignment_scan_github_api",
                pass_fds=(self.observation_lock_fd,)
                if self.observation_lock_fd is not None
                else (),
            )
        except (OSError, ObservationError) as error:
            raise ScanError(f"GitHub API unavailable for {path}: {error}") from error
        if result.returncode:
            raise ScanError(
                f"GitHub API failed for {path}: {result.stderr.strip()}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ScanError(f"GitHub API returned invalid JSON for {path}") from error
        if not isinstance(payload, dict):
            raise ScanError(f"GitHub API returned non-object for {path}")
        return payload

    def ref_exists(self, ref: str) -> bool | None:
        """True / False / None for present / confirmed absent / indeterminate.

        `_gh` deliberately collapses every non-200 into a fail-closed error. A
        staleness verdict needs the opposite: it must be able to tell a definite
        404 apart from a timeout, because only the first is evidence.
        """
        path = f"repos/{self.args.repo}/git/ref/{ref}"
        with self.api_calls_lock:
            if self.api_calls >= self.args.max_api_calls:
                return None
            self.api_calls += 1
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            result = run_bounded(
                [self.args.gh_cli, "api", path],
                timeout=min(self.args.gh_timeout, remaining),
                operation="assignment_scan_ref_probe",
                pass_fds=(self.observation_lock_fd,)
                if self.observation_lock_fd is not None
                else (),
            )
        except (OSError, ObservationError):
            return None
        if result.returncode == 0:
            return True
        if "404" in (result.stderr or ""):
            return False
        return None

    def _pages(self, path_prefix: str, key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        expected_total: int | None = None
        first_page_ids: tuple[int, ...] = ()
        for page in range(1, self.args.max_pages + 1):
            separator = "&" if "?" in path_prefix else "?"
            path = f"{path_prefix}{separator}per_page={PER_PAGE}&page={page}"
            payload = self._gh(path)
            page_items = _object_list(payload, key, path)
            total = payload.get("total_count")
            if not isinstance(total, int) or total < 0:
                raise ScanError(f"GitHub API returned invalid total_count for {path}")
            if total >= self.args.result_cap:
                raise ScanError(
                    f"GitHub API total_count reached endpoint cap for {path} "
                    f"({total} >= {self.args.result_cap})"
                )
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise ScanError(
                    f"GitHub API total_count changed during pagination for {path_prefix}"
                )
            page_ids: list[int] = []
            for item in page_items:
                item_id = item.get("id")
                if not isinstance(item_id, int):
                    raise ScanError(f"GitHub API returned item without integer id for {path}")
                if item_id in seen_ids:
                    raise ScanError(
                        f"GitHub API returned duplicate id during pagination for {path_prefix}"
                    )
                seen_ids.add(item_id)
                page_ids.append(item_id)
                items.append(item)
            if page == 1:
                first_page_ids = tuple(page_ids)
            if len(items) >= total:
                if len(items) != total:
                    raise ScanError(f"GitHub API pagination exceeded total_count for {path}")
                if page > 1:
                    verify_path = f"{path_prefix}{separator}per_page={PER_PAGE}&page=1"
                    verify_payload = self._gh(verify_path)
                    verify_items = _object_list(verify_payload, key, verify_path)
                    verify_total = verify_payload.get("total_count")
                    verify_ids = tuple(item.get("id") for item in verify_items)
                    if verify_total != expected_total or verify_ids != first_page_ids:
                        raise ScanError(
                            f"GitHub API first page changed during pagination for {path_prefix}"
                        )
                return items
            if len(page_items) < PER_PAGE:
                if len(items) < total:
                    raise ScanError(
                        f"GitHub API pagination ended before total_count for {path} "
                        f"({len(items)} < {total})"
                    )
                return items
        raise ScanError(
            f"GitHub API pagination truncated at {self.args.max_pages} pages for {path_prefix}"
        )

    def _workflow_ids(self) -> dict[str, int]:
        workflows = self._pages(
            f"repos/{self.args.repo}/actions/workflows", "workflows"
        )
        matches: dict[str, list[int]] = {name: [] for name in self.workflows}
        for workflow in workflows:
            name = workflow.get("name")
            if name not in matches:
                continue
            workflow_id = workflow.get("id")
            if not isinstance(workflow_id, int):
                raise ScanError(f"configured workflow {name!r} has no integer id")
            matches[name].append(workflow_id)
        resolved: dict[str, int] = {}
        for name, ids in matches.items():
            if len(ids) != 1:
                raise ScanError(
                    f"configured workflow {name!r} resolved to {len(ids)} ids"
                )
            resolved[name] = ids[0]
        return resolved

    def _runs(self) -> list[dict[str, Any]]:
        found: dict[int, dict[str, Any]] = {}
        for workflow_id in self._workflow_ids().values():
            for status in ("queued", "in_progress"):
                prefix = (
                    f"repos/{self.args.repo}/actions/workflows/{workflow_id}/runs"
                    f"?status={status}"
                )
                for run in self._pages(prefix, "workflow_runs"):
                    run_id = run.get("id")
                    if not isinstance(run_id, int):
                        raise ScanError("configured workflow run has no integer id")
                    found[run_id] = run
        return list(found.values())

    def _scan_run(self, run: dict[str, Any]) -> int:
        matches: set[int] = set()
        queued_jobs = 0
        run_id = int(run["id"])
        # A run already proved stale is not fetched again. That fetch is not
        # free: a permanently queued run is re-read by every lane on every
        # pass, and that added call is what pushes an otherwise healthy scan
        # past its API timeout and blinds the host.
        if self.stale_demand.remembered(run):
            return 0
        prefix = (
            f"repos/{self.args.repo}/actions/runs/{run_id}"
            "/jobs?filter=latest"
        )
        for job in self._pages(prefix, "jobs"):
            if str(job.get("status", "")).lower() != "queued":
                continue
            queued_jobs += 1
            labels_raw = job.get("labels")
            if not isinstance(labels_raw, list):
                raise ScanError(f"queued job in run {run_id} has invalid labels")
            if any(
                not isinstance(label, str) or not label.strip()
                for label in labels_raw
            ):
                raise ScanError(
                    f"queued job in run {run_id} has malformed label elements"
                )
            job_labels = {label.strip().lower() for label in labels_raw}
            # Both predicates are load-bearing. Subset matching models
            # GitHub assignment; explicit membership prevents a legacy
            # generic-only job from being mistaken for event-class demand.
            if self.required_label not in job_labels:
                continue
            if not job_labels.issubset(self.runner_labels):
                continue
            if self.args.min_age_seconds:
                timestamp = str(
                    job.get("created_at")
                    or job.get("started_at")
                    or run.get("updated_at")
                    or run.get("created_at")
                    or ""
                )
                try:
                    queued_at = dt.datetime.fromisoformat(
                        timestamp.replace("Z", "+00:00")
                    )
                except (AttributeError, ValueError) as error:
                    raise ScanError(
                        f"matching queued job in run {run_id} has no valid timestamp"
                    ) from error
                age = dt.datetime.now(dt.timezone.utc) - queued_at
                if age.total_seconds() < self.args.min_age_seconds:
                    continue
            job_id = job.get("id")
            if not isinstance(job_id, int):
                raise ScanError(f"matching queued job in run {run_id} has no integer id")
            matches.add(job_id)
        if self.stale_demand.classify(run, queued_jobs):
            return 0
        return len(matches)

    def scan(self) -> int:
        # Every fleet supervisor shares this host-global observation slot. The
        # exhaustive scanner can issue many GitHub calls; overlapping scans for
        # different lanes made individually healthy ghapp requests time out and
        # left the host scan-blind. The bounded lock wait is part of the overall
        # deadline and failure remains fail-closed.
        with self._observation_lock():
            runs = self._runs()
            # Concurrency remains opt-in for a measured host. The reliable
            # fleet default is one call stream; raising it multiplies pressure
            # inside the host-global scan and must not happen accidentally.
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.args.max_workers
            ) as executor:
                total = sum(executor.map(self._scan_run, runs), start=0)
        self.stale_demand.persist()
        self.stale_demand.emit()
        return total


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workflow", required=True, action="append")
    parser.add_argument("--labels", required=True)
    parser.add_argument("--require-label", required=True)
    parser.add_argument("--min-age-seconds", type=int, default=0)
    parser.add_argument("--gh-cli", default=os.environ.get("TARTCI_GH_CLI") or "gh")
    parser.add_argument(
        "--gh-timeout",
        type=int,
        default=int(os.environ.get("TARTCI_GH_TIMEOUT_SECS", "15")),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=int(os.environ.get("TARTCI_ASSIGNMENT_SCAN_MAX_PAGES", "100")),
    )
    parser.add_argument(
        "--scan-timeout",
        type=int,
        default=int(os.environ.get("TARTCI_ASSIGNMENT_SCAN_TIMEOUT_SECS", "180")),
    )
    parser.add_argument(
        "--result-cap",
        type=int,
        default=int(os.environ.get("TARTCI_ASSIGNMENT_SCAN_RESULT_CAP", "1000")),
    )
    parser.add_argument(
        "--max-api-calls",
        type=int,
        default=int(os.environ.get("TARTCI_ASSIGNMENT_SCAN_MAX_API_CALLS", "1200")),
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.environ.get("TARTCI_ASSIGNMENT_SCAN_MAX_WORKERS", "1")),
    )
    parser.add_argument(
        "--stale-demand-classifier",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("TARTCI_STALE_DEMAND_CLASSIFIER", "1") != "0",
    )
    parser.add_argument(
        "--stale-demand-ttl-seconds",
        type=int,
        default=int(os.environ.get("TARTCI_STALE_DEMAND_TTL_SECS", "21600")),
    )
    parser.add_argument(
        "--stale-demand-min-age-seconds",
        type=int,
        default=int(os.environ.get("TARTCI_STALE_DEMAND_MIN_AGE_SECS", "1800")),
    )
    parser.add_argument("--stale-demand-quarantine-file", default=None)
    parser.add_argument(
        "--observation-lock-file",
        default=os.environ.get("TARTCI_QUEUE_OBSERVATION_LOCK_FILE")
        or str(Path.home() / ".tartci/state/queue-observation.lock"),
    )
    parser.add_argument(
        "--observation-lock-timeout",
        type=float,
        default=float(
            os.environ.get("TARTCI_QUEUE_OBSERVATION_LOCK_TIMEOUT_SECS", "120")
        ),
    )
    args = parser.parse_args()
    if args.gh_timeout <= 0:
        parser.error("--gh-timeout must be positive")
    if args.max_pages <= 0:
        parser.error("--max-pages must be positive")
    if args.scan_timeout <= 0:
        parser.error("--scan-timeout must be positive")
    if args.result_cap <= 0:
        parser.error("--result-cap must be positive")
    if args.max_api_calls <= 0:
        parser.error("--max-api-calls must be positive")
    if not 1 <= args.max_workers <= 16:
        parser.error("--max-workers must be between 1 and 16")
    if (
        not math.isfinite(args.observation_lock_timeout)
        or args.observation_lock_timeout <= 0
    ):
        parser.error("--observation-lock-timeout must be positive")
    if args.min_age_seconds < 0:
        parser.error("--min-age-seconds must be non-negative")
    if args.stale_demand_ttl_seconds <= 0:
        parser.error("--stale-demand-ttl-seconds must be positive")
    if args.stale_demand_min_age_seconds < 0:
        parser.error("--stale-demand-min-age-seconds must be non-negative")
    if args.stale_demand_quarantine_file is None:
        # Follows whatever state directory the observation lock uses, so a test
        # or a redirected fleet root does not write into the real one.
        args.stale_demand_quarantine_file = str(
            Path(args.observation_lock_file).parent / "stale-demand-quarantine.json"
        )
    return args


def main() -> int:
    try:
        print(AssignmentScanner(parse_args()).scan())
    except (ScanError, ValueError) as error:
        print(f"assignment scan failed closed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
