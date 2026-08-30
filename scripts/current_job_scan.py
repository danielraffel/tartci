#!/usr/bin/env python3
"""Observe one ephemeral runner assignment without guessing across API churn."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

PER_PAGE = 100


class ScanError(RuntimeError):
    """The assignment could not be observed completely and authoritatively."""


def _objects(payload: Any, key: str, path: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ScanError(f"GitHub API returned non-object for {path}")
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ScanError(f"GitHub API returned invalid {key} for {path}")
    return value


class CurrentJobScanner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.configured_names = tuple(dict.fromkeys(args.workflow))
        self.deadline = time.monotonic() + args.scan_timeout
        self.api_calls = 0
        self.api_lock = threading.Lock()
        self.page_fingerprints: dict[str, tuple[tuple[int, ...], ...]] = {}

    def _gh(self, path: str) -> dict[str, Any]:
        with self.api_lock:
            if self.api_calls >= self.args.max_api_calls:
                raise ScanError(f"API budget exhausted ({self.args.max_api_calls})")
            self.api_calls += 1
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ScanError("scan exceeded its overall deadline")
        try:
            result = subprocess.run(
                [self.args.gh_cli, "api", path], capture_output=True, text=True,
                timeout=min(self.args.gh_timeout, remaining), check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ScanError(f"GitHub API unavailable for {path}: {error}") from error
        if result.returncode:
            raise ScanError(f"GitHub API failed for {path}: {result.stderr.strip()}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ScanError(f"GitHub API returned invalid JSON for {path}") from error
        if not isinstance(payload, dict):
            raise ScanError(f"GitHub API returned non-object for {path}")
        return payload

    def _pages(self, prefix: str, key: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[int] = set()
        expected_total: int | None = None
        first_ids: tuple[int, ...] = ()
        page_ids: list[tuple[int, ...]] = []
        separator = "&" if "?" in prefix else "?"
        for page in range(1, self.args.max_pages + 1):
            path = f"{prefix}{separator}per_page={PER_PAGE}&page={page}"
            payload = self._gh(path)
            items = _objects(payload, key, path)
            total = payload.get("total_count")
            if not isinstance(total, int) or total < 0:
                raise ScanError(f"GitHub API returned invalid total_count for {path}")
            if total > self.args.result_cap:
                raise ScanError(f"result exceeds cap for {prefix} ({total} > {self.args.result_cap})")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise ScanError(f"total_count changed during pagination for {prefix}")
            ids: list[int] = []
            for item in items:
                item_id = item.get("id")
                if not isinstance(item_id, int):
                    raise ScanError(f"item lacks integer id for {path}")
                if item_id in seen:
                    raise ScanError(f"duplicate id during pagination for {prefix}")
                seen.add(item_id)
                ids.append(item_id)
                result.append(item)
            if page == 1:
                first_ids = tuple(ids)
            page_ids.append(tuple(ids))
            if len(result) == total:
                if page > 1:
                    verify_path = f"{prefix}{separator}per_page={PER_PAGE}&page=1"
                    verify = self._gh(verify_path)
                    verify_ids = tuple(item.get("id") for item in _objects(verify, key, verify_path))
                    if verify.get("total_count") != expected_total or verify_ids != first_ids:
                        raise ScanError(f"first page changed during pagination for {prefix}")
                self.page_fingerprints[prefix] = tuple(page_ids)
                return result
            if len(result) > total or len(items) < PER_PAGE:
                raise ScanError(f"pagination inconsistent for {prefix} ({len(result)} of {total})")
        raise ScanError(f"pagination truncated at {self.args.max_pages} pages for {prefix}")

    def _workflow_contract(self) -> dict[int, str]:
        workflows = self._pages(f"repos/{self.args.repo}/actions/workflows", "workflows")
        by_name: dict[str, list[int]] = {name: [] for name in self.configured_names}
        for workflow in workflows:
            name, workflow_id = workflow.get("name"), workflow.get("id")
            if name in by_name:
                if not isinstance(workflow_id, int):
                    raise ScanError(f"configured workflow {name!r} lacks integer id")
                by_name[name].append(workflow_id)
        contract: dict[int, str] = {}
        for name, ids in by_name.items():
            if len(ids) != 1:
                raise ScanError(f"configured workflow {name!r} resolved to {len(ids)} ids")
            contract[ids[0]] = name
        return contract

    @staticmethod
    def _receipt(kind: str, **values: Any) -> dict[str, Any]:
        return {"kind": kind, **values}

    def _confirm(self, run_id: int, job_id: int, contract: dict[int, str]) -> dict[str, Any]:
        job = self._gh(f"repos/{self.args.repo}/actions/jobs/{job_id}")
        run = self._gh(f"repos/{self.args.repo}/actions/runs/{run_id}")
        if job.get("id") != job_id or run.get("id") != run_id:
            raise ScanError("exact assignment revalidation returned different ids")
        status = str(job.get("status") or "").lower()
        runner = job.get("runner_name")
        workflow_id = run.get("workflow_id")
        workflow_name = run.get("name")
        run_status = str(run.get("status") or "").lower()
        base = {"run_id": run_id, "job_id": job_id, "workflow_id": workflow_id,
                "workflow_name": workflow_name, "runner_name": runner,
                "job_status": status, "run_status": run_status,
                "run_conclusion": run.get("conclusion")}
        if runner != self.args.runner:
            return self._receipt("assignment_changed", **base)
        if status == "completed":
            if run_status != "completed":
                return self._receipt("terminal_pending_run", conclusion=job.get("conclusion"), **base)
            return self._receipt("terminal", conclusion=job.get("conclusion"), **base)
        if status != "in_progress":
            return self._receipt("assignment_changed", **base)
        if not isinstance(workflow_id, int) or contract.get(workflow_id) != workflow_name:
            return self._receipt("unexpected_assignment", **base)
        return self._receipt("active", **base)

    def discover(self) -> dict[str, Any]:
        contract = self._workflow_contract()
        # Repository-wide discovery is load-bearing: filtering expected workflows
        # first makes an unexpected assignment indistinguishable from idle.
        runs = self._pages(
            f"repos/{self.args.repo}/actions/runs?status=in_progress", "workflow_runs"
        )
        matches = self._matches(runs)
        if not matches:
            # A same-count insertion/removal can preserve page 1 while moving an
            # assignment across a deeper boundary. A negative verdict therefore
            # requires a second complete, identical scheduler snapshot.
            first = dict(self.page_fingerprints)
            runs = self._pages(
                f"repos/{self.args.repo}/actions/runs?status=in_progress", "workflow_runs"
            )
            matches = self._matches(runs)
            if not matches and self.page_fingerprints != first:
                raise ScanError("scheduler pages changed while confirming no assignment")
        if len(matches) > 1:
            return self._receipt("ambiguous_assignment", matches=len(matches))
        if not matches:
            return self._receipt("no_assignment")
        return self._confirm(*matches[0], contract)

    def _matches(self, runs: list[dict[str, Any]]) -> list[tuple[int, int]]:
        run_ids: list[int] = []
        for run in runs:
            run_id = run.get("id")
            if not isinstance(run_id, int):
                raise ScanError("in-progress run lacks integer id")
            run_ids.append(run_id)

        def fetch(run_id: int) -> tuple[int, list[dict[str, Any]]]:
            return run_id, self._pages(
                f"repos/{self.args.repo}/actions/runs/{run_id}/jobs?filter=latest", "jobs"
            )

        matches: list[tuple[int, int]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.args.parallelism) as pool:
            futures = [pool.submit(fetch, run_id) for run_id in run_ids]
            for future in concurrent.futures.as_completed(futures):
                run_id, jobs = future.result()
                for job in jobs:
                    if job.get("runner_name") == self.args.runner and str(job.get("status", "")).lower() == "in_progress":
                        job_id = job.get("id")
                        if not isinstance(job_id, int):
                            raise ScanError(f"matching job in run {run_id} lacks integer id")
                        matches.append((run_id, job_id))
        return matches

    def revalidate(self) -> dict[str, Any]:
        return self._confirm(self.args.run_id, self.args.job_id, self._workflow_contract())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--runner", required=True)
    parser.add_argument("--workflow", required=True, action="append")
    parser.add_argument("--mode", choices=("discover", "revalidate"), default="discover")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--gh-cli", default=os.environ.get("TARTCI_GH_CLI") or "gh")
    parser.add_argument("--gh-timeout", type=float, default=5)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--scan-timeout", type=float, default=10)
    parser.add_argument("--result-cap", type=int, default=300)
    parser.add_argument("--max-api-calls", type=int, default=310)
    parser.add_argument("--parallelism", type=int, default=12)
    args = parser.parse_args()
    for field in ("gh_timeout", "max_pages", "scan_timeout", "result_cap", "max_api_calls", "parallelism"):
        if getattr(args, field) <= 0:
            parser.error(f"--{field.replace('_', '-')} must be positive")
    if args.mode == "revalidate" and (not args.run_id or not args.job_id):
        parser.error("--run-id and --job-id are required for revalidate")
    return args


def main() -> int:
    try:
        args = parse_args()
        scanner = CurrentJobScanner(args)
        receipt = scanner.discover() if args.mode == "discover" else scanner.revalidate()
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    except ScanError as error:
        print(json.dumps({"kind": "observation_error", "detail": str(error)},
                         sort_keys=True, separators=(",", ":")))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
