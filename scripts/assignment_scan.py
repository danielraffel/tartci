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
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


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
                yield
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
            result = subprocess.run(
                [self.args.gh_cli, "api", path],
                capture_output=True,
                text=True,
                timeout=min(self.args.gh_timeout, remaining),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
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
                return sum(executor.map(self._scan_run, runs), start=0)


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
