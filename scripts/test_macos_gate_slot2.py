#!/usr/bin/env python3
"""Focused mutation coverage for the managed second macOS gate slot."""

from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "tartci"
PRIMARY_TEMPLATE = ROOT / "launchd/com.danielraffel.pulp.tart-runner-macos.plist.template"
RENDER_TEMPLATE = ROOT / "scripts/render_launchd_template.py"
SLOT2_LABEL = "com.danielraffel.pulp.tart-runner-macos-gate-slot2"


class MacosGateSlot2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.tart_home = self.home / "VMs"
        self.agents = self.home / "Library/LaunchAgents"
        self.agents.mkdir(parents=True)
        self.primary = self.agents / "com.danielraffel.pulp.tart-runner-macos-gate.plist"
        rendered = subprocess.run(
            [
                "python3", str(RENDER_TEMPLATE), str(PRIMARY_TEMPLATE),
                "--set", f"HOME={self.home}", "--set", f"TART_HOME={self.tart_home}",
            ],
            capture_output=True, check=False,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr.decode())
        self.primary.write_bytes(rendered.stdout)
        self.slot2 = self.agents / f"{SLOT2_LABEL}.plist"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def render(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(COMMAND), "gate-slot2", "render",
                "--home", str(self.home), "--tart-home", str(self.tart_home),
                "--output", str(self.slot2),
            ],
            text=True, capture_output=True, check=False,
        )

    def validate(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(COMMAND), "gate-slot2", "validate", str(self.slot2),
             "--sibling", str(self.primary)],
            text=True, capture_output=True, check=False,
        )

    def test_profile_is_managed_exclusive_and_collision_free(self) -> None:
        rendered = self.render()
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        validated = self.validate()
        self.assertEqual(validated.returncode, 0, validated.stderr)
        value = plistlib.loads(self.slot2.read_bytes())
        env = value["EnvironmentVariables"]
        self.assertEqual(value["Label"], SLOT2_LABEL)
        self.assertEqual(value["ProgramArguments"][-4:-2], ["--slot", "2"])
        self.assertNotIn("pulp-gate-fast", value["ProgramArguments"][-1])
        self.assertEqual(env["TARTCI_RUNNER_ASSIGNMENT_MODE"], "event-class-v2")
        self.assertEqual(
            env["TARTCI_RUNNER_WORKFLOW_TIERS"].splitlines(),
            [
                "pulp-build-merge-group|Build and Test",
                "pulp-build-pr-head|Build and Test",
            ],
        )
        self.assertEqual(env["TARTCI_MACOS_VM_CORES"], "6")
        self.assertEqual(env["TARTCI_MACOS_VM_MEM_MB"], "8192")
        self.assertEqual(env["TART_HOME"], str(self.tart_home))

    def test_mutated_identity_collision_is_rejected(self) -> None:
        self.assertEqual(self.render().returncode, 0)
        slot2 = plistlib.loads(self.slot2.read_bytes())
        primary = plistlib.loads(self.primary.read_bytes())
        primary_env = primary["EnvironmentVariables"]
        slot2_env = slot2["EnvironmentVariables"]
        slot2["ProgramArguments"] = [item for item in slot2["ProgramArguments"] if item not in ("--slot", "2")]
        slot2_env["TARTCI_RUNNER_SLOT"] = "1"
        slot2_env["TARTCI_RUNNER_NAME_PREFIX"] = "pulp-vm"
        slot2_env["TARTCI_STATE_DIR"] = primary_env.get(
            "TARTCI_STATE_DIR", str(self.home / ".tartci/state/macos")
        )
        with self.slot2.open("wb") as destination:
            plistlib.dump(slot2, destination)
        result = self.validate()
        self.assertEqual(result.returncode, 2)
        self.assertTrue(
            "same runner name" in result.stderr or "same state file" in result.stderr,
            result.stderr,
        )

    def test_mutated_legacy_generic_label_leak_is_rejected(self) -> None:
        self.assertEqual(self.render().returncode, 0)
        value = plistlib.loads(self.slot2.read_bytes())
        labels = value["EnvironmentVariables"]["TARTCI_RUNNER_LABELS"] + ",pulp-gate-fast"
        value["EnvironmentVariables"]["TARTCI_RUNNER_LABELS"] = labels
        value["ProgramArguments"][-1] = labels
        with self.slot2.open("wb") as destination:
            plistlib.dump(value, destination)
        result = self.validate()
        self.assertEqual(result.returncode, 2)
        self.assertIn("TARTCI_RUNNER_LABELS", result.stderr)
        self.assertIn("legacy generic label leaked", result.stderr)

    def test_mutated_shared_and_separate_surfaces_fail_closed(self) -> None:
        mutations = {
            "cache": ("TARTCI_CI_CACHE", str(self.root / "other-cache"), "share TARTCI_CI_CACHE"),
            "queue": ("TARTCI_QUEUE_LANE_ID", "pulp-vm-01-1", "TARTCI_QUEUE_LANE_ID"),
            "event": (
                "TARTCI_EVENT_LOG",
                str(self.home / ".tartci/state/macos/../macos/events.jsonl"),
                "TARTCI_EVENT_LOG",
            ),
            "jobs": (
                "TARTCI_MACOS_LOGS",
                str(self.home / "VMs/logs/tartci-macos"),
                "TARTCI_MACOS_LOGS",
            ),
        }
        for name, (key, mutated, expected) in mutations.items():
            with self.subTest(name=name):
                self.assertEqual(self.render().returncode, 0)
                value = plistlib.loads(self.slot2.read_bytes())
                value["EnvironmentVariables"][key] = mutated
                with self.slot2.open("wb") as destination:
                    plistlib.dump(value, destination)
                result = self.validate()
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected, result.stderr)

    def test_empty_primary_cache_uses_provider_default(self) -> None:
        self.assertEqual(self.render().returncode, 0)
        primary = plistlib.loads(self.primary.read_bytes())
        primary["EnvironmentVariables"]["TARTCI_CI_CACHE"] = ""
        with self.primary.open("wb") as destination:
            plistlib.dump(primary, destination)
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lexically_aliased_launchd_log_collision_is_rejected(self) -> None:
        self.assertEqual(self.render().returncode, 0)
        primary = plistlib.loads(self.primary.read_bytes())
        aliased = str(
            self.home
            / "Library/Logs/tartci/../tartci/tart-runner-macos-gate-slot2.log"
        )
        primary["StandardOutPath"] = aliased
        primary["StandardErrorPath"] = aliased
        with self.primary.open("wb") as destination:
            plistlib.dump(primary, destination)
        result = self.validate()
        self.assertEqual(result.returncode, 2)
        self.assertIn("launchd log", result.stderr)

    def test_pool_agent_enumeration_includes_slot2_label(self) -> None:
        self.assertEqual(self.render().returncode, 0)
        script = (
            f"source {ROOT / 'providers/common/pool.lib.sh'}; "
            f"tartci_pool_runner_agents {self.agents}"
        )
        result = subprocess.run(
            ["bash", "-c", script], text=True, capture_output=True, check=False,
            env={**os.environ, "HOME": str(self.home)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(SLOT2_LABEL, result.stdout.splitlines())

    def test_installer_is_dry_run_by_default_and_refuses_open_pool(self) -> None:
        env = {
            **os.environ,
            "HOME": str(self.home),
            "TART_HOME": str(self.tart_home),
            "PATH": f"{self.home}/.local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        }
        dry_run = subprocess.run(
            [str(COMMAND), "gate-slot2", "install"],
            text=True, capture_output=True, check=False, env=env,
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertIn("action=dry-run", dry_run.stdout)
        self.assertFalse(self.slot2.exists())

        config = self.home / ".config/tartci"
        config.mkdir(parents=True)
        (config / "native-build-participation").write_text("1\n")
        (config / "pool-state").write_text("on\n")
        apply = subprocess.run(
            [str(COMMAND), "gate-slot2", "install", "--apply"],
            text=True, capture_output=True, check=False, env=env,
        )
        self.assertEqual(apply.returncode, 3)
        self.assertIn("pool admission is open", apply.stderr)
        self.assertFalse(self.slot2.exists())

    def test_installer_applies_only_while_pool_is_closed_and_defers_loading(self) -> None:
        bindir = self.home / ".local/bin"
        bindir.mkdir(parents=True)
        calls = self.root / "launchctl.calls"
        for name, body in {
            "ghapp": "#!/bin/sh\nexit 0\n",
            "tart": "#!/bin/sh\nexit 0\n",
            "tartci": "#!/bin/sh\nexit 0\n",
            "launchctl": f"#!/bin/sh\necho \"$*\" >> {calls}\nexit 1\n",
        }.items():
            path = bindir / name
            path.write_text(body)
            path.chmod(0o755)
        config = self.home / ".config/tartci"
        config.mkdir(parents=True)
        (config / "native-build-participation").write_text("0\n")
        (config / "pool-state").write_text("off\n")
        result = subprocess.run(
            [str(COMMAND), "gate-slot2", "install", "--apply"],
            text=True, capture_output=True, check=False,
            env={
                **os.environ,
                "HOME": str(self.home),
                "TART_HOME": str(self.tart_home),
                "PATH": f"{bindir}:/opt/homebrew/bin:/usr/bin:/bin",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.slot2.exists())
        self.assertIn("pool admission remains closed", result.stdout)
        self.assertNotIn("bootstrap", calls.read_text() if calls.exists() else "")
        self.assertEqual(self.validate().returncode, 0)
        second = subprocess.run(
            [str(COMMAND), "gate-slot2", "install", "--apply"],
            text=True, capture_output=True, check=False,
            env={
                **os.environ,
                "HOME": str(self.home),
                "TART_HOME": str(self.tart_home),
                "PATH": f"{bindir}:/opt/homebrew/bin:/usr/bin:/bin",
            },
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(list(self.agents.glob(f"{SLOT2_LABEL}.plist.pre-slot2.*"))), 1)

    def test_installer_checks_the_rendered_launchd_path(self) -> None:
        extra = self.root / "caller-only-bin"
        extra.mkdir()
        for name in ("ghapp", "tart", "launchctl"):
            path = extra / name
            path.write_text("#!/bin/sh\nexit 1\n" if name == "launchctl" else "#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        config = self.home / ".config/tartci"
        config.mkdir(parents=True)
        (config / "native-build-participation").write_text("0\n")
        (config / "pool-state").write_text("off\n")
        result = subprocess.run(
            [str(COMMAND), "gate-slot2", "install", "--apply"],
            text=True, capture_output=True, check=False,
            env={
                **os.environ,
                "HOME": str(self.home),
                "TART_HOME": str(self.tart_home),
                "PATH": f"{extra}:/opt/homebrew/bin:/usr/bin:/bin",
            },
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("rendered launchd PATH", result.stderr)
        self.assertFalse(self.slot2.exists())

    def test_installer_rejects_missing_rendered_tartci_program(self) -> None:
        bindir = self.home / ".local/bin"
        bindir.mkdir(parents=True)
        for name in ("ghapp", "tart"):
            path = bindir / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        launchctl = bindir / "launchctl"
        launchctl.write_text("#!/bin/sh\nexit 1\n")
        launchctl.chmod(0o755)
        config = self.home / ".config/tartci"
        config.mkdir(parents=True)
        (config / "native-build-participation").write_text("0\n")
        (config / "pool-state").write_text("off\n")
        result = subprocess.run(
            [str(COMMAND), "gate-slot2", "install", "--apply"],
            text=True, capture_output=True, check=False,
            env={
                **os.environ,
                "HOME": str(self.home),
                "TART_HOME": str(self.tart_home),
                "PATH": f"{bindir}:/opt/homebrew/bin:/usr/bin:/bin",
            },
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("rendered tartci program", result.stderr)
        self.assertFalse(self.slot2.exists())


if __name__ == "__main__":
    unittest.main()
