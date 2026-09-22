#!/usr/bin/env python3
"""Installed macOS fleet profile vs its checked-in source."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_doctor as doctor
import macos_fleet_lanes as fleet

ROOT = Path(__file__).resolve().parents[1]
M3 = ROOT / "profiles" / "m3-macos-fleet.toml"


class ProfileDriftTests(unittest.TestCase):
    def _host(self, td: str, body: str) -> tuple[Path, Path]:
        profiles = Path(td) / "profiles"
        profiles.mkdir()
        for path in (ROOT / "profiles").glob("*.toml"):
            shutil.copy(path, profiles / path.name)
        installed = Path(td) / "macos-fleet-profile.toml"
        installed.write_text(body)
        return installed, profiles

    def _cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ROOT / "tartci"), "fleet-macos", "profile-drift", *args],
                              cwd=ROOT, text=True, capture_output=True, check=False)

    def test_identical_copy_is_in_sync(self) -> None:
        # Control for every drift finding below: the same instrument reports
        # a clean install as clean, and it did compare real keys.
        with tempfile.TemporaryDirectory() as td:
            installed, profiles = self._host(td, M3.read_text())
            result = fleet.profile_drift(installed, profiles)
            self.assertEqual(result["state"], "in_sync", result)
            self.assertEqual(result["name"], "m3-macos-fleet")
            self.assertTrue(result["checked_in"].endswith("m3-macos-fleet.toml"))
            proc = self._cli("--installed", str(installed), "--profiles-dir",
                             str(profiles), "--strict")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_missing_host_timeout_and_lane_process_type_is_drift(self) -> None:
        text = M3.read_text()
        self.assertIn("github_api_timeout_seconds", text)
        self.assertIn("process_type", text)
        lines = [line for line in text.splitlines()
                 if not line.startswith(("github_api_timeout_seconds", "process_type"))]
        with tempfile.TemporaryDirectory() as td:
            installed, profiles = self._host(td, "\n".join(lines) + "\n")
            result = fleet.profile_drift(installed, profiles)
            self.assertEqual(result["state"], "drift")
            self.assertIn("host.github_api_timeout_seconds", result["missing_in_installed"])
            self.assertIn("lane[pulp-gate].process_type", result["missing_in_installed"])
            self.assertFalse(result["extra_in_installed"])
            self.assertFalse(result["changed"])
            strict = self._cli("--installed", str(installed), "--profiles-dir",
                               str(profiles), "--strict")
            self.assertEqual(strict.returncode, 1)
            self.assertIn("host.github_api_timeout_seconds", strict.stdout)
            lenient = self._cli("--installed", str(installed), "--profiles-dir",
                                str(profiles))
            self.assertEqual(lenient.returncode, 0)
            self.assertIn("DRIFT", lenient.stdout)

    def test_changed_and_extra_keys_are_named(self) -> None:
        text = M3.read_text().replace('id = "studio"', 'id = "studio"\ncurrent_job_attempt_timeout_seconds = 60', 1)
        text = text.replace('labels = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]',
                            'labels = ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]', 1)
        with tempfile.TemporaryDirectory() as td:
            installed, profiles = self._host(td, text)
            result = fleet.profile_drift(installed, profiles)
            self.assertEqual(result["state"], "drift")
            self.assertIn("host.current_job_attempt_timeout_seconds", result["extra_in_installed"])
            self.assertIn("lane[pulp-gate].labels", result["changed"])

    def test_lane_order_is_not_drift(self) -> None:
        data = M3.read_text()
        head, _, rest = data.partition("[[lane]]")
        lanes = ["[[lane]]" + block for block in rest.split("[[lane]]")]
        with tempfile.TemporaryDirectory() as td:
            installed, profiles = self._host(td, head + "".join(reversed(lanes)))
            self.assertEqual(fleet.profile_drift(installed, profiles)["state"], "in_sync")

    def test_unmatched_or_nameless_or_absent_is_unknown_not_clean(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            installed, profiles = self._host(
                td, M3.read_text().replace('name = "m3-macos-fleet"', 'name = "nope"'))
            result = fleet.profile_drift(installed, profiles)
            self.assertEqual(result["state"], "unknown")
            proc = self._cli("--installed", str(installed), "--profiles-dir", str(profiles))
            self.assertEqual(proc.returncode, 2)
            installed.write_text(M3.read_text().replace('name = "m3-macos-fleet"\n', ""))
            self.assertEqual(fleet.profile_drift(installed, profiles)["state"], "unknown")
            self.assertEqual(
                fleet.profile_drift(Path(td) / "absent.toml", profiles)["state"], "unknown")

    def test_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            installed, profiles = self._host(td, M3.read_text())
            proc = self._cli("--installed", str(installed), "--profiles-dir",
                             str(profiles), "--json")
            self.assertEqual(json.loads(proc.stdout)["schema"], "tartci.profile-drift/v1")


class DoctorFindingTests(unittest.TestCase):
    def test_doctor_reports_drift_in_sync_absent_and_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            config = home / ".config" / "tartci"
            config.mkdir(parents=True)
            (home / "Library" / "LaunchAgents").mkdir(parents=True)

            def finding(**kwargs) -> doctor.Finding:
                rows = doctor.collect(home=home, skip_census=True,
                                      probe=lambda root: {"error": "stub"}, **kwargs)
                return next(row for row in rows if row.check == "profile_drift")

            self.assertEqual(finding().state, doctor.NOT_APPLICABLE)
            (config / "macos-fleet-profile.toml").write_text(M3.read_text())
            clean = finding()
            self.assertEqual((clean.state, clean.code), (doctor.OK, "profile_in_sync"))
            (config / "macos-fleet-profile.toml").write_text(
                M3.read_text().replace("github_api_timeout_seconds = 30\n", ""))
            drifted = finding()
            self.assertEqual((drifted.state, drifted.code), (doctor.PROBLEM, "profile_drift"))
            self.assertIn("host.github_api_timeout_seconds", drifted.detail)
            blind = finding(drift_probe=lambda path: (None, "no tomllib"))
            self.assertEqual((blind.state, blind.code),
                             (doctor.UNKNOWN, "profile_drift_unknown"))

    def test_every_new_code_has_a_reason_row(self) -> None:
        reasons = doctor.load_reasons()
        for code in ("no_installed_profile", "profile_in_sync", "profile_drift",
                     "profile_drift_unknown"):
            self.assertIn(code, doctor.CODES)
            self.assertIn(code, reasons)


if __name__ == "__main__":
    unittest.main()
