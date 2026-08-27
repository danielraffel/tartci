#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallMacosFleetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.agents = self.home / "Library/LaunchAgents"
        self.bin = self.home / ".local/bin"
        self.fakebin = self.root / "fakebin"
        self.agents.mkdir(parents=True)
        self.bin.mkdir(parents=True)
        self.fakebin.mkdir()
        for tool in ("ghapp", "tart", "tartci"):
            path = self.bin / tool
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        self.calls = self.root / "launchctl.calls"
        launchctl = self.fakebin / "launchctl"
        launchctl.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            echo "$*" >> {self.calls}
            if [ "$1" = print ]; then
              case "$2" in *"${{FAKE_LOADED_LABEL:-__none__}}"*) exit 0;; esac
              if [ "${{FAKE_LAUNCHCTL_ERROR:-0}}" = 1 ]; then
                echo "launchctl IPC unavailable" >&2
                exit 64
              fi
              echo "Bad request. Could not find service \"$2\" in domain for user gui: 501" >&2
              exit 1
            fi
            exit 0
        """))
        launchctl.chmod(0o755)
        shipyard = self.fakebin / "shipyard"
        shipyard.write_text("#!/bin/sh\necho \"${FAKE_SHIPYARD_TAG:-m1}\"\n")
        shipyard.chmod(0o755)
        self.config = self.root / "fleet.toml"
        self.config.write_text(textwrap.dedent(f"""\
            schema = 1
            name = "test-fleet"
            [host]
            id = "m1"
            home = "{self.home}"
            tart_home = "{self.home}/VMs"
            cache_root = "{self.home}/cache"
            log_root = "{self.home}/logs"
            [[lane]]
            id = "forge-gate"
            repo = "Generous-Corp/forge"
            golden = "pulp-build-runner:latest"
            labels = ["self-hosted", "macOS", "ARM64", "forge-gate-fast"]
            workflows = ["protected macOS build"]
            replaces_launchd_labels = ["com.danielraffel.forge.tart-runner-macos"]
        """))
        state = self.home / ".config/tartci"
        state.mkdir(parents=True)
        (state / "native-build-participation").write_text("0\n")
        (state / "pool-state").write_text("off\n")
        self.legacy = self.agents / "com.danielraffel.forge.tart-runner-macos.plist"
        self.legacy.write_text("legacy\n")
        self.env = os.environ.copy()
        self.env.update(HOME=str(self.home), PATH=f"{self.fakebin}:{os.environ['PATH']}")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_installer(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        effective = self.env.copy()
        effective.update(env)
        return subprocess.run(
            [str(ROOT / "tartci"), "fleet-macos", "install", str(self.config), *args],
            text=True, capture_output=True, check=False, env=effective,
        )

    def test_dry_run_does_not_install_or_retire(self) -> None:
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("action=dry-run", result.stdout)
        self.assertTrue(self.legacy.exists())
        self.assertEqual([], list(self.agents.glob("*macos-fleet*.plist")))
        self.assertFalse((self.home / ".config/tartci/macos-fleet-install.json").exists())

    def test_apply_installs_exact_rendered_profile_and_retires_declared_legacy(self) -> None:
        stale = self.agents / "com.danielraffel.tartci.tart-runner-macos-fleet.m1.removed.plist"
        stale.write_text("stale\n")
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = list(self.agents.glob("*macos-fleet*.plist"))
        self.assertEqual(1, len(installed))
        self.assertFalse(self.legacy.exists())
        self.assertFalse(stale.exists())
        retired = list((self.agents / ".tartci-retired").rglob("*.retired"))
        self.assertEqual(1, len(retired))
        self.assertEqual("legacy\n", retired[0].read_text())
        stale_backups = list((self.agents / ".tartci-retired").rglob("*.stale"))
        self.assertEqual(1, len(stale_backups))
        self.assertEqual("stale\n", stale_backups[0].read_text())
        receipt = self.home / ".config/tartci/macos-fleet-install.json"
        self.assertEqual("test-fleet", json.loads(receipt.read_text())["profile"])
        self.assertNotIn("bootstrap", self.calls.read_text())

    def test_apply_refuses_open_pool_and_loaded_legacy_without_mutation(self) -> None:
        (self.home / ".config/tartci/native-build-participation").write_text("1\n")
        (self.home / ".config/tartci/pool-state").write_text("on\n")
        open_result = self.run_installer("--apply")
        self.assertEqual(open_result.returncode, 3)
        self.assertTrue(self.legacy.exists())
        (self.home / ".config/tartci/native-build-participation").write_text("0\n")
        (self.home / ".config/tartci/pool-state").write_text("off\n")
        loaded = self.run_installer("--apply", FAKE_LOADED_LABEL="com.danielraffel.forge.tart-runner-macos")
        self.assertEqual(loaded.returncode, 3)
        self.assertTrue(self.legacy.exists())
        self.assertEqual([], list(self.agents.glob("*macos-fleet*.plist")))

    def test_apply_rejects_unusable_rendered_runtime_before_mutation(self) -> None:
        (self.bin / "tartci").unlink()
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not readable and executable", result.stderr)
        self.assertTrue(self.legacy.exists())
        self.assertEqual([], list(self.agents.glob("*macos-fleet*.plist")))

    def test_apply_rejects_profile_for_another_shipyard_host(self) -> None:
        result = self.run_installer("--apply", FAKE_SHIPYARD_TAG="m5")
        self.assertEqual(result.returncode, 3)
        self.assertIn("host mismatch", result.stderr)
        self.assertTrue(self.legacy.exists())
        self.assertEqual([], list(self.agents.glob("*macos-fleet*.plist")))

    def test_apply_refuses_launchctl_inspection_error_without_mutation(self) -> None:
        result = self.run_installer("--apply", FAKE_LAUNCHCTL_ERROR="1")
        self.assertEqual(result.returncode, 3)
        self.assertIn("could not prove", result.stderr)
        self.assertTrue(self.legacy.exists())
        self.assertEqual([], list(self.agents.glob("*macos-fleet*.plist")))

    def test_failed_receipt_publication_restores_dangling_target_symlink(self) -> None:
        target = self.agents / "com.danielraffel.tartci.tart-runner-macos-fleet.m1.forge-gate.plist"
        target.symlink_to(self.root / "missing-target")
        profile = self.home / ".config/tartci/macos-fleet-profile.toml"
        profile.symlink_to(self.root / "missing-profile")
        python = self.fakebin / "python3"
        python.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            if [ "$2" = write-receipt ]; then exit 1; fi
            exec {sys.executable} "$@"
        """))
        python.chmod(0o755)
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(target.is_symlink())
        self.assertEqual(self.root / "missing-target", target.readlink())
        self.assertTrue(profile.is_symlink())
        self.assertEqual(self.root / "missing-profile", profile.readlink())
        self.assertTrue(self.legacy.exists())

    def test_pool_on_refuses_tampered_install_before_opening_admission(self) -> None:
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        target = next(self.agents.glob("*macos-fleet*.plist"))
        target.write_bytes(target.read_bytes() + b"\n")
        self.calls.write_text("")
        result = subprocess.run(
            [str(ROOT / "tartci"), "pool", "on"], text=True,
            capture_output=True, check=False, env=self.env,
        )
        self.assertEqual(result.returncode, 7)
        self.assertIn("no valid receipt", result.stderr)
        self.assertEqual("off", (self.home / ".config/tartci/pool-state").read_text().strip())
        self.assertNotIn("bootstrap", self.calls.read_text())

    def test_pool_on_rejects_receipt_redirected_to_a_decoy_directory(self) -> None:
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        receipt_path = self.home / ".config/tartci/macos-fleet-install.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["agents_dir"] = str(self.root / "decoy")
        receipt_path.write_text(json.dumps(receipt))
        self.calls.write_text("")
        result = subprocess.run(
            [str(ROOT / "tartci"), "pool", "on"], text=True,
            capture_output=True, check=False, env=self.env,
        )
        self.assertEqual(result.returncode, 7)
        self.assertIn("no valid receipt", result.stderr)
        self.assertEqual("off", (self.home / ".config/tartci/pool-state").read_text().strip())
        self.assertNotIn("bootstrap", self.calls.read_text())

    def test_pool_on_rejects_unreceipted_symlinked_fleet_plist(self) -> None:
        link = self.agents / "com.danielraffel.tartci.tart-runner-macos-fleet.m1.forge-gate.plist"
        link.symlink_to(self.legacy)
        self.calls.write_text("")
        result = subprocess.run(
            [str(ROOT / "tartci"), "pool", "on"], text=True,
            capture_output=True, check=False, env=self.env,
        )
        self.assertEqual(result.returncode, 7)
        self.assertIn("no valid receipt", result.stderr)
        self.assertEqual("off", (self.home / ".config/tartci/pool-state").read_text().strip())
        self.assertNotIn("bootstrap", self.calls.read_text())

    def test_pool_on_verifies_receipt_and_loads_only_the_rendered_replacement(self) -> None:
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        receipt = json.loads((self.home / ".config/tartci/macos-fleet-install.json").read_text())
        self.assertEqual(
            str((self.home / ".config/tartci/macos-fleet-profile.toml").resolve()),
            receipt["config_path"],
        )
        self.config.write_text("source profile changed after the locked install snapshot\n")
        self.calls.write_text("")
        result = subprocess.run(
            [str(ROOT / "tartci"), "pool", "on"], text=True,
            capture_output=True, check=False, env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text()
        self.assertIn("tart-runner-macos-fleet.m1.forge-gate", calls)
        self.assertNotIn("com.danielraffel.forge.tart-runner-macos.plist", calls)
        self.assertEqual("on", (self.home / ".config/tartci/pool-state").read_text().strip())


if __name__ == "__main__":
    unittest.main()
