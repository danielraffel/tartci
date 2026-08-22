#!/usr/bin/env python3
"""Behavioral tests for the tartci host lease store."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import leases


SCRIPT = Path(__file__).with_name("leases.py")


class LeaseCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "leases"
        self.pid = os.getpid()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args, "--store-dir", str(self.store), "--json"],
            text=True,
            capture_output=True,
            check=False,
        )
        if check and proc.returncode != 0:
            self.fail(f"{args} failed rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

    def acquire(
        self,
        lease_id: str,
        cores: int,
        *,
        priority: str = "build",
        capacity: int = 8,
        reserved: int = 0,
        mem_mb: int | None = None,
        capacity_mem_mb: int = 0,
        disk_path: Path | None = None,
        disk_growth_mb: int = 0,
        disk_floor_mb: int = 0,
        disk_expected_device_id: str = "",
        disk_expected_mount_path: str = "",
        kind: str = "test",
        pid: int | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        # Core-axis tests keep the memory axis OFF (capacity_mem_mb=0) so they
        # are deterministic regardless of the CI host's RAM; memory-axis tests
        # pass an explicit budget. Without this, a core capacity like `10` on a
        # small-RAM runner would be denied by the derived memory budget.
        extra: list[str] = ["--capacity-mem-mb", str(capacity_mem_mb)]
        if mem_mb is not None:
            extra += ["--mem-mb", str(mem_mb)]
        if disk_path is not None:
            extra += [
                "--disk-path",
                str(disk_path),
                "--disk-growth-mb",
                str(disk_growth_mb),
                "--disk-floor-mb",
                str(disk_floor_mb),
            ]
            if disk_expected_device_id:
                extra += ["--disk-expected-device-id", disk_expected_device_id]
            if disk_expected_mount_path:
                extra += ["--disk-expected-mount-path", disk_expected_mount_path]
        return self.run_cli(
            "acquire",
            "--id",
            lease_id,
            "--cores",
            str(cores),
            "--capacity",
            str(capacity),
            "--reserved-gate-cores",
            str(reserved),
            "--priority",
            priority,
            "--pid",
            str(self.pid if pid is None else pid),
            "--kind",
            kind,
            "--owner",
            "unittest",
            *extra,
            check=check,
        )


class LeaseAcquireReleaseTests(LeaseCliTestCase):
    def test_acquire_status_release(self) -> None:
        acquired = self.acquire("lease-a", 3)
        body = json.loads(acquired.stdout)
        self.assertTrue(body["ok"])
        self.assertEqual(body["lease"]["lease_size_cores"], 3)
        self.assertEqual(body["capacity"]["used_cores"], 3)

        status = json.loads(self.run_cli("status", "--capacity", "8").stdout)
        self.assertEqual([row["id"] for row in status["leases"]], ["lease-a"])

        released = json.loads(self.run_cli("release", "--id", "lease-a", "--capacity", "8").stdout)
        self.assertTrue(released["ok"])
        self.assertEqual(released["capacity"]["used_cores"], 0)

    def test_contention_fails_closed_when_capacity_is_exhausted(self) -> None:
        self.acquire("lease-a", 3, capacity=4)
        denied = self.acquire("lease-b", 2, capacity=4, check=False)
        self.assertEqual(denied.returncode, 75)
        body = json.loads(denied.stdout)
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "capacity_exceeded")

    def test_duplicate_ids_are_rejected(self) -> None:
        self.acquire("lease-a", 1)
        duplicate = self.acquire("lease-a", 1, check=False)
        self.assertNotEqual(duplicate.returncode, 0)
        body = json.loads(duplicate.stdout)
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "duplicate_lease_id")

    def test_heartbeat_updates_timestamp(self) -> None:
        self.acquire("lease-a", 1)
        before = json.loads(self.run_cli("status").stdout)["leases"][0]["heartbeat_at"]
        updated = json.loads(self.run_cli("heartbeat", "--id", "lease-a").stdout)
        self.assertTrue(updated["ok"])
        after = json.loads(self.run_cli("status").stdout)["leases"][0]["heartbeat_at"]
        self.assertGreaterEqual(after, before)


class LeasePriorityTests(LeaseCliTestCase):
    def test_reserved_gate_cores_are_unavailable_to_non_gate_leases(self) -> None:
        self.acquire("build", 6, capacity=10, reserved=4, priority="build")
        denied = self.acquire("extra-build", 1, capacity=10, reserved=4, priority="build", check=False)
        self.assertEqual(denied.returncode, 75)

        gate = self.acquire("gate", 4, capacity=10, reserved=4, priority="gate")
        body = json.loads(gate.stdout)
        self.assertTrue(body["ok"])
        self.assertEqual(body["capacity"]["used_cores"], 10)

    def test_gate_usage_still_blocks_total_overcommit(self) -> None:
        self.acquire("gate", 10, capacity=10, reserved=4, priority="gate")
        denied = self.acquire("build", 1, capacity=10, reserved=4, priority="build", check=False)
        self.assertEqual(denied.returncode, 75)
        body = json.loads(denied.stdout)
        self.assertEqual(body["reason"], "capacity_exceeded")

    def test_status_orders_by_priority(self) -> None:
        self.acquire("low", 1, priority="background")
        self.acquire("high", 1, priority="gate")
        status = json.loads(self.run_cli("status").stdout)
        self.assertEqual([row["id"] for row in status["leases"]], ["high", "low"])


class LeaseStoreIntegrityTests(LeaseCliTestCase):
    def test_corrupt_store_fails_closed(self) -> None:
        self.store.mkdir(parents=True)
        (self.store / "leases.json").write_text("{not-json", encoding="utf-8")

        denied = self.acquire("lease-a", 1, check=False)
        self.assertEqual(denied.returncode, 2)
        body = json.loads(denied.stdout)
        self.assertFalse(body["ok"])
        self.assertIn("invalid lease store JSON", body["error"])


class LeaseReclaimTests(unittest.TestCase):
    def test_vm_guardian_without_exact_start_identity_is_not_trusted(self) -> None:
        identity = leases.process_identity(os.getpid())
        record = {
            "id": "inexact-guardian",
            "pid": 99999999,
            "process_start_time": "Mon Jan  1 00:00:00 2001",
            "host_boot_time": leases.host_boot_time(),
            "command_kind": "qemu-windows-vm",
            "guardian_pid": os.getpid(),
            "guardian_process_start_time": "",
            "guardian_host_boot_time": identity["host_boot_time"],
        }
        self.assertFalse(leases.owner_matches(record))

    def test_reclaims_dead_owner_even_with_fresh_heartbeat(self) -> None:
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=5)
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "leases"
            store.mkdir()
            now = leases.iso(leases.utcnow())
            record = {
                "id": "dead-owner",
                "lease_size_cores": 1,
                "priority": 40,
                "priority_class": "build",
                "pid": exited.pid,
                "process_start_time": "Mon Jan  1 00:00:00 2001",
                "host_boot_time": leases.host_boot_time(),
                "process_group_id": None,
                "session_id": None,
                "command_kind": "test",
                "owner": "unittest",
                "created_at": now,
                "heartbeat_at": now,
            }
            leases.write_records(store, [record])
            args = argparse.Namespace(
                store_dir=str(store),
                capacity=4,
                reserved_gate_cores=0,
                gate_priority=100,
                stale_secs=300,
                role=None,
                role_file=None,
                host_cores=None,
                model=None,
            )
            digest = leases.status_digest(args)
        self.assertEqual(digest["leases"], [])
        self.assertEqual(digest["reaped"][0]["id"], "dead-owner")
        self.assertEqual(digest["reaped"][0]["reason"], "identity_mismatch")

    def test_reclaims_same_pid_with_wrong_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "leases"
            store.mkdir()
            record = {
                "id": "reused-pid",
                "lease_size_cores": 1,
                "priority": 40,
                "priority_class": "build",
                "pid": os.getpid(),
                "process_start_time": "Mon Jan  1 00:00:00 2001",
                "host_boot_time": leases.host_boot_time(),
                "process_group_id": os.getpgid(os.getpid()),
                "session_id": os.getsid(os.getpid()),
                "command_kind": "test",
                "owner": "unittest",
                "created_at": leases.iso(leases.utcnow()),
                "heartbeat_at": leases.iso(leases.utcnow()),
            }
            leases.write_records(store, [record])
            args = argparse.Namespace(
                store_dir=str(store),
                capacity=4,
                reserved_gate_cores=0,
                gate_priority=100,
                stale_secs=300,
                role=None,
                role_file=None,
                host_cores=None,
                model=None,
            )
            digest = leases.status_digest(args)
        self.assertEqual(digest["leases"], [])
        self.assertEqual(digest["reaped"][0]["id"], "reused-pid")
        self.assertEqual(digest["reaped"][0]["reason"], "identity_mismatch")

    def test_stale_heartbeat_with_live_owner_is_reported_not_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "leases"
            store.mkdir()
            identity = leases.process_identity(os.getpid())
            old = "2001-01-01T00:00:00Z"
            record = {
                "id": "live-but-stale",
                "lease_size_cores": 1,
                "priority": 40,
                "priority_class": "build",
                **identity,
                "command_kind": "test",
                "owner": "unittest",
                "created_at": old,
                "heartbeat_at": old,
            }
            leases.write_records(store, [record])
            args = argparse.Namespace(
                store_dir=str(store),
                capacity=4,
                reserved_gate_cores=0,
                gate_priority=100,
                stale_secs=1,
                role=None,
                role_file=None,
                host_cores=None,
                model=None,
            )
            digest = leases.status_digest(args)
        self.assertEqual([row["id"] for row in digest["leases"]], ["live-but-stale"])
        self.assertEqual(digest["reaped"], [])
        self.assertEqual(digest["problems"], ["stale_heartbeat_live_owner:live-but-stale"])
        self.assertFalse(any(key.startswith("_") for key in digest["leases"][0]))


class LeaseMemoryAxisTests(LeaseCliTestCase):
    def test_build_lease_without_mem_charges_a_core_derived_estimate(self) -> None:
        body = json.loads(
            self.acquire("a", 2, capacity=20, capacity_mem_mb=8192).stdout
        )
        self.assertTrue(body["ok"])
        # 2 cores * 1536 MB/job = 3072 MB charged even without an explicit --mem-mb.
        self.assertEqual(body["lease"]["lease_size_mem_mb"], 3072)
        self.assertEqual(body["capacity"]["used_mem_mb"], 3072)
        self.assertEqual(body["capacity"]["available_mem_mb"], 8192 - 3072)

    def test_memory_axis_denies_when_mem_exhausted_though_cores_fit(self) -> None:
        self.acquire("a", 1, capacity=20, capacity_mem_mb=8192, mem_mb=6000)
        denied = self.acquire(
            "b", 1, capacity=20, capacity_mem_mb=8192, mem_mb=4000, check=False
        )
        self.assertEqual(denied.returncode, 75)
        body = json.loads(denied.stdout)
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "memory_exceeded")
        self.assertTrue(body["exceeded_axis"]["memory"])
        self.assertFalse(body["exceeded_axis"]["cores"])  # 2 cores of 20 — cores fit

    def test_legacy_core_only_record_memory_is_estimated_not_free(self) -> None:
        self.acquire("real", 1, capacity=20, capacity_mem_mb=8192, mem_mb=1000)
        store_file = self.store / "leases.json"
        records = json.loads(store_file.read_text())
        # A pre-memory-axis record: clone the real one's identity + timestamps
        # (so reclaim keeps it) but drop lease_size_mem_mb — the only field an
        # old tartci did not write.
        legacy = dict(records[0])
        legacy["id"] = "legacy"
        legacy["lease_size_cores"] = 3
        legacy.pop("lease_size_mem_mb", None)
        records.append(legacy)
        store_file.write_text(json.dumps(records))
        digest = json.loads(
            self.run_cli("status", "--capacity", "20", "--capacity-mem-mb", "8192").stdout
        )
        cap = digest["capacity"]
        # explicit 1000 + legacy estimate (3 * 1536 = 4608) = 5608, NOT 1000.
        self.assertEqual(cap["used_mem_mb"], 1000 + 4608)
        self.assertEqual(cap["memory_accounting"], "estimated_legacy")

    def test_memory_axis_is_off_when_capacity_mem_is_zero(self) -> None:
        body = json.loads(
            self.acquire("a", 1, capacity=20, capacity_mem_mb=0, mem_mb=999999).stdout
        )
        self.assertTrue(body["ok"])  # axis off → an absurd mem request is still granted
        self.assertNotIn("used_mem_mb", body["capacity"])


class LeaseDiskAxisTests(LeaseCliTestCase):
    def test_vm_acquire_requires_explicit_disk_accounting(self) -> None:
        denied = self.acquire(
            "diskless-vm",
            1,
            kind="tart-linux-vm",
            check=False,
        )
        self.assertEqual(denied.returncode, 75)
        body = json.loads(denied.stdout)
        self.assertEqual(body["reason"], "vm_disk_path_required")
        self.assertFalse((self.store / "leases.json").exists())

    def test_live_legacy_diskless_vm_blocks_only_new_vm_admission(self) -> None:
        self.acquire("legacy-source", 1)
        store_file = self.store / "leases.json"
        records = json.loads(store_file.read_text(encoding="utf-8"))
        records[0]["command_kind"] = "tart-linux-vm"
        records[0].pop("disk_device_id", None)
        records[0].pop("disk_reservation_path", None)
        records[0].pop("disk_growth_bytes", None)
        records[0].pop("disk_floor_bytes", None)
        store_file.write_text(json.dumps(records), encoding="utf-8")

        # Native work remains backward-compatible while the old VM drains.
        self.assertEqual(self.acquire("native-build", 1).returncode, 0)
        denied = self.acquire(
            "new-vm",
            1,
            kind="tart-macos-vm",
            disk_path=Path(self.tmp.name),
            check=False,
        )
        self.assertEqual(denied.returncode, 75)
        body = json.loads(denied.stdout)
        self.assertEqual(body["reason"], "legacy_vm_disk_accounting_unknown")
        self.assertEqual(body["legacy_vm_lease_ids"], ["legacy-source"])

        status = self.run_cli("status", check=False)
        self.assertEqual(status.returncode, 1)
        self.assertIn(
            "legacy_vm_disk_accounting_unknown:legacy-source",
            json.loads(status.stdout)["problems"],
        )

    def test_missing_disk_root_is_typed_and_never_created(self) -> None:
        missing = Path(self.tmp.name) / "offline-volume" / "vm-store"
        denied = self.acquire(
            "missing-root",
            1,
            kind="qemu-windows-vm",
            disk_path=missing,
            check=False,
        )
        self.assertEqual(denied.returncode, 75)
        body = json.loads(denied.stdout)
        self.assertEqual(body["reason"], "disk_root_unavailable")
        self.assertIn("does not exist", body["error"])
        self.assertFalse(missing.exists())

        loop_a = Path(self.tmp.name) / "loop-a"
        loop_b = Path(self.tmp.name) / "loop-b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        cyclic = self.acquire(
            "cyclic-root",
            1,
            kind="tart-linux-vm",
            disk_path=loop_a,
            check=False,
        )
        self.assertEqual(cyclic.returncode, 75)
        self.assertEqual(json.loads(cyclic.stdout)["reason"], "disk_root_unavailable")

    def test_expected_device_and_mount_identity_fail_closed(self) -> None:
        probe = leases.disk_probe(self.tmp.name)
        wrong_device = self.acquire(
            "wrong-device",
            1,
            kind="tart-macos-vm",
            disk_path=Path(self.tmp.name),
            disk_expected_device_id=f"{probe['device_id']}-wrong",
            check=False,
        )
        self.assertEqual(wrong_device.returncode, 75)
        self.assertEqual(json.loads(wrong_device.stdout)["reason"], "disk_root_unavailable")

        wrong_mount = Path(self.tmp.name) / "not-the-mount"
        wrong_mount.mkdir()
        denied_mount = self.acquire(
            "wrong-mount",
            1,
            kind="tart-macos-vm",
            disk_path=Path(self.tmp.name),
            disk_expected_mount_path=str(wrong_mount),
            check=False,
        )
        self.assertEqual(denied_mount.returncode, 75)
        self.assertEqual(
            json.loads(denied_mount.stdout)["reason"],
            "disk_root_unavailable",
        )

    def test_path_aliases_share_one_filesystem_reservation(self) -> None:
        real_store = Path(self.tmp.name) / "real-store"
        alias_store = Path(self.tmp.name) / "alias-store"
        real_store.mkdir()
        alias_store.symlink_to(real_store, target_is_directory=True)
        self.acquire(
            "real",
            1,
            kind="tart-macos-vm",
            disk_path=real_store,
            disk_growth_mb=1,
        )
        self.acquire(
            "alias",
            1,
            kind="tart-linux-vm",
            disk_path=alias_store,
            disk_growth_mb=2,
        )
        status = json.loads(self.run_cli("status").stdout)
        self.assertEqual(len(status["disk_volumes"]), 1)
        self.assertEqual(status["disk_volumes"][0]["reservation_count"], 2)
        self.assertEqual(status["disk_volumes"][0]["reserved_bytes"], 3 * 1024 * 1024)

    def test_guardian_keeps_vm_lease_after_supervisor_dies(self) -> None:
        supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        guardian: subprocess.Popen[str] | None = None
        try:
            self.acquire(
                "kept-failed-vm",
                1,
                kind="qemu-windows-vm",
                pid=supervisor.pid,
                disk_path=Path(self.tmp.name),
            )
            guardian = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "guard-exec",
                    "--id",
                    "kept-failed-vm",
                    "--store-dir",
                    str(self.store),
                    "--",
                    "/bin/sleep",
                    "60",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                records = leases.load_records(self.store)
                if records and records[0].get("guardian_pid") == guardian.pid:
                    break
                if guardian.poll() is not None:
                    stdout, stderr = guardian.communicate()
                    self.fail(
                        f"guardian exited before handoff rc={guardian.returncode} "
                        f"stdout={stdout} stderr={stderr}"
                    )
                time.sleep(0.05)
            else:
                self.fail("guardian identity was not committed before timeout")

            supervisor.terminate()
            supervisor.wait(timeout=5)
            held = json.loads(self.run_cli("status").stdout)
            self.assertEqual([row["id"] for row in held["leases"]], ["kept-failed-vm"])
            self.assertEqual(held["leases"][0]["guardian_pid"], guardian.pid)

            guardian.terminate()
            guardian.communicate(timeout=5)
            released = json.loads(self.run_cli("status").stdout)
            self.assertEqual(released["leases"], [])
            self.assertEqual(released["reaped"][0]["id"], "kept-failed-vm")
        finally:
            if supervisor.poll() is None:
                supervisor.terminate()
                supervisor.wait(timeout=5)
            if guardian is not None and guardian.poll() is None:
                guardian.terminate()
            if guardian is not None:
                guardian.communicate(timeout=5)

    def test_status_disk_snapshot_holds_lock_against_release(self) -> None:
        self.acquire(
            "snapshot",
            1,
            disk_path=Path(self.tmp.name),
            disk_growth_mb=1,
        )
        original_probe = leases.disk_probe
        release_proc: subprocess.Popen[str] | None = None
        release_attempted = Path(self.tmp.name) / "release-attempted"

        def probe_while_release_waits(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal release_proc
            release_code = """
