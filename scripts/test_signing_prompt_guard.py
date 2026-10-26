#!/usr/bin/env python3
"""The keychain setup is checked for anything that can raise a password dialog.

On 2026-10-02 the legacy pulp-signing keychain sat on m3's and m5studio's
search list beside its -unattended replacement, and the self-update probe's
bare `codesign --sign` put dialogs on m5studio's screen at each attempt.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fleet_doctor as fd  # noqa: E402
import fleet_self_update as su  # noqa: E402
import keychain_unlock  # noqa: E402
import signing_prompt_guard as guard  # noqa: E402
import tartci_support_manifest as support_manifest  # noqa: E402


class Host(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.kc = self.home / "Library" / "Keychains"
        self.kc.mkdir(parents=True)
        self.legacy = self.kc / "pulp-signing.keychain-db"
        self.sibling = self.kc / "pulp-signing-unattended.keychain-db"
        self.login = self.kc / "login.keychain-db"
        for path in (self.legacy, self.sibling, self.login):
            path.write_text("")
        secrets = self.home / ".config" / "pulp" / "secrets"
        secrets.mkdir(parents=True)
        (secrets / "keychain.env").write_text(
            f'PULP_SIGN_KEYCHAIN="{self.legacy}"\nPULP_SIGN_KEYCHAIN_PW=pw\n')
        self.listed = [self.sibling, self.login]
        self.unlock_rc = 0
        self.info = f'Keychain "{self.sibling}" no-timeout'
        self.calls: list[list[str]] = []
        env = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.home / ".tartci")})
        env.start()
        self.addCleanup(env.stop)
        self.agent(state="ok", age=60)

    def agent(self, state: str | None, age: float = 60) -> None:
        plist = self.home / "Library" / "LaunchAgents" / f"{keychain_unlock.LABEL}.plist"
        if state is None:
            plist.unlink(missing_ok=True)
            return
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("<plist/>")
        path = keychain_unlock.state_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"state": state, "at": time.time() - age,
                                    "detail": "unlock failed (exit 51)" if state == "failed"
                                    else "unlocked, no auto-lock"}))

    def run_(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[1] == "list-keychains":
            return 0, "".join(f'    "{p}"\n' for p in self.listed)
        if argv[1] == "unlock-keychain":
            return self.unlock_rc, "" if self.unlock_rc == 0 else "The specified keychain could not be unlocked"
        if argv[1] == "show-keychain-info":
            return 0, self.info
        raise AssertionError(argv)

    def status(self) -> dict:
        return guard.status(self.home, self.run_)


@unittest.skipUnless(sys.platform == "darwin", "macOS launcher builder")
class LauncherBuildTests(unittest.TestCase):
    """Exercise the builder's actual render and lane pin validation."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        source = root / "source"
        source.mkdir()
        archive = subprocess.run(
            ["git", "-C", str(HERE.parent), "archive", "HEAD"],
            check=True, capture_output=True,
        ).stdout
        subprocess.run(["tar", "-x", "-C", str(source)], input=archive, check=True)
        subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(source), "remote", "add", "origin",
             "https://github.com/danielraffel/tartci.git"], check=True,
        )
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(source), "-c", "user.name=Test", "-c",
             "user.email=test@example.invalid", "commit", "-qm", "fixture"],
            check=True,
        )
        manifest = root / ".tartci-support-manifest.json"
        support_manifest.write(source, manifest)
        staged = support_manifest.stage_install(source, manifest, root / "generations")
        self.support_root = Path(staged["root"])
        self.profile = source / "profiles/m3-macos-fleet.toml"
        self.identity = "Developer ID Application: Daniel Raffel (95CX6P84C4)"

    def run_builder(self, output: Path, approval: Path, *, python: Path | None = None,
                    extra: tuple[str, ...] = (), identity: str | None = None):
        args = [str(HERE / "build_macos_launcher.sh"),
                "--output", str(output), "--approval-output", str(approval),
                "--identity", identity or self.identity, "--support-root", str(self.support_root),
                "--profile", str(self.profile), *extra]
        if python is not None:
            args += ["--python", str(python)]
        return subprocess.run(
            args,
            env={**os.environ, "TARTCI_PYTHON": "", "TARTCI_RENDER_PYTHON": ""},
            capture_output=True, text=True, check=False,
        )

    def test_real_render_without_pin_pins_every_lane(self) -> None:
        root = Path(self.temp.name)
        output, approval = root / "TartCILauncher.app", root / "approval.sha256"
        result = self.run_builder(output, approval)
        self.assertEqual(result.returncode, 0, result.stderr)
        lanes = json.loads(
            (output / "Contents/Resources/lanes.json").read_text()
        )["lanes"]
        self.assertTrue(lanes)
        resolved = subprocess.run(
            [str(HERE / "build_macos_launcher.sh"), "--support-root", str(self.support_root),
             "--print-render-python"],
            env={**os.environ, "TARTCI_PYTHON": "", "TARTCI_RENDER_PYTHON": ""},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        expected = resolved.stdout.strip()
        for lane in lanes.values():
            environment = lane["environment"]
            self.assertEqual(environment["TARTCI_PYTHON"], expected)
            self.assertEqual(environment["SHIPYARD_GHAPP_PYTHON_BINARY"], expected)

    def test_builder_refuses_a_render_that_drops_the_pin(self) -> None:
        root = Path(self.temp.name)
        wrapper = root / "unpin-python"
        wrapper.write_text(
            "#!/bin/bash\n"
            "if [[ \"$1\" == *macos_fleet_lanes.py && \"$2\" == render ]]; then\n"
            "  unset TARTCI_PYTHON TARTCI_RENDER_PYTHON\n"
            "fi\n"
            f"exec {shutil.which('python3')!s} \"$@\"\n"
        )
        wrapper.chmod(0o755)
        result = self.run_builder(root / "bad.app", root / "bad.sha256", python=wrapper)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lane helper interpreter is not pinned", result.stderr)


class LauncherV2BuildTests(LauncherBuildTests):
    """--layout v2: the stable app carries no support tree, lanes or commit."""

    def test_real_render_without_pin_pins_every_lane(self) -> None:
        pass      # a v1 property; covered by LauncherBuildTests

    def test_builder_refuses_a_render_that_drops_the_pin(self) -> None:
        pass      # a v1 property; covered by LauncherBuildTests

    def test_v2_bundle_is_the_executable_info_plist_and_policy_only(self) -> None:
        root = Path(self.temp.name)
        output, approval = root / "v2.app", root / "v2.sha256"
        # A Developer ID from the environment when the host has one, else ad hoc.
        identity = os.environ.get("TARTCI_TEST_SIGNING_IDENTITY", "-")
        result = self.run_builder(output, approval, extra=("--layout", "v2"), identity=identity)
        self.assertEqual(result.returncode, 0, result.stderr)
        files = sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file())
        self.assertEqual(["Contents/Info.plist", "Contents/MacOS/tartci-launcher",
                          "Contents/Resources/inputs.json", "Contents/Resources/policy.json",
                          "Contents/_CodeSignature/CodeResources"], files)
        # The expectations come from the interpreter the builder itself
        # resolves: this test may run on the hosts' Python 3.9, without tomllib.
        builder_python = subprocess.run(
            [str(HERE / "build_macos_launcher.sh"), "--support-root", str(self.support_root),
             "--print-render-python"], env={**os.environ, "TARTCI_PYTHON": "",
                                            "TARTCI_RENDER_PYTHON": ""},
            capture_output=True, text=True, check=True).stdout.strip()
        expected = json.loads(subprocess.run(
            [builder_python, "-c",
             "import json, sys, tomllib; sys.path.insert(0, sys.argv[1]); "
             "import macos_launcher_identity as m; p = tomllib.loads(open(sys.argv[2]).read()); "
             "print(json.dumps({'digest': m.source_inputs_digest(sys.argv[3], p), 'profile': p}))",
             str(HERE), str(self.profile), str(self.support_root)],
            capture_output=True, text=True, check=True).stdout)
        inputs = json.loads((output / "Contents/Resources/inputs.json").read_text())
        self.assertEqual(expected["digest"], inputs["sha256"])
        self.assertEqual((self.support_root / "native/macos/tartci-launcher/Info.plist").read_bytes(),
                         (output / "Contents/Info.plist").read_bytes())
        policy = json.loads((output / "Contents/Resources/policy.json").read_text())
        profile = expected["profile"]
        home = profile["host"]["home"].rstrip("/")
        self.assertEqual({
            "schema": 2, "identifier": profile["launch_helper"]["identifier"],
            "team_id": profile["launch_helper"]["team_id"],
            "tart_home": profile["host"]["tart_home"],
            "generations_root": f"{home}/.local/share/tartci-generations",
            "home": home,
            "pointer": f"{home}/.config/tartci/launcher-generation.json",
            "floor": f"{home}/.config/tartci/launcher-floor.json"}, policy)
        self.assertRegex(approval.read_text(), r"^[0-9a-f]{64}\n$")

    def test_an_unknown_layout_is_refused(self) -> None:
        root = Path(self.temp.name)
        result = self.run_builder(root / "x.app", root / "x.sha256", extra=("--layout", "v3"))
        self.assertEqual(64, result.returncode)
        self.assertFalse((root / "x.app").exists())


