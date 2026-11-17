#!/usr/bin/env python3
"""The reap janitor bounds the launchd log it writes to.

launchd appends every 5-minute reap digest to StandardOutPath. Before this
rule nothing rotated it (87 MB on m5studio after 37 days).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_reap  # noqa: E402


class ReapLogRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.log = Path(self.temp.name) / "tartci-reap.log"
        self.env = mock.patch.dict(os.environ, {"TARTCI_REAP_LOG": str(self.log)})
        self.env.start()
        self.cap = mock.patch.object(vm_reap, "REAP_LOG_MAX_BYTES", 64)
        self.cap.start()

    def tearDown(self) -> None:
        self.cap.stop()
        self.env.stop()
        self.temp.cleanup()

    def fill(self, size: int) -> None:
        self.log.write_bytes(b"x" * size)

    def test_a_full_log_that_is_our_stdout_is_rotated(self) -> None:
        self.fill(100)
        with self.log.open("a") as stdout:
            self.assertTrue(vm_reap.rotate_own_log(stdout))
        self.assertEqual((Path(f"{self.log}.1")).stat().st_size, 100)
        self.assertEqual(self.log.stat().st_size, 0)

    def test_a_log_under_the_cap_is_kept(self) -> None:
        self.fill(10)
        with self.log.open("a") as stdout:
            self.assertFalse(vm_reap.rotate_own_log(stdout))
        self.assertFalse(Path(f"{self.log}.1").exists())

    def test_a_full_log_we_are_not_writing_is_never_renamed(self) -> None:
        self.fill(100)
        other = Path(self.temp.name) / "terminal.out"
        with other.open("a") as stdout:
            self.assertFalse(vm_reap.rotate_own_log(stdout))
        self.assertEqual(self.log.stat().st_size, 100)
        self.assertFalse(Path(f"{self.log}.1").exists())

    def test_generations_are_bounded(self) -> None:
        for _ in range(vm_reap.REAP_LOG_GENERATIONS + 2):
            self.fill(100)
            with self.log.open("a") as stdout:
                vm_reap.rotate_own_log(stdout)
        kept = sorted(p.name for p in Path(self.temp.name).iterdir())
        expected = ["tartci-reap.log"] + [f"tartci-reap.log.{i}"
                                          for i in range(1, vm_reap.REAP_LOG_GENERATIONS + 1)]
        self.assertEqual(kept, expected)

    def test_main_rotates_before_it_prints_the_digest(self) -> None:
        self.fill(100)
        with self.log.open("a") as stdout, \
                mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(vm_reap, "build_digest", return_value=({"vms": []}, 0)):
            self.assertEqual(vm_reap.main(["--json"]), 0)
        # This pass's digest followed the inherited descriptor into generation 1.
        rotated = Path(f"{self.log}.1").read_text()
        self.assertTrue(rotated.startswith("x" * 100))
        self.assertIn('"vms": []', rotated)
        self.assertEqual(self.log.stat().st_size, 0)

    def test_a_rotation_that_cannot_run_never_stops_the_reap(self) -> None:
        self.fill(100)
        with self.log.open("a") as stdout, \
                mock.patch.object(sys, "stdout", stdout), \
                mock.patch.dict(sys.modules, {"disk_reclaim": None}), \
                mock.patch.object(vm_reap, "build_digest", return_value=({"vms": []}, 3)):
            self.assertEqual(vm_reap.main(["--json"]), 3)
        self.assertIn('"vms": []', self.log.read_text())


if __name__ == "__main__":
    unittest.main()
