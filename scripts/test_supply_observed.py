#!/usr/bin/env python3
"""Observed supply: completed GitHub jobs attributed to declared registrations."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import supply_observed as so

ROOT = Path(__file__).resolve().parents[1]
PUBLISHED = json.loads((ROOT / "fleet" / "advertised-labels.json").read_text())
# A recorded, field-trimmed `actions/runs/35792120939/jobs` response
# (Generous-Corp/pulp, Build and Test, merge_group, 2026-09-22).
FIXTURE = ROOT / "tests" / "fixtures" / "pulp-build-and-test-run-35792120939-jobs.json"
REPO = "Generous-Corp/pulp"
GATE = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]


def job(runner, labels, workflow="Build and Test", conclusion="success",
        completed="2026-09-22T10:00:00Z", status="completed"):
    return {"runner_name": runner, "labels": labels, "workflow_name": workflow,
            "status": status, "conclusion": conclusion, "completed_at": completed}


def verdict(result, host, lane, class_label):
    return next(row for row in result["registrations"]
                if (row["host_id"], row["lane"], row.get("class_label")) == (host, lane, class_label))


# The published fleet declares no persistent runner, so the persistent-runner
# path is exercised against a synthetic one on top of the real registrations.
SYNTHETIC_PERSISTENT = {"profile": "m5-macos-fleet", "host_id": "m5",
                        "launchd_label": "actions.runner.example.pulp-preamble-m5",
                        "runner_name": "pulp-preamble-m5"}
WITH_PERSISTENT = {**PUBLISHED, "persistent_runners": [SYNTHETIC_PERSISTENT]}


class RunnerNameTests(unittest.TestCase):
    regs = PUBLISHED["registrations"]
    persistent = WITH_PERSISTENT["persistent_runners"]

    def test_ephemeral_and_slot_names(self) -> None:
        self.assertEqual(so.attribute_runner("studio-pulp-gate-01-42272-1", self.regs, self.persistent),
                         ("lane", "studio", "pulp-gate"))
        self.assertEqual(so.attribute_runner("m5-pulp-gate-slot2-02-9-3", self.regs, self.persistent),
                         ("lane", "m5", "pulp-gate"))
        self.assertEqual(so.attribute_runner("m5-pulp-release-01-1-1", self.regs, self.persistent),
                         ("lane", "m5", "pulp-release"))

    def test_persistent_and_unknown_names(self) -> None:
        self.assertEqual(so.attribute_runner("pulp-preamble-m5", self.regs, self.persistent),
                         ("persistent", "m5", None))
        for name in ("pulp-pr-head-01", "studio-pulp-gate", "studio-pulp-gatex-01-1-1",
                     "GitHub Actions 1000116411"):
            with self.subTest(name=name):
                self.assertEqual(so.attribute_runner(name, self.regs, self.persistent)[0], "unknown")


class RecordedFixtureTests(unittest.TestCase):
    def test_recorded_run_attributes_the_gate_job_to_studio(self) -> None:
        jobs = json.loads(FIXTURE.read_text())["jobs"]
        result = so.classify(PUBLISHED, REPO, jobs)
        row = verdict(result, "studio", "pulp-gate", "pulp-build-merge-group")
        self.assertEqual((row["verdict"], row["count"]), (so.OBSERVED, 1))
        self.assertEqual(row["last_seen"], "2026-09-22T23:07:35Z")
        # Hosted jobs never count; skipped self-hosted jobs are not demand.
        self.assertEqual(result["jobs_considered"], 3)
        self.assertEqual(verdict(result, "m5", "pulp-gate", "pulp-build-pr-head")["verdict"], so.IDLE)
        # Demand existed and another host served it: NOT_OBSERVED, attributed.
        m1 = verdict(result, "m1", "pulp-gate", "pulp-build-merge-group")
        self.assertEqual(m1["verdict"], so.NOT_OBSERVED)
        self.assertEqual(m1["served_by"], {"studio/pulp-gate": 1})
        self.assertEqual(result["undeclared"], [])

    def test_cli_on_fixture(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "supply_observed.py"), "--repo", REPO,
             "--jobs-file", str(FIXTURE), "--json"],
            text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["schema"], "tartci.supply-observed/v1")


class ClassifierTests(unittest.TestCase):
    def test_undeclared_runner_name_is_reported(self) -> None:
        jobs = [job("pulp-pr-head-01-1-1", GATE + ["pulp-build-pr-head"]),
                job("studio-pulp-gate-01-1-1", GATE + ["pulp-build-pr-head"])]
        result = so.classify(PUBLISHED, REPO, jobs)
        self.assertEqual(len(result["undeclared"]), 1)
        self.assertEqual(result["undeclared"][0]["runner"], "pulp-pr-head-01-1-1")
        self.assertIn("no declared", result["undeclared"][0]["why"])
        # Control: the declared runner in the same batch is OBSERVED.
        self.assertEqual(verdict(result, "studio", "pulp-gate", "pulp-build-pr-head")["verdict"],
                         so.OBSERVED)

    def test_declared_lane_running_undeclared_labels_is_reported(self) -> None:
        jobs = [job("studio-pulp-gate-01-1-1", GATE + ["pulp-gate-fast"])]
        result = so.classify(PUBLISHED, REPO, jobs)
        self.assertEqual(result["undeclared"][0]["attributed_to"],
                         {"host_id": "studio", "lane": "pulp-gate"})
        self.assertIn("labels outside", result["undeclared"][0]["why"])

    def test_unserved_release_demand_is_not_observed(self) -> None:
        release = ["self-hosted", "macOS", "ARM64", "pulp-build-vm-release", "pulp-release-tagged"]
        jobs = [job(None, release, workflow="Release CLI", conclusion="cancelled")]
        row = verdict(so.classify(PUBLISHED, REPO, jobs), "m5", "pulp-release", "pulp-release-tagged")
        self.assertEqual(row["verdict"], so.NOT_OBSERVED)
        self.assertEqual(row["served_by"], {"<never assigned>": 1})
        # Control: the same demand served by m5's lane is OBSERVED.
        jobs = [job("m5-pulp-release-01-7-1", release, workflow="Release CLI")]
        row = verdict(so.classify(PUBLISHED, REPO, jobs), "m5", "pulp-release", "pulp-release-tagged")
        self.assertEqual(row["verdict"], so.OBSERVED)

    def test_skipped_jobs_are_not_demand_and_workflow_must_match(self) -> None:
        jobs = [job(None, GATE + ["pulp-build-pr-head"], conclusion="skipped"),
                job("m5-pulp-gate-01-1-1", GATE + ["pulp-build-pr-head"], workflow="Other")]
        result = so.classify(PUBLISHED, REPO, jobs)
        self.assertEqual(verdict(result, "m1", "pulp-gate", "pulp-build-pr-head")["verdict"], so.IDLE)
        self.assertEqual(len(result["undeclared"]), 1)
        self.assertIn("GitHub assigns by labels alone", result["undeclared"][0]["why"])

    def test_persistent_runner_is_reported_separately(self) -> None:
        result = so.classify(WITH_PERSISTENT, REPO, [job("pulp-preamble-m5", ["self-hosted", "preamble"])])
        self.assertEqual(result["persistent"][0]["verdict"], so.PERSISTENT_OBSERVED)
        self.assertEqual(result["undeclared"], [])


class BoundedCollectionTests(unittest.TestCase):
    def test_run_and_job_pages_are_bounded_and_reported(self) -> None:
        calls = []

        def fetch(path):
            calls.append(path)
            if "/jobs" in path:
                return {"jobs": [job("x", ["self-hosted"])] * 100}
            return {"workflow_runs": [{"id": i, "name": "Build and Test"} for i in range(100)]}

        jobs, scan = so.collect_jobs(fetch, REPO, {"Build and Test"}, 24, 3, 2,
                                     now=dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc))
        self.assertEqual(scan["runs_scanned"], 3)
        self.assertTrue(scan["runs_truncated"])
        self.assertTrue(scan["job_pages_truncated"])
        self.assertEqual(len(jobs), 3 * 2 * 100)
        self.assertIn("created=%3E%3D2026-09-21T00:00:00Z", calls[0])

    def test_run_listing_pages_are_bounded_when_nothing_matches(self) -> None:
        calls = []

        def fetch(path):
            calls.append(path)
            return {"workflow_runs": [{"id": 1, "name": "Docs"}] * 100}

        jobs, scan = so.collect_jobs(fetch, REPO, {"Build and Test"}, 24, 10, 1, max_run_pages=4)
        self.assertEqual(len(calls), 4)
        self.assertTrue(scan["runs_truncated"])

    def test_unrelated_workflows_are_not_fetched(self) -> None:
        def fetch(path):
            if "/jobs" in path:
                raise AssertionError(f"fetched jobs for an unrelated run: {path}")
            return {"workflow_runs": [{"id": 1, "name": "Docs"}]}

        jobs, scan = so.collect_jobs(fetch, REPO, {"Build and Test"}, 24, 10, 1)
        self.assertEqual((jobs, scan["runs_scanned"], scan["runs_truncated"]), ([], 0, False))


if __name__ == "__main__":
    unittest.main()
