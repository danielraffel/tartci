#!/usr/bin/env python3
"""Cached status is flagged STALE once its refresher has missed three runs.

m3, 2026-09-26 to 09-28: the launchd watchdog exited before its pass for two
days, so skew.json was never refreshed, and every status surface went on
printing the last measurement as if it were current. Each surface below is
exercised with a fresh measurement (no flag) and an old one (flagged).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_self_update as su  # noqa: E402
import state_age  # noqa: E402

try:
    import tomllib  # noqa: F401  (macos_fleet_lanes needs it; /usr/bin/python3 3.9 lacks it)
    HAVE_TOMLLIB = True
except ModuleNotFoundError:
    HAVE_TOMLLIB = False
import tool_freshness as tf  # noqa: E402

NOW = 1_790_000_000.0
HOUR = 3600.0


def iso(ts: float) -> str:
    return su._iso(ts)


class StaleNoteTests(unittest.TestCase):
    def test_fresh_within_three_intervals_is_not_flagged(self) -> None:
        self.assertIsNone(state_age.stale_note(iso(NOW - 3 * 1800), 1800, "w", NOW))
        self.assertIsNone(state_age.stale_note(NOW - 100, 60, "w", NOW))

    def test_older_than_three_intervals_is_stale_and_names_the_refresher(self) -> None:
        note = state_age.stale_note(iso(NOW - 2 * 86400), 1800, "launchd watchdog", NOW)
        self.assertEqual(note, "STALE (measured 48.0 h ago, older than 3 x 30 min; "
                               "is the launchd watchdog running?)")
        self.assertIn("STALE", state_age.stale_note(NOW - 181, 60, "sensor", NOW))

    def test_an_unreadable_time_is_never_current(self) -> None:
        for stamp in (None, "now", "", True):
            self.assertIn("unreadable time", state_age.stale_note(stamp, 60, "x", NOW))


class SurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.home / ".tartci")})
        env.start()
        self.addCleanup(env.stop)
        clock = mock.patch.object(state_age, "clock", lambda: NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def test_skew_measured_two_days_ago_reads_stale(self) -> None:
        state = su.state_dir_for(self.home)
        su._write_json(state / "skew.json", {
            "state": "behind", "behind": 1, "stale": False, "installed": "a" * 40,
            "oldest_undeployed": iso(NOW - 3 * 86400), "measured_at": iso(NOW - 2 * 86400)})
        summary = su.summary(self.home)
        self.assertIn("skew STALE (measured 48.0 h ago", summary["problem"])
        self.assertIn("STALE (measured 48.0 h ago", summary["lines"][0])
        su._write_json(state / "skew.json", {
            "state": "current", "installed": "a" * 40, "measured_at": iso(NOW - 600)})
        self.assertIsNone(su.summary(self.home)["problem"])
        self.assertNotIn("STALE", su.summary(self.home)["lines"][0])

    def test_tool_freshness_measured_long_ago_reads_stale(self) -> None:
        row = {"tool": "shipyard", "state": "current", "installed": "0.230.0",
               "latest_tag": "v0.230.0", "measured_at": iso(NOW - 5 * HOUR)}
        tf._write_json(tf.state_dir_for(self.home) / "state.json",
                       {"measured_at": iso(NOW - 5 * HOUR), "tools": {"shipyard": row}})
        summary = tf.summary(self.home)
        self.assertIn("tool freshness STALE (measured 5.0 h ago", summary["problem"])
        self.assertIn("tools: freshness STALE", summary["lines"][-1])
        tf._write_json(tf.state_dir_for(self.home) / "state.json",
                       {"measured_at": iso(NOW - 600), "tools": {"shipyard": row}})
        self.assertIsNone(tf.summary(self.home)["problem"])

    @unittest.skipUnless(HAVE_TOMLLIB, "macos_fleet_lanes needs tomllib (Python 3.11+)")
    def test_host_vitals_the_sensor_stopped_refreshing_reads_stale(self) -> None:
        import macos_fleet_lanes as lanes
        path = self.home / "host_vitals.json"
        fsev = {"rss_mb": 20, "cpu_pct": 1.0, "warn": False, "warn_mb": 1024}
        path.write_text(json.dumps({"sampled_at": int(NOW - 3600), "fseventsd": fsev}))
        with mock.patch.object(lanes.time, "time", lambda: NOW):
            stale = lanes.host_vitals_summary(path)
            self.assertIn("host vitals STALE (measured 60 min ago", stale["problem"])
            self.assertIn("host-vitals sensor", stale["lines"][0])
            path.write_text(json.dumps({"sampled_at": int(NOW - 30), "fseventsd": fsev}))
            self.assertIsNone(lanes.host_vitals_summary(path)["problem"])


if __name__ == "__main__":
    unittest.main()
