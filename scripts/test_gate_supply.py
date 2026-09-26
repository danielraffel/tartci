#!/usr/bin/env python3
"""The fallback-lane policy: a peer's free leasable gate slots, and the decision.

Three layers, each with its control beside it:
  * the peer report (scripts/gate_supply.py report): which lanes count as free,
    and how the VM cap and the lease store cap them;
  * the decision (gate_supply.decide): grant / hold / unknown;
  * the supervisor (providers/tart-macos/runner.sh): a young job the minimum
    age hides is selected only on a grant, the pre-mint recheck keeps the job
    that justified the boot visible, and every unknown keeps the age rule.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate_supply  # noqa: E402
from fleet_lane_discovery import Lane  # noqa: E402
from test_assignment_v2 import RunnerFixture, _write_exec  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MERGE = "pulp-build-merge-group"
PR = "pulp-build-pr-head"
REPO = "Generous-Corp/pulp"
TIERS = f"{MERGE}|Build and Test\n{PR}|Build and Test"


def stamp(age: float = 0.0) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")


def lane_env(slot: int = 1, repo: str = REPO, cores: int | None = 12) -> dict:
    env = {"TARTCI_RUNNER_REPO": repo, "TARTCI_RUNNER_WORKFLOW_TIERS": TIERS,
           "TARTCI_RUNNER_SLOT": str(slot)}
    if cores:
        env["TARTCI_MACOS_VM_CORES"] = str(cores)
    return env


class ClassifyAndFitTests(unittest.TestCase):
    def test_phases_split_into_free_in_flight_blocked_unknown(self) -> None:
        now = time.time()
        cases = {
            "waiting": "free", "backoff": "free", "loop": "free",
            "booting": "in_flight", "minting-jit": "in_flight", "idle-wait": "in_flight",
            "job-running": "blocked", "vm-lease-denied": "blocked", "scan_blind": "blocked",
            "draining": "blocked", "something-new": "unknown",
        }
        for phase, expected in cases.items():
            with self.subTest(phase=phase):
                state, _ = gate_supply.classify_lane(
                    lane_env(), {"ts": stamp(5), "phase": phase, "labels": ""}, MERGE,
                    [MERGE, PR], now)
                self.assertEqual(state, expected)

    def test_a_stale_heartbeat_is_unknown_never_free(self) -> None:
        state, detail = gate_supply.classify_lane(
            lane_env(), {"ts": stamp(600), "phase": "waiting"}, MERGE, [MERGE, PR], time.time())
        self.assertEqual(state, "unknown")
        self.assertIn("stale", detail)
        control, _ = gate_supply.classify_lane(
            lane_env(), {"ts": stamp(10), "phase": "waiting"}, MERGE, [MERGE, PR], time.time())
        self.assertEqual(control, "free")

    def test_a_lane_in_flight_for_the_other_class_does_not_cover_this_one(self) -> None:
        state, _ = gate_supply.classify_lane(
            lane_env(), {"ts": stamp(5), "phase": "booting", "labels": f"a,b,{PR}"},
            MERGE, [MERGE, PR], time.time())
        self.assertEqual(state, "blocked")
        own, _ = gate_supply.classify_lane(
            lane_env(), {"ts": stamp(5), "phase": "booting", "labels": f"a,b,{MERGE}"},
            MERGE, [MERGE, PR], time.time())
        self.assertEqual(own, "in_flight")

    def test_m5_second_lane_can_never_fit_a_12_core_vm_in_a_14_core_universe(self) -> None:
        busy = {"total_cores": 14, "used_cores": 12, "total_mem_mb": 100000, "used_mem_mb": 16384}
        self.assertEqual(gate_supply.lease_fit_count(busy, 12, 16384, 2), 0)
        idle = {"total_cores": 14, "used_cores": 0, "total_mem_mb": 100000, "used_mem_mb": 0}
        self.assertEqual(gate_supply.lease_fit_count(idle, 12, 16384, 2), 1)
        m1 = {"total_cores": 6, "used_cores": 0}
        self.assertEqual(gate_supply.lease_fit_count(m1, 3, 8192, 2), 2)
        tight_memory = {"total_cores": 40, "used_cores": 0, "total_mem_mb": 20000, "used_mem_mb": 0}
        self.assertEqual(gate_supply.lease_fit_count(tight_memory, 12, 16384, 2), 1)

    def test_derived_memory_matches_the_shell_formula(self) -> None:
        for cores in (1, 3, 6, 12, 16):
            with self.subTest(cores=cores):
                shell = subprocess.run(
                    ["bash", "-c",
                     f'TARTCI_ROOT="{ROOT}"; source "{ROOT}/providers/common/vm-lease.lib.sh";'
                     f' tartci_vm_lease_derived_mem_mb {cores}'],
                    text=True, capture_output=True, check=True).stdout.strip()
                self.assertEqual(int(shell), gate_supply.derived_vm_mem_mb(cores))


class BuildReportTests(unittest.TestCase):
    def _report(self, lanes: dict, *, running: int = 0, cap: int = 2, reserved: int = 0,
                capacity: dict | None = None, pool: str = "on", running_error: bool = False):
        rows = [Lane(label, label, Path(f"/state/{label}"), "") for label in lanes]
        envs = {label: value[0] for label, value in lanes.items()}
        beats = {Path(f"/state/{label}"): value[1] for label, value in lanes.items()}

        def running_reader() -> int | None:
            return None if running_error else running

        return gate_supply.build_report(
            REPO, MERGE, lanes=rows, env_reader=envs.__getitem__,
            heartbeat_reader=beats.get,
            capacity_reader=lambda: capacity if capacity is not None else {
                "total_cores": 26, "used_cores": 0},
            running_reader=running_reader, default_cores_reader=lambda: 12,
            pool_reader=lambda: pool, cap_reader=lambda: cap, reservations_reader=lambda: reserved,
            host="studio")

    def test_free_is_capped_by_vm_slots_and_lease_fit(self) -> None:
        lanes = {"a": (lane_env(1), {"ts": stamp(5), "phase": "waiting"}),
                 "b": (lane_env(2), {"ts": stamp(5), "phase": "waiting"})}
        self.assertEqual(self._report(lanes)["free"], 2)
        self.assertEqual(self._report(lanes, running=1)["free"], 1)
        self.assertEqual(self._report(lanes, reserved=2)["free"], 0)
        m3_busy_agents = {"total_cores": 26, "used_cores": 14}
        self.assertEqual(self._report(lanes, capacity=m3_busy_agents)["free"], 1)
        full = {"total_cores": 26, "used_cores": 24}
        report = self._report(lanes, capacity=full)
        self.assertEqual((report["verdict"], report["free"]), ("ok", 0))

    def test_lanes_for_another_repo_or_class_are_ignored(self) -> None:
        lanes = {"forge": (lane_env(1, repo="Generous-Corp/forge"), {"ts": stamp(5), "phase": "waiting"}),
                 "legacy": ({"TARTCI_RUNNER_REPO": REPO}, {"ts": stamp(5), "phase": "waiting"})}
        report = self._report(lanes)
        self.assertEqual((report["verdict"], report["free"], report["lanes"]), ("ok", 0, []))

    def test_in_flight_lanes_are_counted_separately(self) -> None:
        lanes = {"a": (lane_env(1), {"ts": stamp(5), "phase": "booting", "labels": ""}),
                 "b": (lane_env(2), {"ts": stamp(5), "phase": "job-running"})}
        report = self._report(lanes)
        self.assertEqual((report["free"], report["in_flight"]), (0, 1))

    def test_an_unreadable_inventory_falls_back_to_reservations_like_the_slot_claim(self) -> None:
        lanes = {"a": (lane_env(1), {"ts": stamp(5), "phase": "waiting"})}
        report = self._report(lanes, running_error=True)
        self.assertEqual((report["verdict"], report["inventory"], report["free"]), ("ok", "reservations", 1))
        full = self._report(lanes, running_error=True, reserved=2)
        self.assertEqual((full["verdict"], full["free"]), ("ok", 0))

    def test_blindness_is_unknown_never_zero(self) -> None:
        lanes = {"a": (lane_env(1), {"ts": stamp(5), "phase": "waiting"})}
        stale = {"a": (lane_env(1), {"ts": stamp(900), "phase": "waiting"})}
        self.assertEqual(self._report(stale)["verdict"], "unknown")
        self.assertEqual(self._report(lanes, pool="unknown")["verdict"], "unknown")
        self.assertEqual(
            gate_supply.build_report(REPO, MERGE, lanes=None, lane_problems=["launchctl_unreadable"])["verdict"],
            "unknown")

    def test_a_draining_pool_has_no_free_slots(self) -> None:
        lanes = {"a": (lane_env(1), {"ts": stamp(5), "phase": "waiting"})}
        report = self._report(lanes, pool="draining")
        self.assertEqual((report["verdict"], report["free"]), ("ok", 0))


class DecideTests(unittest.TestCase):
    def _peer(self, host: str, free: int, in_flight: int = 0, verdict: str = "ok",
              fetched_at: float | None = None) -> dict:
        return {"host": host, "verdict": verdict, "repo": REPO, "class": MERGE,
                "free": free, "in_flight": in_flight,
                "fetched_at": time.time() if fetched_at is None else fetched_at}

    def _decide(self, demand: int, peers: list, local: dict | None = None, slot: int = 1):
        return gate_supply.decide(demand, peers, local or {"verdict": "ok", "lanes": []},
                                  own_slot=slot, own_state_dir="/state/me", repo=REPO,
                                  class_label=MERGE, max_age=60, now=time.time())

    def test_peers_with_a_free_slot_hold_and_busy_peers_grant(self) -> None:
        self.assertEqual(self._decide(1, [self._peer("studio", 1), self._peer("m5", 0)])[0], "hold")
        self.assertEqual(self._decide(1, [self._peer("studio", 0), self._peer("m5", 0)])[0], "grant")

    def test_demand_beyond_peer_cover_grants(self) -> None:
        peers = [self._peer("studio", 1), self._peer("m5", 0, in_flight=1)]
        self.assertEqual(self._decide(2, peers)[0], "hold")
        verdict, detail = self._decide(3, peers)
        self.assertEqual(verdict, "grant")
        self.assertIn("excess=1", detail)

    def test_an_unknown_or_stale_peer_is_unknown(self) -> None:
        self.assertEqual(self._decide(1, [self._peer("studio", 0, verdict="unknown")])[0], "unknown")
        stale = self._peer("studio", 0, fetched_at=time.time() - 3600)
        verdict, detail = self._decide(1, [stale])
        self.assertEqual(verdict, "unknown")
        self.assertIn("old", detail)
        self.assertEqual(self._decide(1, [self._peer("studio", 0)])[0], "grant")

    def test_a_report_for_another_class_is_unknown(self) -> None:
        other = self._peer("studio", 0)
        other["class"] = PR
        self.assertEqual(self._decide(1, [other])[0], "unknown")

    def test_a_free_lower_slot_sibling_takes_the_job_first(self) -> None:
        local = {"verdict": "ok", "lanes": [
            {"state_dir": "/state/me", "slot": 2, "state": "free"},
            {"state_dir": "/state/sib", "slot": 1, "state": "free"}]}
        self.assertEqual(self._decide(1, [self._peer("studio", 0)], local, slot=2)[0], "hold")
        self.assertEqual(self._decide(2, [self._peer("studio", 0)], local, slot=2)[0], "grant")
        # The lower slot itself is not held back by a HIGHER free sibling.
        local_low = {"verdict": "ok", "lanes": [
            {"state_dir": "/state/me", "slot": 1, "state": "free"},
            {"state_dir": "/state/sib", "slot": 2, "state": "free"}]}
        self.assertEqual(self._decide(1, [self._peer("studio", 0)], local_low, slot=1)[0], "grant")

    def test_an_in_flight_sibling_covers_one_job(self) -> None:
        local = {"verdict": "ok", "lanes": [{"state_dir": "/state/sib", "slot": 2, "state": "in_flight"}]}
        self.assertEqual(self._decide(1, [self._peer("studio", 0)], local)[0], "hold")

    def test_unreadable_siblings_are_unknown(self) -> None:
        verdict, _ = self._decide(1, [self._peer("studio", 0)], {"verdict": "unknown", "reason": "x"})
        self.assertEqual(verdict, "unknown")


class HostReportCliTests(unittest.TestCase):
    """`gate_supply.py report` against a fake host: plists, heartbeats, leases."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        agents = self.root / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        labels = []
        for slot, phase in ((1, "job-running"), (2, "waiting")):
            label = f"com.danielraffel.tartci.tart-runner-macos-fleet.m5.pulp-gate{'' if slot == 1 else '.slot2'}"
            state = self.root / f"state{slot}"
            state.mkdir()
            env = lane_env(slot) | {"TARTCI_STATE_DIR": str(state)}
            (agents / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label, "EnvironmentVariables": env}))
            (state / "m5-pulp-gate.state.json").write_text(json.dumps({"ts": stamp(3), "phase": phase}))
            labels.append(f"123\t0\t{label}")
        listing = self.root / "launchctl.txt"
        listing.write_text("PID\tStatus\tLabel\n" + "\n".join(labels) + "\n")
        self.env = {"HOME": str(self.root), "TARTCI_FLEET_LAUNCHCTL_LIST_FILE": str(listing),
                    "TARTCI_LANE_AGENTS_DIR": str(agents)}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _report(self, used_cores: int) -> dict:
        capacity = {"total_cores": 14, "used_cores": used_cores, "total_mem_mb": 100000, "used_mem_mb": 0}
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(Path, "home", return_value=self.root), \
                mock.patch("leases.usage", return_value=capacity), \
                mock.patch("leases.capacity_config", return_value={}), \
                mock.patch("leases.load_records", return_value=[]), \
                mock.patch.object(gate_supply.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, "1\n", "")):
            return gate_supply.host_report(REPO, MERGE)

    def test_the_idle_second_lane_beside_a_running_12_core_vm_is_not_free(self) -> None:
        report = self._report(used_cores=12)
        self.assertEqual(report["verdict"], "ok", report)
        self.assertEqual(report["free"], 0)
        self.assertEqual([row["state"] for row in report["lanes"]], ["blocked", "free"])
        control = self._report(used_cores=0)
        self.assertEqual(control["free"], 1, control)


