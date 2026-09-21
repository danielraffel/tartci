#!/usr/bin/env python3
"""Tests for the macOS observe surface.

`tartci observe macos` is the read-only view operators and agents actually
read. It rendered "no matching macOS supervisors" next to "problems=0" and
exited 0 on a host running five fleet lanes, so a blind view was
indistinguishable from a healthy one by both prose and exit status. These
tests pin both halves of that distinction.
"""

from __future__ import annotations

import io
import contextlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import macos_observe  # noqa: E402


def digest(matched, expected, observations=()):
    return {
        "digest": {
            "host": "h",
            "capacity": {"running_macos_vms": 1, "macos_cap": 2, "free": 1},
            "problems": [],
            "supervisor_coverage": {
                "matched": matched, "expected": expected, "source": "launchctl",
            },
        },
        "observations": list(observations),
    }


class TestObserveExitCode(unittest.TestCase):
    def test_blind_host_with_loaded_lanes_exits_nonzero(self) -> None:
        """The incident shape: five lanes loaded, nothing matched."""
        self.assertEqual(macos_observe.observe_exit_code(digest(0, 5)), 3)

    def test_idle_host_with_no_lanes_exits_zero(self) -> None:
        """No lanes loaded is a correct, quiet state -- never an alarm."""
        self.assertEqual(macos_observe.observe_exit_code(digest(0, 0)), 0)

    def test_full_coverage_exits_zero(self) -> None:
        self.assertEqual(
            macos_observe.observe_exit_code(digest(2, 2, [{"supervisor": {}}])), 0
        )

    def test_partial_coverage_exits_nonzero(self) -> None:
        self.assertEqual(macos_observe.observe_exit_code(digest(1, 5)), 3)

    def test_unknown_expectation_with_no_observations_exits_nonzero(self) -> None:
        """Could-not-establish must not pass as could-not-find."""
        self.assertEqual(macos_observe.observe_exit_code(digest(0, None)), 3)


class TestObserveRendering(unittest.TestCase):
    def render(self, data) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            macos_observe.print_human(data)
        return buf.getvalue()

    def test_blind_host_says_blind_not_idle(self) -> None:
        out = self.render(digest(0, 5))
        self.assertIn("supervisors=0/5", out)
        self.assertIn("BLIND", out)
        self.assertIn("failure to observe", out)
        # The old wording read as a statement about the host. It must be gone.
        self.assertNotIn("no matching macOS supervisors", out)

    def test_host_with_no_lanes_does_not_say_blind(self) -> None:
        out = self.render(digest(0, 0))
        self.assertIn("supervisors=0/0", out)
        self.assertNotIn("BLIND", out)
        self.assertIn("no macOS fleet supervisors are loaded", out)

    def test_unknown_denominator_renders_as_unknown(self) -> None:
        out = self.render(digest(0, None))
        self.assertIn("UNKNOWN", out)
        self.assertIn("could not be established", out)

    def test_coverage_line_shapes(self) -> None:
        self.assertEqual(
            macos_observe.coverage_line({"supervisor_coverage": {"matched": 2, "expected": 5}}),
            "supervisors=2/5",
        )
        self.assertEqual(
            macos_observe.coverage_line({"supervisor_coverage": {"matched": 0, "expected": None}}),
            "supervisors=0/? (expected count unavailable)",
        )


class TestServingBlockedRendering(unittest.TestCase):
    """The supervisor line is where a serve-less lane hides.

    `phase=waiting heartbeat_age=3s` is exactly what a lane that has not served
    a job in three hours looks like, so the line that already reads as health
    has to carry the contradicting fact or nobody goes looking for it.
    """

    def test_a_serving_lane_adds_nothing(self) -> None:
        self.assertEqual(
            macos_observe.serving_suffix({
                "serving_blocked_since": "", "serving_blocked_streak": 0,
            }),
            "",
        )

    def test_a_blocked_lane_is_named_on_the_supervisor_line(self) -> None:
        suffix = macos_observe.serving_suffix({
            "serving_blocked_since": "2026-09-20T00:00:00Z",
            "serving_blocked_streak": 143,
            "serving_blocked_last_phase": "admission-error",
        })
        self.assertIn("serving_blocked_since=2026-09-20T00:00:00Z", suffix)
        self.assertIn("serve_less_streak=143", suffix)
        self.assertIn("last_phase=admission-error", suffix)

    def test_a_generation_predating_the_counter_is_marked_unknown(self) -> None:
        """Absent is not zero. A lane whose runner never wrote the counter must
        not render the same as one that wrote 0."""
        suffix = macos_observe.serving_suffix({
            "serving_blocked_since": "2026-09-20T00:00:00Z",
            "serving_blocked_streak": None,
        })
        self.assertIn("serve_less_streak=?", suffix)

    def test_the_suffix_reaches_the_printed_line(self) -> None:
        data = {
            "digest": {
                "host": "h", "capacity": {}, "problems": [],
                "supervisor_coverage": {"matched": 1, "expected": 1},
            },
            "observations": [{"supervisor": {
                "runner": "lane-01", "phase": "waiting", "vm": "",
                "heartbeat_age_secs": 3,
                "serving_blocked_since": "2026-09-20T00:00:00Z",
                "serving_blocked_streak": 143,
                "serving_blocked_last_phase": "admission-error",
            }}],
        }
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            macos_observe.print_human(data)
        printed = buffer.getvalue()
        self.assertIn("phase=waiting", printed)
        self.assertIn("serve_less_streak=143", printed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
