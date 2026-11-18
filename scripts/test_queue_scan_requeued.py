#!/usr/bin/env python3
"""An old run whose dependent job is newly queued is demand on every refresh.

2026-11-18: Vellum merge-group run 44294314185 was created at 11:23Z. Its GPU
proof ended at 12:52:31Z and queued sterile-consumer, which put the run back
in `queued` with updated_at 12:52:31Z. At 13:18-13:22Z m3, m5studio and m1
each booted a PR-class VM with merge-group demand counted as 0: more than ten
newer runs, two of them newer merge groups, filled the newest and merge-group
quotas, and the old run waited for its turn in the rotation. The scan now
reads such requeued runs first, merge_group oldest first, from the same
--max-job-fetches cap.
"""
from __future__ import annotations

import argparse
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import test_paginated_queue_scan as helpers

WORKFLOW = "Locked macOS GPU proof"
MERGE_CLASS = "self-hosted,macOS,ARM64,vellum-host-m3,vellum-build-macos-merge-group"
PR_CLASS = "self-hosted,macOS,ARM64,vellum-host-m3,vellum-build-macos"
INCIDENT = 44294314185


def setUpModule() -> None:
    helpers.setUpModule()


def tearDownModule() -> None:
    helpers.tearDownModule()


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _run(run_id: int, created: datetime, event: str, status: str,
         updated: datetime | None = None) -> dict[str, Any]:
    return {"id": run_id, "name": WORKFLOW, "event": event, "status": status,
            "created_at": _stamp(created), "updated_at": _stamp(updated or created)}


class IncidentShapeTests(unittest.TestCase):
    def _queue(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, list]]:
        now = datetime.now(timezone.utc)
        base = ["self-hosted", "macOS", "ARM64"]
        # The incident run: created 2 h ago, proof done 35 min ago, journey queued.
        incident = _run(INCIDENT, now - timedelta(minutes=124), "merge_group", "queued",
                        updated=now - timedelta(minutes=35))
        newer_merge = [_run(500 + i, now - timedelta(minutes=40 - 10 * i), "merge_group",
                            "in_progress") for i in range(3)]
        newer_prs = [_run(100 + i, now - timedelta(minutes=60 - 4 * i), "pull_request", "queued")
                     for i in range(12)]
        jobs: dict[int, list] = {INCIDENT: [
            {"id": 1, "status": "completed", "labels": [*base, "vellum-build-macos-merge-group"]},
            {"id": 2, "status": "queued", "labels": [*base, "vellum-build-macos-merge-group"]},
        ]}
        for run in newer_merge:
            jobs[run["id"]] = [{"id": run["id"] * 10, "status": "in_progress",
                                "labels": [*base, "vellum-build-macos-merge-group"]}]
        for run in newer_prs:
            jobs[run["id"]] = [{"id": run["id"] * 10, "status": "queued",
                                "labels": [*base, "vellum-build-macos"]}]
        return [incident, *newer_prs], newer_merge, jobs

    def _scan(self, labels: str, refreshes: int, **overrides: Any) -> list[int]:
        queued, in_progress, jobs = self._queue()

        def api(path: str) -> dict[str, Any]:
            if path == "rate_limit":
                return helpers.AUTHENTICATED_RATE_LIMIT
            if path.endswith("/actions/workflows?per_page=100"):
                return {"workflows": [{"id": 99, "name": WORKFLOW}]}
            if "status=queued" in path:
                return {"workflow_runs": queued}
            if "status=in_progress" in path:
                return {"workflow_runs": in_progress}
            if "status=pending" in path:
                return {"workflow_runs": []}
            for run_id, run_jobs in jobs.items():
                if f"/actions/runs/{run_id}/jobs?" in path:
                    return {"jobs": run_jobs}
            raise AssertionError(f"unexpected API path: {path}")

        with tempfile.TemporaryDirectory() as directory:
            # The Vellum lanes' rendered settings.
            options = {"force_refresh": True, "workflow": WORKFLOW, "labels": labels,
                       "negative_ttl": 30, "merge_group_quota": 2,
                       "oldest_reserve_max_age": 172800, **overrides}
            counts = []
            for _ in range(refreshes):
                scanner = helpers.PaginatedQueueScanTests()._scanner(
                    "tart-macos-tier", Path(directory) / "state.json", api, **options)
                counts.append(scanner.scan())
            return counts

    def test_the_requeued_merge_group_job_is_counted_on_every_refresh(self) -> None:
        self.assertEqual(self._scan(MERGE_CLASS, 12, requeued_quota=2), [1] * 12)

    def test_control_without_it_the_journey_is_missed(self) -> None:
        counts = self._scan(MERGE_CLASS, 12, requeued_quota=0)
        self.assertIn(0, counts, counts)

    def test_the_pr_class_still_sees_its_work(self) -> None:
        self.assertTrue(all(c >= 1 for c in self._scan(PR_CLASS, 6, requeued_quota=2)))

    def test_the_cli_default_is_on(self) -> None:
        import sys
        import queue_scan
        argv = ["queue_scan.py", "--repo", "o/r", "--labels", "a", "--state-file", "/tmp/x",
                "--provider", "p", "--lane-id", "l", "--workflow", "w"]
        old = sys.argv
        try:
            sys.argv = argv
            self.assertEqual(queue_scan.parse_args().requeued_quota, 2)
            sys.argv = [*argv, "--requeued-quota", "-1"]
            with self.assertRaises(SystemExit):
                queue_scan.parse_args()
        finally:
            sys.argv = old


