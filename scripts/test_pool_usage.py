#!/usr/bin/env python3
"""`tartci pool status --usage` (scripts/pool_usage.py) against synthetic lane logs.

Every number asserted here is computed by hand from the fixture, so a change in
how a VM interval, a demand sample or a discard is counted fails a test rather
than shifting a report nobody re-derives.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pool_usage as pu  # noqa: E402

UNTIL = "2026-09-25T12:00:00Z"   # window: 11:00:00Z -> 12:00:00Z with --range 1h


def ts(hms: str) -> str:
    return f"2026-09-25T{hms}Z"


def ev(hms: str, event: str, vm: str = "", detail: str = "", runner: str = "lane-a",
       **fields) -> dict:
    row = {"ts": ts(hms), "event": event, "runner": runner, "vm": vm, "detail": detail}
    if fields:
        row["fields"] = fields
    return row


def write_lane(root: pathlib.Path, lane: str, rows: list, state_vm: str | None = None,
               runner: str = "lane-a") -> None:
    d = root / lane
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "events.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
        f.write("{not json\n")   # a torn line is skipped, never fatal
    if state_vm is not None:
        (d / f"{runner}.state.json").write_text(json.dumps({"vm": state_vm}))


def report(root: pathlib.Path, *extra: str) -> dict:
    out = io.StringIO()
    with redirect_stdout(out):
        pu.main(["--json", "--range", "1h", "--until", UNTIL, "--now", UNTIL,
                 "--events-root", str(root), "--slots", "2", "--host-label", "h", *extra])
    return json.loads(out.getvalue())


def waiting(hms: str, running: int, runner: str = "lane-a", poll: int = 20) -> dict:
    return ev(hms, "demand_waiting", detail=f"queued=1 running={running}/2 reason=slot_full",
              runner=runner, queued=1, running=running, cap=2, poll=poll, reason="slot_full")


class IdleWithDemandFixture(unittest.TestCase):
    """The acceptance fixture: a known idle-with-demand duration, reported exactly."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_known_idle_with_demand_is_reported_exactly(self) -> None:
        # lane-a: one free slot (running 1 of 2) while it waited with demand.
        # Samples at :00, :25, :50 form one run (each gap <= 2 polls); the last
        # covers one poll (20 s) because the lane then went quiet.
        # 25 + 25 + 20 = 70 s.  A stale_demand line inside the run is noise.
        # lane-b: waited while the host was FULL (2 of 2): 30 + 20 = 50 s of
        # full-host wait and zero idle.  Then a second idle burst on lane-a at
        # 11:30:00 (one sample, ended after 7 s by a job_claim): +7 s.
        write_lane(self.root, "a", [
            waiting("11:10:00", 1),
            ev("11:10:10", "assignment_stale_demand"),
            waiting("11:10:25", 1),
            waiting("11:10:50", 1),
            waiting("11:30:00", 1),
            ev("11:30:07", "job_claim", detail="queued=1"),
        ])
        write_lane(self.root, "b", [
            waiting("11:20:00", 2, runner="lane-b"),
            waiting("11:20:30", 2, runner="lane-b"),
        ], runner="lane-b")
        host = report(self.root)["hosts"][0]
        lanes = {l["lane"]: l for l in host["lanes"]}
        self.assertEqual(lanes["a"]["idle_with_demand_seconds"], 77.0)
        self.assertEqual(lanes["a"]["full_host_wait_seconds"], 0.0)
        self.assertEqual(lanes["b"]["idle_with_demand_seconds"], 0.0)
        self.assertEqual(lanes["b"]["full_host_wait_seconds"], 50.0)
        self.assertEqual(host["idle_with_demand_seconds"], 77.0)
        self.assertEqual(host["idle_with_demand_by_reason"], {"demand_waiting:slot_full": 77.0})
        self.assertEqual(host["full_host_wait_seconds"], 50.0)
        self.assertEqual(lanes["a"]["demand_samples"], 4)

    def test_overlapping_lanes_count_host_idle_time_once(self) -> None:
        # Two lanes idle with demand over the same 20 s: the host lost 20 s,
        # not 40; each lane still reports its own 20 s.
        write_lane(self.root, "a", [waiting("11:10:00", 0)])
        write_lane(self.root, "b", [waiting("11:10:00", 0, runner="lane-b")], runner="lane-b")
        host = report(self.root)["hosts"][0]
        self.assertEqual([l["idle_with_demand_seconds"] for l in host["lanes"]], [20.0, 20.0])
        self.assertEqual(host["idle_with_demand_seconds"], 20.0)

    def test_window_clips_a_sample_and_legacy_yields_parse_from_detail(self) -> None:
        # Starts 10 s before the window: only the in-window 10 s count. The
        # legacy yield event carries no fields; running/cap come from detail
        # and the poll is --default-poll.
        write_lane(self.root, "a", [
            waiting("10:59:50", 1),
            ev("11:40:00", "yielded_to_priority",
               detail="workflow=Release queued=2 priority_demand=1 running=0/2"),
        ])
        host = report(self.root, "--default-poll", "15")["hosts"][0]
        self.assertEqual(host["idle_with_demand_seconds"], 25.0)
        self.assertEqual(host["idle_with_demand_by_reason"],
                         {"demand_waiting:slot_full": 10.0, "yielded_to_priority": 15.0})

    def test_unknown_guest_count_is_neither_idle_nor_full(self) -> None:
        write_lane(self.root, "a", [ev("11:10:00", "demand_waiting", queued=1, running="unknown",
                                       cap=2, poll=20, reason="slot_full")])
        host = report(self.root)["hosts"][0]
        self.assertEqual((host["idle_with_demand_seconds"], host["full_host_wait_seconds"]), (0.0, 0.0))

    def test_missing_demand_events_are_called_a_lower_bound(self) -> None:
        write_lane(self.root, "a", [ev("11:10:00", "scan_blind")])
        host = report(self.root)["hosts"][0]
        self.assertEqual(host["demand_waiting_first_seen"], None)
        self.assertTrue(any("lower bounds" in n for n in host["notes"]))
        write_lane(self.root, "a", [waiting("10:00:00", 1), ev("11:10:00", "scan_blind")])
        self.assertEqual(report(self.root)["hosts"][0]["notes"], [])


