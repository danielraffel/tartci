#!/usr/bin/env python3
"""Receipt checks that must not turn routine host events into outages.

* macOS updates replace /usr/bin/python3 (m1, m3: 2026-09-25). The receipt
  mismatch is named interpreter_changed_by_os_update with its remedy, but
  only for the OS-managed interpreter with OS-update evidence; anything else
  stays an ordinary fail-closed mismatch.
* `pool on` kickstarts a persistent Actions service and then verifies it is
  running; a single immediate read raced the start and failed pool on (m5).
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import macos_fleet_lanes as fleet

OLD = {"path": "/usr/bin/python3", "owner_uid": 0, "mode": 0o755, "sha256": "b8763cf2"}
NEW = {**OLD, "sha256": "34129c71"}


class InterpreterClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.receipt = Path(self.td.name) / "receipt.json"
        self.version = Path(self.td.name) / "SystemVersion.plist"
        self.addCleanup(self.td.cleanup)

    def write(self, *, build: str | None, current_build: str) -> None:
        support = {"os_build": build} if build else {}
        self.receipt.write_text(json.dumps({"support": support}))
        self.version.write_bytes(plistlib.dumps({"ProductBuildVersion": current_build}))

    def evidence(self, recorded=OLD, current=NEW):
        with mock.patch.object(fleet, "SYSTEM_VERSION_PLIST", self.version):
            return fleet.interpreter_changed_by_os(self.receipt, recorded, current)

    def test_build_change_is_os_update(self) -> None:
        self.write(build="25A1", current_build="26A1")
        self.assertIn("25A1 -> 26A1", self.evidence())

    def test_same_build_is_not_os_update(self) -> None:
        self.write(build="26A1", current_build="26A1")
        self.assertIsNone(self.evidence())

    def _history(self, *entries) -> Path:
        path = Path(self.td.name) / "InstallHistory.plist"
        path.write_bytes(plistlib.dumps([
            {"displayName": name, "displayVersion": version, "date": when,
             "processName": "softwareupdated"} for name, version, when in entries]))
        return path

    def test_old_receipt_on_m3_classifies_from_install_history(self) -> None:
        # m3, 2026-09-25: receipt written Sep 24 19:19Z with no os_build; the
        # sealed image's SystemVersion.plist is dated Sep 3 (BEFORE the
        # receipt), and InstallHistory records macOS 27.0 at Sep 25 06:23Z.
        import datetime as dt
        self.write(build=None, current_build="26A428")
        self.version.write_bytes(plistlib.dumps({"ProductBuildVersion": "26A428",
                                                 "ProductVersion": "27.0"}))
        receipt_time = dt.datetime(2026, 9, 24, 19, 19, tzinfo=dt.timezone.utc).timestamp()
        image_time = dt.datetime(2026, 9, 3, 10, 34, tzinfo=dt.timezone.utc).timestamp()
        os.utime(self.receipt, (receipt_time, receipt_time))
        os.utime(self.version, (image_time, image_time))
        history = self._history(
            ("macOS 26.6.2", "26.6.2", dt.datetime(2026, 8, 21, 8, 31, 50)),
            ("macOS 27.0", "27.0", dt.datetime(2026, 9, 25, 6, 23, 0)),
            ("XProtectPayloads", "163", dt.datetime(2026, 9, 25, 7, 17, 20)))
        with mock.patch.object(fleet, "INSTALL_HISTORY_PLIST", history):
            evidence = self.evidence()
        self.assertIn("macOS 27.0", evidence)
        # Control: the last macOS install predates the receipt (only a data
        # update came after), so nothing explains the change.
        history = self._history(
            ("macOS 27.0", "27.0", dt.datetime(2026, 9, 20, 6, 23, 0)),
            ("XProtectPayloads", "163", dt.datetime(2026, 9, 25, 7, 17, 20)))
        with mock.patch.object(fleet, "INSTALL_HISTORY_PLIST", history):
            self.assertIsNone(self.evidence())
        # Control: no install history at all.
        with mock.patch.object(fleet, "INSTALL_HISTORY_PLIST", Path(self.td.name) / "none"):
            self.assertIsNone(self.evidence())

    def test_non_os_interpreter_stays_an_ordinary_mismatch(self) -> None:
        self.write(build="25A1", current_build="26A1")
        for recorded, current in (
                ({**OLD, "path": "/opt/homebrew/bin/python3"},
                 {**NEW, "path": "/opt/homebrew/bin/python3"}),
                (OLD, {**NEW, "owner_uid": 501}),
                (OLD, {**NEW, "mode": 0o777}),
                (OLD, OLD)):
            with self.subTest(current=current):
                self.assertIsNone(self.evidence(recorded, current))

    def test_readiness_names_the_code_and_remedy(self) -> None:
        exc = fleet.InterpreterChangedByOSUpdate(
            "interpreter_changed_by_os_update: ... remedy: reinstall the same generation")
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(fleet, "verify_receipt", side_effect=exc), \
                mock.patch.object(fleet, "config_verdicts", return_value={}):
            value = fleet.fleet_readiness(Path(td) / "r.json", Path(td) / "c.toml", Path(td),
                                          Path(td), True, "on")
        self.assertEqual(value["problems"][0]["code"], "interpreter_changed_by_os_update")
        self.assertIn("reinstall the same generation", value["problems"][0]["detail"])
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(fleet, "verify_receipt", side_effect=ValueError("x")), \
                mock.patch.object(fleet, "config_verdicts", return_value={}):
            value = fleet.fleet_readiness(Path(td) / "r.json", Path(td) / "c.toml", Path(td),
                                          Path(td), True, "on")
        self.assertEqual(value["problems"][0]["code"], "receipt_mismatch")

    def test_receipt_accepts_the_os_build_field(self) -> None:
        source = (Path(__file__).resolve().parent / "macos_fleet_lanes.py").read_text()
        self.assertIn('support_keys | {"os_build"}', source)
        self.assertIn('"os_build": os_build()', source)


class PersistentStartTests(unittest.TestCase):
    def _run_verify(self, outputs: list[str], grace: float = 1000.0):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            text = outputs.pop(0) if len(outputs) > 1 else outputs[0]
            return subprocess.CompletedProcess(argv, 0, text, "")
        receipt = {"plists": {}, "persistent_plists": {"actions.runner.o-r.x.plist": {}}}
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(fleet, "verify_receipt", return_value=receipt), \
                mock.patch.object(fleet, "verify_loaded_snapshot", return_value={}), \
                mock.patch.object(fleet, "_verify_persistent_loaded_output",
                                  side_effect=lambda name, payload, out, d: (
                                      fleet.fail("loaded persistent LaunchAgent x is not running")
                                      if "state = running" not in out else "ok")), \
                mock.patch.object(fleet.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(fleet.time, "sleep"), \
                mock.patch.object(fleet, "PERSISTENT_START_GRACE_SECONDS", grace):
            (Path(td) / "actions.runner.o-r.x.plist").write_bytes(b"x")
            (Path(td) / "r").write_text("{}")
            try:
                fleet.verify_loaded(Path(td) / "r", Path(td) / "c", Path(td), Path(td))
                return True, len(calls)
            except ValueError as exc:
                return str(exc), len(calls)

    def test_waits_for_a_just_kickstarted_persistent_runner(self) -> None:
        ok, calls = self._run_verify(["\tstate = spawn scheduled\n", "\tstate = waiting\n",
                                      "\tstate = running\n\tpid = 7\n"])
        self.assertIs(ok, True)
        self.assertEqual(calls, 3)

    def test_a_runner_that_never_runs_still_fails(self) -> None:
        ok, _ = self._run_verify(["\tstate = waiting\n"], grace=0.0)
        self.assertIn("is not running", ok)


if __name__ == "__main__":
    unittest.main()
