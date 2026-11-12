#!/usr/bin/env python3
"""Regression tests for scripts/runtime_measure.py."""

from __future__ import annotations

import json
import hashlib
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TARTCI = ROOT / "tartci"


class RuntimeMeasureCliTest(unittest.TestCase):
    def run_cli(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        process_env = os.environ.copy()
        process_env.update(env or {})
        return subprocess.run(
            [str(TARTCI), "runtime", *args],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_env,
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

    def test_schema_v2_has_stable_correlation_identity_and_unavailable_phases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            timing = root / "timing.tsv"
            timing.write_text("phase\tseconds\nboot_to_ssh\t1.2\ntotal\t1.2\n", encoding="utf-8")
            self.run_cli(
                "--store", str(root / "store"), "complete", "--repo", "owner/repo",
                "--provider", "tart-macos", "--runner-name", "lane", "--run-id", "7",
                "--job-id", "8", "--attempt", "2", "--timing-path", str(timing),
                "--image-generation", "image-r17", "--toolchain-generation", "xcode-26",
                "--cache-generation", "ccache-g4", "--exit-code", "0", "--json",
            )
            payload = json.loads(self.run_cli(
                "--store", str(root / "store"), "summary", "--repo", "owner/repo",
                "--run-id", "7", "--job-id", "8", "--json",
            ).stdout)
            record = payload["records"][0]
            self.assertEqual(record["schema_version"], 2)
            self.assertEqual(record["correlation_id"], "corr:" + hashlib.sha256(
                b"github:7/8/2"
            ).hexdigest()[:32])
            self.assertEqual(record["image_generation"], "image-r17")
            self.assertEqual(record["toolchain_generation"], "xcode-26")
            self.assertEqual(record["cache_generation"], "ccache-g4")
            rows = {row["phase"]: row for row in record["phase_rows"]}
            self.assertEqual(rows["vm_boot"]["status"], "unavailable")
            self.assertEqual(rows["boot_to_ssh"], {"phase": "boot_to_ssh", "duration_ms": 1200, "status": "measured"})
            self.assertEqual(rows["build"]["status"], "unavailable")
            self.assertIsNone(rows["build"]["duration_ms"])
            self.assertEqual(rows["total"]["duration_ms"], 1200)

    def test_backfill_is_repeatable_and_does_not_stamp_current_generations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            timing = root / "logs" / "lane" / "timing.tsv"
            timing.parent.mkdir(parents=True)
            timing.write_text("phase\tseconds\nboot_to_ssh\t1\ntotal\t1\n", encoding="utf-8")
            for _ in range(2):
                self.run_cli(
                    "--store", str(root / "store"), "backfill", "--repo", "owner/repo",
                    "--timing", str(root / "logs"), env={"TARTCI_IMAGE_GENERATION": "live-r9"},
                )
            rows = [json.loads(line) for line in (root / "store" / "records" / "owner__repo.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["correlation_id"], rows[1]["correlation_id"])
            self.assertEqual({rows[0][key] for key in ("image_generation", "toolchain_generation", "cache_generation")}, {"unavailable"})

    def test_metrics_backfill_emits_v2_identity_and_phase_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            metrics = root / "metrics.jsonl"
            metrics.write_text(json.dumps({"provider": "tart-linux", "ts": "2026-10-16T00:00:00Z", "build_s": 2, "ctest_s": 3, "configure_s": 1}) + "\n", encoding="utf-8")
            self.run_cli("--store", str(root / "store"), "backfill", "--repo", "owner/repo", "--metrics-jsonl", str(metrics))
            record = json.loads((root / "store" / "records" / "owner__repo.jsonl").read_text())
            self.assertEqual(record["schema_version"], 2)
            self.assertTrue(record["correlation_id"].startswith("corr:"))
            phases = {row["phase"]: row for row in record["phase_rows"]}
            self.assertEqual(phases["build"]["duration_ms"], 2000)
            self.assertEqual(phases["test"]["duration_ms"], 3000)
            self.assertEqual(phases["configure"]["duration_ms"], 1000)

    def test_live_generation_environment_is_used_and_total_is_measured_from_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            result = self.run_cli(
                "--store", str(root / "store"), "complete", "--repo", "owner/repo",
                "--provider", "tart-linux", "--runner-name", "lane",
                "--started-at", "2026-10-16T00:00:00Z", "--completed-at", "2026-10-16T00:00:02Z",
                "--exit-code", "0", "--json",
                env={"TARTCI_IMAGE_GENERATION": "img-r9", "TARTCI_TOOLCHAIN_GENERATION": "clang-r4", "TARTCI_CACHE_GENERATION": "cache-r3"},
            )
            record = json.loads(result.stdout)["record"]
            self.assertEqual(record["image_generation"], "img-r9")
            self.assertEqual(record["toolchain_generation"], "clang-r4")
            self.assertEqual(record["cache_generation"], "cache-r3")
            phases = {row["phase"]: row for row in record["phase_rows"]}
            self.assertEqual(phases["total"], {"phase": "total", "duration_ms": 2000, "status": "measured"})

    def test_phase_translation_explicit_correlation_attempt_and_queue_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            timing = root / "timing.tsv"
            timing.write_text(
                "phase\tseconds\ncleanup\t0.1\nlc_clone\t0.2\nlc_boot_ip\t0.3\nlc_ip_ssh\t0.4\nlc_prep\t0.5\nlc_register\t0.6\nlc_teardown\t0.7\ntotal\t9\n",
                encoding="utf-8",
            )
            common = [
                "--store", str(root / "store"), "complete", "--repo", "owner/repo",
                "--provider", "tart-macos", "--runner-name", "lane", "--run-id", "7",
                "--job-id", "8", "--timing-path", str(timing), "--queued-at", "2026-10-16T00:00:00Z",
                "--started-at", "2026-10-16T00:00:05Z", "--exit-code", "0", "--json",
            ]
            first = json.loads(self.run_cli(*common, "--attempt", "1").stdout)["record"]
            second = json.loads(self.run_cli(*common, "--attempt", "2").stdout)["record"]
            explicit = json.loads(self.run_cli(*common, "--attempt", "3", "--correlation-id", "corr:explicit").stdout)["record"]
            self.assertNotEqual(first["correlation_id"], second["correlation_id"])
            self.assertEqual(explicit["correlation_id"], "corr:explicit")
            phases = {row["phase"]: row for row in first["phase_rows"]}
            for phase in ("vm_clone", "vm_boot", "guest_ready", "setup", "dispatch", "teardown"):
                self.assertEqual(phases[phase]["status"], "measured", phase)
            self.assertEqual(phases["queue_wait"]["duration_ms"], 5000)
            self.assertEqual(phases["queue_wait"]["started_at"], "2026-10-16T00:00:00Z")
            self.assertEqual(phases["queue_wait"]["completed_at"], "2026-10-16T00:00:05Z")
            self.assertEqual(phases["total"]["duration_ms"], 9000)
            self.assertEqual(phases["cleanup"]["status"], "measured")

    def test_golden_name_without_content_hash_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = json.loads(self.run_cli(
                "--store", str(pathlib.Path(tmp) / "store"), "complete", "--repo", "owner/repo",
                "--provider", "tart-macos", "--golden", "some-vm-name", "--exit-code", "0", "--json",
            ).stdout)["record"]
            self.assertEqual(record["image_generation"], "unavailable")

    def test_metrics_backfill_same_timestamp_uses_line_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            metrics = root / "metrics.jsonl"
            source = {"provider": "tart-linux", "ts": "2026-10-16T00:00:00Z", "build_s": 1}
            metrics.write_text(json.dumps(source) + "\n" + json.dumps(source) + "\n", encoding="utf-8")
            self.run_cli("--store", str(root / "store"), "backfill", "--repo", "owner/repo", "--metrics-jsonl", str(metrics))
            rows = [json.loads(line) for line in (root / "store" / "records" / "owner__repo.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["correlation_id"], rows[1]["correlation_id"])


if __name__ == "__main__":
    unittest.main()
