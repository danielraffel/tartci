#!/usr/bin/env python3
"""A booted VM's SSH wait ends on wall-clock time, and heartbeats while it waits.

ConnectTimeout bounds only the TCP connect. A guest whose network answers but
whose sshd never completes the handshake holds each `ssh ... true` for about
60 s, so the old 90-attempt loop (meant as 180 s) ran for about 93 minutes and
wrote no heartbeat. m1's pulp-gate.slot2 sat in `booting` for 71+ minutes on
2026-10-09, and readiness could only say `heartbeat_stale`.

These tests run runner.sh's own tartci_vm_ssh_wait against a fake `ssh` that
hangs, so they fail if the per-attempt bound or the deadline is lost.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"


def function_body(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\{{\n(.*?)^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group(0)


class Harness:
    def __init__(self, test: unittest.TestCase, *, hang_secs: int, succeed_on: int = 0) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "calls"
        self.calls.write_text("")
        self.heartbeats = self.tmp / "heartbeats"
        self.heartbeats.write_text("")
        ssh = self.bin / "ssh"
        # Count the call, succeed on the Nth when asked, otherwise hang the way
        # a guest stuck in the handshake does.
        ssh.write_text(
            "#!/bin/bash\n"
            f"echo x >>{str(self.calls)!r}\n"
            f"n=$(wc -l <{str(self.calls)!r} | tr -d ' ')\n"
            f"[ {succeed_on} -gt 0 ] && [ \"$n\" -ge {succeed_on} ] && exit 0\n"
            f"sleep {hang_secs}\n"
            "exit 255\n")
        ssh.chmod(0o755)

    def run(self, *, deadline: int, per: int, phase: str = "booting",
            timeout: float = 60) -> tuple[subprocess.CompletedProcess, float]:
        source = RUNNER.read_text(encoding="utf-8")
        script = (
            "set -uo pipefail\n"
            f"TARTCI_ROOT={str(ROOT)!r}\n"
            "SSH_OPTS=(-o ConnectTimeout=10)\n"
            "SSH_KEY_PRIV=/dev/null\n"
            f"heartbeat(){{ echo \"$1\" >>{str(self.heartbeats)!r}; }}\n"
            + function_body(source, "tartci_vm_ssh_wait") + "\n"
            f"BOOT_SSH_HEARTBEAT_PHASE={phase!r}\n"
            "tartci_vm_ssh_wait admin@192.0.2.1; rc=$?\n"
            'echo "rc=$rc waited=$BOOT_SSH_WAITED_SECS attempts=$BOOT_SSH_ATTEMPTS"\n'
        )
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                   TARTCI_BOOT_SSH_DEADLINE_SECS=str(deadline),
                   TARTCI_BOOT_SSH_ATTEMPT_SECS=str(per))
        started = time.monotonic()
        result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                                env=env, timeout=timeout, check=False)
        return result, time.monotonic() - started

    def call_count(self) -> int:
        return len(self.calls.read_text().splitlines())

    def heartbeat_phases(self) -> list[str]:
        return self.heartbeats.read_text().split()


def summary(result: subprocess.CompletedProcess) -> dict[str, int]:
    line = result.stdout.strip().splitlines()[-1]
    return {k: int(v) for k, v in (pair.split("=") for pair in line.split())}


class BootSshWaitTests(unittest.TestCase):
    def test_a_hanging_handshake_ends_at_the_deadline_not_after_every_attempt(self) -> None:
        # Each attempt would hang 60 s. With the count bound this ran for
        # attempts x 60 s; with the deadline it ends near 6 s.
        h = Harness(self, hang_secs=60)
        result, elapsed = h.run(deadline=6, per=2)
        out = summary(result)
        self.assertEqual(out["rc"], 1, result.stderr)
        self.assertLess(elapsed, 15, f"the wait outlived its deadline: {elapsed:.1f}s")
        self.assertGreaterEqual(out["attempts"], 2)
        self.assertGreaterEqual(out["waited"], 5)

    def test_every_failed_attempt_refreshes_the_boot_phase(self) -> None:
        h = Harness(self, hang_secs=60)
        result, _ = h.run(deadline=6, per=2, phase="warm-booting")
        self.assertEqual(summary(result)["rc"], 1)
        phases = h.heartbeat_phases()
        self.assertGreaterEqual(len(phases), 2, phases)
        self.assertEqual(set(phases), {"warm-booting"})

    def test_a_guest_that_answers_on_the_third_try_succeeds(self) -> None:
        h = Harness(self, hang_secs=60, succeed_on=3)
        result, elapsed = h.run(deadline=60, per=1)
        out = summary(result)
        self.assertEqual(out["rc"], 0, result.stderr)
        self.assertEqual(out["attempts"], 3)
        self.assertEqual(h.call_count(), 3)
        self.assertLess(elapsed, 30)

    def test_a_guest_that_answers_at_once_writes_no_heartbeat(self) -> None:
        h = Harness(self, hang_secs=60, succeed_on=1)
        result, _ = h.run(deadline=60, per=5)
        self.assertEqual(summary(result)["attempts"], 1)
        self.assertEqual(h.heartbeat_phases(), [])

    def test_a_non_numeric_knob_falls_back_to_the_defaults(self) -> None:
        source = function_body(RUNNER.read_text(encoding="utf-8"), "tartci_vm_ssh_wait")
        self.assertIn("*[!0-9]*|0) deadline=180", source)
        self.assertIn("*[!0-9]*|0) per=15", source)


class BootHelperUsesTheBoundedWaitTests(unittest.TestCase):
    def test_the_boot_helper_waits_through_the_bounded_function(self) -> None:
        body = function_body(RUNNER.read_text(encoding="utf-8"), "boot_vm_to_ssh")
        self.assertIn('tartci_vm_ssh_wait "$VM_USER@$ip"', body)
        self.assertNotIn("seq 1 90", body, "an attempt-count bound is not a time bound")
        self.assertIn('BOOT_SSH_HEARTBEAT_PHASE="$boot_phase"', body)

    def test_the_failure_keeps_the_no_ssh_detail_its_readers_match(self) -> None:
        body = function_body(RUNNER.read_text(encoding="utf-8"), "boot_vm_to_ssh")
        self.assertIn('event boot_failed "no_ssh" \\', body)
        self.assertIn('"waited_s=$BOOT_SSH_WAITED_SECS"', body)


if __name__ == "__main__":
    unittest.main()
