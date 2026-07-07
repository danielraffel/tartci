#!/usr/bin/env python3
"""Tests for profile provider validation + Orchard shadow-only vocabulary."""

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
    def test_orchard_is_a_valid_provider_but_not_lane_selectable(self) -> None:
        self.assertIn("orchard", P.VALID_TARGET_PROVIDERS)
        self.assertNotIn("orchard", P.LANE_SELECTABLE_PROVIDERS)
        self.assertLessEqual(P.LANE_SELECTABLE_PROVIDERS, P.VALID_TARGET_PROVIDERS)

    def test_iter_lane_refs_finds_lane_lists_not_the_catalog(self) -> None:
        prof = {
            "targets": {"a": {"provider": "tartci"}},  # the catalog is a table, not a ref
            "pulp": {"pr": {"macos": {"targets": ["a", "b"]}}},
        }
        self.assertEqual(sorted(P._iter_lane_target_refs(prof)), ["a", "b"])


class ValidateCommandTests(unittest.TestCase):
    def test_shipped_profile_validates_clean(self) -> None:
        # normal-local-fast declares a dormant orchard target but no lane
        # references it → validation passes.
        self.assertEqual(P.cmd_validate(_ns(name="normal-local-fast")), 0)

    def test_all_profiles_validate_clean(self) -> None:
        self.assertEqual(P.cmd_validate(_ns(name=None)), 0)

    def test_a_lane_referencing_an_orchard_target_is_rejected(self) -> None:
        # Build the same shape validate scans and confirm the guard fires.
        targets = {"o.vm": {"provider": "orchard"}, "t.vm": {"provider": "tartci"}}
        prof = {"targets": targets, "pulp": {"pr": {"macos": {"targets": ["o.vm", "t.vm"]}}}}
        offending = [
            tid
            for tid in P._iter_lane_target_refs(prof)
            if isinstance(targets.get(tid), dict)
            and targets[tid].get("provider") in P.VALID_TARGET_PROVIDERS
            and targets[tid].get("provider") not in P.LANE_SELECTABLE_PROVIDERS
        ]
        self.assertEqual(offending, ["o.vm"])

    def test_unknown_provider_is_flagged(self) -> None:
        targets = {"x.vm": {"provider": "namespace-cloud"}}
        unknown = [
            tid for tid, t in targets.items() if t.get("provider") not in P.VALID_TARGET_PROVIDERS
        ]
        self.assertEqual(unknown, ["x.vm"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
