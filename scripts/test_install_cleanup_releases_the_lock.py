#!/usr/bin/env python3
"""A staged launcher bundle must not be able to strand the pool lock.

`ditto` copies the launcher with its support tree already `a-w`, so the staged
candidate arrives read-only. Under `set -e` a failing `rm -rf` in the EXIT trap
aborts cleanup *before* the lock release, orphaning the pool transition lock and
wedging the host on "pool transition busy" until a manual repair.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "install_macos_fleet.sh"


class InstallCleanupTests(unittest.TestCase):
    def test_removal_restores_write_before_deleting(self) -> None:
        body = SCRIPT.read_text()
        cleanup = body[body.index("cleanup() {"):body.index("trap cleanup EXIT")]
        self.assertIn("chmod -R u+w -- \"$launcher_candidate\"", cleanup)
        # the chmod must come BEFORE the rm, or it buys nothing
        self.assertLess(
            cleanup.index("chmod -R u+w -- \"$launcher_candidate\""),
            cleanup.index("rm -rf -- \"$launcher_candidate\""),
        )

    def test_no_cleanup_step_can_abort_before_the_lock_release(self) -> None:
        body = SCRIPT.read_text()
        cleanup = body[body.index("cleanup() {"):body.index("trap cleanup EXIT")]
        release = cleanup.index("tartci_pool_lock_release")
        # Every command between the candidate removal and the lock release must
        # be failure-tolerant, since this trap runs under `set -e`.
        between = cleanup[cleanup.index("$launcher_candidate"):release]
        for line in between.splitlines():
            stripped = line.strip()
            if stripped.startswith(("rm ", "chmod ", "mv ")):
                self.assertTrue(
                    stripped.endswith("|| true"),
                    f"under set -e this can strand the lock: {stripped}",
                )

    def test_a_readonly_staged_bundle_is_actually_removable(self) -> None:
        """Behavioural control: the shape ditto produces, and the fix on it."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cand = root / ".TartCILauncher.app.test"
            support = cand / "Contents" / "Resources" / "support"
            support.mkdir(parents=True)
            (support / "file").write_text("x")
            subprocess.run(["chmod", "-R", "a-w", str(support)], check=True)

            # POSITIVE CONTROL: without the chmod, removal genuinely fails.
            naive = subprocess.run(
                ["bash", "-c", f'rm -rf -- "{cand}"'],
                capture_output=True, text=True,
            )
            self.assertTrue(cand.exists(), "control invalid: rm unexpectedly succeeded")
            self.assertNotEqual(naive.returncode, 0)

            fixed = subprocess.run(
                ["bash", "-c",
                 f'chmod -R u+w -- "{cand}" 2>/dev/null || true; rm -rf -- "{cand}" || true'],
                capture_output=True, text=True,
            )
            self.assertEqual(fixed.returncode, 0)
            self.assertFalse(cand.exists(), "the fix did not remove the bundle")

    def test_set_e_abort_semantics_are_real(self) -> None:
        """The premise: a failing command in a set -e trap skips what follows."""
        with tempfile.TemporaryDirectory() as td:
            ro = Path(td) / "ro"
            (ro / "sub").mkdir(parents=True)
            subprocess.run(["chmod", "a-w", str(ro)], check=True)
            proc = subprocess.run(
                ["bash", "-c",
                 f'set -euo pipefail\n'
                 f'cleanup(){{ rc=$?; trap - EXIT; rm -rf -- "{ro}/sub"; '
                 f'echo REACHED; exit $rc; }}\n'
                 f'trap cleanup EXIT\nexit 1'],
                capture_output=True, text=True,
            )
            self.assertNotIn("REACHED", proc.stdout)
            subprocess.run(["chmod", "-R", "u+w", str(ro)], check=False)


if __name__ == "__main__":
    unittest.main()
