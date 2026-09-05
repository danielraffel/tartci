#!/usr/bin/env python3
"""Coverage for `--exclude-assigned`: an already-served job is not demand.

`priority_demand()` scans a priority workflow for both `queued` and
`in_progress` jobs. Counting `in_progress` is deliberate and guards a real
race — a priority run can flip to `in_progress` via its GitHub-hosted
resolver/classify job before its self-hosted leg is queued — but it also
means a job that already holds a runner reserves a *second* slot it will
never occupy. On the hosted resolver leg it will never occupy a self-hosted
slot at all.

The observed cost: a host with both VM slots free and a release job queued,
yielding indefinitely to a priority lane whose own supervisors reported
nothing queued.

These tests are deliberately fixture-driven rather than live. The live fleet
cannot distinguish the cases: whether `priority_demand=1` is correct depends
on whether the matching job is assigned, and that flips minute to minute. A
probe run against the real queue answers only "what is true right now", which
is exactly the sampling error this guard exists to remove.
"""
from __future__ import annotations

import argparse
import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "queue_scan.py"
SPEC = importlib.util.spec_from_file_location("queue_scan", MODULE_PATH)
assert SPEC and SPEC.loader
queue_scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue_scan)

RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"

PRIORITY_LABELS = [
    "self-hosted",
    "macOS",
    "ARM64",
    "pulp-build",
    "pulp-build-vm",
    "pulp-build-pr-head",
]

ORIGIN = datetime(2026, 9, 4, tzinfo=timezone.utc)
STAMP = ORIGIN.isoformat().replace("+00:00", "Z")


def _run(run_id: int) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": "Build and Test",
        "status": "in_progress",
        "created_at": STAMP,
        "updated_at": STAMP,
    }


def _api(job: dict[str, Any]) -> Callable[[str], dict[str, Any]]:
    """Serve exactly one priority run carrying exactly one job."""
    run = _run(4242)

    def api(path: str) -> dict[str, Any]:
        if path.endswith("/actions/workflows?per_page=100"):
            return {"workflows": [{"id": 99, "name": "Build and Test"}]}
        if "status=in_progress" in path or "status=queued" in path:
            return {"workflow_runs": [run]}
        if "status=pending" in path:
            return {"workflow_runs": []}
        if "/actions/runs/4242/jobs?" in path:
            return {"jobs": [job]}
        if "/jobs?" in path:
            return {"jobs": []}
        raise AssertionError(f"unexpected API path: {path}")

    return api


