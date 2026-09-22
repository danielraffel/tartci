#!/usr/bin/env python3
"""Offline advertised-labels snapshot (tartci.advertised-labels/v1)."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import macos_fleet_lanes as fleet

ROOT = Path(__file__).resolve().parents[1]
M3 = ROOT / "profiles" / "m3-macos-fleet.toml"
M5 = ROOT / "profiles" / "m5-macos-fleet.toml"
BASE = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]


def _rows(snapshot: dict, lane: str, profile: str | None = None) -> list[dict]:
    return [row for row in snapshot["registrations"]
            if row["lane"] == lane and (profile is None or row["profile"] == profile)]


class SnapshotShapeTests(unittest.TestCase):
    def test_schema_and_provenance(self) -> None:
        snapshot = fleet.advertised_labels_snapshot([M3], "a" * 40)
        self.assertEqual(snapshot["schema"], "tartci.advertised-labels/v1")
        self.assertEqual(snapshot["generated_from"], {
            "repo": "danielraffel/tartci", "commit": "a" * 40,
            "profiles": ["profiles/m3-macos-fleet.toml"]})
        for row in snapshot["registrations"]:
            self.assertEqual(set(row), {
                "profile", "host_id", "lane", "repo", "assignment_mode",
                "class_label", "labels", "workflows"})

    def test_commit_is_head_inside_a_checkout_and_null_outside(self) -> None:
        head = fleet.git_head(ROOT)
        self.assertIsNotNone(head)
        self.assertRegex(head, r"^[0-9a-f]{40}$")
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(fleet.git_head(Path(td)))

    def test_cli_json_is_the_contract(self) -> None:
        proc = subprocess.run(
            [str(ROOT / "tartci"), "fleet-macos", "advertised-labels",
             str(M3), str(M5), "--json"],
            cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)
        self.assertEqual(value["schema"], "tartci.advertised-labels/v1")
        self.assertEqual(value["generated_from"]["profiles"],
                         ["profiles/m3-macos-fleet.toml", "profiles/m5-macos-fleet.toml"])
        self.assertEqual({row["profile"] for row in value["registrations"]},
                         {"m3-macos-fleet", "m5-macos-fleet"})

    def test_invalid_profile_is_refused_by_load_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.toml"
            bad.write_text(M3.read_text().replace('assignment_mode = "event-class-v2"',
                                                  'assignment_mode = "bogus"'))
            proc = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "advertised-labels", str(bad)],
                cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("unsupported assignment_mode", proc.stderr)


class RegistrationRuleTests(unittest.TestCase):
    def test_m3_pulp_gate_registers_one_set_per_class_and_never_gate_fast(self) -> None:
        rows = _rows(fleet.advertised_labels_snapshot([M3], None), "pulp-gate")
        self.assertEqual([row["labels"] for row in rows], [
            BASE + ["pulp-build-merge-group"], BASE + ["pulp-build-pr-head"]])
        self.assertEqual([row["class_label"] for row in rows],
                         ["pulp-build-merge-group", "pulp-build-pr-head"])
        for row in rows:
            self.assertEqual(row["assignment_mode"], "event-class-v2")
            self.assertEqual(row["workflows"], ["Build and Test"])
            self.assertEqual(row["host_id"], "studio")
            self.assertNotIn("pulp-gate-fast", row["labels"])

    def test_v2_subtracts_omit_labels_the_profile_carries(self) -> None:
        # The checked-in profile no longer lists pulp-gate-fast, so the
        # subtraction is only exercised by a profile that still carries it.
        data = fleet.load(M3)
        lane = next(lane for lane in data["lane"] if lane["id"] == "pulp-gate")
        lane["labels"] = [*lane["labels"], "pulp-gate-fast"]
        rows = [row for row in fleet.advertised_registrations(data)
                if row["lane"] == "pulp-gate"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotIn("pulp-gate-fast", row["labels"])
        # Control: the same edit in a legacy (non-V2) lane keeps the label,
        # proving the check can see pulp-gate-fast at all.
        legacy = copy.deepcopy(data)
        legacy_lane = next(l for l in legacy["lane"] if l["id"] == "pulp-gate")
        legacy_lane.pop("assignment_mode")
        legacy_lane.pop("assignment_omit_labels")
        legacy_rows = [row for row in fleet.advertised_registrations(legacy)
                       if row["lane"] == "pulp-gate"]
        for row in legacy_rows:
            self.assertIn("pulp-gate-fast", row["labels"])

    def test_v2_subtracts_every_tier_label_from_the_base(self) -> None:
        rows = fleet.registrations_from_env({
            "TARTCI_RUNNER_LABELS": "self-hosted,macOS,ARM64,pulp-build-vm,pulp-build-pr-head",
            "TARTCI_RUNNER_ASSIGNMENT_MODE": "event-class-v2",
            "TARTCI_RUNNER_WORKFLOW_TIERS":
                "pulp-build-merge-group|Build and Test\npulp-build-pr-head|Build and Test",
            "TARTCI_ASSIGNMENT_V2_OMIT_LABELS": "pulp-gate-fast",
            "TARTCI_ASSIGNMENT_V2_CLASS_LABELS": "pulp-build-merge-group,pulp-build-pr-head",
        })
        self.assertEqual(rows[0]["labels"],
                         ["self-hosted", "macOS", "ARM64", "pulp-build-vm",
                          "pulp-build-merge-group"])

    def test_m5_release_lane_is_legacy_with_tier_label_appended(self) -> None:
        rows = _rows(fleet.advertised_labels_snapshot([M5], None), "pulp-release")
        release = ["self-hosted", "macOS", "ARM64", "pulp-build-vm-release"]
        self.assertEqual(rows, [
            {"profile": "m5-macos-fleet", "host_id": "m5", "lane": "pulp-release",
             "repo": "Generous-Corp/pulp", "assignment_mode": "legacy",
             "class_label": "pulp-release-tagged",
             "labels": release + ["pulp-release-tagged"],
             "workflows": ["Release CLI", "Sign and Release"]},
            {"profile": "m5-macos-fleet", "host_id": "m5", "lane": "pulp-release",
             "repo": "Generous-Corp/pulp", "assignment_mode": "legacy",
             "class_label": "pulp-release-pr-gate",
             "labels": release + ["pulp-release-pr-gate"],
             "workflows": ["Release-path PR gate"]},
        ])

    def test_non_tier_lane_mints_for_lane_workflows(self) -> None:
        rows = _rows(fleet.advertised_labels_snapshot([M3], None), "forge-gate")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["assignment_mode"], "legacy")
        self.assertIsNone(rows[0]["class_label"])
        self.assertEqual(rows[0]["workflows"], ["build", "protected macOS build"])
        self.assertEqual(rows[0]["labels"],
                         ["self-hosted", "macOS", "ARM64", "forge-build",
                          "forge-build-vm", "forge-gate-fast"])

    def test_reachability_uses_case_insensitive_subset_and_workflow(self) -> None:
        rows = _rows(fleet.advertised_labels_snapshot([M3], None), "pulp-gate")
        pr_head = rows[1]
        job = ["self-hosted", "macos", "ARM64", "pulp-build-vm", "pulp-build-pr-head"]
        self.assertTrue(fleet.reachable(pr_head, "Generous-Corp/pulp", "Build and Test", job))
        self.assertFalse(fleet.reachable(pr_head, "Generous-Corp/pulp", "Other", job))
        self.assertFalse(fleet.reachable(
            pr_head, "Generous-Corp/pulp", "Build and Test", [*job, "pulp-gate-fast"]))


class RunnerParityTests(unittest.TestCase):
    """The replay must agree with the provider's own base-label computation."""

    def test_python_replay_matches_assignment_v2_heredoc(self) -> None:
        source = (ROOT / "providers/tart-macos/assignment-v2.lib.sh").read_text()
        match = re.search(
            r'ASSIGNMENT_V2_BASE_LABELS="\$\(python3 - "\$LABELS" '
            r'"\$ASSIGNMENT_V2_OMIT_LABELS" "\$ASSIGNMENT_V2_CLASS_LABELS" '
            r"<<'PY'\n(.*?)\nPY\n", source, re.S)
        self.assertIsNotNone(match, "assignment-v2 base-label computation moved")
        cases = [
            ("self-hosted,macOS,ARM64,pulp-build,pulp-build-vm,pulp-gate-fast",
             "pulp-gate-fast", "pulp-build-merge-group,pulp-build-pr-head"),
            ("self-hosted,macOS,ARM64,PULP-BUILD-PR-HEAD,pulp-build-vm",
             "", "pulp-build-merge-group,pulp-build-pr-head"),
        ]
        for labels, omit, classes in cases:
            provider = subprocess.run(
                [sys.executable, "-c", match.group(1), labels, omit, classes],
                text=True, capture_output=True, check=True).stdout.strip()
            rows = fleet.registrations_from_env({
                "TARTCI_RUNNER_LABELS": labels,
                "TARTCI_RUNNER_ASSIGNMENT_MODE": "event-class-v2",
                "TARTCI_RUNNER_WORKFLOW_TIERS": "pulp-build-pr-head|Build and Test",
                "TARTCI_ASSIGNMENT_V2_OMIT_LABELS": omit,
                "TARTCI_ASSIGNMENT_V2_CLASS_LABELS": classes,
            })
            self.assertEqual(rows[0]["labels"][:-1], provider.split(","), labels)

    def test_rendered_environment_is_what_the_snapshot_reads(self) -> None:
        with M3.open("rb") as handle:
            raw = tomllib.load(handle)
        self.assertIn("pulp-gate", [lane["id"] for lane in raw["lane"]])
        data = fleet.load(M3)
        lane = next(lane for lane in data["lane"] if lane["id"] == "pulp-gate")
        env = fleet.lane_plist(data, lane)["EnvironmentVariables"]
        self.assertEqual(env["TARTCI_ASSIGNMENT_V2_CLASS_LABELS"],
                         "pulp-build-merge-group,pulp-build-pr-head")
        self.assertEqual(env["TARTCI_RUNNER_ASSIGNMENT_MODE"], "event-class-v2")


if __name__ == "__main__":
    unittest.main()
