#!/usr/bin/env python3
"""lane_busy sees a lane that is past waiting before `tart run` exists.

On 2026-09-22 studio-pulp-gate-01-96005-1 held its VM lease and had logged
"launching JIT runner" with no job line yet; the process-tree check alone read
that lane idle. These tests drive the probe with a fake launchctl/ps runner, a
real state dir and a lease list, so nothing touches the host.
"""

from __future__ import annotations

import datetime as dt
import json
import plistlib
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lane_busy

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.danielraffel.tartci.tart-runner-macos-fleet.studio.pulp-gate"
NOW = 1_800_000_000.0
SUPERVISOR = 500   # launchd's pid for the lane (the launcher / tartci serve)
RUNNER_SH = 501    # runner.sh, a descendant (writes supervisor_pid = $$)


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Host:
    def __init__(self, td: Path, *, extra_ps: str = "", poll: str | None = None) -> None:
        self.agents = td / "agents"
        self.agents.mkdir()
        self.state = td / "state"
        self.state.mkdir()
        env = {"TARTCI_STATE_DIR": str(self.state)}
        if poll:
            env["TARTCI_VM_POLL"] = poll
        with (self.agents / f"{LABEL}.plist").open("wb") as handle:
            plistlib.dump({"Label": LABEL, "EnvironmentVariables": env}, handle)
        self.ps = (f"{SUPERVISOR} 1 /Users/x/.local/libexec/TartCILauncher.app/Contents/MacOS/tartci-launcher\n"
                   f"{RUNNER_SH} {SUPERVISOR} /bin/bash /Users/x/support/providers/tart-macos/runner.sh --loop\n"
                   + extra_ps)
        self.leases: list[dict] = []

    def beat(self, phase: str, *, age: float = 5, pid: int = RUNNER_SH) -> None:
        (self.state / "studio-pulp-gate-01.state.json").write_text(json.dumps({
            "ts": iso(NOW - age), "phase": phase, "supervisor_pid": str(pid),
            "vm": "studio-pulp-gate-01-96005-1"}))

    def run(self, argv):
        if argv[0] == "launchctl":
            return 0, f"state = running\n\tpid = {SUPERVISOR}\n", ""
        if argv[0] == "ps":
            return 0, self.ps, ""
        raise AssertionError(argv)

    def probe(self, leases=None):
        return lane_busy.probe([LABEL], run=self.run, agents_dir=self.agents,
                               leases=self.leases if leases is None else leases, now=NOW)[0]


class PhaseTests(unittest.TestCase):
    def test_runner_heartbeat_phases_are_all_classified(self) -> None:
        """Every literal phase runner.sh writes is in exactly one set."""
        source = (ROOT / "providers/tart-macos/runner.sh").read_text()
        phases = set(re.findall(r"heartbeat ([a-z_-]+)\s*$", source, re.M))
        phases |= set(re.findall(r"printf ([a-z_-]+)", " ".join(
            line for line in source.splitlines() if "heartbeat \"$(" in line)))
        self.assertGreater(len(phases), 15)
        both = lane_busy.BUSY_PHASES & lane_busy.IDLE_PHASES
        self.assertEqual(both, set())
        self.assertEqual(phases - (lane_busy.BUSY_PHASES | lane_busy.IDLE_PHASES), set())

    def test_every_busy_phase_is_busy(self) -> None:
        for phase in sorted(lane_busy.BUSY_PHASES):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as td:
                host = Host(Path(td))
                host.beat(phase)
                self.assertEqual(host.probe().state, lane_busy.BUSY)

    def test_every_idle_phase_is_idle(self) -> None:
        for phase in sorted(lane_busy.IDLE_PHASES):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as td:
                host = Host(Path(td))
                host.beat(phase)
                self.assertEqual(host.probe().state, lane_busy.IDLE)

    def test_unrecognised_phase_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = Host(Path(td))
            host.beat("some-new-phase")
            self.assertEqual(host.probe().state, lane_busy.UNKNOWN)