class FallbackSupervisorTests(RunnerFixture, unittest.TestCase):
    """runner.sh with a fake `ssh` standing in for the preferred hosts."""

    def setUp(self) -> None:
        super().setUp()
        self.peers = self.root / "peers"
        self.peers.mkdir()
        _write_exec(self.root / "ssh", (
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do case \"$a\" in -o) ;; *=*) ;; *) target=\"$a\"; break;; esac; done\n"
            f"f=\"{self.peers}/$target.json\"\n"
            "[ -f \"$f\" ] || { echo \"ssh: connect to host $target: Connection refused\" >&2; exit 255; }\n"
            "cat \"$f\"\n"))
        listing = self.root / "launchctl.txt"
        listing.write_text("PID\tStatus\tLabel\n")
        self.env.update({
            "TARTCI_FLEET_LAUNCHCTL_LIST_FILE": str(listing),
            "TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS": "600",
            "TARTCI_RUNNER_NAME": "m1-pulp-gate",
        })

    def _peer(self, target: str, free: int, verdict: str = "ok", in_flight: int = 0) -> None:
        (self.peers / f"{target}.json").write_text(json.dumps({
            "schema": gate_supply.SCHEMA, "host": target, "verdict": verdict,
            "reason": None if verdict == "ok" else "stale heartbeat (waiting, 900s > 120s)",
            "repo": REPO, "class": MERGE, "free": free, "in_flight": in_flight, "lanes": []}))

    def _select(self) -> list[str]:
        result = self._runner("--print-selection")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().split("\t")

    def _events(self, name: str) -> list[dict]:
        log = self.root / "state" / "events.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if f'"event":"{name}"' in line]

    def _enable(self) -> None:
        self.env["TARTCI_FALLBACK_PEERS"] = "studio=m3,m5=m5"

    def test_control_the_minimum_age_hides_a_young_job_without_the_policy(self) -> None:
        self._state(merge=True, fresh_merge=True)
        self._peer("m3", 0)
        self._peer("m5", 0)
        self.assertEqual(self._select()[0], "0")
        self.assertEqual(self._events("fallback_grant"), [])

    def test_busy_peers_grant_a_young_job(self) -> None:
        self._enable()
        self._state(merge=True, fresh_merge=True)
        self._peer("m3", 0)
        self._peer("m5", 0)
        selection = self._select()
        self.assertEqual(selection[0], "1", selection)
        self.assertEqual(selection[2], "0")
        self.assertIn(MERGE, selection[1].split(","))
        grants = self._events("fallback_grant")
        self.assertEqual(len(grants), 1, grants)
        self.assertIn("excess=1", grants[0]["detail"])
        self.assertTrue((self.root / "state" / "m1-pulp-gate.fallback-grant").exists())

    def test_a_peer_with_a_free_leasable_slot_holds(self) -> None:
        self._enable()
        self._state(merge=True, fresh_merge=True)
        self._peer("m3", 1)
        self._peer("m5", 0)
        self.assertEqual(self._select()[0], "0")
        self.assertEqual(len(self._events("fallback_hold")), 1)

    def test_an_unreachable_or_stale_peer_keeps_the_minimum_age(self) -> None:
        self._enable()
        self._state(merge=True, fresh_merge=True)
        self._peer("m5", 0)  # m3 has no fixture: ssh fails
        self.assertEqual(self._select()[0], "0")
        unknown = self._events("fallback_unknown")
        self.assertEqual(len(unknown), 1, unknown)
        self.assertIn("studio", unknown[0]["detail"])
        self._peer("m3", 0, verdict="unknown")
        self._state(merge=True, fresh_merge=True)
        self.assertEqual(self._select()[0], "0")
        self.assertIn("stale heartbeat", self._events("fallback_unknown")[-1]["detail"])

    def test_an_old_job_is_selected_by_the_age_rule_without_asking_peers(self) -> None:
        self._enable()
        self._state(merge=True)  # created long ago
        selection = self._select()
        self.assertEqual(selection[0], "1")
        self.assertEqual(self._events("fallback_grant") + self._events("fallback_unknown"), [])

    def test_pre_mint_keeps_the_granted_job_visible_and_only_for_that_tier(self) -> None:
        self._enable()
        self._state(merge=True, fresh_merge=True)
        self._peer("m3", 0)
        self._peer("m5", 0)
        self.assertEqual(self._select()[0], "1")
        admitted = self._runner("--print-pre-mint-selection", "0")
        self.assertEqual(admitted.stdout.strip(), "1", admitted.stderr)
        # Control: without the grant the same recheck cannot see the young job.
        (self.root / "state" / "m1-pulp-gate.fallback-grant").unlink()
        denied = self._runner("--print-pre-mint-selection", "0")
        self.assertEqual(denied.stdout.strip(), "0", denied.stderr)

    def test_decision_hook_is_off_without_the_knob(self) -> None:
        self._state(merge=True, fresh_merge=True)
        result = self._runner("--print-fallback-decision", "0")
        self.assertEqual(result.stdout.strip(), "off", result.stderr)
        self._enable()
        self._peer("m3", 0)
        self._peer("m5", 1)
        result = self._runner("--print-fallback-decision", "0")
        self.assertTrue(result.stdout.startswith("hold "), result.stdout + result.stderr)

    def test_configuration_is_refused_outside_its_contract(self) -> None:
        self._enable()
        self.env["TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS"] = "0"
        result = self._runner("--print-selection")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MIN_QUEUED_AGE_SECONDS > 0", result.stderr)
        self.env["TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS"] = "600"
        self.env["TARTCI_RUNNER_ASSIGNMENT_MODE"] = "legacy"
        result = self._runner("--print-selection")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires event-class-v2", result.stderr)


