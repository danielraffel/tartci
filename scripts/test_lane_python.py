#!/usr/bin/env python3
"""The doctor names a lane PATH whose python3 cannot import tomllib.

Lanes run gate_supply decide, macos_fleet_lanes render and host_profile with a
bare python3, which is right only while the lane PATH puts a 3.11+ python3
ahead of /usr/bin. These tests put a real interpreter that cannot import
tomllib (as macOS's /usr/bin/python3 3.9 cannot) first on a lane's PATH.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_doctor as fd  # noqa: E402
import lane_python as lp  # noqa: E402
import testing_support  # noqa: E402

PREFIX = "com.danielraffel.tartci.tart-runner-macos-fleet."


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.agents = self.tmp / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        # The real interpreter with tomllib hidden, as on a 3.9 without it.
        site = self.tmp / "no-tomllib"
        site.mkdir()
        (site / "sitecustomize.py").write_text("import sys\nsys.modules['tomllib'] = None\n")
        python = self.bin / "python3"
        python.write_text(f'#!/bin/sh\nPYTHONPATH="{site}" exec "{sys.executable}" "$@"\n')
        python.chmod(0o755)
        self.version = sys.version.split()[0]

    def lane(self, name: str, path: str) -> None:
        (self.agents / f"{PREFIX}{name}.plist").write_bytes(plistlib.dumps(
            {"Label": PREFIX + name, "EnvironmentVariables": {"PATH": path}}))


class StatusTests(Fixture):
    def test_a_tomllib_less_python3_first_on_the_lane_path_is_a_problem(self) -> None:
        self.lane("studio.pulp-gate", f"{self.bin}:/usr/bin:/bin")
        value = lp.status(self.agents, PREFIX)
        finding = fd.check_lane_python(value)
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "lane_python_no_tomllib"))
        # It names the interpreter and the version it found, and the lane.
        self.assertIn(f"{self.bin / 'python3'} {self.version}", finding.detail)
        self.assertIn(f"{PREFIX}studio.pulp-gate", finding.detail)

    def test_the_same_lane_with_a_tomllib_python3_first_is_ok(self) -> None:
        if sys.version_info < (3, 11):
            self.skipTest("needs a Python 3.11+ to put first on PATH")
        good = self.tmp / "good"
        good.mkdir()
        (good / "python3").symlink_to(sys.executable)
        self.lane("studio.pulp-gate", f"{good}:{self.bin}:/usr/bin:/bin")
        finding = fd.check_lane_python(lp.status(self.agents, PREFIX))
        self.assertEqual((finding.state, finding.code), (fd.OK, "lane_python_tomllib"))

    def test_distinct_paths_are_probed_once_each_and_only_the_bad_one_is_named(self) -> None:
        self.lane("studio.a", f"{self.bin}:/usr/bin:/bin")
        self.lane("studio.b", f"{self.bin}:/usr/bin:/bin")
        empty = self.tmp / "empty"
        empty.mkdir()
        self.lane("studio.c", str(empty))
        calls = []

        def run(argv, env):
            calls.append(env["PATH"])
            return lp._run(argv, env)

        value = lp.status(self.agents, PREFIX, run)
        self.assertEqual(len(calls), 1)  # studio.c has no python3 to run
        rows = {tuple(row["labels"]): row for row in value["rows"]}
        self.assertEqual(rows[(f"{PREFIX}studio.a", f"{PREFIX}studio.b")]["tomllib"], False)
        self.assertEqual(rows[(f"{PREFIX}studio.c",)]["error"], "no python3 on this PATH")
        detail = fd.check_lane_python(value).detail
        self.assertIn("no python3 for " + f"{PREFIX}studio.c", detail)

    def test_no_lane_plist_is_not_applicable(self) -> None:
        finding = fd.check_lane_python(lp.status(self.agents, PREFIX))
        self.assertEqual(finding.code, "lane_python_not_applicable")

    def test_an_unrunnable_python3_is_unknown_not_ok(self) -> None:
        broken = self.tmp / "broken"
        broken.mkdir()
        (broken / "python3").write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
        (broken / "python3").chmod(0o755)
        self.lane("studio.pulp-gate", str(broken))
        finding = fd.check_lane_python(lp.status(self.agents, PREFIX))
        self.assertEqual((finding.state, finding.code), (fd.UNKNOWN, "lane_python_unknown"))
        self.assertIn("boom", finding.detail)

    def test_every_code_has_a_reason_row(self) -> None:
        for code in ("lane_python_no_tomllib", "lane_python_not_applicable",
                     "lane_python_tomllib", "lane_python_unknown"):
            self.assertIn(code, fd.CODES)


class DoctorTests(Fixture):
    @testing_support.requires_tomllib
    def test_the_doctor_runs_the_probe_on_the_installed_lanes(self) -> None:
        self.lane("studio.pulp-gate", f"{self.bin}:/usr/bin:/bin")
        rows = fd.collect(home=self.tmp, skip_census=True, probe=lambda root: {"error": "stub"})
        finding = next(row for row in rows if row.check == "lane_python")
        self.assertEqual(finding.code, "lane_python_no_tomllib")


if __name__ == "__main__":
    unittest.main()
