import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CpuPressureEntryPointTests(unittest.TestCase):
    def run_guard(self, profile: str):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ps = root / "ps"
            ps.write_text("#!/bin/sh\ncat <<'EOF'\n123 1 00:10 00:00 90.0 /Users/test/agent-worktrees/runaway/python\n124 1 00:10 00:00 99.0 /usr/bin/tart vm run\n125 1 00:10 00:00 90.0 /tmp/build/python\nEOF\n")
            ps.chmod(ps.stat().st_mode | stat.S_IXUSR)
            config = root / "profile.toml"
            config.write_text(profile)
            env = {**os.environ, "TARTCI_CPU_GUARD_PS": str(ps)}
            args = [str(ROOT / "tartci"), "doctor", "cpu-pressure", "--profile",
                    str(config), "--json"]
            return subprocess.run(args, env=env, text=True, capture_output=True)

    def test_alert_attributes_process_tree_and_worktree(self):
        result = self.run_guard("[cpu_pressure]\npressure_fraction = 0.20\ncores = 4\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual([row["pid"] for row in value["offenders"]], [123, 125])
        row = value["offenders"][0]
        self.assertEqual(row["action"], "alert")
        self.assertIn("agent-worktrees/runaway", row["worktree"])
        self.assertIn("parent_session", row)
        self.assertIn("process_tree", row)

    def test_allowlist_is_explicit_and_observation_only(self):
        result = self.run_guard("[cpu_pressure]\npressure_fraction = 0.20\ncores = 4\n")
        value = json.loads(result.stdout)
        self.assertTrue(all(row["action"] == "alert" for row in value["offenders"]))
        self.assertNotIn(124, [row["pid"] for row in value["offenders"]])
        self.assertIn(125, [row["pid"] for row in value["offenders"]])


if __name__ == "__main__":
    unittest.main()
