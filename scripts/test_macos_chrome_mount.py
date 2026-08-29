#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
CHROME_MOUNT = ROOT / "providers" / "tart-macos" / "chrome-mount.lib.sh"


class MacosChromeMountTests(unittest.TestCase):
    def run_probe(
        self, app: Path, repo: str = "Generous-Corp/forge"
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "TARTCI_RUNNER_REPO": repo,
                "TARTCI_RUNNER_GROUP_ID": "11",
                "TARTCI_RUNNER_CHROME_APP_DIR": str(app),
            }
        )
        return subprocess.run(
            ["/bin/bash", str(RUNNER), "--print-chrome-mount"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_valid_app_emits_one_read_only_tart_mount(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "Google Chrome.app"
            executable = app / "Contents" / "MacOS" / "Google Chrome"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            result = self.run_probe(app)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), f"google-chrome:{app}:ro")

    def test_missing_executable_fails_before_provider_startup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "Google Chrome.app"
            app.mkdir()
            result = self.run_probe(app)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("configured Google Chrome executable is unavailable", result.stderr)
            self.assertNotIn("tart not installed", result.stderr)

    def test_direct_non_forge_configuration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "Google Chrome.app"
            executable = app / "Contents" / "MacOS" / "Google Chrome"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            result = self.run_probe(app, repo="Generous-Corp/pulp")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("restricted to Generous-Corp/forge", result.stderr)

    def test_guest_preflight_precedes_jit_mint(self) -> None:
        source = RUNNER.read_text()
        contract = CHROME_MOUNT.read_text()
        run_one = source[source.index("run_one(){") :]
        self.assertLess(
            run_one.index('install_and_preflight_chrome "$ip"'),
            run_one.index("generate-jitconfig"),
        )
        self.assertIn("/Volumes/My Shared Files/google-chrome", contract)
        self.assertIn("guest Google Chrome target already exists", contract)
        self.assertIn('tart_dirs+=(--dir="$CHROME_MOUNT_ARG")', source)


if __name__ == "__main__":
    unittest.main()