class PriorityDemandExcludeAssignedTests(unittest.TestCase):
    def _scan(self, job: dict[str, Any], exclude_assigned: int) -> int:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            values = {
                "repo": "owner/repo",
                "workflow": "Build and Test",
                "labels": ",".join(PRIORITY_LABELS),
                "job_statuses": "queued,in_progress",
                "state_file": str(state),
                "shared_cache_file": str(state.parent / "shared-discovery.json"),
                "observation_lock_file": str(state.parent / "host.lock"),
                "observation_lock_timeout": 2.0,
                "provider": "tart-macos",
                "lane_id": "test-slot-1-priority",
                "gh_cli": "unused",
                "gh_timeout": 5,
                "max_run_pages": 2,
                "max_job_fetches": 5,
                "newest_quota": 2,
                "max_api_calls": 14,
                "workflow_cache_ttl": 86400,
                "discovery_ttl": 160,
                "force_refresh": False,
                "expected_fleet_hosts": 3,
                "expected_discovery_namespaces": 4,
                "page_offset_cap": 64,
                "fleet_api_budget_per_hour": 4000,
                "stagger_max_seconds": 0,
                "negative_ttl": 300,
                "max_age_seconds": 0,
                "min_age_seconds": 0,
                "match_labels": 1,
                "exclude_assigned": exclude_assigned,
            }
            scanner = queue_scan.QueueScanner(argparse.Namespace(**values))
            scanner._gh = _api(job)
            return scanner.scan()

    # -- the defect --------------------------------------------------------

    def test_an_assigned_in_progress_job_is_not_demand(self) -> None:
        """The job holds a runner already; it does not need a second slot."""
        job = {
            "id": 1,
            "status": "in_progress",
            "runner_name": "studio-pulp-gate-01-65248-25",
            "labels": list(PRIORITY_LABELS),
        }
        self.assertEqual(self._scan(job, exclude_assigned=1), 0)

    def test_control_the_same_assigned_job_still_counts_without_the_flag(
        self,
    ) -> None:
        """Planted control for the test above, and it must go red on regression.

        Without it, a `0` could equally mean the fixture never produced a
        matching job at all — the failure mode where a check passes because it
        measured nothing. Same job, same labels, only the flag differs.
        """
        job = {
            "id": 1,
            "status": "in_progress",
            "runner_name": "studio-pulp-gate-01-65248-25",
            "labels": list(PRIORITY_LABELS),
        }
        self.assertEqual(self._scan(job, exclude_assigned=0), 1)

    # -- what must NOT change ---------------------------------------------

    def test_a_genuinely_queued_job_is_still_demand(self) -> None:
        """The legitimate yield must survive.

        A queued job with no runner is real, unserved priority demand, and the
        secondary lane should still stand aside for it. This is the case
        observed live at the moment of writing, and conflating it with the
        defect above would trade one wrong verdict for another.
        """
        job = {
            "id": 2,
            "status": "queued",
            "labels": list(PRIORITY_LABELS),
        }
        self.assertEqual(self._scan(job, exclude_assigned=1), 1)

    def test_an_unassigned_in_progress_job_is_still_demand(self) -> None:
        """The documented race guard is preserved.

        A run can be `in_progress` before its self-hosted leg is assigned. The
        exclusion keys on `runner_name`, not on status, precisely so this job
        keeps its reservation.
        """
        job = {
            "id": 3,
            "status": "in_progress",
            "labels": list(PRIORITY_LABELS),
        }
        self.assertEqual(self._scan(job, exclude_assigned=1), 1)

    def test_an_empty_runner_name_is_treated_as_unassigned(self) -> None:
        """GitHub returns `""` rather than omitting the key on some payloads."""
        job = {
            "id": 4,
            "status": "in_progress",
            "runner_name": "",
            "labels": list(PRIORITY_LABELS),
        }
        self.assertEqual(self._scan(job, exclude_assigned=1), 1)

    # -- the flag has to actually be wired --------------------------------

    def test_the_priority_scan_passes_the_flag(self) -> None:
        """A guard that is present but unwired is not a guard.

        The flag defaults to off, so adding it to `queue_scan.py` alone changes
        nothing at all. This asserts the priority call site opts in, which is
        the half that a code-only review misses.
        """
        body = RUNNER.read_text(encoding="utf-8")
        marker = "--provider tart-macos-priority \\"
        self.assertIn(marker, body)
        after = body.split(marker, 1)[1][:400]
        self.assertIn("--exclude-assigned 1", after)

    def test_a_namespace_without_the_option_keeps_the_old_behaviour(self) -> None:
        """In-process callers predate the option and must not start raising.

        `QueueScanner` is constructed directly from an `argparse.Namespace` by
        several suites and by in-process consumers, none of which know about a
        newly added field. Reading it off `self.args` unguarded turns every one
        of them into an `AttributeError` — which is how this landed the first
        time, breaking fifteen unrelated tests that never touch priority
        demand. The option resolves once, tolerantly, defaulting to off.
        """
        job = {
            "id": 5,
            "status": "in_progress",
            "runner_name": "studio-pulp-gate-01-65248-25",
            "labels": list(PRIORITY_LABELS),
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            values = {
                "repo": "owner/repo",
                "workflow": "Build and Test",
                "labels": ",".join(PRIORITY_LABELS),
                "job_statuses": "queued,in_progress",
                "state_file": str(state),
                "shared_cache_file": str(state.parent / "shared-discovery.json"),
                "observation_lock_file": str(state.parent / "host.lock"),
                "observation_lock_timeout": 2.0,
                "provider": "tart-macos",
                "lane_id": "legacy-caller",
                "gh_cli": "unused",
                "gh_timeout": 5,
                "max_run_pages": 2,
                "max_job_fetches": 5,
                "newest_quota": 2,
                "max_api_calls": 14,
                "workflow_cache_ttl": 86400,
                "discovery_ttl": 160,
                "force_refresh": False,
                "expected_fleet_hosts": 3,
                "expected_discovery_namespaces": 4,
                "page_offset_cap": 64,
                "fleet_api_budget_per_hour": 4000,
                "stagger_max_seconds": 0,
                "negative_ttl": 300,
                "max_age_seconds": 0,
                "min_age_seconds": 0,
                "match_labels": 1,
                # deliberately no "exclude_assigned"
            }
            scanner = queue_scan.QueueScanner(argparse.Namespace(**values))
            scanner._gh = _api(job)
            self.assertEqual(scanner.scan(), 1)

    def test_the_flag_defaults_to_off(self) -> None:
        """Changing what counts as demand is a scheduling decision.

        Every other caller of `queue_scan.py` — including the plain queued-work
        scan, where an assigned job is a different question entirely — must be
        unaffected by an upgrade that merely installs this flag.
        """
        body = (ROOT / "scripts" / "queue_scan.py").read_text(encoding="utf-8")
        self.assertIn(
            '"--exclude-assigned", type=int, choices=(0, 1), default=0',
            body,
        )


if __name__ == "__main__":
    unittest.main()
