#!/usr/bin/env python3
"""Hermetic tests for the GitHub-hosted queue-saturation detector.

Exercises the pure decision core (`classify_saturation`) and the clock helper
(`_iso_age_secs`) with synthetic inputs — no network, no `gh`, no real clock —
so the triad contract is what we lock down. Runs on any platform in CI.

Run:  python3 -m unittest scripts.test_gh_queue_saturation -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gh_queue_saturation as sat  # noqa: E402

REQUIRED = {"self-hosted", "macOS"}
TRIP = 50
GRACE = 900


def _runner(name, status="online", busy=False, labels=("self-hosted", "macOS", "ARM64")):
    return {"name": name, "status": status, "busy": busy, "labels": list(labels)}


class ClassifySaturationTests(unittest.TestCase):
    def _classify(self, queued, runners, ages):
        return sat.classify_saturation(
            queued, runners, ages,
            queue_trip=TRIP, grace_secs=GRACE, required_labels=REQUIRED,
        )

    def test_full_triad_is_saturation(self):
        # deep queue + idle required runner + a check stuck past grace
        v = self._classify(154, [_runner("studio-01")], [1800])
        self.assertTrue(v.saturated)
        self.assertEqual(v.idle_runners, ["studio-01"])

    def test_shallow_queue_is_not_saturation(self):
        # idle runner + stuck check but the queue is shallow → just quiet/slow
        v = self._classify(3, [_runner("studio-01")], [1800])
        self.assertFalse(v.saturated)
        self.assertFalse(v.queue_high)

    def test_busy_runner_is_not_saturation(self):
        # deep queue + stuck check but the required runner is BUSY → that's real
        # load / a wedged runner (runner-health's job), not GitHub starvation.
        v = self._classify(154, [_runner("studio-01", busy=True)], [1800])
        self.assertFalse(v.saturated)
        self.assertFalse(v.idle_capacity)

    def test_offline_runner_is_not_idle_capacity(self):
        v = self._classify(154, [_runner("studio-01", status="offline")], [1800])
        self.assertFalse(v.saturated)
        self.assertFalse(v.idle_capacity)

    def test_no_stuck_check_is_not_saturation(self):
        # deep queue + idle runner but nothing has waited past grace → transient
        v = self._classify(154, [_runner("studio-01")], [120])
        self.assertFalse(v.saturated)
        self.assertFalse(v.stuck_checks)

    def test_empty_ages_is_not_stuck(self):
        v = self._classify(154, [_runner("studio-01")], [])
        self.assertFalse(v.saturated)
        self.assertFalse(v.stuck_checks)

    def test_runner_missing_required_label_is_ignored(self):
        # a Linux self-hosted runner is idle, but it is not required-gate capacity
        linux = _runner("linux-ephr", labels=("self-hosted", "Linux", "ARM64"))
        v = self._classify(154, [linux], [1800])
        self.assertFalse(v.saturated)
        self.assertFalse(v.idle_capacity)

    def test_threshold_boundary_is_inclusive(self):
        v = self._classify(TRIP, [_runner("s")], [GRACE])
        self.assertTrue(v.queue_high)
        self.assertTrue(v.stuck_checks)
        self.assertTrue(v.saturated)

    def test_empty_required_labels_matches_any_self_hosted(self):
        # required_labels=∅ → any self-hosted runner counts as capacity
        linux = _runner("linux-ephr", labels=("self-hosted", "Linux"))
        v = sat.classify_saturation(
            154, [linux], [1800],
            queue_trip=TRIP, grace_secs=GRACE, required_labels=set(),
        )
        self.assertTrue(v.saturated)

    def test_reasons_are_populated_both_ways(self):
        self.assertTrue(self._classify(154, [_runner("s")], [1800]).reasons)
        self.assertTrue(self._classify(1, [_runner("s")], [0]).reasons)


class IsoAgeTests(unittest.TestCase):
    NOW = 1_000_000  # fixed synthetic epoch

    def test_age_is_positive_seconds(self):
        # 1800s before NOW
        import datetime as dt
        iso = dt.datetime.fromtimestamp(self.NOW - 1800, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(sat._iso_age_secs(iso, self.NOW), 1800)

    def test_empty_is_zero(self):
        self.assertEqual(sat._iso_age_secs("", self.NOW), 0)

    def test_malformed_is_zero_not_crash(self):
        self.assertEqual(sat._iso_age_secs("not-a-date", self.NOW), 0)

    def test_future_clamps_to_zero(self):
        import datetime as dt
        iso = dt.datetime.fromtimestamp(self.NOW + 500, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(sat._iso_age_secs(iso, self.NOW), 0)


if __name__ == "__main__":
    unittest.main()
