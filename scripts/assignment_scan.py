#!/usr/bin/env python3
"""Fail-closed queue scan for exclusive JIT assignment classes.

The scan answers one question: does this assignment class have queued demand?
Presence and absence are not symmetric answers to it. A single matching job is
a complete proof of presence — no further looking can retract it — while
absence is only ever established by looking everywhere. So the scan stops at
the first witness and reports `1`, and pays the exhaustive pass only to report
`0`.

That asymmetry is what keeps the scan affordable without weakening it. Every
caller tests the result against zero, so a witness carries the same decision a
full count would, and the expensive exhaustive pass is still mandatory before
any claim that there is nothing to serve — which is the claim that would idle a
lane with work waiting. Pass --exhaustive-count for the true magnitude when
diagnosing; it is not needed to make a decision.

The asymmetry governs the whole pass, not only its last phase. Enumerating
every run before reading a single job spends the listing calls, and the
snapshot reconciliation that makes those listings trustworthy, whether or not
the first run already settles the question. The witness then cannot arrive
until the most failure-prone part of the scan is already paid for. So the walk
hands each page of runs to the job scan as that page arrives, takes the
listings whose runs are `queued` before the ones that are `in_progress`, and
abandons the rest the moment a witness appears. Reconciliation is what makes an
empty listing believable, so it is enforced whenever a listing is walked to its
end, and skipped only where a witness has already made absence moot.

Resolving a workflow name to its id is the one input that does not change
between polls, so it is read from a short-lived host-shared cache instead of
being re-fetched every time. An id that no longer resolves fails the scan
closed on its own listing call, so the cache cannot turn a broken lookup into
an empty queue; the bounded lifetime covers the narrower case of a second
workflow appearing under an already-cached display name.
"""
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
_RETRY_BACKOFF_CAP = 4.0


class ScanError(RuntimeError):
    """The queue could not be observed completely and authoritatively."""


class TransientApiFault(ScanError):
    """One API call failed in a way a fresh attempt can resolve.

    A dropped connection, a TLS handshake that never completed, a call killed
    at its own timeout: none of these are evidence about the queue, so
    abandoning the whole scan on the first one throws away an observation that
    a retry would have completed. Retrying is not failing open — an exhausted
    retry budget still raises, and the scan still fails closed.
    """


class WitnessAbandon(Exception):
    """A matching queued job was seen, so the walk in progress can stop here.

    Carried out of a listing walk rather than returned, because the walk is
    several frames deep in pagination when the witness lands. It is not a
    ScanError: nothing failed, and the scan reports presence.
    """


class PaginationRace(ScanError):
    """The listing mutated between pages, so this pass never saw one snapshot.

    Runs enter and leave `queued`/`in_progress` continuously on an active
    repository, so `total_count` and the page bodies are not read atomically.
    That is churn in the thing being measured, not API unreliability, and a
    fresh pass usually lands between edits. Accepting a torn read instead
    would under-count the queue, which is the one error this scanner exists to
    prevent, so the pass is retried rather than salvaged.
    """


