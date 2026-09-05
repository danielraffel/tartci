#!/usr/bin/env python3
"""Bounded, paginated, fair GitHub Actions queue scanner for VM providers."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
import zlib
from math import ceil
from pathlib import Path
from typing import Any

from bounded_subprocess import run_bounded

RUNS_PER_PAGE = 100


class GitHubApiError(RuntimeError):
    def __init__(self, path: str, returncode: int, stderr: str) -> None:
        super().__init__(f"GitHub API failed for {path}: {stderr.strip()}")
        self.status = 404 if "HTTP 404" in stderr or "404 Not Found" in stderr else None
        self.returncode = returncode


def _created_key(run: dict[str, Any]) -> str:
    return str(run.get("created_at") or "")


def _fresh(timestamp: str, max_age_seconds: int) -> bool:
    if max_age_seconds <= 0:
        return True
    try:
        created = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return False
    return (dt.datetime.now(dt.timezone.utc) - created).total_seconds() <= max_age_seconds


def _old_enough(timestamp: str, min_age_seconds: int, now: int) -> bool:
    if min_age_seconds <= 0:
        return True
    try:
        created = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return False
    return now - int(created.timestamp()) >= min_age_seconds


class QueueScanner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        configured_workflows = (
            [args.workflow] if isinstance(args.workflow, str) else args.workflow
        )
        self.workflows = tuple(
            dict.fromkeys(
                workflow
                for workflow in configured_workflows
                if isinstance(workflow, str) and workflow
            )
        )
        if not self.workflows:
            raise ValueError("at least one workflow name is required")
        self.workflow_set = set(self.workflows)
        self.labels = {label.strip().lower() for label in args.labels.split(",") if label.strip()}
        self.job_statuses = {
            status.strip().lower()
            for status in args.job_statuses.split(",")
            if status.strip()
        }
        if args.newest_quota >= args.max_job_fetches:
            raise ValueError("newest_quota must be less than max_job_fetches")
        namespace = "\0".join(
            (
                args.repo.lower(),
                "\0".join(sorted(self.workflows)),
                ",".join(sorted(self.labels)),
                args.provider,
                args.lane_id,
                ",".join(sorted(self.job_statuses)),
            )
        )
        digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:16]
        base_state_path = Path(args.state_file)
        suffix = base_state_path.suffix or ".json"
        self.state_path = base_state_path.with_name(
            f"{base_state_path.stem}.{digest}{suffix}"
        )
        self.lock_path = self.state_path.with_suffix(f"{self.state_path.suffix}.lock")
        discovery_namespace = "\0".join(
            (args.repo.lower(), *sorted(self.workflows))
        )
        discovery_digest = hashlib.sha256(
            discovery_namespace.encode("utf-8")
        ).hexdigest()[:16]
        base_discovery_path = Path(args.shared_cache_file)
        discovery_suffix = base_discovery_path.suffix or ".json"
        self.discovery_path = base_discovery_path.with_name(
            f"{base_discovery_path.stem}.{discovery_digest}{discovery_suffix}"
        )
        self.discovery_lock_path = self.discovery_path.with_suffix(
            f"{self.discovery_path.suffix}.lock"
        )
        self.observation_lock_path = Path(args.observation_lock_file)
        self.lock_deadline = time.monotonic() + args.observation_lock_timeout
        self.state: dict[str, Any] = {}
        self.now = int(time.time())
        self.api_calls = 0
        self.observation_lock_fd: int | None = None

    def _gh(self, path: str) -> dict[str, Any]:
        if self.api_calls >= self.args.max_api_calls:
            raise RuntimeError(
                f"GitHub API call budget exhausted ({self.args.max_api_calls})"
            )
        self.api_calls += 1
        result = run_bounded(
            [self.args.gh_cli, "api", path],
            timeout=self.args.gh_timeout,
            operation="queue_scan_github_api",
            pass_fds=(self.observation_lock_fd,)
            if self.observation_lock_fd is not None
            else (),
        )
        if result.returncode:
            raise GitHubApiError(path, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError(f"GitHub API returned non-object for {path}")
        return payload

    @staticmethod
    def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return dict(default)
        return payload if isinstance(payload, dict) else dict(default)

    @staticmethod
    def _save_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)

    def _load_state(self) -> dict[str, Any]:
        return self._load_json(
            self.state_path,
            {"cursor_run_id": None, "negative": {}},
        )

    def _save_state(self, cursor_run_id: int | None, negative: dict[str, Any]) -> None:
        self._save_json(
            self.state_path,
            {
                **self.state,
                "cursor_run_id": cursor_run_id,
                "negative": negative,
            },
        )

    @contextlib.contextmanager
    def _bounded_lock(self, path: Path, kind: str) -> Any:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            while True:
                try:
                    fcntl.flock(
                        handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                    break
                except BlockingIOError:
                    if time.monotonic() >= self.lock_deadline:
                        raise RuntimeError(
                            f"{kind} lock timed out after "
                            f"{self.args.observation_lock_timeout}s"
                        )
                    time.sleep(0.05)
            try:
                yield handle
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _state_lock(self) -> Any:
        with self._bounded_lock(self.lock_path, "queue state"):
            self.state = self._load_state()
            yield

    @contextlib.contextmanager
    def _discovery_lock(self) -> Any:
        with self._bounded_lock(self.discovery_lock_path, "queue discovery"):
            yield

    @contextlib.contextmanager
    def _observation_lock(self) -> Any:
        """Bound concurrent GitHub observation across every lane on this host."""
        self.observation_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._bounded_lock(
            self.observation_lock_path, "host queue observation"
        ) as handle:
            # Duplicate the locked open-file description into an inheritable
            # descriptor so every bounded gh/ghapp tree retains the flock if
            # this scanner is killed. The kernel then releases host authority
            # only after the old observation tree exits.
            self.observation_lock_fd = os.dup(handle.fileno())
            os.set_inheritable(self.observation_lock_fd, True)
            try:
                yield
            finally:
                os.close(self.observation_lock_fd)
                self.observation_lock_fd = None

    def _workflow_id(self, discovery: dict[str, Any]) -> int | None:
        # Keep the focused workflow endpoint for the legacy one-name case.
        # GitHub has no multi-workflow runs endpoint, so a list uses the bounded
        # repository runs endpoint and filters exact names in _runs().
        if len(self.workflows) != 1:
            return None
        workflow_name = self.workflows[0]
        cached_id = discovery.get("workflow_id")
        cached_name = discovery.get("workflow_name")
        checked_at = int(discovery.get("workflow_checked_at", 0))
        if (
            isinstance(cached_id, int)
            and cached_name == workflow_name
            and self.now - checked_at < self.args.workflow_cache_ttl
        ):
            return cached_id
        workflows = self._gh(f"repos/{self.args.repo}/actions/workflows?per_page=100")
        for workflow in workflows.get("workflows", []):
            if workflow.get("name") == workflow_name:
                value = workflow.get("id")
                if value is not None:
                    workflow_id = int(value)
                    discovery["workflow_id"] = workflow_id
                    discovery["workflow_name"] = workflow_name
                    discovery["workflow_checked_at"] = self.now
                    return workflow_id
        return None

    def _run_page(
        self, workflow_id: int | None, status: str, page: int
    ) -> list[dict[str, Any]]:
        if workflow_id is None:
            path = (
                f"repos/{self.args.repo}/actions/runs"
                f"?status={status}&per_page={RUNS_PER_PAGE}&page={page}"
            )
        else:
            path = (
                f"repos/{self.args.repo}/actions/workflows/{workflow_id}/runs"
                f"?status={status}&per_page={RUNS_PER_PAGE}&page={page}"
            )
        page_runs = self._gh(path).get("workflow_runs", [])
        if not isinstance(page_runs, list):
            raise ValueError(f"workflow_runs is not a list for {path}")
        return page_runs

    def _runs(self, discovery: dict[str, Any]) -> list[dict[str, Any]]:
        for attempt in range(2):
            workflow_id = self._workflow_id(discovery)
            runs: list[dict[str, Any]] = []
            seen: set[int] = set()
            page_cursors_raw = discovery.get("run_page_cursor")
            page_cursors = page_cursors_raw if isinstance(page_cursors_raw, dict) else {}
            page_sweeps_raw = discovery.get("run_page_sweeps")
            page_sweeps = page_sweeps_raw if isinstance(page_sweeps_raw, dict) else {}
            try:
                # GitHub can report a workflow run as `pending` while its
                # self-hosted job is already `queued`. Include that parent-run
                # state or a demand-driven runner can never observe the job it
                # must register to serve.
                for status in ("pending", "queued", "in_progress"):
                    sweep = int(page_sweeps.get(status, 0))
                    completed_sweep = False
                    pages = [1]
                    newest_page = self._run_page(workflow_id, status, 1)
                    page_sets = [newest_page]
                    if len(newest_page) == RUNS_PER_PAGE and self.args.max_run_pages > 1:
                        cursor = page_cursors.get(status, 2)
                        cursor = cursor if isinstance(cursor, int) and cursor >= 2 else 2
                        for offset in range(self.args.max_run_pages - 1):
                            page = cursor + offset
                            page_runs = self._run_page(workflow_id, status, page)
                            pages.append(page)
                            page_sets.append(page_runs)
                            if len(page_runs) < RUNS_PER_PAGE:
                                page_cursors[status] = 2
                                completed_sweep = True
                                break
                        else:
                            page_cursors[status] = pages[-1] + 1
                    else:
                        page_cursors[status] = 2
                        completed_sweep = True

                    for page, page_runs in zip(pages, page_sets):
                        for run in page_runs:
                            if (
                                not isinstance(run, dict)
                                or run.get("name") not in self.workflow_set
                            ):
                                continue
                            run_id = run.get("id")
                            if not isinstance(run_id, int) or run_id in seen:
                                continue
                            seen.add(run_id)
                            runs.append(
                                {
                                    **run,
                                    "_page_key": f"{status}:{page}",
                                    "_page_sweep": sweep,
                                }
                            )
                    if completed_sweep:
                        page_sweeps[status] = sweep + 1
            except GitHubApiError as error:
                if attempt == 0 and workflow_id is not None and error.status == 404:
                    for key in (
                        "workflow_id",
                        "workflow_name",
                        "workflow_checked_at",
                    ):
                        discovery.pop(key, None)
                    continue
                raise
            discovery["run_page_cursor"] = page_cursors
            discovery["run_page_sweeps"] = page_sweeps
            return runs
        raise RuntimeError("workflow ID recovery exhausted")

    def _candidate_runs(
        self, runs: list[dict[str, Any]], state: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[int]]:
        newest = sorted(runs, key=_created_key, reverse=True)
        latency = newest[: self.args.newest_quota]
        latency_ids = {int(run["id"]) for run in latency}
        backlog = [
            run for run in runs if int(run["id"]) not in latency_ids
        ]
        if not backlog:
            return latency, []

        groups: dict[str, list[dict[str, Any]]] = {}
        for run in backlog:
            key = str(run.get("_page_key") or "legacy")
            groups.setdefault(key, []).append(run)
        for group in groups.values():
            group.sort(key=_created_key)

        offsets_raw = state.get("run_item_offsets")
        offsets = offsets_raw if isinstance(offsets_raw, dict) else {}
        offset_tick = int(state.get("run_offset_tick", 0)) + 1
        group_keys = sorted(groups)
        # Page 1 already contributes the latency quota on every refresh. Spend
        # the bounded backlog fetch capacity on the rotating older page when one
        # is present, and persist an independent offset for that exact
        # status/page. Otherwise a page change makes a run-ID cursor disappear
        # and restarts at the same first three entries forever.
        older_keys = [key for key in group_keys if not key.endswith(":1")]
        eligible_keys = older_keys or group_keys
        group_start = int(state.get("run_group_cursor", 0)) % len(eligible_keys)
        key = eligible_keys[group_start]
        group = groups[key]
        capacity = self.args.max_job_fetches - len(latency)
        selected: list[dict[str, Any]] = []
        entry = offsets.get(key)
        if isinstance(entry, dict):
            offset = int(entry.get("offset", 0)) % len(group)
        elif isinstance(entry, int):
            offset = entry % len(group)
        else:
            # An evicted page must not restart at zero on the next sweep. The
            # bounded per-status sweep survives offset-map pruning and advances
            # by the backlog capacity, which is coprime to the 100-run page.
            sweep = int(group[0].get("_page_sweep", 0))
            offset = (sweep * capacity) % len(group)
        for step in range(min(capacity, len(group))):
            selected.append(group[(offset + step) % len(group)])
        offsets[key] = {
            "offset": (offset + len(selected)) % len(group),
            "last_seen": offset_tick,
        }
        if len(offsets) > self.args.page_offset_cap:
            keep = sorted(
                offsets,
                key=lambda item: (
                    int(offsets[item].get("last_seen", 0))
                    if isinstance(offsets[item], dict)
                    else 0,
                    item,
                ),
                reverse=True,
            )[: self.args.page_offset_cap]
            offsets = {item: offsets[item] for item in keep}
        state["run_item_offsets"] = offsets
        state["run_offset_tick"] = offset_tick
        state["run_group_cursor"] = (group_start + 1) % len(eligible_keys)
        return [*latency, *selected], [int(run["id"]) for run in selected]

    def _discover(self) -> list[dict[str, Any]]:
        with self._discovery_lock():
            discovery = self._load_json(
                self.discovery_path,
                {"cursor_run_id": None, "run_page_cursor": {}},
            )
            fetched_at = int(discovery.get("fetched_at", 0))
            cached_runs = discovery.get("runs")
            if (
                not self.args.force_refresh
                and isinstance(cached_runs, list)
                and self.now - fetched_at < self.args.discovery_ttl
            ):
                return cached_runs

            # Namespace locks coalesce identical scans. This host-global lock
            # additionally prevents distinct repos/workflows from launching
            # simultaneous gh/ghapp bursts. On slower hosts those bursts can
            # all exceed their subprocess deadline and make healthy lanes look
            # scan-blind. A bounded wait fails closed instead of hanging the
            # supervisor or publishing a partial snapshot.
            with self._observation_lock():
                self.now = int(time.time())
                self.api_calls = 0
                runs = self._runs(discovery)
                candidates, backlog_ids = self._candidate_runs(runs, discovery)
                discovered: list[dict[str, Any]] = []
                for run in candidates[: self.args.max_job_fetches]:
                    run_id = int(run["id"])
                    jobs = self._gh(
                        f"repos/{self.args.repo}/actions/runs/{run_id}"
                        "/jobs?filter=latest&per_page=100"
                    ).get("jobs", [])
                    if not isinstance(jobs, list):
                        raise ValueError(f"jobs is not a list for run {run_id}")
                    discovered.append({**run, "_jobs": jobs})

                discovery["fetched_at"] = int(time.time())
                discovery["runs"] = discovered
                self._save_json(self.discovery_path, discovery)
                return discovered

    def _scan_locked(self) -> int:
        runs = self._discover()
        candidates = runs
        backlog_ids: list[int] = []
        negative_raw = self.state.get("negative")
        negative = negative_raw if isinstance(negative_raw, dict) else {}
        negative = {
            key: value
            for key, value in negative.items()
            if isinstance(value, dict)
            and self.now - int(value.get("checked_at", 0)) < self.args.negative_ttl
        }

        last_backlog_id: int | None = None
        matches = 0
        matched_job_ids: set[int] = set()
        backlog_id_set = set(backlog_ids)
        for run in candidates:
            run_id = int(run["id"])
            cached = negative.get(str(run_id))
            if (
                cached
                and cached.get("updated_at") == run.get("updated_at")
                and self.now - int(cached.get("checked_at", 0)) < self.args.negative_ttl
            ):
                if run_id in backlog_id_set:
                    last_backlog_id = run_id
                continue
            if run_id in backlog_id_set:
                last_backlog_id = run_id
            jobs = run.get("_jobs", [])
            if not isinstance(jobs, list):
                raise ValueError(f"jobs is not a list for run {run_id}")
            run_matches = 0
            for job in jobs:
                if (
                    not isinstance(job, dict)
                    or str(job.get("status", "")).lower() not in self.job_statuses
                ):
                    continue
                timestamp = str(
                    job.get("created_at")
                    or job.get("started_at")
                    or run.get("updated_at")
                    or run.get("created_at")
                    or ""
                )
                if not _fresh(timestamp, self.args.max_age_seconds):
                    continue
                if not _old_enough(timestamp, self.args.min_age_seconds, self.now):
                    continue
                if (
                    getattr(self.args, "exclude_assigned", 0)
                    and str(job.get("status", "")).lower() == "in_progress"
                    and job.get("runner_name")
                ):
                    continue
                job_labels = {
                    str(label).lower() for label in job.get("labels", []) if str(label)
                }
                if self.args.match_labels and (
                    not job_labels or not job_labels.issubset(self.labels)
                ):
                    continue
                if not self.args.match_labels or job_labels:
                    job_id = job.get("id")
                    if isinstance(job_id, int):
                        if job_id in matched_job_ids:
                            continue
                        matched_job_ids.add(job_id)
                    run_matches += 1
            if run_matches:
                matches += run_matches
                negative.pop(str(run_id), None)
                continue
            negative[str(run_id)] = {
                "checked_at": self.now,
                "updated_at": run.get("updated_at"),
            }

        cursor = last_backlog_id
        if cursor is None and isinstance(self.state.get("cursor_run_id"), int):
            cursor = self.state["cursor_run_id"]
        self._save_state(cursor, negative)
        return matches

    def scan(self) -> int:
        with self._state_lock():
            return self._scan_locked()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument(
        "--workflow",
        required=True,
        action="append",
        help=(
            "exact workflow name to scan; repeat for one shared-label lane "
            "serving multiple workflows"
        ),
    )
    parser.add_argument("--labels", required=True)
    parser.add_argument("--job-statuses", default="queued")
    parser.add_argument("--state-file", required=True)
    parser.add_argument(
        "--shared-cache-file",
        default=os.environ.get("TARTCI_SHARED_QUEUE_CACHE")
        or str(Path.home() / ".tartci/state/queue-discovery.json"),
    )
    parser.add_argument(
        "--observation-lock-file",
        default=os.environ.get("TARTCI_QUEUE_OBSERVATION_LOCK_FILE")
        or str(Path.home() / ".tartci/state/queue-observation.lock"),
        help="host-global lock shared by all queue-observation namespaces",
    )
    parser.add_argument(
        "--observation-lock-timeout",
        type=float,
        default=float(
            os.environ.get("TARTCI_QUEUE_OBSERVATION_LOCK_TIMEOUT_SECS", "120")
        ),
        help="seconds to wait for the host-global observation lock",
    )
    parser.add_argument("--provider", required=True)
    parser.add_argument(
        "--lane-id",
        default=os.environ.get("TARTCI_QUEUE_LANE_ID")
        or os.environ.get("TARTCI_RUNNER_SLOT")
        or f"supervisor-{os.getppid()}",
    )
    parser.add_argument("--gh-cli", default=os.environ.get("TARTCI_GH_CLI") or "gh")
    parser.add_argument("--gh-timeout", type=int, default=int(os.environ.get("TARTCI_GH_TIMEOUT_SECS", "15")))
    parser.add_argument("--max-run-pages", type=int, default=int(os.environ.get("TARTCI_QUEUE_RUN_PAGES", "2")))
    parser.add_argument("--max-job-fetches", type=int, default=int(os.environ.get("TARTCI_QUEUE_JOB_FETCHES", "5")))
    parser.add_argument("--newest-quota", type=int, default=int(os.environ.get("TARTCI_QUEUE_NEWEST_QUOTA", "2")))
    parser.add_argument("--max-api-calls", type=int, default=int(os.environ.get("TARTCI_QUEUE_MAX_API_CALLS", "14")))
    parser.add_argument("--workflow-cache-ttl", type=int, default=int(os.environ.get("TARTCI_QUEUE_WORKFLOW_CACHE_TTL_SECS", "86400")))
    parser.add_argument("--discovery-ttl", type=int, default=int(os.environ.get("TARTCI_QUEUE_DISCOVERY_TTL_SECS", "160")))
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="ignore a shared discovery cache hit for this safety-critical scan",
    )
    parser.add_argument("--expected-fleet-hosts", type=int, default=int(os.environ.get("TARTCI_QUEUE_EXPECTED_FLEET_HOSTS", "3")))
    parser.add_argument("--expected-discovery-namespaces", type=int, default=int(os.environ.get("TARTCI_QUEUE_EXPECTED_DISCOVERY_NAMESPACES", "4")))
    parser.add_argument("--page-offset-cap", type=int, default=int(os.environ.get("TARTCI_QUEUE_PAGE_OFFSET_CAP", "64")))
    # ghapp's installation core limit is 12,500/hour. Keep discovery below
    # 4,000 so at least 8,500 calls remain for Actions, registration, and
    # operator traffic instead of consuming the installation's full allowance.
    parser.add_argument("--fleet-api-budget-per-hour", type=int, default=int(os.environ.get("TARTCI_QUEUE_FLEET_API_BUDGET_PER_HOUR", "4000")))
    parser.add_argument("--stagger-max-seconds", type=int, default=int(os.environ.get("TARTCI_QUEUE_STAGGER_MAX_SECS", "4")))
    parser.add_argument("--negative-ttl", type=int, default=int(os.environ.get("TARTCI_QUEUE_NEGATIVE_TTL_SECS", "30")))
    parser.add_argument("--max-age-seconds", type=int, default=0)
    parser.add_argument("--min-age-seconds", type=int, default=0)
    parser.add_argument("--match-labels", type=int, choices=(0, 1), default=1)
    # Priority lanes count queued+in_progress to avoid a race where a priority
    # run's hosted resolver leg flips to in_progress before its self-hosted leg
    # is queued. But an in_progress job that ALREADY has a runner is being
    # served: it needs no further slot, and counting it makes every other host
    # in the fleet yield capacity to work that is already under way. Opt in per
    # caller so ordinary queue scans are unchanged.
    parser.add_argument("--exclude-assigned", type=int, choices=(0, 1), default=0)
    args = parser.parse_args()
    for field in (
        "gh_timeout",
        "max_run_pages",
        "max_job_fetches",
        "newest_quota",
        "max_api_calls",
        "workflow_cache_ttl",
        "discovery_ttl",
        "expected_fleet_hosts",
        "expected_discovery_namespaces",
        "page_offset_cap",
        "fleet_api_budget_per_hour",
        "negative_ttl",
    ):
        if getattr(args, field) <= 0:
            parser.error(f"--{field.replace('_', '-')} must be positive")
    if args.stagger_max_seconds < 0:
        parser.error("--stagger-max-seconds must be non-negative")
    if (
        not math.isfinite(args.observation_lock_timeout)
        or args.observation_lock_timeout <= 0
    ):
        parser.error("--observation-lock-timeout must be positive")
    if args.min_age_seconds < 0:
        parser.error("--min-age-seconds must be non-negative")
    statuses = {
        status.strip().lower()
        for status in args.job_statuses.split(",")
        if status.strip()
    }
    if not statuses or not statuses.issubset({"queued", "in_progress"}):
        parser.error("--job-statuses must contain queued and/or in_progress")
    if args.newest_quota >= args.max_job_fetches:
        parser.error("--newest-quota must be less than --max-job-fetches")
    # One workflow lookup, three status page windows, and job fetches. Reserve
    # two more calls for the bounded stale-workflow-ID recovery: the failed
    # status request plus the replacement workflow lookup.
    maximum_calls = 3 + (3 * args.max_run_pages) + args.max_job_fetches
    if maximum_calls > args.max_api_calls:
        parser.error(
            "--max-api-calls must cover workflow lookup, run pages, and job fetches "
            f"({maximum_calls} required)"
        )
    fleet_calls_per_hour = (
        ceil(3600 / args.discovery_ttl)
        * maximum_calls
        * args.expected_fleet_hosts
        * args.expected_discovery_namespaces
    )
    if fleet_calls_per_hour > args.fleet_api_budget_per_hour:
        parser.error(
            "shared discovery settings exceed fleet API budget "
            f"({fleet_calls_per_hour} > {args.fleet_api_budget_per_hour} calls/hour)"
        )
    return args


def main() -> int:
    args = parse_args()
    try:
        if args.stagger_max_seconds:
            identity = "\0".join(
                (
                    args.repo,
                    "\0".join(sorted(dict.fromkeys(args.workflow))),
                    args.labels,
                    args.provider,
                    args.lane_id,
                )
            )
            delay_ms = zlib.crc32(identity.encode("utf-8")) % (
                args.stagger_max_seconds * 1000
            )
            time.sleep(delay_ms / 1000)
        count = QueueScanner(args).scan()
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"queue scan failed ({args.provider}): {exc}", file=os.sys.stderr)
        return 1
    print(count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
