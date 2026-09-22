#!/usr/bin/env python3
"""Published declared supply (fleet/advertised-labels.json) and its host fact-check."""

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
PUBLISHED = ROOT / "fleet" / "advertised-labels.json"
M3 = ROOT / "profiles" / "m3-macos-fleet.toml"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(ROOT / "tartci"), "fleet-macos", *args],
                          cwd=ROOT, text=True, capture_output=True, check=False)


class PublishedSupplyTests(unittest.TestCase):
    def test_committed_file_is_current(self) -> None:
        """The CI gate: fleet/advertised-labels.json cannot go stale in-repo."""
        self.assertEqual(PUBLISHED.read_text(), fleet.render_published(fleet.published_snapshot()),
                         "fleet/advertised-labels.json is stale: run `tartci fleet-macos "
                         "advertised-labels --publish > fleet/advertised-labels.json`")
        proc = _cli("advertised-labels", "--check", str(PUBLISHED))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_check_rejects_a_stale_copy(self) -> None:
        # Control for the gate above: the same check does fail on a change.
        with tempfile.TemporaryDirectory() as td:
            stale = Path(td) / "a.json"
            value = json.loads(PUBLISHED.read_text())
            value["registrations"][0]["labels"].append("pulp-gate-fast")
            stale.write_text(json.dumps(value, indent=2) + "\n")
            proc = _cli("advertised-labels", "--check", str(stale))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("stale", proc.stderr)

    def test_published_form_carries_no_commit_and_every_fleet_profile(self) -> None:
        value = json.loads(PUBLISHED.read_text())
        self.assertEqual(value["schema"], "tartci.advertised-labels/v1")
        self.assertIsNone(value["generated_from"]["commit"])
        self.assertEqual(value["generated_from"]["profiles"],
                         [f"profiles/{p.name}" for p in sorted(
                             (ROOT / "profiles").glob("*-macos-fleet.toml"))])
        self.assertGreaterEqual(len(value["generated_from"]["profiles"]), 3)
        self.assertIn({"profile": "m5-macos-fleet", "host_id": "m5",
                       "launchd_label": "actions.runner.danielraffel-pulp.pulp-preamble-m5",
                       "runner_name": "pulp-preamble-m5"}, value["persistent_runners"])

    def test_a_new_fleet_profile_is_published_without_listing_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copytree(ROOT / "profiles", root / "profiles")
            before = fleet.published_snapshot(root)
            m1 = ROOT / "profiles" / "m1-macos-fleet.toml"
            text = m1.read_text().replace('name = "m1-macos-fleet"', 'name = "x9-macos-fleet"')
            text = text.replace('id = "m1"', 'id = "x9"', 1)
            (root / "profiles" / "x9-macos-fleet.toml").write_text(text)
            after = fleet.published_snapshot(root)
            self.assertFalse(any(p.endswith("x9-macos-fleet.toml")
                                 for p in before["generated_from"]["profiles"]))
            self.assertTrue(any(p.endswith("x9-macos-fleet.toml")
                                for p in after["generated_from"]["profiles"]))
            hosts = {row["host_id"] for row in after["registrations"]}
            self.assertIn("x9", hosts)
            # A non-fleet profile in the same directory is not swept in.
            self.assertNotIn("normal-local-fast", " ".join(after["generated_from"]["profiles"]))

    def test_all_matches_explicit_profiles(self) -> None:
        proc = _cli("advertised-labels", "--all", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)
        self.assertEqual(value["registrations"],
                         json.loads(PUBLISHED.read_text())["registrations"])
        self.assertRegex(value["generated_from"]["commit"] or "", r"^[0-9a-f]{40}$")


class VerifySupplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.published = json.loads(PUBLISHED.read_text())
        self.td = tempfile.TemporaryDirectory()
        self.installed = Path(self.td.name) / "profile.toml"

    def tearDown(self) -> None:
        self.td.cleanup()

    def verdicts(self, text: str, published: dict | None = None) -> dict:
        self.installed.write_text(text)
        return fleet.verify_supply(self.installed, published or self.published)

    def test_checked_in_profile_matches(self) -> None:
        # Control for every mismatch below.
        result = self.verdicts(M3.read_text())
        self.assertEqual(result["state"], "match")
        self.assertEqual(result["host_id"], "studio")
        self.assertEqual(len(result["lanes"]), 5)
        self.assertTrue(all(row["verdict"] == fleet.MATCH for row in result["lanes"]))

    def test_changed_labels_differ(self) -> None:
        result = self.verdicts(M3.read_text().replace(
            '"vellum-host-m3"]', '"vellum-host-m3", "extra"]'))
        self.assertEqual(result["state"], "mismatch")
        row = next(r for r in result["lanes"] if r["lane"] == "vellum-gate")
        self.assertEqual(row["verdict"], fleet.LABELS_DIFFER)
        self.assertIn("labels", row["differs"])

    def test_installed_only_and_declared_only(self) -> None:
        text = M3.read_text()
        # Drop spectr-gate from the installed copy, rename it to a new lane.
        installed = text.replace('id = "spectr-gate"', 'id = "spectr-new"')
        result = self.verdicts(installed)
        verdicts = {row["lane"]: row["verdict"] for row in result["lanes"]}
        self.assertEqual(verdicts["spectr-new"], fleet.INSTALLED_ONLY)
        self.assertEqual(verdicts["spectr-gate"], fleet.DECLARED_ONLY)
        self.assertEqual(result["state"], "mismatch")

    def test_unknown_host_or_unreadable_is_never_match(self) -> None:
        text = M3.read_text().replace('id = "studio"', 'id = "nohost"', 1)
        self.assertEqual(self.verdicts(text)["state"], "unknown")
        self.assertEqual(fleet.verify_supply(Path(self.td.name) / "absent.toml",
                                             self.published)["state"], "unknown")
        self.assertEqual(fleet.verify_supply(self.installed, None, "boom")["state"], "unknown")
        self.installed.write_text("schema = 1\n")
        self.assertEqual(fleet.verify_supply(self.installed, self.published)["state"], "unknown")

    def test_cli_exit_codes(self) -> None:
        self.installed.write_text(M3.read_text())
        ok = _cli("verify-supply", "--installed", str(self.installed))
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("MATCH", ok.stdout)
        self.installed.write_text(M3.read_text().replace('"vellum-host-m3"]', '"x"]'))
        self.assertEqual(_cli("verify-supply", "--installed", str(self.installed)).returncode, 1)
        bad = Path(self.td.name) / "bad.json"
        bad.write_text("{}")
        proc = _cli("verify-supply", "--installed", str(self.installed), "--published", str(bad))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("UNKNOWN", proc.stdout)

    def test_doctor_finding(self) -> None:
        home = Path(self.td.name) / "home"
        config = home / ".config" / "tartci"
        config.mkdir(parents=True)
        (home / "Library" / "LaunchAgents").mkdir(parents=True)

        def finding() -> doctor.Finding:
            rows = doctor.collect(home=home, skip_census=True,
                                  probe=lambda root: {"error": "stub"})
            return next(row for row in rows if row.check == "supply")

        self.assertEqual(finding().state, doctor.NOT_APPLICABLE)
        (config / "macos-fleet-profile.toml").write_text(M3.read_text())
        self.assertEqual((finding().state, finding().code), (doctor.OK, "supply_match"))
        (config / "macos-fleet-profile.toml").write_text(
            M3.read_text().replace('"vellum-host-m3"]', '"x"]'))
        bad = finding()
        self.assertEqual((bad.state, bad.code), (doctor.PROBLEM, "supply_mismatch"))
        self.assertIn("vellum-gate=LABELS_DIFFER", bad.detail)
        (config / "macos-fleet-profile.toml").write_text(
            M3.read_text().replace('id = "studio"', 'id = "nohost"', 1))
        self.assertEqual(finding().code, "supply_unknown")


if __name__ == "__main__":
    unittest.main()
