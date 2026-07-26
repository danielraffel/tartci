#!/usr/bin/env python3
"""Hermetic tests for retiring the old Orchard LaunchAgents."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


DISABLE_ORCHARD = Path(__file__).with_name("disable_orchard.sh")


class NoOrchardCleanupTests(unittest.TestCase):
    def test_cleanup_is_idempotent_and_verifies_both_labels_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            agents = home / "Library/LaunchAgents"
            agents.mkdir(parents=True)
            labels = (
                "com.danielraffel.tartci.orchard-controller",
                "com.danielraffel.tartci.orchard-worker",
            )
            for label in labels:
                (agents / f"{label}.plist").write_text(
                    "retired", encoding="utf-8"
                )
            calls = home / "calls"
            fake_bin = home / "bin"
            fake_bin.mkdir()
            (fake_bin / "launchctl").write_text(
                """#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
case "$1" in
  bootout) exit 0 ;;
  print) exit 1 ;;
esac
exit 2
""",
                encoding="utf-8",
            )
            (fake_bin / "launchctl").chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "CALLS": str(calls),
                }
            )
            for _ in range(2):
                result = subprocess.run(
                    ["/bin/bash", str(DISABLE_ORCHARD), "--apply"],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("LaunchAgents are absent", result.stdout)
            call_text = calls.read_text(encoding="utf-8")
            for label in labels:
                self.assertFalse((agents / f"{label}.plist").exists())
                self.assertIn(
                    f"bootout gui/{os.getuid()}/{label}", call_text
                )
                self.assertIn(f"print gui/{os.getuid()}/{label}", call_text)

    def test_cleanup_fails_when_a_retired_label_remains_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin = home / "bin"
            fake_bin.mkdir()
            (fake_bin / "launchctl").write_text(
                '#!/bin/sh\n[ "$1" = print ] && exit 0\nexit 0\n',
                encoding="utf-8",
            )
            (fake_bin / "launchctl").chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                }
            )
            result = subprocess.run(
                ["/bin/bash", str(DISABLE_ORCHARD), "--apply"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("still loaded", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
