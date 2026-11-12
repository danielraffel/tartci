#!/usr/bin/env python3
"""launch_set: building, signing, verifying and pointing at a launch set."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launch_set as ls
import tartci_support_manifest as support_manifest

TEAM = "AB12CD34EF"
STORE = "/Volumes/Example/VMs"


HERE = Path(__file__).resolve().parent
PIN = "/usr/bin/python3"


def make_generation(root: Path, name: str = "a" * 40 + "-1", *, pin: str = PIN,
                    tartci: str = "#!/bin/bash\nexit 0\n") -> Path:
    """A support generation the real `tartci_support_manifest.py verify
    --immutable` accepts once frozen, with v1's `.tartci-launch` as
    stage-install writes it, and v2's entrypoint and shim."""
    generation = root / name
    (generation / "scripts").mkdir(parents=True)
    members = []
    for relative, data, mode in (
            ("tartci", tartci.encode(), 0o755),
            ("scripts/x.py", b"print(1)\n", 0o644),
            ("scripts/tartci_support_manifest.py",
             (HERE / "tartci_support_manifest.py").read_bytes(), 0o644),
            ("scripts/python_child.py", (HERE / "python_child.py").read_bytes(), 0o644)):
        path = generation / relative
        path.write_bytes(data)
        os.chmod(path, mode)
        members.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "mode": mode})
    (generation / ls.MANIFEST_NAME).write_text(json.dumps(
        {"schema": 2, "repository": "https://github.com/danielraffel/tartci.git",
         "source_commit": "a" * 40, "members": members}))
    os.chmod(generation / ls.MANIFEST_NAME, 0o444)
    launch = generation / support_manifest.LAUNCH_NAME
    launch.write_bytes(support_manifest.canonical_wrapper_bytes(generation))
    os.chmod(launch, 0o555)
    ls.write_runtime(generation, pin)
    return generation


LANES = {"studio-pulp-gate": {"environment": {"TART_HOME": STORE, "PATH": "/usr/bin:/bin",
                                              "TARTCI_PYTHON": PIN}}}


class FakeCodesign:
    """codesign stand-in: signing writes a digest of the set; verification
    accepts only that digest and the pinned requirement."""

    def __init__(self, *, sign_rc: int = 0) -> None:
        self.sign_rc = sign_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        target = Path(argv[-1])
        sig = Path(argv[argv.index("--detached") + 1])
        if "--sign" in argv:
            if self.sign_rc == 0:
                sig.write_text(hashlib.sha256(target.read_bytes()).hexdigest())
            return subprocess.CompletedProcess(argv, self.sign_rc, "", "" if self.sign_rc == 0 else "no identity")
        ok = (sig.is_file() and sig.read_text() == hashlib.sha256(target.read_bytes()).hexdigest()
              and f"-R={ls.requirement(TEAM)}" in argv)
        return subprocess.CompletedProcess(argv, 0 if ok else 1, "", "" if ok else "invalid signature")


class LaunchSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        # Canonical, so the trust checks see the same path whatever TMPDIR is.
        self.root = Path(os.path.realpath(self.td.name)) / "generations"
        self.root.mkdir()
        os.chmod(self.root, 0o755)
        self.pointer = self.root.parent / "config" / "launcher-generation.json"
        self.generation = make_generation(self.root)

    def tearDown(self) -> None:
        for dirpath, dirs, _files in os.walk(self.td.name):
            for name in dirs:
                os.chmod(os.path.join(dirpath, name), 0o755)
        self.td.cleanup()

    def written(self, generation: Path | None = None, sequence: int = 2) -> Path:
        generation = generation or self.generation
        os.chmod(generation, 0o755)
        ls.write(generation, ls.build(generation, LANES, tart_home=STORE, python=PIN, sequence=sequence))
        return generation

    def publish(self, generation: Path, codesign=None, ready=lambda: None) -> dict:
        return ls.publish(generation, pointer=self.pointer, root=self.root, team_id=TEAM,
                          identity="Developer ID Application: Example", keychain="/k.keychain-db",
                          signing_ready=ready, run=codesign or FakeCodesign())

    def test_the_set_pins_the_manifest_entrypoint_and_lanes(self) -> None:
        value = ls.build(self.generation, LANES, tart_home=STORE, python=PIN, sequence=7)
        self.assertEqual(7, value["sequence"])
        self.assertEqual("a" * 40, value["source_commit"])
        self.assertEqual(hashlib.sha256((self.generation / ls.MANIFEST_NAME).read_bytes()).hexdigest(),
                         value["manifest_sha256"])
        self.assertEqual(".tartci-launch-v2", value["entrypoint"]["path"])
        shim = str(self.generation / "bin/python3")
        self.assertEqual({"pin": PIN, "shim": {"path": "bin/python3", "sha256": hashlib.sha256(
            (self.generation / "bin/python3").read_bytes()).hexdigest()}}, value["python"])
        # The lane's interpreter is rewritten to the shim; nothing else changes.
        self.assertEqual({**LANES["studio-pulp-gate"]["environment"], "TARTCI_PYTHON": shim},
                         value["lanes"]["studio-pulp-gate"]["environment"])

    def test_the_shim_execs_the_pin_isolated_and_the_entrypoint_uses_it(self) -> None:
        self.assertEqual(b'#!/bin/sh\nexec /usr/bin/python3 -B -E -s -S "$@"\n',
                         (self.generation / "bin/python3").read_bytes())
        entry = (self.generation / ls.ENTRYPOINT).read_text()
        self.assertIn(f"{self.generation}/bin/python3 {self.generation}/scripts/tartci_support_manifest.py", entry)
        self.assertNotIn("/usr/bin/python3", entry)
        # A Homebrew Cellar pin carries "@" and must be accepted.
        ls.shim_bytes("/opt/homebrew/Cellar/python@3.12/3.12.13_2/Frameworks/Python.framework/"
                      "Versions/3.12/bin/python3.12")
        for pin in ("python3", "/opt/x y/python3", "/opt/$(id)/python3", '/opt/"/python3'):
            with self.subTest(pin=pin), self.assertRaisesRegex(ls.LaunchSetError, "plain absolute"):
                ls.shim_bytes(pin)

    def test_a_lane_interpreter_other_than_the_pin_is_refused(self) -> None:
        for key in ("TARTCI_PYTHON", "SHIPYARD_GHAPP_PYTHON_BINARY"):
            lanes = {"x": {"environment": {"TART_HOME": STORE, key: "/opt/homebrew/bin/python3.12"}}}
            with self.subTest(key=key), self.assertRaisesRegex(ls.LaunchSetError, "not the pinned"):
                ls.build(self.generation, lanes, tart_home=STORE, python=PIN, sequence=1)

    def test_a_shim_or_entrypoint_not_matching_the_pin_is_refused(self) -> None:
        with self.assertRaisesRegex(ls.LaunchSetError, "bin/python3 is not the expected bytes"):
            ls.build(self.generation, LANES, tart_home=STORE, python="/opt/homebrew/bin/python3.11",
                     sequence=1)
        os.chmod(self.generation / ls.ENTRYPOINT, 0o755)
        (self.generation / ls.ENTRYPOINT).write_text("#!/bin/bash\nexec /usr/bin/python3\n")
        with self.assertRaisesRegex(ls.LaunchSetError, "tartci-launch-v2 is not the expected bytes"):
            ls.build(self.generation, LANES, tart_home=STORE, python=PIN, sequence=1)

    def test_sibling_imports_still_work_through_the_shim(self) -> None:
        # peer_stall_alert imports host_off from its own directory; -I (whose
        # -P drops that directory) would break it, the shim's flags must not.
        work = Path(os.path.realpath(self.td.name)) / "shimmed"
        work.mkdir()
        ls.write_runtime(work, sys.executable)
        script = HERE / "peer_stall_alert.py"
        result = subprocess.run([str(work / "bin/python3"), str(script)], capture_output=True,
                                text=True, timeout=120, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        if sys.version_info >= (3, 11):
            control = subprocess.run([sys.executable, "-P", "-S", str(script)], capture_output=True,
                                     text=True, timeout=120, check=False)
            self.assertNotEqual(0, control.returncode, "-P must break the sibling import")
            self.assertIn("ModuleNotFoundError", control.stderr)

    def test_a_lane_off_the_store_or_with_a_loader_variable_is_refused(self) -> None:
        for lanes in ({"x": {"environment": {"TART_HOME": "/Users/example/VMs"}}},
                      {"x": {"environment": {"TART_HOME": STORE, "DYLD_INSERT_LIBRARIES": "/x"}}},
                      {"../x": {"environment": {"TART_HOME": STORE}}}, {}):
            with self.subTest(lanes=lanes), self.assertRaises(ls.LaunchSetError):
                ls.build(self.generation, lanes, tart_home=STORE, python=PIN, sequence=1)
        for sequence in (0, -1, "2", 2.0, True):
            with self.subTest(sequence=sequence), self.assertRaisesRegex(ls.LaunchSetError, "sequence"):
                ls.build(self.generation, LANES, tart_home=STORE, python=PIN, sequence=sequence)

    def test_the_swift_requirement_is_the_python_requirement(self) -> None:
        # One source of truth in two languages: rebuild the launcher's
        # requirement from main.swift's string literals and compare.
        import re as _re
        swift = (Path(__file__).resolve().parents[1]
                 / "native/macos/tartci-launcher/main.swift").read_text()
        body = swift[swift.index("private func launchSetRequirement"):]
        body = body[body.index('return "anchor apple generic"'):]
        body = body[:body.index("\n}\n")]
        literals = _re.findall(r'"((?:[^"\\]|\\.)*)"', body)
        rebuilt = ("".join(literals).replace('\\"', '"')
                   .replace("\\(policy.team_id)", TEAM)
                   .replace("\\(launchSetIdentifier)", ls.LAUNCH_SET_IDENTIFIER))
        self.assertEqual(ls.requirement(TEAM), rebuilt)

    def test_the_requirement_pins_the_policy_team(self) -> None:
        self.assertEqual('anchor apple generic'
                         ' and certificate 1[field.1.2.840.113635.100.6.2.6] exists'
                         ' and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
                         ' and certificate leaf[subject.OU] = "AB12CD34EF"'
                         ' and identifier "com.danielraffel.tartci.launch-set"', ls.requirement(TEAM))
        for bad in ("ab12cd34ef", "AB12", 'X" or "1', ""):
            with self.subTest(bad=bad), self.assertRaises(ls.LaunchSetError):
                ls.requirement(bad)

    def test_signing_needs_the_dedicated_keychain(self) -> None:
        self.written()
        codesign = FakeCodesign()
        for keychain in (None, ""):
            with self.subTest(keychain=keychain), self.assertRaisesRegex(ls.LaunchSetError, "dedicated"):
                ls.sign(self.generation, identity="x", keychain=keychain, run=codesign)
        self.assertEqual([], codesign.calls)
        ls.sign(self.generation, identity="x", keychain="/k.keychain-db", run=codesign)
        argv = codesign.calls[0]
        self.assertEqual("/k.keychain-db", argv[argv.index("--keychain") + 1])
        self.assertEqual(ls.LAUNCH_SET_IDENTIFIER, argv[argv.index("--identifier") + 1])

    def test_a_keychain_that_is_not_ready_leaves_the_pointer_unchanged(self) -> None:
        older = self.written(make_generation(self.root, "b" * 40 + "-1"), sequence=1)
        self.publish(older)
        before = self.pointer.read_bytes()
        codesign = FakeCodesign()
        with self.assertRaisesRegex(ls.LaunchSetError, "not ready, pointer unchanged"):
            self.publish(self.written(), codesign, ready=lambda: "signing keychain is locked")
        self.assertEqual(before, self.pointer.read_bytes())
        self.assertEqual([], codesign.calls, "nothing signed")

    def test_a_failed_signature_leaves_the_pointer_unchanged(self) -> None:
        with self.assertRaisesRegex(ls.LaunchSetError, "signing failed"):
            self.publish(self.written(), FakeCodesign(sign_rc=1))
        self.assertFalse(self.pointer.exists())

    def test_publish_freezes_verifies_and_keeps_the_rollback_generation(self) -> None:
        older = self.written(make_generation(self.root, "b" * 40 + "-1"), sequence=1)
        self.assertEqual({"schema": 1, "generation": str(older), "previous": None}, self.publish(older))
        value = self.publish(self.written())
        self.assertEqual({"schema": 1, "generation": str(self.generation),
                          "previous": str(older)}, value)
        self.assertEqual(0o600, self.pointer.stat().st_mode & 0o777)
        for dirpath, dirs, files in os.walk(self.generation):
            for name in dirs + files:
                self.assertEqual(0, os.lstat(os.path.join(dirpath, name)).st_mode & 0o222, name)
        # Publishing the same generation again keeps the rollback target.
        self.assertEqual(str(older), self.publish(self.generation)["previous"])

    def test_the_v1_launch_file_of_every_generation_is_pinned_by_digest(self) -> None:
        self.written()
        value = json.loads((self.generation / ls.SET_NAME).read_text())
        self.assertEqual([".tartci-launch"], [f["path"] for f in value["files"]])
        ls.freeze(self.generation)
        self.assertIsNone(ls.verify_contents(self.generation, self.root))
        launch = self.generation / ".tartci-launch"
        os.chmod(self.generation, 0o755)
        os.chmod(launch, 0o755)
        launch.write_bytes(launch.read_bytes() + b"#")
        ls.freeze(self.generation)
        self.assertIn(".tartci-launch does not match its digest",
                      ls.verify_contents(self.generation, self.root))
        # Control: a set that does not pin it names it as unhashed.
        os.chmod(self.generation, 0o755)
        value["files"] = []
        ls.write(self.generation, value)
        ls.freeze(self.generation)
        self.assertIn(".tartci-launch is not in the signed manifest",
                      ls.verify_contents(self.generation, self.root))

    def test_contents_are_checked_against_every_digest(self) -> None:
        self.written()
        ls.freeze(self.generation)
        self.assertIsNone(ls.verify_contents(self.generation, self.root))
        for victim in ("tartci", "scripts/x.py", ls.ENTRYPOINT, ls.SHIM, ls.MANIFEST_NAME):
            with self.subTest(victim=victim):
                path = self.generation / victim
                original = path.read_bytes()
                os.chmod(path.parent, 0o755)
                os.chmod(path, 0o644)
                path.write_bytes(original + b"#")
                os.chmod(path, 0o444)
                self.assertIsNotNone(ls.verify_contents(self.generation, self.root))
                os.chmod(path, 0o644)
                path.write_bytes(original)
                os.chmod(path, 0o444)
                os.chmod(path.parent, 0o555)
        self.assertIsNone(ls.verify_contents(self.generation, self.root))

    def test_a_file_the_manifest_does_not_name_is_refused(self) -> None:
        self.written()
        ls.freeze(self.generation)
        cases = (("scripts/__pycache__/x.cpython-311.pyc", "bytecode cache"),
                 ("scripts/__pycache__/", "bytecode cache"),          # even empty
                 ("scripts/extra.py", "scripts/extra.py is not in the signed manifest"),
                 ("planted", "planted is not in the signed manifest"))
        for relative, why in cases:
            with self.subTest(relative=relative):
                target = self.generation / relative
                for directory in (self.generation, self.generation / "scripts"):
                    os.chmod(directory, 0o755)
                if relative.endswith("/"):
                    target.mkdir()
                else:
                    target.parent.mkdir(exist_ok=True)
                    target.write_bytes(b"\x00planted")
                self.assertIn(why, ls.verify_contents(self.generation, self.root) or "")
                if target.is_file():
                    target.unlink()
                if target.name == "__pycache__" or target.parent.name == "__pycache__":
                    (self.generation / "scripts/__pycache__").rmdir()
                ls.freeze(self.generation)
        self.assertIsNone(ls.verify_contents(self.generation, self.root))

    def test_a_python_start_up_variable_in_a_lane_is_refused(self) -> None:
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
            lanes = {"x": {"environment": {"TART_HOME": STORE, name: "/tmp"}}}
            with self.subTest(name=name), self.assertRaisesRegex(ls.LaunchSetError, name):
                ls.build(self.generation, lanes, tart_home=STORE, python=PIN, sequence=1)

    def test_a_fifo_in_the_generation_is_refused(self) -> None:
        self.written()
        os.mkfifo(self.generation / "scripts/pipe")
        ls.freeze(self.generation)
        self.assertEqual("scripts/pipe is not a regular file",
                         ls.verify_contents(self.generation, self.root))

    def test_an_unreadable_previous_generation_does_not_block_the_pointer(self) -> None:
        older = self.written(make_generation(self.root, "b" * 40 + "-1"), sequence=1)
        self.publish(older)
        os.chmod(older, 0o755)
        (older / ls.SET_NAME).unlink()      # damaged: its set is gone
        value = self.publish(self.written(sequence=2))
        self.assertEqual(str(self.generation), value["generation"])
        self.assertIsNone(value["previous"], "a damaged generation is not a rollback target")
        self.assertIn("treated as absent", value["note"])
        self.assertEqual(str(self.generation), ls.read_pointer(self.pointer)["generation"])

    def test_publish_refuses_a_sequence_that_does_not_advance(self) -> None:
        newer = self.written(make_generation(self.root, "c" * 40 + "-1"), sequence=5)
        self.publish(newer)
        before = self.pointer.read_bytes()
        for sequence in (5, 4):
            with self.subTest(sequence=sequence):
                with self.assertRaisesRegex(ls.LaunchSetError, "is not after 5, pointer unchanged"):
                    self.publish(self.written(sequence=sequence))
                self.assertEqual(before, self.pointer.read_bytes())

    def test_writable_or_symlinked_paths_are_untrusted(self) -> None:
        self.written()
        ls.freeze(self.generation)
        target = self.generation / "scripts/x.py"
        for path, mode in ((target, 0o464), (target, 0o446), (target.parent, 0o575),
                           (self.root, 0o775)):
            with self.subTest(path=path.name, mode=oct(mode)):
                before = path.stat().st_mode & 0o777
                os.chmod(path, mode)
                self.assertIn("writable", ls.trust_problem(target, self.root))
                os.chmod(path, before)
        self.assertIsNone(ls.trust_problem(target, self.root))
        os.chmod(self.generation, 0o755)
        link = self.generation / "link"
        link.symlink_to(target)
        self.assertIn("symlink", ls.trust_problem(link, self.root))
        self.assertIn("outside", ls.trust_problem(Path("/usr/bin/true"), self.root))

    def test_unsafe_member_paths_are_refused(self) -> None:
        self.written()
        manifest = json.loads((self.generation / ls.MANIFEST_NAME).read_text())
        manifest["members"].append({"path": "../escape", "sha256": "0" * 64})
        os.chmod(self.generation / ls.MANIFEST_NAME, 0o644)
        (self.generation / ls.MANIFEST_NAME).write_text(json.dumps(manifest))
        self.written()
        ls.freeze(self.generation)
        self.assertIn("unsafe member path", ls.verify_contents(self.generation, self.root))


class LaunchSetInstallTests(unittest.TestCase):
    """install(): what `fleet-macos install` runs for a v2 host after staging."""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.td.name))
        self.root = base / "generations"
        self.root.mkdir()
        os.chmod(self.root, 0o755)
        self.pointer = base / "config" / "launcher-generation.json"
        self.floor = base / "config" / "launcher-floor.json"
        self.render = base / "rendered"
        self.render.mkdir()
        self.write_plist("studio-pulp-gate")
        self.generation = self.staged("a" * 40 + "-1")

    def tearDown(self) -> None:
        for dirpath, dirs, _files in os.walk(self.td.name):
            for name in dirs:
                os.chmod(os.path.join(dirpath, name), 0o755)
        self.td.cleanup()

    def staged(self, name: str) -> Path:
        """A generation as stage-install leaves it: frozen, no v2 runtime."""
        generation = make_generation(self.root, name)
        (generation / ls.SHIM).unlink()
        (generation / "bin").rmdir()
        (generation / ls.ENTRYPOINT).unlink()
        ls.freeze(generation)
        return generation

    def write_plist(self, lane: str, **extra: str) -> None:
        import plistlib  # noqa: PLC0415
        env = {"TARTCI_QUEUE_LANE_ID": lane, "TART_HOME": STORE, "PATH": "/usr/bin:/bin",
               "TARTCI_PYTHON": PIN, **extra}
        (self.render / f"com.example.{lane}.plist").write_bytes(
            plistlib.dumps({"Label": lane, "EnvironmentVariables": env}))

    def install(self, generation: Path, codesign=None, keychain: str = "/k.keychain-db",
                ready=lambda: None) -> dict:
        return ls.install(generation, render_dir=self.render, tart_home=STORE, pin=PIN,
                          pointer=self.pointer, floor=self.floor, root=self.root, team_id=TEAM,
                          identity="Developer ID Application: Example", keychain=keychain,
                          signing_ready=ready, run=codesign or FakeCodesign())

    def signed(self, codesign: FakeCodesign) -> int:
        return sum(1 for call in codesign.calls if "--sign" in call)

    def test_install_writes_the_runtime_signs_the_set_and_points_at_it(self) -> None:
        value = self.install(self.generation)
        self.assertEqual(str(self.generation), value["generation"])
        self.assertEqual(str(self.generation), ls.read_pointer(self.pointer)["generation"])
        launch = json.loads((self.generation / ls.SET_NAME).read_text())
        self.assertEqual(1, launch["sequence"])
        self.assertEqual(["studio-pulp-gate"], sorted(launch["lanes"]))
        self.assertEqual(str(self.generation / ls.SHIM),
                         launch["lanes"]["studio-pulp-gate"]["environment"]["TARTCI_PYTHON"])
        self.assertEqual(ls.shim_bytes(PIN), (self.generation / ls.SHIM).read_bytes())
        self.assertIsNone(ls.verify_contents(self.generation, self.root))
        for dirpath, dirs, files in os.walk(self.generation):
            for name in dirs + files:
                self.assertEqual(0, os.lstat(os.path.join(dirpath, name)).st_mode & 0o222, name)

    def test_the_sequence_moves_past_the_floor_and_the_previous_set(self) -> None:
        self.floor.parent.mkdir(parents=True, exist_ok=True)
        self.floor.write_text(json.dumps({"sequence": 7, "previous_sequence": 6}))
        self.install(self.generation)
        self.assertEqual(8, json.loads((self.generation / ls.SET_NAME).read_text())["sequence"])
        newer = self.staged("b" * 40 + "-1")
        value = self.install(newer)
        self.assertEqual(9, json.loads((newer / ls.SET_NAME).read_text())["sequence"])
        self.assertEqual(str(self.generation), value["previous"])

    def test_reinstalling_the_pointed_generation_with_the_same_lanes_signs_nothing(self) -> None:
        self.install(self.generation)
        before = (self.generation / ls.SET_NAME).read_bytes()
        codesign = FakeCodesign()
        value = self.install(self.generation, codesign)
        self.assertTrue(value["unchanged"])
        self.assertEqual(0, self.signed(codesign))
        self.assertEqual(before, (self.generation / ls.SET_NAME).read_bytes())

    def test_reinstalling_with_changed_lanes_reissues_the_set_in_the_frozen_generation(self) -> None:
        self.install(self.generation)
        self.write_plist("studio-forge-gate")
        codesign = FakeCodesign()
        value = self.install(self.generation, codesign)
        self.assertNotIn("unchanged", value)
        self.assertEqual(1, self.signed(codesign))
        launch = json.loads((self.generation / ls.SET_NAME).read_text())
        self.assertEqual(2, launch["sequence"])
        self.assertEqual(["studio-forge-gate", "studio-pulp-gate"], sorted(launch["lanes"]))
        self.assertIsNone(ls.verify_contents(self.generation, self.root))

    def test_a_keychain_that_is_not_ready_writes_nothing(self) -> None:
        codesign = FakeCodesign()
        with self.assertRaisesRegex(ls.LaunchSetError, "not ready to sign it forward"):
            self.install(self.generation, codesign, ready=lambda: "locked")
        self.assertFalse(self.pointer.exists())
        self.assertFalse((self.generation / ls.SET_NAME).exists())
        self.assertFalse((self.generation / ls.ENTRYPOINT).exists())
        self.assertEqual([], codesign.calls)

    def test_a_failed_signature_leaves_the_pointer_and_freezes_the_generation(self) -> None:
        with self.assertRaisesRegex(ls.LaunchSetError, "signing failed"):
            self.install(self.generation, FakeCodesign(sign_rc=1))
        self.assertFalse(self.pointer.exists())
        self.assertEqual(0o555, self.generation.stat().st_mode & 0o777)

    # Rollback: switch back to `previous`, else sign forward, else stop.

    def rolled_forward(self) -> Path:
        """Install A then B; the pointer names B with A as previous."""
        self.install(self.generation)
        newer = self.staged("b" * 40 + "-1")
        self.install(newer)
        self.assertEqual(str(self.generation), ls.read_pointer(self.pointer)["previous"])
        return newer

    def test_a_rollback_switches_the_pointer_back_without_a_signature(self) -> None:
        newer = self.rolled_forward()
        codesign = FakeCodesign()
        value = self.install(self.generation, codesign, ready=lambda: "keychain locked")
        self.assertTrue(value["rolled_back"])
        self.assertEqual({"schema": 1, "generation": str(self.generation), "previous": str(newer)},
                         ls.read_pointer(self.pointer))
        self.assertEqual(0, self.signed(codesign))
        self.assertEqual(1, json.loads((self.generation / ls.SET_NAME).read_text())["sequence"])

    def test_a_rollback_whose_lanes_changed_signs_forward_when_signing_is_ready(self) -> None:
        newer = self.rolled_forward()
        self.write_plist("studio-forge-gate")
        codesign = FakeCodesign()
        value = self.install(self.generation, codesign)
        self.assertNotIn("rolled_back", value)
        self.assertEqual(1, self.signed(codesign))
        self.assertEqual(3, json.loads((self.generation / ls.SET_NAME).read_text())["sequence"])
        self.assertEqual(str(newer), ls.read_pointer(self.pointer)["previous"])

    def test_a_rollback_that_can_neither_switch_nor_sign_stops_with_the_operator_step(self) -> None:
        newer = self.rolled_forward()
        # The previous set no longer verifies: one byte of a member changed.
        member = self.generation / "scripts/x.py"
        os.chmod(member.parent, 0o755)
        os.chmod(member, 0o644)
        member.write_text("print(2)\n")
        ls.freeze(self.generation)
        before = self.pointer.read_bytes()
        codesign = FakeCodesign()
        with self.assertRaises(ls.LaunchSetError) as caught:
            self.install(self.generation, codesign, ready=lambda: "keychain locked")
        message = str(caught.exception)
        self.assertIn("cannot be reused (scripts/x.py does not match its digest)", message)
        self.assertIn("keychain locked", message)
        self.assertIn("Operator step", message)
        self.assertEqual(before, self.pointer.read_bytes())
        self.assertEqual(0, self.signed(codesign))
        self.assertEqual(str(newer), ls.read_pointer(self.pointer)["generation"])

    def test_a_generation_two_back_is_never_pointed_at_without_signing(self) -> None:
        # A, B, C installed in turn: A's signed set still verifies, but it is
        # two back, so the launcher's floor would refuse it; it is signed
        # forward or not at all.
        newer = self.rolled_forward()
        newest = self.staged("c" * 40 + "-1")
        self.install(newest)
        self.assertEqual(str(newer), ls.read_pointer(self.pointer)["previous"])
        before = self.pointer.read_bytes()
        with self.assertRaisesRegex(ls.LaunchSetError, "not the pointer's previous generation"):
            self.install(self.generation, ready=lambda: "keychain locked")
        self.assertEqual(before, self.pointer.read_bytes())
        codesign = FakeCodesign()
        value = self.install(self.generation, codesign)
        self.assertNotIn("rolled_back", value)
        self.assertEqual(1, self.signed(codesign))
        self.assertEqual(4, json.loads((self.generation / ls.SET_NAME).read_text())["sequence"])

    def test_only_the_previous_generation_is_switched_back_to(self) -> None:
        self.rolled_forward()
        older = self.staged("c" * 40 + "-1")
        with self.assertRaisesRegex(ls.LaunchSetError, "not the pointer's previous generation"):
            self.install(older, ready=lambda: "keychain locked")

    def test_rendered_lanes_must_be_unique_and_present(self) -> None:
        import plistlib  # noqa: PLC0415
        (self.render / "dup.plist").write_bytes(plistlib.dumps(
            {"EnvironmentVariables": {"TARTCI_QUEUE_LANE_ID": "studio-pulp-gate"}}))
        with self.assertRaisesRegex(ls.LaunchSetError, "rendered twice"):
            ls.lanes_from_plists(self.render)
        empty = self.render / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(ls.LaunchSetError, "no lane LaunchAgents"):
            ls.lanes_from_plists(empty)

    def test_the_keychain_must_be_a_keychain_file(self) -> None:
        self.assertIn("no dedicated", ls.keychain_problem(None))
        self.assertIn("not a keychain", ls.keychain_problem(str(self.render / "x.keychain-db")))
        keychain = self.render / "signing.keychain-db"
        keychain.write_text("")
        self.assertIsNone(ls.keychain_problem(str(keychain)))

    def test_signing_is_ready_only_when_a_probe_signature_succeeds(self) -> None:
        keychain = self.render / "signing.keychain-db"
        keychain.write_text("")
        calls = []

        def probe(rc=0, raises=False):
            def run(argv, **kwargs):
                calls.append(list(argv))
                if raises:
                    raise subprocess.TimeoutExpired(argv, 60)
                return subprocess.CompletedProcess(argv, rc, "", "" if rc == 0 else "locked")
            return run
        self.assertIsNone(ls.signing_problem("ID", str(keychain), probe()))
        self.assertEqual(["--keychain", str(keychain)], calls[0][3:5])
        self.assertIn("--timestamp", calls[0])
        self.assertIn("probe failed: locked", ls.signing_problem("ID", str(keychain), probe(1)))
        self.assertIn("timed out", ls.signing_problem("ID", str(keychain), probe(raises=True)))
        self.assertIn("not a keychain", ls.signing_problem("ID", str(self.render / "x"), probe()))

    def test_the_command_refuses_without_a_keychain_file(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HERE / "launch_set.py"), "install",
             "--generation", str(self.generation), "--render-dir", str(self.render),
             "--tart-home", STORE, "--pin", PIN, "--pointer", str(self.pointer),
             "--floor", str(self.floor), "--root", str(self.root), "--team-id", TEAM,
             "--identity", "X", "--keychain", str(self.render / "missing.keychain-db")],
            capture_output=True, text=True, check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("not a keychain file", result.stderr)
        self.assertFalse(self.pointer.exists())


if __name__ == "__main__":
    unittest.main()
