#!/usr/bin/env python3
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "scripts" / "tart_inventory.py"


def write_fake(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class TartInventoryTests(unittest.TestCase):
    def test_counts_only_running_macos_and_unknown_guests(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "tart"
            write_fake(
                fake,
                '''
case "$1:$2" in
  list:--format) printf '%s\\n' '[{"Name":"mac","State":"running"},{"Name":"linux","State":"running"},{"Name":"stopped","State":"stopped"},{"Name":"unknown","State":"running"}]' ;;
  get:mac) printf '%s\\n' '{"OS":"macOS"}' ;;
  get:linux) printf '%s\\n' '{"OS":"linux"}' ;;
  get:unknown) exit 1 ;;
  *) exit 2 ;;
esac
''',
            )
            result = subprocess.run(
                [str(INVENTORY), "--tart", str(fake), "--timeout-seconds", "2"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "2")

    def test_hung_list_is_killed_within_the_total_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "tart"
            write_fake(fake, "sleep 60\n")
            started = time.monotonic()
            result = subprocess.run(
                [str(INVENTORY), "--tart", str(fake), "--timeout-seconds", "0.2"],
                text=True,
                capture_output=True,
                check=False,
                timeout=2,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 75)
            self.assertLess(elapsed, 1.5)
            self.assertIn("timed out", result.stderr)

    def test_malformed_inventory_member_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "tart"
            write_fake(fake, "printf '%s\\n' '[null]'\n")
            result = subprocess.run(
                [str(INVENTORY), "--tart", str(fake), "--timeout-seconds", "2"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 75)
            self.assertIn("non-object entry", result.stderr)

    def test_non_finite_timeout_is_rejected(self) -> None:
        for value in ("nan", "inf", "-inf"):
            result = subprocess.run(
                [str(INVENTORY), f"--timeout-seconds={value}"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2, value)
            self.assertIn("must be positive", result.stderr)

    def test_hung_get_shares_the_same_total_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "tart"
            write_fake(
                fake,
                '''
if [ "$1" = list ]; then
  printf '%s\\n' '[{"Name":"wedged","State":"running"}]'
else
  sleep 60
fi
''',
            )
            started = time.monotonic()
            result = subprocess.run(
                [str(INVENTORY), "--tart", str(fake), "--timeout-seconds", "0.2"],
                text=True,
                capture_output=True,
                check=False,
                timeout=2,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 75)
            self.assertLess(elapsed, 1.5)


if __name__ == "__main__":
    unittest.main()
