#!/usr/bin/env python3
"""Behavioral regressions for the sealed resident launcher app."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native/macos/tartci-launcher/main.swift"


@unittest.skipUnless(os.uname().sysname == "Darwin", "macOS native launcher")
class LauncherRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.app = cls.root / "TartCILauncher.app"
        cls.launcher = cls.app / "Contents/MacOS/tartci-launcher"
        support = cls.app / "Contents/Resources/support"
        cls.launcher.parent.mkdir(parents=True)
        support.mkdir(parents=True)
        cls.pid_file = cls.root / "descendant.pid"
        lanes = {}
        for lane, mode in (
            ("exit-zero", "exit-zero"),
            ("exit-one", "exit-one"),
            ("spawn-and-exit", "spawn-and-exit"),
            ("term-group", "term-group"),
        ):
            lanes[lane] = {"environment": {
                "HOME": str(cls.root),
                "PATH": "/usr/bin:/bin",
                "TART_HOME": "/Volumes/Example/VMs",
                "TARTCI_TEST_MODE": mode,
                "TARTCI_TEST_PID_FILE": str(cls.pid_file),
            }}
        (cls.app / "Contents/Resources/bundle.json").write_text(
            json.dumps({"tart_home": "/Volumes/Example/VMs"})
        )
        (cls.app / "Contents/Resources/lanes.json").write_text(
            json.dumps({"schema": 1, "lanes": lanes})
        )
        launch = support / ".tartci-launch"
        launch.write_text(
            "#!/bin/sh\n"
            "case \"$TARTCI_TEST_MODE\" in\n"
            "  exit-zero) exit 0 ;;\n"
            "  exit-one) exit 1 ;;\n"
            "  spawn-and-exit) trap '' TERM; while :; do sleep 1; done & echo $! > \"$TARTCI_TEST_PID_FILE\"; exit 0 ;;\n"
            "  term-group) sh -c 'trap \"\" TERM INT HUP; while :; do sleep 1; done' & echo $! > \"$TARTCI_TEST_PID_FILE\"; trap 'exit 0' TERM INT HUP; while :; do sleep 1; done ;;\n"
            "  *) exit 99 ;;\n"
            "esac\n"
        )
        launch.chmod(0o755)
        subprocess.run([
            "xcrun", "swiftc", "-O", "-D", "TARTCI_TESTING",
            "-framework", "Security", str(SOURCE), "-o", str(cls.launcher),
        ], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.pid_file.unlink(missing_ok=True)

    def run_lane(self, lane: str, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.launcher), "--lane", lane], check=False, **kwargs
        )

    def test_propagates_child_exit_and_discards_ambient_policy(self) -> None:
        poisoned = dict(os.environ)
        poisoned.update({
            "TARTCI_TEST_MODE": "exit-one",
            "PULP_UNTRUSTED": "1",
            "SHIPYARD_UNTRUSTED": "1",
            "PATH": "/tmp/not-real",
        })
        self.assertEqual(self.run_lane("exit-zero", env=poisoned).returncode, 0)
        self.assertEqual(self.run_lane("exit-one").returncode, 1)

    def test_rejects_arbitrary_execution_and_unknown_lane(self) -> None:
        self.assertEqual(subprocess.run([
            str(self.launcher), "--", "/usr/bin/true",
        ], check=False).returncode, 64)
        self.assertEqual(self.run_lane("not-signed").returncode, 77)
        self.assertEqual(subprocess.run([
            str(self.launcher), "--probe-store", "/tmp",
        ], check=False).returncode, 64)

    def test_kills_descendant_after_direct_child_exits(self) -> None:
        result = self.run_lane("spawn-and-exit", timeout=5)
        self.assertEqual(result.returncode, 0)
        descendant = int(self.pid_file.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"descendant {descendant} survived launcher exit")

    def test_term_kills_full_child_group(self) -> None:
        launcher = subprocess.Popen([
            str(self.launcher), "--lane", "term-group",
        ])
        try:
            deadline = time.monotonic() + 3
            while not self.pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.pid_file.exists())
            descendant = int(self.pid_file.read_text())
            launcher.send_signal(signal.SIGTERM)
            self.assertEqual(launcher.wait(timeout=5), 0)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(descendant, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"descendant {descendant} survived TERM cleanup")
        finally:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait()




V2_TEAM = "AB12CD34EF"
V2_STORE = "/Volumes/Example/VMs"
V2_TEST_REQUIREMENT = 'identifier "com.danielraffel.tartci.launch-set"'
# The generation's `tartci`, reached through v2's real entrypoint (which first
# verifies the cohort through the interpreter shim).
V2_TARTCI = (
    "#!/bin/bash\n"
    "case \"$TARTCI_TEST_MODE\" in\n"
    "  exit-zero) exit 0 ;;\n"
    "  exit-one) exit 1 ;;\n"
    "  env) echo \"${PYTHONDONTWRITEBYTECODE-unset}:${PYTHONNOUSERSITE-unset}:$TARTCI_PYTHON:$(command -v python3)\" > \"$HOME/env.out\"; exit 0 ;;\n"
    "  python) \"$TARTCI_PYTHON\" -c pass && python3 -c pass || exit 98; exit 0 ;;\n"
    # A lane script starting a child the way support code does: through
    # python_child.child_python() (or, as the control, sys.executable).
    "  child|child-control) \"$TARTCI_PYTHON\" -c \"import json, subprocess, sys; "
    "sys.path.insert(0, '$(dirname \"$0\")/scripts'); from python_child import child_python; "
    "py = sys.executable if '$TARTCI_TEST_MODE' == 'child-control' else child_python(); "
    "probe = 'import json, sys; print(json.dumps([sys.flags.no_site, sys.flags.ignore_environment]))'; "
    "open('$HOME/child.out', 'w').write(subprocess.run([py, '-c', probe], capture_output=True, "
    "text=True, check=True).stdout)\" || exit 97; exit 0 ;;\n"
    "  *) exit 99 ;;\n"
    "esac\n"
)


@unittest.skipUnless(os.uname().sysname == "Darwin", "macOS native launcher")
class LauncherV2RuntimeTests(unittest.TestCase):
    """Launcher v2 runs only a signed, intact, trusted launch set; otherwise 77.

    The test build accepts an ad-hoc signature by its identifier
    (TARTCI_TEST_LAUNCH_SET_REQUIREMENT); production pins policy.team_id."""

    @classmethod
    def setUpClass(cls) -> None:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import launch_set
        import test_launch_set
        cls.ls, cls.fixture = launch_set, test_launch_set
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(os.path.realpath(cls.temp.name))
        cls.launcher = cls.base / "TartCILauncher.app/Contents/MacOS/tartci-launcher"
        cls.launcher.parent.mkdir(parents=True)
        subprocess.run(["xcrun", "swiftc", "-O", "-D", "TARTCI_TESTING", "-framework", "Security",
                        str(SOURCE), "-o", str(cls.launcher)], check=True)
        cls.make_venv()

    @classmethod
    def make_venv(cls) -> None:
        """A scratch venv: the pinned interpreter, and the stand-in for a
        user-owned global site-packages (nothing is planted in /opt/homebrew)."""
        import sys
        cls.venv = cls.base / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(cls.venv)], check=True)
        cls.pin = str(cls.venv / "bin/python3")
        cls.venv_site = Path(subprocess.run(
            [cls.pin, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            capture_output=True, text=True, check=True).stdout.strip())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
        # Canonical, so the launcher's realpath check passes whatever TMPDIR is.
        self.work = work = Path(os.path.realpath(self.td.name))
        os.chmod(work, 0o755)
        self.root = work / "generations"
        self.root.mkdir(mode=0o755)
        self.home = work / "home"
        (self.home / ".config/tartci").mkdir(parents=True, mode=0o755)
        for directory in (self.home, self.home / ".config", self.home / ".config/tartci"):
            os.chmod(directory, 0o755)
        self.pointer = self.home / ".config/tartci/launcher-generation.json"
        self.floor = self.home / ".config/tartci/launcher-floor.json"
        self.write_policy(self.root)
        self.generation = self.fixture.make_generation(self.root, pin=self.pin, tartci=V2_TARTCI)
        self.lanes = {mode: {"environment": {"HOME": str(work), "PATH": "/usr/bin:/bin",
                                             "TART_HOME": V2_STORE, "TARTCI_TEST_MODE": mode,
                                             "TARTCI_PYTHON": self.pin}}
                      for mode in ("exit-zero", "exit-one", "env", "python", "child",
                                   "child-control")}
        self.seal()

    def write_policy(self, root) -> None:
        resources = self.launcher.parent.parent / "Resources"
        resources.mkdir(exist_ok=True)
        (resources / "policy.json").write_text(json.dumps({
            "schema": 2, "identifier": "com.danielraffel.tartci.launcher", "team_id": V2_TEAM,
            "tart_home": V2_STORE, "generations_root": str(root), "home": str(self.home),
            "pointer": str(self.pointer), "floor": str(self.floor)}))

    def _cleanup(self) -> None:
        for dirpath, dirs, _files in os.walk(self.td.name):
            for name in dirs:
                os.chmod(os.path.join(dirpath, name), 0o755)
        self.td.cleanup()

    def seal(self, *, identifier: str = "com.danielraffel.tartci.launch-set", value=None,
             sequence: int = 1) -> None:
        """Write, ad-hoc sign and freeze the launch set; point at it."""
        os.chmod(self.generation, 0o755)
        for name in (self.ls.SET_NAME, self.ls.SIG_NAME):
            (self.generation / name).unlink(missing_ok=True)
        self.ls.write(self.generation, value or self.ls.build(self.generation, self.lanes,
                                                              tart_home=V2_STORE, python=self.pin, sequence=sequence))
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", identifier,
                        "--detached", str(self.generation / self.ls.SIG_NAME),
                        str(self.generation / self.ls.SET_NAME)], check=True, capture_output=True)
        self.ls.freeze(self.generation)
        self.point(self.generation)

    def point(self, generation) -> None:
        self.pointer.write_text(json.dumps({"schema": 1, "generation": str(generation)}))
        os.chmod(self.pointer, 0o600)

    def run_lane(self, lane: str = "exit-zero") -> subprocess.CompletedProcess:
        env = {"PATH": "/usr/bin:/bin", "TARTCI_TEST_LAUNCH_SET_REQUIREMENT": V2_TEST_REQUIREMENT}
        return subprocess.run([str(self.launcher), "--lane", lane], env=env, capture_output=True,
                              text=True, timeout=60, check=False)

    def edit(self, relative: str, data: bytes) -> None:
        path = self.generation / relative
        mode = path.stat().st_mode & 0o777
        os.chmod(path.parent, 0o755)
        os.chmod(path, 0o644)
        path.write_bytes(data)
        os.chmod(path, mode)
        os.chmod(path.parent, 0o555)

    def assertRefused(self, why: str) -> None:
        result = self.run_lane()
        self.assertEqual(77, result.returncode, result.stderr)
        self.assertIn(why, result.stderr)

    def test_a_valid_set_runs_its_lane(self) -> None:
        self.assertEqual(0, self.run_lane("exit-zero").returncode, self.run_lane().stderr)
        self.assertEqual(1, self.run_lane("exit-one").returncode)
        result = self.run_lane("not-in-set")
        self.assertEqual(77, result.returncode)
        self.assertIn("not in the signed launch set", result.stderr)

    def test_a_set_signed_with_another_identifier_is_refused(self) -> None:
        self.seal(identifier="com.example.other")
        self.assertRefused("signature is invalid")

    def test_an_unsigned_set_is_refused(self) -> None:
        os.chmod(self.generation, 0o755)
        (self.generation / self.ls.SIG_NAME).unlink()
        os.chmod(self.generation, 0o555)
        self.assertRefused("cannot open")

    def test_a_set_edited_after_signing_is_refused(self) -> None:
        value = json.loads((self.generation / self.ls.SET_NAME).read_text())
        value["lanes"]["exit-zero"]["environment"]["TARTCI_TEST_MODE"] = "exit-one"
        os.chmod(self.generation, 0o755)
        self.edit(self.ls.SET_NAME, (json.dumps(value) + "\n").encode())
        self.assertRefused("signature is invalid")

    def test_one_byte_in_any_member_or_the_entrypoint_is_refused(self) -> None:
        for relative, why in (("tartci", "does not match its digest"),
                              ("scripts/x.py", "does not match its digest"),
                              (self.ls.ENTRYPOINT, "does not match its digest"),
                              (".tartci-launch", "does not match its digest"),
                              (self.ls.MANIFEST_NAME, "support manifest does not match")):
            with self.subTest(relative=relative):
                original = (self.generation / relative).read_bytes()
                self.edit(relative, original + b"#")
                self.assertRefused(why)
                self.edit(relative, original)
                self.assertEqual(0, self.run_lane().returncode)

    def test_a_writable_or_foreign_path_is_refused(self) -> None:
        member = self.generation / "scripts/x.py"
        for path, mode in ((member, 0o464), (member.parent, 0o575), (self.generation, 0o575),
                           (self.root, 0o775), (self.pointer, 0o620)):
            with self.subTest(path=str(path), mode=oct(mode)):
                before = path.stat().st_mode & 0o777
                os.chmod(path, mode)
                self.assertRefused("writable")
                os.chmod(path, before)
        self.assertEqual(0, self.run_lane().returncode)

    def test_a_symlinked_member_is_refused(self) -> None:
        os.chmod(self.generation, 0o755)
        os.chmod(self.generation / "scripts", 0o755)
        real = self.generation / "scripts/x.py"
        moved = self.generation / "scripts/real.py"
        os.rename(real, moved)
        real.symlink_to(moved)
        self.ls.freeze(self.generation)
        self.assertRefused("symlink")

    def test_a_pointer_outside_the_root_is_refused(self) -> None:
        elsewhere = Path(self.td.name) / "elsewhere"
        elsewhere.mkdir()
        for target in (elsewhere, self.root, self.root / ".staging-x", Path("/usr/bin")):
            with self.subTest(target=str(target)):
                self.point(target)
                self.assertRefused("pointer names no generation")

    def test_a_signed_lane_on_another_store_is_refused(self) -> None:
        value = self.ls.build(self.generation, self.lanes, tart_home=V2_STORE, python=self.pin, sequence=1)
        value["lanes"]["exit-zero"]["environment"]["TART_HOME"] = "/Volumes/Example2/VMs"
        self.seal(value=value)
        self.assertRefused("wrong Tart store")



    def plant(self, relative: str) -> None:
        """A file the manifest does not name, in an otherwise frozen generation."""
        for dirpath, _dirs, _files in os.walk(self.generation):
            os.chmod(dirpath, 0o755)
        target = self.generation / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00planted")
        self.ls.freeze(self.generation)

    def test_a_planted_bytecode_cache_is_refused(self) -> None:
        self.plant("scripts/__pycache__/x.cpython-311.pyc")
        self.assertRefused("a bytecode cache in the generation is never hashed")

    def test_an_empty_bytecode_cache_directory_is_refused(self) -> None:
        for dirpath, _dirs, _files in os.walk(self.generation):
            os.chmod(dirpath, 0o755)
        (self.generation / "scripts/__pycache__").mkdir()
        self.ls.freeze(self.generation)
        self.assertRefused("a bytecode cache in the generation is never hashed")

    def test_a_set_that_does_not_pin_the_v1_launch_file_is_refused(self) -> None:
        # Every staged generation carries v1's .tartci-launch; the set pins it.
        value = json.loads((self.generation / self.ls.SET_NAME).read_text())
        self.assertEqual([".tartci-launch"], [f["path"] for f in value["files"]])
        value["files"] = []
        self.seal(value=value)
        self.assertRefused(".tartci-launch is not in the signed manifest")

    def test_a_planted_file_outside_the_manifest_is_refused(self) -> None:
        self.plant("planted.py")
        self.assertRefused("planted.py is not in the signed manifest")

    def test_lanes_get_the_shim_and_no_bytecode_or_user_site(self) -> None:
        self.assertEqual(0, self.run_lane("env").returncode, self.run_lane("env").stderr)
        shim = f"{self.generation}/bin/python3"
        self.assertEqual(f"1:1:{shim}:{shim}\n", (self.work / "env.out").read_text())

    def repin(self, pin: str) -> None:
        """Rewrite the shim and entrypoint for another pinned interpreter and re-seal."""
        for dirpath, _dirs, _files in os.walk(self.generation):
            os.chmod(dirpath, 0o755)
        self.ls.write_runtime(self.generation, pin)
        self.pin = pin
        for row in self.lanes.values():
            row["environment"]["TARTCI_PYTHON"] = pin
        self.seal()

    def plant_site(self, site: Path, marker: Path) -> None:
        site.mkdir(parents=True, exist_ok=True)
        touch = f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n"
        (site / "tartci-plant.pth").write_text(touch)
        for name in ("sitecustomize.py", "usercustomize.py"):
            (site / name).write_text(touch.replace("planted", name))

    def assertPlantsDoNotRun(self, interpreter: str, site: Path) -> None:
        marker = self.work / "planted.ran"
        self.plant_site(site, marker)
        # Control: started plainly, the plants run.
        subprocess.run([interpreter, "-c", "pass"],
                       env={"HOME": str(self.work), "PATH": "/usr/bin:/bin"}, check=True)
        self.assertTrue(marker.exists(), "the plant must run without the shim")
        marker.unlink()
        result = self.run_lane("python")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(marker.exists(), "a planted .pth or sitecustomize ran under the launcher")

    def test_a_planted_global_site_pth_or_sitecustomize_does_not_run(self) -> None:
        self.assertPlantsDoNotRun(self.pin, self.venv_site)

    def test_a_planted_user_site_pth_or_usercustomize_does_not_run(self) -> None:
        brew = "/opt/homebrew/bin/python3.11"
        if not os.access(brew, os.X_OK):
            self.skipTest("no Homebrew python3.11 to pin")
        self.repin(brew)
        site = Path(subprocess.run([brew, "-c", "import site; print(site.getusersitepackages())"],
                                   env={"HOME": str(self.work), "PATH": "/usr/bin:/bin"},
                                   capture_output=True, text=True, check=True).stdout.strip())
        self.assertTrue(str(site).startswith(str(self.work)), site)
        self.assertPlantsDoNotRun(brew, site)

    def test_a_child_started_by_lane_python_stays_isolated(self) -> None:
        # The shim execs the pin, so inside the lane sys.executable is the bare
        # pin; child_python() keeps the grandchild on the shim.
        marker = self.work / "planted.ran"
        self.plant_site(self.venv_site, marker)
        result = self.run_lane("child")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("[1, 1]", (self.work / "child.out").read_text().strip())
        self.assertFalse(marker.exists(), "site start-up code ran in the grandchild")
        # Control: a grandchild started with sys.executable runs site, plant included.
        result = self.run_lane("child-control")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("[0, 0]", (self.work / "child.out").read_text().strip())
        self.assertTrue(marker.exists(), "the control must run the plant")

    def test_safe_paths_allow_at_and_refuse_shell_characters(self) -> None:
        # A Homebrew Cellar pin carries "@": accepted.
        at = self.work / "python@3" / "bin"
        at.mkdir(parents=True)
        (at / "python3").symlink_to(self.pin)
        self.repin(str(at / "python3"))
        self.assertEqual(0, self.run_lane().returncode, self.run_lane().stderr)
        # A pin with a space (or any shell character) never matches: the
        # launcher refuses whatever digest the set carries for it.
        for pin in ("/opt/py thon/python3", "/opt/$(id)/python3", '/opt/"/python3'):
            with self.subTest(pin=pin):
                value = self.ls.build(self.generation, self.lanes, tart_home=V2_STORE,
                                      python=self.pin, sequence=1)
                value["python"]["pin"] = pin
                value["python"]["shim"]["sha256"] = hashlib.sha256(
                    f'#!/bin/sh\nexec {pin} -B -E -s -S "$@"\n'.encode()).hexdigest()
                self.seal(value=value)
                self.assertRefused("interpreter shim does not exec the signed pin")

    def test_a_lane_interpreter_that_is_not_the_shim_is_refused(self) -> None:
        for key in ("TARTCI_PYTHON", "SHIPYARD_GHAPP_PYTHON_BINARY"):
            with self.subTest(key=key):
                value = self.ls.build(self.generation, self.lanes, tart_home=V2_STORE,
                                      python=self.pin, sequence=1)
                value["lanes"]["exit-zero"]["environment"][key] = "/usr/bin/python3"
                self.seal(value=value)
                self.assertRefused(f"signed lane {key} is /usr/bin/python3")

    def test_a_path_whose_first_python3_is_not_the_shim_is_refused(self) -> None:
        # A shim that cannot be executed leaves PATH's first python3 elsewhere.
        shim = self.generation / "bin/python3"
        os.chmod(self.generation / "bin", 0o755)
        os.chmod(shim, 0o444)
        os.chmod(self.generation / "bin", 0o555)
        self.assertRefused("the first python3 on the lane's PATH is not the interpreter shim")

    def test_a_signed_entrypoint_that_is_not_v2s_is_refused(self) -> None:
        # Hashed and signed, but not the entrypoint v2 builds for this
        # generation (it would run Python without the shim).
        value = self.ls.build(self.generation, self.lanes, tart_home=V2_STORE, python=self.pin,
                              sequence=1)
        other = b'#!/bin/bash\nexec /bin/bash "$(dirname "$0")/tartci" "$@"\n'
        entry = self.generation / self.ls.ENTRYPOINT
        os.chmod(self.generation, 0o755)
        os.chmod(entry, 0o755)
        entry.write_bytes(other)
        os.chmod(entry, 0o555)
        value["entrypoint"]["sha256"] = hashlib.sha256(other).hexdigest()
        self.seal(value=value)
        self.assertRefused("entrypoint is not v2's for this generation")

    def test_a_shim_that_does_not_exec_the_signed_pin_is_refused(self) -> None:
        value = self.ls.build(self.generation, self.lanes, tart_home=V2_STORE, python=self.pin,
                              sequence=1)
        value["python"]["pin"] = "/usr/bin/python3"
        self.seal(value=value)
        self.assertRefused("interpreter shim does not exec the signed pin")

    def test_a_python_start_up_variable_in_the_set_is_refused(self) -> None:
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONPYCACHEPREFIX"):
            with self.subTest(name=name):
                value = self.ls.build(self.generation, self.lanes, tart_home=V2_STORE, python=self.pin, sequence=1)
                value["lanes"]["exit-zero"]["environment"][name] = str(self.work)
                self.seal(value=value)
                self.assertRefused(f"signed lane sets {name}")

    def test_a_corrupt_floor_is_refused(self) -> None:
        self.assertEqual(0, self.run_lane().returncode)
        for body in ("{not json", '{"schema": 2, "sequence": 1}', ""):
            with self.subTest(body=body):
                self.floor.write_text(body)
                os.chmod(self.floor, 0o600)
                self.assertRefused("launcher floor")
        self.floor.unlink()                 # a missing floor fails open
        self.assertEqual(0, self.run_lane().returncode)

    def test_a_fifo_in_the_generation_is_refused(self) -> None:
        for dirpath, _dirs, _files in os.walk(self.generation):
            os.chmod(dirpath, 0o755)
        os.mkfifo(self.generation / "scripts/pipe")
        self.ls.freeze(self.generation)
        self.assertRefused("scripts/pipe is not a regular file")

    def test_a_symlink_above_the_generations_root_is_refused(self) -> None:
        alias = self.work / "alias"
        alias.symlink_to(self.work)
        self.write_policy(alias / "generations")
        self.point(alias / "generations" / self.generation.name)
        self.assertRefused("not canonical")

    def test_a_nested_pointer_name_is_refused(self) -> None:
        self.point(self.generation / "scripts")
        self.assertRefused("pointer names no generation")

    def test_a_symlinked_pointer_is_refused(self) -> None:
        real = self.pointer.with_name("real-pointer.json")
        os.rename(self.pointer, real)
        self.pointer.symlink_to(real)
        self.assertRefused("is a symlink")

    def test_a_writable_pointer_directory_is_refused(self) -> None:
        for directory in (self.pointer.parent, self.home / ".config", self.home):
            with self.subTest(directory=str(directory)):
                os.chmod(directory, 0o775)
                self.assertRefused("writable")
                os.chmod(directory, 0o755)
        self.assertEqual(0, self.run_lane().returncode)

    def test_an_older_set_is_refused_except_the_rollback_one(self) -> None:
        for sequence in (4, 6):
            self.seal(sequence=sequence)
            self.assertEqual(0, self.run_lane().returncode)
        self.assertEqual({"schema": 1, "sequence": 6, "previous_sequence": 4},
                         json.loads(self.floor.read_text()))
        self.seal(sequence=4)               # the rollback generation still runs
        self.assertEqual(0, self.run_lane().returncode)
        self.seal(sequence=3)
        self.assertRefused("older than the last one run (6)")
        self.seal(sequence=6)
        self.assertEqual(0, self.run_lane().returncode)
        self.assertEqual(0o600, self.floor.stat().st_mode & 0o777)


@unittest.skipUnless(os.uname().sysname == "Darwin", "macOS native launcher")
class LauncherV2ProductionTests(LauncherV2RuntimeTests):
    """The production build: the requirement is the policy's team, and the
    test override is compiled out, so an ad-hoc launch set never runs."""

    @classmethod
    def setUpClass(cls) -> None:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import launch_set
        import test_launch_set
        cls.ls, cls.fixture = launch_set, test_launch_set
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(os.path.realpath(cls.temp.name))
        app = cls.base / "TartCILauncher.app"
        cls.launcher = app / "Contents/MacOS/tartci-launcher"
        cls.launcher.parent.mkdir(parents=True)
        (app / "Contents/Resources").mkdir()
        subprocess.run(["xcrun", "swiftc", "-O", "-framework", "Security",
                        str(SOURCE), "-o", str(cls.launcher)], check=True)
        (app / "Contents/Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
            "<key>CFBundleExecutable</key><string>tartci-launcher</string>"
            "<key>CFBundleIdentifier</key><string>com.example.tartci.launcher-test</string>"
            "<key>CFBundlePackageType</key><string>APPL</string></dict></plist>")
        cls.app = app
        cls.make_venv()

    def setUp(self) -> None:
        super().setUp()
        # policy.json is written by the parent setUp; the app is sealed after it.
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(self.app)],
                       check=True, capture_output=True)

    def test_an_ad_hoc_launch_set_is_refused_even_with_the_test_override(self) -> None:
        self.assertRefused("not from the pinned team")

    # The parent's tests assume the test requirement; in production every
    # ad-hoc set is refused before any of their specific checks.
    test_a_valid_set_runs_its_lane = None
    test_a_set_signed_with_another_identifier_is_refused = None
    test_an_unsigned_set_is_refused = None
    test_a_set_edited_after_signing_is_refused = None
    test_one_byte_in_any_member_or_the_entrypoint_is_refused = None
    test_a_writable_or_foreign_path_is_refused = None
    test_a_symlinked_member_is_refused = None
    test_a_pointer_outside_the_root_is_refused = None
    test_a_signed_lane_on_another_store_is_refused = None
    test_a_planted_bytecode_cache_is_refused = None
    test_a_planted_file_outside_the_manifest_is_refused = None
    test_a_set_that_does_not_pin_the_v1_launch_file_is_refused = None
    test_an_empty_bytecode_cache_directory_is_refused = None
    test_lanes_get_the_shim_and_no_bytecode_or_user_site = None
    test_a_planted_global_site_pth_or_sitecustomize_does_not_run = None
    test_a_planted_user_site_pth_or_usercustomize_does_not_run = None
    test_a_lane_interpreter_that_is_not_the_shim_is_refused = None
    test_a_path_whose_first_python3_is_not_the_shim_is_refused = None
    test_a_shim_that_does_not_exec_the_signed_pin_is_refused = None
    test_a_signed_entrypoint_that_is_not_v2s_is_refused = None
    test_a_child_started_by_lane_python_stays_isolated = None
    test_safe_paths_allow_at_and_refuse_shell_characters = None
    test_a_python_start_up_variable_in_the_set_is_refused = None
    test_a_corrupt_floor_is_refused = None
    test_a_fifo_in_the_generation_is_refused = None
    test_a_symlink_above_the_generations_root_is_refused = None
    test_a_nested_pointer_name_is_refused = None
    test_a_symlinked_pointer_is_refused = None
    test_a_writable_pointer_directory_is_refused = None
    test_an_older_set_is_refused_except_the_rollback_one = None

if __name__ == "__main__":
    unittest.main()
