#!/usr/bin/env python3
"""Hermetic installer tests for the Shipyard queue janitor."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("shipyard_queue_tick.sh")
SUPPORT = Path(__file__).with_name("shipyard_queue_tick_support.py")
SERVICE = Path(__file__).with_name("shipyard_queue_service_tick.sh")
SERVICE_SUPPORT = Path(__file__).with_name("shipyard_queue_service_tick.py")
STEWARD = Path(__file__).with_name("shipyard_steward_tick.sh")
INSTALLER = Path(__file__).with_name("install_shipyard_queue_tick.sh")


class QueueTickInstallerTests(unittest.TestCase):
    def test_installer_rejects_evilgithub_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            repo = home / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    "https://evilgithub.com/owner/repo.git",
                ],
                check=True,
            )
            wrapper = home / "ghapp"
            wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            wrapper.chmod(0o755)
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER),
                    "--repo-root",
                    str(repo),
                    "--gh-cli",
                    str(wrapper),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("no supported GitHub origin", result.stderr)

    def test_reap_only_requires_explicit_app_wrapper(self) -> None:
        result = subprocess.run(
            [
                "/bin/bash",
                str(INSTALLER),
                "--repo-root",
                ".",
                "--mode",
                "reap-only",
            ],
            cwd=SCRIPT.parents[1],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("all modes require --gh-cli", result.stderr)

    def test_installer_deploys_and_verifies_launchd_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            repo = home / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/owner/repo.git",
                ],
                check=True,
            )
            fake_bin = home / "bin"
            fake_bin.mkdir()
            (fake_bin / "plutil").write_text(
                "#!/bin/sh\nexit 0\n", encoding="utf-8"
            )
            (fake_bin / "ghapp").write_text(
                "#!/bin/sh\nexit 0\n", encoding="utf-8"
            )
            (fake_bin / "launchctl").write_text(
                """#!/bin/sh
if [ "$1" = "print" ]; then
  [ ! -f "$HOME/booted-out" ] || exit 1
  printf '%s\\n' "$HOME/.config/shipyard/queue-tick.env"
  printf '%s\\n' "$HOME/.local/share/tartci/scripts/shipyard_queue_service_tick.sh"
elif [ "$1" = "bootout" ]; then
  touch "$HOME/booted-out"
elif [ "$1" = "bootstrap" ]; then
  rm -f "$HOME/booted-out"
elif [ "$1" = "kickstart" ]; then
  mkdir -p "$HOME/Library/Logs"
  printf '{"status":"healthy"}\\n' > "$HOME/Library/Logs/shipyard-queue-tick.health.json"
  printf '{"status":"healthy"}\\n' > "$HOME/Library/Logs/shipyard-steward-tick.health.json"
