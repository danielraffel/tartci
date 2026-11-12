#!/usr/bin/env python3
from __future__ import annotations

import testing_support  # noqa: E402
testing_support.skip_module_without_tomllib()
import json
import os
import pty
import select
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import textwrap
import threading
import unittest
from unittest import mock
from pathlib import Path

import tartci_support_manifest as support_manifest
import network_profile as network
import macos_fleet_lanes as fleet
import macos_launcher_identity


ROOT = Path(__file__).resolve().parents[1]


class InstallMacosFleetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.uid = os.getuid()
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
            [ "${FAKE_GHAPP_REQUIRE_HOME:-0}" = 1 ] && [ -z "${HOME:-}" ] && {
              echo "fake ghapp requires HOME" >&2
              exit 78
            }
            [ -n "${FAKE_GHAPP_EXPECTED_PATH:-}" ] && [ "$0" != "$FAKE_GHAPP_EXPECTED_PATH" ] && {
              echo "fake ghapp unexpected path: $0" >&2
              exit 79
            }
            [ "${FAKE_AUTH_DENY:-0}" = 1 ] && exit 1
            for arg in "$@"; do
              case "$arg" in
                repos/Generous-Corp/tartci|repos/Generous-Corp/tartci/commits/*)
                  echo "fake ghapp: repository installation not found" >&2
                  exit 1
                  ;;
              esac
              case "$arg" in
                repos/danielraffel/tartci/commits/*)
                  sha="${FAKE_AUTH_SHA:-${arg##*/}}"
                  repo="${FAKE_AUTH_REPOSITORY:-${arg#repos/}}"
                  repo="${repo%%/commits/*}"
                  printf '{"sha":"%s"}\n' "$sha"
                  exit 0
                  ;;
                repos/danielraffel/tartci)
                  repo="${FAKE_AUTH_REPOSITORY:-${arg#repos/}}"
                  printf '{"full_name":"%s"}\n' "$repo"
                  exit 0
                  ;;
              esac
            done
            exit 0
        """))
        ghapp.chmod(0o755)
        self.calls = self.root / "launchctl.calls"
        self.launchctl_state = self.root / "launchctl-state.json"
        self.launchctl_disabled = self.root / "launchctl-disabled.json"
        launchctl = self.fakebin / "launchctl"
        launchctl.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, os, plistlib, sys
            from pathlib import Path

            calls = Path({str(self.calls)!r})
            state_path = Path({str(self.launchctl_state)!r})
            disabled_path = Path({str(self.launchctl_disabled)!r})
            calls.write_text((calls.read_text() if calls.exists() else "") + " ".join(sys.argv[1:]) + "\\n")
            state = json.loads(state_path.read_text()) if state_path.exists() else {{}}
            disabled = set(json.loads(disabled_path.read_text())) if disabled_path.exists() else set()
            command = sys.argv[1] if len(sys.argv) > 1 else ""
            target = sys.argv[2] if len(sys.argv) > 2 else ""
            label = target.rsplit("/", 1)[-1]
            if command == "bootstrap":
                plist_path = Path(sys.argv[3])
                if os.environ.get("FAKE_FAIL_BOOTSTRAP_LABEL") == plist_path.stem:
                    raise SystemExit(70)
                state[plist_path.stem] = str(plist_path)
                state_path.write_text(json.dumps(state))
                raise SystemExit(0)
            if command == "enable":
                if os.environ.get("FAKE_FAIL_ENABLE_LABEL") == label:
                    raise SystemExit(70)
                disabled.discard(label)
                disabled_path.write_text(json.dumps(sorted(disabled)))
                raise SystemExit(0)
            if command == "disable":
                if os.environ.get("FAKE_FAIL_DISABLE_LABEL") == label:
                    raise SystemExit(70)
                disabled.add(label)
                disabled_path.write_text(json.dumps(sorted(disabled)))
                raise SystemExit(0)
            if command == "print-disabled":
                print("disabled services = {{")
                for item in sorted(disabled):
                    print(f'\t"{{item}}" => disabled')
                print("}}")
                raise SystemExit(0)
            if command == "bootout":
                if os.environ.get("FAKE_FAIL_BOOTOUT_LABEL") == label:
                    raise SystemExit(70)
                state.pop(label, None)
                state.pop(label + ":exit", None)
                state_path.write_text(json.dumps(state))
                raise SystemExit(0)
            if command == "kickstart":
                if os.environ.get("FAKE_FAIL_KICKSTART_LABEL") == label:
                    raise SystemExit(70)
                if label == "com.danielraffel.tartci.interpreter-preflight" and label in state:
                    # Run the interpreter preflight's job for real, as launchd
                    # would: its program, its log, its exit code.
                    import subprocess
                    job = plistlib.loads(Path(state[label]).read_bytes())
                    with open(job["StandardOutPath"], "ab") as log:
                        done = subprocess.run(job["ProgramArguments"], stdout=log,
                                              stderr=subprocess.STDOUT, check=False)
                    state[label + ":exit"] = done.returncode
                    state_path.write_text(json.dumps(state))
                raise SystemExit(0)
            if command != "print":
                raise SystemExit(0)
            if label == "com.danielraffel.tartci.interpreter-preflight" and label in state:
                print("gui/{self.uid}/" + label + " = {{")
                if label + ":exit" in state:
                    print("\tlast exit code = " + str(state[label + ":exit"]))
                print("}}")
                raise SystemExit(0)
            forced = os.environ.get("FAKE_LOADED_LABEL", "__none__")
            if forced in target and label not in state:
                print("gui/{self.uid}/" + label + " = {{\\n\\tstate = running\\n}}")
                raise SystemExit(0)
            if os.environ.get("FAKE_LAUNCHCTL_ERROR") == "1":
                print("launchctl IPC unavailable", file=sys.stderr)
                raise SystemExit(64)
            if label not in state:
                print(f'Bad request. Could not find service "{{target}}" in domain for user gui: {self.uid}', file=sys.stderr)
                raise SystemExit(1)
            path = Path(state[label])
            value = plistlib.loads(path.read_bytes())
            print("gui/{self.uid}/" + label + " = {{")
            print(f"\\tpath = {{path}}")
            print("\\ttype = LaunchAgent")
            print("\\tstate = running")
            print("\\tpid = 4242\\n")
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
            print(f"\\texit timeout = {{value.get('ExitTimeOut', 5)}} seconds\\n")
            print("\\tenvironment = {{")
            for key, item in value["EnvironmentVariables"].items():
                print(f"\\t\\t{{key}} => {{item}}")
            print("\\t\\tOSLogRateLimit => 64")
            print(f"\\t\\tXPC_SERVICE_NAME => {{label}}")
            extra = os.environ.get("FAKE_OBSOLETE_ENV")
            if extra:
                print(f"\\t\\t{{extra}} => stale")
            print("\\t}}\\n")
            if value.get("ProcessType") == "Interactive":
                print("\\tspawn type = interactive (4)")
            properties = []
            if value.get("KeepAlive") and not os.environ.get("FAKE_OMIT_KEEPALIVE"):
                properties.append("keepalive")
            if value.get("RunAtLoad"):
                properties.append("runatload")
            if value.get("SessionCreate"):
                properties.append("creates session")
            properties.append("inferred program")
            print(f"\\tproperties = {{' | '.join(properties)}}")
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
        # Every installer subprocess gets an explicit repository binding unless
        # a test deliberately removes it. This prevents ambient GH_REPO from
        # hiding the bound-identity path under test.
        self.env["GH_REPO"] = "danielraffel/tartci"
        self.env["TARTCI_TESTING"] = "1"
        # The fixture has no governed Runner.Worker; make that evidence
        # explicit rather than inheriting unrelated host processes.
        ps = self.fakebin / "ps-empty"
        ps.write_text("#!/bin/sh\nexit 0\n")
        ps.chmod(0o755)
        self.env["TARTCI_POOL_PS_BIN"] = str(ps)
        inventory = self.fakebin / "tart-inventory"
        inventory.write_text("#!/bin/sh\nprintf '%s\\n' '[]'\n")
        inventory.chmod(0o755)
        self.env["TARTCI_POOL_TART_BIN"] = str(inventory)
        self.support_source = self.root / "support-source"
        for name in sorted(support_manifest.filesystem_names(ROOT)):
            source = ROOT / name
            target = self.support_source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        subprocess.run(["git", "init", "-q", str(self.support_source)], check=True)
        # No background gc: a detached auto-gc after the fixture commit repacks
        # .git/objects while tearDown walks it ("Directory not empty").
        for key, value in (("gc.auto", "0"), ("maintenance.auto", "false")):
            subprocess.run(["git", "-C", str(self.support_source), "config", key, value],
                           check=True)
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

    def run_installer(
        self, *args: str, installer_root: Path = ROOT, **env: str
    ) -> subprocess.CompletedProcess[str]:
        effective = self.env.copy()
        effective.update(env)
        return subprocess.run(
            [str(installer_root / "tartci"), "fleet-macos", "install", str(self.config),
             "--support-source", str(self.support_source),
             "--support-manifest", str(self.support_manifest), *args],
            text=True, capture_output=True, check=False, env=effective,
        )

    def run_agent_wrapper_tty(self, wrapper: Path, env: dict[str, str]) -> tuple[int, str]:
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.execve(str(wrapper), [str(wrapper), "--reap", "2", "--yes"], env)
            except OSError:
                os._exit(127)
            os._exit(127)
        output = bytearray()
        sent = False
        status = None
        deadline = time.monotonic() + 20
        while status is None:
            if time.monotonic() >= deadline:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
                self.fail("installed agent-hygiene wrapper timed out")
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(master, 4096)
                    if chunk:
                        output.extend(chunk)
                        if not sent and b"terminate these process groups" in output:
                            os.write(master, b"n\n")
                            sent = True
                except OSError:
                    pass
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited == 0:
                status = None
        os.close(master)
        return os.waitstatus_to_exitcode(status), output.decode(errors="replace")

    def configure_persistent_runner(self) -> tuple[str, Path]:
        label = "actions.runner.Generous-Corp-pulp.pulp-preamble-test"
        self.config.write_text(
            self.config.read_text().replace(
                f'log_root = "{self.home}/logs"\n',
                f'log_root = "{self.home}/logs"\n'
                f'persistent_runner_labels = ["{label}"]\n',
            )
        )
        persistent = self.agents / f"{label}.plist"
        persistent.write_bytes(plistlib.dumps({
            "Label": label,
            "ProgramArguments": ["/usr/bin/true"],
            "WorkingDirectory": str(self.home),
            "StandardOutPath": str(self.home / "persistent.log"),
            "StandardErrorPath": str(self.home / "persistent.err"),
            "EnvironmentVariables": {"ACTIONS_RUNNER_SVC": "1"},
            "ProcessType": "Interactive",
            "SessionCreate": True,
            "RunAtLoad": True,
        }, sort_keys=False))
        persistent.chmod(0o644)
        return label, persistent

    def test_explicit_toml_python_path_with_spaces_is_used(self) -> None:
        interpreter_dir = self.root / "Python Tools"
        interpreter_dir.mkdir()
        interpreter = interpreter_dir / "python3"
        shutil.copy2(sys.executable, interpreter)
        interpreter.chmod(0o755)
        result = self.run_installer("--apply", TARTCI_PYTHON=str(interpreter))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_python_symlink_cycle_is_bounded(self) -> None:
        first = self.root / "python-cycle-0"
        first.symlink_to(sys.executable)
        previous = first
        for index in range(1, 18):
            link = self.root / f"python-cycle-{index}"
            link.symlink_to(previous)
            previous = link
        first.unlink()
        first.symlink_to(previous)
        result = self.run_installer("--preflight", TARTCI_PYTHON=str(first))
        self.assertEqual(result.returncode, 127)
        self.assertIn("symlink cycle", result.stderr)

    def test_install_persists_pinned_interpreter_for_launcher_and_ghapp(self) -> None:
        interpreter = self.root / "pinned-python3"
        shutil.copy2(sys.executable, interpreter)
        interpreter.chmod(0o755)
        result = self.run_installer("--apply", TARTCI_PYTHON=str(interpreter))
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = list(self.agents.glob("*macos-fleet*.plist"))
        self.assertEqual(len(installed), 1)
        env = plistlib.loads(installed[0].read_bytes())["EnvironmentVariables"]
        expected = os.path.realpath(interpreter)
        self.assertEqual(env["TARTCI_PYTHON"], expected)
        self.assertEqual(env["SHIPYARD_GHAPP_PYTHON_BINARY"], expected)
        self.assertTrue(env["PATH"].startswith(str(Path(expected).parent) + ":"))

    def set_profile_python(self, path: Path) -> None:
        self.config.write_text(self.config.read_text().replace(
            f'log_root = "{self.home}/logs"\n',
            f'log_root = "{self.home}/logs"\npython = "{path}"\n'))

    def test_profile_host_python_wins_and_is_pinned_for_every_agent(self) -> None:
        declared = self.root / "declared-python3.12"
        shutil.copy2(sys.executable, declared)
        declared.chmod(0o755)
        other = self.root / "env-python3"
        shutil.copy2(sys.executable, other)
        other.chmod(0o755)
        self.set_profile_python(declared)
        refused = self.home / ".tartci/state/interpreter-refused"
        refused.parent.mkdir(parents=True, exist_ok=True)
        refused.write_text("2026-10-15T09:00:00Z\tcom.danielraffel.tartci.reclaim\tno pin\n")
        result = self.run_installer("--apply", TARTCI_PYTHON=str(other))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uses the profile's host.python", result.stderr)
        expected = os.path.realpath(declared)
        installed = list(self.agents.glob("*macos-fleet*.plist"))
        env = plistlib.loads(installed[0].read_bytes())["EnvironmentVariables"]
        self.assertEqual(env["TARTCI_PYTHON"], expected)
        pin = self.home / ".config/tartci/python.pin"
        self.assertEqual(pin.read_text().strip(), expected)
        self.assertFalse((self.home / ".tartci/state/interpreter-refused").exists())
        self.assertIn(f"pinned interpreter {expected}", result.stdout)

    def pin_mise(self, binary: Path, sha256: str) -> None:
        self.config.write_text(self.config.read_text()
                               + f'\n[mise]\nversion = "2026.10.8"\npath = "{binary}"\nsha256 = "{sha256}"\n')

    def mise_binary(self) -> tuple[Path, str]:
        binary = self.root / "opt/mise-2026.10.8/bin/mise"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\necho '2026.10.8 macos-arm64 (2026-10-13)'\n")
        binary.chmod(0o755)
        import hashlib
        return binary, hashlib.sha256(binary.read_bytes()).hexdigest()

    def test_a_pinned_mise_is_verified_and_recorded(self) -> None:
        binary, digest = self.mise_binary()
        self.pin_mise(binary, digest)
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.home / ".config/tartci/mise.pin").read_text())
        self.assertEqual((record["path"], record["sha256"]), (str(binary), digest))

    def test_a_different_mise_binary_refuses_the_install_before_any_agent(self) -> None:
        binary, digest = self.mise_binary()
        self.pin_mise(binary, "0" * 64)
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(f"has sha256 {digest}, but the profile pins {'0' * 64}", result.stderr)
        self.assertEqual([], list(self.agents.glob("*macos-fleet*.plist")))
        self.assertFalse((self.home / ".config/tartci/mise.pin").exists())

    def fake_rustup(self, records: bool) -> Path:
        rustup = self.root / ".cargo/bin/rustup"
        rustup.parent.mkdir(parents=True, exist_ok=True)
        body = ('printf \'version = "12"\\nauto_self_update = "%s"\\n\' "$3" > "$RUSTUP_HOME/settings.toml"\n'
                if records else "")
        rustup.write_text(f"#!/bin/sh\n{body}")
        rustup.chmod(0o755)
        self.config.write_text(self.config.read_text()
                               + f'\n[rustup]\npath = "{rustup}"\nauto_self_update = "disable"\n')
        (self.root / "rustup-home").mkdir(exist_ok=True)
        return self.root / "rustup-home"

    def test_rustup_is_set_to_never_self_update(self) -> None:
        home = self.fake_rustup(records=True)
        result = self.run_installer("--apply", RUSTUP_HOME=str(home))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('auto_self_update = "disable"', (home / "settings.toml").read_text())

    def test_a_rustup_that_keeps_self_update_refuses_the_install(self) -> None:
        home = self.fake_rustup(records=False)
        result = self.run_installer("--apply", RUSTUP_HOME=str(home))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("expected 'disable'", result.stderr)
        self.assertEqual([], list(self.agents.glob("*macos-fleet*.plist")))

    def preflight(self, rc: int) -> Path:
        script = self.root / f"preflight-{rc}"
        script.write_text("#!/bin/sh\n"
                          f"echo \"preflight said $2 cannot write the store\" >&2\nexit {rc}\n")
        script.chmod(0o755)
        return script

    def interpreter(self, name: str) -> Path:
        path = self.root / name
        shutil.copy2(sys.executable, path)
        path.chmod(0o755)
        return path

    def lane_python(self) -> str:
        installed = list(self.agents.glob("*macos-fleet*.plist"))
        return plistlib.loads(installed[0].read_bytes())["EnvironmentVariables"]["TARTCI_PYTHON"]

    def test_a_new_interpreter_that_fails_the_preflight_is_not_switched_to(self) -> None:
        # m5s, 2026-10-15: the lanes moved to a binary with no grant for the
        # external-volume store, and the host could not come back on.
        old, new = self.interpreter("old-python3"), self.interpreter("new-python3")
        first = self.run_installer("--apply", TARTCI_PYTHON=str(old))
        self.assertEqual(first.returncode, 0, first.stderr)
        result = self.run_installer("--apply", TARTCI_PYTHON=str(new),
                                    TARTCI_INTERPRETER_PREFLIGHT=str(self.preflight(1)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"keeps the lane interpreter {os.path.realpath(old)}", result.stderr)
        self.assertEqual(self.lane_python(), os.path.realpath(old))
        record = (self.home / ".tartci/state/interpreter-switch-refused").read_text()
        stamp, tried, kept, reason = record.rstrip("\n").split("\t")
        self.assertEqual((tried, kept), (os.path.realpath(new), os.path.realpath(old)))
        self.assertIn("cannot write the store", reason)

    def test_a_new_interpreter_that_passes_the_preflight_is_switched_to(self) -> None:
        # Control, same instrument: the same switch with a passing preflight.
        old, new = self.interpreter("old-python3"), self.interpreter("new-python3")
        self.assertEqual(self.run_installer("--apply", TARTCI_PYTHON=str(old)).returncode, 0)
        refused = self.home / ".tartci/state/interpreter-switch-refused"
        refused.parent.mkdir(parents=True, exist_ok=True)
        refused.write_text("2026-10-15T13:59:30Z\tx\ty\tz\n")
        result = self.run_installer("--apply", TARTCI_PYTHON=str(new),
                                    TARTCI_INTERPRETER_PREFLIGHT=str(self.preflight(0)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lane_python(), os.path.realpath(new))
        self.assertFalse(refused.exists())

    def test_the_real_preflight_refuses_a_new_interpreter_that_cannot_write_the_store(self) -> None:
        # No TARTCI_INTERPRETER_PREFLIGHT: the installer runs
        # scripts/interpreter_preflight.py, which starts the NEW binary as a
        # launchd job (the fake launchctl runs it) that writes into tart_home.
        old, new = self.interpreter("old-python3"), self.interpreter("new-python3")
        self.assertEqual(self.run_installer("--apply", TARTCI_PYTHON=str(old)).returncode, 0)
        store = self.home / "VMs"
        store.mkdir(exist_ok=True)
        store.chmod(0o500)
        try:
            result = self.run_installer("--apply", TARTCI_PYTHON=str(new))
        finally:
            store.chmod(0o755)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"keeps the lane interpreter {os.path.realpath(old)}", result.stderr)
        self.assertIn(f"could not use {store}", result.stderr)
        self.assertEqual(self.lane_python(), os.path.realpath(old))
        self.assertIn("kickstart gui/", self.calls.read_text())
        self.assertTrue((self.home / ".tartci/state/interpreter-switch-refused").exists())

    def test_the_real_preflight_switches_to_a_new_interpreter_that_can_write_the_store(self) -> None:
        # Control, same instrument: a writable store passes the real preflight.
        old, new = self.interpreter("old-python3"), self.interpreter("new-python3")
        self.assertEqual(self.run_installer("--apply", TARTCI_PYTHON=str(old)).returncode, 0)
        (self.home / "VMs").mkdir(exist_ok=True)
        result = self.run_installer("--apply", TARTCI_PYTHON=str(new))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lane_python(), os.path.realpath(new))
        self.assertIn("interpreter-preflight", self.calls.read_text())
        self.assertEqual(list((self.home / "VMs").iterdir()), [])

    def recording_preflight(self) -> tuple[Path, Path]:
        calls = self.root / "preflight.calls"
        script = self.root / "preflight-recording"
        script.write_text(f"#!/bin/sh\necho \"$2\" >>{calls}\nexit 0\n")
        script.chmod(0o755)
        return script, calls

    def pin_to(self, python: Path) -> Path:
        pin = self.home / ".config/tartci/python.pin"
        pin.write_text(os.path.realpath(python) + "\n")
        return pin

    def test_a_rollback_interpreter_wins_over_the_profiles_host_python(self) -> None:
        # The snapshot profile can name the switch the preflight refused; the
        # rollback must reinstall what the previous lanes ran. Self-update
        # pins that interpreter before the rollback install.
        old, new = self.interpreter("old-python3"), self.interpreter("new-python3")
        self.set_profile_python(new)
        self.pin_to(old)
        result = self.run_installer("--apply", TARTCI_ROLLBACK_PYTHON=os.path.realpath(old),
                                    TARTCI_INTERPRETER_PREFLIGHT=str(self.preflight(1)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(rollback) uses the previous lanes' interpreter", result.stderr)
        self.assertEqual(self.lane_python(), os.path.realpath(old))
        pin = (self.home / ".config/tartci/python.pin").read_text().strip()
        self.assertEqual(pin, os.path.realpath(old))
        # Control, same profile: without the rollback, host.python wins.
        result = self.run_installer("--apply", TARTCI_INTERPRETER_PREFLIGHT=str(self.preflight(0)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lane_python(), os.path.realpath(new))

    def test_a_rollback_is_not_preflighted_but_a_switch_is(self) -> None:
        # The lanes run new; the rollback restores old, pinned by self-update.
        old, new = self.interpreter("old-python3"), self.interpreter("new-python3")
        self.assertEqual(self.run_installer("--apply", TARTCI_PYTHON=str(new),
                                            TARTCI_INTERPRETER_PREFLIGHT=str(self.preflight(0))
                                            ).returncode, 0)
        stub, calls = self.recording_preflight()
        self.pin_to(old)
        result = self.run_installer("--apply", TARTCI_ROLLBACK_PYTHON=os.path.realpath(old),
                                    TARTCI_INTERPRETER_PREFLIGHT=str(stub))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lane_python(), os.path.realpath(old))
        self.assertFalse(calls.exists(), "a rollback must not run the preflight")
        # Control, same stub: switching back to new is a switch, so it runs.
        result = self.run_installer("--apply", TARTCI_PYTHON=str(new),
                                    TARTCI_INTERPRETER_PREFLIGHT=str(stub))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text().split(), [os.path.realpath(new)])

    def test_a_rollback_interpreter_a_lane_runs_is_honoured(self) -> None:
        old, new = self.interpreter("old-python3"), self.interpreter("new-python3")
        self.assertEqual(self.run_installer("--apply", TARTCI_PYTHON=str(old)).returncode, 0)
        (self.home / ".config/tartci/python.pin").unlink()
        self.set_profile_python(new)
        result = self.run_installer("--apply", TARTCI_ROLLBACK_PYTHON=os.path.realpath(old))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lane_python(), os.path.realpath(old))

    def test_a_stray_rollback_interpreter_is_refused(self) -> None:
        # Neither the pin nor any lane names it: an exported variable, not a
        # rollback, so it may not override host.python or skip the preflight.
        old, stray = self.interpreter("old-python3"), self.interpreter("stray-python3")
        self.assertEqual(self.run_installer("--apply", TARTCI_PYTHON=str(old)).returncode, 0)
        result = self.run_installer("--apply", TARTCI_ROLLBACK_PYTHON=os.path.realpath(stray))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("is not a rollback interpreter", result.stderr)
        self.assertEqual(self.lane_python(), os.path.realpath(old))

    def test_a_relative_rollback_interpreter_is_refused(self) -> None:
        result = self.run_installer("--apply", TARTCI_ROLLBACK_PYTHON="python3")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("TARTCI_ROLLBACK_PYTHON must be an absolute path", result.stderr)
        self.assertEqual(list(self.agents.glob("*macos-fleet*.plist")), [])

    def test_a_missing_profile_python_refuses_without_falling_back(self) -> None:
        self.set_profile_python(self.root / "gone" / "python3.12")
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 127, result.stdout + result.stderr)
        self.assertIn("host.python is not an executable file", result.stderr)
        self.assertEqual(list(self.agents.glob("*macos-fleet*.plist")), [])
        self.assertFalse((self.home / ".config/tartci/python.pin").exists())

    def test_slow_pinned_interpreter_fails_fast_with_path(self) -> None:
        interpreter = self.root / "slow-python"
        interpreter.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            case "$*" in
              *api.github.com*) sleep 5 ;;
            esac
            exec {sys.executable} "$@"
        """))
        interpreter.chmod(0o755)
        result = self.run_installer(TARTCI_PYTHON=str(interpreter), TARTCI_TESTING="0")
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertIn("timed out after 3s", result.stderr)
        self.assertIn(str(interpreter), result.stderr)

    def test_blocked_pinned_interpreter_fails_with_path(self) -> None:
        interpreter = self.root / "blocked-python"
        interpreter.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            case "$*" in
              *api.github.com*) exit 1 ;;
            esac
            exec {sys.executable} "$@"
        """))
        interpreter.chmod(0o755)
        result = self.run_installer(TARTCI_PYTHON=str(interpreter), TARTCI_TESTING="0")
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertIn("interpreter GitHub probe failed", result.stderr)
        self.assertIn(str(interpreter), result.stderr)

    def test_pinned_interpreter_probe_positive_control(self) -> None:
        interpreter = self.root / "working-python"
        interpreter.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            case "$*" in
              *api.github.com*) exit 0 ;;
            esac
            exec {sys.executable} "$@"
        """))
        interpreter.chmod(0o755)
        result = self.run_installer(
            "--apply", TARTCI_PYTHON=str(interpreter), TARTCI_TESTING="0"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installed_generation_refuses_itself_as_support_source(self) -> None:
        # The installed tartci lives in a read-only generation, not a git
        # checkout; defaulting the support source to it used to fail with a
        # misleading "support member failed verification".
        staged = support_manifest.stage_install(
            self.support_source, self.support_manifest, self.root / "generations"
        )
        generation = Path(str(staged["root"]))
        result = subprocess.run(
            [str(generation / "tartci"), "fleet-macos", "install", str(self.config)],
            text=True, capture_output=True, check=False, env=self.env,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("installed TartCI generation, not a git checkout", result.stderr)
        self.assertIn("self-update", result.stderr)
        self.assertNotIn("failed verification", result.stderr)

    def test_dry_run_does_not_install_or_retire(self) -> None:
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("action=dry-run", result.stdout)
        self.assertTrue(self.legacy.exists())
        self.assertEqual([], list(self.agents.glob("*macos-fleet*.plist")))
        self.assertFalse((self.home / ".config/tartci/macos-fleet-install.json").exists())

    def test_dry_run_authenticates_before_exit_with_home_and_rendered_path(self) -> None:
        result = self.run_installer(
            FAKE_GHAPP_REQUIRE_HOME="1",
            FAKE_GHAPP_EXPECTED_PATH=str((self.bin / "ghapp").resolve()),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("action=dry-run", result.stdout)

    def test_installed_agent_hygiene_wrapper_keeps_human_only_gate(self) -> None:
        if shutil.which("zsh") is None:
            self.skipTest("agent-hygiene is a macOS zsh entry point")
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        wrapper = self.bin / "agent-hygiene"
        self.assertTrue(wrapper.is_file())
        snapshot = self.root / "agent-ps"
        snapshot.write_text("999991 1 00-00:01 10 /Users/test/.local/bin/claude\n")
        transcripts = self.root / "agent-transcripts"
        transcripts.mkdir()
        transcript = transcripts / "session-999991.jsonl"
        transcript.write_text("old")
        old = time.time() - 3 * 86400
        os.utime(transcript, (old, old))
        env = self.env.copy()
        env.update(AH_PS_SNAPSHOT_FILE=str(snapshot), AH_TRANSCRIPT_DIRS=str(transcripts),
                   AH_STALE_DAYS="2", AH_PROC_PCT="1000", AH_SWAP_PCT="1000")
        code, output = self.run_agent_wrapper_tty(wrapper, env)
        self.assertEqual(code, 0, output)
        self.assertIn("--yes is ignored", output)
        self.assertIn("terminate these process groups", output)
        self.assertNotIn("done; victims log", output)

    def test_dry_run_bound_legacy_repository_resolves_and_reports_slug(self) -> None:
        result = self.run_installer(GH_REPO="danielraffel/tartci")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("repository=danielraffel/tartci", result.stdout)
        self.assertIn("action=dry-run", result.stdout)

    def test_dry_run_bound_default_repository_reports_auth_failure(self) -> None:
        result = self.run_installer(GH_REPO="Generous-Corp/tartci")
        self.assertEqual(result.returncode, 3)
        self.assertIn("could not authenticate", result.stderr)
        self.assertNotIn("unbound variable", result.stderr)

    def test_mutant_removing_bound_assignment_fails_bound_fixture(self) -> None:
        mutant_root = self.root / "mutant-installer"
        shutil.copytree(
            ROOT,
            mutant_root,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        path = mutant_root / "scripts/install_macos_fleet.sh"
        source = path.read_text()
        assignment = (
            '"$DEFAULT_TARTCI_REPOSITORY"|"$LEGACY_TARTCI_REPOSITORY") '
            'TARTCI_REPOSITORY="$REQUESTED_TARTCI_REPOSITORY" ;;'
        )
        self.assertIn(assignment, source)
        path.write_text(source.replace(assignment, assignment.split(") ", 1)[0] + ") ;;", 1))
        result = self.run_installer(installer_root=mutant_root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TARTCI_REPOSITORY: unbound variable", result.stderr)

    def test_dry_run_resolves_legacy_repository_when_gh_repo_is_unset(self) -> None:
        self.env.pop("GH_REPO", None)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("action=dry-run", result.stdout)

    def test_dry_run_reports_auth_failure_instead_of_hiding_it(self) -> None:
        result = self.run_installer(FAKE_AUTH_DENY="1")
        self.assertEqual(result.returncode, 3)
        self.assertIn("could not authenticate", result.stderr)
        self.assertNotIn("action=dry-run", result.stdout)

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
        result = self.run_installer("--apply", TARTCI_PYTHON=str(python))
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
        result = self.run_installer("--apply", TARTCI_PYTHON=str(python))
        self.assertEqual(1, result.returncode)
        self.assertEqual(prior, (self.bin / "tartci").read_bytes())
        self.assertFalse(
            (self.home / ".config/tartci/macos-fleet-install.json").exists()
        )
        self.assertEqual([], list(self.bin.glob(".tartci-wrapper.*")))

    def test_installer_prefers_versioned_python_before_unversioned_python3(self) -> None:
        for name in ("python3.12", "python3.11", "python3"):
            interpreter = self.fakebin / name
            interpreter.write_text(textwrap.dedent(f"""\
                #!/bin/sh
                exec {sys.executable} "$@"
            """))
            interpreter.chmod(0o755)
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = list(self.agents.glob("*macos-fleet*.plist"))
        self.assertEqual(len(installed), 1)
        environment = plistlib.loads(installed[0].read_bytes())["EnvironmentVariables"]
        self.assertEqual(Path(environment["TARTCI_PYTHON"]).name, "python3.12")

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
            [str(self.fakebin / "launchctl"), "bootstrap", f"gui/{self.uid}", str(target)],
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

    def test_pool_on_restores_only_profile_receipted_persistent_runner(self) -> None:
        label = "actions.runner.Generous-Corp-pulp.pulp-preamble-test"
        self.config.write_text(
            self.config.read_text().replace(
                f'log_root = "{self.home}/logs"\n',
                f'log_root = "{self.home}/logs"\n'
                f'persistent_runner_labels = ["{label}"]\n',
            )
        )
        persistent = self.agents / f"{label}.plist"
        persistent.write_bytes(plistlib.dumps({
            "Label": label,
            "ProgramArguments": ["/usr/bin/true"],
            "WorkingDirectory": str(self.home),
            "StandardOutPath": str(self.home / "persistent.log"),
            "StandardErrorPath": str(self.home / "persistent.err"),
            "EnvironmentVariables": {"ACTIONS_RUNNER_SVC": "1"},
            "ProcessType": "Interactive",
            "SessionCreate": True,
            "RunAtLoad": True,
        }, sort_keys=False))
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        receipt = json.loads(
            (self.home / ".config/tartci/macos-fleet-install.json").read_text()
        )
        self.assertEqual(4, receipt["schema"])
        self.assertEqual([persistent.name], list(receipt["persistent_plists"]))
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
        self.assertIn(f"bootstrap gui/{self.uid} {persistent}", calls)
        self.assertIn(f"kickstart gui/{self.uid}/{label}", calls)
        loaded = json.loads(
            (self.home / ".config/tartci/macos-fleet-loaded.json").read_text()
        )
        self.assertIn(persistent.name, loaded["loaded_services"])

    def test_installer_refuses_loaded_declared_persistent_runner(self) -> None:
        label, _ = self.configure_persistent_runner()
        result = self.run_installer(
            "--apply", FAKE_LOADED_LABEL=label,
        )
        self.assertEqual(3, result.returncode, result.stderr)
        self.assertIn("declared persistent LaunchAgent is loaded", result.stderr)
        self.assertFalse(
            (self.home / ".config/tartci/macos-fleet-install.json").exists()
        )
        self.assertEqual([], list(self.agents.glob("*macos-fleet*.plist")))

    def test_installer_refuses_unsafe_persistent_runner_mode(self) -> None:
        _, persistent = self.configure_persistent_runner()
        persistent.chmod(0o666)
        result = self.run_installer("--apply")
        self.assertEqual(3, result.returncode, result.stderr)
        self.assertIn("not group/world-writable", result.stderr)
        self.assertFalse(
            (self.home / ".config/tartci/macos-fleet-install.json").exists()
        )

    def test_pool_on_kickstart_failure_rolls_back_persistent_runner(self) -> None:
        label, persistent = self.configure_persistent_runner()
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.calls.write_text("")
        result = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env={**self.env, "FAKE_FAIL_KICKSTART_LABEL": label},
        )
        self.assertEqual(7, result.returncode, result.stderr)
        calls = self.calls.read_text()
        self.assertIn(f"bootstrap gui/{self.uid} {persistent}", calls)
        self.assertIn(f"kickstart gui/{self.uid}/{label}", calls)
        self.assertIn(f"disable gui/{self.uid}/{label}", calls)
        self.assertIn(f"bootout gui/{self.uid}/{label}", calls)
        state = json.loads(self.launchctl_state.read_text())
        self.assertNotIn(label, state)
        self.assertEqual(
            "off\n", (self.home / ".config/tartci/pool-state").read_text()
        )
        self.assertEqual(
            "0\n",
            (self.home / ".config/tartci/native-build-participation").read_text(),
        )

    def test_pool_on_bootstrap_failure_stays_fail_closed(self) -> None:
        label, _ = self.configure_persistent_runner()
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.calls.write_text("")
        result = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env={**self.env, "FAKE_FAIL_BOOTSTRAP_LABEL": label},
        )
        self.assertEqual(7, result.returncode, result.stderr)
        calls = self.calls.read_text()
        self.assertIn(f"bootstrap gui/{self.uid} {self.agents / (label + '.plist')}", calls)
        self.assertIn(f"disable gui/{self.uid}/{label}", calls)
        self.assertIn(f"bootout gui/{self.uid}/{label}", calls)
        self.assertNotIn(label, json.loads(self.launchctl_state.read_text()))

    def test_pool_on_rejects_extra_persistent_environment_and_rolls_back(self) -> None:
        label, _ = self.configure_persistent_runner()
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.calls.write_text("")
        result = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env={**self.env, "FAKE_OBSOLETE_ENV": "UNRECEIPTED_EXTRA"},
        )
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertIn(f"bootout gui/{self.uid}/{label}", self.calls.read_text())

    def test_pool_on_reports_unproven_rollback_and_closes_admission(self) -> None:
        label, _ = self.configure_persistent_runner()
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.calls.write_text("")
        result = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env={
                **self.env,
                "FAKE_FAIL_KICKSTART_LABEL": label,
                "FAKE_FAIL_BOOTOUT_LABEL": label,
            },
        )
        self.assertEqual(10, result.returncode, result.stderr)
        self.assertIn("rollback could not prove", result.stderr)
        self.assertIn(label, json.loads(self.launchctl_state.read_text()))
        self.assertEqual(
            "off\n", (self.home / ".config/tartci/pool-state").read_text()
        )
        self.assertEqual(
            "0\n",
            (self.home / ".config/tartci/native-build-participation").read_text(),
        )

    def test_pool_on_refuses_preloaded_runner_when_enable_cannot_be_proven(self) -> None:
        label, persistent = self.configure_persistent_runner()
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        subprocess.run(
            [str(self.fakebin / "launchctl"), "bootstrap", f"gui/{self.uid}", str(persistent)],
            text=True, capture_output=True, check=True, env=self.env,
        )
        subprocess.run(
            [str(self.fakebin / "launchctl"), "disable", f"gui/{self.uid}/{label}"],
            text=True, capture_output=True, check=True, env=self.env,
        )
        self.calls.write_text("")
        result = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env={**self.env, "FAKE_FAIL_ENABLE_LABEL": label},
        )
        self.assertEqual(7, result.returncode, result.stderr)
        calls = self.calls.read_text()
        self.assertNotIn(f"kickstart gui/{self.uid}/{label}", calls)
        self.assertIn(f"bootout gui/{self.uid}/{label}", calls)
        self.assertNotIn(label, json.loads(self.launchctl_state.read_text()))

    def test_pool_on_refuses_drifted_profile_receipted_persistent_runner(self) -> None:
        label = "actions.runner.Generous-Corp-pulp.pulp-preamble-test"
        self.config.write_text(
            self.config.read_text().replace(
                f'log_root = "{self.home}/logs"\n',
                f'log_root = "{self.home}/logs"\n'
                f'persistent_runner_labels = ["{label}"]\n',
            )
        )
        persistent = self.agents / f"{label}.plist"
        persistent.write_bytes(plistlib.dumps({
            "Label": label,
            "ProgramArguments": ["/usr/bin/true"],
            "WorkingDirectory": str(self.home),
            "StandardOutPath": str(self.home / "persistent.log"),
            "StandardErrorPath": str(self.home / "persistent.err"),
            "EnvironmentVariables": {"ACTIONS_RUNNER_SVC": "1"},
            "ProcessType": "Interactive",
            "SessionCreate": True,
            "RunAtLoad": True,
        }, sort_keys=False))
        installed = self.run_installer("--apply")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        value = plistlib.loads(persistent.read_bytes())
        value["ProgramArguments"].append("--drift")
        persistent.write_bytes(plistlib.dumps(value, sort_keys=False))
        self.calls.write_text("")
        result = subprocess.run(
            [str(self.bin / "tartci"), "pool", "on"],
            text=True,
            capture_output=True,
            check=False,
            env=self.env,
        )
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertIn("no valid receipt", result.stderr)
        self.assertNotIn("bootstrap", self.calls.read_text())

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
        self.assertEqual("off\n", (state / "pool-state").read_text())
        self.assertEqual("0\n", (state / "native-build-participation").read_text())

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
        # The fake launchctl reports pid 4242 for every loaded lane; give each
        # lane an idle heartbeat from that supervisor so pool off's mid-job
        # probe reads it idle, as a real waiting supervisor would.
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        agents_dir = self.home / "Library" / "LaunchAgents"
        for plist in agents_dir.glob("com.danielraffel.tartci.tart-runner-macos-fleet.*.plist"):
            state_dir = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["TARTCI_STATE_DIR"]
            Path(state_dir).mkdir(parents=True, exist_ok=True)
            (Path(state_dir) / "lane.state.json").write_text(json.dumps(
                {"ts": now, "phase": "waiting", "supervisor_pid": "4242"}))
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


    def install_and_seal(self, *, commit: str, manifest_sha256: str) -> dict:
        """Install a real generation, then seal a launcher bundle around a cohort.

        A launch_helper lane renders ProgramArguments naming the signed bundle,
        never the staged generation, so plist-versus-render equality cannot tell
        an effective install from one whose new generation nothing execs. Only
        the Developer ID signature check is substituted here, because a test
        cannot mint one.
        """
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.home / ".config/tartci/macos-fleet-install.json").read_text()
        )
        support = receipt["support"]
        bundle = self.home / ".local/libexec/TartCILauncher.app"
        executable = bundle / "Contents/MacOS/tartci-launcher"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        sealed = bundle / fleet.SEALED_SUPPORT
        sealed.mkdir(parents=True, exist_ok=True)
        sealed_launch = sealed / support_manifest.LAUNCH_NAME
        sealed_launch.write_text("#!/bin/sh\nexit 0\n")
        sealed_launch.chmod(0o555)
        (bundle / fleet.SEALED_METADATA).write_text(json.dumps({
            "schema": 1,
            "source_commit": commit,
            "support_manifest_sha256": manifest_sha256,
            "profile_policy_sha256": "d" * 64,
            "tart_home": "/Volumes/Example/VMs",
        }, sort_keys=True))
        approval = self.home / ".config/tartci/m3-launcher-approved.sha256"
        approval.write_text("e" * 64 + "\n")
        approval.chmod(0o600)
        helper_config = self.root / "fleet-launch-helper.toml"
        helper_config.write_text(
            self.config.read_text()
            .replace(f'tart_home = "{self.home}/VMs"', 'tart_home = "/Volumes/Example/VMs"')
            .replace("[github_app]", textwrap.dedent(f"""\
                [launch_helper]
                path = "{bundle}"
                approval_sha256_path = "{approval}"
                identifier = "com.danielraffel.tartci.launcher"
                team_id = "95CX6P84C4"

                [github_app]"""))
        )
        installed_env = plistlib.loads(next(self.agents.glob("*macos-fleet*.plist")).read_bytes())["EnvironmentVariables"]
        for installed in self.agents.glob("*macos-fleet*.plist"):
            installed.unlink()
        data = fleet.load(helper_config)
        previous_render_python = os.environ.get("TARTCI_RENDER_PYTHON")
        os.environ["TARTCI_RENDER_PYTHON"] = installed_env["TARTCI_PYTHON"]
        try:
            rendered = fleet.rendered_plists(
                data, launch_entrypoint=Path(support["launch_entrypoint"]["path"])
            )
        finally:
            if previous_render_python is None:
                os.environ.pop("TARTCI_RENDER_PYTHON", None)
            else:
                os.environ["TARTCI_RENDER_PYTHON"] = previous_render_python
        for name, body in rendered.items():
            target = self.agents / name
            target.write_bytes(body)
            target.chmod(0o644)
            self.assertEqual(
                plistlib.loads(body)["ProgramArguments"][0], str(executable)
            )
        lanes = {}
        for body in rendered.values():
            env = plistlib.loads(body)["EnvironmentVariables"]
            lanes[env["TARTCI_QUEUE_LANE_ID"]] = {"environment": dict(sorted(env.items()))}
        (bundle / "Contents/Resources/lanes.json").write_text(
            json.dumps({"schema": 1, "lanes": lanes})
        )
        return {
            "config": helper_config,
            "support": support,
            "sealed_launch": sealed_launch,
            "output": self.root / "receipt.json",
        }

    def test_real_installer_checks_build_launcher_lanes_pin(self) -> None:
        fixture = self.install_and_seal(commit="a" * 40, manifest_sha256="f" * 64)
        self.config = fixture["config"]
        lanes_path = self.home / ".local/libexec/TartCILauncher.app/Contents/Resources/lanes.json"
        lanes = json.loads(lanes_path.read_text())
        next(iter(lanes["lanes"].values()))["environment"]["TARTCI_PYTHON"] = "/wrong/python"
        lanes_path.write_text(json.dumps(lanes))
        result = self.run_installer(
            "--apply", "--launch-helper-source",
            str(self.home / ".local/libexec/TartCILauncher.app"),
        )
        self.assertIn("launch helper lanes are missing pinned interpreter", result.stderr)

    def write_receipt(self, fixture: dict) -> None:
        support = fixture["support"]
        record = {
            "schema": 1, "path": str(self.home / ".local/libexec/TartCILauncher.app"),
            "source_commit": support["source_commit"],
        }
        with mock.patch.object(
            macos_launcher_identity, "verify", return_value=record
        ), mock.patch.object(
            macos_launcher_identity, "profile_policy_digest", return_value="d" * 64
        ):
            fleet.write_receipt(
                fixture["config"], self.agents, fixture["output"],
                Path(support["root"]),
                Path(support["manifest_path"]),
                Path(support["entrypoint"]["path"]),
                Path(support["entrypoint"]["path"]),
                Path(support["launch_entrypoint"]["path"]),
                support["source_commit"],
            )

    def test_launcher_sealed_around_a_stale_cohort_fails_the_receipt(self) -> None:
        fixture = self.install_and_seal(commit="a" * 40, manifest_sha256="f" * 64)
        with self.assertRaises(ValueError) as caught:
            self.write_receipt(fixture)
        message = str(caught.exception)
        self.assertIn("install_ineffective", message)
        self.assertIn(str(fixture["sealed_launch"]), message)
        self.assertIn(fixture["support"]["launch_entrypoint"]["path"], message)
        self.assertFalse(fixture["output"].exists())

    def test_launcher_sealed_around_the_installed_cohort_writes_the_receipt(self) -> None:
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        support = json.loads(
            (self.home / ".config/tartci/macos-fleet-install.json").read_text()
        )["support"]
        fixture = self.install_and_seal(
            commit=support["source_commit"],
            manifest_sha256=support["manifest_sha256"],
        )
        self.write_receipt(fixture)
        written = json.loads(fixture["output"].read_text())
        self.assertEqual(written["support"]["root"], support["root"])


    def test_verifier_rejects_a_launcher_resealed_around_a_stale_cohort(self) -> None:
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        support = json.loads(
            (self.home / ".config/tartci/macos-fleet-install.json").read_text()
        )["support"]
        fixture = self.install_and_seal(
            commit=support["source_commit"],
            manifest_sha256=support["manifest_sha256"],
        )
        self.write_receipt(fixture)
        bundle = self.home / ".local/libexec/TartCILauncher.app"
        (bundle / fleet.SEALED_METADATA).write_text(json.dumps({
            "schema": 1,
            "source_commit": "a" * 40,
            "support_manifest_sha256": "f" * 64,
            "profile_policy_sha256": "d" * 64,
            "tart_home": "/Volumes/Example/VMs",
        }, sort_keys=True))
        record = json.loads(fixture["output"].read_text())["launch_helper"]
        with mock.patch.object(
            macos_launcher_identity, "verify", return_value=record
        ), mock.patch.object(
            macos_launcher_identity, "profile_policy_digest", return_value="d" * 64
        ):
            with self.assertRaisesRegex(ValueError, "install_ineffective"):
                fleet.verify_receipt(
                    fixture["output"], fixture["config"], self.agents,
                    Path(fixture["support"]["root"]),
                )

    # Launcher v2 (scripts/launcher_layout.py, scripts/launch_set.py).

    V2_SIGNING = ("--launch-set-identity", "Developer ID Application: Example",
                  "--launch-set-keychain", "/nonexistent/signing.keychain-db")

    def opt_in(self, mode: int = 0o600) -> Path:
        path = self.home / ".config/tartci/launcher-v2.enabled"
        path.write_text("")
        path.chmod(mode)
        return path

    def mark_receipt(self, launch_helper: dict) -> None:
        receipt_path = self.home / ".config/tartci/macos-fleet-install.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["launch_helper"] = launch_helper
        receipt_path.write_text(json.dumps(receipt))

    def test_a_sealed_host_without_the_opt_in_plans_v1_unchanged(self) -> None:
        fixture = self.install_and_seal(commit="a" * 40, manifest_sha256="f" * 64)
        self.config = fixture["config"]
        self.mark_receipt({"schema": 1})
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("launcher_layout=v1 installed=v1", result.stdout)
        self.assertNotIn("launcher_change", result.stdout)

    def test_the_opt_in_plans_v2_and_names_the_launcher_change(self) -> None:
        fixture = self.install_and_seal(commit="a" * 40, manifest_sha256="f" * 64)
        self.config = fixture["config"]
        self.mark_receipt({"schema": 1})
        self.opt_in()
        result = self.run_installer(*self.V2_SIGNING)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("launcher_layout=v2 installed=v1", result.stdout)
        self.assertIn("launcher_change=v1->v2", result.stdout)
        self.assertIn("approval digest and the Removable Volumes grant", result.stdout)

    def test_v2_refuses_in_the_plan_without_the_signing_keychain(self) -> None:
        fixture = self.install_and_seal(commit="a" * 40, manifest_sha256="f" * 64)
        self.config = fixture["config"]
        self.opt_in()
        result = self.run_installer()
        self.assertEqual(result.returncode, 2)
        self.assertIn("--launch-set-keychain", result.stderr)
        self.assertNotIn("install plan", result.stdout)

    def test_signing_options_are_refused_for_a_v1_install(self) -> None:
        fixture = self.install_and_seal(commit="a" * 40, manifest_sha256="f" * 64)
        self.config = fixture["config"]
        result = self.run_installer(*self.V2_SIGNING)
        self.assertEqual(result.returncode, 2)
        self.assertIn("only for launcher v2", result.stderr)

    def test_an_untrusted_opt_in_refuses_the_install(self) -> None:
        fixture = self.install_and_seal(commit="a" * 40, manifest_sha256="f" * 64)
        self.config = fixture["config"]
        self.opt_in(0o666)
        result = self.run_installer(*self.V2_SIGNING)
        self.assertEqual(result.returncode, 3)
        self.assertIn("group- or world-writable", result.stderr)

    def test_a_v2_host_that_lost_its_opt_in_refuses_with_the_downgrade_step(self) -> None:
        fixture = self.install_and_seal(commit="a" * 40, manifest_sha256="f" * 64)
        self.config = fixture["config"]
        self.mark_receipt({"schema": 2, "layout": "v2"})
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 3)
        self.assertIn("--launcher-layout v1", result.stderr)
        self.assertIn("never flips back silently", result.stderr)
        result = self.run_installer("--launcher-layout", "v1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("launcher_change=v2->v1", result.stdout)

    def test_the_layout_option_needs_a_launch_helper_profile(self) -> None:
        result = self.run_installer("--launcher-layout", "v2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("without launch_helper", result.stderr)
        result = self.run_installer("--launcher-layout", "v3")
        self.assertEqual(result.returncode, 2)

    def test_a_v2_install_verifies_the_stable_app_not_the_v1_lanes(self) -> None:
        fixture = self.install_and_seal(commit="a" * 40, manifest_sha256="f" * 64)
        self.config = fixture["config"]
        bundle = self.home / ".local/libexec/TartCILauncher.app"
        lanes_path = bundle / "Contents/Resources/lanes.json"
        lanes = json.loads(lanes_path.read_text())
        next(iter(lanes["lanes"].values()))["environment"]["TARTCI_PYTHON"] = "/wrong/python"
        lanes_path.write_text(json.dumps(lanes))
        self.opt_in()
        result = self.run_installer("--apply", "--launch-helper-source", str(bundle),
                                    *self.V2_SIGNING)
        self.assertNotEqual(result.returncode, 0)
        # v1's sealed-lanes check is not v2's: a v2 app carries no lanes, and
        # the v1 bundle is refused as a v2 app instead.
        self.assertNotIn("launch helper lanes are missing pinned interpreter", result.stderr)
        self.assertIn("macos launcher identity rejected", result.stderr)
        self.assertFalse((self.home / ".config/tartci/launcher-generation.json").exists())

    def v2_receipt_fixture(self) -> tuple[dict, Path]:
        """A real install, then a v2 app record and a pointer at its generation.

        The Developer ID checks (the app's own signature, the set's detached
        one) are substituted: a test cannot mint one."""
        import launch_set  # noqa: PLC0415
        fixture = self.install_and_seal(commit="a" * 40, manifest_sha256="f" * 64)
        bundle = self.home / ".local/libexec/TartCILauncher.app"
        shutil.rmtree(bundle / fleet.SEALED_SUPPORT)
        (bundle / fleet.SEALED_METADATA).unlink()
        policy = self.home / ".config/tartci/launcher-generation.json"
        (bundle / "Contents/Resources/policy.json").write_text(json.dumps(
            {"schema": 2, "pointer": str(policy)}))
        support = fixture["support"]
        generation = Path(support["root"])
        generations = generation.parent
        for directory in (generation, generation / "bin"):
            if directory.is_dir():
                directory.chmod(0o755)
        launch_set.write_runtime(generation, "/usr/bin/python3")
        lanes = {}
        for plist in self.agents.glob("*macos-fleet*.plist"):
            env = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]
            lanes[env["TARTCI_QUEUE_LANE_ID"]] = {"environment": {
                key: value for key, value in env.items()
                if key not in ("TARTCI_PYTHON", "SHIPYARD_GHAPP_PYTHON_BINARY")}}
        launch_set.write(generation, launch_set.build(
            generation, lanes, tart_home="/Volumes/Example/VMs", python="/usr/bin/python3",
            sequence=3))
        (generation / launch_set.SIG_NAME).write_text("signature\n")
        launch_set.freeze(generation)
        policy.write_text(json.dumps({"schema": 1, "generation": str(generation)}))
        return fixture, generations

    def write_v2_receipt(self, fixture: dict) -> dict:
        support = fixture["support"]
        app = {"schema": 2, "layout": "v2", "path": str(self.home / ".local/libexec/TartCILauncher.app")}
        with mock.patch.object(macos_launcher_identity, "verify_v2", return_value=dict(app)):
            fleet.write_receipt(
                fixture["config"], self.agents, fixture["output"],
                Path(support["root"]), Path(support["manifest_path"]),
                Path(support["entrypoint"]["path"]), Path(support["entrypoint"]["path"]),
                Path(support["launch_entrypoint"]["path"]), support["source_commit"],
                "v2",
            )
        return json.loads(fixture["output"].read_text())

    def test_a_v2_receipt_records_the_app_and_the_launch_set_it_runs(self) -> None:
        fixture, _ = self.v2_receipt_fixture()
        receipt = self.write_v2_receipt(fixture)
        helper = receipt["launch_helper"]
        self.assertEqual("v2", helper["layout"])
        self.assertEqual(3, helper["launch_set"]["sequence"])
        self.assertEqual(fixture["support"]["root"], helper["launch_set"]["generation"])
        self.assertEqual(64, len(helper["launch_set"]["set_sha256"]))

    def test_a_v2_pointer_at_another_generation_fails_the_receipt(self) -> None:
        fixture, generations = self.v2_receipt_fixture()
        pointer = self.home / ".config/tartci/launcher-generation.json"
        pointer.write_text(json.dumps({"schema": 1, "generation": str(generations / "other")}))
        with self.assertRaisesRegex(ValueError, "pointer .* does not name the installed generation"):
            self.write_v2_receipt(fixture)
        self.assertFalse(fixture["output"].exists())

    def test_a_v2_set_that_no_longer_verifies_fails_verify_installed(self) -> None:
        fixture, _ = self.v2_receipt_fixture()
        receipt = self.write_v2_receipt(fixture)
        app = {key: receipt["launch_helper"][key] for key in ("schema", "layout", "path")}
        verify = lambda: fleet.verify_receipt(  # noqa: E731
            fixture["output"], fixture["config"], self.agents, Path(fixture["support"]["root"]))
        with mock.patch.object(macos_launcher_identity, "verify_v2", return_value=dict(app)):
            verify()
            generation = Path(fixture["support"]["root"])
            generation.chmod(0o755)
            (generation / "stray").write_text("x")
            generation.chmod(0o555)
            with self.assertRaisesRegex(ValueError, "launch set does not verify"):
                verify()

    def rollback_after(self, *, touched: bool, backup: bool,
                       backup_failure: bool = False) -> tuple[Path, Path, subprocess.CompletedProcess]:
        """Run the installer's own rollback() with only the pointer touched."""
        text = (ROOT / "scripts/install_macos_fleet.sh").read_text()
        start = text.index("rollback() {")
        function = text[start:text.index("\n}\n", start) + 3]
        pointer = self.home / ".config/tartci/launcher-generation.json"
        pointer.write_text('{"generation": "new"}\n')
        saved = self.root / "launcher-generation.json.previous"
        if backup:
            if backup_failure:
                saved.mkdir()
                (saved / "cannot-move-a-directory-as-a-file").write_text("x")
            else:
                saved.write_text('{"generation": "old"}\n')
        script = (function + f"pointer_file={shlex.quote(str(pointer))}\n"
                  f"pointer_backup={shlex.quote(str(saved)) if backup else ''}\n"
                  f"pointer_touched={1 if touched else 0}\nrollback\n")
        result = subprocess.run(["/bin/bash", "-c", script], text=True, capture_output=True,
                                check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        return pointer, saved, result

    def test_rollback_puts_the_previous_pointer_back_atomically(self) -> None:
        pointer, saved, _ = self.rollback_after(touched=True, backup=True)
        self.assertEqual('{"generation": "old"}\n', pointer.read_text())
        self.assertFalse(saved.exists(), "restored by one rename, not a copy")
        source = (ROOT / "scripts/install_macos_fleet.sh").read_text()
        restore = source[source.index('if [ "$pointer_touched" = 1 ]'):]
        restore = restore[:restore.index("\n  fi\n")]
        self.assertLess(restore.index('mv -f "$pointer_backup" "$pointer_file"'),
                        restore.index('rm -f "$pointer_file"'),
                        "the pointer is never removed before its backup replaces it")
        self.assertIn("launcher_pointer_restore_failed", restore)

    def test_rollback_without_a_previous_pointer_removes_the_new_one(self) -> None:
        pointer, _, _ = self.rollback_after(touched=True, backup=False)
        self.assertFalse(pointer.exists())

    def test_rollback_leaves_a_pointer_this_install_never_touched(self) -> None:
        # Control: same run, the pointer step never reached.
        pointer, _, _ = self.rollback_after(touched=False, backup=True)
        self.assertEqual('{"generation": "new"}\n', pointer.read_text())

    def test_rollback_reports_a_failed_pointer_restore(self) -> None:
        pointer, saved, result = self.rollback_after(touched=True, backup=True,
                                                      backup_failure=True)
        self.assertEqual('{"generation": "new"}\n', pointer.read_text())
        self.assertTrue(saved.is_dir())
        self.assertIn("FINDING launcher_pointer_restore_failed", result.stderr)
        for part in ("what:", "cause:", "host: left OFF", "fix: mv -f", "verify:"):
            self.assertIn(part, result.stderr)

    def cleanup_exit(self, *, backup_failure: bool) -> subprocess.CompletedProcess:
        """The installer's own cleanup trap after a failed install (exit 1)."""
        text = (ROOT / "scripts/install_macos_fleet.sh").read_text()
        functions = ""
        for name in ("rollback", "cleanup"):
            start = text.index(f"{name}() {{")
            functions += text[start:text.index("\n}\n", start) + 3]
        pointer = self.home / ".config/tartci/launcher-generation.json"
        pointer.write_text('{"generation": "new"}\n')
        saved = self.root / "launcher-generation.json.previous"
        if saved.is_dir():
            shutil.rmtree(saved)
        if backup_failure:
            saved.mkdir()
            (saved / "x").write_text("x")
        else:
            saved.write_text('{"generation": "old"}\n')
        script = (functions + f"pointer_file={shlex.quote(str(pointer))}\n"
                  f"pointer_backup={shlex.quote(str(saved))}\npointer_touched=1\n"
                  "pointer_restore_failed=0\ntrap cleanup EXIT\nexit 1\n")
        return subprocess.run(["/bin/bash", "-c", script], text=True, capture_output=True,
                              check=False)

    def test_a_failed_pointer_restore_fails_the_install_with_its_own_exit_code(self) -> None:
        result = self.cleanup_exit(backup_failure=True)
        self.assertEqual(6, result.returncode, result.stderr)
        self.assertIn("FINDING launcher_pointer_restore_failed", result.stderr)
        # Control: a restore that works keeps the install's own exit code.
        result = self.cleanup_exit(backup_failure=False)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertNotIn("launcher_pointer_restore_failed", result.stderr)

    def test_a_v2_host_whose_receipt_is_gone_does_not_flip_to_v1(self) -> None:
        # The receipt was deleted: the pointer on disk still says v2, so the
        # install refuses instead of silently installing v1 without the opt-in.
        fixture = self.install_and_seal(commit="a" * 40, manifest_sha256="f" * 64)
        self.config = fixture["config"]
        (self.home / ".config/tartci/macos-fleet-install.json").unlink()
        pointer = self.home / ".config/tartci/launcher-generation.json"
        pointer.write_text(json.dumps({"schema": 1, "generation": "/x"}))
        result = self.run_installer()
        self.assertEqual(3, result.returncode, result.stdout)
        self.assertIn("install receipt is missing but the launcher v2 pointer", result.stderr)
        self.assertIn("--launcher-layout v1", result.stderr)
        # Control: without the pointer, a receipt-less sealed host plans v1.
        pointer.unlink()
        result = self.run_installer()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("launcher_layout=v1 installed=none", result.stdout)
        # A v2 app at the profile's launch helper path is evidence too.
        policy = self.home / ".local/libexec/TartCILauncher.app/Contents/Resources/policy.json"
        policy.write_text(json.dumps({"schema": 2}))
        result = self.run_installer()
        self.assertEqual(3, result.returncode, result.stdout)
        self.assertIn("a launcher v2 app at", result.stderr)


if __name__ == "__main__":
    unittest.main()