class ProfileRenderTests(unittest.TestCase):
    def _profile(self, lane_extra: str, ssh_profiles: bool = True) -> str:
        text = (ROOT / "profiles" / "m1-macos-fleet.toml").read_text()
        return text.replace('assignment_idle_retarget_seconds = 120\n',
                            'assignment_idle_retarget_seconds = 120\n' + lane_extra, 1)

    def _run(self, profile: str, *args: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "p.toml"
            path.write_text(profile)
            return subprocess.run([sys.executable, str(ROOT / "scripts/macos_fleet_lanes.py"),
                                   "validate", str(path), *args],
                                  text=True, capture_output=True, check=False)

    def test_the_knob_renders_resolved_ssh_targets(self) -> None:
        import macos_fleet_lanes as fleet  # noqa: PLC0415
        data = fleet.tomllib.loads(self._profile('fallback_preferred_hosts = ["studio", "m5"]\n'))
        lane = next(row for row in data["lane"] if row["id"] == "pulp-gate")
        env = fleet.lane_plist(data, lane)["EnvironmentVariables"]
        self.assertEqual(env["TARTCI_FALLBACK_PEERS"], "studio=m3,m5=m5")
        control = fleet.tomllib.loads(self._profile(""))
        lane = next(row for row in control["lane"] if row["id"] == "pulp-gate")
        self.assertNotIn("TARTCI_FALLBACK_PEERS", fleet.lane_plist(control, lane)["EnvironmentVariables"])

    def test_validation_rejects_self_and_min_age_zero(self) -> None:
        self.assertEqual(self._run(self._profile('fallback_preferred_hosts = ["studio"]\n')).returncode, 0)
        bad_self = self._run(self._profile('fallback_preferred_hosts = ["m1"]\n'))
        self.assertNotEqual(bad_self.returncode, 0)
        self.assertIn("fallback_preferred_hosts", bad_self.stderr + bad_self.stdout)
        zero_age = self._profile('fallback_preferred_hosts = ["studio"]\n').replace(
            "min_queued_age_seconds = 600", "min_queued_age_seconds = 0", 1)
        self.assertNotEqual(self._run(zero_age).returncode, 0)

    def test_checked_in_profiles_do_not_enable_it(self) -> None:
        for path in (ROOT / "profiles").glob("*-macos-fleet.toml"):
            with self.subTest(profile=path.name):
                self.assertNotIn("fallback_preferred_hosts", path.read_text())


if __name__ == "__main__":
    unittest.main()
