#!/usr/bin/env python3
"""The warm pre-booted gate VM: lease upgrade, park, hand-off, expiry, yield.

Layers, each asserted beside its control:
  * leases.py: a memory-only lease holds memory but no cores, and `resize`
    upgrades it in place under one lock (a denial leaves it unchanged);
  * vm-lease.lib.sh: the provider's acquire honours TARTCI_VM_LEASE_MEMORY_ONLY
    and tartci_resize_vm_lease upgrades through the real store;
  * warm-vm.lib.sh, sourced into a harness with the VM boot and teardown
    stubbed: park, hand-off, denied hand-off, expiry, pool close, yield to
    another lane's demand, sibling deferral, and the 2-VM limit through the
    real macos-vm-cap.lib.sh slot claim;
  * status/doctor/lane_busy reporting and the profile knob.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_doctor  # noqa: E402
import lane_busy  # noqa: E402
import warm_vm_status  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LEASES = ROOT / "scripts" / "leases.py"


def leases(store: Path, *args: str) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(LEASES), *args, "--store-dir", str(store),
         "--capacity", "14", "--capacity-mem-mb", "40000", "--reserved-gate-cores", "0",
         "--reserved-gate-mem-mb", "0", "--json"],
        text=True, capture_output=True, check=False)
    try:
        return result.returncode, json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError(result.stdout + result.stderr) from None


class MemoryOnlyLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = Path(self.temp.name) / "leases"
        self.disk = Path(self.temp.name) / "vms"
        self.disk.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _park(self, mem: int = 16384) -> dict:
        rc, out = leases(self.store, "acquire", "--id", "warm", "--cores", "0", "--memory-only",
                         "--mem-mb", str(mem), "--priority", "gate", "--kind", "tart-macos-vm",
                         "--pid", str(os.getpid()), "--disk-path", str(self.disk))
        self.assertEqual(rc, 0, out)
        return out

    def _records(self) -> list[dict]:
        return json.loads((self.store / "leases.json").read_text())

    def test_a_parked_lease_holds_memory_and_no_cores(self) -> None:
        out = self._park()
        self.assertEqual(out["lease"]["lease_size_cores"], 0)
        self.assertTrue(out["lease"]["memory_only"])
        self.assertEqual(out["capacity"]["used_cores"], 0)
        self.assertEqual(out["capacity"]["used_mem_mb"], 16384)
        # A full 12-core VM lease still fits beside it: the parked VM took no cores.
        rc, other = leases(self.store, "acquire", "--id", "cold", "--cores", "12", "--mem-mb", "16384",
                           "--priority", "gate", "--kind", "build", "--pid", str(os.getpid()))
        self.assertEqual(rc, 0, other)

    def test_memory_only_requires_zero_cores_and_memory(self) -> None:
        rc, out = leases(self.store, "acquire", "--id", "x", "--cores", "2", "--memory-only",
                         "--mem-mb", "100", "--kind", "build", "--pid", str(os.getpid()))
        self.assertNotEqual(rc, 0, out)
        self.assertFalse((self.store / "leases.json").exists() and self._records())

    def test_resize_upgrades_in_place_keeping_identity(self) -> None:
        parked = self._park()["lease"]
        records = self._records()
        records[0]["marker"] = "kept"  # any field resize does not own must survive
        (self.store / "leases.json").write_text(json.dumps(records))
        rc, out = leases(self.store, "resize", "--id", "warm", "--cores", "12", "--mem-mb", "16384",
                         "--priority", "110", "--label", "pulp-build-merge-group")
        self.assertEqual(rc, 0, out)
        [record] = self._records()
        self.assertEqual((record["lease_size_cores"], record["priority"]), (12, 110))
        self.assertNotIn("memory_only", record)
        self.assertEqual(record["marker"], "kept")
        self.assertEqual(record["process_start_time"], parked["process_start_time"])
        self.assertEqual(record["created_at"], parked["created_at"])
        self.assertEqual(record["disk_growth_bytes"], parked["disk_growth_bytes"])

    def test_resize_denied_on_cores_leaves_the_lease_unchanged(self) -> None:
        self._park()
        rc, _ = leases(self.store, "acquire", "--id", "agent", "--cores", "4", "--mem-mb", "4096",
                       "--priority", "build", "--kind", "build", "--pid", str(os.getpid()))
        self.assertEqual(rc, 0)
        before = self._records()
        rc, out = leases(self.store, "resize", "--id", "warm", "--cores", "12", "--mem-mb", "16384",
                         "--priority", "100")
        self.assertEqual(rc, 75, out)
        self.assertEqual(out["reason"], "capacity_exceeded")
        self.assertEqual(self._records(), before)
        # Control: once the agent lease is gone the same upgrade fits.
        leases(self.store, "release", "--id", "agent")
        self.assertEqual(leases(self.store, "resize", "--id", "warm", "--cores", "12",
                                "--mem-mb", "16384", "--priority", "100")[0], 0)

    def test_resize_measures_against_other_leases_only(self) -> None:
        # 40000 MB universe: a 30000 MB parked lease upgraded at the same memory
        # must fit. Counting its own memory twice would read 60000 and deny it.
        self._park(mem=30000)
        rc, out = leases(self.store, "resize", "--id", "warm", "--cores", "12", "--mem-mb", "30000",
                         "--priority", "100")
        self.assertEqual(rc, 0, out)

    def test_resize_of_an_unknown_lease_fails(self) -> None:
        rc, out = leases(self.store, "resize", "--id", "nope", "--cores", "2")
        self.assertEqual((rc, out["reason"]), (1, "unknown_lease"))


class LeaseFitGateTests(unittest.TestCase):
    """The per-poll lease-fit gate must not let a parked VM's memory stop the
    lane (its own or another) from reaching the acquisition that asks it to
    yield or upgrades it."""

    def _fit(self, store: Path) -> int:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "lease_fit.py"), "--cores", "12",
             "--mem-mb", "16384", "--priority", "gate", "--store-dir", str(store),
             "--capacity", "26", "--capacity-mem-mb", "40000", "--reserved-gate-cores", "0",
             "--reserved-gate-mem-mb", "0"],
            text=True, capture_output=True, check=False).returncode

    def test_a_parked_memory_only_lease_does_not_fail_the_fit_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = Path(raw) / "leases"
            (Path(raw) / "vms").mkdir()
            rc, out = leases(store, "acquire", "--id", "warm", "--cores", "0", "--memory-only",
                             "--mem-mb", "30000", "--priority", "gate", "--kind", "tart-macos-vm",
                             "--pid", str(os.getpid()), "--disk-path", str(Path(raw) / "vms"))
            self.assertEqual(rc, 0, out)
            self.assertEqual(self._fit(store), 0)
            # Control: the same memory held by an ordinary lease does not fit.
            records = json.loads((store / "leases.json").read_text())
            records[0].pop("memory_only")
            (store / "leases.json").write_text(json.dumps(records))
            self.assertEqual(self._fit(store), 3)


class LeaseLibTests(unittest.TestCase):
    """The provider's own acquire/resize path against a real temporary store."""

    def test_memory_only_env_and_resize_through_the_shell_lib(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "vms").mkdir()
            script = textwrap.dedent(f"""
                set -euo pipefail
                TARTCI_ROOT={str(ROOT)!r}
                export TARTCI_LEASE_DIR={str(root / 'leases')!r}
                export TARTCI_VM_DISK_GROWTH_GB=0 TARTCI_VM_DISK_FREE_FLOOR_GB=0
                export TARTCI_VM_LEASE_HEARTBEAT_SECS=3600
                source "$TARTCI_ROOT/providers/common/vm-lease.lib.sh"
                TARTCI_VM_LEASE_MEMORY_ONLY=1 tartci_acquire_vm_lease warm-vm 1 tart-macos-vm gate "" 1024 {str(root / 'vms')!r} tart-macos lane runner
                python3 -c 'import json,sys; r=json.load(open(sys.argv[1]))[0]; print("parked", r["lease_size_cores"], r.get("memory_only"))' "$TARTCI_LEASE_DIR/leases.json"
                tartci_resize_vm_lease 1 1024 gate lbl
                python3 -c 'import json,sys; r=json.load(open(sys.argv[1]))[0]; print("upgraded", r["lease_size_cores"], r.get("memory_only"))' "$TARTCI_LEASE_DIR/leases.json"
                echo "active=$TARTCI_ACTIVE_VM_LEASE_CORES"
                # The heartbeat helper's own sleep outlives a kill of the
                # helper; end it by exact parent so nothing is left behind.
                pkill -P "$TARTCI_ACTIVE_VM_LEASE_HEARTBEAT_PID" 2>/dev/null || true
                tartci_release_vm_lease
            """)
            # Output goes to a file, not a pipe: the lease heartbeat helper is a
            # background child, and a pipe would stay open for as long as it lives.
            out_path = root / "out.txt"
            with out_path.open("w") as out:
                result = subprocess.run(["bash", "-c", script], text=True, stdout=out,
                                        stderr=subprocess.STDOUT, check=False, timeout=120)
            output = out_path.read_text()
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("parked 0 True", output)
            self.assertIn("upgraded 1 None", output)
            self.assertIn("active=1", output)


