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


class TartVmAbsentTests(unittest.TestCase):
    """--vm-absent is the deletion proof for a pending-delete teardown."""

    def _run(self, body: str, name: str = "gone-vm") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "tart"
            write_fake(fake, body)
            return subprocess.run(
                [str(INVENTORY), "--tart", str(fake), "--timeout-seconds", "0.5",
                 "--vm-absent", name],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )

    def test_readable_listing_without_the_vm_proves_absence(self) -> None:
        result = self._run(
            """
[ "$1 $2 $3 $4 $5" = "list --format json --source local" ] || exit 2
printf '%s\\n' '[{"Name":"other","State":"stopped"}]'
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "absent")

    def test_listed_vm_is_present_even_when_stopped(self) -> None:
        result = self._run(
            "printf '%s\\n' '[{\"Name\":\"gone-vm\",\"State\":\"stopped\"}]'\n"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.strip(), "present")

    def test_unreadable_inventory_never_proves_absence(self) -> None:
        for body in ("sleep 60\n", "exit 9\n", "printf '{bad'\n", "printf '[null]'\n"):
            result = self._run(body)
            self.assertEqual(result.returncode, 75, (body, result.stdout, result.stderr))
            self.assertNotIn("absent", result.stdout)


class TartVmAbsentRecheckTests(unittest.TestCase):
    """--attempts re-checks with backoff; --tart-home adds the directory proof."""

    def _run(self, body: str, *extra: str, store: str | None = None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "tart"
            write_fake(fake, body.replace("@COUNT@", str(root / "count")))
            args = [str(INVENTORY), "--tart", str(fake), "--timeout-seconds", "0.3",
                    "--vm-absent", "gone-vm", *extra]
            if store is not None:
                home = root / "home"
                (home / "vms").mkdir(parents=True)
                if store in ("populated", "with-vm"):
                    (home / "vms" / "golden:latest").mkdir()
                if store == "with-vm":
                    (home / "vms" / "gone-vm").mkdir()
                args += ["--tart-home", str(home)]
            return subprocess.run(args, text=True, capture_output=True, check=False, timeout=10)

    FLAKY = (
        'n=$(cat @COUNT@ 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > @COUNT@\n'
        '[ "$n" -gt 1 ] || exec sleep 30\n'
        "printf '%s\\n' '[]'\n"
    )

    def test_a_timed_out_listing_is_rechecked_with_backoff(self) -> None:
        result = self._run(self.FLAKY, "--attempts", "3", "--backoff-seconds", "0.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "absent")

    def test_a_single_attempt_does_not_recheck(self) -> None:
        # Control for the re-check: the same flaky listing, one attempt.
        result = self._run(self.FLAKY, "--attempts", "1")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertEqual(result.stdout.strip(), "unreadable")

    def test_missing_vm_directory_proves_absence_when_listing_hangs(self) -> None:
        result = self._run("exec sleep 30\n", store="populated")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "absent")

    def test_existing_vm_directory_is_present_when_listing_hangs(self) -> None:
        result = self._run("exec sleep 30\n", store="with-vm")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.strip(), "present")

    def test_empty_store_proves_nothing(self) -> None:
        # A wrong TART_HOME (an empty vms directory) must never read as absent.
        result = self._run("exec sleep 30\n", store="empty")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertEqual(result.stdout.strip(), "unreadable")

    def test_a_listed_vm_wins_over_a_missing_directory(self) -> None:
        result = self._run(
            "printf '%s\\n' '[{\"Name\":\"gone-vm\",\"State\":\"running\"}]'\n",
            store="populated",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.strip(), "present")

    def test_attempt_bounds_are_validated(self) -> None:
        for value in ("0", "11"):
            result = self._run("exit 0\n", "--attempts", value)
            self.assertEqual(result.returncode, 2, value)


if __name__ == "__main__":
    unittest.main()
