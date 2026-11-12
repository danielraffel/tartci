#!/usr/bin/env python3
"""Build classes, dynamic gate lending, and the governor config surface.

The invariant under test: a gate lease is never admitted less often than under
the class-less static model. Everything lending adds is borrowed capacity that
gate admission does not count, and that a gate grant preempts (by QoS, never
by killing).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import governor
import host_profile
import leases

HERE = Path(__file__).resolve().parent
LEASES = HERE / "leases.py"
GOVERNOR = HERE / "governor.py"

# The MacBook-shaped host the design doc works through: T=14, S=8, N=6.
T, S = 14, 8


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "leases"
        self.qos_log = root / "qos.jsonl"
        self.env = dict(os.environ)
        # Never read this machine's own governor file or fleet profile.
        self.env["TARTCI_GOVERNOR_FILE"] = str(root / "absent-governor.toml")
        self.env["TARTCI_FLEET_PROFILE"] = str(root / "absent-fleet.toml")
        self.env["TARTCI_QOS_ACTION_LOG"] = str(self.qos_log)
        for key in list(self.env):
            if key.startswith("TARTCI_GOV_") or key in ("TARTCI_AGENT_FLOOR_CORES",
                                                         "TARTCI_AGENT_FLOOR_POOL_CORES"):
                self.env.pop(key)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cli(self, *args: str, lending: str = "off", prompt: int = 6,
            background_share: int = 4, interactive_share: int = T) -> tuple[dict, int]:
        argv = [
            sys.executable, str(LEASES), *args,
            "--store-dir", str(self.store),
            "--capacity", str(T), "--reserved-gate-cores", str(S),
            "--capacity-mem-mb", "0",
            "--dynamic-lending", lending,
            "--gate-prompt-reserve-cores", str(prompt),
            "--background-share-cores", str(background_share),
            "--interactive-share-cores", str(interactive_share),
            "--json",
        ]
        proc = subprocess.run(argv, text=True, capture_output=True, env=self.env, check=False)
        try:
            body = json.loads(proc.stdout)
        except ValueError:
            self.fail(f"{args}: rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}")
        return body, proc.returncode

    def acquire(self, lease_id: str, cores: int, *, priority: str = "build",
                build_class: str | None = None, min_cores: int = 0,
                allow_floor: bool = False, **kw) -> tuple[dict, int]:
        extra = ["--class", build_class] if build_class else []
        if min_cores:
            extra += ["--min-cores", str(min_cores)]
        if allow_floor:
            extra += ["--allow-floor", "--agent-floor-cores", "6", "--agent-floor-pool-cores", "6"]
        return self.cli(
            "acquire", "--id", lease_id, "--cores", str(cores), "--priority", priority,
            "--pid", str(os.getpid()), "--kind", "test", *extra, **kw,
        )

    def gate(self, lease_id: str, cores: int = 6, **kw) -> tuple[dict, int]:
        return self.acquire(lease_id, cores, priority="110", **kw)

    def qos_actions(self) -> list[dict]:
        if not self.qos_log.exists():
            return []
        return [json.loads(line) for line in self.qos_log.read_text().splitlines() if line]


class LendingOffIsTheStaticModel(StoreTestCase):
    def test_interactive_without_lending_stays_within_guaranteed_budget(self) -> None:
        body, rc = self.acquire("i1", 14, build_class="interactive", min_cores=2)
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["lease"]["lease_size_cores"], T - S)
        self.assertTrue(body["partial"])
        self.assertNotIn("borrowed", body["lease"])

    def test_gate_admission_matches_classless_rule_matrix(self) -> None:
        # For every non-gate fill level, a gate request is admitted iff the
        # static rule admits it. Lending off must not change a single verdict.
        for fill in range(0, T - S + 1):
            for gate_size in (3, 6, 8, 9):
                with self.subTest(fill=fill, gate_size=gate_size):
                    for record in json.loads((self.store / "leases.json").read_text()) \
                            if (self.store / "leases.json").exists() else []:
                        self.cli("release", "--id", record["id"])
                    if fill:
                        self.acquire("ng", fill)
                    body, rc = self.gate("g", gate_size)
                    static_admits = fill + gate_size <= T
                    self.assertEqual(rc == 0, static_admits, body)


def main_branch_verdict(cfg: dict, current: dict, priority: int, size: int, mem: int) -> tuple:
    """The admission rule exactly as main had it before build classes (the
    class-less two-axis check `acquire` inlined), kept verbatim as the oracle."""
    limit = cfg["total"]
    used_for_limit = current["used_cores"]
    if priority < cfg["gate_priority"]:
        limit = max(1, cfg["total"] - cfg["reserved_gate_cores"])
        used_for_limit = current["non_gate_used_cores"]
    total_exceeded = current["used_cores"] + size > cfg["total"]
    class_exceeded = used_for_limit + size > limit
    mem_on = cfg["total_mem_mb"] > 0
    mem_limit = cfg["total_mem_mb"]
    used_mem_for_limit = current.get("used_mem_mb", 0)
    if priority < cfg["gate_priority"]:
        mem_limit = max(cfg["per_job_mem_mb"], cfg["total_mem_mb"] - cfg["reserved_gate_mem_mb"])
        used_mem_for_limit = current.get("non_gate_used_mem_mb", 0)
    mem_exceeded = mem_on and (
        current.get("used_mem_mb", 0) + mem > cfg["total_mem_mb"]
        or used_mem_for_limit + mem > mem_limit
    )
    return total_exceeded, class_exceeded, mem_exceeded


class LendingOffEquivalence(unittest.TestCase):
    """Differential check of the production admission function against main's
    rule: with lending off and no --class, every verdict (gate and non-gate,
    cores and memory, with floor leases in the store) must be identical."""

    def cfg(self, mem_mb: int) -> dict:
        env = {"TARTCI_GOVERNOR_FILE": "/nonexistent/governor.toml",
               "TARTCI_FLEET_PROFILE": "/nonexistent/fleet.toml"}
        with mock.patch.dict(os.environ, env):
            args = leases.parse_args([
                "status", "--capacity", str(T), "--reserved-gate-cores", str(S),
                "--capacity-mem-mb", str(mem_mb), "--reserved-gate-mem-mb", str(mem_mb // 4),
            ])
            cfg = leases.capacity_config(args)
        self.assertEqual(cfg["dynamic_lending"], 0)
        return cfg

    def test_every_verdict_matches_main(self) -> None:
        import random
        rng = random.Random(20260930)
        checked = 0
        for mem_mb in (0, 65536):
            cfg = self.cfg(mem_mb)
            gate_p = cfg["gate_priority"]
            for _ in range(400):
                records = []
                for index in range(rng.randint(0, 5)):
                    priority = rng.choice((10, 50, 90, gate_p, gate_p + 10))
                    record = {"id": f"r{index}", "priority": priority,
                              "lease_size_cores": rng.randint(1, 8),
                              "lease_size_mem_mb": rng.randint(1, 20000)}
                    if rng.random() < 0.2 and priority < gate_p:
                        record["floor"] = True
                    records.append(record)
                current = leases.usage(records, cfg)
                self.assertEqual(current.get("lent_cores"), 0)
                for priority in (50, gate_p, gate_p + 5):
                    for size in range(1, T + 2):
                        mem = rng.choice((size * cfg["per_job_mem_mb"], rng.randint(1, 70000)))
                        new = leases.core_and_memory_verdict(cfg, current, priority, size, mem)
                        self.assertEqual(
                            (new["total_exceeded"], new["class_exceeded"], new["mem_exceeded"]),
                            main_branch_verdict(cfg, current, priority, size, mem),
                            (records, priority, size, mem),
                        )
                        checked += 1
        self.assertGreater(checked, 10000)  # positive control: the loop really ran


class LendingOffNonGateMatrix(StoreTestCase):
    def test_classless_build_admission_matches_static_rule(self) -> None:
        # The CLI (production acquire) path for non-gate, class-less leases.
        for fill in range(0, T - S + 1):
            for size in (1, 3, 6, 7):
                with self.subTest(fill=fill, size=size):
                    store = self.store / "leases.json"
                    for record in json.loads(store.read_text()) if store.exists() else []:
                        self.cli("release", "--id", record["id"])
                    if fill:
                        self.assertEqual(self.acquire("ng", fill)[1], 0)
                    body, rc = self.acquire("b", size)
                    self.assertEqual(rc == 0, fill + size <= T - S, body)
                    self.assertNotIn("build_class", body.get("lease", {}))
        self.assertEqual(self.qos_actions(), [])


class InteractiveBorrowing(StoreTestCase):
    def test_interactive_borrows_idle_gate_reserve_minus_prompt_reserve(self) -> None:
        body, rc = self.acquire("i1", 14, build_class="interactive", min_cores=2, lending="on")
        self.assertEqual(rc, 0, body)
        # max(N=6, T - G(0) - P(6)) = 8
        self.assertEqual(body["lease"]["lease_size_cores"], 8)
        self.assertTrue(body["lease"]["borrowed"])
        self.assertEqual(body["capacity"]["lent_cores"], 2)

    def test_gate_is_admitted_over_lent_cores_and_preempts_the_borrower(self) -> None:
        self.acquire("i1", 8, build_class="interactive", lending="on")
        # One gate VM fits in the prompt reserve without any preemption.
        body, rc = self.gate("g1", 6, lending="on")
        self.assertEqual(rc, 0, body)
        self.assertEqual(self.qos_actions(), [])
        # A second gate VM: the static model guarantees S=8, i.e. 2 more cores
        # with the borrower's 2 lent cores excluded. 2 fits; 3 overlaps them.
        body, rc = self.gate("g2", 2, lending="on")
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["capacity"]["used_cores"], 16)
        actions = self.qos_actions()
        self.assertEqual([(a["id"], a["flag"]) for a in actions], [("i1", "-b")])
        self.assertEqual(body["qos_actions"], [{"id": "i1", "qos": "background", "pid": os.getpid()}])
        status, _ = self.cli("status", lending="on")
        self.assertEqual(status["capacity"]["preempted_lease_ids"], ["i1"])

        # New non-gate admissions are denied while the gate holds the host.
        denied, rc = self.acquire("i2", 2, build_class="interactive", lending="on")
        self.assertEqual(rc, 75, denied)

        # The gate finishes: the borrower is moved back to normal QoS.
        released, rc = self.cli("release", "--id", "g2", lending="on")
        self.assertEqual(rc, 0, released)
        self.assertEqual([(a["id"], a["flag"]) for a in self.qos_actions()],
                         [("i1", "-b"), ("i1", "-B")])
        status, _ = self.cli("status", lending="on")
        self.assertEqual(status["capacity"]["preempted_lease_ids"], [])

    def test_releasing_a_preempted_borrower_restores_its_qos(self) -> None:
        # The lease owner can outlive its lease (a watch loop), so release must
        # take it back out of background QoS.
        self.acquire("i1", 8, build_class="interactive", lending="on")
        self.gate("g1", 6, lending="on")
        self.gate("g2", 2, lending="on")
        self.assertEqual([(a["id"], a["flag"]) for a in self.qos_actions()], [("i1", "-b")])
        body, rc = self.cli("release", "--id", "i1", lending="on")
        self.assertEqual(rc, 0, body)
        self.assertEqual([(a["id"], a["flag"]) for a in self.qos_actions()],
                         [("i1", "-b"), ("i1", "-B")])

    def test_gate_never_admitted_less_than_static_model_with_borrowers(self) -> None:
        # Fill the guaranteed budget with background work, then borrow.
        self.acquire("b1", 4, build_class="background", lending="on")
        self.acquire("c1", 2, lending="on")
        body, rc = self.acquire("i1", 8, build_class="interactive", min_cores=1, lending="on")
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["lease"]["lease_size_cores"], 2)  # T - P - 6 used
        # Static model: non-gate holds 6, so a gate of S=8 must be admitted.
        body, rc = self.gate("g1", 8, lending="on")
        self.assertEqual(rc, 0, body)
        body, rc = self.gate("g2", 1, lending="on")
        self.assertEqual(rc, 75, body)

    def test_newest_borrower_yields_first(self) -> None:
        self.acquire("i-old", 4, build_class="interactive", lending="on", prompt=0)
        self.acquire("i-new", 6, build_class="interactive", lending="on", prompt=0)
        # non-gate 10 = N 6 + lent 4, all attributed to i-new.
        body, rc = self.gate("g1", 8, lending="on", prompt=0)
        self.assertEqual(rc, 0, body)
        self.assertEqual([(a["id"], a["flag"]) for a in self.qos_actions()], [("i-new", "-b")])

    def test_turning_lending_off_keeps_existing_borrowers_preemptible(self) -> None:
        self.acquire("i1", 8, build_class="interactive", lending="on")
        body, rc = self.gate("g1", 8, lending="off")
        self.assertEqual(rc, 0, body)
        self.assertEqual([(a["id"], a["flag"]) for a in self.qos_actions()], [("i1", "-b")])


class StatusReportsClassAvailability(StoreTestCase):
    def test_status_reports_what_each_class_could_get(self) -> None:
        self.acquire("b1", 3, build_class="background", lending="on")
        status, _ = self.cli("status", lending="on")
        cap = status["capacity"]
        # interactive: max(N=6, 14-0-6=8) - 3 used = 5; background: share 4 - 3 = 1
        self.assertEqual(cap["interactive_available_cores"], 5)
        self.assertEqual(cap["background_available_cores"], 1)
        status, _ = self.cli("status", lending="off")
        self.assertEqual(status["capacity"]["interactive_available_cores"], 3)


class BackgroundClass(StoreTestCase):
    def test_background_is_capped_at_its_share_and_never_borrows(self) -> None:
        body, rc = self.acquire("b1", 6, build_class="background", min_cores=1, lending="on")
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["lease"]["lease_size_cores"], 4)
        body, rc = self.acquire("b2", 2, build_class="background", lending="on")
        self.assertEqual(rc, 75, body)
        self.assertTrue(body["class_share_exceeded"])

    def test_two_validations_leave_room_for_an_awaited_build(self) -> None:
        # The 2026-09-30 MacBook incident: two validations held all six
        # non-gate cores and the awaited build got nothing.
        self.acquire("val-1", 6, build_class="background", min_cores=1)
        second, rc = self.acquire("val-2", 6, build_class="background", min_cores=1,
                                  allow_floor=True)
        self.assertEqual(rc, 0, second)
        self.assertTrue(second["floor"])  # the second validation is floored
        awaited, rc = self.acquire("spectr", 14, build_class="interactive", min_cores=2)
        self.assertEqual(rc, 0, awaited)
        self.assertEqual(awaited["lease"]["lease_size_cores"], 2)
        self.assertFalse(awaited["floor"])

    def test_interactive_is_never_floored(self) -> None:
        self.acquire("ng", 6)
        body, rc = self.acquire("i1", 4, build_class="interactive", allow_floor=True)
        self.assertEqual(rc, 75, body)
        self.assertEqual(body["build_class"], "interactive")


class PromptReserve(StoreTestCase):
    def test_auto_reserve_learns_last_gate_lease_size(self) -> None:
        cfg = {"gate_prompt_reserve_cores": 6, "gate_prompt_reserve_auto": 1,
               "serves_gate": 1, "reserved_gate_cores": 8, "gate_priority": 100}
        self.store.mkdir(parents=True)
        self.assertEqual(leases.prompt_reserve(self.store, cfg), (6, "profile_default"))
        leases.write_gate_hint(self.store, 3)
        self.assertEqual(leases.prompt_reserve(self.store, cfg), (3, "last_gate_lease"))
        cfg["serves_gate"] = 0
        self.assertEqual(leases.prompt_reserve(self.store, cfg)[0], 6)

    def test_gate_admission_records_the_hint(self) -> None:
        self.gate("g1", 5)
        self.assertEqual(leases.read_gate_hint(self.store), 5)


class WaitAndPartial(StoreTestCase):
    def test_wait_retries_then_reports_attempts(self) -> None:
        self.acquire("ng", 6)
        with mock.patch.object(leases, "WAIT_POLL_SECS", 0.05):
            args = leases.parse_args([
                "acquire", "--id", "i1", "--cores", "2", "--class", "interactive",
                "--wait-secs", "1", "--store-dir", str(self.store), "--capacity", str(T),
                "--reserved-gate-cores", str(S), "--capacity-mem-mb", "0",
                "--dynamic-lending", "off", "--pid", str(os.getpid()),
            ])
            with mock.patch.dict(os.environ, self.env, clear=True):
                result, rc = leases.acquire_with_wait(args)
        self.assertEqual(rc, 75)
        self.assertGreater(result["wait_attempts"], 1)


class GovernorConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.gov = root / "governor.toml"
        self.fleet = root / "fleet.toml"
        self.patch = mock.patch.dict(os.environ, {
            "TARTCI_GOVERNOR_FILE": str(self.gov),
            "TARTCI_FLEET_PROFILE": str(self.fleet),
            "TARTCI_ROLE_FILE": str(root / "absent-role"),
        })
        self.patch.start()
        for key in list(os.environ):
            if key.startswith("TARTCI_GOV_") or key in ("TARTCI_ROLE", "TARTCI_AGENT_FLOOR_CORES"):
                os.environ.pop(key)

    def tearDown(self) -> None:
        self.patch.stop()
        self.tmp.cleanup()

    def profile(self, **kw) -> dict:
        return host_profile.build_profile(cores=18, memory_mb=131072, model="Mac17,7", **kw)

    def test_defaults_keep_static_numbers_and_lending_off(self) -> None:
        p = self.profile(role="dev-overflow")
        self.assertEqual((p["lease_capacity_cores"], p["reserved_gate_cores"]), (14, 8))
        self.assertFalse(p["dynamic_lending"])
        self.assertFalse(p["serves_gate"])
        self.assertEqual(p["gate_prompt_reserve_cores"], 0)
        self.assertEqual(p["background_share_cores"], 4)
        self.assertEqual(p["interactive_qos"], "normal")

    def test_gate_lane_in_fleet_profile_sets_one_vm_prompt_reserve(self) -> None:
        self.fleet.write_text('[host]\nid = "m5"\n\n[[lane]]\nid = "pulp-gate"\n')
        p = self.profile(role="dev-overflow")
        self.assertTrue(p["serves_gate"])
        self.assertEqual(p["gate_prompt_reserve_cores"], 6)

    def test_role_defaults_for_background_share(self) -> None:
        self.assertEqual(self.profile(role="dedicated-builder")["background_share_cores"], 6)
        self.assertEqual(self.profile(role="light")["background_share_cores"], 2)

    def test_governor_file_drives_the_profile(self) -> None:
        governor.write_governor_file(self.gov, {
            "role": "dedicated-builder", "human_reserved_cores": 3,
            "gate_guarantee_cores": 9, "dynamic_lending": True,
            "background_share_cores": 2, "agent_floor_cores": 4,
        })
        p = self.profile()
        self.assertEqual(p["role"], "dedicated-builder")
        self.assertTrue(p["role_source"].startswith("file:"))
        self.assertEqual(p["headroom_cores"], 3)
        self.assertEqual(p["lease_capacity_cores"], 15)
        self.assertEqual(p["reserved_gate_cores"], 9)
        self.assertEqual(p["non_gate_capacity_cores"], 6)
        self.assertTrue(p["dynamic_lending"])
        self.assertEqual(p["background_share_cores"], 2)
        self.assertEqual(p["agent_floor_cores"], 4)
        exports = host_profile.shell_exports(p)
        self.assertIn("TARTCI_DYNAMIC_LENDING=1", exports)
        self.assertIn("TARTCI_GOVERNOR_SCHEMA=1", exports)

    def test_environment_overrides_file_and_bad_values_are_ignored(self) -> None:
        self.gov.write_text('[governor]\ndynamic_lending = true\nbackground_share_cores = "lots"\n')
        os.environ["TARTCI_GOV_DYNAMIC_LENDING"] = "0"
        try:
            p = self.profile(role="dev-overflow")
        finally:
            os.environ.pop("TARTCI_GOV_DYNAMIC_LENDING")
        self.assertFalse(p["dynamic_lending"])
        self.assertEqual(p["background_share_cores"], 4)
        self.assertTrue(any("background_share_cores" in x for x in p["governor_problems"]))

    def test_unparseable_file_falls_back_to_defaults_and_says_so(self) -> None:
        baseline = self.profile(role="dev-overflow")
        self.gov.write_text("[governor\ndynamic_lending = true\nrole = \"light\"\n")
        broken = self.profile(role="dev-overflow")
        if sys.version_info >= (3, 11):
            self.assertTrue(any(x.startswith("governor_file_unparseable:")
                                for x in broken["governor_problems"]), broken["governor_problems"])
        for key in ("lease_capacity_cores", "reserved_gate_cores", "non_gate_capacity_cores",
                    "dynamic_lending", "background_share_cores", "agent_floor_cores"):
            self.assertEqual(broken[key], baseline[key], key)
        if sys.version_info >= (3, 11):
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                self.assertEqual(governor.cmd_set(["background_share_cores=3"]), 2)
            self.assertIn("[governor\n", self.gov.read_text())  # not overwritten

    def test_set_validates_and_round_trips(self) -> None:
        with mock.patch("sys.stdout"):
            self.assertEqual(governor.cmd_set(["dynamic_lending=true", "background_share_cores=3"]), 0)
            self.assertEqual(governor.cmd_set(["no_such_knob=1"]), 2)
            self.assertEqual(governor.cmd_set(["interactive_share_cores=-1"]), 2)
        self.assertEqual(governor.file_values(self.gov),
                         {"dynamic_lending": True, "background_share_cores": 3})
        with mock.patch("sys.stdout"):
            governor.cmd_set(["background_share_cores"], unset=True)
        self.assertEqual(governor.file_values(self.gov), {"dynamic_lending": True})

    def test_governor_cli_show_json(self) -> None:
        governor.write_governor_file(self.gov, {"role": "light"})
        proc = subprocess.run([sys.executable, str(GOVERNOR), "show", "--json"],
                              text=True, capture_output=True, check=False, env=dict(os.environ))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(proc.stdout)
        self.assertEqual(body["derived"]["role"], "light")
        self.assertIn("dynamic_lending", body["knobs"])


if __name__ == "__main__":
    unittest.main()
