#!/usr/bin/env python3
"""PR-class cap while a repository's merge group is queued.

Properties covered, each from the side that could break:

* capped: with merge-group work queued, a host that already runs N PR-class VMs
  of the repository starts no new one (and logs the repo, host, running count
  and the oldest queued_at);
* uncapped: with no merge-group work queued, or fewer than N running, it claims;
* scope: another repository's PR-class VMs, and this repository's merge-group
  VMs, do not count, and a tier that is not PR-class is never gated;
* fail open: unreadable or stale demand, an unreadable lease store and a broken
  helper all claim;
* stale demand: merge-group work queued past the bound stops capping;
* the bash glue runs under macOS /bin/bash 3.2 with `set -euo pipefail`;
* the profile validator and renderer.

Compatible with the hosts' Python 3.9 (the validator tests need tomllib).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import testing_support

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CAP = SCRIPTS / "pr_class_cap.py"
LIB = ROOT / "providers/tart-macos/pr-class-cap.lib.sh"
sys.path.insert(0, str(SCRIPTS))

import leases  # noqa: E402
import pr_class_cap  # noqa: E402
import slot_demand  # noqa: E402

BASH32 = "/bin/bash"
VELLUM = "Generous-Corp/vellum"
VELLUM_PR = "vellum-build-macos"
VELLUM_MG = "vellum-build-macos-merge-group"
PULP_PR = "pulp-build-pr"


def ago(seconds: int) -> str:
    return slot_demand.iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = self.root / "leases"
        self.store.mkdir()
        self.detail = self.root / "lane.slot-demand.mg.json"
        self.n = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_detail(self, classes: dict, written_ago: int = 5) -> None:
        self.detail.write_text(json.dumps({
            "schema": slot_demand.SCHEMA, "written_at": ago(written_ago),
            "witness_only": False, "classes": classes}))

    def mg_queued(self, queued_ago: int = 120, count: int = 1, **kw) -> None:
        self.write_detail({"merge_group": {"count": count,
                                           "oldest_created_at": ago(queued_ago)}}, **kw)

    def leases(self, *labels: str) -> None:
        """Live VM leases (owned by this process) carrying these class labels."""
        identity = leases.process_identity(os.getpid())
        records = []
        for label in labels:
            self.n += 1
            records.append({
                "id": f"lease-{self.n}", "command_kind": "tart-macos-vm",
                "label": f"self-hosted,macOS,ARM64,{label}", "lease_size_cores": 4,
                "lease_size_mem_mb": 8192, "priority": 50, "priority_class": "vm",
                "pid": identity["pid"], "process_start_time": identity["process_start_time"],
                "host_boot_time": identity["host_boot_time"],
                "created_at": ago(60), "heartbeat_at": ago(5),
            })
        leases.write_records(self.store, records)

    def decide(self, cap: int = 1, pr_labels: str = VELLUM_PR, **kw) -> dict:
        argv = ["--repo", VELLUM, "--host", "m5", "--cap", str(cap), "--pr-labels", pr_labels,
                "--mg-detail-file", str(self.detail), "--store-dir", str(self.store)]
        for key, value in kw.items():
            argv += [f"--{key.replace('_', '-')}", str(value)]
        return pr_class_cap.decide(pr_class_cap.parse_args(argv))


class DecisionTests(Base):
    def test_capped_while_merge_group_is_queued(self) -> None:
        self.mg_queued(queued_ago=300)
        self.leases(VELLUM_PR)
        result = self.decide(cap=1)
        self.assertEqual(result["decision"], "cap")
        self.assertEqual((result["repo"], result["host"], result["running"]),
                         (VELLUM, "m5", 1))
        self.assertEqual(result["mg_oldest_queued_at"], json.loads(
            self.detail.read_text())["classes"]["merge_group"]["oldest_created_at"])
        line = pr_class_cap.render(result)
        for token in (f"repo={VELLUM}", "host=m5", "running=1", "mg_oldest_queued_at="):
            self.assertIn(token, line)

    def test_uncapped_when_no_merge_group_is_queued(self) -> None:
        self.write_detail({"pull_request": {"count": 3, "oldest_created_at": ago(60)}})
        self.leases(VELLUM_PR, VELLUM_PR)
        self.assertEqual(self.decide(cap=1)["decision"], "claim")
        self.write_detail({})
        self.assertEqual(self.decide(cap=1)["decision"], "claim")

    def test_a_host_below_the_cap_claims(self) -> None:
        self.mg_queued()
        self.leases()
        self.assertEqual(self.decide(cap=1)["decision"], "claim")
        self.leases(VELLUM_PR)
        self.assertEqual(self.decide(cap=2)["decision"], "claim")
        self.assertEqual(self.decide(cap=1)["decision"], "cap")

    def test_other_repositories_and_merge_group_vms_are_not_counted(self) -> None:
        self.mg_queued()
        self.leases(PULP_PR, PULP_PR, VELLUM_MG)
        result = self.decide(cap=1)
        self.assertEqual(result["decision"], "claim")
        self.assertEqual(result["running"], 0)
        # Another repository's lane has its own labels and its own cap.
        self.leases(VELLUM_PR)
        self.assertEqual(self.decide(cap=1, pr_labels=PULP_PR)["decision"], "claim")

    def test_off_by_default(self) -> None:
        self.mg_queued()
        self.leases(VELLUM_PR)
        self.assertEqual(self.decide(cap=0)["reason"], "cap_off")

    def test_unreadable_demand_does_not_cap(self) -> None:
        self.leases(VELLUM_PR)
        # Missing file, then a corrupt one, then the wrong schema.
        self.assertEqual(self.decide(cap=1)["decision"], "claim")
        self.detail.write_text("{not json")
        self.assertEqual(self.decide(cap=1)["decision"], "claim")
        self.detail.write_text(json.dumps({"schema": 99, "written_at": ago(1), "classes": {}}))
        self.assertEqual(self.decide(cap=1)["decision"], "claim")
        self.write_detail({"merge_group": {"count": 2, "oldest_created_at": "garbage"}})
        self.assertEqual(self.decide(cap=1)["reason"], "demand_age_unknown")

    def test_unreadable_lease_store_does_not_cap(self) -> None:
        self.mg_queued()
        (self.store / "leases.json").write_text("{not json")
        # Control: the same demand and a readable store cap.
        self.assertTrue(leases.store_file(self.store).exists())
        self.assertEqual(self.decide(cap=1)["reason"], "lease_store_unreadable")
        self.leases(VELLUM_PR)
        self.assertEqual(self.decide(cap=1)["decision"], "cap")

    def test_a_stale_detail_file_does_not_cap(self) -> None:
        self.leases(VELLUM_PR)
        self.mg_queued(written_ago=3600)
        self.assertEqual(self.decide(cap=1)["decision"], "claim")
        self.mg_queued(written_ago=10)
        self.assertEqual(self.decide(cap=1)["decision"], "cap")

    def test_merge_group_queued_past_the_bound_stops_capping(self) -> None:
        self.leases(VELLUM_PR)
        self.mg_queued(queued_ago=7200)
        result = self.decide(cap=1)
        self.assertEqual((result["decision"], result["reason"]), ("claim", "demand_stale"))
        self.mg_queued(queued_ago=7200)
        self.assertEqual(self.decide(cap=1, max_wait_secs=10800)["decision"], "cap")

    def test_a_broken_helper_claims(self) -> None:
        out = subprocess.run([sys.executable, str(CAP), "--cap", "notanumber"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertTrue(out.stdout.startswith("claim"), out.stdout)


class GlueTests(Base):
    """providers/tart-macos/pr-class-cap.lib.sh under /bin/bash 3.2, set -euo pipefail."""

    TIERS = f"vellum-sterile-consumer\n{VELLUM_MG}\n{VELLUM_PR}"

    def gate(self, tier: int, *, cap: str = "1", tiers: str | None = None) -> tuple:
        log = self.root / "events.log"
        script = f"""set -euo pipefail
