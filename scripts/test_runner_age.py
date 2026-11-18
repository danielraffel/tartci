#!/usr/bin/env python3
"""Hermetic tests for the golden runner-agent age check. No network, no VMs."""

from __future__ import annotations

import testing_support  # noqa: E402,F401
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import runner_age as ra  # noqa: E402
import tool_freshness as tf  # noqa: E402

DAY = 86400.0
NOW = 1790000000.0


def rel(version: str, age_days: float) -> tuple:
    return (ra.parse_version(version), "v" + version, NOW - age_days * DAY)


LATEST_OLD = [rel("2.336.0", 60), rel("2.338.2", 33)]  # latest released 33 days ago


class PolicyTests(unittest.TestCase):
    def verdict(self, installed, releases, **kw):
        return ra.classify(installed, releases, NOW, **kw)

    def test_fail_past_deadline(self):
        row = self.verdict("2.336.0", LATEST_OLD)
        self.assertEqual(row["verdict"], "fail")
        self.assertEqual(row["latest"], "2.338.2")

    def test_ok_when_deadline_is_far(self):
        self.assertEqual(self.verdict("2.336.0", [rel("2.338.2", 5)])["verdict"], "ok")

    def test_warn_boundary_is_inclusive(self):
        # deadline = published + 30 days; 10 days left is the warn edge.
        self.assertEqual(self.verdict("2.336.0", [rel("2.338.2", 20)])["verdict"], "warn")
        self.assertEqual(self.verdict("2.336.0", [rel("2.338.2", 19.9)])["verdict"], "ok")

    def test_deadline_boundary_is_not_yet_a_failure(self):
        self.assertEqual(self.verdict("2.336.0", [rel("2.338.2", 30)])["verdict"], "warn")
        self.assertEqual(self.verdict("2.336.0", [rel("2.338.2", 30.01)])["verdict"], "fail")

    def test_warn_days_is_configurable(self):
        releases = [rel("2.338.2", 25)]
        self.assertEqual(self.verdict("2.336.0", releases)["verdict"], "warn")
        self.assertEqual(self.verdict("2.336.0", releases, warn_days=3)["verdict"], "ok")

    def test_fail_below_minimum_even_when_not_late(self):
        row = self.verdict("2.330.0", [rel("2.338.2", 1)], minimum="2.336.0")
        self.assertEqual(row["verdict"], "fail")
        self.assertIn("minimum", row["reason"])

    def test_unknown_installed_is_not_ok(self):
        for installed in (None, "", "garbage"):
            self.assertEqual(self.verdict(installed, [rel("2.338.2", 1)])["verdict"], "unknown")

    def test_unknown_latest_is_not_ok(self):
        self.assertEqual(self.verdict("2.336.0", None)["verdict"], "unknown")
        self.assertEqual(self.verdict("2.336.0", [])["verdict"], "unknown")

    def test_unknown_latest_still_fails_below_minimum(self):
        self.assertEqual(self.verdict("2.330.0", None, minimum="2.336.0")["verdict"], "fail")

    def test_latest_equals_installed_is_ok_even_if_old(self):
        self.assertEqual(self.verdict("2.338.2", [rel("2.338.2", 400)])["verdict"], "ok")

    def test_deadline_runs_from_first_newer_release(self):
        # 2.337.0 is 40 days old and was skipped; the newest release is 2 days old.
        releases = [rel("2.337.0", 40), rel("2.338.2", 2)]
        self.assertEqual(self.verdict("2.336.0", releases)["verdict"], "fail")


