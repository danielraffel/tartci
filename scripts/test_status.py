#!/usr/bin/env python3
"""Cover the disk and reclaim-agent facts `tartci status` reports."""

from __future__ import annotations

import io
import json
import os
import pathlib
import plistlib
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import disk_reclaim as dr  # noqa: E402
import status  # noqa: E402


def write_plist(path: pathlib.Path, env: dict[str, str], **extra) -> None:
    job = {"Label": "com.danielraffel.tartci.reclaim",
           "EnvironmentVariables": env}
    job.update(extra)
    with path.open("wb") as handle:
        plistlib.dump(job, handle)


class ReclaimAgentTests(unittest.TestCase):
    def test_missing_plist_reports_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = status.janitor_agent(
                plist_path=pathlib.Path(tmp) / "absent.plist")
        self.assertFalse(out["installed"])
        self.assertNotIn("settings", out)

    def test_unanswerable_launchd_stays_none_not_false(self):
        """127 from launchctl is "could not ask", not "not loaded".

        This is the reading CI itself produces: the lint suite runs on Linux,
        where there is no launchctl at all. Collapsing that into False would
        have status report every Linux host as a broken agent.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "job.plist"
            write_plist(path, {})
            with mock.patch.object(status, "run", return_value={
                    "ok": False, "returncode": 127, "stdout": "", "stderr": ""}):
                out = status.janitor_agent(plist_path=path)
        self.assertIsNone(out["loaded"])

    def test_launchd_answering_no_reports_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "job.plist"
            write_plist(path, {})
            with mock.patch.object(status, "run", return_value={
                    "ok": False, "returncode": 113, "stdout": "", "stderr": ""}):
                out = status.janitor_agent(plist_path=path)
        self.assertIs(out["loaded"], False)

    def test_reports_installed_settings_and_last_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "reclaim.log"
            log.write_text("pass\n")
            os.utime(log, (1_700_000_000, 1_700_000_000))
            path = pathlib.Path(tmp) / "job.plist"
            write_plist(path,
                        {"TARTCI_RECLAIM_MAXDEPTH": "5",
                         "TARTCI_RECLAIM_FAIL_BELOW_GB": "60",
                         "PATH": "/usr/bin"},
                        StandardOutPath=str(log), StartInterval=3600)
            with mock.patch.object(status, "run", return_value={
                    "ok": True, "returncode": 0, "stdout": "", "stderr": ""}):
                out = status.janitor_agent(plist_path=path)
        self.assertTrue(out["installed"])
        self.assertEqual(out["start_interval_s"], 3600)
        self.assertEqual(out["last_pass_ts"], 1_700_000_000)
        # PATH is not reclaim policy; only the janitor's own knobs are reported.
        self.assertEqual(sorted(out["settings"]),
                         ["TARTCI_RECLAIM_FAIL_BELOW_GB",
                          "TARTCI_RECLAIM_MAXDEPTH"])

    def test_each_janitor_reports_only_its_own_settings(self):
        """The settings filter follows the label, not a fixed prefix.

        Both janitors write their knobs into the same EnvironmentVariables
        dict shape, so a hardcoded TARTCI_RECLAIM_ prefix would report the
        VM janitor as having no settings at all while it has several.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "reap.plist"
            write_plist(path, {"TARTCI_REAP_PREFIXES": "pulp-",
                               "TARTCI_RECLAIM_ROOTS": "/nope",
                               "PATH": "/usr/bin"})
            out = status.janitor_agent(status.REAP_LABEL, "TARTCI_REAP_",
                                       plist_path=path)
        self.assertEqual(out["settings"], {"TARTCI_REAP_PREFIXES": "pulp-"})

    def test_absent_log_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "job.plist"
            write_plist(path, {},
                        StandardOutPath=str(pathlib.Path(tmp) / "nope.log"))
            with mock.patch.object(status, "run", return_value={
                    "ok": True, "returncode": 0, "stdout": "", "stderr": ""}):
                out = status.janitor_agent(plist_path=path)
        self.assertIsNone(out["last_pass_ts"])

    def test_corrupt_plist_reports_error_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "job.plist"
            path.write_bytes(b"this is not a plist")
            with mock.patch.object(status, "run", return_value={
                    "ok": True, "returncode": 0, "stdout": "", "stderr": ""}):
                out = status.janitor_agent(plist_path=path)
        self.assertTrue(out["installed"])
        self.assertIn("error", out)


class DiskSpaceTests(unittest.TestCase):
    def test_roots_come_from_the_janitor_not_a_second_list(self):
        """Status must scan exactly what disk_reclaim would scan.

        A status that measures a different set of volumes than the janitor is
        the one fault nothing downstream can catch: it reports a healthy pass
        forever on a host whose real volume is filling. Asserting equality
        against disk_reclaim.parse_roots is what keeps the two from drifting.
        """
        with tempfile.TemporaryDirectory() as tmp:
            declared = f"{tmp}:{tmp}/nested"
            (pathlib.Path(tmp) / "nested").mkdir()
            out = status.disk_space({"TARTCI_RECLAIM_ROOTS": declared})
        self.assertEqual(out["roots"],
                         [str(root) for root in dr.parse_roots(declared)])
        self.assertTrue(out["roots_declared"])

    def test_installed_plist_roots_beat_this_shell_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ,
                                 {"TARTCI_RECLAIM_ROOTS": "/definitely/not/it"}):
                out = status.disk_space({"TARTCI_RECLAIM_ROOTS": tmp})
        self.assertEqual(out["roots"], [str(pathlib.Path(tmp).resolve())])

    def test_environment_is_used_when_the_plist_declares_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"TARTCI_RECLAIM_ROOTS": tmp}):
                out = status.disk_space({})
        self.assertEqual(out["roots"], [str(pathlib.Path(tmp).resolve())])

    def test_unknown_free_space_does_not_become_a_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(dr, "free_bytes", return_value=None):
                out = status.disk_space({"TARTCI_RECLAIM_ROOTS": tmp})
        self.assertIsNone(out["volumes"][0]["free_bytes"])
        self.assertIsNone(out["tightest_free_bytes"])
        self.assertIsNone(out["tightest_root"])

    def test_failure_reports_error_rather_than_a_clean_reading(self):
        with mock.patch.object(dr, "parse_roots",
                               side_effect=OSError("volume gone")):
            out = status.disk_space({"TARTCI_RECLAIM_ROOTS": "/x"})
        self.assertIn("error", out)
        self.assertNotIn("volumes", out)


