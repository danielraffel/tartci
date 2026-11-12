#!/usr/bin/env python3
"""Tests for the read-only scan App wrapper and its profile wiring.

gh-scan-app mints an installation token through a stub `ghapp`, caches it, and
execs a stub `gh` with GH_TOKEN set. The profile tests prove github_scan_app is
validated like github_app and renders only references, and that the scan call
sites take SCAN_GH_CLI while writes stay on GH_CLI.

Run:  python3 scripts/test_gh_scan_app.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import testing_support

testing_support.skip_module_without_tomllib()

import tomllib  # noqa: E402

import macos_fleet_lanes as fleet  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "gh-scan-app"
CONFIG = ROOT / "profiles" / "m1-macos-fleet.toml"
# The checked-in host profile owns the home directory the App paths must sit under.
HOME = tomllib.loads(CONFIG.read_text())["host"]["home"] + "/.config/shipyard"
SCAN_TABLE = (
    "[github_scan_app]\n"
    'id = "5665767"\n'
    f'private_key_path = "{HOME}/github-apps/generous-admission-reader.private-key.pem"\n'
    f'cache_dir = "{HOME}/ghapp-cache-reader"\n'
)


def without_scan_table(body: str) -> str:
    stripped = re.sub(r"(?ms)^\[github_scan_app\]\n.*?(?=^\[)", "", body, count=1)
    assert stripped != body, "control: the checked-in profile declares the table"
    return stripped


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class WrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)
        self.key = self.dir / "reader.pem"
        self.key.write_text("fixture key reference only\n")
        self.cache = self.dir / "cache"
        self.mints = self.dir / "mints"
        self.expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _stub(self.dir / "ghapp",
              f'echo "$@" >> "{self.mints}"\n'
              f'printf \'{{"token":"minted-token","expires_at":"{self.expires}"}}\'\n')
        _stub(self.dir / "gh",
              'echo "GH_TOKEN=$GH_TOKEN GITHUB_TOKEN=${GITHUB_TOKEN:-unset} ARGS=$*"\n')

    def run_wrapper(self, *args: str, **overrides: str):
        env = {
            "PATH": os.environ["PATH"],
            "TARTCI_SCAN_APP_ID": "5665767",
            "TARTCI_SCAN_APP_PRIVATE_KEY_PATH": str(self.key),
            "TARTCI_SCAN_APP_CACHE_DIR": str(self.cache),
            "SHIPYARD_GH_APP_REPO": "Generous-Corp/pulp",
            "TARTCI_SCAN_APP_GHAPP": str(self.dir / "ghapp"),
            "TARTCI_SCAN_APP_GH": str(self.dir / "gh"),
            "GITHUB_TOKEN": "ambient-must-not-leak",
        }
        env.update(overrides)
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run([str(WRAPPER), *args], env=env, text=True,
                              capture_output=True, check=False)

    def test_mints_with_reader_app_and_runs_gh_with_that_token(self) -> None:
        result = self.run_wrapper("api", "rate_limit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GH_TOKEN=minted-token", result.stdout)
        self.assertIn("GITHUB_TOKEN=unset", result.stdout)
        self.assertIn("ARGS=api rate_limit", result.stdout)
        mint = self.mints.read_text()
        self.assertIn("--app-id 5665767", mint)
        self.assertIn("--repo Generous-Corp/pulp", mint)

    def test_token_is_cached_so_a_second_call_does_not_mint(self) -> None:
        self.run_wrapper("api", "a")
        self.run_wrapper("api", "b")
        self.assertEqual(len(self.mints.read_text().splitlines()), 1)
        cached = next(self.cache.glob("scan-token-*.json"))
        self.assertEqual(stat.S_IMODE(cached.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o700)

    def test_a_token_near_expiry_is_reminted(self) -> None:
        self.run_wrapper("api", "a")
        cached = next(self.cache.glob("scan-token-*.json"))
        soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
        cached.write_text(json.dumps({"token": "stale", "expires_at": soon}))
        result = self.run_wrapper("api", "b")
        self.assertIn("GH_TOKEN=minted-token", result.stdout)
        self.assertEqual(len(self.mints.read_text().splitlines()), 2)

    def test_fails_closed_and_never_falls_back_to_ambient_gh(self) -> None:
        _stub(self.dir / "ghapp", "exit 1\n")
        result = self.run_wrapper("api", "rate_limit")
        self.assertEqual(result.returncode, 75)
        self.assertNotIn("GH_TOKEN", result.stdout)
        self.assertIn("token mint failed", result.stderr)

    def test_refuses_missing_key_missing_config_and_bad_repo(self) -> None:
        self.assertEqual(self.run_wrapper(
            "api", "x", TARTCI_SCAN_APP_PRIVATE_KEY_PATH=str(self.dir / "none.pem")
        ).returncode, 75)
        self.assertEqual(self.run_wrapper("api", "x", TARTCI_SCAN_APP_ID="").returncode, 75)
        self.assertEqual(self.run_wrapper(
            "api", "x", SHIPYARD_GH_APP_REPO="not-a-repo").returncode, 75)
        self.assertFalse(self.mints.exists())

    def test_refuses_a_gh_shim_that_hands_off_to_ghapp(self) -> None:
        _stub(self.dir / "gh", 'exec "$HOME/.local/bin/ghapp" "$@"\n')
        result = self.run_wrapper("api", "rate_limit")
        self.assertEqual(result.returncode, 75)
        self.assertIn("no real gh", result.stderr)

    def test_mint_without_expiry_is_cached_with_a_fallback_lifetime(self) -> None:
        _stub(self.dir / "ghapp", 'printf \'{"token":"minted-token"}\'\n')
        self.assertEqual(self.run_wrapper("api", "a").returncode, 0)
        self.run_wrapper("api", "b")
        cached = json.loads(next(self.cache.glob("scan-token-*.json")).read_text())
        self.assertIn("expires_at", cached)

    def test_corrupt_cache_is_reminted_and_stale_tmp_swept(self) -> None:
        self.run_wrapper("api", "a")
        cached = next(self.cache.glob("scan-token-*.json"))
        cached.write_text("{not json")
        old = self.cache / ".scan-token-old.json.1.tmp"
        old.write_text("x")
        os.utime(old, (1, 1))
        result = self.run_wrapper("api", "b")
        self.assertIn("GH_TOKEN=minted-token", result.stdout)
        self.assertFalse(old.exists())

    def test_enterprise_token_is_not_passed_through(self) -> None:
        _stub(self.dir / "gh", 'echo "ENT=${GH_ENTERPRISE_TOKEN:-unset}"\n')
        result = self.run_wrapper("api", "x", GH_ENTERPRISE_TOKEN="leak")
        self.assertIn("ENT=unset", result.stdout)

    def test_wrapper_never_prints_the_token_on_failure(self) -> None:
        _stub(self.dir / "ghapp", 'echo \'{"token":"leaky"}\'\nexit 3\n')
        result = self.run_wrapper("api", "rate_limit")
        self.assertNotIn("leaky", result.stdout + result.stderr)


class ProfileAndCallSiteTests(unittest.TestCase):
    def profile(self, extra: str = SCAN_TABLE) -> str:
        base = without_scan_table(CONFIG.read_text())
        marker = "[guest_network]"
        self.assertIn(marker, base)
        return base.replace(marker, extra + "\n" + marker, 1)

    def validate(self, body: str):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "p.toml"
            path.write_text(body)
            return subprocess.run([str(ROOT / "tartci"), "fleet-macos", "validate", str(path)],
                                  text=True, capture_output=True, check=False)

    def test_scan_app_table_validates_and_renders_references_only(self) -> None:
        body = self.profile()
        self.assertEqual(self.validate(body).returncode, 0)
        data = tomllib.loads(body)
        env = fleet.lane_plist(data, data["lane"][0])["EnvironmentVariables"]
        self.assertEqual(env["TARTCI_SCAN_APP_ID"], "5665767")
        self.assertTrue(env["TARTCI_SCAN_APP_PRIVATE_KEY_PATH"].endswith(
            "generous-admission-reader.private-key.pem"))
        self.assertEqual(env["SHIPYARD_GITHUB_APP_ID"], "3878000")
        self.assertNotIn("BEGIN", json.dumps(env))

    def test_absent_scan_app_table_renders_nothing(self) -> None:
        data = tomllib.loads(without_scan_table(CONFIG.read_text()))
        env = fleet.lane_plist(data, data["lane"][0])["EnvironmentVariables"]
        self.assertFalse([k for k in env if k.startswith("TARTCI_SCAN_")])

    def test_every_fleet_profile_loads_and_renders_the_scan_app(self) -> None:
        for name in ("m1", "m3", "m5", "m5studio"):
            with self.subTest(host=name):
                path = ROOT / "profiles" / f"{name}-macos-fleet.toml"
                data = fleet.load(path)
                home = data["host"]["home"]
                self.assertEqual(data["github_scan_app"], {
                    "id": "5665767",
                    "private_key_path": f"{home}/.config/shipyard/github-apps/"
                                        "generous-admission-reader.private-key.pem",
                    "cache_dir": f"{home}/.config/shipyard/ghapp-cache-reader",
                })
                self.assertEqual(self.validate_path(path).returncode, 0)
                for lane in data["lane"]:
                    env = fleet.lane_plist(data, lane)["EnvironmentVariables"]
                    if not lane["repo"].startswith("Generous-Corp/"):
                        self.assertFalse([k for k in env if k.startswith("TARTCI_SCAN_")], lane["repo"])
                        continue
                    self.assertEqual(env["TARTCI_SCAN_APP_ID"], "5665767")
                    self.assertTrue(env["TARTCI_SCAN_APP_CACHE_DIR"].endswith("ghapp-cache-reader"))
                    self.assertNotEqual(env.get("SHIPYARD_GITHUB_APP_ID"), "5665767")

    def test_lane_outside_reader_app_installation_keeps_gh_cli(self) -> None:
        data = tomllib.loads(self.profile())
        lane = dict(data["lane"][0], repo="danielraffel/spectr")
        env = fleet.lane_plist(data, lane)["EnvironmentVariables"]
        self.assertFalse([k for k in env if k.startswith("TARTCI_SCAN_")])
        lane = dict(data["lane"][0], repo="Generous-Corp/pulp")
        env = fleet.lane_plist(data, lane)["EnvironmentVariables"]
        self.assertEqual(env["TARTCI_SCAN_APP_ID"], "5665767")

    def validate_path(self, path: Path):
        return subprocess.run([str(ROOT / "tartci"), "fleet-macos", "validate", str(path)],
                              text=True, capture_output=True, check=False)

    def test_scan_app_table_is_validated_like_github_app(self) -> None:
        cases = {
            "partial": SCAN_TABLE.replace(f'cache_dir = "{HOME}/ghapp-cache-reader"\n', ""),
            "bad-id": SCAN_TABLE.replace('"5665767"', '"nope"'),
            "key-outside-root": SCAN_TABLE.replace(
                f"{HOME}/github-apps/generous-admission-reader.private-key.pem",
                "/tmp/reader.pem"),
            "unknown-key": SCAN_TABLE + 'token = "x"\n',
        }
        for name, table in cases.items():
            with self.subTest(name=name):
                result = self.validate(self.profile(table))
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("github_scan_app", result.stderr)

    def test_scans_use_scan_cli_and_writes_stay_on_gh_cli(self) -> None:
        runner = (ROOT / "providers/tart-macos/runner.sh").read_text()
        self.assertIn('SCAN_GH_CLI="$TARTCI_ROOT/scripts/gh-scan-app"', runner)
        self.assertIn('--gh-cli "$SCAN_GH_CLI"', runner)
        self.assertIn('"$JIT_GH_CLI" api -X POST', runner)
        v2 = (ROOT / "providers/tart-macos/assignment-v2.lib.sh").read_text()
        self.assertIn('--gh-cli "${SCAN_GH_CLI:-$GH_CLI}" \\\n    --lane-id', v2)


if __name__ == "__main__":
    unittest.main()
