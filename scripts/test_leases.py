#!/usr/bin/env python3
"""Behavioral tests for the tartci host lease store."""

from __future__ import annotations

import testing_support  # noqa: E402
import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import leases
import lease_disk


SCRIPT = Path(__file__).with_name("leases.py")


class LeaseCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "leases"
        self.pid = os.getpid()
        # Isolate the CLI from the host's installed fleet profile: a fleet host
        # sets [host] agent_floor_qos and floor knobs that change the answers.
        profile = Path(self.tmp.name) / "fleet.toml"
        profile.write_text('schema = 1\n[host]\nid = "test"\n', encoding="utf-8")
        self.cli_env = {k: v for k, v in os.environ.items()
                        if not k.startswith(("TARTCI_AGENT_FLOOR", "TARTCI_RANK_VM_WAITERS",
                                             "TARTCI_FAIR_SHARE"))}
        self.cli_env["TARTCI_FLEET_PROFILE"] = str(profile)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args, "--store-dir", str(self.store), "--json"],
            text=True,
            capture_output=True,
            check=False,
            env=self.cli_env,
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
        reserved_mem_mb: int = 0,
        disk_path: Path | None = None,
        disk_growth_mb: int = 0,
        disk_floor_mb: int = 0,
        disk_expected_device_id: str = "",
        disk_expected_mount_path: str = "",
        kind: str = "test",
        pid: int | None = None,
        check: bool = True,
        extra_args: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        # Core-axis tests keep the memory axis OFF (capacity_mem_mb=0) so they
        # are deterministic regardless of the CI host's RAM; memory-axis tests
        # pass an explicit budget. Without this, a core capacity like `10` on a
        # small-RAM runner would be denied by the derived memory budget.
        extra: list[str] = ["--capacity-mem-mb", str(capacity_mem_mb), *extra_args]
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
            "--reserved-gate-mem-mb",
            str(reserved_mem_mb),
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

    def test_releasing_a_lease_whose_vm_guardian_exited_is_not_an_error(self) -> None:
        # Teardown stops the VM, its guardian (the exec'd `tart run`) exits,
        # and the next store operation reclaims the record before the
        # supervisor's own release arrives. That was rc=1 on every release.
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=5)
        identity = leases.process_identity(self.pid)
        self.store.mkdir(parents=True, exist_ok=True)
        now = leases.iso(leases.utcnow())
        (self.store / "leases.json").write_text(json.dumps([{
            "id": "vm-tart-macos-vm-lane-01-123-4", "lease_size_cores": 3, "priority": 110,
            "priority_class": "gate", "command_kind": "tart-macos-vm", "owner": "unittest",
            "pid": self.pid, "process_start_time": identity["process_start_time"],
            "host_boot_time": identity["host_boot_time"], "created_at": now, "heartbeat_at": now,
            "guardian_pid": exited.pid, "guardian_mode": "exec",
            "guardian_process_start_time": "Mon Jan  1 00:00:00 2001",
            "guardian_host_boot_time": identity["host_boot_time"]}]))
        released = self.run_cli("release", "--id", "vm-tart-macos-vm-lane-01-123-4",
                                "--capacity", "8")
        body = json.loads(released.stdout)
        self.assertEqual((body["ok"], body["already_reclaimed"]), (True, "identity_mismatch"))
        # The control: an id the store never held is still an error.
        unknown = self.run_cli("release", "--id", "never-acquired", "--capacity", "8", check=False)
        self.assertEqual(unknown.returncode, 1)
        self.assertFalse(json.loads(unknown.stdout)["ok"])

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


