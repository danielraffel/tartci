#!/usr/bin/env python3
"""The interval guard starts fleet timer jobs launchd has stopped starting.

launchd is replaced by a fake `launchctl` (an injected runner, or a script on
PATH for the supervisor-child test) whose `runs` counter only moves when the
test says so, the way m3's froze on 2026-10-04.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import fleet_doctor as fd  # noqa: E402
import launchd_interval_guard as lig  # noqa: E402

LIB = ROOT / "providers/tart-macos/interval-guard.lib.sh"
RUNNER = ROOT / "providers/tart-macos/runner.sh"


def write_plist(agents: Path, label: str, **spec) -> None:
    value = {"Label": label, "ProgramArguments": ["/bin/true"]}
    value.update(spec)
    with (agents / f"{label}.plist").open("wb") as handle:
        plistlib.dump(value, handle)


class FakeLaunchd:
    """`launchctl print` / `kickstart` over an in-memory job table."""

    def __init__(self) -> None:
        self.jobs: Dict[str, Dict] = {}
        self.calls: List[List[str]] = []
        self.fail_print = False
        self.raise_on_print = False
        self.disabled: List[str] = []
        self.fail_disabled = False

    def add(self, label: str, runs: int = 10, running: bool = False,
            pended: str = "interval", last_exit: str = "0", state: str = "") -> None:
        self.jobs[label] = {"runs": runs, "running": running, "pended": pended,
                            "last_exit": last_exit, "state": state}

    def kicks(self) -> List[str]:
        return [c[2].rsplit("/", 1)[1] for c in self.calls if c[1] == "kickstart"]

    def __call__(self, argv: List[str], timeout: float) -> Tuple[int, str, str]:
        self.calls.append(argv)
        if argv[1] == "print-disabled":
            if self.fail_disabled:
                return 5, "", "launchctl print-disabled failed"
            body = "".join(f'\t"{label}" => disabled\n' for label in self.disabled)
            return 0, f"disabled services = {{\n{body}}}\n", ""
        label = argv[2].rsplit("/", 1)[1]
        job = self.jobs.get(label)
        if argv[1] == "print":
            if self.raise_on_print:
                raise RuntimeError("launchctl exploded")
            if self.fail_print:
                return 124, "", "timed out"
            if job is None:
                return 113, "", "Could not find service"
            state = job["state"] or ("running" if job["running"] else "not running")
            lines = [f"{argv[2]} = {{", "\tactive count = 0", f"\tstate = {state}",
                     "", f"\truns = {job['runs']}"]
            if job["pended"] and not job["running"]:
                lines.append(f"\tpended nondemand spawn = {job['pended']}")
            lines += [f"\tlast exit code = {job['last_exit']}", "}"]
            return 0, "\n".join(lines) + "\n", ""
        if argv[1] == "kickstart":
            if job is None:
                return 113, "", "Could not find service"
            if label not in getattr(self, "kick_noop", set()):
                job["runs"] += 1  # a demand spawn runs it once and it exits
            return 0, "", ""
        return 64, "", "usage"


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class GuardCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.agents = self.tmp / "LaunchAgents"
        self.agents.mkdir()
        self.state = self.tmp / "state"
        self.events = self.tmp / "lane-events.jsonl"
        self.launchd = FakeLaunchd()
        self.clock = Clock()

    def guard(self, **kw) -> lig.Guard:
        participation = self.tmp / "native-build-participation"
        if not participation.exists():
            participation.write_text("1\n")
        kw.setdefault("participation_file", participation)
        return lig.Guard(self.state, self.agents, domain="gui/501", run=self.launchd,
                         clock=self.clock, event_log=self.events,
                         runner="studio-pulp-gate-01", **kw)

    def interval_agent(self, label: str, interval: int, **job) -> None:
        write_plist(self.agents, label, StartInterval=interval)
        self.launchd.add(label, **job)

    def lane_events(self) -> List[Dict]:
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines()]


class IntervalKickTests(GuardCase):
    def test_a_stuck_agent_is_kicked_after_twice_its_interval_and_not_before(self) -> None:
        self.interval_agent("com.danielraffel.tartci.reap", 300)
        g = self.guard()
        g.pass_once()                      # first sight starts the clock
        self.clock.now += 599
        g.pass_once()
        self.assertEqual(self.launchd.kicks(), [], "kicked before 2x the interval")
        self.clock.now += 1
        receipt = g.pass_once()
        self.assertEqual(self.launchd.kicks(), ["com.danielraffel.tartci.reap"])
        self.assertEqual(receipt["kicked"][0]["seconds_since_progress"], 600)
        kicked = [e for e in self.lane_events() if e["event"] == "interval_agent_kicked"]
        self.assertEqual(len(kicked), 1)
        self.assertEqual(kicked[0]["fields"]["label"], "com.danielraffel.tartci.reap")
        self.assertEqual(kicked[0]["fields"]["interval"], 300)
        self.assertEqual(kicked[0]["fields"]["seconds_since_progress"], 600)
        self.assertEqual(kicked[0]["runner"], "studio-pulp-gate-01")
        # The kick is progress: the next one waits another 2x interval.
        self.clock.now += 300
        g.pass_once()
        self.assertEqual(len(self.launchd.kicks()), 1)
        self.clock.now += 300
        g.pass_once()
        self.assertEqual(len(self.launchd.kicks()), 2)

    def test_progress_survives_a_supervisor_restart(self) -> None:
        self.interval_agent("com.danielraffel.tartci.reap", 300)
        self.guard().pass_once()
        self.clock.now += 600
        self.guard().pass_once()  # a fresh Guard reads the persisted state
        self.assertEqual(self.launchd.kicks(), ["com.danielraffel.tartci.reap"])

    def test_a_running_agent_is_never_kicked(self) -> None:
        self.interval_agent("com.danielraffel.tartci.reclaim", 300, running=True)
        g = self.guard()
        for _ in range(10):
            g.pass_once()
            self.clock.now += 300
        self.assertEqual(self.launchd.kicks(), [])

    def test_a_progressing_agent_is_never_kicked(self) -> None:
        self.interval_agent("com.danielraffel.tartci.reap", 300)
        g = self.guard()
        for _ in range(10):
            g.pass_once()
            self.clock.now += 299
            self.launchd.jobs["com.danielraffel.tartci.reap"]["runs"] += 1
        self.assertEqual(self.launchd.kicks(), [])
        self.assertFalse(g.pass_once()["episode"]["active"])

    def test_an_agent_without_start_interval_is_ignored(self) -> None:
        write_plist(self.agents, "com.danielraffel.tartci.once", RunAtLoad=True)
        self.launchd.add("com.danielraffel.tartci.once")
        write_plist(self.agents, "com.apple.someone.else", StartInterval=60)
        self.launchd.add("com.apple.someone.else")
        g = self.guard()
        for _ in range(5):
            receipt = g.pass_once()
            self.clock.now += 3600
        self.assertEqual(self.launchd.kicks(), [])
        self.assertEqual(receipt["agents_checked"], 0)
        printed = {c[2] for c in self.launchd.calls if c[1] == "print"}
        self.assertEqual(printed, set(), "agents outside the duty were queried")

    def test_a_launchctl_failure_does_not_break_the_pass(self) -> None:
        self.interval_agent("com.danielraffel.tartci.reap", 300)
        g = self.guard()
        g.pass_once()
        self.launchd.raise_on_print = True
        self.clock.now += 900
        receipt = g.pass_once()
        self.assertIn("launchctl exploded", receipt["errors"][0])
        self.launchd.raise_on_print = False
        self.launchd.fail_print = True
        self.assertEqual(g.pass_once()["agents_checked"], 0)
        self.launchd.fail_print = False
        g.pass_once()
        self.assertEqual(self.launchd.kicks(), ["com.danielraffel.tartci.reap"])

    def test_a_failed_kick_is_recorded_and_retried(self) -> None:
        self.interval_agent("com.danielraffel.tartci.reap", 300)
        g = self.guard()
        g.pass_once()
        real = self.launchd.__call__

        def refusing(argv, timeout):
            if argv[1] == "kickstart":
                self.launchd.calls.append(argv)
                return 5, "", "Input/output error"
            return real(argv, timeout)
        g.run = refusing
        self.clock.now += 600
        receipt = g.pass_once()
        self.assertIn("kickstart failed", receipt["errors"][0])
        g.run = self.launchd
        self.clock.now += 60
        g.pass_once()
        self.assertEqual(self.launchd.jobs["com.danielraffel.tartci.reap"]["runs"], 11)


class SelfUpdatePauseTests(GuardCase):
    SELF_UPDATE = "com.danielraffel.tartci.self-update"

    def test_self_update_is_not_kicked_while_a_stall_episode_is_open(self) -> None:
        self.interval_agent(self.SELF_UPDATE, 1800)
        self.interval_agent("com.danielraffel.tartci.reap", 300)
        self.interval_agent("com.danielraffel.tartci.launchd-watchdog", 300)
        g = self.guard()
        g.pass_once()
        for _ in range(16):  # 80 minutes: self-update is overdue, the domain stalled
            self.clock.now += 300
            receipt = g.pass_once()
        self.assertTrue(receipt["episode"]["active"])
        self.assertNotIn(self.SELF_UPDATE, self.launchd.kicks())
        self.assertIn("com.danielraffel.tartci.reap", self.launchd.kicks())
        self.assertEqual([row["label"] for row in receipt["paused"]], [self.SELF_UPDATE])
        # Still counted: a stuck self-update is part of the domain stall.
        self.assertIn(self.SELF_UPDATE, receipt["episode"]["labels"])
        value = lig.status(self.state, now=self.clock.now)
        line = lig.describe(value)
        self.assertIn("self-update paused: launchd timers stalled; a reboot resumes it", line)
        self.assertIn("self-update paused", fd.check_launchd_timers(value).detail)

    def test_an_overdue_self_update_is_kicked_when_no_episode_is_open(self) -> None:
        self.interval_agent(self.SELF_UPDATE, 1800)
        self.interval_agent("com.danielraffel.tartci.reap", 300)
        g = self.guard()
        g.pass_once()
        for _ in range(13):  # reap keeps running on its own; only self-update is stuck
            self.clock.now += 300
            self.launchd.jobs["com.danielraffel.tartci.reap"]["runs"] += 1
            receipt = g.pass_once()
        self.assertFalse(receipt["episode"]["active"])
        self.assertEqual(self.launchd.kicks(), [self.SELF_UPDATE])
        self.assertEqual(receipt["paused"], [])
        self.assertNotIn("paused", lig.describe(lig.status(self.state, now=self.clock.now)))


class KeepAliveKickTests(GuardCase):
    LANE = "com.danielraffel.tartci.tart-runner-macos-fleet.studio.pulp-gate"

    def lane(self, **job) -> str:
        write_plist(self.agents, self.LANE, KeepAlive=True, RunAtLoad=True)
        job.setdefault("last_exit", "75: EX_TEMPFAIL")
        self.launchd.add(self.LANE, **job)
        return self.LANE

    def passes(self, guard: lig.Guard, count: int, step: float) -> None:
        for _ in range(count):
            guard.pass_once()
            self.clock.now += step

    def test_a_lane_that_exited_75_is_kicked_after_two_minutes(self) -> None:
        # m3, 2026-10-04: studio-pulp-gate-01 exited 75 after an unproved
        # delete and launchd never respawned it.
        label = self.lane(pended="inefficient")
        g = self.guard()
        g.pass_once()
        self.clock.now += 119
        g.pass_once()
        self.assertEqual(self.launchd.kicks(), [])
        self.clock.now += 2
        g.pass_once()
        self.assertEqual(self.launchd.kicks(), [label])
        kicked = [e for e in self.lane_events() if e["event"] == "keepalive_agent_kicked"]
        self.assertEqual(len(kicked), 1)
        self.assertIn("last_exit=75", kicked[0]["detail"])
        self.assertEqual(kicked[0]["fields"]["pended"], "inefficient")
        # The kick is a plain kickstart of the lane's label, never -k.
        self.assertIn(["launchctl", "kickstart", f"gui/501/{label}"], self.launchd.calls)
        self.assertFalse([c for c in self.launchd.calls if "-k" in c])

    def test_the_pended_line_is_evidence_not_a_gate(self) -> None:
        self.lane(pended="")
        g = self.guard()
        self.passes(g, 3, 121)
        self.assertEqual(len(self.launchd.kicks()), 1)
        kicked = [e for e in self.lane_events() if e["event"] == "keepalive_agent_kicked"]
        self.assertIn("pended=no", kicked[0]["detail"])

    def test_spawn_scheduled_counts_as_stopped(self) -> None:
        self.lane(state="spawn scheduled")
        self.passes(self.guard(), 3, 121)
        self.assertEqual(len(self.launchd.kicks()), 1)

    def test_a_running_lane_is_never_kicked(self) -> None:
        self.lane(running=True)
        self.passes(self.guard(), 5, 600)
        self.assertEqual(self.launchd.kicks(), [])

    def test_a_clean_exit_is_never_kicked(self) -> None:
        self.lane(last_exit="0", pended="speculative")
        self.passes(self.guard(), 5, 600)
        self.assertEqual(self.launchd.kicks(), [])

    def test_another_exit_code_is_reported_once_and_never_kicked(self) -> None:
        self.lane(last_exit="1", pended="speculative")
        self.passes(self.guard(), 5, 600)
        self.assertEqual(self.launchd.kicks(), [])
        stopped = [e for e in self.lane_events() if e["event"] == "keepalive_agent_stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["fields"]["last_exit"], 1)

    def test_a_disabled_lane_that_exited_75_is_never_kicked(self) -> None:
        self.launchd.disabled = [self.lane()]
        self.passes(self.guard(), 5, 600)
        self.assertEqual(self.launchd.kicks(), [])

    def test_pool_participation_off_kicks_no_lane(self) -> None:
        self.lane()
        (self.tmp / "native-build-participation").write_text("0\n")
        self.passes(self.guard(), 5, 600)
        self.assertEqual(self.launchd.kicks(), [])

    def test_unknown_enablement_kicks_no_lane(self) -> None:
        self.lane()
        self.launchd.fail_disabled = True
        self.passes(self.guard(), 5, 600)
        self.assertEqual(self.launchd.kicks(), [])

    def test_a_failed_print_is_not_read_whatever_it_says(self) -> None:
        # A print that failed (rc 113) but carries text that would otherwise
        # read as an owed exit-75 respawn: nothing is kicked or reported.
        self.lane()
        real = self.launchd.__call__

        def deceptive(argv, timeout):
            rc, out, err = real(argv, timeout)
            return (113, out, "Could not find service") if argv[1] == "print" else (rc, out, err)
        g = lig.Guard(self.state, self.agents, domain="gui/501", run=deceptive,
                      clock=self.clock, event_log=self.events, runner="r",
                      participation_file=self.tmp / "p")
        (self.tmp / "p").write_text("1\n")
        for _ in range(5):
            g.pass_once()
            self.clock.now += 600
        self.assertFalse([c for c in self.launchd.calls if c[1] == "kickstart"])
        self.assertEqual(self.lane_events(), [])

    def test_a_pended_keepalive_agent_that_is_not_a_lane_is_never_kicked(self) -> None:
        write_plist(self.agents, "com.danielraffel.tartci.http-connect-ssh-relay",
                    KeepAlive=True)
        self.launchd.add("com.danielraffel.tartci.http-connect-ssh-relay",
                         pended="speculative", last_exit="75: EX_TEMPFAIL")
        self.passes(self.guard(), 5, 600)
        self.assertEqual(self.launchd.kicks(), [])

    def test_a_lane_whose_kicks_do_not_take_hits_the_ceiling_once(self) -> None:
        label = self.lane()
        self.launchd.kick_noop = {label}
        self.passes(self.guard(), 12, 121)
        self.assertEqual(len(self.launchd.kicks()), lig.KICK_CEILING)
        ceilings = [e for e in self.lane_events() if e["event"] == "keepalive_kick_ceiling"]
        self.assertEqual(len(ceilings), 1)

    def test_a_lane_seen_running_clears_its_ceiling(self) -> None:
        label = self.lane()
        self.launchd.kick_noop = {label}
        g = self.guard()
        self.passes(g, 12, 121)
        self.assertEqual(len(self.launchd.kicks()), lig.KICK_CEILING)
        # Recovery: it runs again, then relapses into an exit-75 stop.
        self.launchd.jobs[label].update(running=True)
        self.passes(g, 1, 121)
        self.launchd.jobs[label].update(running=False)
        self.passes(g, 2, 121)  # first sight of the stop, then past the grace
        self.assertEqual(len(self.launchd.kicks()), lig.KICK_CEILING + 1)


class IntervalCeilingTests(GuardCase):
    REAP = "com.danielraffel.tartci.reap"

    def test_an_interval_agent_whose_kicks_do_not_take_hits_the_ceiling_once(self) -> None:
        self.interval_agent(self.REAP, 300)
        self.launchd.kick_noop = {self.REAP}
        g = self.guard()
        for _ in range(20):
            g.pass_once()
            self.clock.now += 601
        self.assertEqual(len(self.launchd.kicks()), lig.KICK_CEILING)
        ceilings = [e for e in self.lane_events() if e["event"] == "interval_kick_ceiling"]
        self.assertEqual(len(ceilings), 1)

    def test_kicks_that_take_never_reach_the_ceiling(self) -> None:
        # On a starved host every run is a kicked one; a working kick is not a failure.
        self.interval_agent(self.REAP, 300)
        g = self.guard()
        for _ in range(20):
            g.pass_once()
            self.clock.now += 601
        self.assertGreater(len(self.launchd.kicks()), lig.KICK_CEILING)

    def test_a_ceiling_clears_when_the_agent_runs_on_its_own(self) -> None:
        self.interval_agent(self.REAP, 300)
        self.launchd.kick_noop = {self.REAP}
        g = self.guard()
        for _ in range(20):
            g.pass_once()
            self.clock.now += 601
        self.assertEqual(len(self.launchd.kicks()), lig.KICK_CEILING)
        # Recovery: launchd starts it itself.
        self.launchd.jobs[self.REAP]["runs"] += 1
        g.pass_once()
        # Relapse: it stalls again, is kicked again up to the ceiling, and the
        # second ceiling is reported in its turn.
        for _ in range(10):
            self.clock.now += 601
            g.pass_once()
        self.assertEqual(len(self.launchd.kicks()), 2 * lig.KICK_CEILING)
        ceilings = [e for e in self.lane_events() if e["event"] == "interval_kick_ceiling"]
        self.assertEqual(len(ceilings), 2)


class FailureVisibilityTests(GuardCase):
    def test_a_failed_pass_reaches_the_lane_event_log(self) -> None:
        g = self.guard()

        def explode():
            raise OSError("disk full")
        g.pass_once = explode  # type: ignore[assignment]
        lig.loop(g, owner_pid=os.getpid(), sleep=lambda s: None, max_passes=1,
                 log=lambda text: None)
        failed = [e for e in self.lane_events() if e["event"] == "launchd_guard_pass_failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("disk full", failed[0]["detail"])

    def test_a_stale_receipt_is_a_doctor_finding(self) -> None:
        self.state.mkdir()
        (self.state / "status.json").write_text(json.dumps(
            {"ts": 100.0, "agents_checked": 3, "episode": {"active": False}}))
        finding = fd.check_launchd_timers(lig.status(self.state,
                                                     now=100.0 + lig.RECEIPT_STALE_S + 1))
        self.assertEqual(finding.code, "launchd_timers_not_running")
        self.assertNotEqual(finding.state, fd.OK)


class DomainStallTests(GuardCase):
    def test_the_domain_stall_is_reported_once_per_episode(self) -> None:
        self.interval_agent("com.danielraffel.tartci.reap", 300)
        self.interval_agent("com.danielraffel.tartci.launchd-watchdog", 300)
        self.interval_agent("com.danielraffel.tartci.self-update", 1800)
        g = self.guard()
        g.pass_once()
        self.clock.now += 300
        self.assertFalse(g.pass_once()["episode"]["active"])
        for _ in range(12):  # an hour of a stuck domain, with the guard kicking
            self.clock.now += 300
            receipt = g.pass_once()
        self.assertTrue(receipt["episode"]["active"])
        self.assertIn("com.danielraffel.tartci.reap", receipt["episode"]["labels"])
        stalls = [e for e in self.lane_events()
                  if e["event"] == "launchd_interval_spawns_stalled"]
        self.assertEqual(len(stalls), 1, "the episode event repeated")
        self.assertGreaterEqual(len(self.launchd.kicks()), 4)

        value = lig.status(self.state, now=self.clock.now)
        self.assertEqual(value["state"], "stalled")
        line = lig.describe(value)
        self.assertIn("launchd timers: STALLED", line)
        self.assertIn("macOS launchd stopped starting timer jobs", line)
        self.assertIn("a reboot clears it", line)
        finding = fd.check_launchd_timers(value)
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "launchd_timers_stalled"))
        self.assertIn("likely a stalled automatic macOS install", finding.detail)

        # launchd recovers (a reboot): it starts the timers on its own again.
        for _ in range(4):
            self.clock.now += 300
            for job in self.launchd.jobs.values():
                job["runs"] += 1
            receipt = g.pass_once()
        self.assertFalse(receipt["episode"]["active"])
        events = [e["event"] for e in self.lane_events()]
        self.assertEqual(events.count("launchd_interval_spawns_recovered"), 1)
        self.assertEqual(lig.status(self.state, now=self.clock.now)["state"], "ok")

    def test_one_stuck_agent_is_not_a_domain_stall(self) -> None:
        self.interval_agent("com.danielraffel.tartci.reap", 300)
        self.interval_agent("com.danielraffel.tartci.launchd-watchdog", 300)
        g = self.guard()
        for _ in range(12):
            g.pass_once()
            self.clock.now += 300
            self.launchd.jobs["com.danielraffel.tartci.launchd-watchdog"]["runs"] += 1
        self.assertFalse(g.pass_once()["episode"]["active"])


class StatusTests(unittest.TestCase):
    def test_states_and_doctor_codes(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(lig.status(tmp)["state"], "never")
        self.assertEqual(fd.check_launchd_timers(lig.status(tmp)).code, "launchd_timers_never")
        (tmp / "status.json").write_text("{")
        self.assertEqual(fd.check_launchd_timers(lig.status(tmp)).code,
                         "launchd_timers_unreadable")
        (tmp / "status.json").write_text(json.dumps(
            {"ts": 100.0, "agents_checked": 7, "episode": {"active": False}}))
        ok = lig.status(tmp, now=130.0)
        self.assertEqual(ok["state"], "ok")
        self.assertEqual(fd.check_launchd_timers(ok).state, fd.OK)
        stale = lig.status(tmp, now=100.0 + lig.RECEIPT_STALE_S + 1)
        self.assertEqual(fd.check_launchd_timers(stale).code, "launchd_timers_not_running")

    def test_every_code_has_a_reason_row(self) -> None:
        reasons = json.loads((HERE / "fleet_reasons.json").read_text())["reasons"]
        for code in ("launchd_timers_ok", "launchd_timers_stalled",
                     "launchd_timers_not_running", "launchd_timers_never",
                     "launchd_timers_unreadable"):
            self.assertIn(code, fd.CODES)
            self.assertIn(code, reasons)

    def test_pool_status_reports_launchd_timers(self) -> None:
        source = (ROOT / "tartci").read_text()
        self.assertIn('scripts/launchd_interval_guard.py" status --json', source)
        self.assertIn('"launchd_timers":%s', source)
        self.assertIn('scripts/launchd_interval_guard.py" status 2>/dev/null', source)
        out = subprocess.run([sys.executable, str(HERE / "launchd_interval_guard.py"),
                              "status", "--state-dir", str(Path(tempfile.gettempdir())
                                                           / "no-such-guard-dir")],
                             capture_output=True, text=True, check=True)
        self.assertTrue(out.stdout.startswith("launchd timers:"), out.stdout)


class OwnershipTests(GuardCase):
    def test_only_the_lock_owner_acts(self) -> None:
        self.interval_agent("com.danielraffel.tartci.reap", 300)
        owner = lig.acquire_lock(self.state / "owner.lock")
        self.assertIsNotNone(owner)
        self.addCleanup(owner.close)
        # A second supervisor's guard (another process) cannot take the duty.
        probe = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys; sys.path.insert(0, {str(HERE)!r})
                import launchd_interval_guard as lig, pathlib
                print(lig.acquire_lock(pathlib.Path({str(self.state / 'owner.lock')!r})) is None)
            """)], capture_output=True, text=True, check=True)
        self.assertEqual(probe.stdout.strip(), "True")
        # In-process, a non-owner loop never runs a pass.
        stuck = self.guard()
        sleeps: List[float] = []
        orig = lig.acquire_lock
        lig.acquire_lock = lambda path: None
        try:
            lig.loop(stuck, owner_pid=os.getpid(), sleep=sleeps.append, max_passes=3)
        finally:
            lig.acquire_lock = orig
        self.assertEqual(self.launchd.calls, [])
        self.assertEqual(sleeps, [lig.CADENCE_S] * 2)
        owner.close()
        successor = lig.acquire_lock(self.state / "owner.lock")
        self.assertIsNotNone(successor, "the duty did not move once the owner let go")
        successor.close()

    def test_a_failing_pass_does_not_end_the_loop(self) -> None:
        g = self.guard()
        passes = []

        def explode():
            passes.append(1)
            raise OSError("disk full")
        g.pass_once = explode  # type: ignore[assignment]
        logged: List[str] = []
        self.assertEqual(lig.loop(g, owner_pid=os.getpid(), sleep=lambda s: None,
                                  max_passes=3, log=logged.append), 0)
        self.assertEqual(len(passes), 3)
        self.assertIn("disk full", logged[0])

    def test_the_loop_ends_with_its_supervisor(self) -> None:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.assertEqual(lig.loop(self.guard(), owner_pid=dead.pid,
                                  sleep=lambda s: self.fail("slept")), 0)


