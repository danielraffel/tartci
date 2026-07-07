#!/usr/bin/env python3
"""Tests for the host onboarding helpers (providers/common/onboard.lib.sh)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers" / "common" / "onboard.lib.sh"


def _bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


class OnboardRoleTests(unittest.TestCase):
    def test_auto_derives_and_persists_role_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            role_file = Path(td) / "role"
            proc = _bash(
                textwrap.dedent(
                    f"""
                    set -euo pipefail
                    source {LIB}
                    tartci_onboard_role {ROOT}
                    cat "$TARTCI_ROLE_FILE"
                    """
                ),
                env={
                    "TARTCI_ROLE_FILE": str(role_file),
                    "TARTCI_HOST_CORES": "8",
                    "TARTCI_HOST_MEM_MB": "16384",
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(role_file.read_text().strip(), "light")  # 8-core → light

    def test_existing_role_file_wins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            role_file = Path(td) / "role"
            role_file.write_text("dedicated-builder\n")
            proc = _bash(
                f"set -euo pipefail; source {LIB}; tartci_onboard_role {ROOT}",
                env={"TARTCI_ROLE_FILE": str(role_file)},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("dedicated-builder", proc.stdout)
            # operator intent preserved — not overwritten
            self.assertEqual(role_file.read_text().strip(), "dedicated-builder")


class OnboardVerifyTests(unittest.TestCase):
    def test_verify_passes_on_a_governed_host(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = _bash(
                f"set -euo pipefail; source {LIB}; tartci_onboard_verify {ROOT}",
                env={
                    "TARTCI_HOST_CORES": "8",
                    "TARTCI_HOST_MEM_MB": "16384",
                    "TARTCI_LEASE_DIR": td,
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("host onboarded", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