import json
import pathlib
import sys

sys.path.insert(0, sys.argv[1])
import leases

pathlib.Path(sys.argv[3]).write_text("attempted", encoding="utf-8")
parsed = leases.parse_args(
    ["release", "--id", "snapshot", "--store-dir", sys.argv[2], "--json"]
)
result, rc = leases.release(parsed)
print(json.dumps(result))
raise SystemExit(rc)
"""
            release_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    release_code,
                    str(SCRIPT.parent),
                    str(self.store),
                    str(release_attempted),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while not release_attempted.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(release_attempted.exists(), "release process did not start")
            time.sleep(0.1)
            self.assertIsNone(release_proc.poll(), "release escaped the status transaction lock")
            return original_probe(*args, **kwargs)

        args = leases.parse_args(["status", "--store-dir", str(self.store), "--json"])
        with mock.patch.object(leases, "disk_probe", side_effect=probe_while_release_waits):
            digest = leases.status_digest(args)
        self.assertEqual([row["id"] for row in digest["leases"]], ["snapshot"])
        self.assertEqual(digest["disk_volumes"][0]["reserved_bytes"], 1024 * 1024)
        self.assertIsNotNone(release_proc)
        stdout, stderr = release_proc.communicate(timeout=5)
        self.assertEqual(release_proc.returncode, 0, f"stdout={stdout} stderr={stderr}")

    def test_disk_denial_reports_free_reserved_and_required(self) -> None:
        free_mb = leases.disk_probe(self.tmp.name)["free_bytes"] // (1024 * 1024)
        floor_mb = max(0, free_mb - 128)
        self.acquire(
            "a",
            1,
            disk_path=Path(self.tmp.name),
            disk_growth_mb=96,
            disk_floor_mb=floor_mb,
        )
        denied = self.acquire(
            "b",
            1,
            disk_path=Path(self.tmp.name),
            disk_growth_mb=96,
            disk_floor_mb=floor_mb,
            check=False,
        )
        self.assertEqual(denied.returncode, 75)
        body = json.loads(denied.stdout)
        self.assertEqual(body["reason"], "disk_capacity_exceeded")
        self.assertTrue(body["exceeded_axis"]["disk"])
        self.assertEqual(body["disk"]["reserved_bytes"], 96 * 1024 * 1024)
        self.assertEqual(body["disk"]["requested_bytes"], 96 * 1024 * 1024)
        self.assertGreater(body["disk"]["required_bytes"], body["disk"]["free_bytes"])

    def test_success_reports_preexisting_reserved_and_new_requested(self) -> None:
        first = json.loads(
            self.acquire(
                "first",
                1,
                disk_path=Path(self.tmp.name),
                disk_growth_mb=1,
            ).stdout
        )
        self.assertEqual(first["disk"]["reserved_bytes"], 0)
        self.assertEqual(first["disk"]["requested_bytes"], 1024 * 1024)
        second = json.loads(
            self.acquire(
                "second",
                1,
                disk_path=Path(self.tmp.name),
                disk_growth_mb=2,
            ).stdout
        )
        self.assertEqual(second["disk"]["reserved_bytes"], 1024 * 1024)
        self.assertEqual(second["disk"]["requested_bytes"], 2 * 1024 * 1024)

    def test_concurrent_acquire_cannot_overcommit_one_volume(self) -> None:
        free_mb = leases.disk_probe(self.tmp.name)["free_bytes"] // (1024 * 1024)
        floor_mb = max(0, free_mb - 256)
        common = [
            sys.executable,
            str(SCRIPT),
            "acquire",
            "--cores",
            "1",
            "--capacity",
            "8",
            "--capacity-mem-mb",
            "0",
            "--reserved-gate-cores",
            "0",
            "--pid",
            str(self.pid),
            "--disk-path",
            self.tmp.name,
            "--disk-growth-mb",
            "160",
            "--disk-floor-mb",
            str(floor_mb),
            "--store-dir",
            str(self.store),
            "--json",
        ]
        procs = [
            subprocess.Popen([*common, "--id", lease_id], text=True, stdout=subprocess.PIPE)
            for lease_id in ("concurrent-a", "concurrent-b")
        ]
        results = []
        for proc in procs:
            stdout, _ = proc.communicate(timeout=10)
            results.append((proc.returncode, json.loads(stdout)))
        self.assertEqual(sorted(rc for rc, _ in results), [0, 75])
        status = json.loads(self.run_cli("status").stdout)
        self.assertEqual(len(status["leases"]), 1)
        self.assertEqual(status["disk_volumes"][0]["reserved_bytes"], 160 * 1024 * 1024)

    def test_release_removes_disk_reservation(self) -> None:
        self.acquire(
            "disk-release",
            1,
            disk_path=Path(self.tmp.name),
            disk_growth_mb=1,
        )
        released = json.loads(self.run_cli("release", "--id", "disk-release").stdout)
        self.assertTrue(released["ok"])
        status = json.loads(self.run_cli("status").stdout)
        self.assertEqual(status["leases"], [])
        self.assertEqual(status["disk_volumes"], [])

    def test_distinct_devices_do_not_share_reservations(self) -> None:
        records = [{"disk_device_id": "dev-a", "disk_growth_bytes": 90}]
        probe = {
            "device_id": "dev-b",
            "mount_path": "/b",
            "reservation_path": "/b/store",
            "free_bytes": 100,
        }
        state = leases.disk_capacity(records, probe, requested_bytes=20, floor_bytes=30)
        self.assertEqual(state["reserved_bytes"], 0)
        self.assertEqual(state["required_bytes"], 50)

    def test_dead_owner_reap_releases_disk_reservation(self) -> None:
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=5)
        now = leases.iso(leases.utcnow())
        probe = leases.disk_probe(self.tmp.name)
        self.store.mkdir()
        leases.write_records(
            self.store,
            [
                {
                    "id": "dead-disk-owner",
                    "lease_size_cores": 1,
                    "priority": 40,
                    "pid": exited.pid,
                    "process_start_time": "Mon Jan  1 00:00:00 2001",
                    "host_boot_time": leases.host_boot_time(),
                    "disk_device_id": probe["device_id"],
                    "disk_reservation_path": self.tmp.name,
                    "disk_growth_bytes": 1024,
                    "disk_floor_bytes": 0,
                    "created_at": now,
                    "heartbeat_at": now,
                }
            ],
        )
        status = json.loads(self.run_cli("reap").stdout)
        self.assertEqual(status["leases"], [])
        self.assertEqual(status["disk_volumes"], [])
        self.assertEqual(status["reaped"][0]["id"], "dead-disk-owner")


class LeaseMinimalPathTests(unittest.TestCase):
    """Lease probes must survive a launchd PATH that omits /usr/sbin."""

    def test_host_boot_time_survives_path_without_usr_sbin(self) -> None:
        old_path = os.environ.get("PATH")
        os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        try:
            boot = leases.host_boot_time()
        finally:
            if old_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old_path
        self.assertTrue(boot)
        self.assertNotEqual(boot, "unknown")

    def test_run_reports_missing_binary_instead_of_raising(self) -> None:
        proc = leases.run(["tartci-no-such-binary-exists"])
        self.assertEqual(proc.returncode, 127)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
