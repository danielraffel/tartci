#!/usr/bin/env python3
"""Behavioral tests for the Shipyard github-auth check in `tartci doctor`.

A host onboarded via tartci must wire Shipyard's GitHub auth to the GitHub-App
installation token (the `[github.auth]` command helper in Shipyard's
config.toml). A host that never imported that config silently degrades to the
ambient `gh` token (anonymous 60/hr) and Shipyard's menu bar shows "updates
paused" with no error — the m1 outage on 2026-07-06.

`tartci doctor` closes that gap by running `shipyard auth doctor` and WARNing
when the effective source is `gh-cli (ambient)` instead of
`github-app-installation`. The decisive properties, tested by behavior with a
stub `shipyard` on PATH (no real Shipyard/network needed):

  1. correct host   (github-app-installation) -> reports OK, no warning.
  2. degraded host  (gh-cli ambient)          -> WARNs + names the import fix.
  3. no shipyard installed                     -> skipped, non-fatal, exit 0.

In every case `tartci doctor` must exit 0 — the check never fails a host.

Run:  python3 scripts/test_shipyard_auth_doctor.py
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARTCI = ROOT / "tartci"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class ShipyardAuthDoctorCheck(unittest.TestCase):
    """Drive `tartci doctor` with a stub `shipyard` and assert the auth line."""

    def _run_doctor(self, tmp: Path, *, install_shipyard: bool,
                    doctor_output: str = "") -> subprocess.CompletedProcess:
        # Minimal PATH: our stub dir + the real dirs holding bash/coreutils.
        # Deliberately EXCLUDES any host-installed `shipyard`, so "not installed"
        # is unambiguous when we don't drop a stub.
        base = [d for d in ("/bin", "/usr/bin", "/opt/homebrew/bin", "/usr/local/bin")
                if Path(d).exists()]
        if install_shipyard:
            _write_exec(tmp / "shipyard",
                        "#!/usr/bin/env bash\n"
                        # Only the `auth doctor` subcommand is exercised.
                        'if [ "$1 $2" = "auth doctor" ]; then\n'
                        f'  cat <<\'OUT\'\n{doctor_output}\nOUT\n'
                        "fi\n")
        env = {
            "HOME": str(tmp),  # doctor runs under `set -u`; needs HOME defined
            "PATH": os.pathsep.join([str(tmp), *base]),
            # Point stores at the throwaway dir so doctor doesn't touch ~/.tartci.
            "TARTCI_HOME": str(tmp / "tartci-home"),
        }
        return subprocess.run(
            ["bash", str(TARTCI), "doctor"],
            capture_output=True, text=True, check=False, env=env,
        )

    def test_correct_host_reports_ok_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            r = self._run_doctor(
                tmp, install_shipyard=True,
                doctor_output="github-auth: ok command helper (github-app-installation)")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("github-app-installation", r.stdout)
            self.assertNotIn("DEGRADED", r.stdout)

    def test_degraded_host_warns_and_names_the_fix(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            r = self._run_doctor(
                tmp, install_shipyard=True,
                doctor_output="github-auth: degraded using gh-cli (ambient)")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("DEGRADED", r.stdout)
            # The actionable remedy — the supported import command — is surfaced.
            self.assertIn("shipyard auth import", r.stdout)

    def test_no_shipyard_installed_is_skipped_and_green(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            r = self._run_doctor(tmp, install_shipyard=False)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("not installed", r.stdout)
            self.assertNotIn("DEGRADED", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