class OccupancyAndDiscards(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def lane(self, state_vm: str | None = "") -> dict:
        write_lane(self.root, "a", [
            # VM1 started before the window: 10:50 -> 11:20, served. In window: 20 min,
            # job from 11:00:00 (assigned 10:55) -> 20 min of job time in window.
            ev("10:50:00", "clone_start"),
            ev("10:51:00", "mint_jit", vm="v1"),
            ev("10:51:05", "boot_ok", vm="v1"),
            ev("10:55:00", "job_assigned", vm="v1"),
            ev("11:20:00", "teardown", vm="v1", detail="rc=0"),
            # VM2: cloned 11:21, discarded pre-mint at 11:23 (no teardown event);
            # ends at its last named event. 2 min busy.
            ev("11:19:59", "job_terminal_receipt", vm="v1", detail="runner_rc=0 result=Succeeded"),
            ev("11:21:00", "clone_start"),
            ev("11:21:30", "ccache_guard", vm="v2"),
            ev("11:23:00", "assignment_v2_pre_mint_denied", vm="v2"),
            ev("11:23:40", "assignment_stale_demand"),
            # Lease denials between VMs (a lease is acquired before the clone).
            ev("11:24:00", "lease_denied", detail="axis=memory", axis="memory"),
            ev("11:25:00", "lease_denied", detail="axis=cores+memory", axis="cores+memory"),
            ev("11:26:00", "lease_denied", detail="axis=disk reason=disk_capacity_exceeded"),
            # VM3: cloned 11:30, minted, idle timeout, teardown 11:40. 10 min.
            ev("11:30:00", "clone_start"),
            ev("11:31:00", "mint_jit", vm="v3"),
            ev("11:38:00", "idle_timeout", vm="v3"),
            ev("11:40:00", "teardown", vm="v3", detail="rc=1"),
            # VM4: cloned 11:45, minted, lane went quiet with no named discard
            # cause and the heartbeat no longer holds it: ends at 11:46 (1 min).
            ev("11:45:00", "clone_start"),
            ev("11:46:00", "mint_jit", vm="v4"),
            ev("11:47:00", "job_claim"),
            # VM5: cloned 11:50, assigned 11:52, still running at 12:00.
            ev("11:50:00", "clone_start"),
            ev("11:51:00", "mint_jit", vm="v5"),
            ev("11:52:00", "job_assigned", vm="v5"),
        ], state_vm=state_vm)
        # lane-b only waits for a lease: 11:05 -> 11:15 (restored), then a
        # second hold that ends at the next admission attempt (11:30 -> 11:33).
        write_lane(self.root, "b", [
            ev("11:05:00", "lease_unfit_now", runner="lane-b"),
            ev("11:15:00", "lease_fit_restored", runner="lane-b"),
            ev("11:30:00", "lease_unfit_now", runner="lane-b"),
            ev("11:33:00", "job_claim", runner="lane-b"),
        ], runner="lane-b")
        return {l["lane"]: l for l in report(self.root)["hosts"][0]["lanes"]}["a"]

    def test_busy_time_served_and_discards(self) -> None:
        a = self.lane(state_vm="v5")
        # 20 + 2 + 10 + 1 + 10 (v5 running to the window end) = 43 min.
        self.assertEqual(a["busy_vm_seconds"], 43 * 60.0)
        self.assertEqual(a["occupancy"], round(43 / 60, 4))
        # job time: v1 11:00-11:20 (20 min) + v5 11:52-12:00 (8 min).
        self.assertEqual(a["job_vm_seconds"], 28 * 60.0)
        self.assertEqual(a["jobs_served"], 1)            # v5 is still running
        self.assertEqual(a["vms_running"], 1)
        self.assertEqual(a["vms_cloned"], 4)             # v1 cloned before the window
        self.assertEqual(a["discards_by_reason"],
                         {"assignment_v2_pre_mint_denied": 1, "idle_timeout": 1, "unrecorded": 1})
        self.assertEqual(a["lease_denials_by_axis"],
                         {"cores": 0, "memory": 1, "disk": 1, "cores+memory": 1})
        self.assertEqual(a["job_results"], {"Succeeded": 1})
        self.assertEqual((a["lease_fit_holds"], a["lease_fit_hold_seconds"]), (0, 0.0))
        host = report(self.root)["hosts"][0]
        self.assertEqual((host["lease_fit_holds"], host["lease_fit_hold_seconds"]), (2, 780.0))

    def test_an_open_vm_the_heartbeat_does_not_hold_ended_at_its_last_event(self) -> None:
        a = self.lane(state_vm="")
        # v5 ended at 11:52 (its last named event): 20 + 2 + 10 + 1 + 2 = 35 min,
        # and with no job_assigned->teardown it counts as served (it was assigned)
        # once it is no longer running.
        self.assertEqual(a["busy_vm_seconds"], 35 * 60.0)
        self.assertEqual(a["vms_running"], 0)
        self.assertEqual(a["jobs_served"], 2)

    def test_host_rollup_and_text_output(self) -> None:
        self.lane(state_vm="v5")
        host = report(self.root)["hosts"][0]
        self.assertEqual(host["capacity_vm_seconds"], 2 * 3600)
        self.assertEqual(host["busy_vm_seconds"], 43 * 60.0)
        self.assertEqual(host["max_concurrent_vms"], 1)
        self.assertIsNone(host["vm_cpu_io"])
        out = io.StringIO()
        with redirect_stdout(out):
            pu.main(["--range", "1h", "--until", UNTIL, "--now", UNTIL,
                     "--events-root", str(self.root), "--slots", "2", "--host-label", "h"])
        text = out.getvalue()
        self.assertIn("occupancy: 43.0 of 120.0 slot-min (35.8%)", text)
        self.assertIn("lease denials by axis: cores 0, memory 1, disk 1, cores+memory 1", text)
        self.assertIn("per-job VM CPU/IO: not sampled", text)
        out = io.StringIO()
        with redirect_stdout(out):
            pu.main(["--summary", "--range", "1h", "--until", UNTIL, "--now", UNTIL,
                     "--events-root", str(self.root), "--slots", "2"])
        self.assertRegex(out.getvalue(), r"^usage \(1h\): occupancy 35\.8% of 2 slot\(s\), "
                                         r"1 job\(s\) served, 3 VM\(s\) discarded")


class ReadingAndPeers(unittest.TestCase):
    def test_tail_read_matches_a_full_read_at_any_chunk_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            with open(path, "w") as f:
                for m in range(60):
                    f.write(json.dumps(ev(f"10:{m:02d}:00", "scan_blind", detail="x" * m)) + "\n")
            cutoff = ts("10:41:00")
            with open(path, "rb") as f:
                full = [l for l in f.read().splitlines() if json.loads(l)["ts"] >= cutoff]
            for chunk in (7, 64, 300, 1 << 20):
                self.assertEqual(pu.read_lines_since(path, cutoff, chunk=chunk), full, chunk)
            self.assertEqual(len(full), 19)

    def test_duration_parsing(self) -> None:
        self.assertEqual([pu.parse_duration(v) for v in ("90m", "24h", "7d", "45", "30s")],
                         [5400, 86400, 604800, 45, 30])
        for bad in ("0h", "1w", "h", ""):
            with self.assertRaises(Exception):
                pu.parse_duration(bad)

    def test_peer_reports_are_merged_into_a_fleet_total(self) -> None:
        # A fake ssh runs the piped source locally against the same fixture, so
        # the peer path (source on stdin, JSON back) is exercised end to end.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "fleet"
            write_lane(root, "a", [waiting("11:10:00", 0)])
            fake = pathlib.Path(tmp) / "fake-ssh"
            fake.write_text("#!/bin/sh\n# args: -o X -o Y TARGET COMMAND\n"
                            f"shift 5\nTARTCI_POOL_USAGE_ROOT={root} exec sh -c \"$1 --now {UNTIL} --slots 2\"\n")
            fake.chmod(0o755)
            env = dict(os.environ, TARTCI_POOL_USAGE_SSH=str(fake), TARTCI_POOL_USAGE_ROOT=str(root))
            proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "pool_usage.py"), "--json",
                                   "--range", "1h", "--until", UNTIL, "--now", UNTIL, "--slots", "2",
                                   "--host-label", "local", "--peer", "p1=t1,p2=t2"],
                                  capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rep = json.loads(proc.stdout)
            self.assertEqual([h["host"] for h in rep["hosts"]], ["local", "p1", "p2"])
            fleet = rep["fleet"]
            self.assertEqual(fleet["hosts"], 3)
            self.assertEqual(fleet["idle_with_demand_seconds"], 60.0)
            self.assertEqual(fleet["capacity_vm_seconds"], 3 * 2 * 3600)

    def test_an_unreachable_peer_is_named_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, TARTCI_POOL_USAGE_SSH="false", TARTCI_POOL_USAGE_ROOT=tmp)
            proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "pool_usage.py"),
                                   "--range", "1h", "--peer", "gone=gone"],
                                  capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("host gone: UNREACHABLE", proc.stdout)
            self.assertIn("UNREACHABLE: gone", proc.stdout)


class Wiring(unittest.TestCase):
    def test_the_waiting_branch_samples_demand_it_did_not_place(self) -> None:
        src = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        branch = src[src.index("tartci_warm_note_demand slot_full"):]
        branch = branch[:branch.index("\n      fi")]
        self.assertRegex(branch, r'event demand_waiting "[^"]*" \\\n\s+queued="\$q" running="\$r" '
                                 r'cap="\$cap" poll="\$POLL" reason=slot_full')

    def test_pool_status_dispatches_usage_and_prints_a_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "fleet"
            write_lane(root, "a", [waiting("11:10:00", 0)])
            env = dict(os.environ, TARTCI_POOL_USAGE_ROOT=str(root), HOME=tmp)
            proc = subprocess.run([str(ROOT / "tartci"), "pool", "status", "--usage", "--json",
                                   "--range", "24h"], capture_output=True, text=True, env=env,
                                  timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["schema"], pu.SCHEMA)
        src = (ROOT / "tartci").read_text()
        self.assertIn('"$status_python" "$HERE/scripts/pool_usage.py" --summary', src)


if __name__ == "__main__":
    unittest.main()
