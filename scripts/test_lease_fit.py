#!/usr/bin/env python3
"""A lane whose VM lease cannot be granted must not poll for work it cannot boot.

On m5 the second gate lane (12-core VM, 14-core lease universe) scanned the
queue and asked Shipyard for admission about once a minute while the first
lane held a VM, and was then denied its lease: 257 denials in a day, each one
contending with the lane that could actually serve. `lease_fit.py` answers the
lease question first, with the lease store's own model and without writing,
and `tartci doctor fleet` / `tartci pool status` report the configuration.

The central property is agreement with acquisition: for every case below the
probe says `fits_now` exactly when `leases.py acquire` would grant the lease.
A probe that drifted from acquisition would either hide a real denial or idle a
lane that could have booted.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_doctor  # noqa: E402
import lease_fit  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LEASES = ROOT / "scripts/leases.py"
FIT = ROOT / "scripts/lease_fit.py"
RUNNER = ROOT / "providers/tart-macos/runner.sh"
FIT_LIB = ROOT / "providers/tart-macos/lease-fit.lib.sh"


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "leases"

    def common(self, capacity: int, reserved: int, mem: int, reserved_mem: int) -> list[str]:
        return [
            "--capacity", str(capacity), "--reserved-gate-cores", str(reserved),
            "--capacity-mem-mb", str(mem), "--reserved-gate-mem-mb", str(reserved_mem),
            "--store-dir", str(self.store),
        ]

    def acquire(self, lease_id: str, cores: int, priority: str, mem_mb: int,
                cfg: list[str], store: Path | None = None) -> int:
        args = [a if a != str(self.store) else str(store or self.store) for a in cfg]
        proc = subprocess.run(
            [sys.executable, "-B", str(LEASES), "acquire", "--id", lease_id,
             "--cores", str(cores), "--priority", priority, "--mem-mb", str(mem_mb),
             "--pid", str(os.getpid()), "--kind", "build", *args, "--json"],
            capture_output=True, text=True, check=False)
        return proc.returncode

    def fit(self, cores: int, priorities: list[str], mem_mb: int, cfg: list[str],
            *extra: str) -> tuple[dict, int]:
        argv = [sys.executable, "-B", str(FIT), "--cores", str(cores), "--mem-mb", str(mem_mb)]
        for priority in priorities:
            argv += ["--priority", priority]
        proc = subprocess.run([*argv, *extra, *cfg], capture_output=True, text=True, check=False)
        return json.loads(proc.stdout), proc.returncode


class AgreesWithAcquisitionTests(StoreCase):
    CASES = (
        # (capacity, reserved, mem, reserved_mem, held[(cores, prio, mem)], ask(cores, prio, mem))
        (14, 0, 0, 0, [], (12, "gate", 16384)),
        (14, 0, 0, 0, [(12, "gate", 16384)], (12, "gate", 16384)),      # m5 slot 2
        (26, 0, 0, 0, [(12, "gate", 16384)], (12, "gate", 16384)),
        (26, 0, 0, 0, [(12, "gate", 16384), (12, "build", 1)], (12, "gate", 16384)),  # m3
        (14, 12, 0, 0, [], (4, "vm", 8192)),                             # non-gate budget 2
        (14, 12, 0, 0, [], (2, "vm", 8192)),
        (14, 0, 32768, 0, [(4, "gate", 20000)], (4, "gate", 16384)),     # memory binds
        (14, 0, 32768, 0, [], (4, "gate", 16384)),
        (6, 0, 0, 0, [(3, "gate", 8192)], (3, "gate", 8192)),            # m1 fits two
    )

    def test_fits_now_exactly_when_acquire_grants(self) -> None:
        for index, (capacity, reserved, mem, rmem, held, ask) in enumerate(self.CASES):
            with self.subTest(case=index):
                store = self.tmp / f"store-{index}"
                self.store = store
                cfg = self.common(capacity, reserved, mem, rmem)
                for n, (cores, prio, mb) in enumerate(held):
                    self.assertEqual(self.acquire(f"held-{n}", cores, prio, mb, cfg), 0)
                result, rc = self.fit(ask[0], [ask[1]], ask[2], cfg)
                probe = self.tmp / f"probe-{index}"
                shutil.copytree(store, probe) if store.exists() else probe.mkdir()
                granted = self.acquire("probe", ask[0], ask[1], ask[2], cfg, store=probe) == 0
                self.assertEqual(result["verdict"] == "fits_now", granted, result)
                self.assertEqual(rc == 0, granted)
                # The probe itself never writes a lease.
                records = json.loads((store / "leases.json").read_text()) if (store / "leases.json").exists() else []
                self.assertEqual(len(records), len(held))


class VerdictTests(StoreCase):
    def test_m5_second_lane_waits_and_can_run_one_at_a_time(self) -> None:
        cfg = self.common(14, 0, 0, 0)
        self.assertEqual(self.acquire("slot1", 12, "gate", 16384, cfg), 0)
        result, rc = self.fit(12, ["gate"], 16384, cfg)
        self.assertEqual((result["verdict"], rc), ("not_now", 3))
        self.assertEqual(result["max_concurrent"], 1)
        self.assertEqual(result["binding_axis_now"], "cores")

    def test_a_vm_larger_than_its_budget_never_fits(self) -> None:
        cfg = self.common(14, 0, 0, 0)
        result, rc = self.fit(16, ["gate"], 16384, cfg)
        self.assertEqual((result["verdict"], rc), ("never", 4))
        self.assertEqual(result["max_concurrent"], 0)

    def test_the_most_permissive_class_decides(self) -> None:
        # Non-gate budget 2, gate budget 14: a lane that can lease as either
        # class fits through the gate class.
        cfg = self.common(14, 12, 0, 0)
        result, rc = self.fit(4, ["vm", "gate"], 8192, cfg)
        self.assertEqual((result["verdict"], rc), ("fits_now", 0))
        only_vm, rc_vm = self.fit(4, ["vm"], 8192, cfg)
        self.assertEqual((only_vm["verdict"], rc_vm), ("never", 4))

    def test_non_gate_cap_is_applied_like_acquisition(self) -> None:
        cfg = self.common(14, 12, 0, 0)
        result, rc = self.fit(4, ["vm"], 8192, cfg, "--non-gate-cap", "2")
        self.assertEqual((result["verdict"], rc), ("fits_now", 0))
        self.assertEqual(result["requested_cores"], 2)

    def test_an_unreadable_store_is_unknown(self) -> None:
        self.store.mkdir(parents=True)
        (self.store / "leases.json").write_text("{not json")
        proc = subprocess.run(
            [sys.executable, "-B", str(FIT), "--cores", "4", "--mem-mb", "8192",
             *self.common(14, 0, 0, 0)], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "unknown")


def write_lane(agents: Path, state_root: Path, name: str, record: dict | None) -> None:
    state = state_root / name
    state.mkdir(parents=True, exist_ok=True)
    label = f"com.danielraffel.tartci.tart-runner-macos-fleet.{name}"
    (agents / f"{label}.plist").write_bytes(plistlib.dumps({
        "Label": label,
        "EnvironmentVariables": {"TARTCI_STATE_DIR": str(state), "TARTCI_RUNNER_NAME": name},
    }))
    if record is not None:
        (state / f"{name}.lease-fit.json").write_text(json.dumps({**record, "lane": name}))


def fit_record(verdict: str, cores: int = 12, budget: int = 14, max_concurrent: int = 1) -> dict:
    return {"verdict": verdict, "requested_cores": cores, "requested_mem_mb": 16384,
            "core_budget": budget, "max_concurrent": max_concurrent}


class ConfigurationFindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.agents = self.tmp / "agents"
        self.agents.mkdir()
        self.state = self.tmp / "state"

    def finding(self):
        records, missing = lease_fit.lane_records(
            self.agents, "com.danielraffel.tartci.tart-runner-macos-fleet.")
        return fleet_doctor.check_lease_fit(records, missing, managed=bool(records or missing))

    def test_m5_two_lanes_one_budget_is_a_problem(self) -> None:
        write_lane(self.agents, self.state, "pulp-gate", fit_record("not_now"))
        write_lane(self.agents, self.state, "pulp-gate-slot2", fit_record("fits_now"))
        finding = self.finding()
        self.assertEqual((finding.state, finding.code),
                         (fleet_doctor.PROBLEM, "lanes_exceed_lease_capacity"))
        self.assertIn("fits 1 at once", finding.detail)

    def test_the_control_m1_two_lanes_that_fit_together(self) -> None:
        write_lane(self.agents, self.state, "pulp-gate", fit_record("fits_now", 3, 6, 2))
        write_lane(self.agents, self.state, "pulp-gate-slot2", fit_record("not_now", 3, 6, 2))
        finding = self.finding()
        self.assertEqual((finding.state, finding.code), (fleet_doctor.OK, "lease_fit_ok"))

    def test_a_lane_that_never_fits_is_a_problem(self) -> None:
        write_lane(self.agents, self.state, "pulp-gate", fit_record("never", 16, 14, 0))
        finding = self.finding()
        self.assertEqual((finding.state, finding.code),
                         (fleet_doctor.PROBLEM, "lane_lease_never_fits"))

    def test_lanes_of_different_sizes_are_not_compared(self) -> None:
        write_lane(self.agents, self.state, "pulp-gate", fit_record("fits_now", 12, 26, 2))
        write_lane(self.agents, self.state, "forge", fit_record("fits_now", 4, 26, 6))
        self.assertEqual(self.finding().state, fleet_doctor.OK)

    def test_unmeasured_and_unmanaged(self) -> None:
        self.assertEqual(self.finding().state, fleet_doctor.NOT_APPLICABLE)
        write_lane(self.agents, self.state, "pulp-gate", None)
        finding = self.finding()
        self.assertEqual((finding.state, finding.code),
                         (fleet_doctor.UNKNOWN, "lease_fit_unmeasured"))

    def test_pool_status_report_text(self) -> None:
        write_lane(self.agents, self.state, "pulp-gate", fit_record("not_now"))
        write_lane(self.agents, self.state, "pulp-gate-slot2", fit_record("not_now"))
        proc = subprocess.run(
            [sys.executable, "-B", str(FIT), "report", "--agents-dir", str(self.agents), "--text"],
            capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lease fit: CONFIGURATION", proc.stdout)
        self.assertIn("fits 1 at once", proc.stdout)
        as_json = subprocess.run(
            [sys.executable, "-B", str(FIT), "report", "--agents-dir", str(self.agents)],
            capture_output=True, text=True, check=False)
        self.assertEqual(len(json.loads(as_json.stdout)["oversubscribed"]), 1)

    def test_the_probe_writes_the_record_the_doctor_reads(self) -> None:
        store = self.tmp / "leases"
        record = self.state / "lane" / "lane.lease-fit.json"
        proc = subprocess.run(
            [sys.executable, "-B", str(FIT), "--cores", "12", "--mem-mb", "16384",
             "--priority", "gate", "--record", str(record), "--lane", "lane",
             "--capacity", "14", "--reserved-gate-cores", "0", "--capacity-mem-mb", "0",
             "--store-dir", str(store)], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = json.loads(record.read_text())
        self.assertEqual((written["lane"], written["verdict"], written["max_concurrent"]),
                         ("lane", "fits_now", 1))


class LoopGateTests(unittest.TestCase):
    """The loop waits locally on not-now/never and proceeds on fits/unknown."""

    def gate(self, probe_rc: int, calls: int = 1) -> tuple[subprocess.CompletedProcess, Path]:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        events = tmp / "events"
        script = (
            "set -euo pipefail\n"
            f"source {str(FIT_LIB)!r}\n"
            "tartci_vm_leases_enabled(){ return 0; }\n"
            "note(){ :; }\n"
            f"event(){{ printf '%s\\n' \"$1\" >>{str(events)!r}; }}\n"
            f"heartbeat(){{ printf 'hb:%s\\n' \"$1\" >>{str(events)!r}; }}\n"
            "tartci_lease_fit_probe(){ echo '{\"requested_cores\":12,\"core_budget\":14}'; "
            f"return {probe_rc}; }}\n"
            + "".join("rc=0; tartci_lease_fit_gate || rc=$?; echo \"gate=$rc\"\n" for _ in range(calls))
        )
        proc = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, check=False)
        return proc, events

    def lines(self, events: Path) -> list[str]:
        return events.read_text().split() if events.exists() else []

    def test_not_now_waits_and_reports_once(self) -> None:
        proc, events = self.gate(3, calls=3)
        self.assertEqual(proc.stdout.split(), ["gate=1"] * 3, proc.stderr)
        self.assertEqual(self.lines(events).count("lease_unfit_now"), 1)
        self.assertEqual(self.lines(events).count("hb:lease-wait"), 3)

    def test_never_stops_polling(self) -> None:
        proc, events = self.gate(4)
        self.assertEqual(proc.stdout.split(), ["gate=1"], proc.stderr)
        self.assertIn("lease_never_fits", self.lines(events))

    def test_fits_and_unknown_proceed(self) -> None:
        for rc in (0, 1, 2):
            with self.subTest(probe_rc=rc):
                proc, _ = self.gate(rc)
                self.assertEqual(proc.stdout.split(), ["gate=0"], proc.stderr)

    def test_disabled_gate_never_probes(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        script = (
            f"source {str(FIT_LIB)!r}\n"
            "tartci_vm_leases_enabled(){ return 0; }\n"
            "tartci_lease_fit_probe(){ echo probed; return 3; }\n"
            "TARTCI_LEASE_FIT_GATE=0 tartci_lease_fit_gate && echo proceed\n"
        )
        proc = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.stdout.strip(), "proceed")

    def test_the_loop_checks_fit_before_scanning(self) -> None:
        source = RUNNER.read_text()
        loop = source.index('if [ "$LOOP" = 1 ]; then')
        gate = source.index("tartci_lease_fit_gate", loop)
        scan = source.index('selection="$(select_work)"', loop)
        self.assertLess(gate, scan)

    def test_new_phases_are_idle_for_lane_busy(self) -> None:
        import lane_busy
        for phase in ("lease-wait", "lease-never-fits", "job-claim-covered"):
            self.assertIn(phase, lane_busy.IDLE_PHASES)
            self.assertNotIn(phase, lane_busy.BUSY_PHASES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
