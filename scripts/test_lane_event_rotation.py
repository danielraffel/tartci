#!/usr/bin/env python3
"""A lane's events.jsonl is bounded: runner.sh renames it to events.jsonl.1
between jobs once it reaches EVENT_LOG_MAX_BYTES, and the readers that look
back over a window read events.jsonl.1 first.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
sys.path.insert(0, str(ROOT / "scripts"))
import pool_usage  # noqa: E402
import pre_mint_outcomes  # noqa: E402
import serving_truth_alarm  # noqa: E402
import vm_dhcp_breaker  # noqa: E402


def _function(source: str, name: str) -> str:
    start = source.index(f"\n{name}(){{") + 1
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(ts: float, event: str, **extra) -> str:
    return json.dumps({"ts": _iso(ts), "event": event, "runner": "lane-01", "vm": "",
                       "detail": "", **extra})


class RunnerRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "events.jsonl"

    def rotate(self, cap: int) -> None:
        source = RUNNER.read_text()
        script = "\n".join([
            "set -u", "RUNNER_NAME=lane-01",
            f"EVENT_LOG_MAX_BYTES={cap}",
            _function(source, "json_sanitize"),
            _function(source, "event"),
            _function(source, "tartci_event_log_rotate"),
            "tartci_event_log_rotate",
        ])
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                env={**os.environ, "EVENT_LOG": str(self.log)}, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_full_log_moves_to_dot_one_and_a_new_log_says_so(self) -> None:
        self.log.write_text("old\n" * 100)
        self.rotate(cap=200)
        self.assertEqual(Path(f"{self.log}.1").read_text(), "old\n" * 100)
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([r["event"] for r in rows], ["event_log_rotated"])
        self.assertEqual(rows[0]["fields"]["bytes"], 400)

    def test_a_log_under_the_cap_is_left_alone(self) -> None:
        self.log.write_text("old\n" * 10)
        self.rotate(cap=200)
        self.assertEqual(self.log.read_text(), "old\n" * 10)
        self.assertFalse(Path(f"{self.log}.1").exists())

    def test_only_one_previous_file_is_kept(self) -> None:
        Path(f"{self.log}.1").write_text("oldest\n")
        self.log.write_text("newer\n" * 100)
        self.rotate(cap=200)
        self.assertEqual(Path(f"{self.log}.1").read_text(), "newer\n" * 100)
        self.assertFalse(Path(f"{self.log}.2").exists())

    def test_the_cap_is_16_mib_and_rotation_runs_only_between_jobs(self) -> None:
        source = RUNNER.read_text()
        self.assertIn("EVENT_LOG_MAX_BYTES=$((16 * 1024 * 1024))", source)
        # Mid-job the watchdog's guard must still find job_assigned in the
        # current file, so the only call is in the loop's no-VM branch.
        calls = [m.start() for m in re.finditer(r"^\s*tartci_event_log_rotate\s*$", source, re.M)]
        self.assertEqual(len(calls), 1)
        guard = source.rindex('if [ -z "$CURRENT_VM" ]; then', 0, calls[0])
        self.assertNotIn("\n    fi\n", source[guard:calls[0]])


class ReaderTests(unittest.TestCase):
    """Rows renamed into events.jsonl.1 are still read, oldest first."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lane = Path(self.temp.name) / "state" / "macos-fleet" / "pulp-gate"
        self.lane.mkdir(parents=True)
        self.now = time.time()
        self.log = self.lane / "events.jsonl"
        Path(f"{self.log}.1").write_text(
            _row(self.now - 3600, "clone_start") + "\n"
            + _row(self.now - 3590, "boot_ip") + "\n"
            + _row(self.now - 3500, "assignment_v2_pre_mint_denied") + "\n")
        self.log.write_text(_row(self.now - 60, "event_log_rotated") + "\n")

    def test_pool_usage_reads_the_previous_file(self) -> None:
        events = pool_usage.load_events(str(self.log), _iso(self.now - 7200))
        self.assertEqual([e["event"] for e in events],
                         ["clone_start", "boot_ip", "assignment_v2_pre_mint_denied",
                          "event_log_rotated"])

    def test_pre_mint_outcomes_counts_the_previous_file(self) -> None:
        rows = pre_mint_outcomes.outcomes({"pulp-gate": self.log})
        self.assertEqual(sum(r["denied"] for r in rows), 1)

    def test_serving_truth_reads_the_previous_file(self) -> None:
        rows = serving_truth_alarm.read_event_paths([self.log], self.now - 7200)
        self.assertEqual([r["event"] for r in rows][:2], ["clone_start", "boot_ip"])

    def test_dhcp_boot_times_read_the_previous_file(self) -> None:
        out = vm_dhcp_breaker.boot_times(Path(self.temp.name) / "state", self.now - 7200)
        self.assertEqual((out["clones"], out["boot_ip"]["n"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
