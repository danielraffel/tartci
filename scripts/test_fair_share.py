#!/usr/bin/env python3
"""Fair share between repositories for the next VM (scripts/fair_share.py).

The shape is m5's on 2026-11-10: a 14-core lease universe whose non-gate
budget is 6 cores, a 2-core agent build in that budget, and Pulp gate VMs
(priority 110) refilling every VM the host frees, while a Vellum lane
(priority "vm", 60) with a 6-core lease waits behind them for hours.
"""

from __future__ import annotations

import testing_support  # noqa: E402,F401
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import fair_share

LEASES = Path(__file__).with_name("leases.py")
LEASE_FIT = Path(__file__).with_name("lease_fit.py")
ROOT = Path(__file__).resolve().parents[1]
VM_LEASE_LIB = ROOT / "providers/common/vm-lease.lib.sh"
LEASE_FIT_LIB = ROOT / "providers/tart-macos/lease-fit.lib.sh"
RUNNER = ROOT / "providers/tart-macos/runner.sh"


def runner_function(name: str) -> str:
    import re
    text = RUNNER.read_text()
    match = re.search(rf"^{name}\(\)\{{\n.*?^\}}$", text, re.MULTILINE | re.DOTALL)
    assert match, f"missing {name} in runner.sh"
    return match.group(0)

CAPACITY = 14
RESERVED = 8  # non-gate budget = 6
VM_KIND = "tart-macos-vm"
PULP = "Generous-Corp/pulp"
VELLUM = "Generous-Corp/vellum"
AFTER = 1800


class FairShareCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "leases"
        self.profile = root / "fleet.toml"
        self.disk = root / "vms"
        self.disk.mkdir()
        self.env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("TARTCI_RANK_VM_WAITERS", "TARTCI_VM_WAITER_FRESH_SECS",
                                 "TARTCI_FAIR_SHARE_AFTER_SECS"))
        }
        self.env["TARTCI_FLEET_PROFILE"] = str(self.profile)
        self.knobs()
        self.now = int(time.time())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def knobs(self, *, fair: int = 0, rank: bool = False) -> None:
        self.profile.write_text(
            'schema = 1\n[host]\nid = "test"\n[leases]\n'
            f'rank_vm_waiters = {"true" if rank else "false"}\n'
            f"fair_share_after_secs = {fair}\n",
            encoding="utf-8",
        )

    def common(self) -> list[str]:
        return ["--store-dir", str(self.store), "--capacity", str(CAPACITY),
                "--reserved-gate-cores", str(RESERVED), "--capacity-mem-mb", "0",
                "--agent-floor-cores", "0"]

    def run_json(self, script: Path, *args: str) -> tuple[int, dict]:
        proc = subprocess.run([sys.executable, str(script), *args, "--json"],
                              text=True, capture_output=True, check=False, env=self.env)
        try:
            return proc.returncode, json.loads(proc.stdout)
        except ValueError:
            self.fail(f"{args}: rc={proc.returncode} out={proc.stdout} err={proc.stderr}")

    def leases(self, *args: str) -> tuple[int, dict]:
        return self.run_json(LEASES, *args, *self.common())

    def agent_build(self, cores: int = 2) -> None:
        rc, body = self.leases("acquire", "--id", "agent-build", "--cores", str(cores),
                               "--priority", "40", "--kind", "pulp-governed-build",
                               "--pid", str(os.getpid()))
        self.assertEqual(rc, 0, body)

    def vm(self, lease_id: str, priority: str, repo: str, *, since: int = 0,
           waiter: str = "", slot_cap: int = 0, cores: int = 6) -> tuple[int, dict]:
        args = ["acquire", "--id", lease_id, "--cores", str(cores), "--priority", priority,
                "--kind", VM_KIND, "--pid", str(os.getpid()), "--disk-path", str(self.disk),
                "--repo", repo, "--demand-since", str(since)]
        if waiter:
            args += ["--waiter-id", waiter]
        if slot_cap:
            args += ["--vm-slot-cap", str(slot_cap)]
        return self.leases(*args)

    def wait(self, waiter: str, priority: str, repo: str, since: int,
             cores: int = 6) -> tuple[int, dict]:
        return self.leases("wait", "--id", waiter, "--cores", str(cores),
                           "--priority", priority, "--kind", VM_KIND,
                           "--lane", waiter, "--pid", str(os.getpid()),
                           "--repo", repo, "--demand-since", str(since))

    def release(self, lease_id: str) -> None:
        rc, body = self.leases("release", "--id", lease_id)
        self.assertEqual(rc, 0, body)

    def fit(self, priority: str, repo: str, since: int = 0, slot_cap: int = 0,
            cores: int = 6) -> tuple[int, dict]:
        args = ["--cores", str(cores), "--priority", priority, "--repo", repo,
                "--demand-since", str(since), *self.common()]
        if slot_cap:
            args += ["--vm-slot-cap", str(slot_cap)]
        return self.run_json(LEASE_FIT, *args)

    def contended_host(self) -> None:
        """An agent build in the non-gate budget and two Pulp VMs; one ends."""
        self.agent_build()
        self.assertEqual(self.vm("vm-pulp-a", "110", PULP)[0], 0)
        self.assertEqual(self.vm("vm-pulp-b", "110", PULP)[0], 0)
        self.release("vm-pulp-a")


