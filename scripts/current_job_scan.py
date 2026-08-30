#!/usr/bin/env python3
"""Resolve one active GitHub Actions runner assignment, bounded and fail closed."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any


PER_PAGE = 100


class ScanError(RuntimeError):
    """The live assignment could not be observed completely."""


def _items(payload: Any, key: str, path: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ScanError(f"GitHub API returned non-object for {path}")
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ScanError(f"GitHub API returned invalid {key} for {path}")
    return value


class CurrentJobScanner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.workflows = set(args.workflow)
        self.deadline = time.monotonic() + args.scan_timeout
        self.api_calls = 0

    def _gh(self, path: str) -> dict[str, Any]:
        if self.api_calls >= self.args.max_api_calls:
            raise ScanError(f"current-job API budget exhausted ({self.args.max_api_calls})")
        self.api_calls += 1
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ScanError("current-job scan exceeded its overall deadline")
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
        first_page_ids: tuple[int, ...] = ()
        for page in range(1, self.args.max_pages + 1):
            separator = "&" if "?" in prefix else "?"
            path = f"{prefix}{separator}per_page={PER_PAGE}&page={page}"
            payload = self._gh(path)
            page_items = _items(payload, key, path)
            total = payload.get("total_count")
            if not isinstance(total, int) or total < 0:
                raise ScanError(f"GitHub API returned invalid total_count for {path}")
            if expected_total is None:
                expected_total = total
                if total > self.args.result_cap:
                    raise ScanError(
                        f"GitHub API result exceeds current-job cap ({total} > {self.args.result_cap})"
                    )
            elif total != expected_total:
                raise ScanError(f"GitHub API total_count changed during pagination for {prefix}")
            page_ids: list[int] = []
            for item in page_items:
                item_id = item.get("id")
                if not isinstance(item_id, int):
                    raise ScanError(f"GitHub API returned item without integer id for {path}")
                if item_id in seen:
                    raise ScanError(f"GitHub API returned duplicate id during pagination for {prefix}")
                seen.add(item_id)
                page_ids.append(item_id)
                result.append(item)
            if page == 1:
                first_page_ids = tuple(page_ids)
            if len(result) >= total:
                if len(result) != total:
                    raise ScanError(f"GitHub API pagination exceeded total_count for {path}")
                if page > 1:
                    verify_path = f"{prefix}{separator}per_page={PER_PAGE}&page=1"
                    verify_payload = self._gh(verify_path)
                    verify_items = _items(verify_payload, key, verify_path)
                    verify_total = verify_payload.get("total_count")
                    verify_ids = tuple(item.get("id") for item in verify_items)
                    if verify_total != expected_total or verify_ids != first_page_ids:
                        raise ScanError(
                            f"GitHub API first page changed during pagination for {prefix}"
                        )
                return result
            if len(page_items) < PER_PAGE:
                raise ScanError(
                    f"GitHub API pagination ended before total_count for {path} "
                    f"({len(result)} < {total})"
                )
        raise ScanError(
            f"GitHub API pagination truncated at {self.args.max_pages} pages for {prefix}"
        )

    def scan(self) -> tuple[int, int, str] | None:
        runs = self._pages(
            f"repos/{self.args.repo}/actions/runs?status=in_progress", "workflow_runs"
        )
        matches: list[tuple[int, int, str]] = []
        for run in runs:
            workflow = run.get("name")
            if workflow not in self.workflows:
                continue
            run_id = int(run["id"])
            jobs = self._pages(
                f"repos/{self.args.repo}/actions/runs/{run_id}/jobs?filter=latest", "jobs"
            )
            for job in jobs:
                if job.get("runner_name") != self.args.runner:
                    continue
                if str(job.get("status", "")).lower() != "in_progress":
                    continue
                job_id = job.get("id")
                if not isinstance(job_id, int):
                    raise ScanError(f"matching job in run {run_id} has no integer id")
                matches.append((run_id, job_id, workflow))
        if len(matches) > 1:
            raise ScanError(
                f"runner {self.args.runner!r} matched {len(matches)} active jobs"
            )
        return matches[0] if matches else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--runner", required=True)
    parser.add_argument("--workflow", required=True, action="append")
    parser.add_argument("--gh-cli", default=os.environ.get("TARTCI_GH_CLI") or "gh")
    parser.add_argument("--gh-timeout", type=int, default=15)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--scan-timeout", type=int, default=60)
    parser.add_argument("--result-cap", type=int, default=300)
    parser.add_argument("--max-api-calls", type=int, default=310)
    args = parser.parse_args()
    for field in ("gh_timeout", "max_pages", "scan_timeout", "result_cap", "max_api_calls"):
        if getattr(args, field) <= 0:
            parser.error(f"--{field.replace('_', '-')} must be positive")
    return args


def main() -> int:
    try:
        match = CurrentJobScanner(parse_args()).scan()
    except ScanError as error:
        print(f"current-job scan failed closed: {error}", file=sys.stderr)
        return 2
    if match is None:
        return 1
    print("\t".join(str(value) for value in match))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