class GuardTests(Host):
    def test_only_the_dedicated_keychain_and_login_is_ok(self) -> None:
        value = self.status()
        self.assertEqual(value["state"], "ok", value)
        self.assertIn("pulp-signing-unattended.keychain-db", guard.describe(value))
        self.assertEqual(fd.check_signing_prompts(value, self.home).code, "signing_prompts_ok")

    def test_a_legacy_keychain_on_the_search_list_is_a_risk(self) -> None:
        # m3 on 2026-10-02: legacy first, then the sibling, then login.
        self.listed = [self.legacy, self.sibling, self.login]
        value = self.status()
        self.assertEqual(value["state"], "risk")
        self.assertIn(str(self.legacy), value["risks"][0])
        finding = fd.check_signing_prompts(value, self.home)
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "signing_prompts_risk"))

    def test_a_drifted_password_is_a_risk_and_settings_are_not_read_locked(self) -> None:
        self.unlock_rc = 51
        value = self.status()
        self.assertEqual(value["state"], "risk")
        self.assertIn("does not unlock", value["risks"][0])
        # Reading settings of a locked keychain is itself what prompts.
        self.assertFalse(any(c[1] == "show-keychain-info" for c in self.calls))

    def test_a_keychain_that_relocks_on_its_own_is_a_risk(self) -> None:
        self.info = f'Keychain "{self.sibling}" lock-on-sleep timeout=300s'
        value = self.status()
        self.assertEqual(value["state"], "risk")
        self.assertIn("re-locks on its own", value["risks"][0])

    def test_a_host_without_keychain_env_is_not_applicable(self) -> None:
        (self.home / ".config" / "pulp" / "secrets" / "keychain.env").unlink()
        self.assertEqual(self.status()["state"], "not_applicable")
        self.assertEqual(self.calls, [])