class CollectTests(unittest.TestCase):
    def test_reads_records_and_skips_absent_goldens(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"TARTCI_HOME": tmp}):
            ra.record("pulp-linux-build:latest", "2.336.0", now=NOW)
            rows = ra.collect(("pulp-build-runner", "pulp-linux-build"), NOW,
                              releases=LATEST_OLD, present=lambda: {"pulp-linux-build"})
        self.assertEqual([r["golden"] for r in rows], ["pulp-linux-build"])
        self.assertEqual(rows[0]["verdict"], "fail")

    def test_no_record_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"TARTCI_HOME": tmp}):
            rows = ra.collect(("pulp-linux-build",), NOW, releases=LATEST_OLD,
                              present=lambda: {"pulp-linux-build"})
        self.assertEqual(rows[0]["verdict"], "unknown")

    def test_macos_golden_is_judged_by_the_pin_not_a_record(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"TARTCI_HOME": tmp}):
            ra.record("pulp-build-runner", "2.336.0", now=NOW)
            rows = ra.collect(("pulp-build-runner",), NOW, releases=LATEST_OLD,
                              present=lambda: {"pulp-build-runner"}, pin=lambda: "2.338.2")
        self.assertEqual((rows[0]["verdict"], rows[0]["installed"], rows[0]["source"]),
                         ("ok", "2.338.2", "pin"))

    def test_stale_macos_pin_fails(self):
        rows = ra.collect(("pulp-build-runner",), NOW, releases=LATEST_OLD,
                          present=lambda: {"pulp-build-runner"}, pin=lambda: "2.336.0")
        self.assertEqual(rows[0]["verdict"], "fail")

    def test_unreadable_macos_pin_is_unknown(self):
        rows = ra.collect(("pulp-build-runner",), NOW, releases=LATEST_OLD,
                          present=lambda: {"pulp-build-runner"}, pin=lambda: None)
        self.assertEqual(rows[0]["verdict"], "unknown")
        self.assertIn("pinned macOS runner", rows[0]["reason"])

    def test_macos_pin_is_read_from_the_supervisor(self):
        self.assertRegex(ra.macos_pin() or "", r"^\d+\.\d+\.\d+$")
        self.assertIsNone(ra.macos_pin(Path("/nonexistent/runner.sh")))

    def test_tart_unlistable_checks_every_named_golden(self):
        rows = ra.collect(("a", "b"), NOW, releases=LATEST_OLD, present=lambda: None,
                          read=lambda g: None)
        self.assertEqual([r["verdict"] for r in rows], ["unknown", "unknown"])

    def test_record_refuses_garbage(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"TARTCI_HOME": tmp}):
            with self.assertRaises(ValueError):
                ra.record("g", "not-a-version")
            self.assertIsNone(ra.read_record("g"))

    def test_unreadable_feed_is_none(self):
        def broken(repo):
            raise OSError("offline")
        self.assertIsNone(ra.fetch_releases(broken))


class SurfaceTests(unittest.TestCase):
    """The doctor CLI (human and --json) and the alert path through tool freshness."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"TARTCI_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        ra.record("pulp-linux-build", "2.336.0", now=NOW)

    def run_cli(self, *args):
        out = []
        with mock.patch.object(ra, "fetch_releases", lambda: LATEST_OLD), \
                mock.patch.object(ra, "present_goldens", lambda: {"pulp-linux-build"}), \
                mock.patch.object(ra.time, "time", lambda: NOW), \
                mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            code = ra.main(["check", *args])
        return code, "\n".join(out)

    def test_json_output_and_exit_code(self):
        code, text = self.run_cli("--json", "--golden", "pulp-linux-build")
        row = json.loads(text)["goldens"][0]
        self.assertEqual((code, row["verdict"], row["installed"], row["latest"]),
                         (1, "fail", "2.336.0", "2.338.2"))

    def test_human_output_names_golden_and_verdict(self):
        code, text = self.run_cli("--golden", "pulp-linux-build")
        self.assertEqual(code, 1)
        self.assertIn("pulp-linux-build: FAIL: installed 2.336.0, latest 2.338.2", text)

    def test_tool_freshness_summary_raises_a_problem(self):
        home = Path(self.tmp.name)
        feed = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><updated>%s</updated>'
                '<link href="https://github.com/actions/runner/releases/tag/v2.338.2"/>'
                '</entry></feed>' % tf._iso(NOW - 33 * DAY))
        settings = {"stale_hours": 12.0, "tools": {},
                    "runner_age": {"goldens": ["pulp-linux-build"], "warn_days": 10.0,
                                   "minimum": None}}
        with mock.patch.object(ra, "present_goldens", lambda: {"pulp-linux-build"}):
            tf.refresh(home, NOW, settings=settings, feed=lambda repo: feed)
        import state_age
        with mock.patch.object(state_age, "clock", lambda: NOW + 60):
            summary = tf.summary(home)
        self.assertIn("runner age pulp-linux-build: FAIL", summary["problem"])
        self.assertIn("runner age pulp-linux-build: FAIL", "\n".join(summary["lines"]))

    def test_tool_freshness_feed_failure_is_unknown_problem(self):
        home = Path(self.tmp.name)

        def broken(repo):
            raise OSError("offline")
        settings = {"stale_hours": 12.0, "tools": {},
                    "runner_age": {"goldens": ["pulp-linux-build"], "warn_days": 10.0,
                                   "minimum": None}}
        with mock.patch.object(ra, "present_goldens", lambda: {"pulp-linux-build"}):
            tf.refresh(home, NOW, settings=settings, feed=broken)
        import state_age
        with mock.patch.object(state_age, "clock", lambda: NOW + 60):
            summary = tf.summary(home)
        self.assertIn("UNKNOWN", summary["problem"])


class SupervisorTests(unittest.TestCase):
    def test_linux_runner_names_the_deprecated_runner_failure(self):
        text = (HERE.parent / "providers/tart-linux/runner.sh").read_text()
        self.assertIn("is deprecated and cannot receive messages", text)
        self.assertIn("runner_version_deprecated:", text)


if __name__ == "__main__":
    unittest.main()
