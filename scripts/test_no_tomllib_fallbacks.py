#!/usr/bin/env python3
"""The no-tomllib branch of every module that degrades on Python 3.9.

These modules run through `tartci_toml_exec_or_python3`, which prefers a
tomllib Python and falls back to the hosts' /usr/bin/python3 (3.9) only when
none exists, or are imported by a module a 3.9 interpreter runs. Without
tomllib the fleet profile cannot be read, and each module must say so and do
nothing with it, rather than crash or act on defaults. That
fallback is the production behaviour on such a host, so it is tested here on
every interpreter: tomllib is removed from the module for the test, which runs
the same branch the real 3.9 does.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import boot_usage  # noqa: E402
import build_disagreement_watch  # noqa: E402
import disk_reclaim  # noqa: E402
import gate_ccache_trim  # noqa: E402
import pulp_reapers  # noqa: E402
import scratch_dirs  # noqa: E402
import tartci_launchd_watchdog  # noqa: E402
import tmp_checkouts  # noqa: E402

REASON = "no tomllib (needs Python 3.11+)"


def never(*args, **kwargs):
    raise AssertionError(f"ran a command without a profile: {args}")


class NoTomllib(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        # A real, opted-in profile: only the missing parser keeps it unread.
        self.profile = self.dir / "profile.toml"
        self.profile.write_text("[reclaim]\npulp_worktree_builds = true\ntmp_checkouts = true\n"
                                "scratch_dirs = true\ngate_ccache_trim = true\n"
                                "[host]\ntart_home = \"/Volumes/Store\"\n[build_disagreement]\nenabled = true\n")

    def without(self, module) -> mock._patch:
        patch = mock.patch.object(module, "tomllib", None)
        patch.start()
        self.addCleanup(patch.stop)
        return patch

    def test_scratch_dirs_reports_and_removes_nothing(self) -> None:
        self.without(scratch_dirs)
        self.assertEqual(scratch_dirs.run(fix=True, profile=self.profile, runner=never),
                         {"enabled": False, "reason": REASON})

    def test_tmp_checkouts_reports_and_removes_nothing(self) -> None:
        self.without(tmp_checkouts)
        self.assertEqual(tmp_checkouts.run(fix=True, profile=self.profile, in_use=None,
                                           runner=never),
                         {"enabled": False, "reason": REASON})

    def test_pulp_reapers_reports_and_runs_no_reaper(self) -> None:
        self.without(pulp_reapers)
        self.assertEqual(pulp_reapers.run(fix=True, profile=self.profile, runner=never,
                                          reaper=never),
                         {"enabled": False, "reason": REASON})

    def test_build_disagreement_watch_is_disabled_and_never_runs_the_detector(self) -> None:
        self.without(build_disagreement_watch)
        report = build_disagreement_watch.cycle(self.profile, self.dir / "state.json",
                                                runner=never)
        self.assertEqual(report, {"state": "disabled", "reason": REASON, "ran": False})

    def test_boot_usage_is_disabled_and_samples_nothing(self) -> None:
        self.without(boot_usage)
        report = boot_usage.run(state_dir=self.dir, profile=self.profile,
                                boot_volume=self.dir, user_tmp=None, runner=never)
        self.assertEqual(report, {"enabled": False, "reason": REASON})
        self.assertFalse((self.dir / "boot-usage").exists())

    def test_disk_reclaim_reads_no_profile_roots_or_lease_store(self) -> None:
        self.without(disk_reclaim)
        with mock.patch.object(disk_reclaim.pulp_reapers, "default_profile_path",
                               return_value=self.profile), \
                mock.patch.dict(disk_reclaim.os.environ, {"TART_HOME": ""}):
            self.assertEqual(disk_reclaim.profile_root_candidates(), [])
            self.assertIsNone(disk_reclaim.resolve_lease_path(None))

    def test_gate_ccache_trim_reads_no_settings(self) -> None:
        self.without(gate_ccache_trim)
        self.assertEqual(gate_ccache_trim.load_settings(self.profile), (None, REASON))

    def test_the_watchdog_reads_no_tart_store_from_the_profile(self) -> None:
        # A partial parse could read a torn profile as an idle default store,
        # so without tomllib the store must come from TART_HOME or not at all.
        self.assertEqual(tartci_launchd_watchdog._profile_tart_home(str(self.profile)),
                         "/Volumes/Store" if tartci_launchd_watchdog.tomllib else None)
        self.without(tartci_launchd_watchdog)
        self.assertIsNone(tartci_launchd_watchdog._profile_tart_home(str(self.profile)))


if __name__ == "__main__":
    unittest.main()
