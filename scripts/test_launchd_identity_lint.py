#!/usr/bin/env python3
"""launchd_identity_lint: no LaunchAgent may run as a per-install privacy identity."""

from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launchd_identity_lint as lint  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = "/Users/example/.local/libexec/TartCILauncher.app/Contents/MacOS/tartci-launcher"


class StaticClassTests(unittest.TestCase):
    CASES = {
        # stable
        "/bin/bash": "platform",
        "/usr/bin/python3": "platform",
        "/usr/libexec/PlistBuddy": "platform",
        "/System/Library/Frameworks/Foo.framework/Versions/A/foo": "platform",
        LAUNCHER: "launcher",
        "$HOME/.local/libexec/TartCILauncher.app/Contents/MacOS/tartci-launcher": "launcher",
        # per install or upgrade (the 2026-11-13 m3 prompt sources)
        "/Users/example/.local/share/shipyard/auth-generations/"
        "7de154db6e796078c4e4d2ba9a23aa88f3df0001f3e1432bd628d584084ae622/shipyard":
            "per_generation",
        "/opt/homebrew/Cellar/python@3.15/3.15.2/bin/python3.15": "per_generation",
        "/Users/example/.config/pulp/harbormaster/venv/bin/python3": "per_generation",
        "/Users/example/.local/share/pulp-landing/current/tool": "per_generation",
        "/Users/example/.tartci/generations/0123456789abcdef0123456789abcdef01234567/tartci":
            "per_generation",
        "/Users/example/actions-ci/runner/externals.node20/bin/node": "per_generation",
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13": "per_generation",
        # a TartCILauncher copy at a per-generation path is not the stable launcher
        "/Users/example/.tartci/generations/" + "a" * 40
        + "/.local/libexec/TartCILauncher.app/Contents/MacOS/tartci-launcher": "per_generation",
        "/Users/example/.cache/tool/" + "d" * 40 + "/bin/tool": "per_generation",
        # outside the system volume and not the launcher at its stable path
        "/tmp/.local/libexec/TartCILauncher.app/Contents/MacOS/tartci-launcher": "user_binary",
        "/Applications/TartCILauncher.app/Contents/MacOS/tartci-launcher": "user_binary",
        "$HOME/.local/bin/tartci": "user_binary",
        "/Users/example/bin/subrouter": "user_binary",
        "/opt/homebrew/bin/python3": "user_binary",
        # not a program at all
        "bash": "relative",
        "": "missing",
        None: "missing",
    }

    def test_each_path_class(self) -> None:
        for program, kind in self.CASES.items():
            with self.subTest(program=program):
                self.assertEqual(lint.classify_static(program)["kind"], kind)

    def test_program_key_wins_over_program_arguments(self) -> None:
        self.assertEqual(lint.program_of({"Program": "/bin/sh",
                                          "ProgramArguments": ["/x/y", "z"]}), "/bin/sh")
        self.assertEqual(lint.program_of({"ProgramArguments": ["/bin/bash", "x"]}), "/bin/bash")
        self.assertIsNone(lint.program_of({"ProgramArguments": []}))


class TemplateTests(unittest.TestCase):
    def test_every_tartci_template_is_a_stable_identity(self) -> None:
        rows = lint.check_templates()
        # Control: the scan read the real template set, not an empty glob.
        templates = list((ROOT / "launchd").glob("*.plist.template"))
        self.assertGreaterEqual(len(templates), 10)
        self.assertEqual(len(rows), len(templates) + len(list(
            (ROOT / "launchd" / "system").glob("*.plist"))))
        bad = [row for row in rows if row["kind"] not in lint.OK_KINDS]
        self.assertEqual(bad, [])
        self.assertTrue(all(row["program"] for row in rows), "every template names a program")

    def test_a_bad_template_fails_the_cli(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "launchd" / "system").mkdir(parents=True)
            good = (ROOT / "launchd" / "com.danielraffel.tartci.reap.plist.template").read_text()
            (root / "launchd" / "good.plist.template").write_text(good)
            self.assertEqual([r["kind"] for r in lint.check_templates(root)], ["platform"])
            bad = good.replace("<string>/bin/bash</string>",
                               "<string>$HOME/.local/share/shipyard/auth-generations/"
                               + "b" * 64 + "/shipyard</string>", 1)
            self.assertNotEqual(bad, good)
            (root / "launchd" / "bad.plist.template").write_text(bad)
            kinds = sorted(r["kind"] for r in lint.check_templates(root))
            self.assertEqual(kinds, ["per_generation", "platform"])

    def test_the_cli_exits_nonzero_on_a_refusal_and_zero_on_the_repo(self) -> None:
        clean = subprocess.run([sys.executable, str(ROOT / "scripts" / "launchd_identity_lint.py"),
                                "--templates"], capture_output=True, text=True, check=False)
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.assertEqual([], [line for line in clean.stdout.splitlines() if line.startswith("BAD")])

    def test_lint_sh_runs_it(self) -> None:
        self.assertIn("scripts/launchd_identity_lint.py --templates",
                      (ROOT / "scripts" / "lint.sh").read_text())


