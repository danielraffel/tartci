#!/usr/bin/env python3
"""python_child: the interpreter TartCI Python children start with."""

from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import python_child  # noqa: E402


class ChildPythonTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TARTCI_PYTHON", None)
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

    def test_unset_or_a_command_name_keeps_this_interpreter(self) -> None:
        self.assertEqual(sys.executable, python_child.child_python())
        os.environ["TARTCI_PYTHON"] = "python3.11"
        self.assertEqual(sys.executable, python_child.child_python())

    def test_v1_resolves_the_interpreter_it_did_before(self) -> None:
        # v1 sets TARTCI_PYTHON to the pinned interpreter this process runs.
        os.environ["TARTCI_PYTHON"] = sys.executable
        self.assertEqual(sys.executable, python_child.child_python())

    def test_an_absolute_tartci_python_is_the_child_interpreter(self) -> None:
        shim = Path(self.td.name) / "python3"
        shim.write_text("#!/bin/sh\nexec true\n")
        shim.chmod(0o755)
        os.environ["TARTCI_PYTHON"] = str(shim)
        self.assertEqual(str(shim), python_child.child_python())

    def test_an_absolute_tartci_python_that_cannot_run_is_refused(self) -> None:
        missing = Path(self.td.name) / "missing"
        plain = Path(self.td.name) / "plain"
        plain.write_text("x")
        plain.chmod(0o644)
        for value in (missing, plain, Path(self.td.name)):
            with self.subTest(value=str(value)):
                os.environ["TARTCI_PYTHON"] = str(value)
                with self.assertRaisesRegex(RuntimeError, "not an executable file"):
                    python_child.child_python()


class CallSiteTests(unittest.TestCase):
    """The call sites start their children with child_python()."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.shim = Path(self.td.name) / "python3"
        self.shim.write_text("#!/bin/sh\nexec true\n")
        self.shim.chmod(0o755)
        os.environ["TARTCI_PYTHON"] = str(self.shim)

    def test_disk_watchdog_probe_runs_under_the_child_interpreter(self) -> None:
        import disk_watchdog
        os.environ.pop("TARTCI_DISK_PROBE_COMMAND", None)
        self.assertEqual(str(self.shim), disk_watchdog.probe_command("/tmp")[0])


def executable_uses(tree: ast.AST) -> list[int]:
    """Lines that read sys.executable anywhere but inside child_python()."""
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "child_python":
            allowed |= {n.lineno for n in ast.walk(node) if hasattr(n, "lineno")}
    return sorted(node.lineno for node in ast.walk(tree)
                  if isinstance(node, ast.Attribute) and node.attr == "executable"
                  and isinstance(node.value, ast.Name) and node.value.id == "sys"
                  and node.lineno not in allowed)


def support_python_files() -> list[Path]:
    return sorted(p for p in (ROOT / "scripts").glob("*.py") if not p.name.startswith("test_"))


SHELL_EXECUTABLE = re.compile(r"\bsys\.executable\b")


class ChildPythonLintTests(unittest.TestCase):
    """No support code starts a Python child with sys.executable: under launcher
    v2 that is the pinned interpreter without the shim's isolation."""

    def test_no_support_python_file_uses_sys_executable(self) -> None:
        offenders = []
        for path in support_python_files():
            for line in executable_uses(ast.parse(path.read_text(), str(path))):
                offenders.append(f"{path.relative_to(ROOT)}:{line}")
        self.assertEqual([], offenders, "start Python children with python_child.child_python()")

    def test_no_python_embedded_in_shell_uses_sys_executable(self) -> None:
        offenders = []
        for path in [ROOT / "tartci", *(ROOT / "providers").rglob("*.sh"),
                     *(ROOT / "scripts").glob("*.sh")]:
            for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if SHELL_EXECUTABLE.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual([], offenders, "start Python children with python_child.child_python()")

    def test_the_lint_finds_a_planted_use(self) -> None:
        # Positive control: argv[0] and a symlink target are both caught; the
        # helper itself is not.
        planted = ("import os, subprocess, sys\n"
                   "subprocess.run([sys.executable, '-c', 'pass'])\n"
                   "os.symlink(sys.executable, '/tmp/x')\n"
                   "def child_python():\n"
                   "    return sys.executable\n")
        self.assertEqual([2, 3], executable_uses(ast.parse(planted)))
        self.assertEqual([], executable_uses(ast.parse((HERE / "python_child.py").read_text())))
        self.assertTrue(SHELL_EXECUTABLE.search(
            'out = subprocess.check_output([sys.executable, "x.py"])'))


if __name__ == "__main__":
    unittest.main()