TARTCI_ROOT={ROOT}
REPO={VELLUM}
WORKFLOW_TIERS=x
ASSIGNMENT_MODE=legacy
TIER_LABELS_CONFIG='{tiers if tiers is not None else self.TIERS}'
STATE_DIR={self.root}
RUNNER_NAME=lane
TARTCI_PR_CLASS_CAP_WHILE_MG={cap}
event(){{ printf '%s\\n' "$*" >> {log}; }}
tartci_claim_delay_class(){{
  local want="$1" i=0 l
  while IFS= read -r l; do
    [ "$i" = "$want" ] && {{ printf '%s\\n' "$l"; return 0; }}; i=$((i+1))
  done <<< "$TIER_LABELS_CONFIG"; return 1
}}
tartci_slot_priority_detail_file(){{ printf '{self.detail}\\n'; }}
source {LIB}
rc=0
tartci_pr_class_cap_gate "self-hosted,$(tartci_claim_delay_class {tier})" {tier} || rc=$?
echo "rc=$rc"
"""
        env = {**os.environ, "TARTCI_LEASE_DIR": str(self.store)}
        out = subprocess.run([BASH32, "-c", script], capture_output=True, text=True, env=env)
        events = log.read_text() if log.exists() else ""
        return out.stdout.strip(), out.stderr, events

    def run_gate(self, tier: int, **kw) -> tuple:
        # The helper reads the store given by --store-dir's default, which honours this env.
        os.environ["TARTCI_LEASE_DIR"] = str(self.store)
        try:
            return self.gate(tier, **kw)
        finally:
            os.environ.pop("TARTCI_LEASE_DIR", None)

    def test_the_pr_class_claim_is_capped_and_logged(self) -> None:
        self.mg_queued()
        self.leases(VELLUM_PR)
        out, err, events = self.run_gate(2)
        self.assertEqual(out, "rc=75", err)
        self.assertIn("pr_class_capped", events)
        self.assertIn("running=1", events)

    def test_off_merge_group_and_other_tiers_are_never_gated(self) -> None:
        self.mg_queued()
        self.leases(VELLUM_PR)
        self.assertEqual(self.run_gate(2, cap="0")[0], "rc=0")
        self.assertEqual(self.run_gate(1)[0], "rc=0")  # the merge-group tier
        self.assertEqual(self.run_gate(0)[0], "rc=0")  # the tier before it
        self.assertEqual(self.run_gate(2, tiers=f"{VELLUM_PR}")[0], "rc=0")  # no MG tier

    def test_an_unreadable_demand_file_claims(self) -> None:
        self.leases(VELLUM_PR)
        self.assertEqual(self.run_gate(2)[0], "rc=0")


class RunnerWiringTests(unittest.TestCase):
    def test_the_runner_gates_the_claim_on_the_cap(self) -> None:
        text = (ROOT / "providers/tart-macos/runner.sh").read_text()
        self.assertIn('source "$TARTCI_ROOT/providers/tart-macos/pr-class-cap.lib.sh"', text)
        self.assertIn('tartci_pr_class_cap_gate "$selected_labels" "$selected_tier" || pc=1', text)
        # A capped poll must not reach the claim, and the tier scan must write
        # the detail file the gate reads even with slot priority off.
        self.assertIn('[ "$pc" -eq 0 ] && [ "$sp" -eq 0 ] && resv="$(tartci_warm_or_claim_slot', text)
        self.assertIn("tartci_slot_priority_enabled || tartci_pr_class_cap_enabled", text)
        self.assertIn("reason=pr_class_cap", text)


@testing_support.requires_tomllib
class ProfileTests(unittest.TestCase):
    PATH = ROOT / "profiles" / "m5studio-macos-fleet.toml"
    KEY = "pr_class_cap_while_merge_group_queued"

    def load(self, body: str) -> dict:
        import macos_fleet_lanes as fleet
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m5studio.toml"
            path.write_text(body)
            return fleet.load(path)

    def with_cap(self, value: str = "1") -> str:
        body = self.PATH.read_text()
        anchor = 'priority = "vm"\n'
        self.assertEqual(body.count(anchor), 1)
        return body.replace(anchor, anchor + f"{self.KEY} = {value}\n", 1)

    def test_no_shipped_profile_enables_it(self) -> None:
        for path in (ROOT / "profiles").glob("*-macos-fleet.toml"):
            self.assertNotIn(self.KEY, path.read_text(), path.name)

    def test_the_lane_renders_the_env_and_others_do_not(self) -> None:
        import macos_fleet_lanes as fleet
        data = self.load(self.with_cap("1"))
        seen = 0
        for body in fleet.rendered_plists(data).values():
            env = plistlib.loads(body)["EnvironmentVariables"]
            if env.get("TARTCI_RUNNER_REPO") == VELLUM:
                seen += 1
                self.assertEqual(env["TARTCI_PR_CLASS_CAP_WHILE_MG"], "1")
            else:
                self.assertNotIn("TARTCI_PR_CLASS_CAP_WHILE_MG", env)
        self.assertGreaterEqual(seen, 1)
        for body in fleet.rendered_plists(self.load(self.PATH.read_text())).values():
            self.assertNotIn("TARTCI_PR_CLASS_CAP_WHILE_MG",
                             plistlib.loads(body)["EnvironmentVariables"])

    def test_the_validator_rejects_bad_config(self) -> None:
        body = self.PATH.read_text()
        cases = {
            "must be 1 or 2": self.with_cap("0"),
            "must be 1 or 2 ": self.with_cap("3"),
            "exactly one": self.with_cap("1").replace(
                'label = "vellum-build-macos-merge-group"', 'label = "vellum-build-macos-mg"'),
            "at least one tier after": self.with_cap("1").replace(
                '[[lane.tier]]\nlabel = "vellum-build-macos"\nworkflow = "Locked macOS GPU proof"\n'
                'runner_group_id = 8\n', ""),
        }
        for message, mutated in cases.items():
            self.assertNotEqual(mutated, body, message)
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message.strip()):
                    self.load(mutated)
        self.load(self.with_cap("1"))


if __name__ == "__main__":
    unittest.main()