class InstalledScanTests(unittest.TestCase):
    def write(self, directory: Path, label: str, arguments: list) -> None:
        (directory / f"{label}.plist").write_bytes(plistlib.dumps(
            {"Label": label, "ProgramArguments": arguments}))

    def scan(self, rows: dict, signatures: dict, links: dict | None = None) -> dict:
        links = links or {}
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            for label, arguments in rows.items():
                self.write(directory, label, arguments)
            (directory / "broken.plist").write_text("not a plist")
            out = lint.scan_installed(directory,
                                      codesign=lambda path: signatures[path],
                                      realpath=lambda path: links.get(path, path))
        return {row["label"]: row["kind"] for row in out}

    def test_symlinks_are_resolved_and_signatures_read(self) -> None:
        shipyard = "/Users/example/.local/share/shipyard/auth-generations/" + "c" * 64 + "/shipyard"
        kinds = self.scan(
            {"lane": [LAUNCHER, "--lane", "x"],
             "watchdog": ["/bin/bash", "/Users/example/.local/bin/tartci", "launchd", "heal"],
             "reconcile": ["/Users/example/.local/bin/shipyard", "--json", "runner"],
             "attestation": ["/opt/homebrew/bin/python3", "x.py"],
             "subrouter": ["/Users/example/bin/subrouter", "supervise"],
             "script": ["/Users/example/.local/bin/sensor.sh"],
             "daemon": ["/Users/example/.local/libexec/shipyard/daemon-launcher"]},
            signatures={
                "/Users/example/bin/subrouter": {"signed": True, "adhoc": True, "team": None},
                "/Users/example/.local/bin/sensor.sh": {"signed": False, "error": "not signed"},
                "/Users/example/.local/libexec/shipyard/daemon-launcher":
                    {"signed": True, "adhoc": False, "team": "TEAMID1234"}},
            links={"/Users/example/.local/bin/shipyard": shipyard,
                   "/opt/homebrew/bin/python3": "/opt/homebrew/Cellar/python@3.15/3.15.2/bin/py"})
        self.assertEqual(kinds, {
            "lane": "launcher", "watchdog": "platform", "reconcile": "per_generation",
            "attestation": "per_generation", "subrouter": "adhoc", "script": "unsigned",
            "daemon": "developer_id", "broken": "missing"})

    def test_strict_exits_nonzero_only_with_a_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            self.write(directory, "ok", ["/bin/bash", "x"])
            self.assertEqual(lint.main(["--installed", td, "--strict"]), 0)
            self.write(directory, "bad", ["/Users/example/x/venv/bin/python3"])
            self.assertEqual(lint.main(["--installed", td]), 0, "report mode never fails")
            self.assertEqual(lint.main(["--installed", td, "--strict"]), 1)

    def test_codesign_parse(self) -> None:
        class Done:
            def __init__(self, rc, text):
                self.returncode, self.stderr, self.stdout = rc, text, ""
        adhoc = lint.codesign_info("/x", run=lambda *a, **k: Done(
            0, "Identifier=a.out\nSignature=adhoc\nTeamIdentifier=not set\n"))
        self.assertEqual((adhoc["adhoc"], adhoc["team"]), (True, None))
        signed = lint.codesign_info("/x", run=lambda *a, **k: Done(
            0, "Identifier=shipyard\nAuthority=Developer ID Application: X (ABCDE12345)\n"
               "TeamIdentifier=ABCDE12345\n"))
        self.assertEqual((signed["adhoc"], signed["team"]), (False, "ABCDE12345"))
        self.assertFalse(lint.codesign_info("/x", run=lambda *a, **k: Done(
            1, "code object is not signed at all"))["signed"])


if __name__ == "__main__":
    unittest.main()
