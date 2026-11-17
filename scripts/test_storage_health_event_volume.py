#!/usr/bin/env python3
"""The macOS lane writes a healthy store probe to its event log on change only.

The probe runs on every idle poll. Before this rule every healthy verdict
appended a `storage_health` row, about 3,700 a day per idle lane and most of
each lane's events.jsonl. These tests run the real shell functions from
providers/tart-macos/runner.sh against a scripted probe and a fake clock.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"


def _function(source: str, name: str) -> str:
    start = source.index(f"\n{name}(){{") + 1
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


def _assignment(source: str, name: str) -> str:
    match = re.search(rf"^{name}=.*$", source, re.M)
    if match is None:
        raise AssertionError(f"{name} is not assigned at top level in runner.sh")
    return match.group(0)


class StorageHealthEventVolumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        scripts = self.dir / "root" / "scripts"
        scripts.mkdir(parents=True)
        # The probe pops its next exit code from a queue file.
        (scripts / "storage_health.py").write_text(textwrap.dedent("""\
            import pathlib, sys
            q = pathlib.Path(sys.argv[0]).with_name("queue")
            codes = q.read_text().split()
            q.write_text(" ".join(codes[1:]))
            if codes[0] != "0":
                print("probe_timeout")
            sys.exit(int(codes[0]))
        """))
        (scripts / "tcc_wedge.py").write_text("")
        self.queue = scripts / "queue"
        self.clock = self.dir / "clock"
        self.events = self.dir / "events.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_probes(self, steps: list[tuple[int, int]]) -> list[dict]:
        """Run one supervisor with STEPS of (probe exit code, clock seconds)."""
        source = RUNNER.read_text()
        self.queue.write_text(" ".join(str(rc) for rc, _ in steps))
        calls = "\n".join(
            f'echo {now} >"$CLOCK"; tartci_storage_health_check /store || true'
            for _, now in steps)
        script = "\n".join([
            "set -u",
            'date(){ if [ "${1:-}" = +%s ]; then cat "$CLOCK"; else command date "$@"; fi; }',
            "heartbeat(){ :; }",
            "note(){ :; }",
            'RUNNER_NAME=lane-01; SLOT=1; STORAGE_HEALTH_TIMEOUT=5',
            _assignment(source, "STORAGE_HEALTH_STATE"),
            _assignment(source, "STORAGE_HEALTH_OK_EVENT_SECS"),
            _assignment(source, "STORAGE_HEALTH_OK_EVENT_AT"),
            _function(source, "json_sanitize"),
            _function(source, "event"),
            _function(source, "tartci_storage_health_check"),
            calls,
        ])
        env = dict(os.environ, TARTCI_ROOT=str(self.dir / "root"),
                   EVENT_LOG=str(self.events), CLOCK=str(self.clock))
        env.pop("TARTCI_STORAGE_HEALTH_STATE", None)
        env.pop("TARTCI_QUEUE_LANE_ID", None)
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def kinds(self, rows: list[dict]) -> list[str]:
        return [row["event"] for row in rows]

    def test_repeated_healthy_probes_write_one_row(self) -> None:
        rows = self.run_probes([(0, 1000), (0, 1022), (0, 1044), (0, 1066)])
        self.assertEqual(self.kinds(rows), ["storage_health"])
        self.assertEqual(rows[0]["fields"], {"state": "ok", "path": "/store"})

    def test_a_failure_is_written_every_probe_and_recovery_is_written(self) -> None:
        rows = self.run_probes([(0, 1000), (1, 1022), (1, 1044), (0, 1066), (0, 1088)])
        self.assertEqual(self.kinds(rows), [
            "storage_health", "yielded_storage_health", "yielded_storage_health",
            "storage_health"])

    def test_a_healthy_breadcrumb_is_written_each_interval(self) -> None:
        source = RUNNER.read_text()
        interval = int(_assignment(source, "STORAGE_HEALTH_OK_EVENT_SECS").split("=", 1)[1])
        rows = self.run_probes([(0, 1000), (0, 1000 + interval - 1),
                                (0, 1000 + interval), (0, 1000 + interval + 22)])
        self.assertEqual(self.kinds(rows), ["storage_health", "storage_health"])

    def test_the_check_still_returns_the_probe_verdict(self) -> None:
        source = RUNNER.read_text()
        self.queue.write_text("0 0 1")
        script = "\n".join([
            'date(){ if [ "${1:-}" = +%s ]; then echo 1000; else command date "$@"; fi; }',
            "heartbeat(){ :; }", "note(){ :; }",
            'RUNNER_NAME=lane-01; SLOT=1; STORAGE_HEALTH_TIMEOUT=5',
            _assignment(source, "STORAGE_HEALTH_STATE"),
            _assignment(source, "STORAGE_HEALTH_OK_EVENT_SECS"),
            _assignment(source, "STORAGE_HEALTH_OK_EVENT_AT"),
            _function(source, "json_sanitize"),
            _function(source, "event"),
            _function(source, "tartci_storage_health_check"),
            'tartci_storage_health_check /store; a=$?',
            'tartci_storage_health_check /store; b=$?',
            'tartci_storage_health_check /store; c=$?',
            'echo "$a $b $c $STORAGE_HEALTH_STATE"',
        ])
        env = dict(os.environ, TARTCI_ROOT=str(self.dir / "root"),
                   EVENT_LOG=str(self.events))
        env.pop("TARTCI_STORAGE_HEALTH_STATE", None)
        out = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(out.stdout.split(), ["0", "0", "75", "unknown"], out.stderr)


if __name__ == "__main__":
    unittest.main()
