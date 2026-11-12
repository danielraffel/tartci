"""The watchdog re-renders a drifted queue-saturation agent from its template."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tartci_launchd_watchdog as wd  # noqa: E402

LABEL = wd.QUEUE_SATURATION_LABEL


class QueueSaturationReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.agents = Path(self.home) / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)
        self.plist = self.agents / f"{LABEL}.plist"
        self.calls: list[list[str]] = []

    def run_pass(self, loaded: bool = True):
        def fake_run(argv, **kw):
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        return wd.queue_saturation_pass(self.home, fake_run, loaded=lambda label: loaded)

    def write(self, value: dict) -> None:
        self.plist.write_bytes(plistlib.dumps(value))

    def m5_shape(self) -> dict:
        # m5's agent from 2026-07-19: a stale checkout path and no PULP_SAT_GH_CLI.
        return {"Label": LABEL,
                "ProgramArguments": ["/usr/bin/python3",
                                     f"{self.home}/.local/share/tartci/scripts/gh_queue_saturation.py",
                                     "--status"],
                "StartInterval": 300,
                "EnvironmentVariables": {"HOME": self.home, "PULP_SAT_APPLY": "1",
                                         "PULP_SAT_QUEUE_TRIP": "40"}}

    def test_a_drifted_agent_is_re_rendered_keeping_its_tuning(self) -> None:
        self.write(self.m5_shape())
        line = self.run_pass()
        self.assertIn("re-rendered", line)
        value = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(value["ProgramArguments"],
                         ["/bin/bash", f"{self.home}/.local/bin/tartci", "queue-saturation", "--status"])
        env = value["EnvironmentVariables"]
        self.assertEqual(env["PULP_SAT_GH_CLI"], "ghapp")
        self.assertEqual(env["PULP_SAT_APPLY"], "1")
        self.assertEqual(env["PULP_SAT_QUEUE_TRIP"], "40")
        self.assertEqual([c[:2] for c in self.calls],
                         [["launchctl", "bootout"], ["launchctl", "bootstrap"],
                          ["launchctl", "kickstart"]])

    def test_a_current_agent_is_left_alone(self) -> None:
        # Control, same instrument: the installed copy already is the template.
        self.write(wd.desired_queue_saturation_plist(self.home, self.m5_shape()))
        self.assertIsNone(self.run_pass())
        self.assertEqual(self.calls, [])

    def test_an_absent_or_unloaded_agent_is_not_installed(self) -> None:
        self.assertIsNone(self.run_pass())
        self.write(self.m5_shape())
        self.assertIsNone(self.run_pass(loaded=False))
        self.assertEqual(self.calls, [])
        self.assertIn("gh_queue_saturation.py", self.plist.read_text())

    def test_the_heal_pass_runs_it_and_tartci_dispatches_it(self) -> None:
        source = (HERE / "tartci_launchd_watchdog.py").read_text()
        self.assertIn("saturation_line = queue_saturation_pass()", source[source.index("def main("):])
        self.assertIn("queue-saturation) shift; cmd_queue_saturation", (HERE.parent / "tartci").read_text())



class ScheduleBackstopReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        agents = Path(self.home) / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        self.plist = agents / f"{wd.SCHEDULE_BACKSTOP_LABEL}.plist"
        self.calls: list[list[str]] = []

    def run_pass(self, loaded: bool = True):
        def fake_run(argv, **kw):
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        return wd.schedule_backstop_pass(self.home, fake_run, loaded=lambda label: loaded)

    def m3_shape(self) -> dict:
        # m3's live backstop on 2026-10-03: a stale-checkout script, and live.
        return {"Label": wd.SCHEDULE_BACKSTOP_LABEL,
                "ProgramArguments": ["/usr/bin/python3",
                                     f"{self.home}/.local/share/tartci/scripts/schedule_backstop.py"],
                "StartInterval": 300,
                "EnvironmentVariables": {"TARTCI_BACKSTOP_APPLY": "1",
                                         "TARTCI_BACKSTOP_AUTHORITY": "1",
                                         "TARTCI_BACKSTOP_REPO": "Generous-Corp/pulp"}}

    def test_a_stale_backstop_is_re_rendered_and_stays_live(self) -> None:
        self.plist.write_bytes(plistlib.dumps(self.m3_shape()))
        self.assertIn("schedule-backstop agent re-rendered", self.run_pass())
        value = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(value["ProgramArguments"],
                         ["/bin/bash", f"{self.home}/.local/bin/tartci", "schedule-backstop"])
        env = value["EnvironmentVariables"]
        self.assertEqual((env["TARTCI_BACKSTOP_APPLY"], env["TARTCI_BACKSTOP_AUTHORITY"]), ("1", "1"))
        self.assertEqual([c[:2] for c in self.calls],
                         [["launchctl", "bootout"], ["launchctl", "bootstrap"],
                          ["launchctl", "kickstart"]])

    def test_a_current_or_unloaded_backstop_is_left_alone(self) -> None:
        # Controls: already the template, or someone switched it off.
        current = wd.desired_agent_plist(wd.SCHEDULE_BACKSTOP_LABEL, self.home, self.m3_shape(),
                                         "TARTCI_BACKSTOP_")
        self.plist.write_bytes(plistlib.dumps(current))
        self.assertIsNone(self.run_pass())
        self.plist.write_bytes(plistlib.dumps(self.m3_shape()))
        self.assertIsNone(self.run_pass(loaded=False))
        self.assertEqual(self.calls, [])

    def test_the_heal_pass_runs_it(self) -> None:
        source = (HERE / "tartci_launchd_watchdog.py").read_text()
        self.assertIn("backstop_line = schedule_backstop_pass()", source[source.index("def main("):])

if __name__ == "__main__":
    unittest.main()