fi
exit 0
""",
                encoding="utf-8",
            )
            for command in ("plutil", "ghapp", "launchctl"):
                (fake_bin / command).chmod(0o755)
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("SHIPYARD_", "TARTCI_"))
            }
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                }
            )
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER),
                    "--repo-root",
                    "repo",
                    "--authority",
                    "--mode",
                    "live",
                    "--gh-cli",
                    "ghapp",
                    "--install",
                ],
                env=env,
                cwd=home,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = (
                home / ".local/share/tartci/scripts/shipyard_queue_tick.sh"
            )
            installed_support = (
                home
                / ".local/share/tartci/scripts/shipyard_queue_tick_support.py"
            )
            installed_service = home / ".local/share/tartci/scripts/shipyard_queue_service_tick.sh"
            installed_service_support = home / ".local/share/tartci/scripts/shipyard_queue_service_tick.py"
            installed_steward = home / ".local/share/tartci/scripts/shipyard_steward_tick.sh"
            self.assertTrue(os.access(installed, os.X_OK))
            self.assertEqual(installed.read_bytes(), SCRIPT.read_bytes())
            self.assertEqual(
                installed_support.read_bytes(), SUPPORT.read_bytes()
            )
            self.assertEqual(installed_service.read_bytes(), SERVICE.read_bytes())
            self.assertEqual(installed_service_support.read_bytes(), SERVICE_SUPPORT.read_bytes())
            self.assertEqual(installed_steward.read_bytes(), STEWARD.read_bytes())
            config = home / ".config/shipyard/queue-tick.env"
            self.assertIn(
                f"SHIPYARD_QUEUE_REPO_ROOT={repo.resolve()}",
                config.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "SHIPYARD_QUEUE_AUTHORITY=1",
                config.read_text(encoding="utf-8"),
            )
            self.assertIn("queue and steward health verdicts are healthy", result.stdout)
            with (
                home
                / "Library/LaunchAgents/"
                "com.danielraffel.shipyard.queue-tick.plist"
            ).open("rb") as source:
                plist = plistlib.load(source)
            environment = plist["EnvironmentVariables"]
            self.assertEqual(environment["SHIPYARD_TICK_APPLY"], "1")
            self.assertEqual(environment["SHIPYARD_TICK_REAP_ONLY"], "0")

    def test_live_mode_requires_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER),
                    "--repo-root",
                    str(repo),
                    "--mode",
                    "live",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires --authority", result.stderr)

    def test_failed_candidate_rolls_back_prior_bytes_and_loaded_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            repo = home / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/owner/repo.git",
                ],
                check=True,
            )
            install_dir = home / ".local/share/tartci/scripts"
            config_dir = home / ".config/shipyard"
            agents = home / "Library/LaunchAgents"
            logs = home / "Library/Logs"
            for path in (install_dir, config_dir, agents, logs):
                path.mkdir(parents=True)
            installed = install_dir / "shipyard_queue_tick.sh"
            installed_support = install_dir / "shipyard_queue_tick_support.py"
            installed_service = install_dir / "shipyard_queue_service_tick.sh"
            installed_service_support = install_dir / "shipyard_queue_service_tick.py"
            installed_steward = install_dir / "shipyard_steward_tick.sh"
            config = config_dir / "queue-tick.env"
            plist = agents / "com.danielraffel.shipyard.queue-tick.plist"
            installed.write_bytes(b"prior-script")
            installed_support.write_bytes(b"prior-support")
            installed_service.write_bytes(b"prior-service")
            installed_service_support.write_bytes(b"prior-service-support")
            installed_steward.write_bytes(b"prior-steward")
            config.write_bytes(b"prior-config")
            plist.write_bytes(b"prior-plist")
            calls = home / "calls"
            fake_bin = home / "bin"
            fake_bin.mkdir()
            for command in ("ghapp", "plutil", "sleep"):
                (fake_bin / command).write_text(
                    "#!/bin/sh\nexit 0\n", encoding="utf-8"
                )
            (fake_bin / "launchctl").write_text(
                """#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
case "$1" in
  print)
    [ ! -f "$HOME/booted-out" ] || exit 1
    printf '%s\\n' "$HOME/.config/shipyard/queue-tick.env"
    printf '%s\\n' "$HOME/.local/share/tartci/scripts/shipyard_queue_service_tick.sh"
    exit 0
    ;;
  bootout)
    touch "$HOME/booted-out"
    exit 0
    ;;
  bootstrap)
    rm -f "$HOME/booted-out"
    exit 0
    ;;
  kickstart)
    printf '{"status":"starting"}\\n' > "$HOME/Library/Logs/shipyard-queue-tick.health.json"
    exit 0
    ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            for command in ("ghapp", "plutil", "sleep", "launchctl"):
                (fake_bin / command).chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "CALLS": str(calls),
                    "SHIPYARD_QUEUE_INSTALL_HEALTH_WAIT_SECS": "1",
                }
            )
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER),
                    "--repo-root",
                    str(repo),
                    "--authority",
                    "--mode",
                    "live",
                    "--gh-cli",
                    "ghapp",
                    "--install",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("rolling back", result.stderr)
            self.assertEqual(installed.read_bytes(), b"prior-script")
            self.assertEqual(
                installed_support.read_bytes(), b"prior-support"
            )
            self.assertEqual(installed_service.read_bytes(), b"prior-service")
            self.assertEqual(installed_service_support.read_bytes(), b"prior-service-support")
            self.assertEqual(installed_steward.read_bytes(), b"prior-steward")
            self.assertEqual(config.read_bytes(), b"prior-config")
            self.assertEqual(plist.read_bytes(), b"prior-plist")
            call_text = calls.read_text(encoding="utf-8")
            self.assertGreaterEqual(
                call_text.count("bootstrap"), 2, result.stderr
            )
            self.assertGreaterEqual(call_text.count("bootout"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
