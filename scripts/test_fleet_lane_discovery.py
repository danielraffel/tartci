#!/usr/bin/env python3
"""Tests for fleet lane discovery and the supervisor-coverage poka-yoke.

The two that matter most are a matched pair, and they must BOTH hold:

  * ``test_blind_on_a_host_with_loaded_lanes_is_a_problem`` -- a tool that
    matches zero supervisors on a host running fleet lanes must say so. This
    is the regression guard for the incident: `observe macos` printed
    "no matching macOS supervisors" beside "problems=0" while five lanes were
    loaded.
  * ``test_a_host_with_no_lanes_is_silent`` -- a host that genuinely runs no
    lanes must produce NO problem. A detector that cannot tell "nothing here"
    from "I cannot see" is worse than no detector, and this fleet has already
    been burned once by a health check that declared a working host dead.
"""

from __future__ import annotations

import plistlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_lane_discovery as fld  # noqa: E402

PREFIX = fld.FLEET_LABEL_PREFIX

LAUNCHCTL_REAL = "\n".join([
    "PID\tStatus\tLabel",
    "81199\t75\t" + PREFIX + "studio.pulp-gate",
    "55580\t75\t" + PREFIX + "studio.forge-gate",
    "92828\t0\t" + PREFIX + "studio.vellum-gate",
    "99624\t75\t" + PREFIX + "studio.spectr-gate",
    "48788\t75\t" + PREFIX + "studio.pulp-gate.slot2",
    "-\t0\tcom.apple.Safari",
    "1234\t0\tcom.danielraffel.pulp.tart-runner-linux",
    "",
])


def write_plist(agents: Path, label: str, state_dir: str | None, runner: str = "") -> None:
    env: dict[str, str] = {}
    if state_dir is not None:
        env["TARTCI_STATE_DIR"] = state_dir
    if runner:
        env["TARTCI_RUNNER_NAME"] = runner
    (agents / f"{label}.plist").write_bytes(
        plistlib.dumps({"Label": label, "EnvironmentVariables": env})
    )


class TestLabelExtraction(unittest.TestCase):
    def test_extracts_only_fleet_labels(self) -> None:
        labels = fld.fleet_labels(LAUNCHCTL_REAL)
        self.assertEqual(len(labels), 5)
        self.assertIn(PREFIX + "studio.pulp-gate", labels)
        self.assertIn(PREFIX + "studio.pulp-gate.slot2", labels)

    def test_non_fleet_labels_are_ignored(self) -> None:
        """Negative control: a loaded non-fleet agent must not become a lane."""
        labels = fld.fleet_labels(LAUNCHCTL_REAL)
        self.assertNotIn("com.apple.Safari", labels)
        self.assertNotIn("com.danielraffel.pulp.tart-runner-linux", labels)

    def test_empty_listing_yields_no_labels(self) -> None:
        self.assertEqual(fld.fleet_labels("PID\tStatus\tLabel\n"), [])


class TestLaneFromPlist(unittest.TestCase):
    def test_state_dir_and_identity_come_from_the_plist(self) -> None:
        lane = fld.lane_from_plist(
            PREFIX + "studio.pulp-gate",
            {"EnvironmentVariables": {
                "TARTCI_STATE_DIR": "/tmp/x/macos-fleet/pulp-gate",
                "TARTCI_RUNNER_NAME": "studio-pulp-gate-01",
            }},
        )
        self.assertEqual(lane.identity, "pulp-gate")
        self.assertEqual(lane.state_dir, Path("/tmp/x/macos-fleet/pulp-gate"))
        self.assertEqual(lane.runner_name, "studio-pulp-gate-01")

    def test_missing_state_dir_is_none_not_a_guessed_default(self) -> None:
        """Guessing a default path is how the stale legacy glob survived."""
        lane = fld.lane_from_plist(PREFIX + "studio.pulp-gate", {"EnvironmentVariables": {}})
        self.assertIsNone(lane.state_dir)


