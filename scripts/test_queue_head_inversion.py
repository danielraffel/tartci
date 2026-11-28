#!/usr/bin/env python3
"""Queue-head inversion: diagnostics, detector, and the pool status surface.

2026-11-28: Vellum run 46104476461 sat queued 69 minutes while runners with its
labels took newer jobs; run 46014772617 waited 126 minutes and the scans never
counted it. Nothing logged which run and job ids a scan counted, and nothing
noticed the inversion.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

import assignment_scan
import pool_usage
import queue_scan
import queue_head_inversion as qhi
import slot_demand
import test_paginated_queue_scan as helpers

WORKFLOW = "Locked macOS GPU proof"
LABEL = "vellum-build-macos-merge-group"
BASE = ["self-hosted", "macOS", "ARM64"]
NOW = dt.datetime(2026, 11, 28, 4, 56, tzinfo=dt.timezone.utc)


def setUpModule() -> None:
    helpers.setUpModule()


def tearDownModule() -> None:
    helpers.tearDownModule()


def ago(minutes: float) -> str:
    return slot_demand.iso(NOW - dt.timedelta(minutes=minutes))


def run(run_id: int, event: str = "merge_group") -> dict[str, Any]:
    return {"id": run_id, "event": event}


def queued_job(job_id: int, minutes: float, labels: Any = None) -> dict[str, Any]:
    out = {"id": job_id, "created_at": ago(minutes), "status": "queued"}
    if labels is not None:
        out["labels"] = labels
    return out


def started_job(job_id: int, created: float, started: float, runner: str,
                labels: Any = None) -> dict[str, Any]:
    out = {"id": job_id, "created_at": ago(created), "started_at": ago(started),
           "status": "in_progress", "runner_name": runner}
    if labels is not None:
        out["labels"] = labels
    return out


def detail(queued: list[tuple[int, dict]], started: list[tuple[int, dict]]) -> dict[str, Any]:
    out = slot_demand.DemandDetail()
    for run_id, job in queued:
        out.add(run(run_id), job)
    for run_id, job in started:
        out.note_started(run(run_id), job)
    return out.as_dict()


class DiagnosticsTests(unittest.TestCase):
    def test_counted_ids_are_listed_per_class_oldest_first_and_bounded(self) -> None:
        d = slot_demand.DemandDetail()
        for i in range(15):
            d.add(run(1000 + i), queued_job(2000 + i, minutes=100 - i))
        d.add(run(7, "pull_request"), queued_job(8, minutes=5))
        classes = d.as_dict()["classes"]
        listed = classes["merge_group"]["oldest_jobs"]
        self.assertEqual(len(listed), slot_demand.MAX_LISTED_JOBS)
        self.assertEqual(listed[0], {"run_id": 1000, "job_id": 2000, "created_at": ago(100),
                                     "labels": []})
        self.assertEqual(classes["merge_group"]["count"], 15)
        self.assertEqual(classes["pull_request"]["oldest_jobs"][0]["job_id"], 8)

    def test_started_jobs_are_bounded_newest_first(self) -> None:
        d = slot_demand.DemandDetail()
        for i in range(30):
            d.note_started(run(i), started_job(i, created=200, started=100 - i, runner="r"))
        rows = d.as_dict()["started"]
        self.assertEqual(len(rows), slot_demand.MAX_STARTED_JOBS)
        self.assertEqual(rows[0]["job_id"], 29)

    def test_scan_demand_ids_event_is_written_once_per_change(self) -> None:
        d = detail([(1, queued_job(10, 5))], [])
        with tempfile.TemporaryDirectory() as tmp:
            log, state = str(Path(tmp) / "events.jsonl"), str(Path(tmp) / "state.json")
            kw = dict(event_log=log, state_file=state, runner="lane", tier="t0", now=NOW)
            self.assertEqual(qhi.record_scan(d, **kw), ["scan_demand_ids"])
            self.assertEqual(qhi.record_scan(d, **kw), [])
            d2 = detail([(1, queued_job(10, 5)), (2, queued_job(11, 4))], [])
            self.assertEqual(qhi.record_scan(d2, **kw), ["scan_demand_ids"])
            lines = [json.loads(x) for x in Path(log).read_text().splitlines()]
        self.assertEqual(lines[0]["fields"]["counted"]["merge_group"],
                         [{"run_id": 1, "job_id": 10, "created_at": ago(5), "labels": []}])
        self.assertEqual(lines[0]["fields"]["tier"], "t0")


class DetectorTests(unittest.TestCase):
    def test_positive_older_job_starved_by_newer_jobs(self) -> None:
        # The 2026-11-28 shape: queued 69 min, two newer jobs got runners.
        d = detail([(46104476461, queued_job(140854026289, 69))],
                   [(46104477453, started_job(140854028996, 65, 64, "studio-slot2")),
                    (46106139411, started_job(140859201898, 45, 44, "m5studio-01"))])
        found = qhi.detect(d, NOW)
        self.assertEqual(len(found), 1)
        inv = found[0]
        self.assertEqual((inv["run_id"], inv["job_id"]), (46104476461, 140854026289))
        self.assertEqual(inv["age_seconds"], 69 * 60)
        self.assertEqual([t["job_id"] for t in inv["took"]], [140854028996, 140859201898])
        self.assertIn("cancel and re-run run 46104476461", inv["next_action"])
        self.assertIn("steward", inv["next_action"])

    def test_negative_old_job_waits_on_a_full_fleet(self) -> None:
        # Nothing with these labels started after it: no runner was minted for
        # newer work, the fleet is just full.
        d = detail([(1, queued_job(10, 90))], [])
        self.assertEqual(qhi.detect(d, NOW), [])

    def test_negative_jobs_older_than_the_queued_one_are_not_evidence(self) -> None:
        d = detail([(1, queued_job(10, 90))],
                   [(2, started_job(20, created=120, started=100, runner="r")),
                    (3, started_job(21, created=120, started=60, runner="r"))])
        self.assertEqual(qhi.detect(d, NOW), [])

    def test_negative_below_the_threshold(self) -> None:
        d = detail([(1, queued_job(10, 10))],
                   [(2, started_job(20, created=8, started=7, runner="r"))])
        self.assertEqual(qhi.detect(d, NOW), [])

    def test_only_merge_group_is_a_higher_tier(self) -> None:
        d = slot_demand.DemandDetail()
        d.add(run(1, "pull_request"), queued_job(10, 90))
        d.note_started(run(2), started_job(20, created=60, started=50, runner="r"))
        self.assertEqual(qhi.detect(d.as_dict(), NOW), [])

    def test_event_is_off_by_default_and_deduped_when_on(self) -> None:
        d = detail([(1, queued_job(10, 69))],
                   [(2, started_job(20, created=60, started=50, runner="r"))])
        with tempfile.TemporaryDirectory() as tmp:
            log, state = str(Path(tmp) / "events.jsonl"), str(Path(tmp) / "state.json")
            kw = dict(event_log=log, state_file=state, runner="lane", tier="t0")
            self.assertEqual(qhi.record_scan(d, now=NOW, **kw), ["scan_demand_ids"])
            on = dict(kw, detect_inversion=True)
            self.assertEqual(qhi.record_scan(d, now=NOW, **on), ["queue_head_inversion"])
            later = NOW + dt.timedelta(seconds=60)
            self.assertEqual(qhi.record_scan(d, now=later, **on), [])
            again = NOW + dt.timedelta(seconds=qhi.REEMIT_SECS + 1)
            self.assertEqual(qhi.record_scan(d, now=again, **on), ["queue_head_inversion"])
            events = [json.loads(x) for x in Path(log).read_text().splitlines()]
        inv = [e for e in events if e["event"] == "queue_head_inversion"]
        self.assertEqual(len(inv), 2)
        self.assertEqual(inv[0]["fields"]["job_id"], 10)
        self.assertNotIn("queue_head_inversion", [e["event"] for e in events[:1]])


class RobustnessTests(unittest.TestCase):
    def test_two_steady_tiers_sharing_a_lane_id_write_nothing_after_the_first(self) -> None:
        a = detail([(1, queued_job(10, 5))], [])
        b = detail([(2, queued_job(11, 4))], [])
        with tempfile.TemporaryDirectory() as tmp:
            kw = dict(event_log=str(Path(tmp) / "e.jsonl"), state_file=str(Path(tmp) / "s.json"),
                      runner="lane", now=NOW, detect_inversion=True)
            first = [qhi.record_scan(a, tier="t0", **kw), qhi.record_scan(b, tier="t1", **kw)]
            self.assertEqual(first, [["scan_demand_ids"], ["scan_demand_ids"]])
            for _ in range(5):
                self.assertEqual(qhi.record_scan(a, tier="t0", **kw), [])
                self.assertEqual(qhi.record_scan(b, tier="t1", **kw), [])
            self.assertEqual(len(Path(kw["event_log"]).read_text().splitlines()), 2)

    def test_a_witness_only_scan_writes_no_ids_and_detects_nothing(self) -> None:
        d = detail([(1, queued_job(10, 69))],
                   [(2, started_job(20, created=60, started=50, runner="r"))])
        d["witness_only"] = True
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "e.jsonl"
            got = qhi.record_scan(d, event_log=str(log), state_file=str(Path(tmp) / "s.json"),
                                  runner="lane", tier="t", now=NOW, detect_inversion=True)
            self.assertEqual(got, [])
            self.assertFalse(log.exists())

    def test_a_bad_threshold_env_falls_back_and_does_not_kill_either_parser(self) -> None:
        qargv = ["x", "--repo", "o/r", "--workflow", "w", "--labels", "a",
                 "--state-file", "s", "--provider", "p"]
        aargv = ["x", "--repo", "o/r", "--workflow", "w", "--labels", "a",
                 "--require-label", "b", "--class-labels", "b"]
        for value in ("30m", "", "0", "-5"):
            for module, argv in ((queue_scan, qargv), (assignment_scan, aargv)):
                with mock.patch.dict(os.environ, {"TARTCI_QUEUE_INVERSION_SECS": value}), \
                        mock.patch.object(sys, "argv", argv), \
                        mock.patch("sys.stderr"):
                    args = module.parse_args()
                self.assertEqual(args.inversion_threshold_secs, 1800, (module.__name__, value))
        with mock.patch.dict(os.environ, {"TARTCI_QUEUE_INVERSION_SECS": "600"}), \
                mock.patch.object(sys, "argv", qargv):
            self.assertEqual(queue_scan.parse_args().inversion_threshold_secs, 600)

    def test_labels_null_on_a_job_does_not_break_the_scan_note(self) -> None:
        scanner = object.__new__(queue_scan.QueueScanner)
        scanner.args = type("A", (), {"match_labels": 1})()
        scanner.labels = {"self-hosted"}
        d = slot_demand.DemandDetail()
        job = {"id": 1, "status": "in_progress", "runner_name": "r",
               "started_at": ago(5), "labels": None}
        scanner._note_started(d, run(1), job)
        self.assertEqual(d.as_dict()["started"], [])
        d.note_started(run(1), job)
        self.assertEqual(d.as_dict()["started"][0]["labels"], [])


class LabelTests(unittest.TestCase):
    GPU = ["self-hosted", "macos", "gpu"]
    PLAIN = ["self-hosted", "macos"]

    def test_gpu_job_is_not_inverted_by_newer_plain_jobs(self) -> None:
        d = detail([(1, queued_job(10, 90, self.GPU))],
                   [(2, started_job(20, 60, 50, "r", self.PLAIN))])
        self.assertEqual(qhi.detect(d, NOW), [])

    def test_plain_job_is_inverted_by_a_newer_gpu_job(self) -> None:
        d = detail([(1, queued_job(10, 90, self.PLAIN))],
                   [(2, started_job(20, 60, 50, "r", self.GPU))])
        self.assertEqual(len(qhi.detect(d, NOW)), 1)

    def test_gpu_job_is_inverted_by_a_newer_gpu_job(self) -> None:
        d = detail([(1, queued_job(10, 90, self.GPU))],
                   [(2, started_job(20, 60, 50, "r", self.GPU))])
        self.assertEqual(len(qhi.detect(d, NOW)), 1)


class ScanIntegrationTests(unittest.TestCase):
    def _scan(self, with_newer_runner: bool, detect: bool = True) -> list[dict[str, Any]]:
        now = dt.datetime.now(dt.timezone.utc)
        stamp = lambda m: (now - dt.timedelta(minutes=m)).isoformat().replace("+00:00", "Z")
        runs_queued = [{"id": 1, "name": WORKFLOW, "event": "merge_group", "status": "queued",
                        "created_at": stamp(69), "updated_at": stamp(69)}]
        runs_active = [{"id": 2, "name": WORKFLOW, "event": "merge_group",
                        "status": "in_progress", "created_at": stamp(60),
                        "updated_at": stamp(50)}]
        jobs = {1: [{"id": 11, "status": "queued", "created_at": stamp(69),
                     "labels": [*BASE, LABEL]}]}
        if with_newer_runner:
            jobs[2] = [{"id": 21, "status": "in_progress", "created_at": stamp(60),
                        "started_at": stamp(55), "runner_name": "studio-slot2",
                        "labels": [*BASE, LABEL]}]

        def api(path: str) -> dict[str, Any]:
            if path == "rate_limit":
                return helpers.AUTHENTICATED_RATE_LIMIT
            if path.endswith("/actions/workflows?per_page=100"):
                return {"workflows": [{"id": 99, "name": WORKFLOW}]}
            if "status=queued" in path:
                return {"workflow_runs": runs_queued}
            if "status=in_progress" in path:
                return {"workflow_runs": runs_active if with_newer_runner else []}
            if "status=pending" in path:
                return {"workflow_runs": []}
            for run_id, run_jobs in jobs.items():
                if f"/actions/runs/{run_id}/jobs?" in path:
                    return {"jobs": run_jobs}
            raise AssertionError(f"unexpected API path: {path}")

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "events.jsonl"
            scanner = helpers.PaginatedQueueScanTests()._scanner(
                "tart-macos", Path(tmp) / "state.json", api,
                force_refresh=True, workflow=WORKFLOW,
                labels=",".join([*BASE, LABEL]), merge_group_quota=2,
                oldest_reserve_max_age=172800, event_log=str(log),
                inversion_detect=detect, inversion_threshold_secs=1800)
            self.assertEqual(scanner.scan(), 1)
            if not log.exists():
                return []
            return [json.loads(x) for x in log.read_text().splitlines()]

    def test_positive_scan_emits_ids_and_the_inversion(self) -> None:
        events = self._scan(with_newer_runner=True)
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds, ["scan_demand_ids", "queue_head_inversion"], kinds)
        self.assertEqual(events[0]["fields"]["counted"]["merge_group"][0]["job_id"], 11)
        inv = events[1]["fields"]
        self.assertEqual((inv["run_id"], inv["job_id"]), (1, 11))
        self.assertEqual(inv["took"][0]["runner_name"], "studio-slot2")

    def test_negative_scan_full_fleet_logs_ids_but_no_inversion(self) -> None:
        kinds = [e["event"] for e in self._scan(with_newer_runner=False)]
        self.assertEqual(kinds, ["scan_demand_ids"])

    def test_detector_is_off_by_default(self) -> None:
        kinds = [e["event"] for e in self._scan(with_newer_runner=True, detect=False)]
        self.assertEqual(kinds, ["scan_demand_ids"])


class PoolStatusTests(unittest.TestCase):
    def test_scan_diagnostics_do_not_end_a_vm_mid_job(self) -> None:
        def ev(t: float, name: str, vm: str = "v1") -> dict:
            return {"t": NOW.timestamp() + t, "i": int(t), "event": name, "vm": vm,
                    "runner": "r", "detail": "", "fields": {}}
        events = [ev(0, "clone_start", ""), ev(1, "mint_jit"), ev(2, "job_assigned"),
                  ev(60, "scan_demand_ids", ""), ev(61, "queue_head_inversion", ""),
                  ev(600, "teardown")]
        vms = pool_usage.reconstruct_vms(events, NOW.timestamp() + 700, None)
        self.assertEqual(len(vms), 1)
        self.assertEqual(vms[0].end - vms[0].start, 600)
        self.assertEqual(vms[0].assigned_at, NOW.timestamp() + 2)

    def test_the_newest_observation_wins_across_lanes(self) -> None:
        d = detail([(1, queued_job(10, 69))],
                   [(2, started_job(20, created=60, started=50, runner="r"))])
        with tempfile.TemporaryDirectory() as tmp:
            for lane, when in (("a-lane", NOW + dt.timedelta(minutes=30)), ("b-lane", NOW)):
                path = Path(tmp) / lane
                path.mkdir()
                (path / "events.jsonl").write_text("")
                qhi.record_scan(d, event_log=str(path / "events.jsonl"),
                                state_file=str(path / "s.json"), runner=lane, tier="t",
                                now=when, detect_inversion=True)
            until = NOW.timestamp() + 3600
            rep = pool_usage.host_report(tmp, "h", until - 86400, until, until, 2, "t", 30)
        self.assertEqual(len(rep["queue_head_inversions"]), 1)
        self.assertEqual(rep["queue_head_inversions"][0]["at"],
                         slot_demand.iso(NOW + dt.timedelta(minutes=30)))

    def _report(self, with_event: bool) -> dict[str, Any]:
        d = detail([(1, queued_job(10, 69))],
                   [(2, started_job(20, created=60, started=50, runner="studio-slot2"))])
        with tempfile.TemporaryDirectory() as tmp:
            lane = Path(tmp) / "lane1"
            lane.mkdir()
            log = lane / "events.jsonl"
            log.write_text("")
            if with_event:
                qhi.record_scan(d, event_log=str(log), state_file=str(lane / "s.json"),
                                runner="lane1", tier="t", now=NOW, detect_inversion=True)
            until = NOW.timestamp() + 60
            return pool_usage.host_report(tmp, "h", until - 86400, until, until, 2, "test", 30)

    def test_inversion_is_surfaced_in_status_and_summary(self) -> None:
        rep = self._report(True)
        self.assertEqual(rep["queue_head_inversions"][0]["job_id"], 10)
        text = "\n".join(pool_usage.render_host(rep))
        self.assertIn("QUEUE-HEAD INVERSION: run 1 job 10", text)
        self.assertIn("cancel and re-run run 1", text)
        self.assertIn("QUEUE-HEAD INVERSION x1", pool_usage.summary_line(rep, 86400))

    def test_no_inversion_leaves_status_unchanged(self) -> None:
        rep = self._report(False)
        self.assertEqual(rep["queue_head_inversions"], [])
        self.assertNotIn("INVERSION", "\n".join(pool_usage.render_host(rep)))
        self.assertNotIn("INVERSION", pool_usage.summary_line(rep, 86400))


if __name__ == "__main__":
    unittest.main()
