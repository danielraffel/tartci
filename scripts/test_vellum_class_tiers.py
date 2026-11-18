#!/usr/bin/env python3
"""Vellum's GPU lanes serve three classes: sterile journey, merge group, PR.

sterile-consumer (the 1.4 min journey after a proof) requests
vellum-sterile-consumer once Vellum's variable selects it. A merge-group
"Locked macOS GPU proof" job requests vellum-build-macos-merge-group and every
other event requests vellum-build-macos. Each host's vellum-gate lane declares
all three as workflow tiers in that order, so a runner registers with exactly
one class label and can only take a job of that class: a VM booted for a
journey can no longer be handed another PR's 30-minute proof. Every lower
class must stay served: a lane with no demand above takes it on the same scan.
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import testing_support  # noqa: E402

testing_support.skip_module_without_tomllib()

import tomllib  # noqa: E402
import macos_fleet_lanes as fleet  # noqa: E402

PROFILES = {h: ROOT / "profiles" / f"{h}-macos-fleet.toml" for h in ("m1", "m3", "m5", "m5studio")}
RUNNER = ROOT / "providers/tart-macos/runner.sh"
STERILE = "vellum-sterile-consumer"
MERGE_GROUP = "vellum-build-macos-merge-group"
PR_HEAD = "vellum-build-macos"
WORKFLOW = "Locked macOS GPU proof"


def vellum_lane(host: str) -> dict:
    data = tomllib.loads(PROFILES[host].read_text())
    return next(lane for lane in data["lane"] if lane["id"] == "vellum-gate")


def runner_function(name: str) -> str:
    match = re.search(rf"^{name}\(\)\{{\n.*?^\}}$", RUNNER.read_text(), re.MULTILINE | re.DOTALL)
    assert match, f"missing {name} in runner.sh"
    return match.group(0)


class ProfileTests(unittest.TestCase):
    def test_every_host_serves_three_classes_sterile_first(self) -> None:
        for host in PROFILES:
            with self.subTest(host=host):
                lane = vellum_lane(host)
                self.assertEqual([(t["label"], t["workflow"], t["runner_group_id"])
                                  for t in lane["tier"]],
                                 [(STERILE, WORKFLOW, 8), (MERGE_GROUP, WORKFLOW, 8),
                                  (PR_HEAD, WORKFLOW, 8)])
                # No class label in the base: a runner carries exactly one.
                for label in (STERILE, MERGE_GROUP, PR_HEAD):
                    self.assertNotIn(label, lane["labels"])
                # Every proof queues a journey as it ends: rescan after any serve.
                self.assertIs(lane["refresh_after_every_serve"], True)
                self.assertEqual(lane["labels"][-1], f"vellum-host-{host}")
                self.assertEqual(lane["job_claim_labels"], ["self-hosted", "macOS", "ARM64"])
                self.assertNotIn("workflows", lane)

    def test_rendered_lane_registers_one_class_per_tier(self) -> None:
        import plistlib
        rendered = fleet.rendered_plists(fleet.load(PROFILES["m5studio"]))
        env = next(plistlib.loads(body)["EnvironmentVariables"]
                   for name, body in rendered.items() if ".vellum-gate" in name)
        self.assertEqual(env["TARTCI_RUNNER_LABELS"],
                         "self-hosted,macOS,ARM64,vellum-host-m5studio")
        self.assertEqual(env["TARTCI_RUNNER_WORKFLOW_TIERS"].splitlines(),
                         [f"{STERILE}|{WORKFLOW}", f"{MERGE_GROUP}|{WORKFLOW}",
                          f"{PR_HEAD}|{WORKFLOW}"])
        self.assertEqual(env["TARTCI_JOB_CLAIM_LABELS"], "self-hosted,macOS,ARM64")
        self.assertEqual(env["TARTCI_REFRESH_AFTER_EVERY_SERVE"], "1")

    def test_only_vellum_lanes_rescan_after_every_serve(self) -> None:
        import plistlib
        for host, profile in PROFILES.items():
            for name, body in fleet.rendered_plists(fleet.load(profile)).items():
                env = plistlib.loads(body)["EnvironmentVariables"]
                with self.subTest(host=host, plist=name):
                    if env.get("TARTCI_RUNNER_REPO") == "Generous-Corp/vellum":
                        self.assertEqual(env.get("TARTCI_REFRESH_AFTER_EVERY_SERVE"), "1")
                    else:
                        self.assertNotIn("TARTCI_REFRESH_AFTER_EVERY_SERVE", env)

    def test_the_validator_refuses_a_non_boolean_or_a_lane_without_tiers(self) -> None:
        import tempfile
        base = PROFILES["m5"].read_text()
        line = "refresh_after_every_serve = true\n"
        self.assertEqual(base.count(line), 1)
        with tempfile.TemporaryDirectory() as td:
            for bad in ('refresh_after_every_serve = "yes"\n', "refresh_after_every_serve = 1\n"):
                with self.subTest(value=bad):
                    path = Path(td) / "p.toml"
                    path.write_text(base.replace(line, bad))
                    with self.assertRaisesRegex(ValueError, "refresh_after_every_serve"):
                        fleet.load(path)
            # A single-label lane has no tier loop to refresh.
            spectr = 'id = "spectr-gate"\n'
            self.assertEqual(base.count(spectr), 1)
            path = Path(td) / "p.toml"
            path.write_text(base.replace(spectr, spectr + line))
            with self.assertRaisesRegex(ValueError, "refresh_after_every_serve"):
                fleet.load(path)


class SelectionTests(unittest.TestCase):
    """select_work, the lane's own scan, against stubbed per-class counts."""

    def select(self, merge_group: str, pr_head: str, sterile: str = "0") -> str:
        script = (
            "set -u\n"
            "ASSIGNMENT_MODE=legacy\nLABELS=self-hosted,macOS,ARM64,vellum-host-m5\n"
            f"WORKFLOW_TIERS='{STERILE}|{WORKFLOW}'\n"
            f"TIER_LABELS_CONFIG=$'{STERILE}\\n{MERGE_GROUP}\\n{PR_HEAD}'\n"
            "SCAN_FORCE_REFRESH=0\n"
            "tier_queued_work(){ case \"$1\" in "
            f"{STERILE}) echo {sterile} ;; "
            f"{MERGE_GROUP}) echo {merge_group} ;; {PR_HEAD}) echo {pr_head} ;; esac; }}\n"
            "queued_work(){ echo 0; }\n"
            + runner_function("select_work") + "\nselect_work\n"
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_no_merge_group_demand_takes_a_pr_job_on_the_same_scan(self) -> None:
        self.assertEqual(self.select("0", "2"),
                         f"2|self-hosted,macOS,ARM64,vellum-host-m5,{PR_HEAD}|2")

    def test_merge_group_demand_is_served_before_pr(self) -> None:
        self.assertEqual(self.select("1", "3"),
                         f"1|self-hosted,macOS,ARM64,vellum-host-m5,{MERGE_GROUP}|1")

    def test_a_sterile_journey_is_served_before_every_proof(self) -> None:
        self.assertEqual(self.select("1", "3", sterile="1"),
                         f"1|self-hosted,macOS,ARM64,vellum-host-m5,{STERILE}|0")

    def test_a_blind_sterile_scan_is_blind_not_empty(self) -> None:
        """A failed scan of the first class must not be read as 'serve a proof'."""
        self.assertTrue(self.select("1", "2", sterile="ERR").startswith("ERR|"))

    def test_no_demand_at_all_boots_nothing(self) -> None:
        self.assertTrue(self.select("0", "0").startswith("0|"))

    def test_a_blind_merge_group_scan_is_blind_not_empty(self) -> None:
        """A failed scan of the first class must not be read as 'serve PR'."""
        self.assertTrue(self.select("ERR", "2").startswith("ERR|"))


class RefreshAfterServeTests(unittest.TestCase):
    """A lane that just served a merge-group job rescans uncached once.

    Vellum's sterile-consumer is queued when its GPU proof ends, seconds before
    the GPU VM tears down. The shared discovery snapshot is up to 160 s old, so
    the freed lane's cached scan shows no merge-group work and boots a PR proof.
    """

    def run_bash(self, body: str) -> str:
        script = (
            "set -u\n"
            "LABELS=self-hosted,macOS,ARM64,vellum-host-m5\n"
            f"WORKFLOW_TIERS='{MERGE_GROUP}|{WORKFLOW}'\n"
            f"TIER_LABELS_CONFIG=$'{MERGE_GROUP}\\n{PR_HEAD}'\n"
            # The cache hides the just-queued sterile job; only a forced
            # refresh sees it. PR-class work is visible either way.
            "tier_queued_work(){ case \"$1\" in "
            f"{MERGE_GROUP}) [ \"$2\" = 1 ] && echo 1 || echo 0 ;; {PR_HEAD}) echo 2 ;; esac; }}\n"
            "queued_work(){ echo 0; }\n"
            "tartci_assignment_v2_select(){ echo \"v2|force=${1:-0}|0\"; }\n"
            + runner_function("select_work") + "\n"
            + runner_function("refresh_after_serve") + "\n"
            + runner_function("next_selection") + "\n"
            + body
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_a_served_merge_group_job_asks_for_a_refresh(self) -> None:
        for labels in (f"self-hosted,macOS,ARM64,vellum-host-m5,{MERGE_GROUP}",
                       "self-hosted,macOS,ARM64,pulp-build,pulp-build-merge-group"):
            with self.subTest(labels=labels):
                self.assertEqual(self.run_bash(f"refresh_after_serve 1 '{labels}'"), "1")

    def test_a_pr_job_or_an_unserved_boot_does_not(self) -> None:
        self.assertEqual(self.run_bash(
            f"refresh_after_serve 1 'self-hosted,macOS,ARM64,vellum-host-m5,{PR_HEAD}'"), "0")
        self.assertEqual(self.run_bash(
            f"refresh_after_serve 0 'self-hosted,macOS,ARM64,vellum-host-m5,{MERGE_GROUP}'"), "0")
        self.assertEqual(self.run_bash("refresh_after_serve 1 ''"), "0")

    def test_the_cached_scan_hides_the_sterile_job_and_picks_pr_work(self) -> None:
        self.assertEqual(self.run_bash("ASSIGNMENT_MODE=legacy; next_selection 0"),
                         f"2|self-hosted,macOS,ARM64,vellum-host-m5,{PR_HEAD}|1")

    def test_the_forced_selection_after_serving_takes_the_sterile_job(self) -> None:
        self.assertEqual(self.run_bash(
            "ASSIGNMENT_MODE=legacy\n"
            f"next_selection \"$(refresh_after_serve 1 \"$LABELS,{MERGE_GROUP}\")\""),
            f"1|self-hosted,macOS,ARM64,vellum-host-m5,{MERGE_GROUP}|0")

    def test_event_class_v2_forces_its_own_selection_cache(self) -> None:
        self.assertEqual(self.run_bash("ASSIGNMENT_MODE=event-class-v2; next_selection 1"),
                         "v2|force=1|0")
        self.assertEqual(self.run_bash("ASSIGNMENT_MODE=event-class-v2; next_selection 0"),
                         "v2|force=0|0")

    def test_loop_sets_the_flag_after_run_one_and_spends_it_once(self) -> None:
        text = RUNNER.read_text()
        run_one = text.index('run_one "$i" "$selected_labels" "$selected_tier" || run_rc=$?')
        flag = text.index('SCAN_REFRESH_NEXT="$(refresh_after_serve "$CURRENT_SERVED" "$selected_labels")"')
        # Set after run_one returns, before the post-run heartbeat that ends
        # the work entry, so every served job reaches it.
        post_run = text.index('heartbeat "${LAST_HEARTBEAT_PHASE:-waiting}"', run_one)
        self.assertLess(run_one, flag)
        self.assertLess(flag, post_run)
        self.assertIn(
            'if [ "$SCAN_REFRESH_NEXT" = 1 ]; then\n'
            '      SCAN_REFRESH_NEXT=0\n'
            '      selection="$(next_selection 1)"', text)
        self.assertIn('selection="$(next_selection 0)"', text)
        # The loop-top selection goes through next_selection, never around it.
        loop = text[text.index("tartci_heartbeat_keepalive_start\n    if [ \"$SCAN_REFRESH_NEXT\""):run_one]
        self.assertNotIn('selection="$(select_work)"', loop)


class RefreshAfterEveryServeTests(unittest.TestCase):
    """With the journey as its own first class, every served proof refreshes.

    A PR-class proof queues its sterile-consumer job as it ends, exactly as a
    merge-group proof does. Without the refresh the freed lane's cached scan
    hides the journey, finds PR proofs and boots one of those instead.
    """

    def run_bash(self, body: str) -> str:
        script = (
            "set -u\n"
            "LABELS=self-hosted,macOS,ARM64,vellum-host-m5\n"
            f"WORKFLOW_TIERS='{STERILE}|{WORKFLOW}'\n"
            f"TIER_LABELS_CONFIG=$'{STERILE}\\n{MERGE_GROUP}\\n{PR_HEAD}'\n"
            # The cache hides the just-queued journey; only a forced refresh
            # sees it. PR-class proofs are visible either way.
            "tier_queued_work(){ case \"$1\" in "
            f"{STERILE}) [ \"$2\" = 1 ] && echo 1 || echo 0 ;; "
            f"{MERGE_GROUP}) echo 0 ;; {PR_HEAD}) echo 2 ;; esac; }}\n"
            "queued_work(){ echo 0; }\n"
            "tartci_assignment_v2_select(){ echo \"v2|force=${1:-0}|0\"; }\n"
            + runner_function("select_work") + "\n"
            + runner_function("refresh_after_serve") + "\n"
            + runner_function("next_selection") + "\n"
            + body
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_a_served_pr_proof_asks_for_a_refresh_on_an_opted_in_lane(self) -> None:
        self.assertEqual(self.run_bash(
            "TARTCI_REFRESH_AFTER_EVERY_SERVE=1 "
            f"refresh_after_serve 1 \"$LABELS,{PR_HEAD}\""), "1")

    def test_off_by_default_and_never_without_a_served_job(self) -> None:
        self.assertEqual(self.run_bash(f"refresh_after_serve 1 \"$LABELS,{PR_HEAD}\""), "0")
        self.assertEqual(self.run_bash(
            "TARTCI_REFRESH_AFTER_EVERY_SERVE=1 "
            f"refresh_after_serve 0 \"$LABELS,{PR_HEAD}\""), "0")
        self.assertEqual(self.run_bash(
            "TARTCI_REFRESH_AFTER_EVERY_SERVE=1 refresh_after_serve 1 ''"), "0")

    def test_the_cached_scan_after_a_pr_proof_boots_another_proof(self) -> None:
        self.assertEqual(self.run_bash("ASSIGNMENT_MODE=legacy; next_selection 0"),
                         f"2|self-hosted,macOS,ARM64,vellum-host-m5,{PR_HEAD}|2")

    def test_the_forced_selection_after_a_pr_proof_takes_the_journey(self) -> None:
        self.assertEqual(self.run_bash(
            "ASSIGNMENT_MODE=legacy\nexport TARTCI_REFRESH_AFTER_EVERY_SERVE=1\n"
            f"next_selection \"$(refresh_after_serve 1 \"$LABELS,{PR_HEAD}\")\""),
            f"1|self-hosted,macOS,ARM64,vellum-host-m5,{STERILE}|0")

    def test_the_runner_reads_the_rendered_flag(self) -> None:
        self.assertIn('"${TARTCI_REFRESH_AFTER_EVERY_SERVE:-0}" = 1',
                      runner_function("refresh_after_serve"))

if __name__ == "__main__":
    unittest.main()
