#!/usr/bin/env python3
"""scripts/install_pf_enable_ref.sh against a launchctl double and a scratch daemons dir."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_pf_enable_ref.sh"
SOURCE = ROOT / "launchd" / "system" / "com.danielraffel.pf-enable-ref.plist"
LABEL = "com.danielraffel.pf-enable-ref"


class InstallPfEnableRef(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.daemons = self.tmp / "LaunchDaemons"
        self.daemons.mkdir()
        self.token = self.tmp / "pf-enable-ref.token"
        self.calls = self.tmp / "calls"
        self.loaded = self.tmp / "loaded"      # holds the plist path launchd "has"
        self.exit_code = self.tmp / "exit"
        self.exit_code.write_text("0")
        double = self.tmp / "launchctl"
        double.write_text(
            "#!/bin/bash\n"
            f"echo \"$*\" >>{str(self.calls)!r}\n"
            "case \"$1\" in\n"
            f"  print) [ -s {str(self.loaded)!r} ] || exit 113\n"
            f"         printf '\\tpath = %s\\n\\tlast exit code = %s\\n' \"$(cat {str(self.loaded)!r})\""
            f" \"$(cat {str(self.exit_code)!r})\" ;;\n"
            f"  bootstrap) printf '%s' \"$3\" >{str(self.loaded)!r}\n"
            f"             [ \"$(cat {str(self.exit_code)!r})\" != 0 ] || echo tok123 >{str(self.token)!r} ;;\n"
            f"  bootout) : >{str(self.loaded)!r} ;;\n"
            "esac\n")
        double.chmod(0o755)
        self.env = {**os.environ, "TARTCI_LAUNCHCTL_BIN": str(double),
                    "TARTCI_PF_LAUNCHDAEMONS_DIR": str(self.daemons),
                    "TARTCI_PF_TOKEN_FILE": str(self.token)}

    def run_it(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=60)

    def verbs(self) -> list[str]:
        return [line.split()[0] for line in self.calls.read_text().splitlines()] \
            if self.calls.exists() else []

    def test_the_shipped_plist_takes_a_reference_at_boot(self):
        value = plistlib.loads(SOURCE.read_bytes())
        self.assertEqual(value["Label"], LABEL)
        self.assertTrue(value["RunAtLoad"])
        self.assertIn("/sbin/pfctl -E", " ".join(value["ProgramArguments"]))
        self.assertNotIn("pfctl -d", " ".join(value["ProgramArguments"]))
        import pf_reference
        target = self.daemons / f"{LABEL}.plist"
        shutil.copy(SOURCE, target)
        self.assertEqual(pf_reference.boot_holders(self.daemons), [LABEL])

    def test_plan_changes_nothing(self):
        out = self.run_it()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("plan: write", out.stdout)
        self.assertIn("(plan only", out.stdout)
        self.assertFalse((self.daemons / f"{LABEL}.plist").exists())
        self.assertEqual(self.verbs(), ["print"])

    def test_install_writes_bootstraps_and_verifies(self):
        out = self.run_it("--install")
        self.assertEqual(out.returncode, 0, out.stderr)
        target = self.daemons / f"{LABEL}.plist"
        self.assertEqual(target.read_bytes(), SOURCE.read_bytes())
        self.assertEqual(oct(target.stat().st_mode & 0o777), "0o644")
        self.assertIn(f"bootstrap system {target}", self.calls.read_text())
        self.assertIn("installed and loaded", out.stdout)

    def test_a_rerun_takes_no_second_reference(self):
        self.run_it("--install")
        before = self.verbs().count("bootstrap")
        out = self.run_it("--install")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("already installed and loaded", out.stdout)
        self.assertEqual(self.verbs().count("bootstrap"), before)

    def test_a_changed_plist_is_booted_out_and_bootstrapped(self):
        self.run_it("--install")
        target = self.daemons / f"{LABEL}.plist"
        target.write_text(target.read_text().replace("pf-enable-ref.token", "old.token"))
        out = self.run_it("--install")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(target.read_bytes(), SOURCE.read_bytes())
        self.assertEqual(self.verbs()[-3:-1], ["bootout", "bootstrap"])

    def test_a_failed_run_is_reported_not_called_installed(self):
        self.exit_code.write_text("1")
        out = self.run_it("--install")
        self.assertEqual(out.returncode, 5)
        self.assertIn("not verified: last exit code '1'", out.stderr)

    def test_install_against_the_real_domain_needs_root(self):
        if os.getuid() == 0:
            self.skipTest("running as root")
        env = {k: v for k, v in self.env.items() if k != "TARTCI_PF_LAUNCHDAEMONS_DIR"}
        out = self.run_it("--install", env=env)
        self.assertEqual(out.returncode, 4)
        self.assertIn("run it with sudo", out.stderr)
        self.assertNotIn("bootstrap", self.calls.read_text() if self.calls.exists() else "")

    def test_it_never_runs_pfctl_itself(self):
        text = SCRIPT.read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")
                         and "echo" not in l)
        self.assertNotIn("pfctl", code)


if __name__ == "__main__":
    unittest.main()
