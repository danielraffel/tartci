#!/usr/bin/env python3
from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

SCRIPT = Path(__file__).with_name("macos_runner_identity_guard.py")
MIGRATE = Path(__file__).with_name("migrate_macos_gate_agent.sh")
sys.path.insert(0, str(SCRIPT.parent))
import macos_runner_identity_guard as guard  # noqa: E402


class MacosRunnerIdentityGuardTests(unittest.TestCase):
    def test_launchctl_parser_uses_services_and_bare_arguments(self) -> None:
        domain = """
endpoint destination = com.apple.xpc.launchd.domain.user.501
services = {
    "local.custom.tart-macos" => {
        active count = 1
    }
    39006 - local.column.tart-macos
    - -9 local.negative.tart-macos
    - (pe) local.parenthesized.tart-macos
    - - tartci.macos
}
"""
        self.assertEqual(
            guard._service_labels(domain),
            {
                "local.custom.tart-macos",
                "local.column.tart-macos",
                "local.negative.tart-macos",
                "local.parenthesized.tart-macos",
                "tartci.macos",
            },
        )
        self.assertNotIn("com.apple.xpc.launchd.domain.user.501", guard._service_labels(domain))
        spec = guard._cached_spec(
            "arguments = {\n\t/bin/bash\n\ttartci\n\tserve\n\tmacos\n}\n"
        )
        self.assertEqual(spec["ProgramArguments"], ["/bin/bash", "tartci", "serve", "macos"])

    def test_duplicate_loaded_labels_same_identity_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "agents"
            agents.mkdir()
            legacy = {
                "Label": "local.custom.tart-macos",
                "ProgramArguments": ["/bin/bash", "/opt/pulp/tools/ci/tart-runner.sh", "--loop"],
                "EnvironmentVariables": {
                    "HOME": str(root),
                    "TARTCI_RUNNER_NAME": "pulp-studio-01",
                    "TARTCI_STATE_DIR": str(root / "state"),
                },
            }
            with (agents / "local.custom.tart-macos.plist").open("wb") as destination:
                plistlib.dump(legacy, destination)
            launchctl = root / "launchctl"
            launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launchctl.chmod(0o755)
            env = os.environ.copy()
            env["TARTCI_LAUNCHCTL"] = str(launchctl)
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--current-label", "com.danielraffel.pulp.tart-runner-macos-gate",
                    "--runner-name", "pulp-studio-01",
                    "--state-dir", str(root / "state"),
                    "--agents-dir", str(agents),
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("duplicate loaded macOS runner", result.stderr)
            self.assertIn("local.custom.tart-macos", result.stderr)

    def test_unloaded_duplicate_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "agents"
            agents.mkdir()
            plist = {
                "Label": "com.danielraffel.pulp.tart-runner",
                "ProgramArguments": ["tartci", "serve", "macos"],
                "EnvironmentVariables": {"HOME": str(root), "TARTCI_RUNNER_NAME": "same"},
            }
            with (agents / "legacy.plist").open("wb") as destination:
                plistlib.dump(plist, destination)
            launchctl = root / "launchctl"
            launchctl.write_text(
                "#!/bin/sh\ncase \"$2\" in gui/*/*) exit 1 ;; *) exit 0 ;; esac\n",
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            env = os.environ.copy()
            env["TARTCI_LAUNCHCTL"] = str(launchctl)
            result = subprocess.run(
                [
                    str(SCRIPT), "--current-label", "com.danielraffel.pulp.tart-runner-macos-gate",
                    "--runner-name", "same", "--state-dir", str(root / ".tartci/state/macos"),
                    "--agents-dir", str(agents),
                ],
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0)


class MacosGateMigrationTests(unittest.TestCase):
    def _fixture(self, root: Path, *, bootstrap_rc: int = 0) -> tuple[dict[str, str], Path]:
        agents = root / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        legacy = agents / "com.danielraffel.pulp.tart-runner.plist"
        with legacy.open("wb") as destination:
            plistlib.dump(
                {
                    "Label": "com.danielraffel.pulp.tart-runner",
                    "ProgramArguments": ["/bin/bash", "/opt/pulp/tools/ci/tart-runner.sh", "--labels", "self-hosted,macOS,pulp-build"],
                    "EnvironmentVariables": {
                        "HOME": str(root),
                        "TARTCI_RUNNER_NAME": "custom-required-01",
                        "TARTCI_MACOS_GOLDEN": "custom:latest",
                    },
                },
                destination,
            )
        fake_bin = root / "bin"
        fake_bin.mkdir()
        (fake_bin / "plutil").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "launchctl").write_text(
            f"""#!/bin/sh
case "$1" in
  print)
    case "$2" in
      */com.danielraffel.pulp.tart-runner)
        [ ! -f "$HOME/old-booted-out" ]
        ;;
      */com.danielraffel.pulp.tart-runner-macos-gate)
        [ -f "$HOME/Library/LaunchAgents/com.danielraffel.pulp.tart-runner-macos-gate.plist" ] || exit 1
        printf 'state = running\\n'
        exit 0
        ;;
      *) printf 'state = running\\n'; exit 0 ;;
    esac
    ;;
  bootout)
    case "$2" in */com.danielraffel.pulp.tart-runner) : > "$HOME/old-booted-out" ;; esac
    exit 0
    ;;
  bootstrap) exit {bootstrap_rc} ;;
  kickstart)
    mkdir -p "$HOME/.tartci/state/macos"
    /bin/sleep 1
    printf '{{"runner":"custom-required-01"}}\\n' > "$HOME/.tartci/state/macos/custom-required-01.state.json"
    exit 0
    ;;
  *) exit 0 ;;
esac
""",
            encoding="utf-8",
        )
        for command in ("plutil", "launchctl"):
            (fake_bin / command).chmod(0o755)
        env = os.environ.copy()
        env.update({"HOME": str(root), "PATH": f"{fake_bin}:/usr/bin:/bin"})
        return env, legacy

    def test_migration_preserves_custom_runner_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env, legacy = self._fixture(root)
            result = subprocess.run([str(MIGRATE), "--apply", "--attest-external-gui-label-updated"], env=env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(legacy.exists())
            replacement = legacy.with_name("com.danielraffel.pulp.tart-runner-macos-gate.plist")
            with replacement.open("rb") as source:
                plist = plistlib.load(source)
            self.assertEqual(plist["EnvironmentVariables"]["TARTCI_RUNNER_NAME"], "custom-required-01")
            self.assertEqual(plist["EnvironmentVariables"]["TARTCI_MACOS_GOLDEN"], "custom:latest")
            self.assertEqual(
                plist["ProgramArguments"][:4],
                ["/bin/bash", f"{root}/.local/bin/tartci", "serve", "macos"],
            )
            self.assertEqual(
                plist["EnvironmentVariables"]["TARTCI_LAUNCHD_LABEL"],
                "com.danielraffel.pulp.tart-runner-macos-gate",
            )
            rerun = subprocess.run([str(MIGRATE), "--apply", "--attest-external-gui-label-updated"], env=env, text=True, capture_output=True, check=False)
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            with replacement.open("rb") as source:
                rerun_plist = plistlib.load(source)
            self.assertEqual(rerun_plist["EnvironmentVariables"]["TARTCI_RUNNER_NAME"], "custom-required-01")

    def test_failed_replacement_restores_legacy_plist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env, legacy = self._fixture(root, bootstrap_rc=1)
            before = legacy.read_bytes()
            result = subprocess.run([str(MIGRATE), "--apply", "--attest-external-gui-label-updated"], env=env, text=True, capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("restoring prior", result.stderr)
            self.assertEqual(legacy.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
