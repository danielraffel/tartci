#!/usr/bin/env python3
"""Behavioral tests for the tartci host lease store."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
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
            str(self.pid),
            "--kind",
            "test",
            "--owner",
            "unittest",
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