PRELUDE = r'''
set -u
TARTCI_ROOT="{root}"
RUNNER_NAME="${{RUNNER_NAME:-m6-pulp-gate}}"
REPO="${{REPO:-Generous-Corp/pulp}}"
SLOT=1
POLL=20
HOST_NAME=m6
LABELS="self-hosted,macOS,pulp-build"
ASSIGNMENT_MODE=event-class-v2
SSH_OPTS=()
SSH_KEY_PRIV=/dev/null
VM_USER=admin
CAP=2
CURRENT_VM=""; CURRENT_IP=""; CURRENT_RPID=""; CURRENT_RESV=""
CURRENT_GUEST_CORES=""; CURRENT_GUEST_MEM_MB=""; CURRENT_PIP_WHEELHOUSE=0
BOOT_LEASE_DENIED=0
TARTCI_ACTIVE_VM_LEASE_ID=""
i=0
LOG="{log}"
export TARTCI_MACOS_CAP_FILE="{home}/cap" TARTCI_MACOS_LOCKDIR="{home}/lock.d" TARTCI_MACOS_RESV_DIR="{home}/resv"
export TARTCI_WARM_VM_DIR="{home}/warm"
json_sanitize(){{ printf '%s' "$1" | tr '\n\r\t"' '    '; }}
note(){{ :; }}
die(){{ echo "$*" >&2; exit 9; }}
event(){{ printf 'event %s %s\n' "$1" "${{2:-}}" >> "$LOG"; }}
heartbeat(){{ printf 'heartbeat %s\n' "$1" >> "$LOG"; }}
ephemeral_boot_name(){{ printf '%s-%s-%s' "$RUNNER_NAME" "$$" "$1"; }}
tartci_pool_admission_open(){{ [ ! -e "{home}/pool-closed" ]; }}
tartci_pool_lock_absent(){{ true; }}
running_macos_vms(){{ cat "{home}/running" 2>/dev/null || echo 0; }}
tartci_vm_lease_priority(){{ echo gate; }}
sweep_lane_ghost_runners(){{ :; }}
tartci_assignment_v2_invalidate_selection(){{ printf 'invalidated\n' >> "$LOG"; }}
ssh(){{ [ ! -e "{home}/ssh-down" ]; }}
boot_vm_to_ssh(){{
  printf 'boot memory_only=%s vm=%s\n' "${{TARTCI_VM_LEASE_MEMORY_ONLY:-0}}" "$2" >> "$LOG"
  echo $(( $(running_macos_vms) + 1 )) > "{home}/running"
  sleep 120 >/dev/null 2>&1 & CURRENT_RPID=$!
  CURRENT_VM="$2"; CURRENT_IP=192.0.2.10; CURRENT_GUEST_CORES=12; CURRENT_GUEST_MEM_MB=16384
  TARTCI_ACTIVE_VM_LEASE_ID="vm-tart-macos-vm-$2"
}}
discard_current_vm(){{
  printf 'discard %s\n' "$CURRENT_VM" >> "$LOG"
  if [ -e "{home}/discard-fails" ]; then CURRENT_TEARDOWN_PENDING=delete; return 1; fi
  [ -z "$CURRENT_RPID" ] || kill "$CURRENT_RPID" 2>/dev/null || true
  echo $(( $(running_macos_vms) - 1 )) > "{home}/running"
  CURRENT_VM=""; CURRENT_RPID=""; CURRENT_IP=""
}}
tartci_release_vm_lease(){{ printf 'release %s\n' "$TARTCI_ACTIVE_VM_LEASE_ID" >> "$LOG"; TARTCI_ACTIVE_VM_LEASE_ID=""; }}
tartci_resize_vm_lease(){{
  printf 'resize cores=%s mem=%s priority=%s labels=%s\n' "$1" "$2" "$3" "$4" >> "$LOG"
  if [ -e "{home}/resize-denied" ]; then return 75; fi
  return 0
}}
source "$TARTCI_ROOT/providers/tart-macos/macos-vm-cap.lib.sh"
source "$TARTCI_ROOT/providers/tart-macos/warm-vm.lib.sh"
'''


class WarmLibTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.log = self.home / "log"
        self.log.touch()
        self.prelude = self.home / "prelude.sh"
        self.prelude.write_text(PRELUDE.format(root=ROOT, home=self.home, log=self.log))
        self.env = {**os.environ, "TARTCI_WARM_VM": "1", "HOME": str(self.home)}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _bash(self, body: str, **env: str) -> subprocess.CompletedProcess:
        script = f'source "{self.prelude}"\n{textwrap.dedent(body)}\n' \
                 '[ -z "$WARM_RPID" ] || kill "$WARM_RPID" 2>/dev/null || true\n'
        return subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                              check=False, env={**self.env, **env}, timeout=120)

    def _log(self) -> str:
        return self.log.read_text()

    def _state(self) -> dict | None:
        path = self.home / "warm" / "parked.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_park_boots_a_memory_only_vm_and_publishes_it(self) -> None:
        result = self._bash('''
            tartci_warm_try_park || exit 3
            [ -n "$WARM_VM" ] && [ -z "$CURRENT_VM" ] || exit 4
            [ "$(tartci_warm_or_claim_slot 2)" = "$WARM_RESV" ] || exit 5
            [ -f "$WARM_RESV" ] || exit 6
            python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); assert v["state"]=="parked" and v["reserved_cores"]==0' "$TARTCI_WARM_VM_DIR/parked.json" || exit 7
        ''')
        self.assertEqual(result.returncode, 0, result.stderr + self._log())
        self.assertIn("boot memory_only=1", self._log())
        self.assertIn("event warm_parked", self._log())
        self.assertIn("reserved_cores=0", self._log())

    def test_park_is_off_without_the_knob(self) -> None:
        result = self._bash("tartci_warm_try_park && exit 3; exit 0", TARTCI_WARM_VM="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("boot", self._log())

    def test_the_two_vm_limit_is_respected(self) -> None:
        (self.home / "running").write_text("2\n")
        result = self._bash("tartci_warm_try_park && exit 3; exit 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("boot", self._log())
        self.assertFalse((self.home / "warm" / "claim.d").exists())
        # Control: one VM running leaves a slot, which the parked VM then takes,
        # so another lane's claim on the same 2-VM host finds nothing.
        (self.home / "running").write_text("1\n")
        result = self._bash('''
            tartci_warm_try_park || exit 3
            other="$(tartci_claim_macos_slot 2)"
            [ -z "$other" ] || exit 4
        ''')
        self.assertEqual(result.returncode, 0, result.stderr + self._log())

    def test_at_most_one_parked_vm_per_host(self) -> None:
        (self.home / "warm").mkdir()
        holder = subprocess.Popen(["sleep", "30"])
        try:
            (self.home / "warm" / "claim.d").mkdir()
            (self.home / "warm" / "claim.d" / "pid").write_text(f"{holder.pid}\n")
            result = self._bash("tartci_warm_try_park && exit 3; exit 0")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("boot", self._log())
        finally:
            holder.kill()
            holder.wait()
        # Control: a dead holder's claim is stolen.
        result = self._bash("tartci_warm_try_park || exit 3")
        self.assertEqual(result.returncode, 0, result.stderr + self._log())

    def test_handoff_upgrades_the_lease_and_transfers_the_vm(self) -> None:
        result = self._bash('''
            tartci_warm_try_park || exit 3
            parked="$WARM_VM"
            tartci_warm_handoff 7 "self-hosted,pulp-build-merge-group" 110 api || exit 4
            [ "$CURRENT_VM" = "$parked" ] && [ -z "$WARM_VM" ] && [ -z "$WARM_RESV" ] || exit 5
            [ "$CURRENT_GUEST_CORES" = 12 ] || exit 6
            [ ! -e "$TARTCI_WARM_VM_DIR/parked.json" ] || exit 7
            [ ! -e "$TARTCI_WARM_VM_DIR/claim.d" ] || exit 8
            kill "$CURRENT_RPID"
        ''')
        self.assertEqual(result.returncode, 0, result.stderr + self._log())
        self.assertIn("resize cores=12 mem=16384 priority=110 labels=self-hosted,pulp-build-merge-group",
                      self._log())
        self.assertIn("event warm_handoff parked_seconds=", self._log())

    def test_a_denied_upgrade_keeps_the_vm_parked(self) -> None:
        (self.home / "resize-denied").touch()
        result = self._bash('''
            tartci_warm_try_park || exit 3
            rc=0; tartci_warm_handoff 7 lbl 100 api || rc=$?
            [ "$rc" = 75 ] || exit 4
            [ -n "$WARM_VM" ] && [ -z "$CURRENT_VM" ] && [ -f "$WARM_RESV" ] || exit 5
            [ -e "$TARTCI_WARM_VM_DIR/parked.json" ] || exit 6
        ''')
        self.assertEqual(result.returncode, 0, result.stderr + self._log())
        self.assertIn("event warm_handoff_denied", self._log())
        self.assertNotIn("discard", self._log())

    def test_a_dead_parked_vm_is_discarded_and_the_caller_boots_cold(self) -> None:
        result = self._bash('''
            tartci_warm_try_park || exit 3
            kill "$WARM_RPID"; wait "$WARM_RPID" 2>/dev/null || true
            rc=0; tartci_warm_handoff 7 lbl 100 api || rc=$?
            [ "$rc" = 1 ] && [ -z "$WARM_VM" ] && [ -z "$CURRENT_VM" ] || exit 4
        ''')
        self.assertEqual(result.returncode, 0, result.stderr + self._log())
        self.assertIn("event warm_expired reason=vm_died", self._log())

    def test_expiry_after_the_max_park_age(self) -> None:
        result = self._bash('''
            tartci_warm_try_park || exit 3
            tartci_warm_tick
            [ -n "$WARM_VM" ] || exit 4
            WARM_PARKED_AT=$(( $(date +%s) - WARM_MAX_PARK ))
            resv="$WARM_RESV"
            tartci_warm_tick
            [ -z "$WARM_VM" ] || exit 5
            [ ! -e "$resv" ] || exit 6
            [ ! -e "$TARTCI_WARM_VM_DIR/parked.json" ] || exit 7
            # A cooldown follows, so the lane does not re-park at once.
            tartci_warm_try_park && exit 8
            exit 0
        ''')
        self.assertEqual(result.returncode, 0, result.stderr + self._log())
        self.assertEqual(self._log().count("event warm_expired"), 1, self._log())
        self.assertIn("reason=max_park_age", self._log())
        self.assertIn("release vm-tart-macos-vm-", self._log())

    def test_an_unproved_teardown_keeps_its_slot_for_the_loop_to_reconcile(self) -> None:
        result = self._bash(f'''
            tartci_warm_try_park || exit 3
            resv="$WARM_RESV"; vm="$WARM_VM"
            touch "{self.home}/discard-fails"
            WARM_PARKED_AT=$(( $(date +%s) - WARM_MAX_PARK ))
            tartci_warm_tick && exit 4
            [ -z "$WARM_VM" ] && [ "$CURRENT_VM" = "$vm" ] || exit 5
            [ "$CURRENT_RESV" = "$resv" ] && [ -f "$resv" ] || exit 6
            [ ! -e "$TARTCI_WARM_VM_DIR/parked.json" ] || exit 7
            kill "$CURRENT_RPID" 2>/dev/null || true
        ''')
        self.assertEqual(result.returncode, 0, result.stderr + self._log())
        self.assertNotIn("release ", self._log())

    def test_a_closed_pool_tears_it_down(self) -> None:
        result = self._bash(f'''
            tartci_warm_try_park || exit 3
            touch "{self.home}/pool-closed"
            tartci_warm_tick
            [ -z "$WARM_VM" ] || exit 4
        ''')
        self.assertEqual(result.returncode, 0, result.stderr + self._log())
        self.assertIn("reason=pool_closed", self._log())

    def test_it_yields_to_another_lanes_demand(self) -> None:
        other = (f'RUNNER_NAME=m6-forge-gate bash -c \'source "{self.prelude}"; '
                 'tartci_warm_note_demand slot_full\'')
        result = self._bash(f'''
            tartci_warm_try_park || exit 3
            tartci_warm_tick
            [ -n "$WARM_VM" ] || exit 4
            {other}
            tartci_warm_tick
            [ -z "$WARM_VM" ] || exit 5
        ''')
        self.assertEqual(result.returncode, 0, result.stderr + self._log())
        self.assertIn("event warm_yield_requested reason=slot_full", self._log())
        self.assertIn("reason=yield_demand", self._log())
        # Control: with nothing parked, the other lane leaves no marker at all.
        self.log.write_text("")
        result = self._bash(other)
        self.assertNotIn("warm_yield_requested", self._log())

    def test_the_parked_sleep_wakes_on_demand(self) -> None:
        other = (f'(sleep 2; RUNNER_NAME=m6-vellum-gate bash -c \'source "{self.prelude}"; '
                 'tartci_warm_note_demand memory_denied\') &')
        result = self._bash(f'''
            tartci_warm_try_park || exit 3
            {other}
            start=$(date +%s)
            tartci_warm_sleep 60
            [ $(( $(date +%s) - start )) -lt 30 ] || exit 4
        ''')
        self.assertEqual(result.returncode, 0, result.stderr + self._log())

    def test_only_a_memory_denial_asks_it_to_yield(self) -> None:
        result = self._bash(f'''
            tartci_warm_try_park || exit 3
            RUNNER_NAME=m6-forge-gate bash -c 'source "{self.prelude}"
              TARTCI_LAST_VM_LEASE_DENIAL="{{\\"exceeded_axis\\": {{\\"cores\\": true, \\"memory\\": false}}}}"
              tartci_warm_note_lease_denial'
            tartci_warm_demand_pending && exit 4
            RUNNER_NAME=m6-forge-gate bash -c 'source "{self.prelude}"
              TARTCI_LAST_VM_LEASE_DENIAL="{{\\"exceeded_axis\\": {{\\"cores\\": false, \\"memory\\": true}}}}"
              tartci_warm_note_lease_denial'
            tartci_warm_demand_pending || exit 5
        ''')
        self.assertEqual(result.returncode, 0, result.stderr + self._log())

    def test_a_sibling_defers_and_requests_a_handoff(self) -> None:
        sibling = (f'RUNNER_NAME=m6-pulp-gate-slot2 bash -c \'source "{self.prelude}"; '
                   'tartci_warm_sibling_defer\'')
        other_repo = (f'REPO=Generous-Corp/forge RUNNER_NAME=m6-forge-gate bash -c '
                      f'\'source "{self.prelude}"; tartci_warm_sibling_defer\'')
        result = self._bash(f'''
            {sibling} && exit 3
            tartci_warm_try_park || exit 4
            {other_repo} && exit 5
            {sibling} || exit 6
            [ -e "$TARTCI_WARM_VM_DIR/handoff-request" ] || exit 7
            start=$(date +%s)
            tartci_warm_sleep 60
            [ $(( $(date +%s) - start )) -lt 30 ] || exit 8
        ''')
        self.assertEqual(result.returncode, 0, result.stderr + self._log())
        self.assertIn("event warm_sibling_defer", self._log())
        self.assertIn("invalidated", self._log())

    def test_configuration_is_bounded(self) -> None:
        result = self._bash("tartci_warm_configure", TARTCI_WARM_VM_MAX_PARK_SECS="10")
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertIn("MAX_PARK", result.stderr)
        result = self._bash("tartci_warm_configure", TARTCI_WARM_VM="yes")
        self.assertEqual(result.returncode, 9)
        self.assertEqual(self._bash("tartci_warm_configure").returncode, 0)


class RunnerWiringTests(unittest.TestCase):
    """The loop's use of the lib, read from the source (the loop needs a host)."""

    def setUp(self) -> None:
        self.source = (ROOT / "providers/tart-macos/runner.sh").read_text()

    def test_the_loop_ticks_first_claims_through_the_parked_slot_and_parks_when_idle(self) -> None:
        loop = self.source[self.source.index('heartbeat loop\n  while true; do'):]
        self.assertLess(loop.index("tartci_warm_tick"), loop.index("tartci_pool_admission_open"))
        self.assertIn('resv="$(tartci_warm_or_claim_slot "$cap" "$r")"', loop)
        self.assertNotIn("tartci_claim_macos_slot", loop)
        self.assertIn("tartci_warm_try_park", loop)
        self.assertIn("tartci_warm_note_demand slot_full", loop)
        self.assertIn('[ "$resv" != "$WARM_RESV" ]', loop)

    def test_cleanup_discards_a_parked_vm_before_releasing_capacity(self) -> None:
        body = self.source[self.source.index("cleanup(){"):self.source.index("handle_supervisor_signal(){")]
        self.assertLess(body.index("tartci_warm_discard supervisor_exit"), body.index("tartci_release_vm_lease"))

    def test_run_one_hands_off_before_any_cold_boot(self) -> None:
        body = self.source[self.source.index("run_one(){"):]
        self.assertLess(body.index("tartci_warm_handoff"), body.index("boot_vm_to_ssh"))
        # Admission and the JIT mint still run after the hand-off, as for a cold VM.
        self.assertLess(body.index("tartci_warm_handoff"), body.index("heartbeat admission-check"))
        self.assertLess(body.index("heartbeat admission-check"), body.index("generate-jitconfig"))


class ReportingTests(unittest.TestCase):
    def test_status_states(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            self.assertEqual(warm_vm_status.status(directory)["state"], "none")
            now = time.time()
            record = {"supervisor_pid": os.getpid(), "ts": int(now), "parked_at": int(now) - 60,
                      "max_park_seconds": 1800, "vm": "m6-vm", "lane": "m6-pulp-gate"}
            (directory / "parked.json").write_text(json.dumps(record))
            self.assertEqual(warm_vm_status.status(directory, now=now)["state"], "parked")
            self.assertEqual(warm_vm_status.status(directory, now=now + 3600)["state"], "stale")
            self.assertEqual(warm_vm_status.status(
                directory, now=now, pid_alive=lambda _pid: False)["state"], "stale")
            record["parked_at"] = int(now) - 4000
            (directory / "parked.json").write_text(json.dumps(record))
            self.assertEqual(warm_vm_status.status(directory, now=now)["state"], "overdue")
            (directory / "parked.json").write_text("{")
            self.assertEqual(warm_vm_status.status(directory)["state"], "unreadable")

    def test_doctor_maps_every_state_to_a_documented_code(self) -> None:
        reasons = fleet_doctor.load_reasons()
        for state, verdict in (("none", fleet_doctor.NOT_APPLICABLE), ("parked", fleet_doctor.OK),
                               ("stale", fleet_doctor.PROBLEM), ("overdue", fleet_doctor.PROBLEM),
                               ("unreadable", fleet_doctor.UNKNOWN)):
            finding = fleet_doctor.check_warm_vm({"state": state})
            self.assertEqual(finding.state, verdict, state)
            self.assertIn(finding.code, fleet_doctor.CODES)
            self.assertIn(finding.code, reasons)

    def test_lane_busy_reads_a_parked_warm_vm_as_idle_and_a_job_as_busy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            def beat(phase: str) -> None:
                (state / "r.state.json").write_text(json.dumps(
                    {"ts": stamp, "phase": phase, "supervisor_pid": "4242"}))

            env = {"TARTCI_STATE_DIR": str(state)}
            beat("warm-parked")
            self.assertTrue(lane_busy.parked_warm_vm(env, {4242}, time.time()))
            beat("job-running")
            self.assertFalse(lane_busy.parked_warm_vm(env, {4242}, time.time()))
            beat("warm-parked")
            self.assertFalse(lane_busy.parked_warm_vm(env, {9999}, time.time()))


class ProfileTests(unittest.TestCase):
    def _profile(self, extra: str, lane: str = "pulp-gate") -> dict:
        import macos_fleet_lanes as fleet  # noqa: PLC0415
        text = (ROOT / "profiles" / "m3-macos-fleet.toml").read_text()
        anchor = f'id = "{lane}"\n'
        return fleet.tomllib.loads(text.replace(anchor, anchor + extra, 1))

    def test_warm_vm_renders_only_on_slot_one(self) -> None:
        import macos_fleet_lanes as fleet  # noqa: PLC0415
        data = self._profile("warm_vm = true\nwarm_vm_max_park_seconds = 1200\n")
        lane = next(row for row in data["lane"] if row["id"] == "pulp-gate")
        slot1 = fleet.lane_plist(data, lane, slot=1)["EnvironmentVariables"]
        slot2 = fleet.lane_plist(data, lane, slot=2)["EnvironmentVariables"]
        self.assertEqual((slot1["TARTCI_WARM_VM"], slot1["TARTCI_WARM_VM_MAX_PARK_SECS"]), ("1", "1200"))
        self.assertNotIn("TARTCI_WARM_VM", slot2)
        control = self._profile("")
        lane = next(row for row in control["lane"] if row["id"] == "pulp-gate")
        self.assertNotIn("TARTCI_WARM_VM", fleet.lane_plist(control, lane)["EnvironmentVariables"])

    def _validate(self, text: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "p.toml"
            path.write_text(text)
            return subprocess.run([sys.executable, str(ROOT / "scripts/macos_fleet_lanes.py"),
                                   "validate", str(path)], text=True, capture_output=True, check=False)

    def test_validation(self) -> None:
        base = (ROOT / "profiles" / "m3-macos-fleet.toml").read_text()
        one = base.replace('id = "pulp-gate"\n', 'id = "pulp-gate"\nwarm_vm = true\n', 1)
        self.assertEqual(self._validate(one).returncode, 0, self._validate(one).stderr)
        two = one.replace('id = "forge-gate"\n', 'id = "forge-gate"\nwarm_vm = true\n', 1)
        self.assertNotEqual(self._validate(two).returncode, 0)
        age_alone = base.replace('id = "pulp-gate"\n', 'id = "pulp-gate"\nwarm_vm_max_park_seconds = 900\n', 1)
        self.assertNotEqual(self._validate(age_alone).returncode, 0)

    def test_no_checked_in_profile_enables_it(self) -> None:
        for path in (ROOT / "profiles").glob("*.toml"):
            with self.subTest(profile=path.name):
                self.assertNotIn("warm_vm", path.read_text())


if __name__ == "__main__":
    unittest.main()
