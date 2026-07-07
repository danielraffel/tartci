#!/usr/bin/env python3
"""Tests for tartci host resource profile derivation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import host_profile


class HostProfileRoleTests(unittest.TestCase):
    def test_dedicated_builder_budget(self) -> None:
        profile = host_profile.build_profile(
            role="dedicated-builder",
            cores=28,
            model="Mac15,14",
        )
        self.assertEqual(profile["role"], "dedicated-builder")
        self.assertEqual(profile["headroom_cores"], 2)
        self.assertEqual(profile["lease_capacity_cores"], 26)
        self.assertEqual(profile["pulp_build_jobs"], 12)
        self.assertEqual(profile["reserved_gate_cores"], 14)
        self.assertEqual(profile["qos"], "normal")

    def test_dev_overflow_budget(self) -> None:
        profile = host_profile.build_profile(
            role="dev-overflow",
            cores=18,
            model="Mac17,7",
        )
        self.assertEqual(profile["headroom_cores"], 4)
        self.assertEqual(profile["lease_capacity_cores"], 14)
        self.assertEqual(profile["pulp_build_jobs"], 6)
        self.assertEqual(profile["reserved_gate_cores"], 8)
        self.assertEqual(profile["qos"], "background")

    def test_dev_overflow_vm_pool_fits_non_gate_budget(self) -> None:
        # A dev-overflow VM lane runs at non-gate priority, so it can only ever
        # acquire a lease if vm_pool_cores fits the non-gate budget
        # (lease_capacity - reserved_gate_cores). If it does not, the VM is
        # permanently capacity_exceeded while the idle macOS gate holds its
        # reservation, which starves the required-gate Linux preamble
        # fleet-wide. Assert the fit at the real Mac Studio size (28 cores),
        # the host that surfaced the deadlock.
        profile = host_profile.build_profile(role="dev-overflow", cores=28)
        non_gate = profile["lease_capacity_cores"] - profile["reserved_gate_cores"]
        self.assertEqual(non_gate, 6)
        self.assertEqual(profile["vm_pool_cores"], 6)
        self.assertLessEqual(profile["vm_pool_cores"], non_gate)

    def test_light_budget_is_clamped_to_small_hosts(self) -> None:
        profile = host_profile.build_profile(role="light", cores=4, model="portable")
        self.assertEqual(profile["headroom_cores"], 3)
        self.assertEqual(profile["lease_capacity_cores"], 1)
        self.assertEqual(profile["pulp_build_jobs"], 1)
        self.assertEqual(profile["reserved_gate_cores"], 0)

    def test_role_file_wins_over_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            role_file = Path(td) / "role"
            role_file.write_text("light\n", encoding="utf-8")
            role, source = host_profile.resolve_role(
                role_file=str(role_file),
                cores=28,
                model="Mac15,14",
            )
        self.assertEqual(role, "light")
        self.assertTrue(source.startswith("file:"))

    def test_default_never_promotes_to_dedicated_builder(self) -> None:
        role, source = host_profile.resolve_role(
            cores=28,
            model="Mac15,14",
            role_file="/tmp/tartci-missing-role-for-test",
        )
        self.assertEqual(source, "default")
        self.assertEqual(role, "dev-overflow")

    def test_shell_exports_include_pulp_build_jobs(self) -> None:
        profile = host_profile.build_profile(role="dev-overflow", cores=18)
        text = host_profile.shell_exports(profile)
        self.assertIn("PULP_BUILD_JOBS=6", text)
        self.assertIn("TARTCI_GATE_RESERVED_CORES=8", text)

    def test_json_shape_is_stable(self) -> None:
        profile = host_profile.build_profile(role="light", cores=10)
        encoded = json.loads(json.dumps(profile))
        self.assertEqual(encoded["schema"], 1)
        self.assertIn("no mitigation yet", encoded["notes"])


class HostProfileEnvironmentTests(unittest.TestCase):
    def test_environment_role_wins_over_default(self) -> None:
        old = os.environ.get("TARTCI_ROLE")
        os.environ["TARTCI_ROLE"] = "light"
        try:
            role, source = host_profile.resolve_role(
                cores=18,
                model="Mac17,7",
                role_file="/tmp/tartci-missing-role-for-test",
            )
        finally:
            if old is None:
                os.environ.pop("TARTCI_ROLE", None)
            else:
                os.environ["TARTCI_ROLE"] = old
        self.assertEqual((role, source), ("light", "environment"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
