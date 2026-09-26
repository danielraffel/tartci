#!/usr/bin/env python3
"""Behavioral tests for the pre-admission VM lease fit probe.

Measured on the Pulp gate fleet: m5's lease universe is 14 cores and a gate VM
needs 12, so its second gate lane can never lease a VM while the first holds
one. It still ran the Shipyard admission precheck, the ghost-runner sweep and
the disk probes every poll, and was then denied at acquisition (`lease denied
... available_cores 2`, 257 times in a day), adding to the admission and
observation-lock contention every other lane waits on.

These tests DRIVE the real `run_one` body against the real lease library and
the real lease store, with a stub `shipyard` that records whether it was asked.
The clone is observed via the `clone_start` event, which the harness turns into
a distinguishable exit.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MACOS_RUNNER = ROOT / "providers/tart-macos/runner.sh"
ADMISSION_LIB = ROOT / "providers/common/admission-clean.lib.sh"
LEASE_LIB = ROOT / "providers/common/vm-lease.lib.sh"
LEASES = ROOT / "scripts/leases.py"
HOST_PROFILE = ROOT / "scripts/host_profile.py"

CLONE_REACHED_EXIT = 17
GATE_VM_CORES = 12
LABELS = "self-hosted,macOS,ARM64,pulp-build-vm,pulp-build-pr-head"
REPO = "Generous-Corp/pulp"

# A deterministic m5-shaped host: 16 cores as a dedicated builder leaves a
# 14-core lease universe, which fits one 12-core gate VM and not two.
HOST_ENV = {
    "TARTCI_HOST_CORES": "16",
    "TARTCI_HOST_MEM_MB": "131072",
    "TARTCI_ROLE": "dedicated-builder",
}


def function_body(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\{{\n(.*?)^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group(1)


def admit_envelope() -> dict[str, object]:
    return {
        "schema_version": 1,
        "command": "runner:admission-clean",
        "verdict": "admit",
        "reason": "clean",
        "repo": REPO,
        "base": "main",
        "labels": sorted({label.lower() for label in LABELS.split(",")}),
        "observed_at": "2026-09-26T05:34:56.027586+00:00",
        "blocker_run_ids": [],
    }


class Harness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.events = tmp / "events.tsv"
        self.notes = tmp / "notes.log"
        self.heartbeats = tmp / "heartbeats.log"
        self.shipyard_calls = tmp / "shipyard-calls"
        self.state = tmp / "state"
        self.state.mkdir()
        self.store = tmp / "leases"
        self.env = os.environ.copy()
        self.env.update(HOST_ENV)
        self.env.update(
            {
                "PATH": os.pathsep.join([str(self.bin), self.env.get("PATH", "/usr/bin:/bin")]),
                "TARTCI_ROOT": str(ROOT),
                "TARTCI_LEASE_DIR": str(self.store),
                "TARTCI_ADMISSION_CLEAN_MODE": "required",
                "TARTCI_SHIPYARD_CLI": "stub-shipyard",
                "TARTCI_ADMISSION_CLEAN_STATE_DIR": str(tmp / "breaker"),
            }
        )
        self.env.pop("TARTCI_VM_LEASES", None)
        self.env.pop("TARTCI_VM_LEASE_FIT_PROBE", None)
        self.env.pop("TARTCI_VM_LEASE_PRIORITY", None)
        payload = tmp / "verdict.json"
        payload.write_text(json.dumps(admit_envelope()), encoding="utf-8")
        stub = self.bin / "stub-shipyard"
        stub.write_text(
            "#!/bin/bash\n"
            f"printf 'called\\n' >>{str(self.shipyard_calls)!r}\n"
            f"cat {str(payload)!r}\n"
            "printf '\\n'\n"
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def lease_capacity(self) -> int:
        out = subprocess.run(
            [sys.executable, str(HOST_PROFILE), "--json"],
            env=self.env, text=True, capture_output=True, check=True,
        )
        return int(json.loads(out.stdout)["lease_capacity_cores"])

    def hold(self, cores: int) -> None:
        """Another holder on this host: a live lease owned by this test process."""
        subprocess.run(
            [
                sys.executable, str(LEASES), "acquire", "--id", "other-holder",
                "--cores", str(cores), "--mem-mb", "16384", "--priority", "100",
                "--pid", str(os.getpid()), "--kind", "test", "--json",
            ],
            env=self.env, text=True, capture_output=True, check=True,
        )

    def run(self, *, calls: int = 1, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        body = function_body(MACOS_RUNNER.read_text(encoding="utf-8"), "run_one")
        invocations = "".join(
            "rc=0; run_one %d %r 0 || rc=$?\n" % (n + 1, LABELS) for n in range(calls)
        )
        harness = self.tmp / "harness.sh"
        harness.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            f"TARTCI_ROOT={str(ROOT)!r}\n"
            f"source {str(ADMISSION_LIB)!r}\n"
            # The REAL lease library, so the probe, its fail-open rules and the
            # lease store under it are what run. Only the parts that would touch
            # a real host (acquisition, VM sizing, disk) are stubbed below.
            f"source {str(LEASE_LIB)!r}\n"
            "ephemeral_boot_name(){ printf 'lane-vm-%s' \"$1\"; }\n"
            "now_epoch(){ printf '0'; }\n"
            "runner_group_id_for_tier(){ printf '11'; }\n"
            "runner_api_root_for_group(){ printf 'repos/%s' \"$REPO\"; }\n"
            "jit_admission_denied(){ return 1; }\n"
            "tartci_pool_lock_absent(){ return 0; }\n"
            "tartci_check_macos_disk_floor_with_cleanup_once(){ return 0; }\n"
            "tartci_prepare_and_check_disk_root_observed(){ return 0; }\n"
            "tartci_prepare_disk_root(){ return 0; }\n"
            "reclaim_runner_name(){ :; }\n"
            "sweep_lane_ghost_runners(){ :; }\n"
            f"tartci_vm_lease_cores(){{ printf '{GATE_VM_CORES}'; }}\n"
            "tartci_vm_lease_mem_mb(){ printf '16384'; }\n"
            "tartci_acquire_vm_lease(){ return 0; }\n"
            "tartci_release_vm_lease(){ :; }\n"
            "discard_current_vm(){ :; }\n"
            "runtime_emit_complete(){ :; }\n"
            f"note(){{ printf '%s\\n' \"$*\" >>{str(self.notes)!r}; }}\n"
            f"heartbeat(){{ printf '%s\\n' \"$1\" >>{str(self.heartbeats)!r}; }}\n"
            "event(){\n"
            f"  printf '%s\\t%s\\n' \"$1\" \"${{2:-}}\" >>{str(self.events)!r}\n"
            f"  [ \"$1\" = clone_start ] && exit {CLONE_REACHED_EXIT}\n"
            "  return 0\n"
            "}\n"
            f"REPO={REPO!r}\n"
            f"LABELS={LABELS!r}\n"
            "GOLDEN='pulp-build-runner:latest'\n"
            "RUNNER_NAME='lane-02'\n"
            "SLOT=2\n"
            f"STATE_DIR={str(self.state)!r}\n"
            f"TART_HOME={str(self.tmp / 'vms')!r}\n"
            f"CACHE_ROOT={str(self.tmp / 'cache')!r}\n"
            f"MACOS_LOGROOT={str(self.tmp / 'logs')!r}\n"
            "FETCHCONTENT_SOURCE_ROOT=''\n"
            "CHROME_MOUNT_ARG=''\n"
            "ASSIGNMENT_MODE='off'\n"
            "CURRENT_VM=''\n"
            "CURRENT_IP=''\n"
            "CURRENT_RESV=''\n"
            "CURRENT_RPID=''\n"
            "CURRENT_LABELS=''\n"
            "CURRENT_RUNNER_API_ROOT=''\n"
            "SERVING_BLOCKED_SINCE=''\n"
            "VM_LEASE_INFEASIBLE_REPORTED=0\n"
            "RUNNER_VERSION='2.336.0'\n"
            f"run_one(){{\n{body}}}\n"
            f"{invocations}"
            "exit $rc\n",
            encoding="utf-8",
        )
        harness.chmod(0o755)
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run(
            ["/bin/bash", str(harness)], env=env, text=True,
            capture_output=True, check=False, timeout=120,
        )

    def event_names(self) -> list[str]:
        if not self.events.exists():
            return []
        return [line.split("\t", 1)[0] for line in self.events.read_text().splitlines() if line]

    def shipyard_asked(self) -> bool:
        return self.shipyard_calls.exists()


class LeaseFitProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.harness = Harness(Path(self._tmp.name))
        capacity = self.harness.lease_capacity()
        # The m5 shape the test is about. If the profile arithmetic changes,
        # say so rather than silently testing a host where both VMs fit.
        self.assertEqual(capacity, 14, "host profile no longer yields the m5-shaped universe")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_lane_whose_lease_cannot_fit_skips_admission_and_boot(self) -> None:
        self.harness.hold(GATE_VM_CORES)
        result = self.harness.run()
        self.assertEqual(result.returncode, 75, result.stderr)
        names = self.harness.event_names()
        self.assertIn("vm_lease_infeasible", names)
        self.assertNotIn("admission_precheck", names)
        self.assertNotIn("clone_start", names)
        self.assertFalse(
            self.harness.shipyard_asked(),
            "a lane that cannot lease a VM still polled Shipyard admission",
        )
        self.assertIn("vm-lease-infeasible", self.harness.heartbeats.read_text().split())
        detail = self.harness.events.read_text()
        self.assertIn("reason=capacity_exceeded", detail)
        self.assertIn("available_cores=2", detail)

    def test_the_blocked_stretch_is_reported_once_not_per_poll(self) -> None:
        self.harness.hold(GATE_VM_CORES)
        result = self.harness.run(calls=3)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertEqual(self.harness.event_names().count("vm_lease_infeasible"), 1)
        self.assertEqual(
            self.harness.heartbeats.read_text().split().count("vm-lease-infeasible"), 3,
            "every blocked poll must still heartbeat",
        )

    def test_a_lane_whose_lease_fits_proceeds_to_admission_and_clone(self) -> None:
        # Control: the same lane on an idle host asks admission and clones.
        result = self.harness.run()
        self.assertEqual(result.returncode, CLONE_REACHED_EXIT, result.stderr)
        names = self.harness.event_names()
        self.assertNotIn("vm_lease_infeasible", names)
        self.assertIn("admission_precheck", names)
        self.assertTrue(self.harness.shipyard_asked())

    def test_an_unreadable_lease_store_fails_open(self) -> None:
        self.harness.store.mkdir()
        (self.harness.store / "leases.json").write_text("{not json", encoding="utf-8")
        result = self.harness.run()
        self.assertEqual(result.returncode, CLONE_REACHED_EXIT, result.stderr)
        self.assertNotIn("vm_lease_infeasible", self.harness.event_names())

    def test_the_probe_can_be_turned_off(self) -> None:
        self.harness.hold(GATE_VM_CORES)
        result = self.harness.run(extra_env={"TARTCI_VM_LEASE_FIT_PROBE": "0"})
        self.assertEqual(result.returncode, CLONE_REACHED_EXIT, result.stderr)
        self.assertNotIn("vm_lease_infeasible", self.harness.event_names())

    def test_disabled_leases_never_block(self) -> None:
        self.harness.hold(GATE_VM_CORES)
        result = self.harness.run(extra_env={"TARTCI_VM_LEASES": "0"})
        self.assertEqual(result.returncode, CLONE_REACHED_EXIT, result.stderr)


if __name__ == "__main__":
    unittest.main()
