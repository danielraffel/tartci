#!/usr/bin/env python3
"""Bench expiry is opt-in per clone and never removes unmarked owner data."""

import contextlib
import json
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bench_prune  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


class BenchPruneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = pathlib.Path(self.temp.name) / "bench"
        self.store.mkdir()

    def clone(self, name, age=20, marker=True):
        path = self.store / name
        path.mkdir()
        (path / "disk.img").write_bytes(b"abc")
        if marker:
            pathlib.Path(str(path) + ".tartci-bench.json").write_text(json.dumps({
                "owner": "test", "created": (NOW - timedelta(days=age)).isoformat(),
                "ttl_days": 14,
            }))
        return path

    @staticmethod
    def lsof(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "")

    def test_expired_is_planned_and_deleted_with_marker_on_apply(self):
        path = self.clone("old")
        report = bench_prune.plan(self.store, runner=self.lsof, now=NOW)
        self.assertEqual(report["entries"][0]["verdict"], "delete")
        self.assertTrue(path.exists())
        self.assertEqual(bench_prune.apply(report, runner=self.lsof), [])
        self.assertFalse(path.exists())
        self.assertFalse(pathlib.Path(str(path) + ".tartci-bench.json").exists())

    def test_young_open_unmarked_and_corrupt_are_safe(self):
        self.clone("young", age=2)
        self.clone("open")
        self.clone("unmarked", marker=False)
        corrupt = self.clone("corrupt")
        pathlib.Path(str(corrupt) + ".tartci-bench.json").write_text("{")

        def lsof(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "123\n" if "open" in argv[-1] else "", "")

        report = bench_prune.plan(self.store, runner=lsof, now=NOW)
        verdicts = {entry["name"]: entry["verdict"] for entry in report["entries"]}
        self.assertEqual(verdicts, {"young": "keep", "open": "keep",
                                    "unmarked": "unmanaged", "corrupt": "keep"})
        self.assertEqual(bench_prune.apply(report, runner=lsof), [])
        for name in verdicts:
            self.assertTrue((self.store / name).exists())

    def test_plan_without_apply_changes_nothing(self):
        path = self.clone("old")
        with mock.patch.dict(os.environ, {"TARTCI_BENCH": str(self.store)}):
            self.assertEqual(bench_prune.main(["--json"], runner=self.lsof, now=NOW), 0)
        self.assertTrue(path.exists())

    def test_lsof_error_keeps_expired_clone(self):
        self.clone("old")
        def failed(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 30)
        report = bench_prune.plan(self.store, runner=failed, now=NOW)
        self.assertEqual(report["entries"][0]["verdict"], "keep")

    def test_unreadable_store_returns_error_report(self):
        store = mock.Mock(spec=pathlib.Path)
        store.is_dir.return_value = True
        store.is_symlink.return_value = False
        store.iterdir.side_effect = PermissionError("denied")
        report = bench_prune.plan(store, runner=self.lsof, now=NOW)
        self.assertEqual(report["entries"], [])
        self.assertIn("bench store unavailable", report["error"])

    def test_non_string_created_is_kept_as_unreadable(self):
        clone = self.clone("bad-created")
        pathlib.Path(str(clone) + ".tartci-bench.json").write_text(json.dumps({
            "owner": "test", "created": None, "ttl_days": 14,
        }))
        report = bench_prune.plan(self.store, runner=self.lsof, now=NOW)
        row = next(entry for entry in report["entries"] if entry["name"] == "bad-created")
        self.assertEqual(row["verdict"], "keep")
        self.assertIn("invalid created", row["reason"])

    def test_empty_bench_env_uses_home_default(self):
        home = pathlib.Path(self.temp.name) / "home"
        default_store = home / "bench"
        default_store.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"TARTCI_HOME": str(home), "TARTCI_BENCH": ""}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(bench_prune.main(["--json"], runner=self.lsof, now=NOW), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["store"], str(default_store))

    def test_new_windows_clone_gets_marker(self):
        golden = pathlib.Path(self.temp.name) / "goldens"
        golden.mkdir()
        (golden / "base-windows.qcow2").write_bytes(b"golden")
        fake_open = pathlib.Path(self.temp.name) / "open"
        fake_open.write_text("#!/bin/sh\nexit 0\n")
        fake_open.chmod(0o755)
        env = dict(os.environ, TARTCI_GOLDENS=str(golden), TARTCI_BENCH=str(self.store),
                   TARTCI_BENCH_OWNER="bench-test", TARTCI_BENCH_TTL_DAYS="3",
                   PATH=f"{self.temp.name}:{os.environ.get('PATH', '')}")
        proc = subprocess.run(["bash", str(ROOT / "bench/bench.sh"), "windows", "clone"],
                              env=env, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        marker = json.loads((self.store / "clone.qcow2.tartci-bench.json").read_text())
        self.assertEqual(marker["owner"], "bench-test")
        self.assertEqual(marker["ttl_days"], 3)
        self.assertIsNotNone(datetime.fromisoformat(marker["created"].replace("Z", "+00:00")).tzinfo)

    def test_zero_ttl_is_rejected(self):
        golden = pathlib.Path(self.temp.name) / "goldens"
        golden.mkdir()
        (golden / "base-windows.qcow2").write_bytes(b"golden")
        env = dict(os.environ, TARTCI_GOLDENS=str(golden), TARTCI_BENCH=str(self.store),
                   TARTCI_BENCH_TTL_DAYS="00")
        proc = subprocess.run(["bash", str(ROOT / "bench/bench.sh"), "windows", "clone"],
                              env=env, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid TARTCI_BENCH_TTL_DAYS: 00", proc.stderr)
        self.assertFalse((self.store / "clone.qcow2").exists())

    def test_marker_failure_removes_new_clone(self):
        golden = pathlib.Path(self.temp.name) / "goldens"
        golden.mkdir()
        (golden / "base-windows.qcow2").write_bytes(b"golden")
        marker = self.store / "clone.qcow2.tartci-bench.json"
        marker.write_text("existing")
        env = dict(os.environ, TARTCI_GOLDENS=str(golden), TARTCI_BENCH=str(self.store))
        proc = subprocess.run(["bash", str(ROOT / "bench/bench.sh"), "windows", "clone"],
                              env=env, capture_output=True, text=True, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.store / "clone.qcow2").exists())
        self.assertEqual(marker.read_text(), "existing")


if __name__ == "__main__":
    unittest.main()
