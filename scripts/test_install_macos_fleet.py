#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock
from pathlib import Path

import tartci_support_manifest as support_manifest
import network_profile as network


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
        ghapp = self.bin / "ghapp"
        ghapp.write_text(textwrap.dedent("""\
            #!/bin/sh
            [ "${FAKE_AUTH_DENY:-0}" = 1 ] && exit 1
            for arg in "$@"; do
              case "$arg" in
                repos/danielraffel/tartci/commits/*)
                  printf '%s\n' "${FAKE_AUTH_SHA:-${arg##*/}}"
                  exit 0
                  ;;
              esac
            done
            exit 0
        """))
        ghapp.chmod(0o755)
        self.calls = self.root / "launchctl.calls"
        self.launchctl_state = self.root / "launchctl-state.json"
        launchctl = self.fakebin / "launchctl"
        launchctl.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, os, plistlib, sys
            from pathlib import Path

            calls = Path({str(self.calls)!r})
            state_path = Path({str(self.launchctl_state)!r})
            calls.write_text((calls.read_text() if calls.exists() else "") + " ".join(sys.argv[1:]) + "\\n")
            state = json.loads(state_path.read_text()) if state_path.exists() else {{}}
            command = sys.argv[1] if len(sys.argv) > 1 else ""
            target = sys.argv[2] if len(sys.argv) > 2 else ""
            label = target.rsplit("/", 1)[-1]
            if command == "bootstrap":
                plist_path = Path(sys.argv[3])
                state[plist_path.stem] = str(plist_path)
                state_path.write_text(json.dumps(state))
                raise SystemExit(0)
            if command == "bootout":
                state.pop(label, None)
                state_path.write_text(json.dumps(state))
                raise SystemExit(0)
            if command != "print":
                raise SystemExit(0)
            forced = os.environ.get("FAKE_LOADED_LABEL", "__none__")
            if forced in target and label not in state:
                print("gui/501/" + label + " = {{\\n\\tstate = running\\n}}")
                raise SystemExit(0)
            if os.environ.get("FAKE_LAUNCHCTL_ERROR") == "1":
                print("launchctl IPC unavailable", file=sys.stderr)
                raise SystemExit(64)
            if label not in state:
                print(f'Bad request. Could not find service "{{target}}" in domain for user gui: 501', file=sys.stderr)
                raise SystemExit(1)
            path = Path(state[label])
            value = plistlib.loads(path.read_bytes())
            print("gui/501/" + label + " = {{")
            print(f"\\tpath = {{path}}")
            print("\\ttype = LaunchAgent")
            print("\\tstate = running\\n")
            print(f"\\tprogram = {{value['ProgramArguments'][0]}}")
            print("\\targuments = {{")
            arguments = list(value["ProgramArguments"])
            if os.environ.get("FAKE_WRONG_ARGUMENT"):
                arguments.append(os.environ["FAKE_WRONG_ARGUMENT"])
            for argument in arguments:
                print(f"\\t\\t{{argument}}")
            print("\\t}}\\n")
            print(f"\\tworking directory = {{value['WorkingDirectory']}}\\n")
            print(f"\\tstdout path = {{value['StandardOutPath']}}")
            print(f"\\tstderr path = {{value['StandardErrorPath']}}\\n")
            print(f"\\texit timeout = {{value['ExitTimeOut']}} seconds\\n")
            print("\\tenvironment = {{")
            for key, item in value["EnvironmentVariables"].items():
                print(f"\\t\\t{{key}} => {{item}}")
            extra = os.environ.get("FAKE_OBSOLETE_ENV")
            if extra:
                print(f"\\t\\t{{extra}} => stale")
            print("\\t}}\\n")
            print("\\tproperties = keepalive | runatload | inferred program")
            print("}}")
        """))
        launchctl.chmod(0o755)
        shipyard = self.fakebin / "shipyard"
        shipyard.write_text("#!/bin/sh\necho \"${FAKE_SHIPYARD_TAG:-m1}\"\n")
        shipyard.chmod(0o755)
        self.github_app_key = (
            self.home / ".config/shipyard/github-apps/shipyard-local.private-key.pem"
        )
        self.github_app_key.parent.mkdir(parents=True)
        self.github_app_key.write_text("fixture key reference only\n")
        self.github_app_key.chmod(0o600)
        self.github_app_cache = self.home / ".config/shipyard/ghapp-cache"
        self.github_app_cache.mkdir()
        self.github_app_cache.chmod(0o700)
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
            [github_app]
            id = "3878000"
            private_key_path = "{self.github_app_key}"
            cache_dir = "{self.github_app_cache}"
            [[lane]]
            id = "forge-gate"
            repo = "Generous-Corp/forge"
            runner_group_id = 11
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
        self.support_source = self.root / "support-source"
        for name in sorted(support_manifest.filesystem_names(ROOT)):
            source = ROOT / name
            target = self.support_source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        subprocess.run(["git", "init", "-q", str(self.support_source)], check=True)
        subprocess.run(["git", "-C", str(self.support_source), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.support_source), "config", "user.name", "Test"], check=True)
        subprocess.run([
            "git", "-C", str(self.support_source), "remote", "add", "origin",
            "https://github.com/danielraffel/tartci.git",
        ], check=True)
        subprocess.run(["git", "-C", str(self.support_source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.support_source), "commit", "-qm", "fixture"], check=True)
        self.support_manifest = self.support_source / support_manifest.MANIFEST_NAME
        support_manifest.write(self.support_source, self.support_manifest)

    def tearDown(self) -> None:
        for directory in [self.root, *self.root.rglob("*")]:
            if directory.is_dir() and not directory.is_symlink():
                directory.chmod(0o755)
        self.temp.cleanup()

    def run_installer(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        effective = self.env.copy()
        effective.update(env)
        return subprocess.run(
            [str(ROOT / "tartci"), "fleet-macos", "install", str(self.config),
             "--support-source", str(self.support_source),
             "--support-manifest", str(self.support_manifest), *args],
            text=True, capture_output=True, check=False, env=effective,
        )

    def test_explicit_toml_python_path_with_spaces_is_used(self) -> None:
        interpreter_dir = self.root / "Python Tools"
        interpreter_dir.mkdir()
        interpreter = interpreter_dir / "python3"
        shutil.copy2(sys.executable, interpreter)
        interpreter.chmod(0o755)
        result = self.run_installer("--apply", TARTCI_PYTHON=str(interpreter))
        self.assertEqual(result.returncode, 0, result.stderr)

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
        self.assertEqual(
            plistlib.loads(installed[0].read_bytes())["EnvironmentVariables"]
            ["TARTCI_RUNNER_GROUP_ID"],
            "11",
        )
        installed_env = plistlib.loads(installed[0].read_bytes())[
            "EnvironmentVariables"
        ]
        self.assertEqual(installed_env["TARTCI_RUNNER_REPO"], "Generous-Corp/forge")
        self.assertEqual(installed_env["SHIPYARD_GH_APP_REPO"], "Generous-Corp/forge")
        self.assertEqual(installed_env["SHIPYARD_GITHUB_APP_ID"], "3878000")
        self.assertEqual(
            installed_env["SHIPYARD_GITHUB_APP_PRIVATE_KEY_PATH"],
            str(self.github_app_key),
        )
        self.assertEqual(
            installed_env["SHIPYARD_GITHUB_APP_CACHE_DIR"],
            str(self.github_app_cache),
        )
        self.assertFalse(self.legacy.exists())
        self.assertFalse(stale.exists())
        retired = list((self.agents / ".tartci-retired").rglob("*.retired"))
        self.assertEqual(1, len(retired))
        self.assertEqual("legacy\n", retired[0].read_text())
        stale_backups = list((self.agents / ".tartci-retired").rglob("*.stale"))
        self.assertEqual(1, len(stale_backups))
        self.assertEqual("stale\n", stale_backups[0].read_text())
        receipt = self.home / ".config/tartci/macos-fleet-install.json"
        receipt_value = json.loads(receipt.read_text())
        self.assertEqual("test-fleet", receipt_value["profile"])
        support_root = Path(receipt_value["support"]["root"])
        self.assertEqual(support_root, Path(receipt_value["support"]["entrypoint"]["support_root"]))
        self.assertEqual(
            support_manifest.canonical_wrapper_bytes(support_root),
            (self.bin / "tartci").read_bytes(),
        )
        self.assertTrue((support_root / support_manifest.MANIFEST_NAME).is_file())
        installed_plist = plistlib.loads(installed[0].read_bytes())
        self.assertEqual(
            str(support_root / support_manifest.LAUNCH_NAME),
            installed_plist["ProgramArguments"][1],
        )
        self.assertNotIn("bootstrap", self.calls.read_text())

    def test_apply_uses_installed_ghapp_context_when_profile_has_no_app_projection(self) -> None:
        value = self.config.read_text()
        start = value.index("[github_app]\n")
        end = value.index("[[lane]]\n", start)
        self.config.write_text(value[:start] + value[end:])
        result = self.run_installer("--apply")
        self.assertEqual(0, result.returncode, result.stderr)
        installed = plistlib.loads(
            next(self.agents.glob("*macos-fleet*.plist")).read_bytes()
        )
        environment = installed["EnvironmentVariables"]
        self.assertNotIn("SHIPYARD_GITHUB_APP_ID", environment)
        receipt = json.loads(
            (self.home / ".config/tartci/macos-fleet-install.json").read_text()
        )
        self.assertEqual(
            receipt["support"]["source_commit"],
            receipt["support"]["source_authority"]["commit"],
        )

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

    def test_apply_atomically_repairs_missing_canonical_entrypoint(self) -> None:
        (self.bin / "tartci").unlink()
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.home / ".config/tartci/macos-fleet-install.json").read_text()
        )
        support_root = Path(receipt["support"]["root"])
        self.assertEqual(
            support_manifest.canonical_wrapper_bytes(support_root),
            (self.bin / "tartci").read_bytes(),
        )

    def test_apply_replaces_entrypoint_symlink_without_touching_its_target(self) -> None:
        decoy = self.root / "decoy-tartci"
        decoy.write_text("preserve me\n")
        (self.bin / "tartci").unlink()
        (self.bin / "tartci").symlink_to(decoy)
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.bin / "tartci").is_symlink())
        self.assertEqual("preserve me\n", decoy.read_text())

    def test_reinstall_continuous_reader_observes_only_complete_wrappers(self) -> None:
        first = self.run_installer("--apply")
        self.assertEqual(0, first.returncode, first.stderr)
        old_wrapper = (self.bin / "tartci").read_bytes()
        changed = self.support_source / "scripts/current_job_scan.py"
        changed.write_bytes(changed.read_bytes() + b"# next generation\n")
        subprocess.run(
            ["git", "-C", str(self.support_source), "add", str(changed)], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.support_source), "commit", "-qm", "next"],
            check=True,
        )
        support_manifest.write(self.support_source, self.support_manifest)
        manifest = support_manifest.load(self.support_manifest)
        manifest_digest = support_manifest.digest(self.support_manifest)
        support_root = (
            self.home / ".local/share/tartci-generations"
            / f"{manifest['source_commit']}-{manifest_digest[:16]}"
        )
        allowed = {
            old_wrapper,
            support_manifest.canonical_wrapper_bytes(support_root),
        }
        stop = threading.Event()
        failures: list[str] = []

        def read_continuously() -> None:
            while not stop.is_set():
                try:
                    value = (self.bin / "tartci").read_bytes()
                except OSError as exc:
                    failures.append(f"unavailable: {exc}")
                    return
                if value not in allowed:
                    failures.append(f"mixed: {value!r}")
                    return

        reader = threading.Thread(target=read_continuously)
        reader.start()
        try:
            second = self.run_installer("--apply")
        finally:
            stop.set()
            reader.join()
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual([], failures)
        self.assertEqual(
            support_manifest.canonical_wrapper_bytes(support_root),
            (self.bin / "tartci").read_bytes(),
        )

    def test_apply_rejects_unsafe_github_app_key_before_mutation(self) -> None:
        self.github_app_key.chmod(0o644)
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("private key must be an owned mode-0600 regular file", result.stderr)
        self.assertTrue(self.legacy.exists())
        self.assertEqual([], list(self.agents.glob("*macos-fleet*.plist")))

    def test_apply_rejects_unsafe_github_app_cache_before_mutation(self) -> None:
        self.github_app_cache.chmod(0o755)
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cache must be an owned mode-0700 directory", result.stderr)
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

    def test_apply_requires_authenticated_exact_repository_commit(self) -> None:
        for env, message in (
            ({"FAKE_AUTH_DENY": "1"}, "could not authenticate"),
            ({"FAKE_AUTH_SHA": "0" * 40}, "mismatched commit"),
        ):
            with self.subTest(env=env):
                result = self.run_installer("--apply", **env)
                self.assertEqual(3, result.returncode)
                self.assertIn(message, result.stderr)
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
        self.assertEqual("#!/bin/sh\nexit 0\n", (self.bin / "tartci").read_text())

    def test_failed_final_verification_restores_prior_wrapper_generation(self) -> None:
        prior = (self.bin / "tartci").read_bytes()
        python = self.fakebin / "python3"
        python.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            if [ "$2" = verify-installed ]; then exit 1; fi
            exec {sys.executable} "$@"
        """))
        python.chmod(0o755)
        result = self.run_installer("--apply")
        self.assertEqual(1, result.returncode)
        self.assertEqual(prior, (self.bin / "tartci").read_bytes())
        self.assertFalse(
            (self.home / ".config/tartci/macos-fleet-install.json").exists()
        )
        self.assertEqual([], list(self.bin.glob(".tartci-wrapper.*")))

    def test_sigkill_after_receipt_is_fail_closed_and_reinstall_recovers(self) -> None:
        crashed = self.run_installer(
            "--apply", TARTCI_TESTING="1", TARTCI_INSTALL_CRASH_AFTER="receipt"
        )
        self.assertEqual(-9, crashed.returncode, crashed.stderr)
        receipt = json.loads(
            (self.home / ".config/tartci/macos-fleet-install.json").read_text()
        )
        support_root = Path(receipt["support"]["root"])
        repaired_lock = subprocess.run(
            [str(support_root / "tartci"), "pool", "repair-lock"],
            text=True,
            capture_output=True,
            check=False,
            env=self.env,
        )
        self.assertEqual(0, repaired_lock.returncode, repaired_lock.stderr)
        refused = subprocess.run(
            [str(support_root / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env=self.env,
        )
        self.assertEqual(7, refused.returncode, refused.stderr)
        self.assertIn("no valid receipt", refused.stderr)
        repaired = self.run_installer("--apply")
        self.assertEqual(0, repaired.returncode, repaired.stderr)
        admitted = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env=self.env,
        )
        self.assertEqual(0, admitted.returncode, admitted.stderr)

    def test_sigkill_after_wrapper_is_coherent_on_restart(self) -> None:
        crashed = self.run_installer(
            "--apply", TARTCI_TESTING="1", TARTCI_INSTALL_CRASH_AFTER="wrapper"
        )
        self.assertEqual(-9, crashed.returncode, crashed.stderr)
        repaired_lock = subprocess.run(
            [str(self.bin / "tartci"), "pool", "repair-lock"],
            text=True,
            capture_output=True,
            check=False,
            env=self.env,
        )
        self.assertEqual(0, repaired_lock.returncode, repaired_lock.stderr)
        admitted = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env=self.env,
        )
        self.assertEqual(0, admitted.returncode, admitted.stderr)

    def test_pool_on_refuses_tampered_install_before_opening_admission(self) -> None:
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        target = next(self.agents.glob("*macos-fleet*.plist"))
        target.write_bytes(target.read_bytes() + b"\n")
        self.calls.write_text("")
        result = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"], text=True,
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
            [str(self.bin / "tartci"), "pool", "on"], text=True,
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
            [str(self.bin / "tartci"), "pool", "on"], text=True,
            capture_output=True, check=False, env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text()
        self.assertIn("tart-runner-macos-fleet.m1.forge-gate", calls)
        self.assertNotIn("com.danielraffel.forge.tart-runner-macos.plist", calls)
        self.assertEqual("on", (self.home / ".config/tartci/pool-state").read_text().strip())
        loaded = json.loads(
            (self.home / ".config/tartci/macos-fleet-loaded.json").read_text()
        )
        self.assertEqual(1, loaded["schema"])
        self.assertEqual(set(receipt["plists"]), set(loaded["loaded_services"]))

    def test_loaded_verifier_accepts_exact_receipted_network_overlay(self) -> None:
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        target = next(self.agents.glob("*macos-fleet*.plist"))
        profile = self.home / ".config/tartci/custom/network-profile.toml"
        profile.parent.mkdir()
        profile.write_text(
            "schema_version = 1\n[http_connect_relay]\n"
            "enabled = true\nrelay_hosts = [\"relay-a\", \"relay-b\"]\n"
            "github_cli = \"ghapp\"\n"
            "github_probe_repo = \"Generous-Corp/pulp\"\n"
            "probe_timeout_seconds = 15\n"
        )
        with (
            mock.patch.object(network, "_loaded_path", return_value=None),
            mock.patch.object(network, "_reload", return_value=True),
            mock.patch.object(network, "authenticated_probe", return_value=(True, "authenticated")),
            mock.patch.object(network, "_any_tart_vm_running", return_value=False),
            mock.patch.object(network.Path, "home", return_value=self.home),
            mock.patch.dict(os.environ, {
                "TARTCI_POOL_TRANSITION_LOCK": str(self.root / "pool.lock")
            }),
        ):
            reconciled = network.reconcile(
                profile,
                self.agents,
                participation_path=self.home / ".config/tartci/native-build-participation",
            )
        self.assertTrue(reconciled["ok"], reconciled)
        receipt_path = self.home / ".config/tartci/macos-fleet-install.json"
        receipt = json.loads(receipt_path.read_text())
        support_root = Path(receipt["support"]["root"])
        subprocess.run(
            [str(self.fakebin / "launchctl"), "bootstrap", "gui/501", str(target)],
            text=True, capture_output=True, check=True, env=self.env,
        )
        loaded_path = self.home / ".config/tartci/macos-fleet-loaded.json"
        verified = subprocess.run(
            [sys.executable, str(support_root / "scripts/macos_fleet_lanes.py"),
             "verify-loaded", str(receipt_path),
             "--config", str(self.home / ".config/tartci/macos-fleet-profile.toml"),
             "--agents-dir", str(self.agents),
             "--support-root", str(support_root),
             "--output", str(loaded_path)],
            text=True, capture_output=True, check=False,
            env={**self.env, "TARTCI_NETWORK_PROFILE": str(profile)},
        )
        self.assertEqual(0, verified.returncode, verified.stderr)
        loaded = json.loads(loaded_path.read_text())
        self.assertEqual([target.name], list(loaded["loaded_services"]))

    def test_pool_on_does_not_activate_unreceipted_runner_services(self) -> None:
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        foreign = self.agents / "actions.runner.Generous-Corp-pulp.unreceipted.plist"
        foreign.write_text("unreceipted runner fixture\n")
        self.calls.write_text("")
        result = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env=self.env,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        calls = self.calls.read_text()
        self.assertIn("tart-runner-macos-fleet.m1.forge-gate", calls)
        self.assertNotIn("unreceipted", calls)

    def test_pool_on_refuses_obsolete_loaded_governed_environment(self) -> None:
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        result = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env={**self.env, "FAKE_OBSOLETE_ENV": "TARTCI_VM_LEASE_PRIORITY"},
        )
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertIn("loaded runner generation did not match", result.stderr)
        self.assertEqual("off", (self.home / ".config/tartci/pool-state").read_text().strip())
        self.assertFalse((self.home / ".config/tartci/macos-fleet-loaded.json").exists())

    def test_failed_reopen_rolls_back_every_newly_loaded_service(self) -> None:
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        state = self.home / ".config/tartci"
        (state / "native-build-participation").write_text("1\n")
        (state / "pool-state").write_text("on\n")
        self.calls.write_text("")
        result = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env={**self.env, "FAKE_OBSOLETE_ENV": "TARTCI_VM_LEASE_PRIORITY"},
        )
        self.assertEqual(7, result.returncode, result.stderr)
        calls = self.calls.read_text()
        self.assertIn("bootstrap", calls)
        self.assertIn("bootout", calls)
        self.assertEqual({}, json.loads(self.launchctl_state.read_text()))

    def test_pool_on_refuses_tampered_entrypoint_before_loading_services(self) -> None:
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        receipt = json.loads(
            (self.home / ".config/tartci/macos-fleet-install.json").read_text()
        )
        support_root = Path(receipt["support"]["root"])
        (self.bin / "tartci").write_text("#!/bin/bash\nexit 1\n")
        (self.bin / "tartci").chmod(0o755)
        self.calls.write_text("")
        result = subprocess.run(
            [str(support_root / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env=self.env,
        )
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertIn("no valid receipt", result.stderr)
        self.assertNotIn("bootstrap", self.calls.read_text())

    def test_ordinary_wrapper_restart_revalidates_immutable_support(self) -> None:
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        receipt = json.loads(
            (self.home / ".config/tartci/macos-fleet-install.json").read_text()
        )
        support_root = Path(receipt["support"]["root"])
        launch = support_root / support_manifest.LAUNCH_NAME
        (self.bin / "tartci").write_text("#!/bin/bash\nexit 99\n")
        (self.bin / "tartci").chmod(0o755)
        direct = subprocess.run(
            ["/bin/bash", str(launch), "help"],
            text=True,
            capture_output=True,
            check=False,
            env=self.env,
        )
        self.assertEqual(0, direct.returncode, direct.stderr)
        helper = support_root / "scripts/current_job_scan.py"
        helper.chmod(0o755)
        helper.write_bytes(helper.read_bytes() + b"# drift\n")
        restarted = subprocess.run(
            ["/bin/bash", str(launch), "help"],
            text=True,
            capture_output=True,
            check=False,
            env=self.env,
        )
        self.assertEqual(2, restarted.returncode)
        self.assertIn("failed verification", restarted.stderr)

    def test_pool_on_refuses_loaded_argument_drift_and_remains_closed(self) -> None:
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        result = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env={**self.env, "FAKE_WRONG_ARGUMENT": "--foreign"},
        )
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertEqual("off", (self.home / ".config/tartci/pool-state").read_text().strip())

    def test_pool_restart_revalidates_same_generation_before_readmission(self) -> None:
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        first = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"], text=True,
            capture_output=True, check=False, env=self.env,
        )
        self.assertEqual(0, first.returncode, first.stderr)
        first_loaded = json.loads(
            (self.home / ".config/tartci/macos-fleet-loaded.json").read_text()
        )
        stopped = subprocess.run(
            [str(self.bin / "tartci"), "pool", "off"], text=True,
            capture_output=True, check=False, env=self.env,
        )
        self.assertEqual(0, stopped.returncode, stopped.stderr)
        second = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"], text=True,
            capture_output=True, check=False, env=self.env,
        )
        self.assertEqual(0, second.returncode, second.stderr)
        second_loaded = json.loads(
            (self.home / ".config/tartci/macos-fleet-loaded.json").read_text()
        )
        self.assertEqual(
            first_loaded["install_receipt_sha256"],
            second_loaded["install_receipt_sha256"],
        )
        self.assertEqual(
            first_loaded["loaded_services"], second_loaded["loaded_services"]
        )


if __name__ == "__main__":
    unittest.main()