class RequeuedRankingTests(unittest.TestCase):
    def scanner(self, **args: Any) -> Any:
        import queue_scan
        value = object.__new__(queue_scan.QueueScanner)
        value.args = argparse.Namespace(**{
            "newest_quota": 2, "max_job_fetches": 5, "page_offset_cap": 64,
            "merge_group_quota": 2, "negative_ttl": 30, "oldest_reserve_max_age": 0,
            "requeued_quota": 2, "discovery_ttl": 160, **args})
        value.now = int(datetime.now(timezone.utc).timestamp())
        return value

    def test_merge_group_first_then_oldest_and_fresh_queues_excluded(self) -> None:
        now = datetime.now(timezone.utc)
        runs = [
            _run(1, now - timedelta(minutes=90), "pull_request", "queued", now - timedelta(minutes=5)),
            _run(2, now - timedelta(minutes=60), "merge_group", "queued", now - timedelta(minutes=5)),
            _run(3, now - timedelta(minutes=120), "merge_group", "queued", now - timedelta(minutes=9)),
            # Never started: updated when created, so not requeued.
            _run(4, now - timedelta(minutes=200), "merge_group", "queued"),
            # In progress and updated since the last discovery.
            _run(5, now - timedelta(minutes=80), "merge_group", "in_progress", now - timedelta(seconds=30)),
            # In progress, last updated long ago.
            _run(6, now - timedelta(minutes=80), "merge_group", "in_progress", now - timedelta(minutes=30)),
        ]
        self.assertEqual([r["id"] for r in self.scanner()._requeued_runs(runs)], [3, 2, 5, 1])

    def test_one_rotating_slot_and_the_cap_survive(self) -> None:
        now = datetime.now(timezone.utc)
        runs = [_run(i, now - timedelta(minutes=100 + i), "merge_group", "queued",
                     now - timedelta(minutes=1)) for i in range(1, 8)]
        runs += [_run(100 + i, now - timedelta(minutes=i), "pull_request", "queued")
                 for i in range(1, 9)]
        candidates, picked = self.scanner()._candidate_runs(runs, {})
        ids = [int(r["id"]) for r in candidates]
        self.assertLessEqual(len(ids), 5)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(picked)
        # Two newest PR runs keep their slots.
        self.assertIn(101, ids)
        self.assertIn(102, ids)

    def test_a_stale_update_is_not_requeued(self) -> None:
        now = datetime.now(timezone.utc)
        runs = [_run(1, now - timedelta(days=3), "merge_group", "queued", now - timedelta(hours=7)),
                _run(2, now - timedelta(hours=2), "merge_group", "queued", now - timedelta(hours=1))]
        self.assertEqual([r["id"] for r in self.scanner()._requeued_runs(runs)], [2])

    def test_a_requeued_run_off_the_read_pages_is_still_read(self) -> None:
        """Remembered from the listing that saw it, until its jobs show none queued."""
        now = datetime.now(timezone.utc)
        incident = _run(INCIDENT, now - timedelta(minutes=124), "merge_group", "queued",
                        now - timedelta(minutes=35))
        prs = [_run(100 + i, now - timedelta(minutes=i), "pull_request", "queued")
               for i in range(1, 9)]
        scanner = self.scanner()
        state: dict[str, Any] = {}
        first, _ = scanner._candidate_runs([incident, *prs], state)
        self.assertEqual(int(first[0]["id"]), INCIDENT)
        # Next refresh reads another page: the incident run is not listed.
        second, _ = scanner._candidate_runs(list(prs), state)
        self.assertEqual(int(second[0]["id"]), INCIDENT)
        # A fetch shows its job still queued: kept. Then served: forgotten.
        scanner._forget_served(state, INCIDENT, [{"status": "queued"}])
        self.assertIn(str(INCIDENT), state["requeued_runs"])
        scanner._forget_served(state, INCIDENT, [{"status": "completed"}, {"status": "in_progress"}])
        third, _ = scanner._candidate_runs(list(prs), state)
        self.assertNotIn(INCIDENT, [int(r["id"]) for r in third])

    def test_memory_is_bounded(self) -> None:
        import queue_scan
        now = datetime.now(timezone.utc)
        runs = [_run(i, now - timedelta(hours=3), "pull_request", "queued",
                     now - timedelta(minutes=i)) for i in range(1, 40)]
        state: dict[str, Any] = {}
        self.scanner()._candidate_runs(runs, state)
        self.assertEqual(len(state["requeued_runs"]), queue_scan.REQUEUE_MEMORY_MAX)
        # The newest updates are the ones kept.
        self.assertIn("1", state["requeued_runs"])
        self.assertNotIn("39", state["requeued_runs"])

    def test_off_keeps_the_old_selection(self) -> None:
        now = datetime.now(timezone.utc)
        runs = [_run(i, now - timedelta(minutes=100 + i), "merge_group", "queued",
                     now - timedelta(minutes=1)) for i in range(1, 5)]
        runs += [_run(100 + i, now - timedelta(minutes=i), "pull_request", "queued")
                 for i in range(1, 9)]
        import queue_scan
        off = self.scanner(requeued_quota=0)
        plain = object.__new__(queue_scan.QueueScanner)
        plain.args = argparse.Namespace(**{k: v for k, v in vars(off.args).items()
                                           if k != "requeued_quota"})
        plain.now = off.now
        self.assertEqual([r["id"] for r in off._candidate_runs(runs, {})[0]],
                         [r["id"] for r in plain._candidate_runs(runs, {})[0]])


if __name__ == "__main__":
    unittest.main()