def _object_list(payload: Any, key: str, path: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ScanError(f"GitHub API returned non-object for {path}")
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ScanError(f"GitHub API returned invalid {key} for {path}")
    return value


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
        # Provisional only: --scan-timeout budgets the SCAN, and the scan has
        # not started yet. _observation_lock rebases this the instant the lock
        # is held, so waiting for the lock never eats the scan's budget. The
        # provisional value still bounds anything that reads the deadline
        # before acquisition, so no path is ever unbounded.
        self.deadline = time.monotonic() + args.scan_timeout
        self.observation_lock_path = Path(args.observation_lock_file)
        self.api_calls = 0
        self.api_calls_lock = threading.Lock()
        self.observation_lock_fd: int | None = None
        self.witness = threading.Event()
        self.workflow_id_cache_path = Path(args.workflow_id_cache_file)

    @contextlib.contextmanager
    def _observation_lock(self) -> Any:
        self.observation_lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_deadline = time.monotonic() + self.args.observation_lock_timeout
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
            # The wait is over, so the scan begins now. Budgeting the scan from
            # invocation instead charged it for however long the queue behind
            # this host-global lock happened to be, which is the one quantity
            # the scan does not control: a lane that waited most of its budget
            # then ran an exhaustive pass on the remainder, and the leftover
            # was handed to `gh` as a shortened per-call timeout, so the scan
            # died mid-pass and reported the queue unobservable. The total is
            # still bounded -- by --observation-lock-timeout plus
            # --scan-timeout, each explicit.
            self.deadline = time.monotonic() + self.args.scan_timeout
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

    def _gh_once(self, path: str) -> dict[str, Any]:
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
            raise TransientApiFault(
                f"GitHub API unavailable for {path}: {error}"
            ) from error
        if result.returncode:
            raise TransientApiFault(
                f"GitHub API failed for {path}: {result.stderr.strip()}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise TransientApiFault(
                f"GitHub API returned invalid JSON for {path}"
            ) from error
        if not isinstance(payload, dict):
            raise ScanError(f"GitHub API returned non-object for {path}")
        return payload

    def _retry_sleep(self, attempt: int) -> bool:
        """Back off before the next attempt, or report that none is affordable.

        The nap is clamped to the time actually left, so a retry can never push
        the scan past the deadline the caller budgeted for it.
        """
        delay = min(self.args.retry_backoff * (2 ** (attempt - 1)), _RETRY_BACKOFF_CAP)
        remaining = self.deadline - time.monotonic()
        if remaining <= delay:
            return False
        time.sleep(delay)
        return True

    def _gh(self, path: str) -> dict[str, Any]:
        """Read one API page, retrying only faults that carry no queue verdict."""
        attempts = self.args.api_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return self._gh_once(path)
            except TransientApiFault as error:
                if attempt == attempts or not self._retry_sleep(attempt):
                    raise ScanError(
                        f"{error} (after {attempt} attempt(s))"
                    ) from error
        raise AssertionError("unreachable")

    def _pages(self, path_prefix: str, key: str) -> list[dict[str, Any]]:
        """Read a whole listing into memory, restarting a torn pass.

        The retry is of the entire pagination, never of one page: pages are
        only consistent with each other within a single pass, so resuming a
        torn read mid-way would splice two different snapshots together. Each
        attempt therefore collects into a fresh list.
        """
        attempts = self.args.pagination_retries + 1
        for attempt in range(1, attempts + 1):
            items: list[dict[str, Any]] = []
            try:
                self._walk_listing_once(path_prefix, key, items.extend)
                return items
            except PaginationRace as error:
                if attempt == attempts or not self._retry_sleep(attempt):
                    raise ScanError(
                        f"{error} (after {attempt} pagination attempt(s))"
                    ) from error
        raise AssertionError("unreachable")

    def _walk_listing_once(self, path_prefix: str, key: str, visit: Any) -> None:
        """Page a listing, handing each page to `visit` the moment it arrives.

        The reconciliation is the same one a whole-listing read performs: a
        stable total_count, no repeated ids, a body that adds up to that total,
        and a re-read of the first page once the listing spanned more than one.
        Those checks are what make an empty listing mean the queue is empty.
        What differs is when the pages are used. `visit` sees each page before
        the next is requested, so it can raise WitnessAbandon and leave the
        remaining pages, and the reconciliation, unbought. That is sound only
        because abandoning happens exactly when presence is already proved, and
        a proof of presence is not something a fuller reading could retract.
        """
        seen_ids: set[int] = set()
        expected_total: int | None = None
        first_page_ids: tuple[int, ...] = ()
        delivered = 0
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
                raise PaginationRace(
                    f"GitHub API total_count changed during pagination for {path_prefix}"
                )
            page_ids: list[int] = []
            for item in page_items:
                item_id = item.get("id")
                if not isinstance(item_id, int):
                    raise ScanError(f"GitHub API returned item without integer id for {path}")
                if item_id in seen_ids:
                    raise PaginationRace(
                        f"GitHub API returned duplicate id during pagination for {path_prefix}"
                    )
                seen_ids.add(item_id)
                page_ids.append(item_id)
            if page == 1:
                first_page_ids = tuple(page_ids)
            visit(page_items)
            delivered += len(page_items)
            if delivered >= total:
                if delivered != total:
                    raise PaginationRace(
                        f"GitHub API pagination exceeded total_count for {path}"
                    )
                if page > 1:
                    verify_path = f"{path_prefix}{separator}per_page={PER_PAGE}&page=1"
                    verify_payload = self._gh(verify_path)
                    verify_items = _object_list(verify_payload, key, verify_path)
                    verify_total = verify_payload.get("total_count")
                    verify_ids = tuple(item.get("id") for item in verify_items)
                    if verify_total != expected_total or verify_ids != first_page_ids:
                        raise PaginationRace(
                            f"GitHub API first page changed during pagination for {path_prefix}"
                        )
                return
            if len(page_items) < PER_PAGE:
                if delivered < total:
                    raise PaginationRace(
                        f"GitHub API pagination ended before total_count for {path} "
                        f"({delivered} < {total})"
                    )
                return
        raise ScanError(
            f"GitHub API pagination truncated at {self.args.max_pages} pages for {path_prefix}"
        )

    def _walk_listing(self, path_prefix: str, key: str, visit: Any) -> None:
        """Walk a listing, restarting a pass that the queue moved under.

        `visit` is called again for the pages a restarted pass re-reads, so it
        must tolerate seeing a run twice. The caller holds the scanned-run set
        that makes the repeat free.
        """
        attempts = self.args.pagination_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                self._walk_listing_once(path_prefix, key, visit)
                return
            except PaginationRace as error:
                if attempt == attempts or not self._retry_sleep(attempt):
                    raise ScanError(
                        f"{error} (after {attempt} pagination attempt(s))"
                    ) from error
        raise AssertionError("unreachable")

    def _cached_workflow_ids(self) -> dict[str, int] | None:
        """Return every configured workflow's id from cache, or None.

        All or nothing: a partial hit still needs the listing call a full hit
        exists to avoid, so it is not a hit. An unreadable, malformed or
        expired cache reads as a miss and the scan resolves the ids live.
        """
        ttl = self.args.workflow_id_cache_ttl
        if ttl <= 0:
            return None
        try:
            payload = json.loads(
                self.workflow_id_cache_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        entry = payload.get(self.args.repo)
        if not isinstance(entry, dict):
            return None
        fetched_at = entry.get("fetched_at")
        if not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool):
            return None
        age = time.time() - fetched_at
        if not 0 <= age < ttl:
            return None
        workflows = entry.get("workflows")
        if not isinstance(workflows, dict):
            return None
        resolved: dict[str, int] = {}
        for name in self.workflows:
            workflow_id = workflows.get(name)
            if not isinstance(workflow_id, int) or isinstance(workflow_id, bool):
                return None
            resolved[name] = workflow_id
        return resolved

    def _store_workflow_ids(self, resolved: dict[str, int]) -> None:
        """Publish the resolved ids for the other lanes sharing this host.

        Best effort by construction: the cache only ever saves a call, so a
        host that cannot write one keeps scanning correctly and pays for the
        listing on every poll.
        """
        if self.args.workflow_id_cache_ttl <= 0:
            return
        try:
            self.workflow_id_cache_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                payload = json.loads(
                    self.workflow_id_cache_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload[self.args.repo] = {
                "fetched_at": time.time(),
                "workflows": dict(resolved),
            }
            tmp = self.workflow_id_cache_path.with_name(
                f"{self.workflow_id_cache_path.name}.{os.getpid()}.tmp"
            )
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.workflow_id_cache_path)
        except OSError:
            return

    def _ordered_run_listings(self, workflow_ids: dict[str, int]) -> list[str]:
        """Order the run listings by how likely each is to end the scan early.

        A run that is itself `queued` is where a queued job most often sits, so
        those listings come before the `in_progress` ones for every configured
        workflow. An `in_progress` run can still hold a queued job and is still
        read before any claim of absence; it is only read later.
        """
        ordered: list[str] = []
        for status in ("queued", "in_progress"):
            seen: set[int] = set()
            for workflow_id in workflow_ids.values():
                if workflow_id in seen:
                    continue
                seen.add(workflow_id)
                ordered.append(
                    f"repos/{self.args.repo}/actions/workflows/{workflow_id}/runs"
                    f"?status={status}"
                )
        return ordered

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

    def _scan_run(self, run: dict[str, Any]) -> int:
        if self.witness.is_set():
            # Another run already produced a witness, so this run cannot change
            # the verdict. Returning before the request is the whole saving.
            return 0
        matches: set[int] = set()
        run_id = int(run["id"])
        prefix = (
            f"repos/{self.args.repo}/actions/runs/{run_id}"
            "/jobs?filter=latest"
        )
        for job in self._pages(prefix, "jobs"):
            if str(job.get("status", "")).lower() != "queued":
                continue
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
            if not self.args.exhaustive_count:
                # One witness settles the question the caller actually asks.
                self.witness.set()
                return len(matches)
        return len(matches)

    def scan(self) -> int:
        # Every fleet supervisor shares this host-global observation slot. The
        # exhaustive scanner can issue many GitHub calls; overlapping scans for
        # different lanes made individually healthy ghapp requests time out and
        # left the host scan-blind. The wait is bounded separately from the scan
        # budget, so queueing behind other lanes costs this scan time to finish
        # but never time to work; failure remains fail-closed either way.
        # Reading the workflow ids from cache is file IO, so it happens
        # before the host-global lock rather than under it.
        workflow_ids = self._cached_workflow_ids()
        total = 0
        with self._observation_lock():
            if workflow_ids is None:
                workflow_ids = self._workflow_ids()
                self._store_workflow_ids(workflow_ids)
            scanned: set[int] = set()

            # Concurrency remains opt-in for a measured host. The reliable
            # fleet default is one call stream; raising it multiplies pressure
            # inside the host-global scan and must not happen accidentally.
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.args.max_workers
            ) as executor:

                def visit(page_items: list[dict[str, Any]]) -> None:
                    nonlocal total
                    fresh: list[dict[str, Any]] = []
                    for run in page_items:
                        run_id = run.get("id")
                        if not isinstance(run_id, int):
                            raise ScanError("configured workflow run has no integer id")
                        # A run reachable from two listings, or re-read by a
                        # restarted pass, is scanned once.
                        if run_id in scanned:
                            continue
                        scanned.add(run_id)
                        fresh.append(run)
                    if not fresh:
                        return
                    total += sum(executor.map(self._scan_run, fresh), start=0)
                    if self.witness.is_set():
                        raise WitnessAbandon

                try:
                    for prefix in self._ordered_run_listings(workflow_ids):
                        self._walk_listing(prefix, "workflow_runs", visit)
                except WitnessAbandon:
                    pass
        if self.witness.is_set():
            # Deliberately not the running total: the scan stopped early, so the
            # total is a partial count and reporting it would invent precision
            # the observation does not have. Callers test this against zero.
            return 1
        return total


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workflow", required=True, action="append")
    parser.add_argument("--labels", required=True)
    parser.add_argument("--require-label", required=True)
    parser.add_argument("--min-age-seconds", type=int, default=0)
    parser.add_argument(
        "--exhaustive-count",
        action="store_true",
        default=os.environ.get("TARTCI_ASSIGNMENT_SCAN_EXHAUSTIVE_COUNT") == "1",
    )
    parser.add_argument("--gh-cli", default=os.environ.get("TARTCI_GH_CLI") or "gh")
    parser.add_argument(
        "--api-retries",
        type=int,
        default=int(os.environ.get("TARTCI_ASSIGNMENT_SCAN_API_RETRIES", "2")),
    )
    parser.add_argument(
        "--pagination-retries",
        type=int,
        default=int(os.environ.get("TARTCI_ASSIGNMENT_SCAN_PAGINATION_RETRIES", "2")),
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=float(os.environ.get("TARTCI_ASSIGNMENT_SCAN_RETRY_BACKOFF_SECS", "0.5")),
    )
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
        "--workflow-id-cache-file",
        default=os.environ.get("TARTCI_ASSIGNMENT_WORKFLOW_ID_CACHE_FILE")
        or str(Path.home() / ".tartci/state/assignment-workflow-ids.json"),
    )
    parser.add_argument(
        "--workflow-id-cache-ttl",
        type=float,
        default=float(
            os.environ.get("TARTCI_ASSIGNMENT_WORKFLOW_ID_CACHE_TTL_SECS", "300")
        ),
    )
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
    if not 0 <= args.api_retries <= 8:
        parser.error("--api-retries must be between 0 and 8")
    if not 0 <= args.pagination_retries <= 8:
        parser.error("--pagination-retries must be between 0 and 8")
    if not math.isfinite(args.retry_backoff) or args.retry_backoff < 0:
        parser.error("--retry-backoff must be non-negative")
    if (
        not math.isfinite(args.observation_lock_timeout)
        or args.observation_lock_timeout <= 0
    ):
        parser.error("--observation-lock-timeout must be positive")
    if args.min_age_seconds < 0:
        parser.error("--min-age-seconds must be non-negative")
    if not math.isfinite(args.workflow_id_cache_ttl):
        parser.error("--workflow-id-cache-ttl must be finite")
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
