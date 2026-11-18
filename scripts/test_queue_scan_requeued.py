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


class ProfileKeyTests(unittest.TestCase):
    def test_a_lane_can_set_or_turn_off_the_quota(self) -> None:
        import plistlib
        import tempfile as tf
        import macos_fleet_lanes as fleet
        root = Path(__file__).resolve().parents[1]
        base = (root / "profiles" / "m5-macos-fleet.toml").read_text()
        anchor = "queue_merge_group_quota = 2\n"
        self.assertEqual(base.count(anchor), 1)
        with tf.TemporaryDirectory() as td:
            path = Path(td) / "p.toml"
            path.write_text(base.replace(anchor, anchor + "queue_requeued_quota = 0\n"))
            env = next(plistlib.loads(body)["EnvironmentVariables"]
                       for name, body in fleet.rendered_plists(fleet.load(path)).items()
                       if name.endswith(".vellum-gate.plist"))
            self.assertEqual(env["TARTCI_QUEUE_REQUEUED_QUOTA"], "0")
            path.write_text(base.replace(anchor, anchor + "queue_requeued_quota = -1\n"))
            with self.assertRaisesRegex(ValueError, "queue_requeued_quota"):
                fleet.load(path)
        # Absent, nothing is rendered and the CLI default applies.
        env = next(plistlib.loads(body)["EnvironmentVariables"]
                   for name, body in fleet.rendered_plists(fleet.load(root / "profiles" / "m5-macos-fleet.toml")).items()
                   if name.endswith(".vellum-gate.plist"))
        self.assertNotIn("TARTCI_QUEUE_REQUEUED_QUOTA", env)


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

    def test_requeued_pr_runs_never_displace_a_merge_group(self) -> None:
        now = datetime.now(timezone.utc)
        prs = [_run(i, now - timedelta(hours=2, minutes=i), "pull_request", "queued",
                    now - timedelta(minutes=2)) for i in range(1, 5)]
        merge = [_run(50, now - timedelta(minutes=30), "merge_group", "in_progress")]
        fresh = [_run(100 + i, now - timedelta(minutes=i), "pull_request", "queued")
                 for i in range(1, 9)]
        ids = [int(r["id"]) for r in self.scanner()._candidate_runs([*prs, *merge, *fresh], {})[0]]
        self.assertIn(50, ids)
        self.assertLessEqual(len(ids), 5)

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
        scanner._forget_served(state, INCIDENT, [{"status": "queued", "labels": ["self-hosted", "macOS"]}])
        self.assertIn(str(INCIDENT), state["requeued_runs"])
        scanner._forget_served(state, INCIDENT, [{"status": "completed"}, {"status": "in_progress"}])
        third, _ = scanner._candidate_runs(list(prs), state)
        self.assertNotIn(INCIDENT, [int(r["id"]) for r in third])

    def test_a_remembered_in_progress_run_outlives_the_discovery_window(self) -> None:
        now = datetime.now(timezone.utc)
        run = _run(7, now - timedelta(hours=2), "merge_group", "in_progress",
                   now - timedelta(seconds=30))
        prs = [_run(100 + i, now - timedelta(minutes=i), "pull_request", "queued")
               for i in range(1, 9)]
        scanner = self.scanner()
        state: dict[str, Any] = {}
        scanner._candidate_runs([run, *prs], state)
        # 20 min later its page is not read; its update is older than the window.
        scanner.now += 1200
        later, _ = scanner._candidate_runs(list(prs), state)
        self.assertEqual(int(later[0]["id"]), 7)
        # Listed again, with the same old update: still first.
        listed, _ = scanner._candidate_runs([run, *prs], state)
        self.assertEqual(int(listed[0]["id"]), 7)

    def test_a_hosted_queued_job_does_not_keep_a_run(self) -> None:
        now = datetime.now(timezone.utc)
        # A PR run, so the plain merge-group quota does not read it either.
        run = _run(8, now - timedelta(hours=2), "pull_request", "queued", now - timedelta(minutes=3))
        prs = [_run(100 + i, now - timedelta(minutes=i), "pull_request", "queued")
               for i in range(1, 9)]
        scanner = self.scanner()
        state: dict[str, Any] = {}
        scanner._candidate_runs([run, *prs], state)
        scanner._forget_served(state, 8, [{"status": "queued", "labels": ["ubuntu-24.04"]}],
                               run["updated_at"])
        self.assertNotIn("8", state["requeued_runs"])
        # Listed again unchanged, also after a refresh that read another page:
        # no priority slot.
        for listing in ([run, *prs], list(prs), [run, *prs]):
            again, _ = scanner._candidate_runs(listing, state)
            self.assertNotEqual(int(again[0]["id"]), 8)
        # Once the run changes (a new job queued), it is requeued again.
        changed = {**run, "updated_at": _stamp(now - timedelta(minutes=1))}
        later, _ = scanner._candidate_runs([changed, *prs], state)
        self.assertEqual(int(later[0]["id"]), 8)

    def test_a_cleared_run_outside_memory_loses_its_slot(self) -> None:
        """More requeued runs than memory holds: the oldest is still cleared."""
        now = datetime.now(timezone.utc)
        # The oldest-created run also has the oldest update, so it is not
        # among the 16 newest updates memory keeps.
        runs = [_run(i, now - timedelta(hours=3, minutes=i), "pull_request", "queued",
                     now - timedelta(minutes=i)) for i in range(1, 31)]
        scanner = self.scanner()
        state: dict[str, Any] = {}
        first, _ = scanner._candidate_runs(runs, state)
        oldest = int(first[0]["id"])
        self.assertEqual(oldest, 30)
        self.assertNotIn(str(oldest), state["requeued_runs"])
        scanner._forget_served(state, oldest, [{"status": "queued", "labels": ["ubuntu-24.04"]}],
                               next(r["updated_at"] for r in runs if r["id"] == oldest))
        second, _ = scanner._candidate_runs(runs, state)
        self.assertNotEqual(int(second[0]["id"]), oldest)

    def reads(self, runs: list[dict[str, Any]], refreshes: int = 12) -> dict[int, int]:
        """Reads per run at production cadence: 160 s apart, state through JSON."""
        import json
        scanner = self.scanner()
        state: dict[str, Any] = {}
        counts: dict[int, int] = {}
        for _ in range(refreshes):
            for run in scanner._candidate_runs([dict(r) for r in runs], state)[0]:
                counts[int(run["id"])] = counts.get(int(run["id"]), 0) + 1
            state = json.loads(json.dumps(state))
            scanner.now += 160
        return counts

    def backlog(self, now: datetime) -> list[dict[str, Any]]:
        # Twelve newer PR runs and one older never-started run, which holds
        # the oldest-run reservation as Vellum's queue did on 2026-11-18.
        return [_run(100 + i, now - timedelta(minutes=i), "pull_request", "queued")
                for i in range(1, 13)] + [
            _run(50, now - timedelta(hours=5), "pull_request", "queued")]

    def test_two_requeued_merge_groups_are_both_read_regularly(self) -> None:
        now = datetime.now(timezone.utc)
        a = _run(1, now - timedelta(hours=3), "merge_group", "queued", now - timedelta(minutes=9))
        b = _run(2, now - timedelta(hours=2), "merge_group", "queued", now - timedelta(minutes=8))
        counts = self.reads([a, b, *self.backlog(now)])
        self.assertGreaterEqual(counts.get(1, 0), 6)
        self.assertGreaterEqual(counts.get(2, 0), 6)

    def test_a_fresh_merge_group_is_not_hidden_by_a_requeued_one(self) -> None:
        now = datetime.now(timezone.utc)
        a = _run(1, now - timedelta(hours=3), "merge_group", "queued", now - timedelta(minutes=9))
        # Fresh, its first job queued, but behind two newer running groups.
        c = _run(3, now - timedelta(minutes=40), "merge_group", "queued")
        running = [_run(5 + i, now - timedelta(minutes=20 - i), "merge_group", "in_progress")
                   for i in range(2)]
        counts = self.reads([a, c, *running, *self.backlog(now)])
        self.assertGreaterEqual(counts.get(1, 0), 6)
        self.assertGreaterEqual(counts.get(3, 0), 6)

    def test_requeued_pr_runs_wait_behind_merge_groups_but_are_read(self) -> None:
        now = datetime.now(timezone.utc)
        r1 = _run(1, now - timedelta(hours=4), "pull_request", "queued", now - timedelta(minutes=9))
        c = _run(3, now - timedelta(minutes=40), "merge_group", "queued")
        counts = self.reads([r1, c, *self.backlog(now)])
        self.assertEqual(counts.get(3, 0), 12)
        self.assertGreaterEqual(counts.get(1, 0), 2)

    def test_a_group_whose_job_just_started_does_not_take_the_turn(self) -> None:
        """Run 44317241223: a job picked up after 18 min moved updated_at."""
        now = datetime.now(timezone.utc)
        a = _run(1, now - timedelta(minutes=124), "merge_group", "queued", now - timedelta(minutes=35))
        b = _run(2, now - timedelta(minutes=19), "merge_group", "in_progress",
                 now - timedelta(seconds=20))
        counts = self.reads([a, b, *self.backlog(now)])
        self.assertEqual(counts.get(1, 0), 12, counts)

    def test_three_requeued_groups_are_all_read(self) -> None:
        now = datetime.now(timezone.utc)
        groups = [_run(i, now - timedelta(hours=3, minutes=i), "merge_group", "queued",
                       now - timedelta(minutes=9)) for i in (1, 2, 3)]
        counts = self.reads([*groups, *self.backlog(now)], refreshes=18)
        for run_id in (1, 2, 3):
            self.assertGreaterEqual(counts.get(run_id, 0), 5, counts)

    def test_no_merge_group_quota_no_priority_for_pr_or_running_runs(self) -> None:
        now = datetime.now(timezone.utc)
        requeued_pr = _run(1, now - timedelta(hours=4), "pull_request", "queued",
                           now - timedelta(minutes=9))
        running = _run(2, now - timedelta(hours=1), "merge_group", "in_progress",
                       now - timedelta(seconds=20))
        scanner = self.scanner(merge_group_quota=0)
        ids = [int(r["id"]) for r in scanner._candidate_runs(
            [requeued_pr, running, *self.backlog(now)], {})[0]]
        off = self.scanner(merge_group_quota=0, requeued_quota=0)
        base = [int(r["id"]) for r in off._candidate_runs(
            [requeued_pr, running, *self.backlog(now)], {})[0]]
        self.assertEqual(ids, base)

    def test_the_in_progress_window_runs_from_the_previous_listing(self) -> None:
        now = datetime.now(timezone.utc)
        scanner = self.scanner()
        state: dict[str, Any] = {}
        scanner._candidate_runs(self.backlog(now), state)
        # The next listing comes 300 s later (cache 160 s plus fetch and poll).
        scanner.now += 300
        later = datetime.fromtimestamp(scanner.now, timezone.utc)
        run = _run(9, later - timedelta(hours=1), "merge_group", "in_progress",
                   later - timedelta(seconds=250))
        scanner._with_remembered([run], state)
        self.assertEqual([int(r["id"]) for r in scanner._requeued_runs([run])], [9])

    def test_the_cleared_map_is_bounded(self) -> None:
        import queue_scan
        scanner = self.scanner()
        state: dict[str, Any] = {}
        for run_id in range(1, 400):
            scanner._forget_served(state, run_id, [], "2026-11-18T12:00:00Z")
        scanner._with_remembered([], state)
        self.assertLessEqual(len(state["requeued_cleared"]), queue_scan.REQUEUE_CLEARED_MAX)

    def test_the_reservation_is_not_charged_for_a_picked_run(self) -> None:
        """Requeued groups are the oldest runs. The reservation is judged on the
        backlog the priority pick leaves, so it reads the other group instead
        of being charged for the picked one: both are read on every refresh."""
        now = datetime.now(timezone.utc)
        a = _run(1, now - timedelta(hours=3), "merge_group", "queued", now - timedelta(minutes=9))
        b = _run(2, now - timedelta(hours=2), "merge_group", "queued", now - timedelta(minutes=8))
        prs = [_run(100 + i, now - timedelta(minutes=i), "pull_request", "queued")
               for i in range(1, 13)]
        scanner = self.scanner()
        state: dict[str, Any] = {}
        for _ in range(3):
            ids = [int(r["id"]) for r in scanner._candidate_runs([a, b, *prs], state)[0]]
            self.assertIn(1, ids)
            self.assertIn(2, ids)
            scanner.now += 10

    def test_off_writes_no_requeued_state(self) -> None:
        scanner = self.scanner(requeued_quota=0)
        state: dict[str, Any] = {}
        for run_id in range(50):
            scanner._forget_served(state, run_id, [], "2026-11-18T12:00:00Z")
        self.assertNotIn("requeued_cleared", state)

    def test_memory_trim_keeps_merge_groups(self) -> None:
        now = datetime.now(timezone.utc)
        merge = _run(1, now - timedelta(hours=3), "merge_group", "queued", now - timedelta(minutes=50))
        prs = [_run(100 + i, now - timedelta(hours=2), "pull_request", "queued",
                    now - timedelta(minutes=i)) for i in range(1, 25)]
        state: dict[str, Any] = {}
        self.scanner()._candidate_runs([merge, *prs], state)
        self.assertIn("1", state["requeued_runs"])

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
