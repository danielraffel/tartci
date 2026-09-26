#!/usr/bin/env python3
"""The disk reclaimer must be installed by setup, not left to a manual step.

Three hosts ran without it because installing it was documented rather than
wired: m5 reached 14 GiB free with 488 build dirs and refused 276 leases. These
tests pin the two properties that keep that from recurring — `setup` reaches the
installer, and the installer is safe to call on every setup.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_reclaim_agent.sh"
LABEL = "com.danielraffel.tartci.reclaim"


def run(argv, env=None):
    return subprocess.run(argv, cwd=ROOT, text=True, capture_output=True,
                          env={**os.environ, **(env or {})}, check=False)


class SetupWiresTheReclaimer(unittest.TestCase):
    def test_setup_invokes_the_installer(self):
        # The gap was never "no installer" — it was that nothing called one. A
        # grep is the whole assertion: if setup stops calling it, a new machine
        # silently ships without the janitor again.
        body = (ROOT / "tartci").read_text()
        start = body.index("cmd_setup()")
        end = body.index("cmd_bench()", start)
        self.assertIn("install_reclaim_agent.sh", body[start:end],
                      "cmd_setup must install the disk reclaimer")

    def test_doctor_reports_both_janitors(self):
        body = (ROOT / "tartci").read_text()
        start = body.index("cmd_doctor()")
        end = body.index("cmd_setup()", start)
        section = body[start:end]
        self.assertIn("janitors:", section)
        self.assertIn("com.danielraffel.tartci.${_j%%:*}", section)


class InstallerIsSafeToRepeat(unittest.TestCase):
    def test_plan_mode_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "agents"
            res = run([str(INSTALLER), "--plan"], {"TARTCI_AGENTS_DIR": str(agents)})
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertFalse((agents / f"{LABEL}.plist").exists(),
                             "--plan must not write the agent")
            self.assertIn("plan:", res.stdout)

    def test_renders_a_valid_plist_naming_the_reclaimer(self):
        # Render through the same path the installer uses, so a template that
        # stops invoking `tartci reclaim` fails here rather than on a host.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "rendered.plist"
            res = run(["python3", "scripts/render_launchd_template.py",
                       f"launchd/{LABEL}.plist.template", "--set", f"HOME={td}"])
            self.assertEqual(res.returncode, 0, res.stderr)
            out.write_text(res.stdout)
            spec = plistlib.loads(out.read_bytes())
            self.assertEqual(spec["Label"], LABEL)
            self.assertIn("reclaim", spec["ProgramArguments"])
            self.assertIn("--fix", spec["ProgramArguments"])
            # Space-triggered, not calendar-triggered: the whole point is to act
            # when the volume is filling, and an hourly pass is what makes the
            # pressure thresholds meaningful.
            self.assertGreater(int(spec["StartInterval"]), 0)
            env = spec.get("EnvironmentVariables") or {}
            self.assertIn("TARTCI_RECLAIM_PRESSURE_FREE_GB", env)
            self.assertIn("TARTCI_RECLAIM_FAIL_BELOW_GB", env)

    def test_rejects_an_unknown_argument(self):
        res = run([str(INSTALLER), "--wat"])
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
