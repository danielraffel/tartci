#!/usr/bin/env python3
"""Regression checks for per-boot macOS Actions Runner version enforcement."""

from pathlib import Path
import os
import subprocess
import unittest
from typing import Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"


class MacosRunnerVersionTest(unittest.TestCase):
    def run_print(self, extra_env: Optional[Dict[str, str]] = None) -> str:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.check_output(
            ["bash", str(RUNNER), "--print-runner-version"],
            cwd=ROOT,
            env=env,
            text=True,
        ).strip()

    def test_current_default_is_reported(self) -> None:
        self.assertEqual(self.run_print(), "2.336.0")

    def test_operator_override_is_reported(self) -> None:
        self.assertEqual(
            self.run_print({"TARTCI_RUNNER_VERSION": "9.8.7"}), "9.8.7"
        )

    def test_invalid_override_fails_closed(self) -> None:
        result = subprocess.run(
            ["bash", str(RUNNER), "--print-runner-version"],
            cwd=ROOT,
            env={**os.environ, "TARTCI_RUNNER_VERSION": "latest; false"},
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid Actions Runner version", result.stderr)

    def test_version_is_enforced_before_jit_registration(self) -> None:
        source = RUNNER.read_text()
        ensure = source.index('if ! ensure_runner_version "$ip"; then')
        mint = source.index('heartbeat minting-jit', ensure)
        self.assertLess(ensure, mint)
        self.assertIn("TARTCI_DIAG actions-runner-version=%s", source)
        self.assertIn("shasum -a 256", source)
        self.assertIn("--retry-all-errors", source)


if __name__ == "__main__":
    unittest.main()
