#!/usr/bin/env python3
"""boot_usage: which CI and agent paths hold the boot volume, and which grow."""

from __future__ import annotations

import testing_support  # noqa: E402
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import boot_usage as bu  # noqa: E402
import pulp_reapers as pr  # noqa: E402
import reclaim_status as rs  # noqa: E402

GIB = 1024 ** 3
DAY = 86400


def du_runner(sizes: dict[str, int]):
    """A `du -skx` double reporting `sizes[path]` bytes."""
    def run(argv, **_kwargs):
        path = argv[-1]
        return subprocess.CompletedProcess(argv, 0, stdout=f"{sizes[path] // 1024}\t{path}\n")
    return run


def sample(ts: float, **paths_gib: float) -> dict:
    paths = {path: {"state": "measured", "bytes": int(gib * GIB)}
             for path, gib in paths_gib.items()}
    return {"ts": ts, "total_bytes": sum(e["bytes"] for e in paths.values()), "paths": paths}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name).resolve()
        self.a = self.tmp / "cache"
        self.b = self.tmp / "derived"
        for path in (self.a, self.b):
            path.mkdir()
        self.device = self.tmp.stat().st_dev
        self.profile = self.tmp / "profile.toml"
        self.profile.write_text("[reclaim]\nscratch_dirs = true\n")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TARTCI_BOOT_USAGE", None)

    def run_pass(self, now: float, sizes: dict[str, int]) -> dict:
        return bu.run(state_dir=self.tmp / "state", profile=self.profile,
                      boot_volume=self.tmp, user_tmp=None, now=now,
                      paths=[self.a, self.b, self.tmp / "absent"],
                      runner=du_runner({str(self.a): sizes["a"], str(self.b): sizes["b"]}))


class Measure(Base):
    def test_paths_on_the_boot_device_are_measured_and_summed(self):
        report = bu.measure([self.a, self.b, self.tmp / "absent"], self.device,
                            du_runner({str(self.a): 3 * GIB, str(self.b): 2 * GIB}), now=1)
        self.assertEqual(report["total_bytes"], 5 * GIB)
        self.assertNotIn(str(self.tmp / "absent"), report["paths"])

    def test_a_path_on_another_volume_is_recorded_and_not_counted(self):
        # A cache moved to Workshop, or a symlink to it, frees the boot volume.
        report = bu.measure([self.a], self.device + 1, du_runner({}), now=1)
        self.assertEqual(report["paths"][str(self.a)], {"state": "off_boot"})
        self.assertEqual(report["total_bytes"], 0)


class DefaultPaths(unittest.TestCase):
    def test_runner_folders_and_stale_stores_are_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            for name in ("actions-runner-pulp-preamble", "actions-runner-v8", "actions-ci"):
                (home / name).mkdir()
            with mock.patch.object(pathlib.Path, "home", return_value=home):
                paths = bu.default_paths(None)
        names = {p.name for p in paths}
        for name in ("actions-runner-pulp-preamble", "actions-runner-v8", "actions-ci",
                     ".tart", "ccache", "pulp-trace-work", "DerivedData", ".codex"):
            self.assertIn(name, names)
        self.assertEqual(len(paths), len(set(paths)))


class Evaluate(unittest.TestCase):
    def test_over_the_total_names_the_largest_paths(self):
        warnings = bu.evaluate([sample(0, **{"/c": 100, "/d": 60, "/e": 1})], 150, 10)
        self.assertEqual(len(warnings), 1)
        self.assertIn("161 GiB > 150 GiB", warnings[0])
        self.assertIn("/c 100.0 GiB, /d 60.0 GiB", warnings[0])

    def test_under_the_total_is_quiet(self):
        self.assertEqual(bu.evaluate([sample(0, **{"/c": 100})], 150, 10), [])

    def test_a_path_growing_faster_than_the_rate_is_named(self):
        history = [sample(0, **{"/c": 10, "/d": 10}), sample(2 * DAY, **{"/c": 40, "/d": 15})]
        warnings = bu.evaluate(history, 150, 10)
        self.assertEqual(warnings, ["boot volume: /c grew 15.0 GiB/day (> 10)"])

    def test_a_baseline_younger_than_the_minimum_is_not_a_rate(self):
        history = [sample(0, **{"/c": 10}), sample(3600, **{"/c": 40})]
        self.assertEqual(bu.evaluate(history, 150, 10), [])

    def test_a_baseline_older_than_the_window_is_ignored(self):
        history = [sample(0, **{"/c": 0}), sample(30 * DAY, **{"/c": 100})]
        self.assertEqual(bu.evaluate(history, 150, 1), [])


class Run(Base):
    @testing_support.requires_tomllib
    def test_samples_at_most_once_a_day_and_keeps_history(self):
        first = self.run_pass(1000, {"a": GIB, "b": GIB})
        self.assertTrue(first["sampled"])
        again = self.run_pass(1000 + 3600, {"a": 9 * GIB, "b": GIB})
        self.assertFalse(again["sampled"])
        self.assertEqual(again["total_bytes"], 2 * GIB)
        later = self.run_pass(1000 + DAY, {"a": 20 * GIB, "b": GIB})
        self.assertTrue(later["sampled"])
        self.assertEqual(later["warnings"],
                         [f"boot volume: {self.a} grew 19.0 GiB/day (> 10)"])
        lines = (self.tmp / "state" / "boot-usage" / "history.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["ts"] for line in lines], [1000, 1000 + DAY])

    @testing_support.requires_tomllib
    def test_profile_thresholds_apply(self):
        self.profile.write_text("[reclaim]\nboot_usage_warn_gb = 1\n")
        report = self.run_pass(1000, {"a": 2 * GIB, "b": 0})
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("> 1 GiB", report["warnings"][0])

    def test_off_without_a_fleet_profile_or_when_disabled(self):
        self.profile.unlink()
        self.assertFalse(self.run_pass(1000, {"a": 0, "b": 0})["enabled"])
        self.profile.write_text("[reclaim]\nboot_usage = false\n")
        self.assertFalse(self.run_pass(1000, {"a": 0, "b": 0})["enabled"])
        self.profile.write_text("[reclaim]\n")
        os.environ["TARTCI_BOOT_USAGE"] = "0"
        self.assertFalse(self.run_pass(1000, {"a": 0, "b": 0})["enabled"])
        self.assertFalse((self.tmp / "state" / "boot-usage").exists())

    def test_the_reclaim_table_validates_its_keys(self):
        self.assertEqual(pr.validate_table({"boot_usage": True, "boot_usage_warn_gb": 120,
                                            "boot_usage_growth_gb_per_day": 5}), [])
        problems = pr.validate_table({"boot_usage": "yes", "boot_usage_warn_gb": 0})
        self.assertEqual(len(problems), 2, problems)


class Status(unittest.TestCase):
    def test_pool_status_and_doctor_see_the_warning(self):
        # fleet_doctor reports reclaim_status.degraded(); describe() prints it.
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            (state / "last-run.json").write_text(json.dumps({
                "finished_ts": 1000, "exit_code": 0, "mode": "fix",
                "boot_usage": {"enabled": True, "warnings": [
                    "boot volume: /Users/x/.codex grew 12.0 GiB/day (> 10)"]}}))
            value = rs.status(state, now=1060)
        self.assertEqual(rs.degraded(value),
                         ["boot volume: /Users/x/.codex grew 12.0 GiB/day (> 10)"])
        self.assertTrue(rs.describe(value).startswith("reclaim: WARN boot volume: /Users/x/.codex"))


if __name__ == "__main__":
    unittest.main()
