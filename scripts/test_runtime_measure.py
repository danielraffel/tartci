#!/usr/bin/env python3
"""Regression tests for scripts/runtime_measure.py."""

from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TARTCI = ROOT / "tartci"


class RuntimeMeasureCliTest(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(TARTCI), "runtime", *args],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def test_empty_summary_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_cli("--store", tmp, "summary", "--repo", "owner/repo", "--run-id", "123", "--json")
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["found"])
            self.assertEqual(payload["records"], [])

    def test_complete_summary_and_export_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            timing_dir = root / "linux-ephr-test"
            timing_dir.mkdir()
            timing_path = timing_dir / "timing.tsv"
            timing_path.write_text(
                "phase\tseconds\nboot_to_ssh\t1.2\nrunner_process\t3.4\ncleanup\t0.3\ntotal\t4.9\n",
                encoding="utf-8",
            )
            self.run_cli(
                "--store",
                str(root / "store"),
                "complete",
                "--repo",
                "owner/repo",
                "--provider",
                "tart-linux",
                "--platform",
                "linux",
                "--arch",
                "arm64",
                "--runner-name",
                "linux-ephr-test",
                "--run-id",
                "123",
                "--job-id",
                "456",
                "--timing-path",
                str(timing_path),
                "--exit-code",
                "0",
                "--json",
            )
            summary = self.run_cli(
                "--store",
                str(root / "store"),
                "summary",
                "--repo",
                "owner/repo",
                "--run-id",
                "123",
                "--job-id",
                "456",
                "--json",
            )
            payload = json.loads(summary.stdout)
            self.assertTrue(payload["found"])
            record = payload["records"][0]
            self.assertEqual(record["external_id"], "github:123/456/")
            self.assertEqual(record["boot_ms"], 1200)
            self.assertEqual(record["run_ms"], 3400)
            self.assertEqual(record["total_ms"], 4900)

            exported = self.run_cli("--store", str(root / "store"), "export", "--repo", "owner/repo")
            rows = [json.loads(line) for line in exported.stdout.splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["runner_name"], "linux-ephr-test")

    def test_backfill_timing_tolerates_missing_github_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            timing_dir = root / "logs" / "linux-ephr-test"
            timing_dir.mkdir(parents=True)
            (timing_dir / "timing.tsv").write_text(
                "phase\tseconds\nboot_to_ssh\t1\nrunner_process\t2\ntotal\t3\n",
                encoding="utf-8",
            )
            proc = self.run_cli(
                "--store",
                str(root / "store"),
                "backfill",
                "--repo",
                "owner/repo",
                "--timing",
                str(root / "logs"),
            )
            self.assertEqual(json.loads(proc.stdout)["backfilled"], 1)


if __name__ == "__main__":
    unittest.main()
