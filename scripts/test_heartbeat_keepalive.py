#!/usr/bin/env python3
"""An idle lane's heartbeat stays fresh through a long queue scan.

`tartci pool supply` reads a heartbeat older than 120 s as a dead supervisor
and reports the whole host `unknown`. The scan is silent for 90-200 s on a
busy host, so the keepalive re-writes the current phase until the supervisor's
next heartbeat, and never outlives the supervisor.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers/tart-macos/heartbeat-keepalive.lib.sh"
RUNNER = ROOT / "providers/tart-macos/runner.sh"


def heartbeat_function() -> str:
    """runner.sh's heartbeat() prologue with the state-file write stubbed to a log line."""
    source = RUNNER.read_text(encoding="utf-8")
    body = source[source.index("heartbeat(){\n"):]
    prologue = body[:body.index("  ts=")]
    return (prologue
            + '  printf "%s %s %s\\n" "$(date +%s)" "$phase" "${BASHPID:-$$}" >>"$LOG"\n'
            + "}\n")


class Keepalive:
    def __init__(self, test: unittest.TestCase) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        test.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.log = self.tmp / "heartbeats"
        self.log.touch()

    def script(self, body: str, interval: int = 1) -> str:
        return (
            "set -euo pipefail\n"
            f"LOG={str(self.log)!r}\n"
            "SUPERVISOR_PID=$$\n"
            f"export TARTCI_HEARTBEAT_KEEPALIVE_SECS={interval}\n"
            f"source {str(LIB)!r}\n"
            + heartbeat_function()
            + body
        )

    def run(self, body: str, interval: int = 1, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/bash", "-c", self.script(body, interval)],
                              capture_output=True, text=True, timeout=timeout, check=False)

    def phases(self) -> list[str]:
        return [line.split()[1] for line in self.log.read_text().splitlines()]


class KeepaliveTests(unittest.TestCase):
    def test_a_long_silent_step_keeps_the_current_phase_fresh(self) -> None:
        k = Keepalive(self)
        result = k.run("heartbeat backoff\ntartci_heartbeat_keepalive_start\nsleep 3.5\n"
                       "heartbeat loop\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        phases = k.phases()
        self.assertEqual(phases[0], "backoff")
        self.assertEqual(phases[-1], "loop")
        # Refreshed at least twice during the 3.5 s step, and only the old phase.
        self.assertGreaterEqual(phases.count("backoff"), 3, phases)
        self.assertEqual(set(phases[1:-1]), {"backoff"})

    def test_the_next_heartbeat_stops_it_and_nothing_overwrites_the_new_phase(self) -> None:
        k = Keepalive(self)
        result = k.run("heartbeat waiting\ntartci_heartbeat_keepalive_start\nsleep 1.5\n"
                       "heartbeat booting\nsleep 3\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        phases = k.phases()
        self.assertEqual(phases[-1], "booting", phases)
        self.assertEqual(phases.count("booting"), 1, phases)

    def test_stop_is_prompt_with_a_long_interval(self) -> None:
        k = Keepalive(self)
        started = time.monotonic()
        result = k.run("heartbeat waiting\ntartci_heartbeat_keepalive_start\n"
                       "tartci_heartbeat_keepalive_stop\n", interval=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_dead_supervisor_is_not_kept_alive(self) -> None:
        k = Keepalive(self)
        proc = subprocess.Popen(["/bin/bash", "-c", k.script(
            "heartbeat waiting\ntartci_heartbeat_keepalive_start\nsleep 30\n")])
        time.sleep(2.5)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        time.sleep(1.5)
        count = len(k.phases())
        time.sleep(3)
        self.assertEqual(len(k.phases()), count, "the keepalive outlived its supervisor")
        self.assertGreaterEqual(count, 2, "control: it did refresh while the supervisor lived")

    def test_off_and_invalid_configuration(self) -> None:
        k = Keepalive(self)
        result = k.run("heartbeat waiting\ntartci_heartbeat_keepalive_start\nsleep 2.5\n",
                       interval=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(k.phases(), ["waiting"])
        for bad in ("x", "111"):
            bad_run = subprocess.run(
                ["/bin/bash", "-c", f"source {str(LIB)!r}; tartci_heartbeat_keepalive_validate"],
                env={**os.environ, "TARTCI_HEARTBEAT_KEEPALIVE_SECS": bad},
                capture_output=True, text=True, check=False)
            self.assertEqual(bad_run.returncode, 2, bad)


class RunnerWiringTests(unittest.TestCase):
    def test_the_loop_scan_runs_under_the_keepalive(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        loop = source.index('if [ "$LOOP" = 1 ]; then')
        start = source.index("tartci_heartbeat_keepalive_start", loop)
        scan = source.index('selection="$(select_work)"', loop)
        self.assertLess(start, scan)
        self.assertNotIn("\n    heartbeat ", source[start:scan])

    def test_heartbeat_and_cleanup_stop_it(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        for fn in ("heartbeat(){", "cleanup(){"):
            body = source[source.index(fn):]
            body = body[:body.index("\n}\n")]
            self.assertIn("tartci_heartbeat_keepalive_stop", body, fn)
        self.assertTrue(re.search(r"tartci_heartbeat_keepalive_validate \|\| die", source))


if __name__ == "__main__":
    unittest.main()
