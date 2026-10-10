#!/usr/bin/env python3
"""tartci's own commands read the Tart store the lanes use, and say which.

Over ssh to m1 on 2026-10-09, with no TART_HOME in the shell, `tartci doctor
--reap --json` read Tart's default store and reported 0 running VMs and 2
free slots while two gate VMs ran in the lanes' store.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import tart_home  # noqa: E402
import testing_support  # noqa: E402

PROFILE = '[host]\nid = "m1"\ntart_home = "/Users/x/VMs"\n'


def write_profile(home: Path, text: str = PROFILE) -> Path:
    path = home / ".config/tartci/macos-fleet-profile.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.home = Path(self.td.name)

    @testing_support.requires_tomllib
    def test_an_ssh_shell_without_tart_home_reads_the_profile_store(self) -> None:
        write_profile(self.home)
        value = tart_home.resolve({}, self.home)
        self.assertEqual((value["path"], value["source"]), ("/Users/x/VMs", "profile"))

    @testing_support.requires_tomllib
    def test_an_explicit_tart_home_wins_and_a_mismatch_is_named(self) -> None:
        write_profile(self.home)
        value = tart_home.resolve({"TART_HOME": "/tmp/other"}, self.home)
        self.assertEqual((value["path"], value["source"]), ("/tmp/other", "env"))
        self.assertIn("differs from the fleet profile", value["warning"])
        same = tart_home.resolve({"TART_HOME": "/Users/x/VMs"}, self.home)
        self.assertNotIn("warning", same)

    @testing_support.requires_tomllib
    def test_the_dispatcher_export_keeps_the_profile_as_its_source(self) -> None:
        write_profile(self.home)
        value = tart_home.resolve({"TART_HOME": "/Users/x/VMs",
                                   "TARTCI_TART_HOME_SOURCE": "profile"}, self.home)
        self.assertEqual(value["source"], "profile")

    def test_no_profile_is_tart_default_and_says_so(self) -> None:
        value = tart_home.resolve({}, self.home)
        self.assertEqual((value["path"], value["source"]), (str(self.home / ".tart"), "default"))
        self.assertIn("no fleet profile", value["detail"])

    @testing_support.requires_tomllib
    def test_a_profile_without_the_key_or_without_tomllib_is_default(self) -> None:
        write_profile(self.home, '[host]\nid = "m1"\n')
        self.assertIn("declares no [host].tart_home",
                      tart_home.resolve({}, self.home)["detail"])
        write_profile(self.home)
        with mock.patch.object(tart_home, "tomllib", None):
            value = tart_home.resolve({}, self.home)
        self.assertEqual(value["source"], "default")
        self.assertIn("no tomllib", value["detail"])

    @testing_support.requires_tomllib
    def test_the_profile_path_override_is_honoured(self) -> None:
        other = self.home / "elsewhere.toml"
        other.write_text('[host]\ntart_home = "/Volumes/Store/VMs"\n')
        value = tart_home.resolve({"TARTCI_MACOS_FLEET_PROFILE": str(other)}, self.home)
        self.assertEqual(value["path"], "/Volumes/Store/VMs")


def dispatcher_function(name: str) -> str:
    source = (ROOT / "tartci").read_text()
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group(0)


class DispatcherTests(unittest.TestCase):
    def run_resolve(self, home: Path, env: dict[str, str]) -> str:
        script = (
            f"HERE={str(ROOT)!r}\n"
            f"tartci_toml_python_path(){{ echo {sys.executable!r}; }}\n"
            + dispatcher_function("tartci_resolve_tart_home") + "\n"
            "tartci_resolve_tart_home\n"
            'echo "TART_HOME=${TART_HOME:-unset} SOURCE=${TARTCI_TART_HOME_SOURCE:-unset}"\n'
            "/usr/bin/env | grep '^TART_HOME=' || echo 'not exported'\n")
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("TART_HOME", "TARTCI_TART_HOME_SOURCE",
                              "TARTCI_MACOS_FLEET_PROFILE")}
        result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                                env={**clean, "HOME": str(home), **env}, check=True)
        return result.stdout

    @testing_support.requires_tomllib
    def test_an_unset_tart_home_is_exported_from_the_profile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_profile(Path(td))
            out = self.run_resolve(Path(td), {})
        self.assertIn("TART_HOME=/Users/x/VMs SOURCE=profile", out)
        self.assertIn("\nTART_HOME=/Users/x/VMs\n", "\n" + out, "children must inherit it")

    def test_a_set_tart_home_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_profile(Path(td))
            out = self.run_resolve(Path(td), {"TART_HOME": "/tmp/mine"})
        self.assertIn("TART_HOME=/tmp/mine SOURCE=env", out)

    def test_without_a_profile_nothing_is_exported(self) -> None:
        # Control, same instrument: only the profile is missing.
        with tempfile.TemporaryDirectory() as td:
            out = self.run_resolve(Path(td), {})
        self.assertIn("TART_HOME=unset SOURCE=default", out)
        self.assertIn("not exported", out)

    def test_doctor_and_observe_resolve_before_any_tart_call(self) -> None:
        doctor = dispatcher_function("cmd_doctor")
        self.assertLess(doctor.index("tartci_resolve_tart_home"), doctor.index('"--reap"'),
                        "doctor --reap (Shipyard's health probe) must read the lanes' store")
        self.assertIn("tartci_resolve_tart_home", dispatcher_function("cmd_observe"))
        self.assertIn("scripts/tart_home.py", doctor, "bare doctor prints the store")


class VmReapTests(unittest.TestCase):
    @testing_support.requires_tomllib
    def test_a_direct_run_exports_the_profile_store_for_its_tart_calls(self) -> None:
        import vm_reap
        with tempfile.TemporaryDirectory() as td:
            write_profile(Path(td))
            env = {k: v for k, v in os.environ.items()
                   if k not in ("TART_HOME", "TARTCI_TART_HOME_SOURCE",
                                "TARTCI_MACOS_FLEET_PROFILE")}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(Path, "home", return_value=Path(td)):
                store = vm_reap.ensure_tart_home()
                exported = os.environ.get("TART_HOME")
        self.assertEqual(store["source"], "profile")
        self.assertEqual(exported, "/Users/x/VMs")


if __name__ == "__main__":
    unittest.main()