class TwoReposContendTests(FairShareCase):
    def test_without_fair_share_pulp_takes_every_freed_vm(self) -> None:
        """The starvation, reproduced: off, a starved Vellum lane never fits."""
        self.contended_host()
        starved = self.now - 4 * 3600
        self.assertEqual(self.wait("waiter-vellum", "vm", VELLUM, starved)[0], 0)
        rc, body = self.vm("vm-vellum", "vm", VELLUM, since=starved, waiter="waiter-vellum")
        self.assertEqual((rc, body["reason"]), (75, "capacity_exceeded"))
        rc, body = self.vm("vm-pulp-c", "110", PULP)
        self.assertEqual(rc, 0, body)

    def test_a_starved_lane_of_another_repo_gets_the_next_vm(self) -> None:
        self.knobs(fair=AFTER)
        self.contended_host()
        starved = self.now - 4 * 3600
        rc, body = self.wait("waiter-vellum", "vm", VELLUM, starved)
        self.assertTrue(body["registered"], body)
        # Pulp's next acquire yields the freed cores to the starved Vellum lane.
        rc, body = self.vm("vm-pulp-c", "110", PULP)
        self.assertEqual((rc, body["reason"]), (75, "deferred_to_waiter"), body)
        self.assertEqual(body["waiter"]["repo"], VELLUM)
        # Vellum is admitted at gate priority, against the host budget.
        rc, body = self.vm("vm-vellum", "vm", VELLUM, since=starved, waiter="waiter-vellum")
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["lease"]["priority"], 100)
        self.assertEqual(body["lease"]["fair_share_promoted_from"], 60)
        self.assertEqual(body["lease"]["repo"], VELLUM)
        # The grant withdrew Vellum's waiter: Pulp is no longer held back.
        self.release("vm-vellum")
        self.assertEqual(self.vm("vm-pulp-c", "110", PULP)[0], 0)

    def test_a_lane_not_yet_starved_changes_nothing(self) -> None:
        self.knobs(fair=AFTER)
        self.contended_host()
        recent = self.now - 60
        self.assertEqual(self.wait("waiter-vellum", "vm", VELLUM, recent)[0], 0)
        rc, body = self.vm("vm-vellum", "vm", VELLUM, since=recent, waiter="waiter-vellum")
        self.assertEqual((rc, body["reason"]), (75, "capacity_exceeded"))
        self.assertEqual(self.vm("vm-pulp-c", "110", PULP)[0], 0)

    def test_the_last_free_vm_slot_is_kept_for_a_starved_lane(self) -> None:
        """m5s: the VM slot cap binds before cores do."""
        self.knobs(fair=AFTER)
        self.assertEqual(self.vm("vm-pulp-a", "110", PULP, cores=4)[0], 0)
        starved = self.now - 4 * 3600
        self.assertEqual(self.wait("waiter-vellum", "vm", VELLUM, starved, cores=4)[0], 0)
        rc, body = self.vm("vm-pulp-b", "110", PULP, slot_cap=2, cores=4)
        self.assertEqual((rc, body["reason"]), (75, "deferred_to_waiter"), body)
        rc, body = self.vm("vm-vellum", "vm", VELLUM, since=starved, waiter="waiter-vellum",
                           slot_cap=2, cores=4)
        self.assertEqual(rc, 0, body)

    def test_the_longest_starved_repo_goes_first(self) -> None:
        cfg = {"fair_share_after_secs": AFTER, "gate_priority": 100}
        waiters = [
            {"id": "w-forge", "command_kind": VM_KIND, "repo": "Generous-Corp/forge",
             "demand_since": self.now - 3 * 3600},
            {"id": "w-vellum", "command_kind": VM_KIND, "repo": VELLUM,
             "demand_since": self.now - 5 * 3600},
        ]
        rivals = fair_share.starved_rivals(cfg, waiters, repo=PULP, since=0, now=self.now)
        self.assertEqual([w["id"] for w in rivals], ["w-vellum", "w-forge"])
        # A starved requester only yields to repos starved longer than itself.
        rivals = fair_share.starved_rivals(cfg, waiters, repo="Generous-Corp/forge",
                                           since=self.now - 3 * 3600, now=self.now)
        self.assertEqual([w["id"] for w in rivals], ["w-vellum"])


