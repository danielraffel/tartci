#!/usr/bin/env python3
"""Tests for the host-level pool opt-out helpers (providers/common/pool.lib.sh)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers" / "common" / "pool.lib.sh"


def _bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


class ParticipationTests(unittest.TestCase):
    def test_absent_file_defaults_to_participating(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "nope"
            proc = _bash(f"source {LIB}; tartci_pool_read_participation {f}")
            self.assertEqual(proc.stdout.strip(), "1", proc.stderr)

    def test_explicit_zero_is_opted_out(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "p"
            f.write_text("0\n")
            proc = _bash(f"source {LIB}; tartci_pool_read_participation {f}")
            self.assertEqual(proc.stdout.strip(), "0", proc.stderr)

    def test_garbage_defaults_to_participating(self) -> None:
        # A corrupt/unexpected value must never silently pull a host out.
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "p"
            f.write_text("banana\n")
            proc = _bash(f"source {LIB}; tartci_pool_read_participation {f}")
            self.assertEqual(proc.stdout.strip(), "1", proc.stderr)

    def test_write_then_read_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "sub" / "p"  # exercises mkdir -p
            proc = _bash(
                f"source {LIB}; tartci_pool_write_participation 0 {f}; "
                f"tartci_pool_read_participation {f}"
            )
            self.assertEqual(proc.stdout.strip(), "0", proc.stderr)
            self.assertEqual(Path(f).read_text().strip(), "0")


class RunnerAgentEnumerationTests(unittest.TestCase):
    def _fixture_dir(self, td: str) -> Path:
        d = Path(td) / "LaunchAgents"
        d.mkdir()
        # active runner agents (should match)
        for name in (
            "com.danielraffel.pulp.tart-runner-linux.plist",
            "com.danielraffel.pulp.qemu-runner-windows.plist",
            "actions.runner.danielraffel-pulp.pulp-preamble-m5.plist",
        ):
            (d / name).write_text("<plist/>")
        # non-runner or inactive (should NOT match)
        for name in (
            "com.danielraffel.pulp.queue-saturation.plist",  # not a runner
            "com.danielraffel.pulp.tart-runner-linux.plist.pre-engage.bak",  # .bak
            "com.danielraffel.pulp.tart-runner-macos.plist.disabled-20260611",  # disabled
            "com.apple.something.plist",  # unrelated
        ):
            (d / name).write_text("<plist/>")
        return d

    def test_enumerates_only_active_runner_agents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = self._fixture_dir(td)
            proc = _bash(f"source {LIB}; tartci_pool_runner_agents {d} | sort")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            got = sorted(l for l in proc.stdout.splitlines() if l.strip())
            self.assertEqual(
                got,
                [
                    "actions.runner.danielraffel-pulp.pulp-preamble-m5",
                    "com.danielraffel.pulp.qemu-runner-windows",
                    "com.danielraffel.pulp.tart-runner-linux",
                ],
            )

    def test_missing_dir_is_empty_not_error(self) -> None:
        proc = _bash(f"source {LIB}; tartci_pool_runner_agents /no/such/dir; echo rc=$?")
        self.assertIn("rc=0", proc.stdout)
        self.assertEqual([l for l in proc.stdout.splitlines() if l != "rc=0"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
