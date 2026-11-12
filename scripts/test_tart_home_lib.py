#!/usr/bin/env python3
"""providers/common/tart-home.lib.sh: the Tart store a provider script uses."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers" / "common" / "tart-home.lib.sh"
LINUX = [ROOT / "providers" / "tart-linux" / name for name in ("provision.sh", "run.sh", "runner.sh")]


def toml_python() -> str | None:
    """An interpreter with tomllib, for the helper to run (3.11+)."""
    if sys.version_info >= (3, 11):
        return sys.executable
    for candidate in ("/opt/homebrew/bin/python3.11", "/opt/homebrew/bin/python3.12",
                      "/usr/local/bin/python3.11"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


class TartHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.python = toml_python()
        if self.python is None:
            self.skipTest("no Python 3.11+ for the helper's tomllib read")
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = Path(self.td.name)

    def run_lib(self, *, tart_home: str | None, profile: str | None) -> subprocess.CompletedProcess:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.dir), "TARTCI_PYTHON": self.python,
               "TARTCI_PYTHON_PIN": str(self.dir / "no-pin")}
        if tart_home is not None:
            env["TART_HOME"] = tart_home
        if profile is not None:
            path = self.dir / "profile.toml"
            path.write_text(profile)
            env["TARTCI_FLEET_PROFILE"] = str(path)
        else:
            env["TARTCI_FLEET_PROFILE"] = str(self.dir / "missing.toml")
        return subprocess.run(["/bin/bash", "-c", f'. "{LIB}"; tartci_default_tart_home'],
                              env=env, capture_output=True, text=True, timeout=60, check=False)

    def test_an_explicit_tart_home_wins(self) -> None:
        result = self.run_lib(tart_home="/Volumes/Example/VMs",
                              profile='[host]\ntart_home = "/Volumes/Store/VMs"\n')
        self.assertEqual((0, "/Volumes/Example/VMs\n"), (result.returncode, result.stdout))

    def test_otherwise_the_profile_names_the_store(self) -> None:
        result = self.run_lib(tart_home=None, profile='[host]\ntart_home = "/Volumes/Store/VMs"\n')
        self.assertEqual((0, "/Volumes/Store/VMs\n"), (result.returncode, result.stdout), result.stderr)

    def test_without_either_it_refuses_instead_of_guessing(self) -> None:
        result = self.run_lib(tart_home=None, profile=None)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("set TART_HOME", result.stderr)

    def test_a_profile_without_an_absolute_store_is_refused(self) -> None:
        for body in ('[host]\nid = "x"\n', '[host]\ntart_home = "VMs"\n'):
            with self.subTest(body=body):
                result = self.run_lib(tart_home=None, profile=body)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertIn("no absolute host.tart_home", result.stderr)

    def test_linux_providers_take_their_store_from_the_helper(self) -> None:
        for script in LINUX:
            with self.subTest(script=script.name):
                text = script.read_text()
                self.assertIn('TART_HOME="$(tartci_default_tart_home)" || exit 1', text)
                self.assertNotIn("${TART_HOME:-/", text)


if __name__ == "__main__":
    unittest.main()
