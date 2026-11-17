#!/usr/bin/env python3
"""Lease admission, reclaim and the VM disk floor count macOS-purgeable space.

statfs omits space macOS can purge on demand (m5studio: 49 GB raw free,
560 GB for important usage), so a lease was refused and reclaim fell back to
its 7-day age gate on a volume with room. Each call site now takes
disk_available's number, keeps the 20 GiB raw floor, and falls back to the raw
figure when the macOS reading is unavailable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testing_support  # noqa: E402,F401
import disk_available as da  # noqa: E402
import disk_reclaim as dr  # noqa: E402
import lease_disk as ld  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATE_HELPER = ROOT / "providers" / "common" / "vm-state.lib.sh"
GIB = 1024 ** 3
TOTAL = 1858 * GIB


def usage(raw_free_gib):
    free = int(raw_free_gib * GIB)
    return types.SimpleNamespace(total=TOTAL, free=free, used=TOTAL - free)


class Purgeable(unittest.TestCase):
    """Run with the macOS reading on, and `important` (GiB or None) mocked."""

    def setUp(self) -> None:
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.cache_file = Path(cache.name) / "disk-important-cache.json"
        env = mock.patch.dict(os.environ, {"TARTCI_DISK_PURGEABLE": "1",
                                           "TARTCI_DISK_CACHE_FILE": str(self.cache_file)})
        env.start()
        self.addCleanup(env.stop)

    def important(self, gib):
        value = None if gib is None else int(gib * GIB)
        patcher = mock.patch.object(da, "important_free_bytes", return_value=value)
        patcher.start()
        self.addCleanup(patcher.stop)


class DiskAvailableTests(Purgeable):
    def test_effective_available(self) -> None:
        self.assertEqual(da.effective_available(49 * GIB, TOTAL, 560 * GIB), (560 * GIB, False))
        self.assertEqual(da.effective_available(15 * GIB, TOTAL, 900 * GIB), (15 * GIB, True))
        self.assertEqual(da.effective_available(49 * GIB, TOTAL, None), (49 * GIB, False))
        self.assertEqual(da.effective_available(49 * GIB, TOTAL, 9 * TOTAL), (TOTAL, False))

    def test_the_off_switch_ignores_the_macos_reading(self) -> None:
        self.important(560)
        with mock.patch.dict(os.environ, {"TARTCI_DISK_PURGEABLE": "0"}):
            self.assertEqual(da.measure("/", 49 * GIB, TOTAL)["available_bytes"], 49 * GIB)


class CacheTests(Purgeable):
    def test_two_calls_within_the_ttl_run_the_helper_once(self) -> None:
        patcher = mock.patch.object(da, "important_free_bytes", return_value=560 * GIB)
        helper = patcher.start()
        self.addCleanup(patcher.stop)
        with tempfile.TemporaryDirectory() as directory:
            first = da.measure(directory, 49 * GIB, TOTAL, now=1000.0)
            second = da.measure(directory, 49 * GIB, TOTAL, now=1030.0)
            third = da.measure(directory, 49 * GIB, TOTAL, now=1000.0 + da.CACHE_TTL_S + 1)
        self.assertEqual(helper.call_count, 2, "the third call is past the TTL")
        self.assertFalse(first["important_cached"])
        self.assertTrue(second["important_cached"])
        self.assertEqual(second["available_bytes"], 560 * GIB)
        self.assertFalse(third["important_cached"])

    def test_a_raw_drop_below_the_floor_is_caught_with_a_warm_cache(self) -> None:
        patcher = mock.patch.object(da, "important_free_bytes", return_value=560 * GIB)
        helper = patcher.start()
        self.addCleanup(patcher.stop)
        with tempfile.TemporaryDirectory() as directory:
            warm = da.measure(directory, 49 * GIB, TOTAL, now=1000.0)
            full = da.measure(directory, 5 * GIB, TOTAL, now=1010.0)
        self.assertEqual(warm["available_bytes"], 560 * GIB)
        self.assertEqual(helper.call_count, 1)
        self.assertTrue(full["important_cached"])
        self.assertTrue(full["below_raw_floor"])
        self.assertEqual(full["available_bytes"], 5 * GIB)

    def test_a_raw_drop_above_the_floor_comes_off_the_cached_figure(self) -> None:
        patcher = mock.patch.object(da, "important_free_bytes", return_value=100 * GIB)
        patcher.start()
        self.addCleanup(patcher.stop)
        with tempfile.TemporaryDirectory() as directory:
            da.measure(directory, 60 * GIB, TOTAL, now=1000.0)
            later = da.measure(directory, 30 * GIB, TOTAL, now=1010.0)
        self.assertEqual(later["available_bytes"], 70 * GIB)

    def test_an_unreadable_or_corrupt_cache_is_ignored(self) -> None:
        patcher = mock.patch.object(da, "important_free_bytes", return_value=560 * GIB)
        helper = patcher.start()
        self.addCleanup(patcher.stop)
        self.cache_file.write_text("{not json")
        with tempfile.TemporaryDirectory() as directory:
            result = da.measure(directory, 49 * GIB, TOTAL, now=1000.0)
        self.assertEqual(helper.call_count, 1)
        self.assertEqual(result["available_bytes"], 560 * GIB)
        self.assertIn("volumes", self.cache_file.read_text())


class LeaseDiskTests(Purgeable):
    def probe(self, raw_gib):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(ld.shutil, "disk_usage", return_value=usage(raw_gib)):
            return ld._disk_probe_impl(directory)

    def test_purgeable_heavy_volume_offers_the_important_usage_capacity(self) -> None:
        self.important(560)
        result = self.probe(49)
        self.assertEqual(result["free_bytes"], 560 * GIB)
        self.assertEqual(result["raw_free_bytes"], 49 * GIB)
        self.assertEqual(result["important_free_bytes"], 560 * GIB)

    def test_raw_free_under_the_floor_gets_no_purgeable_credit(self) -> None:
        self.important(900)
        self.assertEqual(self.probe(15)["free_bytes"], 15 * GIB)

    def test_helper_failure_uses_the_raw_number(self) -> None:
        self.important(None)
        self.assertEqual(self.probe(49)["free_bytes"], 49 * GIB)


class ReclaimTests(Purgeable):
    def measure(self, raw_gib):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(dr.shutil, "disk_usage", return_value=usage(raw_gib)):
            return dr.free_bytes(Path(directory)), dr.used_percent(Path(directory))

    def test_purgeable_heavy_volume_is_not_tight(self) -> None:
        self.important(560)
        free, percent = self.measure(49)
        self.assertEqual(free, 560 * GIB)
        self.assertLess(percent, 75)

    def test_raw_free_under_the_floor_is_tight(self) -> None:
        self.important(900)
        free, percent = self.measure(15)
        self.assertEqual(free, 15 * GIB)
        self.assertGreater(percent, 99)

    def test_helper_failure_uses_the_raw_number(self) -> None:
        self.important(None)
        free, percent = self.measure(49)
        self.assertEqual(free, 49 * GIB)
        self.assertGreater(percent, 97)


class WedgedVolumeTests(Purgeable):
    """disk_available.measure stats the path and reads a cache: a wedged
    volume must cost reclaim a bounded wait, never an indefinite one."""

    def test_a_blocked_measurement_reads_as_unreadable_within_the_deadline(self) -> None:
        import threading
        import time
        release = threading.Event()
        self.addCleanup(release.set)
        real = dr.bounded_fs_call

        def quick(operation, **kwargs):
            return real(operation, **dict(kwargs, timeout=0.3))

        def wedged(path, raw_free, total, **kwargs):
            release.wait(10)
            return {"available_bytes": 560 * GIB}

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(dr.shutil, "disk_usage", return_value=usage(49)), \
                mock.patch.object(dr, "bounded_fs_call", quick), \
                mock.patch.object(da, "measure", wedged):
            started = time.monotonic()
            free = dr.free_bytes(Path(directory))
            percent = dr.used_percent(Path(directory))
            elapsed = time.monotonic() - started
        self.assertIsNone(free)
        self.assertIsNone(percent)
        self.assertLess(elapsed, 3, "free_bytes/used_percent waited on the wedged measurement")


class VmDiskFloorTests(unittest.TestCase):
    """tartci_check_disk_floor: helper number first, df when the helper fails."""

    def run_floor(self, helper_script: str) -> int:
        real = subprocess.run(["/bin/sh", "-c", "command -v python3"], capture_output=True,
                              text=True, check=True).stdout.strip()
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            (bin_dir / "df").write_text("#!/bin/sh\nprintf 'Filesystem 1024-blocks Used Available\\n"
                                        "/dev/x 100 1 1048576\\n'\n")  # 1 GiB
            (bin_dir / "python3").write_text(
                "#!/bin/sh\ncase \"$*\" in *disk_available.py*) " + helper_script
                + ";; *) exec " + real + " \"$@\";; esac\n")
            for name in ("df", "python3"):
                (bin_dir / name).chmod(0o755)
            script = textwrap.dedent(f"""
                set -euo pipefail
                export PATH={bin_dir}:$PATH
                export TARTCI_VM_DISK_FREE_FLOOR_GB=25
                note() {{ :; }}
                source {STATE_HELPER}
                tartci_check_disk_floor {td}
            """)
            return subprocess.run(["/bin/bash", "-c", script], capture_output=True,
                                  text=True, check=False).returncode

    def test_purgeable_space_clears_the_floor(self) -> None:
        # df says 1 GiB; the helper says 560 GiB (587202560 KiB).
        self.assertEqual(self.run_floor("echo 587202560"), 0)

    def test_helper_failure_falls_back_to_df_and_refuses(self) -> None:
        self.assertEqual(self.run_floor("exit 1"), 75)


if __name__ == "__main__":
    unittest.main(verbosity=2)
