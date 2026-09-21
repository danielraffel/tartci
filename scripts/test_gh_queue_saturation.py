#!/usr/bin/env python3
"""Hermetic tests for the GitHub-hosted queue-saturation detector.

Exercises the pure decision core (`classify_saturation`) and the clock helper
(`_iso_age_secs`) with synthetic inputs — no network, no `gh`, no real clock —
so the triad contract is what we lock down. Runs on any platform in CI.

Run:  python3 -m unittest scripts.test_gh_queue_saturation -v
"""

from __future__ import annotations

import os
import tempfile
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

    def test_app_cli_is_explicit_and_never_falls_back_to_gh(self):
        old = os.environ.pop("PULP_SAT_GH_CLI", None)
        try:
            with self.assertRaisesRegex(RuntimeError, "must name an explicit"):
                sat._gh()
            os.environ["PULP_SAT_GH_CLI"] = "gh"
            with self.assertRaisesRegex(RuntimeError, "refuses ambient gh"):
                sat._gh()
            with tempfile.TemporaryDirectory() as directory:
                wrapper = os.path.join(directory, "ghapp")
                with open(wrapper, "w", encoding="utf-8") as destination:
                    destination.write("#!/bin/sh\n")
                os.chmod(wrapper, 0o755)
                os.environ["PULP_SAT_GH_CLI"] = wrapper
                self.assertEqual(sat._gh(), wrapper)
        finally:
            if old is None:
                os.environ.pop("PULP_SAT_GH_CLI", None)
            else:
                os.environ["PULP_SAT_GH_CLI"] = old
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


class PartialCensusTests(unittest.TestCase):
    """An unread registration scope makes "no capacity" unproven, not true."""

    def _classify(self, runners, *, census_complete):
        return sat.classify_saturation(
            154, runners, [1800],
            queue_trip=TRIP, grace_secs=GRACE, required_labels=REQUIRED,
            census_complete=census_complete,
        )

    def test_complete_census_with_no_runner_concludes_no_capacity(self) -> None:
        v = self._classify([], census_complete=True)

        self.assertFalse(v.capacity_unknown)
        self.assertTrue(any("no idle required-gate runner" in r for r in v.reasons))

    def test_partial_census_with_no_runner_reports_unknown(self) -> None:
        v = self._classify([], census_complete=False)

        self.assertTrue(v.capacity_unknown)
        self.assertFalse(v.saturated)
        self.assertTrue(any("UNKNOWN" in r for r in v.reasons))
        self.assertFalse(any("no idle required-gate runner" in r for r in v.reasons))

    def test_a_found_runner_is_capacity_even_from_a_partial_census(self) -> None:
        v = self._classify([_runner("studio-01")], census_complete=False)

        self.assertFalse(v.capacity_unknown)
        self.assertTrue(v.saturated)


class GatherScopeTests(unittest.TestCase):
    """The capacity leg reads both registration scopes."""

    def gather(self, runner_pages: dict, *, fail: set = frozenset()):
        seen: list[str] = []

        def fake_gh_json(args):
            path = args[1]
            seen.append(path)
            if "actions/runs?" in path:
                return {"total_count": 0, "workflow_runs": []}
            for endpoint in fail:
                if path.startswith(endpoint):
                    raise RuntimeError("HTTP 403")
            for endpoint, rows in runner_pages.items():
                if path.startswith(endpoint):
                    return {"runners": rows}
            return {"runners": []}

        original_gh, original_json = sat._gh, sat._gh_json
        sat._gh, sat._gh_json = (lambda: "ghapp"), fake_gh_json
        try:
            return sat.gather("Generous-Corp/pulp"), seen
        finally:
            sat._gh, sat._gh_json = original_gh, original_json

    def test_an_organization_only_runner_reaches_the_classifier(self) -> None:
        (queued, runners, ages, complete), seen = self.gather(
            {
                "orgs/Generous-Corp/actions/runners": [
                    {"name": "pulp-intel-macmini", "status": "online", "busy": False,
                     "labels": [{"name": "self-hosted"}, {"name": "macOS"}]}
                ]
            }
        )

        self.assertIn("orgs/Generous-Corp/actions/runners?per_page=100", seen)
        self.assertEqual([r["name"] for r in runners], ["pulp-intel-macmini"])
        self.assertTrue(complete)

    def test_an_unreadable_scope_marks_the_census_incomplete(self) -> None:
        (queued, runners, ages, complete), _ = self.gather(
            {}, fail={"orgs/Generous-Corp/actions/runners"}
        )

        self.assertFalse(complete)
        self.assertEqual(runners, [])


if __name__ == "__main__":
    unittest.main()
