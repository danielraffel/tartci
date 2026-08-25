#!/usr/bin/env python3
"""Hermetic install and rollback tests for the stewardship scheduler."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


INSTALLER = Path(__file__).with_name("install_shipyard_steward_scheduler.sh")


class StewardSchedulerInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin", "https://github.com/owner/repo.git"],
            check=True,
        )
        self.shipyard = (self.bin / "shipyard").resolve()
        self.shipyard.write_text(
            """#!/bin/sh
if [ "$1" = "--version" ]; then printf 'shipyard 0.113.0\\n'; exit 0; fi
exit 97
""",
            encoding="utf-8",
        )
        self.shipyard.chmod(0o755)
        launchctl = self.bin / "launchctl-test-double"
        launchctl.write_text(
            """#!/bin/sh
state="$HOME/.launchctl-loaded"
case "$1" in
  print)
    [ -f "$state" ] || exit 3
    printf '%s\\n%s\\n' "$HOME/.local/share/tartci/scripts/shipyard_steward_scheduler.py" "$HOME/.config/shipyard/steward-scheduler.json"
    ;;
  bootout)
    [ "${FAIL_BOOTOUT-0}" != 1 ] || exit 19
    rm -f "$state"
    ;;
  bootstrap)
    if [ "${FAIL_BOOTSTRAP-0}" = 1 ] && ! grep -q '^old-plist$' "${3:-/dev/null}" 2>/dev/null; then
      exit 17
    fi
    : > "$state"
    if [ -x "$HOME/.local/share/tartci/scripts/shipyard_steward_scheduler.py" ]; then
      "$HOME/.local/share/tartci/scripts/shipyard_steward_scheduler.py"
    fi
    ;;
  kickstart) exit 99 ;;
  *) exit 98 ;;
esac
""",
            encoding="utf-8",
        )
        launchctl.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_installer(
        self, *extra: str, fail_bootstrap: bool = False, fail_bootout: bool = False
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                "SHIPYARD_STEWARD_INSTALL_HEALTH_WAIT_SECS": "3",
                "FAIL_BOOTSTRAP": "1" if fail_bootstrap else "0",
                "FAIL_BOOTOUT": "1" if fail_bootout else "0",
                "TARTCI_LAUNCHCTL_BIN": str(self.bin / "launchctl-test-double"),
                "TARTCI_LAUNCHCTL_INTERPRETER": "/bin/sh",
            }
        )
        return subprocess.run(
            [
                str(INSTALLER),
                "--repo", f"owner/repo={self.repo}",
                "--shipyard", str(self.shipyard),
                *extra,
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

    def test_plan_is_disabled_and_preserves_legacy_tick(self) -> None:
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mode=disabled", result.stdout)
        self.assertIn("legacy_queue_tick=preserved", result.stdout)
        self.assertFalse((self.home / ".config/shipyard/steward-scheduler.json").exists())

    def test_live_requires_explicit_authority(self) -> None:
        result = self.run_installer("--mode", "live")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --authority", result.stderr)

    def test_disabled_install_publishes_exact_config_and_health(self) -> None:
        result = self.run_installer("--install")
        self.assertEqual(result.returncode, 0, result.stderr)
        config_path = self.home / ".config/shipyard/steward-scheduler.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertFalse(config["enabled"])
        self.assertFalse(config["authority"])
        self.assertEqual(config["repositories"], [{"repo": "owner/repo", "checkout": str(self.repo.resolve())}])
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        install_dir = self.home / ".local/share/tartci/scripts"
        self.assertEqual(install_dir.stat().st_mode & 0o022, 0)
        health = json.loads(
            (self.home / "Library/Logs/shipyard-steward-scheduler.health.json").read_text(encoding="utf-8")
        )
        self.assertEqual(health["status"], "disabled")

    def test_failed_bootstrap_restores_prior_files(self) -> None:
        installed = self.home / ".local/share/tartci/scripts/shipyard_steward_scheduler.py"
        config = self.home / ".config/shipyard/steward-scheduler.json"
        plist = self.home / "Library/LaunchAgents/com.danielraffel.shipyard.steward-scheduler.plist"
        for path, value in ((installed, "old-script"), (config, "old-config"), (plist, "old-plist")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
        (self.home / ".launchctl-loaded").touch()
        result = self.run_installer("--install", fail_bootstrap=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(installed.read_text(), "old-script")
        self.assertEqual(config.read_text(), "old-config")
        self.assertEqual(plist.read_text(), "old-plist")
        self.assertTrue((self.home / ".launchctl-loaded").exists())

    def test_loaded_job_bootout_failure_aborts_before_replacement(self) -> None:
        installed = self.home / ".local/share/tartci/scripts/shipyard_steward_scheduler.py"
        config = self.home / ".config/shipyard/steward-scheduler.json"
        plist = self.home / "Library/LaunchAgents/com.danielraffel.shipyard.steward-scheduler.plist"
        for path, value in ((installed, "old-script"), (config, "old-config"), (plist, "old-plist")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
        (self.home / ".launchctl-loaded").touch()
        result = self.run_installer("--install", fail_bootout=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not be booted out", result.stderr)
        self.assertEqual(installed.read_text(), "old-script")
        self.assertEqual(config.read_text(), "old-config")
        self.assertEqual(plist.read_text(), "old-plist")


if __name__ == "__main__":
    unittest.main()
