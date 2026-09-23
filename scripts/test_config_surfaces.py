#!/usr/bin/env python3
"""Installed-vs-declared drift surfaces without anyone remembering to ask.

pool status, the periodic watchdog heal pass, and pool on / on --plan all
report profile drift and the published-supply verdict. None of them refuses
or acts on it: that would turn a configuration difference into an outage.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import macos_fleet_lanes as fleet
from test_pool_plan import FakePoolHost

ROOT = Path(__file__).resolve().parents[1]
M3 = ROOT / "profiles" / "m3-macos-fleet.toml"
WATCHDOG = ROOT / "scripts" / "tartci_launchd_watchdog.py"
DRIFTED = M3.read_text().replace("github_api_timeout_seconds = 30\n", "")
MISMATCHED = M3.read_text().replace('"vellum-host-m3"]', '"vellum-host-m3", "extra"]')
MUTATIONS = ("bootout", "bootstrap", "kickstart", "disable", "enable", "unload", "load")


class ReadinessCarriesConfigTests(unittest.TestCase):
    def _readiness(self, text: str | None) -> dict:
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "profile.toml"
            if text is not None:
                config.write_text(text)
            return fleet.fleet_readiness(Path(td) / "absent-receipt.json", config,
                                         Path(td), ROOT, True, "on")

    def test_in_sync_drift_and_mismatch_are_reported(self) -> None:
        clean = self._readiness(M3.read_text())["config"]
        self.assertEqual((clean["profile_drift"]["state"], clean["supply"]["state"]),
                         ("in_sync", "match"))
        drift = self._readiness(DRIFTED)["config"]["profile_drift"]
        self.assertEqual(drift["state"], "drift")
        self.assertIn("host.github_api_timeout_seconds", drift["keys"])
        supply = self._readiness(MISMATCHED)["config"]["supply"]
        self.assertEqual(supply["state"], "mismatch")
        self.assertIn("vellum-gate=LABELS_DIFFER", supply["mismatched"])

    def test_drift_never_clears_fleet_ready_by_itself(self) -> None:
        value = self._readiness(DRIFTED)
        codes = {p["code"] for p in value["problems"]}
        self.assertNotIn("profile_drift", codes)

    def test_unreadable_profile_is_unknown(self) -> None:
        config = self._readiness(M3.read_text().replace('name = "m3-macos-fleet"\n', ""))["config"]
        self.assertEqual(config["profile_drift"]["state"], "unknown")
        self.assertEqual(fleet.render_config_verdicts(None).splitlines(),
                         ["profile drift: UNKNOWN (not checked from here)",
                          "supply: UNKNOWN (not checked from here)"])


class PoolStatusTests(unittest.TestCase):
    def _host(self, td: str, profile: str | None) -> FakePoolHost:
        host = FakePoolHost(Path(td), busy=False)
        if profile is not None:
            (host.root / "home" / ".config" / "tartci" / "macos-fleet-profile.toml").write_text(profile)
        return host

    def test_status_text_and_json_show_drift(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = self._host(td, M3.read_text())
            text = host.pool("status").stdout
            self.assertIn("profile drift: ok", text)
            self.assertIn("supply: ok", text)
            (host.root / "home" / ".config" / "tartci" / "macos-fleet-profile.toml").write_text(DRIFTED)
            text = host.pool("status").stdout
            self.assertIn("profile drift: DRIFT (host.github_api_timeout_seconds)", text)
            value = json.loads(host.pool("status", "--json").stdout)
            self.assertEqual(value["fleet"]["config"]["profile_drift"]["state"], "drift")

    def test_status_shows_unknown_never_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = self._host(td, M3.read_text().replace('id = "studio"', 'id = "nohost"', 1)
                              .replace('name = "m3-macos-fleet"\n', ""))
            text = host.pool("status").stdout
            self.assertRegex(text, r"profile drift: UNKNOWN \(")
            self.assertRegex(text, r"supply: UNKNOWN \(")
            self.assertNotIn("profile drift: ok", text)


class PoolOnReportsDriftTests(unittest.TestCase):
    def test_on_plan_and_on_print_drift_and_do_not_refuse_on_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td), busy=False)
            profile = host.root / "home" / ".config" / "tartci" / "macos-fleet-profile.toml"
            profile.write_text(DRIFTED)
            plan = host.pool("on", "--plan")
            self.assertIn("profile drift: DRIFT", plan.stdout)
            self.assertIn("does not refuse on this", plan.stdout)
            real = host.pool("on")
            self.assertIn("profile drift: DRIFT", real.stdout)
            self.assertIn("does not refuse on this", real.stdout)
            # The fake receipt is invalid, so both still refuse with the
            # receipt code (7): drift added no refusal of its own.
            self.assertEqual((plan.returncode, real.returncode), (7, 7))
            # Control: an in-sync profile prints ok and no disclaimer.
            profile.write_text(M3.read_text())
            clean = host.pool("on", "--plan")
            self.assertIn("profile drift: ok", clean.stdout)
            self.assertNotIn("does not refuse on this", clean.stdout)


class WatchdogWarnTests(unittest.TestCase):
    def _run(self, td: Path, profile: str, *extra: str) -> subprocess.CompletedProcess[str]:
        config = td / "profile.toml"
        config.write_text(profile)
        bindir = td / "bin"
        bindir.mkdir(exist_ok=True)
        (bindir / "launchctl").write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{td}/launchctl.log"\nexit 1\n')
        (bindir / "launchctl").chmod(0o755)
        agents = td / "agents"
        agents.mkdir(exist_ok=True)
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
               "TARTCI_HOME": str(td / "tartci-home"), "TARTCI_TART_CLI": "/nonexistent"}
        return subprocess.run(
            [sys.executable, str(WATCHDOG), "--launch-agents-dir", str(agents),
             "--fleet-config", str(config), "--fleet-receipt", str(td / "none.json"), *extra],
            env=env, text=True, capture_output=True, check=False)

    def test_heal_pass_warns_rate_limited_and_never_acts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            first = self._run(td, DRIFTED)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("WARN config: profile_drift=DRIFT", first.stdout)
            second = self._run(td, DRIFTED)
            self.assertNotIn("WARN config", second.stdout)  # rate-limited
            changed = self._run(td, MISMATCHED)
            self.assertIn("WARN config:", changed.stdout)  # new verdict warns again
            self.assertIn("supply=MISMATCH", changed.stdout)
            again = self._run(td, MISMATCHED, "--config-warn-interval-seconds", "0")
            self.assertIn("WARN config:", again.stdout)
            as_json = json.loads(self._run(td, DRIFTED, "--json").stdout)
            self.assertEqual(as_json["config"]["profile_drift"]["state"], "drift")
            calls = (td / "launchctl.log").read_text() if (td / "launchctl.log").exists() else ""
            self.assertFalse([line for line in calls.splitlines()
                              if line.split(" ", 1)[0] in MUTATIONS], calls)

    def test_clean_profile_does_not_warn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            proc = self._run(Path(raw), M3.read_text())
            self.assertNotIn("WARN config", proc.stdout)
            self.assertIn("launchd-watchdog", proc.stdout)


class CommitProvenanceTests(unittest.TestCase):
    def test_verify_supply_names_both_commits_when_they_differ(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            installed = Path(td) / "p.toml"
            installed.write_text(M3.read_text())
            receipt = Path(td) / "r.json"
            receipt.write_text(json.dumps({"support": {"source_commit": "a" * 40}}))
            proc = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-supply",
                 "--installed", str(installed), "--receipt", str(receipt)],
                cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("NOTE: published supply is from tartci", proc.stdout)
            self.assertIn("aaaaaaaaaaaa", proc.stdout)
            head = fleet.git_head(ROOT)
            receipt.write_text(json.dumps({"support": {"source_commit": head}}))
            same = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-supply",
                 "--installed", str(installed), "--receipt", str(receipt)],
                cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertNotIn("NOTE:", same.stdout)
            self.assertIn(f"published at: {head}", same.stdout)

    def test_installed_generation_takes_its_manifest_commit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "fleet").mkdir()
            (root / ".tartci-support-manifest.json").write_text(
                json.dumps({"source_commit": "b" * 40}))
            self.assertEqual(fleet.published_commit(root / "fleet" / "advertised-labels.json"),
                             "b" * 40)
        self.assertIsNone(fleet.published_commit("https://example.com/x.json"))


class RegenerationGateTests(unittest.TestCase):
    def test_ci_runs_the_regeneration_gate(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertRegex(workflow, r"python3 -m unittest discover -s scripts -p 'test_\*\.py'")
        self.assertTrue((ROOT / "scripts" / "test_fleet_supply.py").is_file())

    def test_profile_edit_without_regenerating_fails_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copytree(ROOT / "profiles", root / "profiles")
            shutil.copytree(ROOT / "fleet", root / "fleet")
            committed = (root / "fleet" / "advertised-labels.json").read_text()
            # Control: an untouched copy regenerates byte-identical.
            self.assertEqual(fleet.render_published(fleet.published_snapshot(root)), committed)
            profile = root / "profiles" / "m3-macos-fleet.toml"
            profile.write_text(profile.read_text().replace(
                '"vellum-host-m3"]', '"vellum-host-m3", "new-label"]'))
            self.assertNotEqual(fleet.render_published(fleet.published_snapshot(root)), committed)

    def test_published_file_has_no_timestamp(self) -> None:
        text = (ROOT / "fleet" / "advertised-labels.json").read_text()
        self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
        self.assertNotIn("generated_at", text)


if __name__ == "__main__":
    unittest.main()
