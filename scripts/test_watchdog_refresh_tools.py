#!/usr/bin/env python3
"""A tool-freshness refresh that crashed or hung used to vanish without a trace."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tartci_launchd_watchdog as wd  # noqa: E402


def run_with(**result):
    def fake(*args, **kwargs):
        if "raise" in result:
            raise result["raise"]
        return subprocess.CompletedProcess(args[0], result.get("rc", 0),
                                           result.get("out", ""), result.get("err", ""))
    with mock.patch.object(wd, "_toml_python", return_value="python3"), \
         mock.patch.object(wd.subprocess, "run", side_effect=fake):
        return wd.refresh_tools()


class RefreshToolsIsLoudTests(unittest.TestCase):
    def test_failures_are_named(self) -> None:
        self.assertEqual(run_with(rc=1, err="Traceback (most recent call last):\n  ...\n"
                                             "KeyError: 'latest_tag'"),
                         "exit 1: KeyError: 'latest_tag'")
        self.assertEqual(run_with(**{"raise": subprocess.TimeoutExpired("x", 600)}),
                         "timed out after 600s")
        self.assertEqual(run_with(**{"raise": OSError("no such file")}), "could not run: no such file")
        self.assertEqual(run_with(rc=2, err="usage: tool_freshness"), "exit 2: usage: tool_freshness")
        with mock.patch.object(wd, "_toml_python", return_value=None):
            self.assertIn("no Python 3.11+", wd.refresh_tools())

    def test_a_stale_tool_or_success_is_not_a_failure(self) -> None:
        self.assertIsNone(run_with(rc=0))
        self.assertIsNone(run_with(rc=1, out="pulp: 0.881.2 behind latest v0.884.0 STALE"))

    def test_the_heal_pass_prints_it(self) -> None:
        source = Path(wd.__file__).read_text()
        self.assertIn("tools_error = refresh_tools()", source)
        self.assertIn("WARN tool-freshness refresh failed", source)


if __name__ == "__main__":
    unittest.main()
