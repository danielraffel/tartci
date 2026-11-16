#!/usr/bin/env python3
"""The idle clock starts at "Listening for Jobs", not at listener launch.

GitHub's runner needs 40-68 s to reach listening (m1 p50 60 s), so a 60 s
idle clock started at launch killed 8-13% of VMs a moment before they could
be offered a job. These tests run runner.sh's own listener function against a
fake ssh guest, with the clock scaled down (idle 4 s, polls every 5 s).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_guest_heartbeat import Listener  # noqa: E402


class ListenerIdleClockTests(unittest.TestCase):
    def test_a_slow_listener_that_gets_a_job_just_after_listening_is_served(self) -> None:
        # Listening at 7 s is past the 4 s idle timeout measured from launch;
        # the job arrives a second later.
        listener = Listener(self, "sleep 7\necho 'Listening for Jobs'\nsleep 1\n"
                                  "echo 'Running job: build'\nsleep 8\nexit 0\n")
        result, _ = listener.run(stale=600, idle=4, cap=60)
        events = listener.event_text()
        self.assertIn("rc=0", result.stdout, result.stderr)
        self.assertIn("job_assigned", events)
        self.assertNotIn("idle_timeout", events)

    def test_a_listening_runner_with_no_job_is_killed_after_the_idle_timeout(self) -> None:
        listener = Listener(self, "sleep 2\necho 'Listening for Jobs'\nexec sleep 300\n")
        result, elapsed = listener.run(stale=600, idle=4, cap=60)
        events = listener.event_text()
        self.assertIn("rc=124", result.stdout, result.stderr)
        self.assertIn("idle_timeout", events)
        self.assertIn("since=listening", events)
        # Listening at 2 s, seen at the 5 s poll, idle 4 s later: killed at 10 s,
        # where a clock started at launch would have killed it at 5 s.
        self.assertGreaterEqual(elapsed, 9)
        self.assertLess(elapsed, 25)

    def test_a_listener_that_never_listens_is_killed_at_the_startup_cap(self) -> None:
        listener = Listener(self, "exec sleep 300\n")
        result, elapsed = listener.run(stale=600, idle=4, cap=12)
        events = listener.event_text()
        self.assertIn("rc=124", result.stdout, result.stderr)
        self.assertIn("listener_startup_timeout", events)
        self.assertIn("never_listening=1", events)
        self.assertNotIn("idle_timeout", events)
        self.assertGreaterEqual(elapsed, 12)
        self.assertLess(elapsed, 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
