#!/usr/bin/env python3
"""tcc_stuck_recovery: jobs a TCC wedge left blocked on /Volumes are restarted
once per episode, and a survivor is paged as needing a reboot.

Fakes stand in for launchctl, ps, lsof and kill. Each positive case has a
control on the same inputs: a fresh job, a job with nothing on /Volumes, a
healthy store, another user's process, a runner lane, and the switch off are
all left alone.
"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tartci_launchd_watchdog as wd  # noqa: E402
import tcc_stuck_recovery as rec  # noqa: E402

NOW = 1_800_000_000.0
UID = 501
LSTART = "Mon Nov 16 22:18:01 2026"
WEDGED = {"state": "failing", "drain_required": True, "failing_since": NOW - 3600,
          "last_result": {"ok": False, "reason": "timeout after 10s"},
          "classification": {"kind": "tcc_prompt_pending", "classified_at": "2027-01-15T08:00:00Z"}}
HEALTHY = {"state": "healthy", "drain_required": False, "last_result": {"ok": True}}


def ps_line(pid, ppid, etime, command, uid=UID, lstart=LSTART):
    return f"{pid:>6} {ppid:>6} {uid:>4} {etime:>12} {lstart} {command}"


class FakeHost:
    """launchctl list, ps snapshots (one per call, the last repeats), lsof."""

    def __init__(self, jobs, snapshots, cwd=None, kickstart_rc=0):
        self.jobs = jobs                      # label -> pid
        self.snapshots = list(snapshots)
        self.cwd = dict(cwd or {})
        self.kickstart_rc = kickstart_rc
        self.calls: list[list[str]] = []

    def run(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["/bin/launchctl", "list"]:
            rows = ["PID\tStatus\tLabel"] + [f"{pid}\t0\t{label}" for label, pid in self.jobs.items()]
            return subprocess.CompletedProcess(argv, 0, "\n".join(rows) + "\n", "")
        if argv[0] == "/bin/ps":
            text = self.snapshots[0] if len(self.snapshots) == 1 else self.snapshots.pop(0)
            if text is None:
                return subprocess.CompletedProcess(argv, 1, "", "ps failed")
            return subprocess.CompletedProcess(argv, 0, text, "")
        if argv[0] == "/usr/sbin/lsof":
            pids = [int(p) for p in argv[argv.index("-p") + 1].split(",")]
            out = []
            for pid in pids:
                if pid in self.cwd:
                    out += [f"p{pid}", "fcwd", f"n{self.cwd[pid]}"]
            return subprocess.CompletedProcess(argv, 0, "\n".join(out) + "\n", "")
        if argv[:3] == ["/bin/launchctl", "kickstart", "-k"]:
            return subprocess.CompletedProcess(argv, self.kickstart_rc, "", "")
        raise AssertionError(f"unexpected command {argv}")

    def kickstarts(self):
        return [c[3] for c in self.calls if c[:3] == ["/bin/launchctl", "kickstart", "-k"]]


class RecoveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.agents = self.home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)
        self.state = self.home / "state"
        self.kills: list[tuple[int, int]] = []
        self.sleeps: list[float] = []
        self.issues: list[tuple[str, str]] = []
        self.kill_error: Exception | None = None

    def agent(self, label, interval=300, **extra):
        value = {"Label": label, "ProgramArguments": ["/bin/bash", "/Users/example/job.sh"], **extra}
        if interval:
            value["StartInterval"] = interval
        (self.agents / f"{label}.plist").write_bytes(plistlib.dumps(value))

    def kill(self, pid, sig):
        self.kills.append((pid, sig))
        if self.kill_error is not None:
            raise self.kill_error

    def run_pass(self, host, store=WEDGED, enabled=True, dry_run=False, now=NOW):
        def issue(title, body):
            self.issues.append((title, body))
            return 0, "77"
        return rec.recovery_pass(store, now=now, dry_run=dry_run, enabled=enabled,
                                 home=str(self.home), state_dir=self.state, uid=UID,
                                 run=host.run, kill=self.kill, sleep=self.sleeps.append,
                                 host="m3", issue=issue, close=lambda n: (0, ""))

    def events(self):
        path = self.state / "events.jsonl"
        return [json.loads(x)["event"] for x in path.read_text().splitlines()] if path.exists() else []


# The measured m3 shape: the job is a /bin/bash pass runner on the internal
# volume; the child that hangs runs a script on /Volumes in its own group.
STUCK_TREE = "\n".join([
    ps_line(22434, 1, "02:05:00", "/bin/bash /Users/example/fleet-pass.sh repair"),
    ps_line(50629, 22434, "02:04:00", "/opt/homebrew/bin/python3 /Volumes/Example/x/must-review.py"),
])
GONE = ps_line(30000, 1, "00:05", "/bin/bash /Users/example/fleet-pass.sh repair",
               lstart="Mon Nov 16 23:59:01 2026")


class RecoveryTests(RecoveryTestCase):
    def test_a_stuck_volumes_job_is_kickstarted_once_per_episode(self) -> None:
        self.agent("com.danielraffel.fleet-pass.repair", interval=120)
        host = FakeHost({"com.danielraffel.fleet-pass.repair": 22434}, [STUCK_TREE, GONE])
        lines = self.run_pass(host)
        self.assertEqual(host.kickstarts(), [f"gui/{UID}/com.danielraffel.fleet-pass.repair"])
        self.assertTrue(any("kickstarted com.danielraffel.fleet-pass.repair" in x for x in lines), lines)
        self.assertTrue(any("exited after the kickstart" in x for x in lines), lines)
        self.assertEqual(self.sleeps, [rec.SETTLE_S])
        self.assertEqual(self.kills, [])
        self.assertEqual(self.issues, [])
        self.assertIn("tcc_stuck_kickstart", self.events())
        # Same episode, same stuck tree again: no second kickstart.
        host = FakeHost({"com.danielraffel.fleet-pass.repair": 22434}, [STUCK_TREE])
        self.run_pass(host, now=NOW + 300)
        self.assertEqual(host.kickstarts(), [])

    def test_a_volumes_cwd_found_by_lsof_counts(self) -> None:
        self.agent("com.pulp.fleet.prewall")
        tree = ps_line(81331, 1, "1-01:00:00", "/usr/local/bin/python3 prewall_warning.py")
        host = FakeHost({"com.pulp.fleet.prewall": 81331}, [tree, ""],
                        cwd={81331: "/Volumes/Example/Code/tools"})
        self.run_pass(host)
        self.assertEqual(host.kickstarts(), [f"gui/{UID}/com.pulp.fleet.prewall"])
        lsof = [c for c in host.calls if c[0] == "/usr/sbin/lsof"][0]
        self.assertIn("-b", lsof)  # never stat the path

    def test_a_fresh_job_or_one_off_the_store_is_left_alone(self) -> None:
        self.agent("com.example.fresh", interval=600)
        self.agent("com.example.internal", interval=60)
        tree = "\n".join([
            # 25 min: under max(3 x 600 s, 30 min).
            ps_line(100, 1, "25:00", "/usr/bin/python3 /Volumes/Example/a.py"),
            ps_line(200, 1, "05:00:00", "/usr/bin/python3 /Users/example/b.py"),
        ])
        host = FakeHost({"com.example.fresh": 100, "com.example.internal": 200}, [tree])
        self.assertEqual(self.run_pass(host), [])
        self.assertEqual(host.kickstarts(), [])
        self.assertEqual(self.sleeps, [])

    def test_runners_services_the_watchdog_and_other_users_are_never_touched(self) -> None:
        self.agent("com.danielraffel.tartci.tart-runner-macos-fleet.studio.pulp-gate")
        self.agent("com.example.service", interval=None, KeepAlive=True)
        self.agent(rec.SELF_LABEL)
        self.agent("com.example.root-owned")
        tree = "\n".join([
            ps_line(1, 0, "9-00:00:00", "/sbin/launchd", uid=0),
            ps_line(10, 1, "05:00:00", "/bin/bash /Volumes/Example/serve"),
            ps_line(11, 1, "05:00:00", "/usr/bin/python3 /Volumes/Example/svc.py"),
            ps_line(12, 1, "05:00:00", "/bin/bash /Volumes/Example/wd"),
            ps_line(13, 1, "05:00:00", "/bin/bash /Volumes/Example/r", uid=0),
        ])
        host = FakeHost({"com.danielraffel.tartci.tart-runner-macos-fleet.studio.pulp-gate": 10,
                         "com.example.service": 11, rec.SELF_LABEL: 12,
                         "com.example.root-owned": 13}, [tree])
        self.run_pass(host)
        self.assertEqual(host.kickstarts(), [])

    def test_a_healthy_store_does_nothing(self) -> None:
        self.agent("com.danielraffel.fleet-pass.repair", interval=120)
        host = FakeHost({"com.danielraffel.fleet-pass.repair": 22434}, [STUCK_TREE])
        self.assertEqual(self.run_pass(host, store=HEALTHY), [])
        self.assertEqual(host.calls, [])

    def test_a_survivor_is_killed_and_a_failed_kill_escalates_to_reboot(self) -> None:
        self.agent("com.danielraffel.fleet-pass.repair", interval=120)
        self.kill_error = PermissionError(1, "Operation not permitted")
        host = FakeHost({"com.danielraffel.fleet-pass.repair": 22434}, [STUCK_TREE])
        lines = self.run_pass(host)
        self.assertEqual(self.kills, [(22434, signal.SIGKILL)])
        self.assertTrue(any("WARN tcc-wedge needs reboot" in x for x in lines), lines)
        self.assertEqual(len(self.issues), 1)
        self.assertIn('"class":"tcc_wedge_needs_reboot"', self.issues[0][1])
        self.assertIn(rec.CLASS, self.events())

    def test_a_killed_job_whose_child_stays_stuck_still_pages(self) -> None:
        self.agent("com.danielraffel.fleet-pass.repair", interval=120)
        child_only = ps_line(50629, 1, "02:04:10", "/opt/homebrew/bin/python3 /Volumes/Example/x/must-review.py")
        host = FakeHost({"com.danielraffel.fleet-pass.repair": 22434},
                        [STUCK_TREE, STUCK_TREE, child_only])
        lines = self.run_pass(host)
        self.assertEqual(self.kills, [(22434, signal.SIGKILL)])
        self.assertEqual(self.sleeps, [rec.SETTLE_S, rec.KILL_SETTLE_S])
        self.assertTrue(any("needs reboot" in x and "50629" in x for x in lines), lines)

    def test_a_reused_pid_is_not_killed(self) -> None:
        self.agent("com.danielraffel.fleet-pass.repair", interval=120)
        reused = ps_line(22434, 1, "00:03", "/usr/bin/some-other", lstart="Tue Nov 17 00:00:01 2026")
        host = FakeHost({"com.danielraffel.fleet-pass.repair": 22434}, [STUCK_TREE, reused])
        self.run_pass(host)
        self.assertEqual(self.kills, [])

    def test_an_unreadable_snapshot_after_the_kickstart_signals_nothing(self) -> None:
        self.agent("com.danielraffel.fleet-pass.repair", interval=120)
        host = FakeHost({"com.danielraffel.fleet-pass.repair": 22434}, [STUCK_TREE, None])
        lines = self.run_pass(host)
        self.assertEqual(self.kills, [])
        self.assertTrue(any("needs reboot" in x for x in lines), lines)

    def test_the_switch_turns_it_off(self) -> None:
        self.agent("com.danielraffel.fleet-pass.repair", interval=120)
        host = FakeHost({"com.danielraffel.fleet-pass.repair": 22434}, [STUCK_TREE])
        lines = self.run_pass(host, enabled=False)
        self.assertEqual(host.kickstarts(), [])
        self.assertEqual(self.kills, [])
        self.assertTrue(any(f"{rec.ENV_SWITCH}=0" in x for x in lines), lines)
        # The default reads the environment.
        host = FakeHost({"com.danielraffel.fleet-pass.repair": 22434}, [STUCK_TREE])
        with mock.patch.dict(os.environ, {rec.ENV_SWITCH: "0"}):
            rec.recovery_pass(WEDGED, now=NOW, home=str(self.home), state_dir=self.state,
                              uid=UID, run=host.run, kill=self.kill, sleep=self.sleeps.append,
                              host="m3", issue=lambda t, b: (0, "1"), close=lambda n: (0, ""))
        self.assertEqual(host.kickstarts(), [])

    def test_a_dry_run_changes_nothing(self) -> None:
        self.agent("com.danielraffel.fleet-pass.repair", interval=120)
        host = FakeHost({"com.danielraffel.fleet-pass.repair": 22434}, [STUCK_TREE])
        lines = self.run_pass(host, dry_run=True)
        self.assertEqual(host.kickstarts(), [])
        self.assertTrue(any("would kickstart" in x for x in lines))
        self.assertFalse((self.state / "tcc-stuck-recovery.json").exists())


class ParseTests(unittest.TestCase):
    def test_etime_forms(self) -> None:
        self.assertEqual(rec.parse_etime("05"), None)
        self.assertEqual(rec.parse_etime("01:05"), 65)
        self.assertEqual(rec.parse_etime("02:00:00"), 7200)
        self.assertEqual(rec.parse_etime("1-00:00:01"), 86401)

    def test_wedge_reading(self) -> None:
        self.assertTrue(rec.wedge(WEDGED)[0])
        self.assertFalse(rec.wedge(HEALTHY)[0])
        hung = {"state": "failing", "drain_required": True,
                "last_result": {"reason": "timeout after 10s"}, "failing_since": 5}
        self.assertEqual(rec.wedge(hung), (True, "5"))
        # One timeout below the threshold is not yet a wedge.
        self.assertFalse(rec.wedge(dict(hung, drain_required=False))[0])


class WatchdogWiringTests(unittest.TestCase):
    def test_main_runs_it_after_store_health_and_it_never_raises(self) -> None:
        source = Path(wd.__file__).read_text()
        body = source[source.index("def main("):]
        self.assertIn("tcc_stuck_recovery_pass(store_health_result", body)
        self.assertLess(body.index("store_health_pass("), body.index("tcc_stuck_recovery_pass("))
        line = wd.tcc_stuck_recovery_pass(WEDGED, dry_run=True, run=lambda argv: 1 / 0,
                                          home=tempfile.gettempdir(), uid=UID)
        self.assertIn("could not read", line or "")


if __name__ == "__main__":
    unittest.main()