class OneRepoOrSameRepoTests(FairShareCase):
    def test_a_lane_alone_on_the_host_is_admitted_exactly_as_before(self) -> None:
        """Only Vellum has demand: no promotion, the same answer as with it off."""
        results = []
        for fair in (0, AFTER):
            self.knobs(fair=fair)
            self.agent_build(cores=4)
            starved = self.now - 4 * 3600
            self.wait("waiter-vellum", "vm", VELLUM, starved)
            rc, body = self.vm("vm-vellum", "vm", VELLUM, since=starved, waiter="waiter-vellum")
            results.append((rc, body.get("reason"), body.get("priority")))
            self.leases("withdraw", "--id", "waiter-vellum")
            self.release("agent-build")
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], (75, "capacity_exceeded", 60))

    def test_fair_share_never_reorders_one_repository(self) -> None:
        """A starved Pulp PR-head waiter does not hold back a Pulp merge group."""
        self.knobs(fair=AFTER)
        self.contended_host()
        starved = self.now - 4 * 3600
        self.assertEqual(self.wait("waiter-pulp-pr", "100", PULP, starved)[0], 0)
        rc, body = self.vm("vm-pulp-mg", "110", PULP)
        self.assertEqual(rc, 0, body)
        self.assertNotIn("fair_share_promoted_from", body["lease"])


class LeaseFitTests(FairShareCase):
    def test_a_lane_that_would_displace_a_starved_repo_yields_before_scanning(self) -> None:
        self.knobs(fair=AFTER)
        self.contended_host()
        self.wait("waiter-vellum", "vm", VELLUM, self.now - 4 * 3600)
        rc, body = self.fit("110", PULP)
        self.assertEqual((rc, body["verdict"]), (5, "yield"), body)
        self.assertEqual(body["yield_to"]["repo"], VELLUM)
        # Control: with the knob off the same lane fits now.
        self.knobs(fair=0)
        rc, body = self.fit("110", PULP)
        self.assertEqual((rc, body["verdict"]), (0, "fits_now"), body)

    def test_a_starved_lane_is_asked_about_at_gate_priority(self) -> None:
        self.knobs(fair=AFTER)
        self.contended_host()
        rc, body = self.fit("vm", VELLUM, since=self.now - 60)
        self.assertEqual((rc, body["verdict"]), (3, "not_now"), body)
        rc, body = self.fit("vm", VELLUM, since=self.now - 4 * 3600)
        self.assertEqual((rc, body["verdict"]), (0, "fits_now"), body)
        self.assertEqual(body["priority"], 100)


