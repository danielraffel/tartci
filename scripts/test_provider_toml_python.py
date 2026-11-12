#!/usr/bin/env python3
"""Provider scripts run their tomllib helpers under a Python that has it.

An operator runs the golden provisioners directly over ssh. On m1 the login
shell's `python3` is /usr/bin/python3 3.9.6, which has no tomllib, so
`pulp-source-pin.py` and `pulp-macos-readiness.py`, run with a bare `python3`,
died with `ModuleNotFoundError: No module named 'tomllib'`. They now go through
`tartci_toml_python` from providers/common/toml-python.lib.sh, the resolver the
tartci shim uses.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "providers" / "common"


def toml_helpers() -> list[str]:
    return sorted(path.name for path in COMMON.glob("*.py")
                  if re.search(r"^\s*import tomllib\b", path.read_text(), re.M))


def provider_scripts() -> list[Path]:
    return sorted((ROOT / "providers").rglob("*.sh"))


def bare_calls(text: str, helper: str) -> list[int]:
    """Lines where `text` runs `helper` with python3 itself, bare or by path, not the resolver."""
    names = re.findall(r'^\s*([A-Z_]+)="[^"\n]*/' + re.escape(helper) + '"', text, re.M)
    lines = []
    for target in [re.escape(helper)] + [r"\$\{?" + name + r"\}?" for name in names]:
        for match in re.finditer(r'(?<![\w.-])python3 "?[^"\s]*' + target, text):
            lines.append(text.count("\n", 0, match.start()) + 1)
    return sorted(lines)


class StaticTests(unittest.TestCase):
    @staticmethod
    def _assert_exec_tail_positions(source: str) -> None:
        fleet_start = source.index("cmd_fleet_macos() {")
        fleet_end = source.index("\ncmd_support_manifest()", fleet_start)
        fleet_lines = set(source[fleet_start:fleet_end].splitlines())
        for line in source.splitlines():
            if "tartci_toml_python_exec" not in line:
                continue
            if line in fleet_lines:
                continue
            if line.lstrip().startswith("held)"):
                continue
            raise AssertionError(f"exec helper outside a tail dispatcher: {line}")

    def test_no_provider_script_runs_a_toml_helper_with_a_bare_python3(self) -> None:
        helpers = toml_helpers()
        # Control: the two helpers this guards are found.
        self.assertIn("pulp-source-pin.py", helpers)
        self.assertIn("pulp-macos-readiness.py", helpers)
        bare = [f"{script.relative_to(ROOT)}:{line}: {helper}"
                for script in provider_scripts() for helper in helpers
                for line in bare_calls(script.read_text(), helper)]
        self.assertEqual(bare, [])

    def test_the_scan_sees_a_bare_call_by_variable_and_by_path(self) -> None:
        text = ('PIN="$ROOT/providers/common/pulp-source-pin.py"\n'
                'x="$(python3 "$PIN" "$M")"\n'
                'python3 "$ROOT/providers/common/pulp-source-pin.py" m\n'
                'tartci_toml_python "$PIN" "$M"\n'
                '/usr/bin/python3 "${PIN}" m\n')
        self.assertEqual(bare_calls(text, "pulp-source-pin.py"), [2, 3, 5])

    def test_exec_variant_is_used_only_by_tail_dispatchers(self) -> None:
        source = (ROOT / "tartci").read_text()
        self._assert_exec_tail_positions(source)

    def test_tail_dispatchers_require_exec_variant(self) -> None:
        source = (ROOT / "tartci").read_text()

        def assert_required(text: str) -> None:
            fleet = text[text.index("cmd_fleet_macos() {"):text.index("\ncmd_support_manifest()", text.index("cmd_fleet_macos() {"))]
            self.assertRegex(fleet, r"validate\|render\|write-receipt\|verify-installed\|verify-loaded\|advertised-labels\|profile-drift\|verify-supply\|config-verdicts\)[\s\S]*?tartci_toml_python_exec")
            for sub in ("self-update", "tool-freshness", "support-agents"):
                start = fleet.index(f"{sub})")
                end = fleet.index(";;", start)
                self.assertIn("tartci_toml_python_exec", fleet[start:end])
            held = text[text.index("held) shift;"):text.index(";;", text.index("held) shift;"))]
            self.assertIn("tartci_toml_python_exec", held)

        assert_required(source)
        mutant = source.replace(
            'self-update)\n      tartci_toml_python_exec',
            'self-update)\n      tartci_toml_python',
            1,
        )
        with self.assertRaises(AssertionError):
            assert_required(mutant)

    def test_grouped_tail_site_check_is_case_bounded_and_kills_g6(self) -> None:
        source = (ROOT / "tartci").read_text()

        def assert_grouped_tail(text: str) -> None:
            fleet_start = text.index("cmd_fleet_macos() {")
            fleet_end = text.index("\ncmd_support_manifest()", fleet_start)
            fleet = text[fleet_start:fleet_end]
            case_start = fleet.index("validate|render|write-receipt|verify-installed|verify-loaded|advertised-labels|profile-drift|verify-supply|config-verdicts)")
            case_end = fleet.index(";;", case_start)
            self.assertIn("tartci_toml_python_exec", fleet[case_start:case_end])

        assert_grouped_tail(source)
        mutant = source.replace(
            "validate|render|write-receipt|verify-installed|verify-loaded|advertised-labels|profile-drift|verify-supply|config-verdicts)\n      tartci_toml_python_exec",
            "validate|render|write-receipt|verify-installed|verify-loaded|advertised-labels|profile-drift|verify-supply|config-verdicts)\n      tartci_toml_python",
            1,
        )
        with self.assertRaises(AssertionError):
            assert_grouped_tail(mutant)

    def test_exec_placement_guard_kills_pool_on_mutants(self) -> None:
        source = (ROOT / "tartci").read_text()
        probe = ('tartci_toml_python "$HERE/scripts/macos_fleet_lanes.py" \\\n'
                 '              probe-launch-helper')
        verify = ('tartci_toml_python "$HERE/scripts/macos_fleet_lanes.py" verify-loaded')
        for old in (probe, verify):
            with self.subTest(mutant=old):
                mutant = source.replace(old, old.replace(
                    "tartci_toml_python ", "tartci_toml_python_exec "), 1)
                with self.assertRaises(AssertionError):
                    self._assert_exec_tail_positions(mutant)


class OperatorShellTests(unittest.TestCase):
    """`provision.sh pulp-readiness` under a PATH whose python3 lacks tomllib."""

    def run_readiness(self, toml_python: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as td:
            bindir = Path(td) / "bin"
            bindir.mkdir()
            fake = bindir / "python3"
            # A 3.9 stand-in: no tomllib, and it says so if it is ever run.
            fake.write_text("#!/bin/sh\n"
                            "case \"$*\" in *'import tomllib'*) exit 1;; esac\n"
                            "echo 'FAKE-PY39: ModuleNotFoundError: tomllib' >&2; exit 1\n")
            fake.chmod(0o755)
            for versioned in ("python3.11", "python3.12"):
                stub = bindir / versioned
                stub.write_text("#!/bin/sh\nexit 1\n")
                stub.chmod(0o755)
            env = {**os.environ, "PATH": f"{bindir}:/bin:/usr/bin",
                   "TARTCI_PYTHON": toml_python, "HOME": td}
            return subprocess.run(
                ["bash", str(ROOT / "providers" / "tart-macos" / "provision.sh"),
                 "pulp-readiness"], env=env, capture_output=True, text=True, timeout=60)

    def test_readiness_runs_under_the_resolved_toml_python(self) -> None:
        if sys.version_info < (3, 11):
            self.skipTest("needs a Python 3.11+ to resolve to")
        result = self.run_readiness(sys.executable)
        self.assertNotIn("FAKE-PY39", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        # The reporter read the TOML manifest: its report names the pinned source.
        self.assertIn('"pulp_commit"', result.stdout)
        self.assertIn('"status": "unready"', result.stdout)

    def test_shared_wrapper_returns_to_a_pool_on_shaped_script(self) -> None:
        # Use a stand-in so this contract is exercised on the Python 3.9 CI
        # lane too; the real 3.9 interpreter is correctly rejected because it
        # has no tomllib.
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "python-tomllib"
            fake.write_text(
                "#!/bin/sh\n"
                "case \"$2\" in *'import tomllib'*) exit 0;; esac\n"
                "case \"$2\" in *'print(\"child\")'*) printf 'child\\n';; esac\n"
            )
            fake.chmod(0o755)
            script = (f"source {COMMON / 'toml-python.lib.sh'}\n"
                      f"TARTCI_PYTHON={fake} tartci_toml_python -c 'print(\"child\")'\n"
                      "printf 'continued\\n'\n")
            result = subprocess.run(["bash", "-c", script], capture_output=True,
                                    text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["child", "continued"])

    def test_no_toml_python_names_the_fix(self) -> None:
        result = self.run_readiness("")
        if Path("/opt/homebrew/bin/python3").exists() or Path("/usr/local/bin/python3").exists():
            # The resolver also tries the Homebrew paths, which a dev Mac has.
            self.skipTest("a Homebrew python3 on this machine would be resolved")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("FAKE-PY39", result.stderr)
        self.assertIn("set TARTCI_PYTHON", result.stderr)

    def test_sigterm_to_wrapper_pid_reaches_the_python_child(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            marker = root / "handled"
            fake = root / "python3.12"
            fake.write_text("#!/bin/sh\n"
                            "if [ \"$1\" = -c ]; then exit 0; fi\n"
                            f"trap 'printf handled > {marker}; exit 42' TERM\n"
                            "printf ready\n"
                            "while :; do sleep 1; done\n")
            fake.chmod(0o755)
            wrapper = root / "wrapper.sh"
            wrapper.write_text("#!/bin/bash\n"
                              f"source {COMMON / 'toml-python.lib.sh'}\n"
                              f"TARTCI_PYTHON={fake} tartci_toml_python_exec ignored\n")
            wrapper.chmod(0o755)
            process = subprocess.Popen([str(wrapper)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True,
                                       start_new_session=True)
            try:
                self.assertEqual(process.stdout.read(5), "ready")
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 42)
                self.assertEqual(marker.read_text(), "handled")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
