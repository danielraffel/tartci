#!/usr/bin/env python3
"""lane_handoff_alert: one issue per stuck supervisor handoff, closed after."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import lane_handoff_alert as alert

NOW = 2_000_000_000.0
HOURS = 3600


class StuckHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.lane = self.root / "macos-fleet" / "pulp-gate-slot2"
        self.lane.mkdir(parents=True)
        self.sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.sleeper.kill)
        self.opened: list[tuple[str, str]] = []
        self.closed: list[str] = []

    def handoff(self, age_hours: float, waiting: int | None = None,
                incumbent: int | None = None) -> None:
        path = self.lane / "lane-1.handoff"
        path.write_text(f"{waiting or self.sleeper.pid} Mon Sep  1 00:00:00 2026\n")
        stamp = NOW - age_hours * HOURS
        os.utime(path, (stamp, stamp))
        if incumbent is not None:
            (self.lane / "lane-1.supervisor").write_text(f"{incumbent} x\n")

    def run_pass(self, now: float = NOW) -> dict:
        def issue(title: str, body: str) -> tuple[int, str]:
            self.opened.append((title, body))
            return 0, "77"

        def close(number: str) -> tuple[int, str]:
            self.closed.append(number)
            return 0, ""
        return alert.alert_pass(now=now, state_root=self.root, issue=issue, close=close,
                                host="m5studio", target="m5s")

    def test_a_wait_past_the_limit_opens_one_issue_with_the_commands(self) -> None:
        self.handoff(7, incumbent=self.sleeper.pid)
        out = self.run_pass()
        self.assertEqual("77", out["issue"])
        self.assertEqual(1, len(self.opened))
        title, body = self.opened[0]
        self.assertTrue(title.startswith("[tartci] m5studio: lane supervisor handoff stuck"))
        self.assertIn("Run: ssh m5s 'tartci pool status'", body)
        self.assertIn(f"ssh m5s 'kill -TERM {self.sleeper.pid}'", body)
        # Once per episode: a second pass opens nothing new.
        self.run_pass(NOW + 300)
        self.assertEqual(1, len(self.opened))

    def test_the_issue_closes_when_the_handoff_completes(self) -> None:
        self.handoff(7, incumbent=self.sleeper.pid)
        self.run_pass()
        (self.lane / "lane-1.handoff").unlink()
        out = self.run_pass(NOW + 300)
        self.assertTrue(out.get("closed"))
        self.assertEqual(["77"], self.closed)

    def test_a_gone_incumbent_is_not_told_to_be_killed(self) -> None:
        self.handoff(7, incumbent=999_999)
        self.run_pass()
        body = self.opened[0][1]
        self.assertNotIn("kill -TERM", body)
        self.assertIn("tartci launchd reload", body)

    def test_a_young_wait_or_a_dead_waiter_raises_nothing(self) -> None:
        self.handoff(1)
        self.assertEqual([], self.run_pass()["stuck"])
        self.handoff(7, waiting=999_999)
        self.assertEqual([], self.run_pass()["stuck"])
        self.assertEqual([], self.opened)

    def test_a_zombie_waiter_raises_nothing(self) -> None:
        child = os.fork()
        if child == 0:
            os._exit(0)
        self.addCleanup(lambda: os.waitpid(child, 0))
        for _ in range(50):
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(child)],
                                   capture_output=True, text=True).stdout.strip()
            if state.startswith("Z"):
                break
        self.handoff(7, waiting=child)
        self.assertEqual([], self.run_pass()["stuck"])

    def test_the_watchdog_reports_and_runs_the_pass(self) -> None:
        import inspect
        from unittest import mock
        import tartci_launchd_watchdog as wd
        home = self.root / "home"
        lane = home / ".tartci" / "state" / "macos-fleet" / "slot2"
        lane.mkdir(parents=True)
        path = lane / "lane-1.handoff"
        path.write_text(f"{self.sleeper.pid} x\n")
        os.utime(path, (NOW - 7 * HOURS, NOW - 7 * HOURS))
        (lane / "lane-1.supervisor").write_text(f"{self.sleeper.pid} x\n")
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            line = wd.lane_handoff_pass(status_only=True, now=NOW)
        self.assertIn("WARN lane handoff stuck: lane-1 (waiting 7 h", line or "")
        self.assertIn("lane_handoff_pass(status_only=", inspect.getsource(wd.main))


if __name__ == "__main__":
    unittest.main()
