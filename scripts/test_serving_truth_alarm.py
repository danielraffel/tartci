"""Serving-truth alarm: one host judging only itself.

Fixtures:
- fixtures/serving_truth_m1_20261013.json: m1's own lane event logs from
  2026-10-12T20:00Z to 2026-10-13T02:10Z, and every self-hosted job (with its
  run) of the Generous-Corp/pulp `Build and Test` runs created 2026-10-13
  00:30-02:06Z. A job was queued at T when created_at <= T < started_at.
- fixtures/gate_job_demand/: queue answers captured 2026-10-13 16:48Z and the
  fleet's advertised-labels.json (real host ids m1, studio, m5, m5studio).
- fixtures/serving_truth_real_replay.json: recorded rows from all four hosts.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gate_job_demand as demand
import serving_truth_alarm as alarm
import tartci_incident as incident

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
ADVERTISED = json.loads((FIX / "gate_job_demand" / "advertised-labels.json").read_text())
REGS = demand.registrations(ADVERTISED)
HOSTS = ("m1", "studio", "m5", "m5studio")
BASE = demand.epoch("2026-10-13T12:00:00Z")
PULP = "Generous-Corp/pulp"
M1 = json.loads((FIX / "serving_truth_m1_20261013.json").read_text())
SNAPSHOT = demand.epoch("2026-10-13T02:06:00Z")


def iso(ts: float) -> str:
    return alarm._iso(ts)


def row(ts: float, event: str, runner: str = "m5-pulp-gate-01", **extra: object) -> dict:
    return {"ts": iso(ts), "event": event, "runner": runner, **extra}


def gate_job(created: float, job_id: int = 7, label: str = "pulp-build-pr-head") -> dict:
    """A queued pulp macOS job, shaped as gate_job_demand returns it."""
    labels = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm", label]
    job = {"id": job_id, "run_id": 1, "repo": PULP, "workflow": "Build and Test",
           "name": "macos", "labels": labels, "created_at": iso(created)}
    job["hosts"] = demand.serving_hosts(job, PULP, "Build and Test", REGS)
    return job


def m1_fetch(at: float):
    """The production fetch, answered from the m1 capture as GitHub stood at `at`."""
    runs = {run["id"]: run for run in M1["runs"]}
    jobs: dict = {}
    for job in M1["jobs"]:
        jobs.setdefault(job["run_id"], []).append(job)

    def status(job: dict) -> str:
        created, started = demand.epoch(job["created_at"]), demand.epoch(job["started_at"])
        completed = demand.epoch(job["completed_at"])
        if created > at:
            return "absent"
        if started is None or at < started:
            return "queued"
        return "in_progress" if completed is None or at < completed else "completed"

    def fetch(path: str) -> dict:
        if path.startswith(f"repos/{PULP}/actions/runs?status="):
            listed = [dict(run, status="in_progress") for run in runs.values()
                      if demand.epoch(run["created_at"]) <= at
                      and any(status(j) in {"queued", "in_progress"} for j in jobs.get(run["id"], []))]
            listed = listed if "status=in_progress" in path else []
            return {"total_count": len(listed), "workflow_runs": listed}
        if path.startswith(f"repos/{PULP}/actions/runs/"):
            run = int(path.split("/actions/runs/", 1)[1].split("/", 1)[0])
            return {"jobs": [dict(j, status=status(j)) for j in jobs.get(run, [])
                             if status(j) != "absent"]}
        return {"total_count": 0, "workflow_runs": []}
    return fetch


def m1_demand(at: float) -> list:
    pulp = [reg for reg in REGS if reg["repo"] == PULP]
    jobs, errors = demand.queued_jobs(m1_fetch(at), pulp, at)
    assert errors == [], errors
    return jobs


def rules(trips: list) -> set:
    return {trip["rule"] for trip in trips}


class HostIdentityTests(unittest.TestCase):
    def test_runner_names_map_to_the_advertised_host_ids(self) -> None:
        for runner, host in (("m1-pulp-gate-01", "m1"), ("studio-pulp-gate-slot2-02", "studio"),
                             ("m5-pulp-gate-01", "m5"), ("m5studio-pulp-gate-01", "m5studio")):
            self.assertEqual(alarm.host_of({"runner": runner}), host)
        self.assertEqual({reg["host_id"] for reg in REGS}, set(HOSTS))
        nested = {"event": "job_assigned", "fields": {"runner": "m5studio-forge-gate-01"}}
        self.assertEqual(alarm.host_of(nested), "m5studio")

    def test_nicknames_m3_and_m5s_judge_the_advertised_host(self) -> None:
        # The mapping is the one shared table (gate_job_demand.HOST_ALIASES):
        # a host configured as m3 or m5s judges studio or m5studio, never a
        # host no registration names.
        self.assertEqual(alarm.host_of({"host": "m3"}), "studio")
        self.assertEqual(alarm.host_of({"runner": "m5s-pulp-gate-01"}), "m5studio")
        jobs = [gate_job(BASE - 43 * 60)]
        for nickname, host in (("m3", "studio"), ("m5s", "m5studio")):
            with self.subTest(host=host):
                silent = alarm.classify([], jobs, me=nickname, regs=REGS, now=BASE)
                self.assertEqual([(t["rule"], t["host"]) for t in silent],
                                 [("host_queued_no_job_start", host)])
                started = [row(BASE - 120, "job_assigned", runner=f"{host}-pulp-gate-01")]
                self.assertEqual(alarm.classify(started, jobs, me=nickname, regs=REGS, now=BASE), [])

    def test_every_host_that_just_started_a_job_is_quiet(self) -> None:
        # The review's repro: all four hosts assigned a job 2 min ago, while a
        # job they all serve has waited 43 min. Nobody trips.
        rows = [row(BASE - 120, "job_assigned", runner=f"{host}-pulp-gate-01") for host in HOSTS]
        rows += [row(BASE - 60, "job_terminal_receipt", runner=f"{host}-pulp-gate-01")
                 for host in HOSTS]
        jobs = [gate_job(BASE - 43 * 60)]
        self.assertEqual(jobs[0]["hosts"], sorted(HOSTS))
        for host in HOSTS:
            with self.subTest(host=host):
                self.assertEqual(alarm.classify(rows, jobs, me=host, regs=REGS, now=BASE), [])


class JudgeOnlySelfTests(unittest.TestCase):
    def test_a_host_never_reports_a_verdict_about_a_peer(self) -> None:
        # m1's own view: it assigned a job 2 min ago, one job has waited 43 min.
        rows = [row(BASE - 120, "job_assigned", runner="m1-pulp-gate-01")]
        jobs = [gate_job(BASE - 43 * 60)]
        self.assertEqual(alarm.classify(rows, jobs, me="m1", regs=REGS, now=BASE), [])

    def test_a_silent_host_trips_only_for_itself(self) -> None:
        rows = [row(BASE - 120, "job_assigned", runner="m1-pulp-gate-01")]
        jobs = [gate_job(BASE - 43 * 60)]
        trips = alarm.classify(rows, jobs, me="m5", regs=REGS, now=BASE)
        self.assertEqual([(t["rule"], t["host"]) for t in trips], [("host_queued_no_job_start", "m5")])
        for host in HOSTS:
            for trip in alarm.classify(rows + [row(BASE - 50, "admission_error", runner="m5-pulp-gate-01")] * 3,
                                       jobs, me=host, regs=REGS, now=BASE):
                self.assertEqual(trip["host"], host)

    def test_peer_rows_never_count_as_this_hosts_starts_or_errors(self) -> None:
        jobs = [gate_job(BASE - 43 * 60)]
        peer = [row(BASE - 120, "job_assigned", runner="studio-pulp-gate-01")]
        self.assertIn("host_queued_no_job_start",
                      rules(alarm.classify(peer, jobs, me="m5studio", regs=REGS, now=BASE)))
        errors = [row(BASE - 300 + 30 * i, "admission_error", runner="studio-pulp-gate-01")
                  for i in range(3)]
        self.assertEqual(alarm.classify(errors, me="m5studio", regs=REGS, now=BASE), [])
        self.assertEqual(rules(alarm.classify(errors, me="studio", regs=REGS, now=BASE)),
                         {"admission_error_streak"})


class DemandTests(unittest.TestCase):
    def test_only_gate_jobs_this_host_serves_are_demand(self) -> None:
        hosted = dict(gate_job(BASE - 3600), labels=["windows-latest"], hosts=[])
        intel = dict(gate_job(BASE - 3600), labels=["self-hosted", "pulp-intel-native"], hosts=[])
        self.assertEqual(alarm.classify([], [hosted, intel], me="m1", regs=REGS, now=BASE), [])
        self.assertIn("host_queued_no_job_start",
                      rules(alarm.classify([], [gate_job(BASE - 3600)], me="m1", regs=REGS, now=BASE)))

    def test_a_job_queued_less_than_the_window_does_not_trip(self) -> None:
        self.assertEqual(alarm.classify([], [gate_job(BASE - 19 * 60)], me="m1", regs=REGS, now=BASE), [])

    def test_an_old_zombie_job_is_not_demand(self) -> None:
        zombie = gate_job(demand.epoch("2026-08-19T00:00:00Z"))
        self.assertEqual(alarm.classify([], [zombie], me="m1", regs=REGS, now=BASE), [])

    def test_the_captured_queue_is_demand_through_the_production_fetch(self) -> None:
        # 16:48:10Z: the in-progress run's queued macOS job, 1 min old. At 17:10
        # it has waited 23 min, and m1 logged no job start.
        import test_gate_job_demand as captured  # noqa: PLC0415
        later = demand.epoch("2026-10-13T17:10:00Z")
        jobs, errors = demand.queued_jobs(captured.Recorder(), captured.pulp_registrations(), later)
        self.assertEqual(errors, [])
        trips = alarm.classify([], jobs, me="m1", regs=REGS, now=later)
        self.assertEqual(rules(trips), {"host_queued_no_job_start"})
        self.assertEqual([j["id"] for j in trips[0]["evidence"]["queued_jobs"]], [115420876725])


class BusyTests(unittest.TestCase):
    def test_a_host_whose_serving_lanes_all_hold_jobs_is_not_flagged(self) -> None:
        jobs = [gate_job(BASE - 3600)]
        busy = [row(BASE - 50 * 60, "job_assigned", runner="m1-pulp-gate-01"),
                row(BASE - 45 * 60, "job_assigned", runner="m1-pulp-gate-slot2-02")]
        self.assertEqual(alarm.classify(busy, jobs, me="m1", regs=REGS, now=BASE), [])
        one_free = busy + [row(BASE - 40 * 60, "job_terminal_receipt", runner="m1-pulp-gate-slot2-02")]
        self.assertIn("host_queued_no_job_start",
                      rules(alarm.classify(one_free, jobs, me="m1", regs=REGS, now=BASE)))

    def test_a_busy_lane_that_does_not_serve_the_job_does_not_suppress(self) -> None:
        jobs = [gate_job(BASE - 3600)]
        other = [row(BASE - 50 * 60, "job_assigned", runner="m1-vellum-gate-01")]
        self.assertIn("host_queued_no_job_start",
                      rules(alarm.classify(other, jobs, me="m1", regs=REGS, now=BASE)))

    def test_an_assignment_older_than_the_lookback_is_not_running(self) -> None:
        jobs = [gate_job(BASE - 3600)]
        stale = [row(BASE - alarm.LOOKBACK_SECONDS - 60, "job_assigned", runner="m1-pulp-gate-01"),
                 row(BASE - 3 * 3600, "scan_blind", runner="m1-pulp-gate-01")]
        self.assertIn("host_queued_no_job_start",
                      rules(alarm.classify(stale, jobs, me="m1", regs=REGS, now=BASE)))


class RuleTests(unittest.TestCase):
    def test_admission_precheck_is_not_a_job_start(self) -> None:
        rows = [row(BASE - 60, "admission_precheck", runner="m1-pulp-gate-01"),
                row(BASE - 30, "admission_check", runner="m1-pulp-gate-01")]
        self.assertIn("host_queued_no_job_start",
                      rules(alarm.classify(rows, [gate_job(BASE - 1800)], me="m1", regs=REGS, now=BASE)))

    def test_scan_blind_on_a_serving_lane_trips_and_other_lanes_do_not(self) -> None:
        jobs = [gate_job(BASE - 60)]
        blind = [row(BASE - 180 + 60 * i, "scan_blind", runner="m1-pulp-gate-01",
                     detail=f"consecutive={i + 1} running=0/2") for i in range(3)]
        self.assertEqual(rules(alarm.classify(blind, jobs, me="m1", regs=REGS, now=BASE)),
                         {"scan_blind_while_queued"})
        vellum = [dict(r, runner="m1-vellum-gate-01") for r in blind]
        self.assertEqual(alarm.classify(vellum, jobs, me="m1", regs=REGS, now=BASE), [])
        self.assertEqual(alarm.classify(blind, [], me="m1", regs=REGS, now=BASE), [])
        two = blind[:2]
        self.assertEqual(alarm.classify(two, jobs, me="m1", regs=REGS, now=BASE), [])

    def test_admission_streak_and_its_breakers(self) -> None:
        def errs(*kinds: str) -> list:
            return [row(BASE - 300 + 30 * i, kind) for i, kind in enumerate(kinds)]
        self.assertEqual(rules(alarm.classify(errs("admission_error", "admission_precheck_error",
                                                   "admission_error"), me="m5", now=BASE)),
                         {"admission_error_streak"})
        for breaker in ("admission_precheck", "job_assigned"):
            with self.subTest(breaker=breaker):
                self.assertEqual(alarm.classify(errs("admission_error", "admission_error", breaker,
                                                     "admission_error", "admission_error"),
                                                me="m5", now=BASE), [])

    def test_burst_needs_three_heads(self) -> None:
        rows = [row(BASE - 100, "job_failed", head_sha="a" * 40, error="license error")]
        self.assertEqual(alarm.classify(rows, me="m5", now=BASE), [])
        rows += [row(BASE - 80, "job_failed", head_sha="b" * 40, error="license error"),
                 row(BASE - 60, "job_failed", head_sha="c" * 40, error="license error")]
        self.assertEqual(rules(alarm.classify(rows, me="m5", now=BASE)), {"host_error_burst"})

    def test_signal_death_needs_a_held_job_on_the_same_lane(self) -> None:
        signal = "INT/TERM quarantine=signal_teardown_unknown"
        rows = [row(BASE - 60, "job_assigned", runner="m5-pulp-gate-01"),
                row(BASE - 10, "supervisor_signal", runner="m5-pulp-gate-slot2-02", detail=signal)]
        self.assertEqual(alarm.classify(rows, me="m5", now=BASE), [])
        rows.append(row(BASE - 5, "supervisor_signal", runner="m5-pulp-gate-01", detail=signal))
        self.assertEqual(rules(alarm.classify(rows, me="m5", now=BASE)), {"lane_death_by_signal"})
        clean = [row(BASE - 60, "job_assigned"), row(BASE - 10, "supervisor_signal",
                                                      detail="TERM quarantine=none")]
        self.assertEqual(alarm.classify(clean, me="m5", now=BASE), [])


    def test_a_signal_after_the_lanes_teardown_is_not_a_death(self) -> None:
        # m1, 2026-10-16: the job ended (listener exited, workflow still
        # running, so no terminal receipt), the VM was torn down, and 61 s
        # later the self-update stopped the idle supervisor, which logged the
        # last job's quarantine.
        quarantined = "INT/TERM quarantine=listener_exited_workflow_active"
        rows = [row(BASE - 2600, "job_assigned", runner="m5-pulp-gate-01",
                    vm="m5-pulp-gate-01-74810-4"),
                row(BASE - 70, "job_lifecycle_quarantine", runner="m5-pulp-gate-01",
                    detail="runner_rc=0 observation=terminal_pending_run "
                           "quarantine=listener_exited_workflow_active"),
                row(BASE - 70, "teardown", runner="m5-pulp-gate-01", detail="rc=0"),
                row(BASE - 63, "vm_lifecycle", runner="m5-pulp-gate-01", vm="",
                    detail="served=1 rc=0"),
                row(BASE - 10, "supervisor_signal", runner="m5-pulp-gate-01", vm="",
                    detail=quarantined)]
        self.assertEqual(alarm.classify(rows, me="m5", now=BASE), [])
        # Control, same rows without the teardown: the lane still held its job.
        held = [r for r in rows if r["event"] != "teardown"]
        self.assertEqual(rules(alarm.classify(held, me="m5", now=BASE)), {"lane_death_by_signal"})

    def test_a_death_mid_job_still_trips_though_cleanup_tears_down_after(self) -> None:
        signal = "INT/TERM quarantine=signal_teardown_unknown"
        rows = [row(BASE - 600, "job_assigned"),
                row(BASE - 10, "supervisor_signal", detail=signal),
                row(BASE - 9, "teardown", detail="rc=0")]
        self.assertEqual(rules(alarm.classify(rows, me="m5", now=BASE)), {"lane_death_by_signal"})
        # A teardown of an earlier VM does not clear a later assignment.
        rows = [row(BASE - 900, "teardown", detail="rc=0"), row(BASE - 600, "job_assigned"),
                row(BASE - 10, "supervisor_signal", detail=signal)]
        self.assertEqual(rules(alarm.classify(rows, me="m5", now=BASE)), {"lane_death_by_signal"})


class RealReplayTests(unittest.TestCase):
    def test_m1_on_2026_10_13_trips_within_20_minutes(self) -> None:
        # m1's last job_assigned was 01:20:32Z, its lanes went scan-blind from
        # 01:39:56Z, and 8 macOS gate jobs were queued at 02:06Z.
        rows = M1["rows"]
        jobs = m1_demand(SNAPSHOT)
        self.assertEqual(len(jobs), 8)
        self.assertTrue(all("m1" in job["hosts"] for job in jobs))
        trips = alarm.classify(rows, jobs, me="m1", regs=REGS, now=SNAPSHOT)
        self.assertIn("host_queued_no_job_start", rules(trips))
        # m1 went free at its last terminal receipt; that is when the symptom began.
        self.assertEqual(next(t for t in trips if t["rule"] == "host_queued_no_job_start")["since"],
                         "2026-10-13T01:37:19Z")
        first = None
        for minute in range(0, 61):
            at = demand.epoch("2026-10-13T01:20:32Z") + 20 * 60 + minute * 60
            if alarm.classify(rows, m1_demand(at), me="m1", regs=REGS, now=at):
                first = at
                break
        self.assertIsNotNone(first)
        # The alarm fires 20 min after the last start plus at most 20 min of
        # queue age; m1 was silent from 01:39:56Z.
        self.assertLessEqual(first - demand.epoch("2026-10-13T01:39:56Z"), 20 * 60)

    def test_m1_is_quiet_while_it_is_serving(self) -> None:
        # 01:25Z: m1 started jobs at 01:02 and 01:20; nothing to report.
        at = demand.epoch("2026-10-13T01:25:00Z")
        self.assertEqual(alarm.classify(M1["rows"], m1_demand(at), me="m1", regs=REGS, now=at), [])

    def test_the_quiet_day_has_no_admission_trips_on_any_host(self) -> None:
        fixture = json.loads((FIX / "serving_truth_real_replay.json").read_text())
        quiet = fixture["cases"]["quiet"]
        end = alarm._epoch(quiet[-1]["ts"])
        for host in HOSTS:
            with self.subTest(host=host):
                self.assertEqual(alarm.classify(quiet, me=host, regs=REGS, now=end), [])


class BodyTests(unittest.TestCase):
    def trip(self) -> dict:
        jobs = m1_demand(SNAPSHOT)
        trips = alarm.classify(M1["rows"], jobs, me="m1", regs=REGS, now=SNAPSHOT)
        return next(t for t in trips if t["rule"] == "host_queued_no_job_start")

    def test_the_body_is_actionable_and_ends_with_one_marker(self) -> None:
        trip = self.trip()
        title, body = alarm.render(trip, "2026-10-13T02:06:00Z")
        self.assertEqual(title, "[tartci] serving-truth: host_queued_no_job_start on m1")
        for heading in ("## What happened", "## Evidence", "## Likely cause", "## Fix (run on `m1`)",
                        "## Safe to auto-apply", "## How to verify"):
            self.assertIn(heading, body)
        self.assertIn("`m1` started 0 gate jobs", body)
        self.assertIn("job 115079270964", body)
        self.assertNotIn("job 115090933480", body)  # queued 12 min: not yet waiting the window
        self.assertIn("2026-10-13T01:20:32Z on `m1-pulp-gate-slot2-02`", body)
        self.assertIn("scan_blind", body)
        self.assertIn("observation lock", body)
        self.assertIn("queue-observation.lock.owner.json", body)
        self.assertIn("tartci pool status", body)
        marker = incident.parse(body)
        self.assertEqual(marker, {"host": "m1", "class": "host_queued_no_job_start",
                                  "symptom_at": trip["since"], "detected_at": "2026-10-13T02:06:00Z",
                                  "fixed_at": None})
        self.assertEqual(sum(1 for line in body.split("\n") if line.startswith(incident.PREFIX)), 1)

    def test_every_rule_renders_every_section(self) -> None:
        signal = "INT/TERM quarantine=signal_teardown_unknown"
        cases = {
            "scan_blind_while_queued": ([row(BASE - 180 + 60 * i, "scan_blind", runner="m1-pulp-gate-01",
                                             detail=f"consecutive={i + 1}") for i in range(3)],
                                        [gate_job(BASE - 60)]),
            "admission_error_streak": ([row(BASE - 90 + 30 * i, "admission_error", runner="m1-pulp-gate-01",
                                            detail="exit 255") for i in range(3)], []),
            "host_error_burst": ([row(BASE - 90 + 30 * i, "job_failed", runner="m1-pulp-gate-01",
                                      head_sha=c * 40, error="license error") for i, c in enumerate("abc")], []),
            "lane_death_by_signal": ([row(BASE - 60, "job_assigned", runner="m1-pulp-gate-01"),
                                      row(BASE - 10, "supervisor_signal", runner="m1-pulp-gate-01",
                                          detail=signal)], []),
        }
        for rule, (rows, jobs) in cases.items():
            with self.subTest(rule=rule):
                trip = next(t for t in alarm.classify(rows, jobs, me="m1", regs=REGS, now=BASE)
                            if t["rule"] == rule)
                _, body = alarm.render(trip, iso(BASE))
                for heading in ("## What happened", "## Evidence", "## Likely cause", "## Fix",
                                "## Safe to auto-apply", "## How to verify"):
                    self.assertIn(heading, body)
                self.assertEqual(incident.parse(body)["class"], rule)


class GitHub:
    """Fake issue API recording every call in order."""

    def __init__(self, fail: set | None = None) -> None:
        self.calls: list = []
        self.bodies: dict = {}
        self.fail = fail or set()
        self.next = 100

    def open(self, title: str, body: str):
        self.calls.append(("open", title))
        if "open" in self.fail:
            return 1, "HTTP 502"
        self.next += 1
        self.bodies[str(self.next)] = body
        return 0, str(self.next)

    def patch(self, number: str, body: str):
        self.calls.append(("patch", number))
        if "patch" in self.fail:
            return 1, "HTTP 502"
        self.bodies[number] = body
        return 0, number

    def close(self, number: str):
        self.calls.append(("close", number))
        if "close" in self.fail:
            return 1, "HTTP 502"
        return 0, "closed"


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "serving-truth"
        self.gh = GitHub()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def tick(self, rows: list, jobs: list, at: float, shadow: bool = False) -> dict:
        return alarm.alert_pass(self.state, rows, jobs, me="m5", regs=REGS, now=at, shadow=shadow,
                                issue=self.gh.open, patch=self.gh.patch, close=self.gh.close)

    def test_each_episode_has_its_own_detection_and_fix(self) -> None:
        jobs = [gate_job(BASE - 3600)]
        first = self.tick([], jobs, BASE)
        self.assertEqual(first["opened"], ["101"])
        self.assertEqual(self.tick([], jobs, BASE + 300)["opened"], [])
        start = [row(BASE + 600, "job_assigned")]
        fixed = self.tick(start, jobs, BASE + 660)
        self.assertEqual(fixed["fixed"][0]["fixed_at"], iso(BASE + 600))
        self.assertEqual(fixed["fixed"][0]["detected_at"], iso(BASE))
        self.assertEqual(self.gh.calls[-2:], [("patch", "101"), ("close", "101")])
        # A day later: a new episode with its own timestamps and its own issue.
        later = BASE + 86400
        second = self.tick([], [gate_job(later - 3600)], later)
        self.assertEqual(second["opened"], ["102"])
        ep = second["episodes"]["host_queued_no_job_start:m5"]
        self.assertEqual((ep["detected_at"], ep["fixed_at"]), (iso(later), None))
        self.assertEqual(incident.parse(self.gh.bodies["102"])["detected_at"], iso(later))

    def test_a_success_from_before_detection_never_fixes(self) -> None:
        jobs = [gate_job(BASE - 3600)]
        old = [row(BASE - 5000, "job_assigned"),
               row(BASE - 4900, "job_terminal_receipt", detail="result=Succeeded")]
        self.tick(old, jobs, BASE)
        out = self.tick(old, [], BASE + 600)
        self.assertEqual(out["fixed"], [])
        self.assertIn("host_queued_no_job_start:m5", out["episodes"])
        self.assertNotIn(("close", "101"), self.gh.calls)

    def test_a_peer_start_never_fixes_this_hosts_episode(self) -> None:
        # The queue drained because studio took the job; m5 has still not
        # started anything since detection, so its episode stays open.
        self.tick([], [gate_job(BASE - 3600)], BASE)
        peer = [row(BASE + 120, "job_assigned", runner="studio-pulp-gate-01")]
        out = self.tick(peer, [], BASE + 180)
        self.assertEqual(out["fixed"], [])
        self.assertIn("host_queued_no_job_start:m5", out["episodes"])
        self.assertNotIn(("close", "101"), self.gh.calls)
        # Control: the same start on m5 itself fixes it.
        own = [row(BASE + 240, "job_assigned", runner="m5-pulp-gate-01")]
        self.assertEqual(self.tick(peer + own, [], BASE + 300)["fixed"][0]["fixed_at"], iso(BASE + 240))

    def test_the_fix_is_patched_into_the_marker_before_close(self) -> None:
        jobs = [gate_job(BASE - 3600)]
        self.tick([], jobs, BASE)
        self.tick([row(BASE + 120, "job_assigned")], jobs, BASE + 180)
        body = self.gh.bodies["101"]
        marker = incident.parse(body)
        self.assertEqual(marker["fixed_at"], iso(BASE + 120))
        self.assertEqual(marker["detected_at"], iso(BASE))
        self.assertEqual(sum(1 for line in body.split("\n") if line.startswith(incident.PREFIX)), 1)
        self.assertLess(self.gh.calls.index(("patch", "101")), self.gh.calls.index(("close", "101")))

    def test_a_failed_patch_keeps_the_issue_open_and_retries(self) -> None:
        jobs = [gate_job(BASE - 3600)]
        self.tick([], jobs, BASE)
        self.gh.fail = {"patch"}
        start = [row(BASE + 120, "job_assigned")]
        out = self.tick(start, jobs, BASE + 180)
        self.assertEqual(out["fixed"], [])
        self.assertNotIn(("close", "101"), self.gh.calls)
        self.gh.fail = set()
        out = self.tick(start, jobs, BASE + 480)
        self.assertEqual(out["fixed"][0]["fixed_at"], iso(BASE + 120))
        self.assertEqual(self.gh.calls[-2:], [("patch", "101"), ("close", "101")])

    def test_a_failed_open_is_retried(self) -> None:
        jobs = [gate_job(BASE - 3600)]
        self.gh.fail = {"open"}
        self.assertEqual(self.tick([], jobs, BASE)["opened"], [])
        self.gh.fail = set()
        self.assertEqual(self.tick([], jobs, BASE + 300)["opened"], ["101"])
        self.assertEqual(incident.parse(self.gh.bodies["101"])["detected_at"], iso(BASE))

    def test_shadow_never_touches_github_and_still_tracks_the_episode(self) -> None:
        jobs = [gate_job(BASE - 3600)]
        out = self.tick([], jobs, BASE, shadow=True)
        self.assertEqual(self.gh.calls, [])
        self.assertTrue(out["receipts"])
        ep = out["episodes"]["host_queued_no_job_start:m5"]
        self.assertEqual(incident.parse(ep["issue_body"])["fixed_at"], None)
        out = self.tick([row(BASE + 120, "job_assigned")], jobs, BASE + 180, shadow=True)
        self.assertEqual(out["fixed"][0]["fixed_at"], iso(BASE + 120))
        self.assertEqual(self.gh.calls, [])

    def test_evidence_from_before_the_last_fix_does_not_reopen(self) -> None:
        errors = [row(BASE - 90 + 30 * i, "admission_error") for i in range(3)]
        self.tick(errors, [], BASE)
        recovered = errors + [row(BASE + 60, "job_assigned")]
        # The assignment fixes the episode; the same old errors, read again
        # without it, cannot start a new one.
        self.assertTrue(self.tick(recovered, [], BASE + 120)["fixed"])
        self.assertEqual(self.tick(errors, [], BASE + 180)["opened"], [])
        fresh = errors + [row(BASE + 200 + 30 * i, "admission_error") for i in range(3)]
        self.assertEqual(self.tick(fresh, [], BASE + 300)["opened"], ["102"])


class ReadTests(unittest.TestCase):
    def test_only_the_recent_tail_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            old = "".join(json.dumps(row(BASE - 86400 + i, "scan_blind")) + "\n" for i in range(20000))
            new = [row(BASE - 60 + i, "job_assigned") for i in range(5)]
            path.write_text(old + "".join(json.dumps(r) + "\n" for r in new) + "not json\n")
            read = []
            real_open = Path.open

            def counting_open(self, *args, **kwargs):
                handle = real_open(self, *args, **kwargs)
                real_read = handle.read

                def tracked(n=-1):
                    data = real_read(n)
                    read.append(len(data))
                    return data
                handle.read = tracked
                return handle
            with mock.patch.object(Path, "open", counting_open):
                rows = alarm.read_events_since(path, BASE - 3600, chunk=512)
            self.assertEqual(rows, new)
            self.assertLess(sum(read), 4096)
            self.assertGreater(path.stat().st_size, 1_000_000)

    def test_rows_split_across_chunks_are_whole(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            rows = [row(BASE - 600 + i, "job_assigned", detail="x" * (i * 7)) for i in range(50)]
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            self.assertEqual(alarm.read_events_since(path, 0, chunk=64), rows)
            self.assertEqual(alarm.read_events_since(path, BASE - 600 + 25, chunk=64), rows[25:])


class WatchdogTests(unittest.TestCase):
    def test_the_watchdog_pass_judges_this_host_in_shadow(self) -> None:
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            lane = home / "state" / "macos-fleet" / "pulp-gate"
            lane.mkdir(parents=True)
            (lane / "events.jsonl").write_text(
                json.dumps(row(BASE - 120, "job_assigned", runner="studio-pulp-gate-01")) + "\n")
            env = {"TARTCI_HOME": str(home), "TARTCI_HOST_ID": "m5",
                   "TARTCI_ADVERTISED_LABELS": str(FIX / "gate_job_demand" / "advertised-labels.json")}
            calls: list = []
            with mock.patch.dict("os.environ", env, clear=True), \
                    mock.patch.object(watchdog, "utcnow", return_value=BASE), \
                    mock.patch.object(alarm, "fetch_demand",
                                      lambda cli, adv, me, now: (calls.append(me) or [gate_job(BASE - 3600)], [])), \
                    mock.patch("host_off._open_issue", side_effect=AssertionError("shadow opened an issue")):
                line = watchdog.serving_truth_pass()
            self.assertEqual(calls, ["m5"])
            self.assertIn("host_queued_no_job_start", line)
            self.assertIn("on m5", line)
            episodes = json.loads((home / "state" / "serving-truth" / alarm.STATE_NAME).read_text())
            self.assertEqual(list(episodes["episodes"]), ["host_queued_no_job_start:m5"])

    def test_an_open_episode_is_reported_once_not_every_pass(self) -> None:
        # m1, 2026-10-16: the same trip was logged at 03:11, 03:16 and 03:22,
        # because its evidence stays in the lookback.
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            lane = home / "state" / "macos-fleet" / "pulp-gate"
            lane.mkdir(parents=True)
            signal = "INT/TERM quarantine=signal_teardown_unknown"
            (lane / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
                row(BASE - 600, "job_assigned", runner="m5-pulp-gate-01"),
                row(BASE - 30, "supervisor_signal", runner="m5-pulp-gate-01", detail=signal))))
            env = {"TARTCI_HOME": str(home), "TARTCI_HOST_ID": "m5",
                   "TARTCI_ADVERTISED_LABELS": str(FIX / "gate_job_demand" / "advertised-labels.json")}
            lines = []
            for at in (BASE, BASE + 300, BASE + 600):
                with mock.patch.dict("os.environ", env, clear=True), \
                        mock.patch.object(watchdog, "utcnow", return_value=at), \
                        mock.patch.object(alarm, "fetch_demand", lambda cli, adv, me, now: ([], [])):
                    lines.append(watchdog.serving_truth_pass())
            self.assertIn("1 rule trip(s) on m5 (lane_death_by_signal), evidence recorded", lines[0])
            self.assertEqual(lines[1:], [None, None])
            episodes = json.loads((home / "state" / "serving-truth" / alarm.STATE_NAME).read_text())
            self.assertEqual(list(episodes["episodes"]), ["lane_death_by_signal:m5"])

    def test_a_failed_demand_read_is_a_warn_line(self) -> None:
        # A blind demand read reads as "no demand" to every rule; this line is
        # the only place it shows. m5 just started a job, so nothing trips.
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        failure = "repos/Generous-Corp/pulp/actions/runs?status=queued&per_page=100: gh exit 1: HTTP 502"
        lines = {}
        for name, errors in (("failed", [failure]), ("control", [])):
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                lane = home / "state" / "macos-fleet" / "pulp-gate"
                lane.mkdir(parents=True)
                (lane / "events.jsonl").write_text(
                    json.dumps(row(BASE - 120, "job_assigned", runner="m5-pulp-gate-01")) + "\n")
                env = {"TARTCI_HOME": str(home), "TARTCI_HOST_ID": "m5",
                       "TARTCI_ADVERTISED_LABELS": str(FIX / "gate_job_demand" / "advertised-labels.json")}
                with mock.patch.dict("os.environ", env, clear=True), \
                        mock.patch.object(watchdog, "utcnow", return_value=BASE), \
                        mock.patch.object(alarm, "fetch_demand",
                                          lambda cli, adv, me, now, errors=errors: ([], list(errors))):
                    lines[name] = watchdog.serving_truth_pass()
        self.assertIsNotNone(lines["failed"], "a failed demand read printed nothing")
        self.assertIn("launchd-watchdog: WARN serving-truth: gate demand partly unread: ", lines["failed"])
        self.assertIn("HTTP 502", lines["failed"])
        self.assertNotIn("rule trip", lines["failed"])
        self.assertIsNone(lines["control"])

    def test_an_unknown_host_id_is_reported_not_guessed(self) -> None:
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            lane = home / "state" / "macos-fleet" / "pulp-gate"
            lane.mkdir(parents=True)
            (lane / "events.jsonl").write_text(json.dumps(row(BASE, "job_assigned")) + "\n")
            env = {"TARTCI_HOME": str(home),
                   "TARTCI_MACOS_FLEET_PROFILE": str(home / "absent.toml")}
            with mock.patch.dict("os.environ", env, clear=True):
                line = watchdog.serving_truth_pass()
            self.assertIn("host_id is unknown", line)



class ConfiguredFleetSourceWatchdogTests(unittest.TestCase):
    def test_an_invalid_configured_source_fails_the_pass_without_another_supply(self) -> None:
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        import test_fleet_source  # noqa: PLC0415 - owns what a valid source is
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            lane = home / "state" / "macos-fleet" / "pulp-gate"
            lane.mkdir(parents=True)
            (lane / "events.jsonl").write_text(
                json.dumps(row(BASE - 120, "job_assigned", runner="studio-pulp-gate-01")) + "\n")
            env = {"TARTCI_HOME": str(home), "TARTCI_HOST_ID": "m5",
                   "TARTCI_CHECKOUT": str(FIX / "gate_job_demand"),
                   **test_fleet_source.configured_source(home / "source")}
            (home / "source" / "profiles" / "canary-macos-fleet.toml").write_text("schema = 2\n")
            with mock.patch.dict("os.environ", env, clear=True), \
                    mock.patch.object(watchdog, "utcnow", return_value=BASE), \
                    mock.patch.object(alarm, "fetch_demand",
                                      side_effect=AssertionError("read a supply")):
                line = watchdog.serving_truth_pass()
            self.assertIn("WARN serving-truth check FAILED (ValueError", line)
            self.assertIn("stale or incomplete", line)

    def test_a_valid_configured_source_is_the_supply_read(self) -> None:
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        import test_fleet_source  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            lane = home / "state" / "macos-fleet" / "pulp-gate"
            lane.mkdir(parents=True)
            (lane / "events.jsonl").write_text(
                json.dumps(row(BASE - 120, "job_assigned", runner="studio-pulp-gate-01")) + "\n")
            fixture = json.loads((FIX / "gate_job_demand" / "advertised-labels.json").read_text())
            env = {"TARTCI_HOME": str(home), "TARTCI_HOST_ID": "m5",
                   **test_fleet_source.configured_source(
                       home / "source", {"registrations": fixture["registrations"]})}
            seen: list = []
            with mock.patch.dict("os.environ", env, clear=True), \
                    mock.patch.object(watchdog, "utcnow", return_value=BASE), \
                    mock.patch.object(alarm, "fetch_demand",
                                      lambda cli, adv, me, now: (seen.append(adv) or [], [])):
                watchdog.serving_truth_pass()
            self.assertEqual(1, len(seen))
            self.assertEqual(test_fleet_source.PRIVATE_REPOSITORY, seen[0]["generated_from"]["repo"])


if __name__ == "__main__":
    unittest.main()
