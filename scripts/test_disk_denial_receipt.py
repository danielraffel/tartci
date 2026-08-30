#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "disk_denial_receipt.py"


def observe(receipt_dir: Path, attempt: dict, *, runner: str = "studio-pulp-gate") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3", str(SCRIPT), "--receipt-dir", str(receipt_dir),
            "--host", "studio",
            "--provider", "tart-macos", "--lane", "studio-pulp-gate",
            "--runner", runner,
        ],
        input=json.dumps(attempt), text=True, capture_output=True, check=False,
    )


class DiskDenialReceiptTests(unittest.TestCase):
    def test_disk_denial_writes_bounded_typed_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "receipts"
            proc = observe(directory, {
                "ok": False,
                "reason": "disk_capacity_exceeded",
                "exceeded_axis": {"cores": False, "memory": False, "disk": True},
                "disk": {
                    "reservation_path": "/Volumes/Workshop/VMs",
                    "device_id": "16777235",
                    "free_bytes": 40,
                    "reserved_bytes": 10,
                    "available_after_reservations_bytes": 30,
                    "floor_bytes": 25,
                    "required_bytes": 59,
                    "requested_bytes": 24,
                },
            })
            self.assertEqual(proc.returncode, 0, proc.stderr)
            body = json.loads((directory / "studio-pulp-gate.disk-admission.json").read_text())
            self.assertEqual(body["status"], "denied")
            self.assertEqual(body["schema_version"], 1)
            self.assertEqual(body["kind"], "tartci.disk-admission")
            self.assertEqual(body["reason"], "disk_capacity_insufficient")
            self.assertTrue(body["host"])
            self.assertEqual(body["provider"], "tart-macos")
            self.assertEqual(body["lane"], "studio-pulp-gate")
            self.assertEqual(body["free_bytes"], 40)
            self.assertEqual(body["reserved_bytes"], 10)
            self.assertEqual(body["floor_bytes"], 25)
            self.assertEqual(body["probe_path"], "/Volumes/Workshop/VMs")
            self.assertEqual(body["device_id"], "16777235")
            self.assertEqual(body["requested_growth_bytes"], 24)
            self.assertEqual(body["required_after_reservations_bytes"], 49)
            self.assertLess(body["free_bytes"], body["required_bytes"])
            self.assertLess(body["available_after_reservations_bytes"], body["required_after_reservations_bytes"])

    def test_later_success_resolves_exact_runner_without_glob_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            other = directory / "other.disk-admission.json"
            other.write_text("keep\n", encoding="utf-8")
            self.assertEqual(observe(directory, {
                "ok": False, "reason": "disk_root_unavailable",
                "disk_path": "/Volumes/Workshop/VMs", "error": "missing",
            }).returncode, 0)
            denied = json.loads((directory / "studio-pulp-gate.disk-admission.json").read_text())
            self.assertEqual((denied["status"], denied["reason"]), ("denied", "disk_probe_failed"))
            self.assertEqual(denied["probe_path"], "/Volumes/Workshop/VMs")
            self.assertEqual(denied["error"], "missing")
            self.assertEqual(observe(directory, {"ok": True, "disk": {"reservation_path": "/tmp"}}).returncode, 0)
            body = json.loads((directory / "studio-pulp-gate.disk-admission.json").read_text())
            self.assertEqual(body["status"], "resolved")
            self.assertEqual(body["reason"], "lease_acquired")
            self.assertEqual(other.read_text(encoding="utf-8"), "keep\n")

    def test_non_disk_denial_resolves_stale_disk_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            observe(directory, {"ok": False, "reason": "disk_capacity_exceeded", "exceeded_axis": {"disk": True}, "disk": {}})
            proc = observe(directory, {"ok": False, "reason": "capacity_exceeded", "exceeded_axis": {"cores": True}})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            body = json.loads((directory / "studio-pulp-gate.disk-admission.json").read_text())
            self.assertEqual((body["status"], body["reason"]), ("resolved", "non_disk_denial"))

    def test_disk_axis_wins_when_another_axis_is_also_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            proc = observe(directory, {
                "ok": False, "reason": "capacity_exceeded",
                "exceeded_axis": {"cores": True, "memory": False, "disk": True},
                "disk": {"free_bytes": 10, "floor_bytes": 20, "required_bytes": 30},
            })
            self.assertEqual(proc.returncode, 0, proc.stderr)
            body = json.loads((directory / "studio-pulp-gate.disk-admission.json").read_text())
            self.assertEqual((body["status"], body["reason"]), ("denied", "disk_capacity_insufficient"))

    def test_severe_disk_integrity_denials_never_resolve_stale_receipt(self) -> None:
        reasons = (
            "disk_probe_failed",
            "disk_growth_misconfigured",
            "legacy_vm_disk_accounting_unknown",
            "disk_device_changed_with_live_reservations",
        )
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            for reason in reasons:
                with self.subTest(reason=reason):
                    proc = observe(directory, {"ok": False, "reason": reason})
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    body = json.loads((directory / "studio-pulp-gate.disk-admission.json").read_text())
                    self.assertEqual((body["status"], body["reason"]), ("denied", reason))

    def test_available_after_reservations_clamps_when_reserved_exceeds_free(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            proc = observe(directory, {
                "ok": False, "reason": "disk_capacity_exceeded",
                "exceeded_axis": {"cores": False, "memory": False, "disk": True},
                "disk": {
                    "device_id": "42", "reservation_path": "/tmp",
                    "free_bytes": 10, "reserved_bytes": 20,
                    "available_after_reservations_bytes": 0,
                    "floor_bytes": 25, "requested_bytes": 5,
                    "required_bytes": 50,
                },
            })
            self.assertEqual(proc.returncode, 0, proc.stderr)
            body = json.loads((directory / "studio-pulp-gate.disk-admission.json").read_text())
            self.assertEqual(body["available_after_reservations_bytes"], 0)
            self.assertEqual(body["required_after_reservations_bytes"], 30)
            self.assertEqual(body["required_bytes"], 50)
            self.assertLess(body["available_after_reservations_bytes"], body["required_after_reservations_bytes"])

    def test_unwritable_shape_fails_without_replacing_existing_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            not_directory = Path(td) / "not-a-directory"
            not_directory.write_text("authority\n", encoding="utf-8")
            proc = observe(not_directory, {"ok": False, "reason": "disk_capacity_exceeded", "exceeded_axis": {"disk": True}})
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(not_directory.read_text(encoding="utf-8"), "authority\n")

    def test_malformed_and_oversized_input_fail_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            command = ["python3", str(SCRIPT), "--receipt-dir", td, "--host", "studio", "--provider", "tart-macos", "--lane", "lane", "--runner", "runner"]
            malformed = subprocess.run(command, input="{bad", text=True, capture_output=True, check=False)
            oversized = subprocess.run(command, input="x" * (1024 * 1024 + 1), text=True, capture_output=True, check=False)
            self.assertNotEqual(malformed.returncode, 0)
            self.assertNotEqual(oversized.returncode, 0)

    def test_publication_deadline_is_bounded_to_safe_range(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            command = [
                "python3", str(SCRIPT), "--receipt-dir", td,
                "--host", "studio",
                "--provider", "tart-macos", "--lane", "lane", "--runner", "runner",
                "--timeout-seconds", "0",
            ]
            proc = subprocess.run(command, input="{}", text=True, capture_output=True, check=False)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("timeout must be greater than zero", proc.stderr)


if __name__ == "__main__":
    unittest.main()
