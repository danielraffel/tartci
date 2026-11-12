"""A stuck run of an uninterruptible interval agent is flagged, not healed."""

from __future__ import annotations

import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tartci_launchd_watchdog as wd  # noqa: E402

RECLAIM = "com.danielraffel.tartci.reclaim"
NOW = 1_800_000_000.0


class StuckIntervalRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "reclaim.log"
        self.log.write_text("disk_reclaim: pass starting: 2 root(s)\n")
        for name, value in (("_run", lambda cmd: (0, "state = running\nlast exit code = 0\n", "")),
                            ("utcnow", lambda: NOW)):
            original = getattr(wd, name)
            setattr(wd, name, value)
            self.addCleanup(setattr, wd, name, original)

    def agent(self, label: str, log_age: float) -> str:
        path = self.root / f"{label}.plist"
        path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/bin/bash", "x"],
                                         "StartInterval": 3600,
                                         "StandardOutPath": str(self.log)}))
        os.utime(self.log, (NOW - log_age, NOW - log_age))
        return str(path)

    def test_a_reclaim_run_past_twice_its_interval_is_attention(self) -> None:
        # m1 on 2026-10-02: one reclaim run sat 14 h in open() while the
        # watchdog logged heal-failed, then rate-limited.
        health = wd.gather_health(RECLAIM, self.agent(RECLAIM, 50_000), 4500, vm_running=False)
        self.assertEqual(health.verdict, "attention", health.reason)
        self.assertIn("stop the run by hand", health.reason)

    def test_a_reclaim_run_inside_its_interval_is_healthy(self) -> None:
        # Control, same instrument: only the log age changed.
        health = wd.gather_health(RECLAIM, self.agent(RECLAIM, 1800), 4500, vm_running=False)
        self.assertEqual(health.verdict, "healthy", health.reason)

    def test_an_interruptible_interval_agent_still_heals(self) -> None:
        label = "com.danielraffel.tartci.reap"
        health = wd.gather_health(label, self.agent(label, 50_000), 4500, vm_running=False)
        self.assertEqual(health.verdict, "wedged", health.reason)


if __name__ == "__main__":
    unittest.main()