@unittest.skipUnless(shutil.which("bash"), "bash required")
class SupervisorChildTests(GuardCase):
    """runner.sh's child: starts, kicks through a real launchctl on PATH, stops."""

    def fake_tools(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        calls = self.tmp / "launchctl.calls"
        fake = bindir / "launchctl"
        fake.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            echo "$*" >> {str(calls)!r}
            case "$1" in
              print) printf '%s = {{\\n\\tstate = not running\\n\\truns = 5\\n\\tpended nondemand spawn = interval\\n}}\\n' "$2" ;;
              kickstart) exit 0 ;;
            esac
        """))
        fake.chmod(0o755)
        # The library's python call, pointed at this test's agents dir.
        shim = bindir / "python3"
        shim.write_text(f"#!/bin/bash\nexec {sys.executable!r} \"$@\" "
                        f"--agents-dir {str(self.agents)!r}\n")
        shim.chmod(0o755)
        return bindir, calls

    def test_the_supervisor_child_kicks_and_dies_with_its_supervisor(self) -> None:
        source = RUNNER.read_text()
        self.assertIn('source "$TARTCI_ROOT/providers/tart-macos/interval-guard.lib.sh"', source)
        self.assertIn("  tartci_interval_guard_stop\n", source[source.index("cleanup(){"):])
        loop_top = source[source.index('if [ "$LOOP" = 1 ]; then'):]
        self.assertLess(loop_top.index("tartci_interval_guard_start"),
                        loop_top.index("while true; do"))

        bindir, calls = self.fake_tools()
        write_plist(self.agents, "com.danielraffel.tartci.reap", StartInterval=300)
        self.state.mkdir()
        (self.state / "state.json").write_text(json.dumps({"agents": {
            "com.danielraffel.tartci.reap": {"runs": 5, "progress_ts": 0, "natural_ts": 0,
                                             "pending_kicks": 0}}, "episode": None}))
        script = textwrap.dedent(f"""\
            set -euo pipefail
            export PATH={str(bindir)!r}:$PATH
            export TARTCI_INTERVAL_GUARD_DIR={str(self.state)!r}
            export TARTCI_INTERVAL_GUARD_LOG={str(self.tmp / 'guard.log')!r}
            export TARTCI_INTERVAL_GUARD_CADENCE_SECS=1
            TARTCI_ROOT={str(ROOT)!r}
            SUPERVISOR_PID=$$ EVENT_LOG={str(self.events)!r} RUNNER_NAME=lane-1
            source {str(LIB)!r}
            tartci_interval_guard_start
            echo "$INTERVAL_GUARD_PID"
            for _ in $(seq 1 100); do grep -q kickstart {str(calls)!r} 2>/dev/null && break; sleep 0.1; done
            tartci_interval_guard_stop
            kill -0 "$1" 2>/dev/null && echo alive || echo gone
        """)
        out = subprocess.run(["/bin/bash", "-c", script, "x", "0"], capture_output=True,
                             text=True, timeout=60, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        child = int(out.stdout.split()[0])
        self.assertIn("kickstart gui/", calls.read_text(),
                      (self.tmp / "guard.log").read_text() if (self.tmp / "guard.log").exists() else "")
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_the_child_exits_on_its_own_when_the_supervisor_is_killed(self) -> None:
        self.state.mkdir()
        bindir, _ = self.fake_tools()
        script = textwrap.dedent(f"""\
            export PATH={str(bindir)!r}:$PATH
            export TARTCI_INTERVAL_GUARD_DIR={str(self.state)!r}
            export TARTCI_INTERVAL_GUARD_LOG={str(self.tmp / 'guard.log')!r}
            export TARTCI_INTERVAL_GUARD_CADENCE_SECS=1
            TARTCI_ROOT={str(ROOT)!r}
            SUPERVISOR_PID=$$ EVENT_LOG={str(self.events)!r} RUNNER_NAME=lane-1
            source {str(LIB)!r}
            tartci_interval_guard_start
            echo "$INTERVAL_GUARD_PID"
            sleep 30
        """)
        proc = subprocess.Popen(["/bin/bash", "-c", script], stdout=subprocess.PIPE,
                                text=True, start_new_session=True)
        child = int(proc.stdout.readline())
        proc.stdout.close()
        os.kill(proc.pid, signal.SIGKILL)  # no cleanup trap runs
        proc.wait()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            os.kill(child, signal.SIGKILL)
            self.fail("the guard outlived its supervisor")
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the orphaned `sleep 30`
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    unittest.main()
