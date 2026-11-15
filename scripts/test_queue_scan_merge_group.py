#!/usr/bin/env python3
"""A merge group's second job must be demand on the first refresh.

2026-11-15: the Vellum merge-group run's `sterile-consumer` job (created when
its GPU job ended, so long after the run) sat queued for 30+ minutes while the
lane kept booting tier 1 for PR jobs: the scan read the jobs of only five runs
per refresh and the merge-group run, older than the newest PR runs, was visited
once per rotation.
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
TIER0 = "self-hosted,macOS,ARM64,vellum-host-m3,vellum-build-macos-merge-group"
TIER1 = "self-hosted,macOS,ARM64,vellum-host-m3,vellum-build-macos"
MERGE_ID = 43704825328


def setUpModule() -> None:
    helpers.setUpModule()


def tearDownModule() -> None:
    helpers.tearDownModule()


def _run(run_id: int, created: datetime, event: str) -> dict[str, Any]:
    stamp = created.isoformat().replace("+00:00", "Z")
    return {
        "id": run_id,
        "name": WORKFLOW,
        "event": event,
        "status": "queued",
        "created_at": stamp,
        "updated_at": stamp,
    }


class MergeGroupSecondJobTests(unittest.TestCase):
    def _queue(self) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
        now = datetime.now(timezone.utc)
        ghosts = [
            _run(1, now - timedelta(days=4), "pull_request"),
            _run(2, now - timedelta(days=4, minutes=-5), "pull_request"),
        ]
        merge = _run(MERGE_ID, now - timedelta(minutes=45), "merge_group")
        prs = [
            _run(100 + i, now - timedelta(minutes=30 - 3 * i), "pull_request")
            for i in range(6)
        ]
        runs = [*ghosts, merge, *prs]
        base = ["self-hosted", "macOS", "ARM64"]
        jobs: dict[int, list[dict[str, Any]]] = {1: [], 2: []}
        jobs[MERGE_ID] = [
            {"id": 1, "status": "completed", "labels": [*base, "vellum-build-macos-merge-group"]},
            {"id": 2, "status": "queued", "labels": [*base, "vellum-build-macos-merge-group"]},
        ]
        for run in prs:
            jobs[run["id"]] = [
                {"id": run["id"] * 10, "status": "queued", "labels": [*base, "vellum-build-macos"]}
            ]
        return runs, jobs

    def _scan(self, labels: str, refreshes: int = 1, **overrides: Any) -> list[int]:
        runs, jobs = self._queue()

        def api(path: str) -> dict[str, Any]:
            if path == "rate_limit":
                return helpers.AUTHENTICATED_RATE_LIMIT
            if path.endswith("/actions/workflows?per_page=100"):
                return {"workflows": [{"id": 99, "name": WORKFLOW}]}
            if "status=queued" in path:
                return {"workflow_runs": runs}
            if "status=pending" in path or "status=in_progress" in path:
                return {"workflow_runs": []}
            for run_id, run_jobs in jobs.items():
                if f"/actions/runs/{run_id}/jobs?" in path:
                    return {"jobs": run_jobs}
            raise AssertionError(f"unexpected API path: {path}")

        with tempfile.TemporaryDirectory() as directory:
            options = {
                "force_refresh": True,
                "workflow": WORKFLOW,
                "labels": labels,
                "negative_ttl": 30,
                "merge_group_quota": 2,
                "oldest_reserve_max_age": 172800,
                **overrides,
            }
            counts = []
            for _ in range(refreshes):
                # Same state directory every time: the rotation offsets and the
                # oldest-run reservation persist between refreshes, as live.
                scanner = helpers.PaginatedQueueScanTests()._scanner(
                    "tart-macos", Path(directory) / "state.json", api, **options
                )
                counts.append(scanner.scan())
            return counts

    def test_tier_zero_demand_is_one_on_the_first_refresh(self) -> None:
        self.assertEqual(self._scan(TIER0), [1])

    def test_tier_zero_demand_is_one_on_every_refresh(self) -> None:
        self.assertEqual(self._scan(TIER0, refreshes=12), [1] * 12)

    def test_control_without_the_fix_tier_zero_misses_it_on_some_refresh(self) -> None:
        counts = self._scan(
            TIER0, refreshes=12, merge_group_quota=0, oldest_reserve_max_age=0
        )
        self.assertIn(0, counts, counts)

    def test_tier_one_still_counts_the_pr_jobs_it_can_serve(self) -> None:
        self.assertGreaterEqual(self._scan(TIER1)[0], 2)


class OldestReservationTests(unittest.TestCase):
    @staticmethod
    def _scanner(max_age: int) -> Any:
        import queue_scan

        value = object.__new__(queue_scan.QueueScanner)
        value.args = argparse.Namespace(
            newest_quota=2,
            max_job_fetches=5,
            page_offset_cap=64,
            merge_group_quota=2,
            negative_ttl=30,
            oldest_reserve_max_age=max_age,
        )
        value.now = int(datetime.now(timezone.utc).timestamp())
        return value

    @staticmethod
    def _runs() -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)

        def make(run_id: int, age: timedelta) -> dict[str, Any]:
            return {**_run(run_id, now - age, "pull_request"), "_page_key": "queued:1", "_page_sweep": 0}

        return [
            make(1, timedelta(days=4)),
            make(2, timedelta(days=3)),
            *(make(10 + i, timedelta(hours=2, minutes=-i)) for i in range(8)),
        ]

    # The days-old runs stay in the rotation; what they lose is the slot that
    # is reserved for the oldest backlog run on every refresh.
    def test_a_days_old_run_does_not_take_the_reserved_slot(self) -> None:
        value = self._scanner(172800)
        state: dict = {}
        value._candidate_runs(self._runs(), state)
        reserved = set(state["oldest_reserved_at"])
        self.assertNotIn("1", reserved, reserved)
        self.assertNotIn("2", reserved, reserved)
        self.assertEqual(reserved, {"10"}, "the oldest young run is reserved instead")

    def test_control_the_oldest_run_is_reserved_when_the_age_limit_is_off(self) -> None:
        value = self._scanner(0)
        state: dict = {}
        value._candidate_runs(self._runs(), state)
        self.assertEqual(set(state["oldest_reserved_at"]), {"1"})

    def test_when_every_backlog_run_is_old_nothing_is_reserved_and_nothing_breaks(self) -> None:
        value = self._scanner(3600)
        selected, _ = value._candidate_runs(self._runs()[:2] + self._runs()[2:4], {})
        self.assertLessEqual(len(selected), 5)


class ScopeTests(unittest.TestCase):
    """The merge-group quota is a Vellum-lane setting; every other lane (Pulp,
    whose queue shows 100+ queued runs, so it depends on the newest slots) keeps
    the plain newest-then-rotation order."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_the_default_is_zero(self) -> None:
        import os
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if k != "TARTCI_QUEUE_MERGE_GROUP_QUOTA"}
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.argv=['x','--repo','o/r','--workflow','w','--labels','l','--state-file','s','--provider','p'];"
             "import queue_scan; print(queue_scan.parse_args().merge_group_quota)"],
            cwd=self.ROOT / "scripts", env=env, capture_output=True, text=True, check=True,
        )
        self.assertEqual(out.stdout.strip(), "0")

    def test_only_the_vellum_lanes_set_it_and_every_host_renders_it(self) -> None:
        import plistlib
        import sys

        sys.path.insert(0, str(self.ROOT / "scripts"))
        import testing_support

        testing_support.skip_module_without_tomllib()
        import macos_fleet_lanes as fleet

        for host in ("m1", "m3", "m5", "m5studio"):
            with self.subTest(host=host):
                rendered = fleet.rendered_plists(
                    fleet.load(self.ROOT / "profiles" / f"{host}-macos-fleet.toml")
                )
                quotas = {}
                for name, body in rendered.items():
                    if "tart-runner-macos-fleet" not in name:
                        continue
                    env = plistlib.loads(body)["EnvironmentVariables"]
                    quotas[name] = env.get("TARTCI_QUEUE_MERGE_GROUP_QUOTA")
                vellum = {n: q for n, q in quotas.items() if ".vellum-gate" in n}
                other = {n: q for n, q in quotas.items() if ".vellum-gate" not in n}
                self.assertTrue(vellum, quotas)
                self.assertEqual(set(vellum.values()), {"2"}, vellum)
                self.assertEqual(set(other.values()), {None}, other)


if __name__ == "__main__":
    unittest.main()
