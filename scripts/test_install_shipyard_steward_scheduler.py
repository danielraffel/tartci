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
        # Exercise the installer's protected-path contract from a private,
        # user-owned ancestor on both macOS and Linux CI.  A fixture rooted in
        # world-writable /tmp is intentionally rejected by the product.
        self.temporary = tempfile.TemporaryDirectory(dir=Path.home())
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
[ -z "${NO_CARRIER-}" ] || exit 2
if [ "$*" = "--json runner carrier --replay /dev/null" ]; then
  printf '{"schema_version":1,"command":"runner.carrier","apply":false,"replay":"/dev/null","plans":[]}\\n'
  exit 0
fi
if [ "$1 $2 $3 $4" = "--json runner carrier --repo" ]; then
  printf '{"schema_version":1,"command":"runner.carrier","apply":false,"classes":[],"repos":[{"repo":"%s","base":"main","prs":[],"errors":[]}]}\\n' "$5"
  exit 0
fi
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
    printf '%s\\n%s\\n' "$HOME/.local/bin/tartci" "$HOME/.config/shipyard/steward-scheduler.json"
    [ -f "$HOME/.launchctl-ran" ] || printf '\\truns = 0\\n'
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
    # DEFER_RUNATLOAD models a busy host: the RunAtLoad launch never happens.
    if [ "${DEFER_RUNATLOAD-0}" != 1 ] && [ -x "$HOME/.local/bin/tartci" ]; then
      : > "$HOME/.launchctl-ran"
      "$HOME/.local/bin/tartci" steward-scheduler --config "$HOME/.config/shipyard/steward-scheduler.json"
    fi
    ;;
  kickstart)
    # A job that already ran must not be started again.
    [ ! -f "$HOME/.launchctl-ran" ] || exit 99
    : > "$HOME/.launchctl-ran"
    "$HOME/.local/bin/tartci" steward-scheduler --config "$HOME/.config/shipyard/steward-scheduler.json"
    ;;
  *) exit 98 ;;
esac
""",
            encoding="utf-8",
        )
        launchctl.chmod(0o755)
        # The installed generation's entry point: this checkout's dispatcher.
        entry = self.home / ".local/bin/tartci"
        entry.parent.mkdir(parents=True)
        entry.write_text(f'#!/bin/sh\nexec /bin/bash "{INSTALLER.parent.parent / "tartci"}" "$@"\n',
                         encoding="utf-8")
        entry.chmod(0o755)
        plutil = self.bin / "plutil"
        plutil.write_text(
            """#!/bin/sh
[ "$1" = "-lint" ] && [ -f "$2" ]
""",
            encoding="utf-8",
        )
        plutil.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_installer(
        self, *extra: str, fail_bootstrap: bool = False, fail_bootout: bool = False,
        defer_runatload: bool = False, no_carrier: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("NO_CARRIER", None)
        if no_carrier:
            environment["NO_CARRIER"] = "1"
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                "SHIPYARD_STEWARD_INSTALL_HEALTH_WAIT_SECS": "3",
                "FAIL_BOOTSTRAP": "1" if fail_bootstrap else "0",
                "FAIL_BOOTOUT": "1" if fail_bootout else "0",
                "DEFER_RUNATLOAD": "1" if defer_runatload else "0",
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

    def test_an_install_on_a_host_that_defers_runatload_still_runs_the_scheduler(self) -> None:
        # launchd never performs the RunAtLoad launch; only the installer's
        # kickstart of a job that has never run gets the first tick going.
        result = self.run_installer("--install", defer_runatload=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / ".launchctl-ran").exists())

    def test_disabled_install_publishes_exact_config_and_health(self) -> None:
        result = self.run_installer("--install")
        self.assertEqual(result.returncode, 0, result.stderr)
        config_path = self.home / ".config/shipyard/steward-scheduler.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["schema_version"], 2)
        self.assertEqual(config["mode"], "disabled")
        self.assertFalse(config["authority"])
        self.assertEqual(config["classes"], [])
        self.assertEqual(config["repositories"], [{"repo": "owner/repo", "checkout": str(self.repo.resolve())}])
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        # Nothing is copied: the agent runs the installed generation.
        self.assertFalse((self.home / ".local/share/tartci/scripts").exists())
        health = json.loads(
            (self.home / "Library/Logs/shipyard-steward-scheduler.health.json").read_text(encoding="utf-8")
        )
        self.assertEqual(health["status"], "disabled")

    def read_config(self) -> dict[str, object]:
        path = self.home / ".config/shipyard/steward-scheduler.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_plan_install_publishes_a_plan_config_and_startup_receipt(self) -> None:
        result = self.run_installer("--mode", "plan", "--install")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.read_config()
        self.assertEqual((config["mode"], config["authority"], config["classes"]), ("plan", False, []))
        startup = json.loads(
            (self.home / "Library/Logs/shipyard-steward-scheduler.startup.json").read_text()
        )
        self.assertEqual((startup["status"], startup["mode"]), ("started", "plan"))

    def test_live_needs_authority_and_a_live_class(self) -> None:
        for extra, needle in (
            (("--mode", "live", "--authority"), "at least one --class"),
            (("--mode", "live", "--authority", "--class", "update_branch"), "invalid --class"),
            (("--mode", "plan", "--authority"), "only valid with --mode live"),
            (("--mode", "plan", "--class", "rearm"), "only valid with --mode live"),
        ):
            with self.subTest(extra=extra):
                result = self.run_installer(*extra)
                self.assertEqual(result.returncode, 2)
                self.assertIn(needle, result.stderr)

    def test_reinstalling_without_live_rolls_a_live_controller_back_to_plan(self) -> None:
        live = self.run_installer("--mode", "live", "--authority", "--class", "redispatch", "--install")
        self.assertEqual(live.returncode, 0, live.stderr)
        config = self.read_config()
        self.assertEqual((config["mode"], config["authority"], config["classes"]), ("live", True, ["redispatch"]))
        (self.home / ".launchctl-ran").unlink()
        back = self.run_installer("--mode", "plan", "--install")
        self.assertEqual(back.returncode, 0, back.stderr)
        config = self.read_config()
        self.assertEqual((config["mode"], config["authority"], config["classes"]), ("plan", False, []))

    def test_a_shipyard_without_the_carrier_is_refused(self) -> None:
        result = self.run_installer("--mode", "plan", no_carrier=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("runner carrier", result.stderr)
        self.assertFalse((self.home / ".config/shipyard/steward-scheduler.json").exists())

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
        self.assertEqual(installed.read_text(), "old-script")  # never touched
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
        self.assertEqual(installed.read_text(), "old-script")  # never touched
        self.assertEqual(config.read_text(), "old-config")
        self.assertEqual(plist.read_text(), "old-plist")


if __name__ == "__main__":
    unittest.main()