class UnlockAgentGuardTests(Host):
    """The keychain is locked again at every login; the agent is what unlocks it."""

    def test_a_missing_agent_is_a_risk(self) -> None:
        self.agent(None)
        value = self.status()
        self.assertEqual(value["state"], "risk")
        self.assertIn("keychain-unlock agent is not installed", value["risks"][0])

    def test_a_failed_last_run_is_a_risk(self) -> None:
        self.agent("failed")
        self.assertIn("last run FAILED", self.status()["risks"][0])

    def test_an_agent_that_stopped_running_is_a_risk(self) -> None:
        self.agent("ok", age=guard.UNLOCK_STALE_SECS + 60)
        self.assertIn("min ago", self.status()["risks"][0])


class PinnedKeychainTests(Host):
    def test_the_sibling_is_the_dedicated_keychain_when_it_exists(self) -> None:
        self.assertEqual(su.signing_keychain(self.home), str(self.sibling))
        self.assertEqual(su.keychain_args(self.home), ["--keychain", str(self.sibling)])
        self.sibling.unlink()
        self.assertEqual(su.signing_keychain(self.home), str(self.legacy))

    def test_the_probe_names_the_keychain_instead_of_walking_the_search_list(self) -> None:
        seen = []

        class Sys:
            def run(self, argv, **kw):
                seen.append(argv)
                return su.Result(0, "", "")
        su.signing_probe(Sys(), "ABC", self.home, su.keychain_args(self.home))
        self.assertIn(["--keychain", str(self.sibling)],
                      [seen[0][i:i + 2] for i in range(len(seen[0]) - 1)])

    def test_the_launcher_build_signs_with_the_named_keychain(self) -> None:
        script = (HERE / "build_macos_launcher.sh").read_text()
        self.assertIn('--keychain) keychain="${2:-}"', script)
        self.assertRegex(script, r'codesign --force --timestamp --options runtime '
                                 r'\$\{keychain_args\[@\]\+"\$\{keychain_args\[@\]\}"\} --sign')
        source = (HERE / "fleet_self_update.py").read_text()
        body = source[source.index("def build_launcher("):]
        body = body[:body.index("\ndef ")]
        call = body[body.index('sys_.run(["bash", "scripts/build_macos_launcher.sh"'):]
        call = call[:call.index("cwd=")]
        self.assertIn('"--profile", str(profile),\n'
                      '                           "--python", launcher_python, *pinned,', call)
        # The builder takes the LAST --keychain it is given, so nothing after
        # the pinned one may name another.
        self.assertNotIn("--keychain", call[call.index("*pinned"):])

    def test_launcher_builder_resolves_a_real_interpreter_without_a_pin(self) -> None:
        env = {**os.environ, "TARTCI_PYTHON": "", "TARTCI_RENDER_PYTHON": ""}
        result = subprocess.run(
            [str(HERE / "build_macos_launcher.sh"), "--support-root", str(HERE.parent),
             "--print-render-python"],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        resolved = Path(result.stdout.strip())
        self.assertTrue(resolved.is_absolute())
        self.assertTrue(resolved.is_file())
        self.assertTrue(os.access(resolved, os.X_OK))
        # Canonical means the symlink chain is followed to the real file, not
        # that the file is named anything in particular. Where no python3.12 or
        # python3.11 is on PATH (ubuntu-26.04 ships 3.14) the resolver falls
        # back to `python3`, and CI's lint job has already replaced
        # /usr/bin/python3 with the real binary, so its name is `python3`.
        self.assertFalse(resolved.is_symlink(), resolved)
        probe = subprocess.run([str(resolved), "-c", "import tomllib"],
                               capture_output=True, text=True, check=False)
        self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_launcher_builder_reports_missing_interpreter(self) -> None:
        result = subprocess.run(
            [str(HERE / "build_macos_launcher.sh"), "--support-root", str(HERE.parent),
             "--print-render-python", "--python", "/does/not/exist"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 127)
        self.assertIn(
            "launcher build requires a Python 3.11+ interpreter with tomllib",
            result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
