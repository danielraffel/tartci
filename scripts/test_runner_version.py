#!/usr/bin/env python3
"""Hermetic tests for stale-runner detection (both directions per guard)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner_version as rv

MSG = "Runner version v2.336.0 is deprecated and cannot receive messages"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LagTests(unittest.TestCase):
    def test_lag(self):
        self.assertEqual(rv.minor_lag("2.338.2", "2.340.0"), 2)
        self.assertEqual(rv.minor_lag("2.338.2", "2.338.9"), 0)
        self.assertEqual(rv.minor_lag("2.338.2", "2.337.0"), 0)
        self.assertIsNone(rv.minor_lag("junk", "2.340.0"))

    def test_evaluate_both_directions(self):
        self.assertEqual(rv.evaluate("2.338.2", "2.339.0", [])[0], "ok")
        self.assertEqual(rv.evaluate("2.338.2", "2.340.0", [])[0], "warn")
        self.assertEqual(rv.evaluate("2.338.2", None, [])[0], "unknown")
        hit = [{"log": "x", "version": "2.336.0"}]
        self.assertEqual(rv.evaluate("2.336.0", "2.336.0", hit)[0], "problem")


class LogTests(unittest.TestCase):
    def test_find_deprecated(self):
        self.assertEqual(rv.find_deprecated("x\n" + MSG + "\n"), "2.336.0")
        self.assertIsNone(rv.find_deprecated("Listening for Jobs"))

    def test_scan_only_matches_pinned(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "vm1.actions-runner.log"), "w") as f:
                f.write(MSG)
            self.assertEqual(len(rv.scan_logs(d, "2.336.0")), 1)
            self.assertEqual(rv.scan_logs(d, "2.338.2"), [])


class CacheTests(unittest.TestCase):
    def test_one_fetch_per_ttl_and_fail_open(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "c.json")
            calls = []

            def fetch():
                calls.append(1)
                return "2.340.0"

            self.assertEqual(rv.latest_release(cache, now=1000, fetch=fetch), "2.340.0")
            self.assertEqual(rv.latest_release(cache, now=1000 + 3600, fetch=fetch), "2.340.0")
            self.assertEqual(len(calls), 1)

            def boom():
                raise RuntimeError("rate limited")

            self.assertEqual(rv.latest_release(cache, now=1000 + 2 * 86400, fetch=boom), "2.340.0")
            self.assertIsNone(rv.latest_release(os.path.join(d, "none.json"), now=1, fetch=boom))


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts/runner_version.py"), *args],
            capture_output=True, text=True)

    def test_problem_exits_1_ok_exits_0(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "c.json")
            with open(cache, "w") as f:
                json.dump({"latest": "2.338.2", "fetched_at": 4e9}, f)
            ok = self.run_cli("check", "--pinned", "2.338.2", "--state-dir", d, "--cache", cache)
            self.assertEqual(ok.returncode, 0, ok.stdout)
            with open(os.path.join(d, "a.actions-runner.log"), "w") as f:
                f.write(MSG)
            bad = self.run_cli("check", "--pinned", "2.336.0", "--state-dir", d, "--cache", cache)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("PROBLEM", bad.stdout)


class LaneWiringTests(unittest.TestCase):
    """The lane must name the event and back off, not loop on fresh VMs."""

    def test_runner_sh_wiring(self):
        with open(os.path.join(ROOT, "providers/tart-macos/runner.sh")) as f:
            src = f.read()
        self.assertIn("event runner_version_deprecated", src)
        self.assertIn("runner_version_backoff_active", src)
        self.assertIn("runner-version-deprecated", src)
        with open(os.path.join(ROOT, "tartci")) as f:
            self.assertIn("check_runner_version", f.read())


if __name__ == "__main__":
    unittest.main()
