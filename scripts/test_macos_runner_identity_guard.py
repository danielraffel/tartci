#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

SCRIPT = Path(__file__).with_name("macos_runner_identity_guard.py")
RUNNER = SCRIPT.parents[1] / "providers" / "tart-macos" / "runner.sh"
MIGRATE = Path(__file__).with_name("migrate_macos_gate_agent.sh")
sys.path.insert(0, str(SCRIPT.parent))
import macos_runner_identity_guard as guard  # noqa: E402
from macos_runner_identity import resolve_plist_identity  # noqa: E402


class MacosRunnerIdentityGuardTests(unittest.TestCase):
    def test_known_non_macos_provider_matching_is_exact(self) -> None:
        known = (
            ["/bin/bash", "/opt/tartci/providers/tart-linux/runner.sh", "--loop"],
            ["/bin/bash", "/opt/tartci/providers/qemu-windows/runner.sh", "--loop"],
            ["/bin/bash", "/Users/test/.local/bin/tartci", "serve", "linux", "--loop"],
            ["/Users/test/.local/bin/tartci", "serve", "windows"],
        )
        for arguments in known:
            with self.subTest(arguments=arguments):
                self.assertTrue(guard._known_non_macos_provider(arguments))

        unknown = (
            ["/opt/tartci/providers/tart-unknown/runner.sh", "--loop"],
            ["/opt/tartci/providers/tart-linux/runner.sh.wrapper", "--loop"],
            ["/Users/test/.local/bin/tartci", "serve", "freebsd"],
            ["/Users/test/.local/bin/tartci", "inspect", "linux"],
            ["/unknown/wrapper", "serve", "linux"],
            [
                "/opt/tartci/providers/tart-macos/runner.sh",
                "/opt/tartci/providers/tart-linux/runner.sh",
            ],
        )
        for arguments in unknown:
            with self.subTest(arguments=arguments):
                self.assertFalse(guard._known_non_macos_provider(arguments))

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

    def test_unrelated_program_only_service_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "agents"
            agents.mkdir()
            launchctl = root / "launchctl"
            launchctl.write_text(
                """#!/bin/sh
case "$2" in
  gui/*) printf 'services = {\\n  "com.example.menu-helper" => {\\n  }\\n}\\n' ;;
  */com.example.menu-helper) printf 'program = /usr/bin/true\\n' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            env = os.environ.copy()
            env["TARTCI_LAUNCHCTL"] = str(launchctl)
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--current-label",
                    "com.danielraffel.pulp.tart-runner-macos-gate",
                    "--runner-name",
                    "pulp-studio-01",
                    "--state-dir",
                    str(root / "state"),
                    "--agents-dir",
                    str(agents),
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_domain_services_are_filtered_before_detail_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "agents"
            agents.mkdir()
            calls = root / "calls"
            launchctl = root / "launchctl"
            launchctl.write_text(
                f"""#!/bin/sh
printf '%s\\n' "$2" >> '{calls}'
case "$2" in
  gui/*) printf 'services = {{\\n  "com.apple.textcontextd" => {{\\n  }}\\n  "com.example.menu-helper" => {{\\n  }}\\n}}\\n' ;;
  */com.apple.textcontextd|*/com.example.menu-helper) exit 77 ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            env = os.environ.copy()
            env["TARTCI_LAUNCHCTL"] = str(launchctl)
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--current-label",
                    "com.danielraffel.pulp.tart-runner-macos-gate",
                    "--runner-name",
                    "pulp-studio-01",
                    "--state-dir",
                    str(root / "state"),
                    "--agents-dir",
                    str(agents),
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(),
                [f"gui/{os.getuid()}"],
            )

    def test_plausible_service_launchctl_timeout_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "agents"
            agents.mkdir()
            launchctl = root / "launchctl"
            launchctl.write_text(
                """#!/bin/sh
case "$2" in
  gui/*/local.tart-macos-runner) sleep 1 ;;
  gui/*) printf 'services = {\n  "local.tart-macos-runner" => {\n  }\n}\n' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            env = os.environ.copy()
            env["TARTCI_LAUNCHCTL"] = str(launchctl)
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--current-label",
                    "com.danielraffel.pulp.tart-runner-macos-gate",
                    "--runner-name",
                    "pulp-studio-01",
                    "--state-dir",
                    str(root / "state"),
                    "--agents-dir",
                    str(agents),
                    "--launchctl-timeout-seconds",
                    "0.05",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("launchctl print timed out", result.stderr)

    def test_loaded_linux_provider_is_positively_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "agents"
            agents.mkdir()
            linux_label = "com.danielraffel.pulp.tart-runner-linux"
            with (agents / f"{linux_label}.plist").open("wb") as destination:
                plistlib.dump(
                    {
                        "Label": linux_label,
                        "ProgramArguments": [
                            "/bin/bash",
                            "/Users/danielraffel/.local/share/tartci/providers/tart-linux/runner.sh",
                            "--loop",
                        ],
                    },
                    destination,
                )
            launchctl = root / "launchctl"
            launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launchctl.chmod(0o755)
            env = os.environ.copy()
            env["TARTCI_LAUNCHCTL"] = str(launchctl)
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--current-label",
                    "com.danielraffel.pulp.tart-runner-macos-gate",
                    "--runner-name",
                    "pulp-vm-m5-01",
                    "--state-dir",
                    str(root / "state"),
                    "--agents-dir",
                    str(agents),
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_provider_under_tart_runner_label_stays_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "agents"
            agents.mkdir()
            unknown_label = "com.danielraffel.pulp.tart-runner-unknown"
            with (agents / f"{unknown_label}.plist").open("wb") as destination:
                plistlib.dump(
                    {
                        "Label": unknown_label,
                        "ProgramArguments": [
                            "/bin/bash",
                            "/Users/danielraffel/.local/share/tartci/providers/tart-unknown/runner.sh",
                            "--loop",
                        ],
                    },
                    destination,
                )
            launchctl = root / "launchctl"
            launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launchctl.chmod(0o755)
            env = os.environ.copy()
            env["TARTCI_LAUNCHCTL"] = str(launchctl)
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--current-label",
                    "com.danielraffel.pulp.tart-runner-macos-gate",
                    "--runner-name",
                    "pulp-vm-m5-01",
                    "--state-dir",
                    str(root / "state"),
                    "--agents-dir",
                    str(agents),
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(
                f"cannot prove plausible Tart runner {unknown_label} is unrelated",
                result.stderr,
            )

    def test_plausible_program_only_runner_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "agents"
            agents.mkdir()
            launchctl = root / "launchctl"
            launchctl.write_text(
                """#!/bin/sh
case "$2" in
  */local.tart-macos-runner) printf 'program = /unknown/wrapper\\n' ;;
  gui/*) printf 'services = {\\n  "local.tart-macos-runner" => {\\n  }\\n}\\n' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            env = os.environ.copy()
            env["TARTCI_LAUNCHCTL"] = str(launchctl)
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--current-label",
                    "com.danielraffel.pulp.tart-runner-macos-gate",
                    "--runner-name",
                    "pulp-studio-01",
                    "--state-dir",
                    str(root / "state"),
                    "--agents-dir",
                    str(agents),
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("cannot prove plausible Tart runner", result.stderr)

    def test_identity_parity_with_runtime_for_all_supported_sources(self) -> None:
        cases = (
            (
                "explicit",
                ["--name", "exact-name", "--state-dir", "/tmp/exact-state"],
                {},
            ),
            ("prefix-slot", ["--name-prefix", "lane", "--slot", "7"], {}),
            (
                "labels",
                ["--labels", "self-hosted,macOS,pulp-build-studio"],
                {},
            ),
            ("host", [], {}),
            (
                "legacy-name",
                [],
                {"PULP_RUNNER_NAME": "legacy-name", "PULP_RUNNER_SLOT": "8"},
            ),
            (
                "legacy-prefix-state",
                [],
                {
                    "PULP_RUNNER_NAME_PREFIX": "legacy-prefix",
                    "PULP_RUNNER_SLOT": "9",
                    "TARTCI_STATE_DIR": "/tmp/legacy-state",
                },
            ),
        )
        for label, cli_args, extra_env in cases:
            with self.subTest(label=label):
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if key
                    not in {
                        "TARTCI_RUNNER_NAME",
                        "PULP_RUNNER_NAME",
                        "TARTCI_RUNNER_NAME_PREFIX",
                        "PULP_RUNNER_NAME_PREFIX",
                        "TARTCI_RUNNER_SLOT",
                        "PULP_RUNNER_SLOT",
                        "TARTCI_RUNNER_LABELS",
                        "PULP_RUNNER_LABELS",
                        "TARTCI_STATE_DIR",
                    }
                }
                env.update(extra_env)
                env["HOME"] = "/tmp/identity-home"
                plist = {
                    "ProgramArguments": [
                        "/bin/bash",
                        str(RUNNER),
                        *cli_args,
                    ],
                    "EnvironmentVariables": {
                        "HOME": env["HOME"],
                        **extra_env,
                    },
                }
                runtime_hostname = subprocess.run(
                    ["hostname", "-s"],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                expected = resolve_plist_identity(
                    plist, hostname=runtime_hostname
                )
                result = subprocess.run(
                    [
                        "/bin/bash",
                        str(RUNNER),
                        *cli_args,
                        "--print-identity",
                    ],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = json.loads(result.stdout)
                self.assertEqual(actual["runner_name"], expected.runner_name)
                self.assertEqual(actual["state_dir"], expected.state_dir)
                self.assertEqual(actual["state_file"], expected.state_file)

    def test_runtime_preserves_event_log_env_unless_state_dir_is_explicit(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"TARTCI_EVENT_LOG", "TARTCI_STATE_DIR"}
        }
        env["TARTCI_EVENT_LOG"] = "/tmp/custom-events.jsonl"
        custom = subprocess.run(
            ["/bin/bash", str(RUNNER), "--print-event-log"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(custom.returncode, 0, custom.stderr)
        self.assertEqual(custom.stdout.strip(), "/tmp/custom-events.jsonl")

        overridden = subprocess.run(
            [
                "/bin/bash",
                str(RUNNER),
                "--state-dir",
                "/tmp/explicit-state",
                "--print-event-log",
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(overridden.returncode, 0, overridden.stderr)
        self.assertEqual(
            overridden.stdout.strip(), "/tmp/explicit-state/events.jsonl"
        )

        normalized = subprocess.run(
            [
                "/bin/bash",
                str(RUNNER),
                "--state-dir",
                "~/.normalized-state",
                "--print-event-log",
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(normalized.returncode, 0, normalized.stderr)
        self.assertEqual(
            normalized.stdout.strip(),
            f"{env.get('HOME', str(Path.home()))}/.normalized-state/events.jsonl",
        )


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
        (fake_bin / "ghapp").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
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
        for command in ("plutil", "ghapp", "launchctl"):
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
            self.assertEqual(
                plist["EnvironmentVariables"]["TARTCI_GH_CLI"], "ghapp"
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