class StatusOutputTests(unittest.TestCase):
    def _quiet_main(self, argv):
        buffer = io.StringIO()
        with mock.patch.object(status, "tart_vms", return_value={}), \
                mock.patch.object(status, "qemu_processes", return_value=[]), \
                mock.patch.object(status, "lease_status", return_value={}), \
                mock.patch.object(status, "profile_names", return_value=[]), \
                redirect_stdout(buffer):
            code = status.main(argv)
        return code, buffer.getvalue()

    def test_json_carries_disk_and_both_janitors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"TARTCI_RECLAIM_ROOTS": tmp}), \
                    mock.patch.object(status, "janitor_agent",
                                      return_value={"installed": False,
                                                    "loaded": None}):
                code, text = self._quiet_main(["--json"])
        self.assertEqual(code, 0)
        data = json.loads(text)
        self.assertIn("disk", data)
        self.assertEqual(sorted(data["janitors"]), ["reap", "reclaim"])
        self.assertEqual(data["disk"]["roots"],
                         [str(pathlib.Path(tmp).resolve())])

    def test_unknown_free_prints_unknown_never_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"TARTCI_RECLAIM_ROOTS": tmp}), \
                    mock.patch.object(dr, "free_bytes", return_value=None), \
                    mock.patch.object(status, "janitor_agent",
                                      return_value={"installed": False,
                                                    "loaded": None}):
                _, text = self._quiet_main([])
        self.assertIn("unknown", text)
        self.assertNotIn("GiB free", text)

    def test_missing_agent_is_stated_loudly(self):
        with mock.patch.object(status, "janitor_agent",
                               return_value={"installed": False,
                                             "loaded": None}):
            _, text = self._quiet_main([])
        self.assertIn("NOT INSTALLED", text)

    def test_a_present_vm_janitor_does_not_mask_a_missing_reclaimer(self):
        """Each janitor is reported on its own line, from its own label.

        m3 carries the VM janitor and no disk reclaimer. Reporting a single
        "janitor" line from whichever one answered first would have read as
        healthy on exactly that host, which is the reading this exists to stop.
        """
        def by_label(label=status.RECLAIM_LABEL, *_args, **_kwargs):
            if label == status.REAP_LABEL:
                return {"installed": True, "loaded": True,
                        "last_pass_ts": None}
            return {"installed": False, "loaded": False}

        with mock.patch.object(status, "janitor_agent", side_effect=by_label):
            _, text = self._quiet_main([])
        self.assertIn("disk reclaimer: NOT INSTALLED", text)
        self.assertIn("VM janitor: installed and loaded", text)

    def test_unreachable_launchd_is_not_reported_as_running(self):
        with mock.patch.object(status, "janitor_agent",
                               return_value={"installed": True,
                                             "loaded": None,
                                             "last_pass_ts": None}):
            _, text = self._quiet_main([])
        self.assertIn("launchd not reachable", text)
        self.assertNotIn("installed and loaded", text)


class DiskSpaceBoundTests(unittest.TestCase):
    """A status command must not hang on the volume it is reporting about.

    Root discovery resolves paths and stats the volume each sits on, and one
    of this fleet's roots is an external mount. Those calls have no timeout of
    their own, so a wedged mount would block `tartci status` for as long as the
    mount stays wedged -- during exactly the incident it is being run to
    explain.
    """

    def test_a_wedged_volume_scan_returns_instead_of_hanging(self):
        started = threading.Event()

        def never_answers(_declared):
            started.set()
            time.sleep(30)
            raise AssertionError("unreachable within the test's lifetime")

        with mock.patch.object(
                status.disk_reclaim, "parse_roots", never_answers):
            begin = time.monotonic()
            result = status.disk_space(timeout=0.2)
            elapsed = time.monotonic() - begin
        self.assertTrue(started.wait(5), "the scan never started")
        self.assertLess(elapsed, 10, "disk_space blocked on the wedged scan")
        self.assertIn("wedged mount", result.get("error", ""))

    def test_a_volume_that_answers_is_reported_normally(self):
        # THE CONTROL. Every assertion above is also satisfied by a
        # disk_space() that has stopped measuring anything at all.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                    os.environ, {"TARTCI_RECLAIM_ROOTS": tmp}):
                result = status.disk_space()
        self.assertNotIn("error", result)
        self.assertTrue(result["roots_declared"])
        self.assertIsInstance(result["tightest_free_bytes"], int)
        self.assertGreater(result["tightest_free_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