class TestCoverage(unittest.TestCase):
    def test_blind_on_a_host_with_loaded_lanes_is_a_problem(self) -> None:
        """THE regression guard. Matching nothing where launchd says five lanes
        are loaded is a failure to observe and must never render as health."""
        cov = fld.coverage(0, [object()] * 5, "launchctl")  # type: ignore[list-item]
        self.assertTrue(cov.shortfall)
        problem = fld.coverage_problem(cov)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("supervisor_coverage:0/5", problem)

    def test_a_host_with_no_lanes_is_silent(self) -> None:
        """The false-alarm guard. No lanes loaded means nothing to match, which
        is a correct and quiet state, not a fault."""
        cov = fld.coverage(0, [], "launchctl")
        self.assertFalse(cov.shortfall)
        self.assertIsNone(fld.coverage_problem(cov))

    def test_full_coverage_is_silent(self) -> None:
        cov = fld.coverage(5, [object()] * 5, "launchctl")  # type: ignore[list-item]
        self.assertFalse(cov.shortfall)
        self.assertIsNone(fld.coverage_problem(cov))

    def test_unknown_expectation_is_never_a_shortfall(self) -> None:
        """launchctl unreadable means the denominator is unknown. Alarming on
        that is the same defect pointed the other way."""
        cov = fld.coverage(0, None, "launchctl")
        self.assertIsNone(cov.expected)
        self.assertFalse(cov.shortfall)
        self.assertIsNone(fld.coverage_problem(cov))

    def test_partial_coverage_fires(self) -> None:
        cov = fld.coverage(3, [object()] * 5, "launchctl")  # type: ignore[list-item]
        self.assertTrue(cov.shortfall)


class TestDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.agents = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_discovers_every_loaded_lane_with_its_own_state_dir(self) -> None:
        for label, ident in (
            ("studio.pulp-gate", "pulp-gate"),
            ("studio.forge-gate", "forge-gate"),
            ("studio.vellum-gate", "vellum-gate"),
            ("studio.spectr-gate", "spectr-gate"),
            ("studio.pulp-gate.slot2", "pulp-gate-slot2"),
        ):
            write_plist(self.agents, PREFIX + label, f"/s/macos-fleet/{ident}")
        lanes, problems = fld.discover_lanes(
            self.agents, list_reader=lambda: LAUNCHCTL_REAL
        )
        self.assertIsNotNone(lanes)
        assert lanes is not None
        self.assertEqual(len(lanes), 5)
        self.assertEqual(problems, [])
        dirs = fld.lane_state_dirs(lanes)
        self.assertIn(Path("/s/macos-fleet/pulp-gate-slot2"), dirs)
        # The legacy single-lane path is NOT among them -- that is the bug.
        self.assertNotIn(Path("/s/macos"), dirs)

    def test_unreadable_launchctl_reports_unknown_not_empty(self) -> None:
        lanes, problems = fld.discover_lanes(self.agents, list_reader=lambda: None)
        self.assertIsNone(lanes)
        self.assertTrue(any("launchctl_unreadable" in p for p in problems))
        self.assertEqual(fld.lane_state_dirs(lanes), [])

    def test_lane_without_state_dir_is_reported_not_guessed(self) -> None:
        write_plist(self.agents, PREFIX + "studio.pulp-gate", None)
        listing = "PID\tStatus\tLabel\n1\t0\t" + PREFIX + "studio.pulp-gate\n"
        lanes, problems = fld.discover_lanes(self.agents, list_reader=lambda: listing)
        assert lanes is not None
        self.assertEqual(len(lanes), 1)
        self.assertTrue(any("lane_state_dir_missing" in p for p in problems))
        self.assertEqual(fld.lane_state_dirs(lanes), [])

    def test_missing_plist_is_a_problem_not_a_silent_drop(self) -> None:
        listing = "PID\tStatus\tLabel\n1\t0\t" + PREFIX + "studio.ghost\n"
        lanes, problems = fld.discover_lanes(self.agents, list_reader=lambda: listing)
        assert lanes is not None
        self.assertTrue(any("lane_plist_unreadable" in p for p in problems))


class TestRunnerPrefixes(unittest.TestCase):
    def test_derives_fleet_vm_ownership_prefixes(self) -> None:
        lanes = [
            fld.Lane(PREFIX + "studio.pulp-gate", "pulp-gate",
                     Path("/s/pulp-gate"), "studio-pulp-gate-01"),
        ]
        prefixes = fld.runner_name_prefixes(lanes)
        self.assertIn("studio-pulp-gate-", prefixes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