class AgentFloorTests(LeaseCliTestCase):
    """A starved build gets a background floor lease that no one else pays for."""

    def floor_acquire(self, lease_id: str, cores: int, *, floor: int = 6, pool: int = 6,
                      allow: bool = True, priority: str = "build", kind: str = "test",
                      capacity: int = 26, reserved: int = 14, capacity_mem_mb: int = 0,
                      reserved_mem_mb: int = 0, mem_mb: int | None = None,
                      qos: str | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
        extra = ["--capacity-mem-mb", str(capacity_mem_mb),
                 "--reserved-gate-mem-mb", str(reserved_mem_mb),
                 "--agent-floor-cores", str(floor), "--agent-floor-pool-cores", str(pool)]
        if mem_mb is not None:
            extra += ["--mem-mb", str(mem_mb)]
        if qos is not None:
            extra += ["--agent-floor-qos", qos]
        if allow:
            extra.append("--allow-floor")
        return self.run_cli(
            "acquire", "--id", lease_id, "--cores", str(cores),
            "--capacity", str(capacity), "--reserved-gate-cores", str(reserved),
            "--priority", priority, "--pid", str(self.pid), "--kind", kind,
            *extra, check=check,
        )

    def fill_non_gate(self) -> None:
        # The live m3 shape on 2026-09-24: one 12-core governed build holds the
        # whole non-gate budget while the 14-core gate reserve is idle.
        self.acquire("agent-1", 12, capacity=26, reserved=14)

    def test_default_is_todays_denial(self) -> None:
        self.fill_non_gate()
        denied = self.floor_acquire("agent-2", 12, floor=0, pool=0)
        self.assertEqual(denied.returncode, 75)
        self.assertEqual(json.loads(denied.stdout)["reason"], "capacity_exceeded")

    def test_callers_that_do_not_opt_in_are_denied_as_before(self) -> None:
        self.fill_non_gate()
        denied = self.floor_acquire("agent-2", 12, allow=False)
        self.assertEqual(denied.returncode, 75)

    def test_starved_build_gets_background_floor_lease(self) -> None:
        self.fill_non_gate()
        granted = self.floor_acquire("agent-2", 12)
        self.assertEqual(granted.returncode, 0, granted.stdout + granted.stderr)
        body = json.loads(granted.stdout)
        self.assertTrue(body["floor"])
        self.assertEqual(body["qos"], "background")
        self.assertEqual(body["lease"]["lease_size_cores"], 6)
        self.assertEqual(body["lease"]["requested_cores"], 12)
        self.assertEqual(body["capacity"]["floor_used_cores"], 6)

    def test_floor_lease_carries_the_configured_qos(self) -> None:
        self.fill_non_gate()
        granted = self.floor_acquire("agent-2", 12, qos="utility")
        self.assertEqual(granted.returncode, 0, granted.stdout + granted.stderr)
        body = json.loads(granted.stdout)
        self.assertEqual(body["qos"], "utility")
        self.assertEqual(body["lease"]["qos"], "utility")

    def test_floor_does_not_take_gate_capacity(self) -> None:
        self.fill_non_gate()
        self.assertEqual(self.floor_acquire("agent-2", 12).returncode, 0)
        # The whole 14-core gate reserve is still admissible, exactly as if the
        # floor lease did not exist.
        gate = self.acquire("gate", 14, capacity=26, reserved=14, priority="gate")
        body = json.loads(gate.stdout)
        self.assertTrue(body["ok"])
        self.assertEqual(body["capacity"]["used_cores"], 26)
        self.assertEqual(body["capacity"]["floor_used_cores"], 6)

    def test_floor_does_not_delay_non_gate_vm_or_build_leases(self) -> None:
        self.fill_non_gate()
        floor = json.loads(self.floor_acquire("agent-2", 12).stdout)
        self.assertTrue(floor["floor"])
        self.run_cli("release", "--id", "agent-1", "--capacity", "26")
        # With the floor lease still live, the full non-gate budget is back for
        # a VM, and the full gate reserve for the gate.
        vm = self.acquire("vm", 12, capacity=26, reserved=14, priority="vm")
        self.assertTrue(json.loads(vm.stdout)["ok"])
        gate = self.acquire("gate", 14, capacity=26, reserved=14, priority="gate")
        self.assertTrue(json.loads(gate.stdout)["ok"])

    def test_pool_caps_concurrent_floor_leases(self) -> None:
        self.fill_non_gate()
        self.assertEqual(self.floor_acquire("agent-2", 12, pool=8).returncode, 0)
        second = json.loads(self.floor_acquire("agent-3", 12, pool=8).stdout)
        self.assertEqual(second["lease"]["lease_size_cores"], 2)
        third = self.floor_acquire("agent-4", 12, pool=8)
        self.assertEqual(third.returncode, 75)

    def test_gate_and_vm_requests_never_take_a_floor(self) -> None:
        self.acquire("gate", 26, capacity=26, reserved=14, priority="gate")
        gate = self.floor_acquire("gate-2", 4, priority="gate")
        self.assertEqual(gate.returncode, 75)
        vm = self.floor_acquire("vm-2", 4, priority="vm", kind="tart-macos-vm")
        self.assertEqual(vm.returncode, 75)

    def test_floor_never_spends_the_gate_memory_reserve(self) -> None:
        # Cores are oversubscribable under background QoS; memory is not.
        self.acquire("agent-1", 12, capacity=26, reserved=14,
                     capacity_mem_mb=40000, reserved_mem_mb=20000, mem_mb=18432)
        denied = self.floor_acquire("agent-2", 12, capacity_mem_mb=40000,
                                    reserved_mem_mb=20000)
        self.assertEqual(denied.returncode, 75)
        granted = self.floor_acquire("agent-3", 12, capacity_mem_mb=40000,
                                     reserved_mem_mb=20000, mem_mb=2048)
        self.assertEqual(granted.returncode, 0, granted.stdout)

    def test_status_reports_floor_pool(self) -> None:
        self.fill_non_gate()
        self.floor_acquire("agent-2", 12)
        status = json.loads(self.run_cli(
            "status", "--capacity", "26", "--reserved-gate-cores", "14",
            "--agent-floor-cores", "6", "--agent-floor-pool-cores", "6",
            "--capacity-mem-mb", "0").stdout)
        cap = status["capacity"]
        self.assertEqual(cap["used_cores"], 12)
        self.assertEqual(cap["non_gate_available_cores"], 0)
        self.assertEqual(cap["floor_available_cores"], 0)


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

    def test_m3_two_twelve_core_pulp_event_guests_fit(self) -> None:
        self.acquire("merge-group", 12, capacity=26, reserved=14, priority="110")
        second = self.acquire(
            "pr-head", 12, capacity=26, reserved=14, priority="100"
        )
        body = json.loads(second.stdout)
        self.assertTrue(body["ok"])
        self.assertEqual(body["capacity"]["used_cores"], 24)

    def test_m3_twelve_core_pulp_and_fourteen_core_gate_fit(self) -> None:
        self.acquire("pulp", 12, capacity=26, reserved=14, priority="100")
        second = self.acquire(
            "other-gate", 14, capacity=26, reserved=14, priority="gate"
        )
        body = json.loads(second.stdout)
        self.assertTrue(body["ok"])
        self.assertEqual(body["capacity"]["used_cores"], 26)

    def test_m3_two_default_fourteen_core_guests_do_not_fit(self) -> None:
        self.acquire("first", 14, capacity=26, reserved=14, priority="gate")
        denied = self.acquire(
            "second", 14, capacity=26, reserved=14, priority="gate", check=False
        )
        self.assertEqual(denied.returncode, 75)
        self.assertEqual(json.loads(denied.stdout)["reason"], "capacity_exceeded")

    def test_m1_two_three_core_pulp_event_guests_use_reserved_capacity(self) -> None:
        self.acquire("merge-group", 3, capacity=6, reserved=3, priority="110")
        second = self.acquire(
            "pr-head", 3, capacity=6, reserved=3, priority="100"
        )
        body = json.loads(second.stdout)
        self.assertTrue(body["ok"])
        self.assertEqual(body["capacity"]["used_cores"], 6)

    def test_m1_vm_priority_override_cannot_use_reserved_capacity(self) -> None:
        self.acquire("first", 3, capacity=6, reserved=3, priority="vm")
        denied = self.acquire(
            "second", 3, capacity=6, reserved=3, priority="vm", check=False
        )
        self.assertEqual(denied.returncode, 75)
        self.assertEqual(json.loads(denied.stdout)["reason"], "capacity_exceeded")



class VmSlotLendingTests(LeaseCliTestCase):
    """A non-gate macOS guest that takes the host's LAST guest slot may use the
    gate reserve, because no gate guest can start while every slot is held.

    Sized like m5 (T=14, S=8, N=6) and m5studio's Vellum lane (6-core guests).
    """

    T, S = 14, 8
    LEND = ("--vm-slot-lending", "on", "--macos-vm-cap", "2")

    def guest(self, lease_id: str, cores: int, *, priority: str = "vm",
              kind: str = "tart-macos-vm", lend: bool = True, mem_mb: int | None = None,
              capacity_mem_mb: int = 0, reserved_mem_mb: int = 0, check: bool = False):
        proc = self.acquire(
            lease_id, cores, priority=priority, capacity=self.T, reserved=self.S,
            kind=kind, disk_path=Path(self.tmp.name), mem_mb=mem_mb,
            capacity_mem_mb=capacity_mem_mb, reserved_mem_mb=reserved_mem_mb,
            extra_args=self.LEND if lend else ("--vm-slot-lending", "off"), check=False)
        if check and proc.returncode != 0:
            self.fail(f"{lease_id} denied: {proc.stdout}")
        return proc, json.loads(proc.stdout)

    def test_second_non_gate_guest_is_denied_without_lending(self) -> None:
        self.guest("vellum-1", 6, lend=False, check=True)
        proc, body = self.guest("vellum-2", 6, lend=False)
        self.assertEqual(proc.returncode, 75)
        self.assertEqual(body["reason"], "capacity_exceeded")
        self.assertEqual(body["core_limit_class"], "non_gate")

    def test_second_non_gate_guest_takes_the_last_slot_with_lending(self) -> None:
        self.guest("vellum-1", 6, check=True)
        proc, body = self.guest("vellum-2", 6)
        self.assertEqual(proc.returncode, 0, body)
        self.assertTrue(body["lease"]["vm_slot_lending"])
        self.assertEqual(body["capacity"]["used_cores"], 12)

    def test_a_parked_guest_upgraded_into_the_last_slot_is_lent_like_an_acquire(self) -> None:
        self.guest("vellum-1", 6, check=True)
        parked = self.acquire("warm", 0, priority="vm", capacity=self.T, reserved=self.S,
                              kind="tart-macos-vm", disk_path=Path(self.tmp.name), mem_mb=4096,
                              extra_args=("--memory-only", *self.LEND), check=False)
        self.assertEqual(parked.returncode, 0, parked.stdout)
        for lend, expected in ((("--vm-slot-lending", "off"), 75), (self.LEND, 0)):
            proc = self.run_cli("resize", "--id", "warm", "--cores", "6", "--mem-mb", "4096",
                                "--capacity", str(self.T), "--reserved-gate-cores", str(self.S),
                                "--capacity-mem-mb", "0", *lend, check=False)
            self.assertEqual(proc.returncode, expected, proc.stdout)

    def test_a_lease_inside_n_is_not_tagged_as_lent(self) -> None:
        self.guest("vellum-1", 3, check=True)
        _, body = self.guest("vellum-2", 3, check=True)
        self.assertNotIn("vm_slot_lending", body["lease"])

    def test_a_gate_build_arriving_while_lending_is_open_finds_t_minus_the_guests(self) -> None:
        # Documented cost of lending (leases.py, "VM slot lending"): pinned so a
        # change to it is a decision, not an accident.
        self.guest("vellum-1", 6, check=True)
        self.guest("vellum-2", 6, check=True)
        denied = self.acquire("gate-build", 4, priority="gate", capacity=self.T,
                              reserved=self.S, extra_args=self.LEND, check=False)
        self.assertEqual(denied.returncode, 75)
        fits = self.acquire("gate-build-2", 2, priority="gate", capacity=self.T,
                            reserved=self.S, extra_args=self.LEND, check=False)
        self.assertEqual(fits.returncode, 0)

    def test_first_guest_stays_in_the_non_gate_budget(self) -> None:
        # One slot is still open for a gate guest of any size, so nothing of S
        # is lent: a 7-core non-gate guest is over N=6 and is refused.
        proc, body = self.guest("vellum-1", 7)
        self.assertEqual(proc.returncode, 75)
        self.assertEqual(body["core_limit_class"], "non_gate")

    def test_lending_never_exceeds_the_host_total(self) -> None:
        self.guest("vellum-1", 6, check=True)
        proc, body = self.guest("vellum-2", 9)
        self.assertEqual(proc.returncode, 75)
        self.assertEqual(body["reason"], "capacity_exceeded")

    def test_a_gate_guest_in_a_slot_keeps_lending_off(self) -> None:
        self.guest("pulp", 4, priority="gate", check=True)
        self.guest("vellum-1", 6, check=True)
        # A stale third record must not let a non-gate guest into S.
        proc, body = self.guest("vellum-2", 4)
        self.assertEqual(proc.returncode, 75)
        self.assertEqual(body["core_limit_class"], "non_gate")

    def test_a_gate_build_holding_cores_keeps_lending_off(self) -> None:
        self.acquire("gate-build", 2, priority="gate", capacity=self.T, reserved=self.S)
        self.guest("vellum-1", 6, check=True)
        proc, body = self.guest("vellum-2", 6)
        self.assertEqual(proc.returncode, 75)
        self.assertEqual(body["core_limit_class"], "non_gate")

    def test_linux_guests_do_not_count_as_macos_slots(self) -> None:
        self.guest("linux", 3, kind="tart-linux-vm", check=True)
        proc, body = self.guest("vellum-1", 6)
        # 3 + 6 = 9 > N: a Linux VM holds no macOS slot, so a gate guest can
        # still start and the vellum guest gets only N.
        self.assertEqual(proc.returncode, 75)
        self.assertEqual(body["core_limit_class"], "non_gate")

    def test_lent_guest_cores_are_not_preemptible_borrowing(self) -> None:
        self.guest("vellum-1", 6, check=True)
        self.guest("vellum-2", 6, check=True)
        status = json.loads(self.run_cli(
            "status", "--capacity", str(self.T), "--reserved-gate-cores", str(self.S),
            "--capacity-mem-mb", "0", "--dynamic-lending", "on", *self.LEND).stdout)
        self.assertEqual(status["capacity"]["lent_cores"], 0)

    def test_agent_builds_stay_in_the_non_gate_budget(self) -> None:
        self.guest("vellum-1", 6, check=True)
        self.guest("vellum-2", 6, check=True)
        denied = self.acquire("agent", 1, priority="build", capacity=self.T,
                              reserved=self.S, extra_args=self.LEND, check=False)
        self.assertEqual(denied.returncode, 75)

    def test_memory_axis_uses_the_host_total_for_the_last_slot(self) -> None:
        # N memory = 20000 - 12000 = 8000: one 6000 MB guest fits, a second
        # only under lending, and never past the host's 20000.
        args = dict(capacity_mem_mb=20000, reserved_mem_mb=12000)
        self.guest("vellum-1", 6, mem_mb=6000, check=True, **args)
        proc, _ = self.guest("vellum-2", 6, mem_mb=6000, lend=False, **args)
        self.assertEqual(proc.returncode, 75)
        proc, body = self.guest("vellum-2", 6, mem_mb=6000, **args)
        self.assertEqual(proc.returncode, 0, body)
        proc, _ = self.guest("vellum-3", 1, mem_mb=9000, **args)
        self.assertEqual(proc.returncode, 75)


class ReleaseClassLeaseAdmissionTests(LeaseCliTestCase):
    """Release classes on m5's pulp-gate slots cannot evict or starve slot 1.

    The lease priorities come from the shell helper applied to the labels the
    rendered m5 profile registers, so this pins the shipped path end to end.
    The host is sized for exactly two gate guests (the macOS guest cap is 2 and
    each supervisor slot holds at most one lease). Admission is one-shot: there
    is no waiter queue and no preemption, and a priority at or above the gate
    class only lifts the non-gate budget. So a tagged release at 120 on slot 2
    occupies exactly what a merge-group guest on slot 2 would, and the release
    PR gate at 115 occupies exactly what a PR-head guest would.
    """

    ROOT = Path(__file__).resolve().parents[1]
    HELPER = ROOT / "providers" / "common" / "vm-lease.lib.sh"
    GUEST, CAPACITY, RESERVED = 12, 24, 12
    CLASSES = ("pulp-build-merge-group", "pulp-build-pr-head",
               "pulp-release-tagged", "pulp-release-pr-gate")

    def setUp(self) -> None:
        super().setUp()
        import plistlib
        import macos_fleet_lanes as fleet

        rendered = fleet.rendered_plists(
            fleet.load(self.ROOT / "profiles" / "m5-macos-fleet.toml"))
        slot2 = [body for name, body in rendered.items()
                 if name.endswith(".m5.pulp-gate.slot2.plist")]
        self.assertEqual(len(slot2), 1)
        env = plistlib.loads(slot2[0])["EnvironmentVariables"]
        self.assertEqual(env["TARTCI_ASSIGNMENT_V2_CLASS_LABELS"], ",".join(self.CLASSES))
        script = "set -euo pipefail\nsource \"$1\"\nshift\nfor l; do tartci_vm_lease_priority \"$l\"; echo; done\n"
        proc = subprocess.run(
            ["bash", "-c", script, "priority", str(self.HELPER),
             *(f"{env['TARTCI_RUNNER_LABELS']},{cls}" for cls in self.CLASSES)],
            text=True, capture_output=True, check=False,
            env={k: v for k, v in os.environ.items() if k != "TARTCI_VM_LEASE_PRIORITY"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.prio = dict(zip(("merge", "pr", "tagged", "pr_gate"), proc.stdout.split()))
        self.assertEqual(self.prio, {"merge": "110", "pr": "100", "tagged": "120", "pr_gate": "115"})

    def _take(self, lease_id: str, klass: str, cores: int | None = None,
              check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.acquire(lease_id, cores or self.GUEST, priority=self.prio[klass],
                            capacity=self.CAPACITY, reserved=self.RESERVED, check=check)

    def _held(self) -> list[str]:
        return sorted(row["id"] for row in json.loads(self.run_cli("status").stdout)["leases"])

    @testing_support.requires_tomllib
    def test_tagged_release_admits_exactly_like_merge_group(self) -> None:
        """120 vs 110 is indistinguishable to admission on every occupancy a
        two-slot host can be in: the higher number buys status order only."""
        occupancies = ((), ("merge",), ("pr",), ("tagged",), ("pr_gate",),
                       ("merge", "pr"), ("tagged", "pr"), ("pr_gate", "merge"))
        base = self.store
        for n, held in enumerate(occupancies):
            verdicts = {}
            for klass in ("tagged", "merge"):
                self.store = base.with_name(f"occ-{n}-{klass}")
                for i, occupant in enumerate(held):
                    self._take(f"held-{i}", occupant)
                verdicts[klass] = self._take("probe", klass, check=False).returncode
            with self.subTest(held=held):
                self.assertEqual(verdicts["tagged"], verdicts["merge"])
        self.store = base

    @testing_support.requires_tomllib
    def test_booting_release_on_slot2_leaves_slot1_its_gate_guest(self) -> None:
        for gate in ("merge", "pr"):
            with self.subTest(slot1=gate):
                self.store = self.store.with_name(f"slot1-{gate}")
                self._take("slot2-release", "tagged")
                body = json.loads(self._take("slot1-gate", gate).stdout)
                self.assertTrue(body["ok"])
                self.assertEqual(body["capacity"]["used_cores"], self.CAPACITY)

    @testing_support.requires_tomllib
    def test_release_never_preempts_running_gate_guests(self) -> None:
        """Both slots busy with gate work: a waiting release is refused and
        both gate leases are untouched. Nothing a release does can take capacity
        a running guest holds."""
        self._take("slot1-gate", "pr")
        self._take("slot2-gate", "merge")
        denied = self._take("slot2-release", "tagged", check=False)
        self.assertEqual(denied.returncode, 75)
        self.assertEqual(json.loads(denied.stdout)["reason"], "capacity_exceeded")
        self.assertEqual(self._held(), ["slot1-gate", "slot2-gate"])

    @testing_support.requires_tomllib
    def test_release_pr_gate_admits_exactly_like_pr_head(self) -> None:
        """Every occupancy a two-slot host can be in gives the release PR gate
        PR-head's verdict, so a slot that boots it holds what a gate guest on
        that slot would and nothing more."""
        occupancies = ((), ("merge",), ("pr",), ("tagged",), ("pr_gate",),
                       ("merge", "pr"), ("tagged", "pr"), ("pr_gate", "merge"))
        base = self.store
        for n, held in enumerate(occupancies):
            verdicts = {}
            for klass in ("pr_gate", "pr"):
                self.store = base.with_name(f"occ-{n}-{klass}")
                for i, occupant in enumerate(held):
                    self._take(f"held-{i}", occupant)
                verdicts[klass] = self._take("probe", klass, check=False).returncode
            with self.subTest(held=held):
                self.assertEqual(verdicts["pr_gate"], verdicts["pr"])
        self.store = base

    @testing_support.requires_tomllib
    def test_release_pr_gate_is_admitted_while_an_ordinary_build_holds_the_non_gate_budget(self) -> None:
        """m5 as measured on 2026-09-27: 14 leasable cores, 8 reserved for the
        gate, 6-core guests, and a governed agent build holding the whole
        6-core non-gate budget. A release PR gate boot must still be admitted
        from the reserve, as a PR-head boot on the same slot is."""
        cores, reserved, guest = 14, 8, 6
        base = self.store
        verdicts = {}
        for klass in ("pr_gate", "pr"):
            self.store = base.with_name(f"governed-{klass}")
            self.acquire("governed-build", guest, priority="40",
                         capacity=cores, reserved=reserved)
            verdicts[klass] = self.acquire(
                "probe", guest, priority=self.prio[klass],
                capacity=cores, reserved=reserved, check=False).returncode
        self.store = base
        self.assertEqual(verdicts, {"pr_gate": 0, "pr": 0})


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


def _reclaim_args(store: Path, stale_secs: int = 300) -> argparse.Namespace:
    return argparse.Namespace(
        store_dir=str(store),
        capacity=4,
        reserved_gate_cores=0,
        gate_priority=100,
        stale_secs=stale_secs,
        role=None,
        role_file=None,
        host_cores=None,
        model=None,
    )


def _fake_tart(directory: Path, body: str) -> Path:
    tart = directory / "tart"
    tart.write_text("#!/bin/sh\n" + body)
    tart.chmod(0o755)
    return tart


class LeaseOwnerDeathMustBeProvenTests(unittest.TestCase):
    """A lease leaves the store only when its owner is provably gone.

    An owner probe that could not answer (ps failed or timed out, tart list
    failed) used to read as "owner dead", which silently returned a running
    gate VM's cores to the pool and let the next admission oversubscribe the
    host.
    """

    def _live_record(self, lease_id: str, **extra: object) -> dict:
        now = leases.iso(leases.utcnow())
        return {
            "id": lease_id,
            "lease_size_cores": 1,
            "priority": 40,
            "priority_class": "build",
            **leases.process_identity(os.getpid()),
            "command_kind": "test",
            "owner": "unittest",
            "created_at": now,
            "heartbeat_at": now,
            **extra,
        }

    def test_failed_start_time_probe_on_a_live_owner_keeps_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "leases"
            store.mkdir()
            leases.write_records(store, [self._live_record("probe-failed")])
            # Control: the record is live while the probe works.
            self.assertEqual(
                [row["id"] for row in leases.status_digest(_reclaim_args(store))["leases"]],
                ["probe-failed"],
            )
            with mock.patch.object(leases, "pid_start", return_value=""):
                digest = leases.status_digest(_reclaim_args(store))
            self.assertEqual([row["id"] for row in digest["leases"]], ["probe-failed"])
            self.assertEqual(digest["reaped"], [])
            self.assertIn("owner_unproven_kept:probe-failed", digest["problems"])
            events = [json.loads(line) for line in
                      (store / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["event"], "lease_kept_unproven")
        self.assertEqual(events[-1]["id"], "probe-failed")
        self.assertEqual(events[-1]["caller"]["pid"], os.getpid())

    def test_owner_start_time_matches_under_any_caller_locale_and_zone(self) -> None:
        # The owner's start time is recorded by a launchd supervisor (C locale,
        # no TZ) and read back by whatever shell runs `tartci lease ...` next.
        recorded = subprocess.run(
            ["/bin/ps", "-p", str(os.getpid()), "-o", "lstart="],
            text=True, capture_output=True, check=True,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        ).stdout
        recorded = " ".join(recorded.split())
        foreign = {"LC_ALL": "en_GB.UTF-8", "LANG": "en_GB.UTF-8", "TZ": "Asia/Kolkata"}
        shifted = subprocess.run(
            ["/bin/ps", "-p", str(os.getpid()), "-o", "lstart="],
            text=True, capture_output=True, check=True,
            env={"PATH": "/usr/bin:/bin", **foreign},
        ).stdout
        # Control: the foreign caller environment really does render differently.
        self.assertNotEqual(" ".join(shifted.split()), recorded)
        with mock.patch.dict(os.environ, foreign):
            self.assertEqual(leases.pid_start(os.getpid()), recorded)
            record = self._live_record("foreign-caller", process_start_time=recorded)
            verdict, evidence = leases.owner_verdict(record)
        self.assertEqual(verdict, leases.OWNER_ALIVE, evidence)

    def test_matching_start_time_outranks_a_changed_boot_time_string(self) -> None:
        # kern.boottime is a rendered string; XNU moves it when the calendar
        # clock is stepped. A pid whose recorded start still matches is the
        # same process, whatever the boot string says now.
        record = self._live_record("boot-string-moved")
        record["host_boot_time"] = "{ sec = 1, usec = 2 } Thu Jan  1 00:00:01 1970"
        verdict, evidence = leases.owner_verdict(record)
        self.assertEqual(verdict, leases.OWNER_ALIVE, evidence)
        # Control: a start time that does not match is still pid reuse.
        record["process_start_time"] = "Mon Jan  1 00:00:00 2001"
        self.assertEqual(leases.owner_verdict(record)[0], leases.OWNER_DEAD)

    def test_vm_lease_is_kept_while_tart_still_runs_its_vm(self) -> None:
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=5)
        cases = {
            # tart list output -> expected outcome for a dead guardian
            '[{"Name":"vm-a","State":"running"}]': "kept",
            '[{"Name":"vm-a","State":"stopped"}]': "reaped",
            "[]": "reaped",
        }
        for listing, outcome in cases.items():
            with self.subTest(listing=listing), tempfile.TemporaryDirectory() as td:
                store = Path(td) / "leases"
                store.mkdir()
                tart = _fake_tart(Path(td), f"echo '{listing}'\n")
                record = self._live_record(
                    "vm-tart-macos-vm-vm-a",
                    command_kind="tart-macos-vm",
                    vm_name="vm-a",
                    guardian_pid=exited.pid,
                    guardian_process_start_time="Mon Jan  1 00:00:00 2001",
                    guardian_host_boot_time=leases.host_boot_time(),
                    guardian_mode="exec",
                )
                leases.write_records(store, [record])
                with mock.patch.dict(os.environ, {"TARTCI_TART_BIN": str(tart)}):
                    digest = leases.status_digest(_reclaim_args(store))
                kept = [row["id"] for row in digest["leases"]]
                reaped = [row["id"] for row in digest["reaped"]]
                if outcome == "kept":
                    self.assertEqual(kept, ["vm-tart-macos-vm-vm-a"])
                    self.assertEqual(reaped, [])
                else:
                    self.assertEqual(kept, [])
                    self.assertEqual(reaped, ["vm-tart-macos-vm-vm-a"])
                    event = json.loads(
                        (store / "events.jsonl").read_text().splitlines()[-1])
                    self.assertEqual(event["event"], "lease_reaped")
                    self.assertIn("vm vm-a not running", event["evidence"])

    def test_vm_lease_is_kept_when_tart_cannot_list(self) -> None:
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=5)
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "leases"
            store.mkdir()
            tart = _fake_tart(Path(td), "exit 1\n")
            record = self._live_record(
                "vm-tart-macos-vm-vm-b",
                command_kind="tart-macos-vm",
                vm_name="vm-b",
                guardian_pid=exited.pid,
                guardian_process_start_time="Mon Jan  1 00:00:00 2001",
                guardian_host_boot_time=leases.host_boot_time(),
                guardian_mode="exec",
            )
            leases.write_records(store, [record])
            with mock.patch.dict(os.environ, {"TARTCI_TART_BIN": str(tart)}):
                digest = leases.status_digest(_reclaim_args(store))
        self.assertEqual([row["id"] for row in digest["leases"]], ["vm-tart-macos-vm-vm-b"])
        self.assertIn("owner_unproven_kept:vm-tart-macos-vm-vm-b", digest["problems"])

    def test_event_journal_keeps_one_bounded_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = Path(td)
            journal = store / "events.jsonl"
            with mock.patch.object(leases, "LEASE_EVENTS_MAX_BYTES", 64):
                for n in range(6):
                    leases.journal_lease_events(store, [{"event": "probe", "n": n}])
            self.assertTrue((store / "events.jsonl.1").exists())
            self.assertLess(journal.stat().st_size, 64 + 512)
            last = json.loads(journal.read_text().splitlines()[-1])
        self.assertEqual(last["n"], 5)

    def test_owner_releasing_its_own_vm_lease_skips_the_inventory(self) -> None:
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=5)
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "leases"
            store.mkdir()
            calls = Path(td) / "calls"
            tart = _fake_tart(Path(td), f"echo x >> '{calls}'; echo '[]'\n")
            record = self._live_record(
                "vm-tart-macos-vm-vm-c",
                command_kind="tart-macos-vm",
                vm_name="vm-c",
                guardian_pid=exited.pid,
                guardian_process_start_time="Mon Jan  1 00:00:00 2001",
                guardian_host_boot_time=leases.host_boot_time(),
                guardian_mode="exec",
            )
            with mock.patch.dict(os.environ, {"TARTCI_TART_BIN": str(tart)}):
                active, reaped, _ = leases.reclaim(
                    [record], 300, releasing_id="vm-tart-macos-vm-vm-c")
            self.assertFalse(calls.exists(), "release by the owner must not list VMs")
        self.assertEqual(active, [])
        self.assertEqual([row["id"] for row in reaped], ["vm-tart-macos-vm-vm-c"])


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

    def test_reserved_gate_memory_is_unavailable_to_non_gate_leases(self) -> None:
        """A non-gate lease may not spend the memory a gate VM is owed.

        Cores are deliberately abundant here (capacity 20, 1 core each) so the
        only thing that can deny the second lease is the memory reserve. This is
        the live shape the reserve exists for: a gate VM and a shipyard-local
        build coexist, and the build must not be able to fill the budget the
        next gate VM needs.
        """
        self.acquire(
            "build",
            1,
            capacity=20,
            capacity_mem_mb=16384,
            reserved_mem_mb=8192,
            mem_mb=8192,
            priority="build",
        )
        denied = self.acquire(
            "extra-build",
            1,
            capacity=20,
            capacity_mem_mb=16384,
            reserved_mem_mb=8192,
            mem_mb=1024,
            priority="build",
            check=False,
        )
        self.assertEqual(denied.returncode, 75)
        body = json.loads(denied.stdout)
        self.assertEqual(body["reason"], "memory_exceeded")
        self.assertTrue(body["exceeded_axis"]["memory"])
        self.assertFalse(body["exceeded_axis"]["cores"])
        # Denied against the NON-GATE limit, not the host budget: 8192 + 1024
        # is well inside the 16384 the host has.
        self.assertEqual(body["memory_limit_class"], "non_gate")
        self.assertEqual(body["memory_limit_mb"], 8192)

        # The reserve it was denied against is real: the gate takes it.
        gate = json.loads(
            self.acquire(
                "gate",
                1,
                capacity=20,
                capacity_mem_mb=16384,
                reserved_mem_mb=8192,
                mem_mb=8192,
                priority="gate",
            ).stdout
        )
        self.assertTrue(gate["ok"])
        self.assertEqual(gate["capacity"]["used_mem_mb"], 16384)

    def test_gate_memory_usage_still_blocks_host_wide_overcommit(self) -> None:
        """The reserve narrows the non-gate class; it does not widen the host."""
        self.acquire(
            "gate",
            1,
            capacity=20,
            capacity_mem_mb=16384,
            reserved_mem_mb=8192,
            mem_mb=16384,
            priority="gate",
        )
        denied = self.acquire(
            "build",
            1,
            capacity=20,
            capacity_mem_mb=16384,
            reserved_mem_mb=8192,
            mem_mb=512,
            priority="build",
            check=False,
        )
        self.assertEqual(denied.returncode, 75)
        body = json.loads(denied.stdout)
        self.assertEqual(body["reason"], "memory_exceeded")

    def test_gate_lease_is_not_held_to_the_non_gate_memory_limit(self) -> None:
        """A gate lease may spend its own reserve — that is what it is for."""
        body = json.loads(
            self.acquire(
                "gate",
                1,
                capacity=20,
                capacity_mem_mb=16384,
                reserved_mem_mb=8192,
                mem_mb=12288,
                priority="gate",
            ).stdout
        )
        self.assertTrue(body["ok"])  # 12288 > the 8192 non-gate limit
        self.assertEqual(body["capacity"]["non_gate_limit_mem_mb"], 8192)
        self.assertEqual(body["capacity"]["non_gate_used_mem_mb"], 0)

    def test_memory_reserve_leaves_non_gate_at_least_one_compile_job(self) -> None:
        """An over-sized reserve is clamped, never down to a zero non-gate class."""
        body = json.loads(
            self.acquire(
                "build",
                1,
                capacity=20,
                capacity_mem_mb=4096,
                reserved_mem_mb=999999,
                mem_mb=1536,
                priority="build",
            ).stdout
        )
        self.assertTrue(body["ok"])
        self.assertEqual(body["capacity"]["reserved_gate_mem_mb"], 4096 - 1536)
        self.assertEqual(body["capacity"]["non_gate_limit_mem_mb"], 1536)

    def test_memory_reserve_is_inert_when_the_axis_is_off(self) -> None:
        body = json.loads(
            self.acquire(
                "a",
                1,
                capacity=20,
                capacity_mem_mb=0,
                reserved_mem_mb=999999,
                mem_mb=999999,
                priority="build",
            ).stdout
        )
        self.assertTrue(body["ok"])
        self.assertNotIn("reserved_gate_mem_mb", body["capacity"])

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

    def test_stalled_disk_probe_has_a_bounded_failure(self) -> None:
        original = lease_disk._disk_probe_impl

        def stalled(*args, **kwargs):
            time.sleep(0.2)
            return original(*args, **kwargs)

        with mock.patch.object(lease_disk, "_disk_probe_impl", side_effect=stalled), \
             mock.patch.object(lease_disk, "DISK_PROBE_TIMEOUT_S", 0.01):
            with self.assertRaises(TimeoutError):
                lease_disk.disk_probe(self.tmp.name)

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
            refreshed = json.loads(
                self.run_cli("heartbeat", "--id", "kept-failed-vm").stdout
            )
            self.assertTrue(refreshed["ok"])
            self.assertEqual(refreshed["capacity"]["used_cores"], 1)
            held = json.loads(self.run_cli("status").stdout)
            self.assertEqual([row["id"] for row in held["leases"]], ["kept-failed-vm"])
            self.assertEqual(held["leases"][0]["guardian_pid"], guardian.pid)
            self.assertEqual(held["disk_volumes"][0]["reservation_count"], 1)

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

    def test_dead_guardian_reaps_even_while_keep_failed_supervisor_lives(self) -> None:
        self.acquire(
            "windows-keep-failed",
            1,
            kind="qemu-windows-vm",
            disk_path=Path(self.tmp.name),
        )
        guardian = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "guard-exec",
                "--id",
                "windows-keep-failed",
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
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                records = leases.load_records(self.store)
                if records and records[0].get("guardian_pid") == guardian.pid:
                    break
                time.sleep(0.05)
            else:
                self.fail("guardian identity was not committed before timeout")
            guardian.terminate()
            guardian.communicate(timeout=5)
            released = json.loads(self.run_cli("status").stdout)
            self.assertEqual(released["leases"], [])
            self.assertEqual(released["reaped"][0]["id"], "windows-keep-failed")
        finally:
            if guardian.poll() is None:
                guardian.terminate()
                guardian.communicate(timeout=5)

    def test_guard_run_records_exact_writer_and_survives_supervisor_death(self) -> None:
        supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        guard: subprocess.Popen[str] | None = None
        writer_pid = 0
        try:
            self.acquire(
                "clone-writer",
                1,
                kind="tart-macos-vm",
                pid=supervisor.pid,
                disk_path=Path(self.tmp.name),
            )
            guard = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "guard-run",
                    "--id",
                    "clone-writer",
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
                if records and records[0].get("guardian_writer_pid"):
                    writer_pid = int(records[0]["guardian_writer_pid"])
                    self.assertEqual(records[0]["guardian_pid"], guard.pid)
                    self.assertEqual(records[0]["guardian_mode"], "managed-child")
                    self.assertTrue(records[0]["guardian_writer_process_start_time"])
                    self.assertTrue(records[0]["guardian_writer_host_boot_time"])
                    break
                if guard.poll() is not None:
                    stdout, stderr = guard.communicate()
                    self.fail(
                        f"guard-run exited before handoff rc={guard.returncode} "
                        f"stdout={stdout} stderr={stderr}"
                    )
                time.sleep(0.05)
            else:
                self.fail("guarded writer identity was not committed before timeout")
            # The guard commits the writer's identity before it releases the
            # writer to exec; a guard killed in between leaves a writer that
            # exits without running, whose lease is rightly reaped. Wait for
            # the exec so the guard dies with the writer already running.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                comm = subprocess.run(["ps", "-p", str(writer_pid), "-o", "comm="],
                                      capture_output=True, text=True).stdout.strip()
                if Path(comm).name == "sleep":
                    break
                time.sleep(0.05)
            else:
                self.fail(f"guarded writer {writer_pid} never exec'd its command")

            supervisor.terminate()
            supervisor.wait(timeout=5)
            guard.terminate()
            guard.wait(timeout=5)
            held = json.loads(self.run_cli("status").stdout)
            self.assertEqual([row["id"] for row in held["leases"]], ["clone-writer"])
            self.assertEqual(held["leases"][0]["guardian_writer_pid"], writer_pid)

            os.kill(writer_pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while leases.pid_start(writer_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            released = json.loads(self.run_cli("status").stdout)
            self.assertEqual(released["leases"], [])
            self.assertEqual(released["reaped"][0]["id"], "clone-writer")
        finally:
            if supervisor.poll() is None:
                supervisor.terminate()
                supervisor.wait(timeout=5)
            if guard is not None and guard.poll() is None:
                guard.terminate()
                guard.communicate(timeout=5)
            if writer_pid and leases.pid_start(writer_pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(writer_pid, signal.SIGKILL)
            if guard is not None:
                for stream in (guard.stdout, guard.stderr):
                    if stream is not None:
                        stream.close()

    def test_guard_run_returns_ownership_to_live_supervisor(self) -> None:
        self.acquire(
            "clone-complete",
            1,
            kind="tart-linux-vm",
            disk_path=Path(self.tmp.name),
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "guard-run",
                "--id",
                "clone-complete",
                "--store-dir",
                str(self.store),
                "--",
                "/usr/bin/true",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        records = leases.load_records(self.store)
        self.assertEqual([record["id"] for record in records], ["clone-complete"])
        self.assertNotIn("guardian_pid", records[0])
        held = json.loads(self.run_cli("status").stdout)
        self.assertEqual([row["id"] for row in held["leases"]], ["clone-complete"])

    def test_guard_run_releases_after_writer_finishes_with_dead_supervisor(self) -> None:
        supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.acquire(
                "orphaned-clone",
                1,
                kind="tart-linux-vm",
                pid=supervisor.pid,
                disk_path=Path(self.tmp.name),
            )
            guard = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "guard-run",
                    "--id",
                    "orphaned-clone",
                    "--store-dir",
                    str(self.store),
                    "--",
                    "/bin/sleep",
                    "1",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                records = leases.load_records(self.store)
                if records and records[0].get("guardian_writer_pid"):
                    break
                time.sleep(0.05)
            else:
                self.fail("guarded writer identity was not committed before timeout")
            supervisor.terminate()
            supervisor.wait(timeout=5)
            stdout, stderr = guard.communicate(timeout=5)
            self.assertEqual(guard.returncode, 0, f"stdout={stdout} stderr={stderr}")
            self.assertEqual(leases.load_records(self.store), [])
        finally:
            if supervisor.poll() is None:
                supervisor.terminate()
                supervisor.wait(timeout=5)

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


# This host's own governor file (~/.config/tartci/governor.toml) must never
# leak into these tests: point the governor at a path that does not exist.
_GOVERNOR_ENV_SAVED = None


def setUpModule() -> None:  # noqa: N802 - unittest hook
    global _GOVERNOR_ENV_SAVED
    _GOVERNOR_ENV_SAVED = os.environ.get("TARTCI_GOVERNOR_FILE")
    os.environ["TARTCI_GOVERNOR_FILE"] = os.path.join(
        tempfile.gettempdir(), "tartci-test-absent-governor.toml"
    )


def tearDownModule() -> None:  # noqa: N802 - unittest hook
    if _GOVERNOR_ENV_SAVED is None:
        os.environ.pop("TARTCI_GOVERNOR_FILE", None)
    else:
        os.environ["TARTCI_GOVERNOR_FILE"] = _GOVERNOR_ENV_SAVED


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

    def test_live_same_path_device_change_blocks_new_admission(self) -> None:
        self.acquire(
            "existing-device",
            1,
            disk_path=Path(self.tmp.name),
            disk_growth_mb=1,
        )
        records = leases.load_records(self.store)
        self.assertEqual(len(records), 1)
        records[0]["disk_device_id"] = "previous-device"
        leases.write_records(self.store, records)

        args = leases.parse_args(
            [
                "acquire",
                "--id",
                "after-remount",
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
                "--kind",
                "tart-linux-vm",
                "--disk-path",
                self.tmp.name,
                "--disk-growth-mb",
                "1",
                "--store-dir",
                str(self.store),
                "--json",
            ]
        )
        result, rc = leases.acquire(args)
        self.assertEqual(rc, 75)
        self.assertEqual(
            result["reason"], "disk_device_changed_with_live_reservations"
        )
        self.assertEqual(result["conflicting_leases"][0]["id"], "existing-device")
        self.assertEqual(len(leases.load_records(self.store)), 1)

    def test_retargeted_logical_path_conflicts_across_devices(self) -> None:
        records = [
            {
                "id": "before-retarget",
                "disk_device_id": "dev-a",
                "disk_logical_path": "/configured/vm-store",
                "disk_reservation_path": "/mounted-a/vm-store",
                "disk_mount_path": "/mounted-a",
            }
        ]
        probe = {
            "device_id": "dev-b",
            "logical_path": "/configured/vm-store",
            "reservation_path": "/mounted-b/vm-store",
            "mount_path": "/mounted-b",
        }
        conflicts = leases.disk_identity_conflicts(records, probe)
        self.assertEqual([row["id"] for row in conflicts], ["before-retarget"])

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
