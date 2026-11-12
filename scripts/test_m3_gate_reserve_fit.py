#!/usr/bin/env python3
"""m3's Pulp gate VMs must fit together inside its gate core reserve.

m3 is a dedicated-builder host: the lease store withholds the role's
`vm_pool_cores` from non-gate leases and lends everything else to agent builds.
When the gate lane's slots together asked for more than that reserve (2 x 12
against 14), the second gate VM could start only while agent builds held almost
nothing, so a gate slot sat lease-denied with jobs queued. Sizing every slot to
fit the reserve means agent builds can never starve a gate slot.

Scoped to m3 on purpose: other hosts size their reserve differently (m5studio
runs 2 x 8 against a 16-core reserve), so a fleet-wide rule against the role
default would be wrong.
"""

from __future__ import annotations

import testing_support  # noqa: E402
testing_support.skip_module_without_tomllib()
import tomllib
import unittest
from pathlib import Path

import host_profile

ROOT = Path(__file__).resolve().parents[1]
M3 = ROOT / "profiles" / "m3-macos-fleet.toml"


def pulp_gate(profile: dict) -> dict:
    lanes = [lane for lane in profile.get("lane", []) if lane.get("id") == "pulp-gate"]
    if len(lanes) != 1:
        raise AssertionError(f"expected one pulp-gate lane, found {len(lanes)}")
    return lanes[0]


def fits(profile: dict) -> tuple[bool, int, int]:
    lane = pulp_gate(profile)
    demand = int(lane.get("supervisors", 1)) * int(lane["vm_cores"])
    reserve = host_profile.ROLE_DEFAULTS["dedicated-builder"].vm_pool_cores
    return demand <= reserve, demand, reserve


class M3GateReserveFit(unittest.TestCase):
    def test_both_gate_slots_fit_the_gate_reserve(self) -> None:
        ok, demand, reserve = fits(tomllib.loads(M3.read_text(encoding="utf-8")))
        self.assertTrue(ok, f"m3 pulp-gate asks {demand} cores against a {reserve}-core reserve")

    def test_control_the_previous_sizing_is_rejected(self) -> None:
        profile = tomllib.loads(M3.read_text(encoding="utf-8"))
        pulp_gate(profile)["vm_cores"] = 12
        self.assertEqual(fits(profile)[:2], (False, 24))


if __name__ == "__main__":
    unittest.main()
