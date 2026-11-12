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

import testing_support  # noqa: E402
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


ROOT = Path(__file__).resolve().parents[1]


def installed_plist(label: str) -> dict:
    """The plist the fleet installer writes for `label`, from the checked-in m3 profile.

    Fixtures are rendered, never hand-written: a hand-written fixture set
    TARTCI_RUNNER_NAME, which the installer never writes, and kept discovery
    green while it named no lane on any host.
    """
    import tomllib
    import macos_fleet_lanes as installer
    profile = tomllib.loads((ROOT / "profiles" / "m3-macos-fleet.toml").read_text())
    for lane in profile["lane"]:
        for slot in range(1, lane.get("supervisors", 1) + 1):
            plist = installer.lane_plist(profile, lane, slot=slot)
            if plist["Label"] == label:
                return plist
    raise AssertionError(f"the m3 profile renders no lane {label}")


def write_plist(agents: Path, label: str, state_dir: str | None) -> None:
    plist = installed_plist(label)
    env = plist["EnvironmentVariables"]
    if state_dir is None:
        env.pop("TARTCI_STATE_DIR", None)
    else:
        env["TARTCI_STATE_DIR"] = state_dir
    (agents / f"{label}.plist").write_bytes(plistlib.dumps(plist))


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
    @testing_support.requires_tomllib
    def test_state_dir_and_identity_come_from_the_plist(self) -> None:
        label = PREFIX + "studio.pulp-gate"
        plist = installed_plist(label)
        self.assertNotIn("TARTCI_RUNNER_NAME", plist["EnvironmentVariables"])
        plist["EnvironmentVariables"]["TARTCI_STATE_DIR"] = "/tmp/x/macos-fleet/pulp-gate"
        lane = fld.lane_from_plist(label, plist)
        self.assertEqual(lane.identity, "pulp-gate")
        self.assertEqual(lane.state_dir, Path("/tmp/x/macos-fleet/pulp-gate"))
        self.assertEqual(lane.runner_name, "studio-pulp-gate-01")

    @testing_support.requires_tomllib
    def test_every_discovered_lane_carries_the_supervisors_runner_name(self) -> None:
        for label, name in (("studio.pulp-gate", "studio-pulp-gate-01"),
                            ("studio.pulp-gate.slot2", "studio-pulp-gate-slot2-02")):
            with self.subTest(label=label):
                lane = fld.lane_from_plist(PREFIX + label, installed_plist(PREFIX + label))
                self.assertEqual(lane.runner_name, name)

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

    @testing_support.requires_tomllib
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

    @testing_support.requires_tomllib
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


class TestInstalledPlistContract(unittest.TestCase):
    """Discovery reads the runner name the supervisor itself derives, from the
    plists the installer actually writes.

    Installed fleet plists set TARTCI_RUNNER_NAME_PREFIX and a slot, never
    TARTCI_RUNNER_NAME. Discovery read only the latter, so every lane was
    nameless, no VM-ownership prefix matched a fleet VM, and the VM janitor
    deleted no stopped VM from 2026-08-15 on. The fixtures then set
    TARTCI_RUNNER_NAME by hand, which is why nothing failed; they are now
    rendered by installed_plist(). This renders every lane
    and slot of every checked-in profile with the installer's own code and
    asks the supervisor (`runner.sh --print-name`) for its name.
    """

    ROOT = Path(__file__).resolve().parents[1]

    def fake_chrome(self) -> Path:
        import tempfile
        if not hasattr(self, "_chrome"):
            tmp = Path(tempfile.mkdtemp())
            self.addCleanup(__import__("shutil").rmtree, tmp, True)
            app = tmp / "Google Chrome.app"
            binary = app / "Contents" / "MacOS" / "Google Chrome"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            self._chrome = app
        return self._chrome

    def rendered(self):
        import os
        import tomllib
        import macos_fleet_lanes as installer
        for profile in sorted((self.ROOT / "profiles").glob("*-macos-fleet.toml")):
            data = tomllib.loads(profile.read_text())
            for lane in data.get("lane", []):
                for slot in range(1, lane.get("supervisors", 1) + 1):
                    yield profile.name, installer.lane_plist(data, lane, slot=slot), os

    @testing_support.requires_tomllib
    def test_discovery_names_every_rendered_lane_as_its_supervisor_does(self) -> None:
        import subprocess
        runner = self.ROOT / "providers" / "tart-macos" / "runner.sh"
        checked = 0
        for profile, plist, os in self.rendered():
            with self.subTest(profile=profile, label=plist["Label"]):
                env_vars = plist["EnvironmentVariables"]
                self.assertNotIn("TARTCI_RUNNER_NAME", env_vars)   # the installer's real shape
                lane = fld.lane_from_plist(plist["Label"], plist)
                env = {**{k: str(v) for k, v in env_vars.items()},
                       "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "HOME": os.environ.get("HOME", "/tmp")}
                if "TARTCI_RUNNER_CHROME_APP_DIR" in env:
                    # The supervisor refuses to start without the host's Chrome;
                    # a stand-in executable satisfies that on a host without it.
                    env["TARTCI_RUNNER_CHROME_APP_DIR"] = str(self.fake_chrome())
                supervisor = subprocess.run(["bash", str(runner), "--print-name"], env=env,
                                            capture_output=True, text=True, timeout=60)
                self.assertEqual(supervisor.returncode, 0, supervisor.stderr[-400:])
                self.assertTrue(lane.runner_name)
                self.assertEqual(lane.runner_name, supervisor.stdout.strip())
                # A VM that lane boots is `<runner>-<pid>-<n>`; its prefix must own it.
                vm = f"{lane.runner_name}-12345-7"
                self.assertTrue(any(vm.startswith(p) for p in fld.runner_name_prefixes([lane])), vm)
                checked += 1
        self.assertGreater(checked, 10)   # every profile's lanes were actually rendered


if __name__ == "__main__":
    unittest.main(verbosity=2)
