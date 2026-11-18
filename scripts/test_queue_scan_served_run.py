#!/usr/bin/env python3
"""The forced post-serve scan reads the just-served job's run first.

A lane that serves a proof refreshes once, uncached, because the proof's
dependent job (Vellum's sterile-consumer) is queued only as the proof ends.
The proof's run is as old as the proof (30+ minutes), so with more than five
active runs it sat in the bounded rotation: a review sampled it in 6 to 16 of
48 refreshes. runner.sh now passes that run as --priority-run, and the scan
reads its jobs first, from the same --max-job-fetches cap.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import test_paginated_queue_scan as helpers

WORKFLOW = "Locked macOS GPU proof"
STERILE = "self-hosted,macOS,ARM64,vellum-host-m3,vellum-sterile-consumer"
MERGE_CLASS = "self-hosted,macOS,ARM64,vellum-host-m3,vellum-build-macos-merge-group"
PR_CLASS = "self-hosted,macOS,ARM64,vellum-host-m3,vellum-build-macos"
SERVED = 43900000001


def setUpModule() -> None:
    helpers.setUpModule()


def tearDownModule() -> None:
    helpers.tearDownModule()


def _run(run_id: int, created: datetime, event: str, status: str) -> dict[str, Any]:
    stamp = created.isoformat().replace("+00:00", "Z")
    return {"id": run_id, "name": WORKFLOW, "event": event, "status": status,
            "created_at": stamp, "updated_at": stamp}


class ServedRunFirstTests(unittest.TestCase):
    merge_status = "in_progress"

    def _queue(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, list]]:
        now = datetime.now(timezone.utc)
        base = ["self-hosted", "macOS", "ARM64"]
        # The served PR run: its proof just completed, its journey is queued.
        served = _run(SERVED, now - timedelta(minutes=40), "pull_request", "in_progress")
        running = [_run(200 + i, now - timedelta(minutes=35 - i), "pull_request", "in_progress")
                   for i in range(3)]
        queued = [_run(100 + i, now - timedelta(minutes=30 - 3 * i), "pull_request", "queued")
                  for i in range(8)]
        merge = _run(300, now - timedelta(minutes=20), "merge_group", "in_progress")
        jobs: dict[int, list] = {
            SERVED: [
                {"id": 1, "status": "completed", "labels": [*base, "vellum-build-macos"]},
                {"id": 2, "status": "queued", "labels": [*base, "vellum-sterile-consumer"]},
            ],
            300: [{"id": 3, "status": self.merge_status,
                   "labels": [*base, "vellum-build-macos-merge-group"]}],
        }
        for run in running:
            jobs[run["id"]] = [{"id": run["id"] * 10, "status": "in_progress",
                                "labels": [*base, "vellum-build-macos"]}]
        for run in queued:
            jobs[run["id"]] = [{"id": run["id"] * 10, "status": "queued",
                                "labels": [*base, "vellum-build-macos"]}]
        return [*queued], [served, *running, merge], jobs

    def _scan(self, labels: str, refreshes: int = 1, **overrides: Any) -> list[int]:
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
            options = {"force_refresh": True, "workflow": WORKFLOW, "labels": labels,
                       "negative_ttl": 30, "merge_group_quota": 2,
                       "oldest_reserve_max_age": 172800, **overrides}
            counts = []
            for _ in range(refreshes):
                scanner = helpers.PaginatedQueueScanTests()._scanner(
                    "tart-macos-tier", Path(directory) / "state.json", api, **options)
                counts.append(scanner.scan())
            return counts

    def test_the_forced_scan_counts_the_journey_on_every_refresh(self) -> None:
        self.assertEqual(self._scan(STERILE, refreshes=12, priority_run=SERVED), [1] * 12)

    def test_control_without_the_served_run_it_misses_the_journey(self) -> None:
        counts = self._scan(STERILE, refreshes=12)
        self.assertIn(0, counts, counts)

    def test_a_queued_merge_group_proof_is_still_counted(self) -> None:
        """The served run takes a newest slot, never the merge-group quota's."""
        self.merge_status = "queued"
        self.assertEqual(self._scan(MERGE_CLASS, refreshes=6), [1] * 6)
        self.assertEqual(self._scan(MERGE_CLASS, refreshes=6, priority_run=SERVED), [1] * 6)

    def test_an_unknown_served_run_changes_nothing(self) -> None:
        self.assertEqual(self._scan(PR_CLASS, refreshes=3, priority_run=1),
                         self._scan(PR_CLASS, refreshes=3))

    def test_the_fetch_cap_still_holds(self) -> None:
        import queue_scan
        import argparse
        now = datetime.now(timezone.utc)
        runs = [_run(SERVED, now - timedelta(minutes=40), "pull_request", "in_progress")]
        runs += [_run(i, now - timedelta(minutes=i), "merge_group", "in_progress")
                 for i in range(1, 4)]
        runs += [_run(100 + i, now - timedelta(minutes=30 + i), "pull_request", "queued")
                 for i in range(8)]
        scanner = object.__new__(queue_scan.QueueScanner)
        scanner.args = argparse.Namespace(newest_quota=2, max_job_fetches=5, page_offset_cap=64,
                                          merge_group_quota=2, negative_ttl=30,
                                          oldest_reserve_max_age=0, priority_run=SERVED)
        scanner.now = int(now.timestamp())
        candidates, _ = scanner._candidate_runs(runs, {})
        ids = [int(run["id"]) for run in candidates]
        self.assertEqual(ids[0], SERVED)
        self.assertLessEqual(len(ids), 5)
        self.assertEqual(len(ids), len(set(ids)))
        # The merge-group quota keeps its slot: the newest merge_group run.
        self.assertIn(1, ids)
        # And the served run's slot came out of the newest quota: the
        # quota's merge-group run, one newest run, the reserved oldest run
        # (107) and one rotating pick.
        state: dict[str, Any] = {}
        ids = [int(run["id"]) for run in scanner._candidate_runs(runs, state)[0]]
        self.assertEqual(ids, [SERVED, 1, 2, 107, 106])
        self.assertIn("107", state["oldest_reserved_at"])


if __name__ == "__main__":
    unittest.main()
