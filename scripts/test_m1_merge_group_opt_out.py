#!/usr/bin/env python3
"""m1 serves no merge_group jobs; every other Pulp gate host still does.

m1's 3-core gate guest ran merge_group macos jobs in 33-35 min against 15-22
min on the other hosts (4 of 33 jobs over a week), so each batch it took held
the merge queue longest. A lane drops a gate class only by naming the classes
it keeps (`v2_gate_classes`), so a missing tier is never a silent typo.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import macos_fleet_lanes as fleet  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MERGE_GROUP = "pulp-build-merge-group"
PR_HEAD = "pulp-build-pr-head"
PROFILES = {
    "m1": "m1-macos-fleet.toml",
    "studio": "m3-macos-fleet.toml",
    "m5": "m5-macos-fleet.toml",
    "m5studio": "m5studio-macos-fleet.toml",
}


def pulp_gate(host: str) -> dict:
    data = fleet.load(ROOT / "profiles" / PROFILES[host])
    return next(lane for lane in data["lane"] if lane["id"] == "pulp-gate")


def tier_labels(host: str) -> list[str]:
    return [tier["label"] for tier in pulp_gate(host)["tier"]]


class ProfileTests(unittest.TestCase):
    def test_m1_does_not_serve_merge_group(self) -> None:
        self.assertNotIn(MERGE_GROUP, tier_labels("m1"))
        self.assertEqual(pulp_gate("m1")["v2_gate_classes"], [PR_HEAD])
        self.assertIn(PR_HEAD, tier_labels("m1"), "m1 keeps PR-head work")

    def test_the_other_gate_hosts_still_serve_merge_group(self) -> None:
        # Control, same instrument: every other host's lane still carries it.
        for host in ("studio", "m5", "m5studio"):
            self.assertEqual(tier_labels(host)[:2], [MERGE_GROUP, PR_HEAD], host)
            self.assertNotIn("v2_gate_classes", pulp_gate(host), host)

    def test_no_rendered_m1_plist_names_merge_group(self) -> None:
        data = fleet.load(ROOT / "profiles" / PROFILES["m1"])
        bodies = fleet.rendered_plists(data)
        self.assertTrue(bodies)
        for name, body in bodies.items():
            self.assertNotIn(MERGE_GROUP.encode(), body, name)


class PublishedSupplyTests(unittest.TestCase):
    def published(self) -> set[str]:
        value = json.loads((ROOT / "fleet/advertised-labels.json").read_text())
        return {row["host_id"] for row in value["registrations"]
                if row.get("class_label") == MERGE_GROUP}

    def test_the_published_supply_drops_m1_and_keeps_the_rest(self) -> None:
        self.assertEqual(self.published(), {"studio", "m5", "m5studio"})


class ValidatorTests(unittest.TestCase):
    def lane(self, **overrides) -> dict:
        lane = copy.deepcopy(pulp_gate("m5"))
        lane.update(overrides)
        return lane

    def check(self, lane: dict) -> None:
        fleet.validate_v2_tiers(lane["id"], lane["repo"], lane["tier"],
                                fleet.v2_gate_classes(lane["id"], lane.get("v2_gate_classes")))

    def test_dropping_the_tier_without_the_key_is_refused(self) -> None:
        lane = self.lane()
        lane["tier"] = [t for t in lane["tier"] if t["label"] != MERGE_GROUP]
        with self.assertRaises(ValueError):
            self.check(lane)
        lane["v2_gate_classes"] = [PR_HEAD]
        self.check(lane)

    def test_the_key_must_be_an_ordered_non_empty_subset(self) -> None:
        for bad in ([], [PR_HEAD, MERGE_GROUP], ["pulp-build-other"], [PR_HEAD, PR_HEAD], "x"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                fleet.v2_gate_classes("pulp-gate", bad)
        self.assertEqual(fleet.v2_gate_classes("pulp-gate", None),
                         (MERGE_GROUP, PR_HEAD))

    def test_a_declared_class_must_have_its_tier(self) -> None:
        lane = self.lane(v2_gate_classes=[PR_HEAD])   # merge-group tier still present
        with self.assertRaises(ValueError):
            self.check(lane)


if __name__ == "__main__":
    unittest.main()
