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

    def test_pulp_linux_prefers_macpro_exact_label_contract(self) -> None:
        _, profile = P.load_profile("normal-local-fast")
        lane = P.resolve_lanes(profile, "Generous-Corp/pulp")
        linux = next(row for row in lane if row["context"] == "pr" and row["lane"] == "linux")
        self.assertEqual(linux["targets"][0]["id"], "macpro.linux-x64-vm")
        self.assertEqual(
            linux["targets"][0]["runs_on_json"],
            ["self-hosted", "Linux", "X64", "pulp-build-linux-x64", "pulp-host-macpro"],
        )
        self.assertEqual(linux["warnings"], [])

    def test_retired_provider_target_is_rejected_as_unknown(self) -> None:
        targets = {
            "o.vm": {"provider": "orchard"},
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
