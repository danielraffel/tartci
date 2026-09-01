#!/usr/bin/env python3
"""Behavioral tests for bounded mutation-command ownership."""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "scripts" / "bounded_command.py"


class BoundedCommandTests(unittest.TestCase):
    def test_hanging_command_is_killed_inside_declared_bound(self) -> None:
        started = time.monotonic()
        result = subprocess.run(
            [
                sys.executable,
                str(COMMAND),
                "--timeout", "0.1",
                "--operation", "teardown-test",
                "--",
                sys.executable, "-c", "import time; time.sleep(60)",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )
        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn("timed out after 0.1s", result.stderr)

    def test_exact_exit_and_streams_are_preserved(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(COMMAND),
                "--timeout", "1",
                "--",
                sys.executable, "-c",
                "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "out\n")
        self.assertEqual(result.stderr, "err\n")


if __name__ == "__main__":
    unittest.main()
