#!/usr/bin/env python3
"""disk_pressure counts space macOS can purge, with a hard floor on raw free.

On m5studio statfs reported 49 GB free (98% used) on /System/Volumes/Data
while volumeAvailableCapacityForImportantUsage reported 560 GB. Counting only
the raw number flagged disk_pressure and held fleet_ready false.
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testing_support  # noqa: E402

testing_support.skip_module_without_tomllib()  # macos_fleet_lanes imports tomllib
import macos_fleet_lanes as fleet  # noqa: E402

GIB = 1024 ** 3
TOTAL = 1858 * GIB


class DiskPressurePurgeableTests(unittest.TestCase):
    def rows(self, raw_free_gib, important_gib):
        usage = types.SimpleNamespace(total=TOTAL, free=int(raw_free_gib * GIB),
                                      used=TOTAL - int(raw_free_gib * GIB))
        important = None if important_gib is None else int(important_gib * GIB)
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(fleet.shutil, "disk_usage", return_value=usage), \
                mock.patch.object(fleet, "important_free_bytes", return_value=important):
            return fleet.disk_pressure(Path(home) / "none.toml", Path(home))

    def home(self, rows):
        return next(r for r in rows if r["role"] == "home")

    def test_purgeable_heavy_volume_is_not_under_pressure(self) -> None:
        # 49 GiB raw free (97.4% used), 560 GiB for important usage.
        row = self.home(self.rows(49, 560))
        self.assertNotEqual(row["state"], "problem", row)
        self.assertEqual(row["raw_free_gib"], 49.0)
        self.assertEqual(row["important_free_gib"], 560.0)
        self.assertLess(row["used_percent"], fleet.HOME_DISK_PROBLEM_PERCENT)
        self.assertGreater(row["raw_used_percent"], 97)

    def test_raw_free_below_the_floor_is_pressure_whatever_is_purgeable(self) -> None:
        row = self.home(self.rows(15, 900))
        self.assertEqual(row["state"], "problem", row)
        self.assertTrue(row["below_raw_floor"])

    def test_helper_failure_falls_back_to_statfs(self) -> None:
        # Same raw numbers as the first test, no important-usage reading:
        # the old behaviour, 97%+ used is a problem.
        row = self.home(self.rows(40, None))
        self.assertEqual(row["state"], "problem", row)
        self.assertIsNone(row["important_free_gib"])
        self.assertEqual(row["used_percent"], row["raw_used_percent"])

    def test_helper_exceptions_and_garbage_read_as_unavailable(self) -> None:
        import bounded_subprocess as bs
        with mock.patch.object(bs, "run_bounded", side_effect=OSError("no swift")):
            self.assertIsNone(fleet.important_free_bytes(Path("/")))
        with mock.patch.object(bs, "run_bounded",
                               side_effect=bs.ObservationError("important_capacity", "timeout", "x")):
            self.assertIsNone(fleet.important_free_bytes(Path("/")))
        bad = types.SimpleNamespace(returncode=0, stdout="not a number\n")
        with mock.patch.object(bs, "run_bounded", return_value=bad):
            self.assertIsNone(fleet.important_free_bytes(Path("/")))
        failed = types.SimpleNamespace(returncode=1, stdout="")
        with mock.patch.object(bs, "run_bounded", return_value=failed):
            self.assertIsNone(fleet.important_free_bytes(Path("/")))

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/swift").exists(),
                         "needs macOS swift")
    def test_the_real_helper_reads_at_least_the_raw_free_space(self) -> None:
        import shutil
        value = fleet.important_free_bytes(Path.home())
        self.assertIsNotNone(value)
        self.assertGreaterEqual(value, shutil.disk_usage(Path.home()).free * 0.9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
