#!/usr/bin/env python3
"""Tests for profile provider validation."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile as P  # noqa: E402


def _ns(**kw: object) -> argparse.Namespace:
    return argparse.Namespace(**kw)


class ProviderVocabularyTests(unittest.TestCase):
    def test_retired_provider_is_rejected(self) -> None:
        self.assertNotIn("orchard", P.VALID_TARGET_PROVIDERS)
        self.assertEqual(P.LANE_SELECTABLE_PROVIDERS, P.VALID_TARGET_PROVIDERS)

    def test_iter_lane_refs_finds_lane_lists_not_the_catalog(self) -> None:
        prof = {
            "targets": {"a": {"provider": "tartci"}},  # the catalog is a table, not a ref
            "pulp": {"pr": {"macos": {"targets": ["a", "b"]}}},
        }
        self.assertEqual(sorted(P._iter_lane_target_refs(prof)), ["a", "b"])


class ValidateCommandTests(unittest.TestCase):
    def test_shipped_profile_validates_clean(self) -> None:
        self.assertEqual(P.cmd_validate(_ns(name="normal-local-fast")), 0)

    def test_all_profiles_validate_clean(self) -> None:
        self.assertEqual(P.cmd_validate(_ns(name=None)), 0)

    def test_pulp_pr_linux_prefers_distinct_pr_safe_macpro_contract(self) -> None:
        _, profile = P.load_profile("normal-local-fast")
        lane = P.resolve_lanes(profile, "Generous-Corp/pulp")
        linux = next(row for row in lane if row["context"] == "pr" and row["lane"] == "linux")
        self.assertEqual(linux["targets"][0]["id"], "macpro.linux-x64-pr-safe-vm")
        self.assertEqual(
            linux["targets"][0]["runs_on_json"],
            [
                "self-hosted", "Linux", "X64", "pulp-build-linux-x64",
                "pulp-host-macpro", "pulp-pr-safe-linux-x64",
            ],
        )
        self.assertIn("until proven", " ".join(linux["warnings"]))

    def test_pulp_pr_and_merge_group_linux_cannot_share_group_or_lane_label(self) -> None:
        _, profile = P.load_profile("normal-local-fast")
        targets = profile["targets"]
        pr = targets["macpro.linux-x64-pr-safe-vm"]
        trusted = targets["macpro.linux-x64-trusted-vm"]
        self.assertNotEqual(pr["runner_group"], trusted["runner_group"])
        self.assertIn("pulp-pr-safe-linux-x64", pr["runs_on_json"])
        self.assertNotIn("pulp-auto-linux-x64", pr["runs_on_json"])
        self.assertIn("pulp-auto-linux-x64", trusted["runs_on_json"])
        self.assertNotIn("pulp-pr-safe-linux-x64", trusted["runs_on_json"])
        self.assertEqual(
            pr["workflow_access"],
            ["Generous-Corp/pulp/.github/workflows/pr-safe-linux.yml@refs/heads/main"],
        )

    def test_privileged_and_release_mutation_policies_are_hosted_only(self) -> None:
        _, profile = P.load_profile("normal-local-fast")
        policy = profile["policy"]
        for key in (
            "privileged", "fork", "untrusted", "secret_bearing",
            "unsupported_architecture", "release_signing", "release_deploy",
        ):
            self.assertEqual(policy[key], "github-only", key)
        repo = profile["repo"]["Generous-Corp/pulp"]
        self.assertEqual(repo["release"]["linux"]["targets"], ["github.linux-x64"])
        self.assertEqual(repo["release"]["signing"]["targets"], ["github.linux-x64"])
        self.assertEqual(repo["release"]["deploy"]["targets"], ["github.linux-x64"])

    def test_native_intel_prefers_macmini_with_hosted_fallback(self) -> None:
        _, profile = P.load_profile("normal-local-fast")
        nightly = profile["repo"]["Generous-Corp/pulp"]["scheduled"]["nightly_intel"]
        self.assertEqual(
            nightly["targets"],
            ["macmini.macos-intel-native", "github.macos-intel"],
        )

    def test_vellum_has_explicit_local_matrix_and_hosted_fallbacks(self) -> None:
        _, profile = P.load_profile("normal-local-fast")
        lanes = P.resolve_lanes(profile, "Generous-Corp/vellum")
        self.assertEqual(
            next(row for row in lanes if row["context"] == "pr" and row["lane"] == "linux")["targets"][0]["id"],
            "macpro.vellum-linux-x64-pr-safe",
        )
        macos = next(row for row in lanes if row["context"] == "pr" and row["lane"] == "macos")
        self.assertEqual(
            [target["id"] for target in macos["targets"][:3]],
            [
                "m3.vellum-macos-arm64-vm",
                "m5.vellum-macos-arm64-vm",
                "m1.vellum-macos-arm64-vm",
            ],
        )
        self.assertEqual(macos["targets"][-1]["id"], "github.macos-arm64")
        intel = next(row for row in lanes if row["context"] == "scheduled" and row["lane"] == "nightly_intel")
        self.assertEqual(intel["targets"][0]["id"], "macmini.vellum-macos-intel-native")

    def test_vellum_release_mutation_lanes_remain_hosted_only(self) -> None:
        _, profile = P.load_profile("normal-local-fast")
        lanes = P.resolve_lanes(profile, "Generous-Corp/vellum")
        for lane_name in ("signing", "deploy"):
            lane = next(row for row in lanes if row["context"] == "release" and row["lane"] == lane_name)
            self.assertEqual(lane["strategy"], "github-only")
            self.assertEqual(lane["targets"], [profile["targets"]["github.linux-x64"] | {"id": "github.linux-x64"}])

    def test_retired_provider_target_is_rejected_as_unknown(self) -> None:
        targets = {
            "o.vm": {"provider": "orchard"},
            "p.vm": {"provider": "proxmox"},
            "t.vm": {"provider": "tartci"},
            "m.native": {"provider": "shipyard"},
        }
        unknown = [
            tid for tid, target in targets.items()
            if target.get("provider") not in P.VALID_TARGET_PROVIDERS
        ]
        self.assertEqual(unknown, ["o.vm"])

    def test_unknown_provider_is_flagged(self) -> None:
        targets = {"x.vm": {"provider": "namespace-cloud"}}
        unknown = [
            tid for tid, t in targets.items() if t.get("provider") not in P.VALID_TARGET_PROVIDERS
        ]
        self.assertEqual(unknown, ["x.vm"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