class RegressionTests(unittest.TestCase):
    def test_2026_09_22_shape_lease_held_launching_no_tart_run_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = Host(Path(td))
            host.beat("idle-wait")  # written right after "launching JIT runner"
            host.leases = [{"id": "vm-tart-macos-vm-studio-pulp-gate-01-96005-1",
                            "command_kind": "tart-macos-vm", "pid": RUNNER_SH,
                            "vm_name": "studio-pulp-gate-01-96005-1"}]
            row = host.probe()
            self.assertEqual(row.state, lane_busy.BUSY)
            self.assertIn("VM lease", row.detail)

    def test_lease_alone_is_busy_even_with_a_stale_waiting_heartbeat(self) -> None:
        # The clone runs with no new heartbeat after `waiting`/`loop`.
        with tempfile.TemporaryDirectory() as td:
            host = Host(Path(td))
            host.beat("waiting", age=3600)
            host.leases = [{"id": "vm-x", "command_kind": "tart-macos-vm", "pid": RUNNER_SH}]
            self.assertEqual(host.probe().state, lane_busy.BUSY)
            # Control: a lease held by some other lane's supervisor is not ours.
            host.leases = [{"id": "vm-y", "command_kind": "tart-macos-vm", "pid": 99999}]
            self.assertEqual(host.probe().state, lane_busy.IDLE)
            # A build lease is not a VM.
            host.leases = [{"id": "b", "command_kind": "shipyard-local", "pid": RUNNER_SH}]
            self.assertEqual(host.probe().state, lane_busy.IDLE)

    def test_tart_clone_descendant_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = Host(Path(td), extra_ps=f"502 {RUNNER_SH} /opt/homebrew/bin/tart clone pulp-build-runner:latest vm1\n")
            host.beat("waiting")
            row = host.probe(leases=[])
            self.assertEqual((row.state, row.worker_kind), (lane_busy.BUSY, "tart clone"))

    def test_unreadable_lease_store_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = Host(Path(td))
            host.beat("waiting")
            self.assertEqual(lane_busy.probe([LABEL], run=host.run, agents_dir=host.agents,
                                             leases=None, now=NOW)[0].state, lane_busy.UNKNOWN)
            bad = Path(td) / "leases.json"
            bad.write_text("{not json")
            self.assertIsNone(lane_busy.lease_records(bad))
            self.assertEqual(lane_busy.lease_records(Path(td) / "absent.json"), [])


class StalenessTests(unittest.TestCase):
    def test_stale_busy_heartbeat_with_nothing_held_is_idle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = Host(Path(td))
            host.beat("booting", age=601)
            row = host.probe()
            self.assertEqual(row.state, lane_busy.IDLE)
            self.assertIn("stale", row.detail)
            host.beat("booting", age=599)  # control: just inside the window
            self.assertEqual(host.probe().state, lane_busy.BUSY)

    def test_stale_window_scales_with_poll(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = Host(Path(td), poll="120")
            host.beat("booting", age=1000)
            self.assertEqual(host.probe().state, lane_busy.BUSY)  # 10 x 120 = 1200 s
            host.beat("booting", age=1300)
            self.assertEqual(host.probe().state, lane_busy.IDLE)

    def test_missing_or_foreign_heartbeat_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = Host(Path(td))
            self.assertEqual(host.probe().state, lane_busy.UNKNOWN)       # none written
            host.beat("job-running", pid=12345)                           # a previous supervisor
            self.assertEqual(host.probe().state, lane_busy.UNKNOWN)
            host.beat("job-running", pid=SUPERVISOR)                      # launchd pid itself counts
            self.assertEqual(host.probe().state, lane_busy.BUSY)

    def test_lane_without_a_state_dir_keeps_the_process_tree_answer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = Host(Path(td))
            (host.agents / f"{LABEL}.plist").write_bytes(plistlib.dumps({"Label": LABEL}))
            self.assertEqual(host.probe().state, lane_busy.IDLE)


if __name__ == "__main__":
    unittest.main()