class ShellClockTests(unittest.TestCase):
    def bash(self, body: str, knob: str = "1800") -> subprocess.CompletedProcess:
        script = (
            "set -euo pipefail\n"
            f"TARTCI_ROOT={ROOT}\nREPO={VELLUM}\n"
            "note(){ echo \"NOTE $*\"; }\nevent(){ echo \"EVENT $*\"; }\n"
            "heartbeat(){ echo \"HEARTBEAT $*\"; }\n"
            f"source {VM_LEASE_LIB}\nsource {LEASE_FIT_LIB}\n"
            f"tartci_profile_value(){{ case \"$1\" in fair_share_after_secs) echo {knob};; *) echo False;; esac; }}\n"
            "tartci_fair_share_enabled || true\n" + body
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)

    def test_clock_starts_on_demand_survives_blind_scans_and_resets(self) -> None:
        proc = self.bash(
            'tartci_fair_share_demand_seen 3 L; s1=$TARTCI_FAIR_DEMAND_SINCE; echo "s1=$s1"\n'
            'tartci_fair_share_demand_seen ERR L; [ "$TARTCI_FAIR_DEMAND_SINCE" = "$s1" ] && echo kept\n'
            'tartci_fair_share_starved && echo starved || echo fresh\n'
            'TARTCI_FAIR_DEMAND_SINCE=$(( $(date +%s) - 1800 )); tartci_fair_share_starved && echo starved\n'
            'tartci_fair_share_args | tr "\\n" " "; echo\n'
            'tartci_fair_share_demand_seen 0 L; echo "reset=$TARTCI_FAIR_DEMAND_SINCE"\n')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        self.assertNotIn("s1=0", out)
        self.assertIn("kept", out)
        self.assertIn("fresh", out)
        self.assertIn("starved", out)
        self.assertIn(f"--repo {VELLUM} --demand-since ", out)
        self.assertIn("reset=0", out)

    def test_knob_off_passes_nothing_and_never_starves(self) -> None:
        proc = self.bash(
            'tartci_fair_share_demand_seen 3 L; echo "since=$TARTCI_FAIR_DEMAND_SINCE"\n'
            'echo "args=[$(tartci_fair_share_args)]"\n', knob="0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("since=0", proc.stdout)
        self.assertIn("args=[]", proc.stdout)

    def test_a_lane_held_by_the_lease_budget_starts_its_clock_with_one_scan(self) -> None:
        """Without the probe a lease-held lane never scans, so never starves."""
        body = (
            "FAIR_SHARE_PROBE_AT=0; SCANS=0\n"
            "tartci_heartbeat_keepalive_start(){ :; }\ntartci_heartbeat_keepalive_stop(){ :; }\n"
            "select_work(){ echo '2|self-hosted,vellum|0'; }\n"
            + runner_function("fair_share_demand_probe") + "\n"
            "LEASE_FIT_LAST_VERDICT=not_now\n"
            "fair_share_demand_probe; echo \"since=$TARTCI_FAIR_DEMAND_SINCE\"\n"
            "first=$TARTCI_FAIR_DEMAND_SINCE; TARTCI_FAIR_DEMAND_SINCE=0\n"
            "fair_share_demand_probe; echo \"throttled=$TARTCI_FAIR_DEMAND_SINCE\"\n"
            "FAIR_SHARE_PROBE_AT=0; LEASE_FIT_LAST_VERDICT=fits_now\n"
            "fair_share_demand_probe; echo \"fits=$TARTCI_FAIR_DEMAND_SINCE\"\n"
        )
        proc = self.bash(body)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("since=0", proc.stdout)
        self.assertIn("EVENT fair_share_demand_probe queued=2", proc.stdout)
        self.assertIn("throttled=0", proc.stdout)
        self.assertIn("fits=0", proc.stdout)

    def test_lease_fit_yield_waits_without_scanning(self) -> None:
        proc = self.bash(
            'tartci_vm_leases_enabled(){ return 0; }\n'
            "tartci_lease_fit_probe(){ echo '{\"requested_cores\":6,\"yield_to\":{\"lane\":\"m5-vellum-gate\",\"repo\":\"Generous-Corp/vellum\"}}'; return 5; }\n"
            'tartci_lease_fit_gate && echo GO || echo WAIT\n')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WAIT", proc.stdout)
        self.assertIn("EVENT fair_share_yield", proc.stdout)
        self.assertIn("yield_to=m5-vellum-gate repo=Generous-Corp/vellum", proc.stdout)
        self.assertIn("HEARTBEAT lease-wait", proc.stdout)


if __name__ == "__main__":
    unittest.main()
