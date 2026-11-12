#!/usr/bin/env python3
"""Structural guards for the VM-lease denial heartbeat.

`--loop` heartbeats on every branch that waits, but the branch that reaches
work does not: it calls `run_one`, which runs hundreds of lines before its
first heartbeat and can return early when the VM lease is denied. A supervisor
that keeps winning a host reservation and losing the lease therefore goes
completely silent while healthy and looping, and a checker that can only read
heartbeat age has no choice but to call it stale.

These guards pin the two halves that have to hold together: the denial path
reports itself, and it records when the blocked streak began so "declining
right now" stays distinguishable from "never serving again".

The streak this path opens is shared with every other cause of a work entry
that serves nothing; the counting and the clearing live in the loop and are
covered by test_serving_blocked_accounting.py.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"


def function_body(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\{{\n(.*?)^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group(1)


# run_one reaches the lease through boot_vm_to_ssh (shared with the warm-VM
# park), which reports a lease-store refusal as BOOT_LEASE_DENIED=1.
BOOT_CALL = 'boot_vm_to_ssh "$i"'


class LeaseDeniedHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = RUNNER.read_text()

    def test_the_boot_helper_owns_the_acquisition_and_flags_a_denial(self) -> None:
        body = function_body(self.source, "boot_vm_to_ssh")
        tail = body[body.index("tartci_acquire_vm_lease"):]
        self.assertIn("|| lease_rc=$?", tail)
        denial = tail[tail.index('if [ "$lease_rc" -ne 0 ]; then'):]
        self.assertIn("BOOT_LEASE_DENIED=1", denial[:denial.index("\n  fi")])
        self.assertIn('return "$lease_rc"', denial)

    def test_a_denied_vm_lease_heartbeats_before_returning(self) -> None:
        body = function_body(self.source, "run_one")
        acquire = body.index(BOOT_CALL)
        tail = body[acquire:]
        denial = tail.index('if [ "$lease_rc" -ne 0 ] && [ "$BOOT_LEASE_DENIED" = 1 ]; then')
        close = tail.index("\n    fi", denial)
        self.assertIn(
            "heartbeat vm-lease-denied", tail[denial:close],
            "the lease-denial path must report itself; without a heartbeat a "
            "healthy supervisor is indistinguishable from a stopped one",
        )

    def test_the_denial_path_returns_the_real_lease_status(self) -> None:
        """`if ! cmd; then` would make `$?` the negation, silently turning a
        denial into success, so the status is captured before the branch."""
        body = function_body(self.source, "run_one")
        tail = body[body.index(BOOT_CALL):]
        self.assertIn("|| lease_rc=$?", tail)
        self.assertIn('return "$lease_rc"', tail)

    def test_the_blocked_streak_records_its_start_not_its_latest_denial(self) -> None:
        """A blocked supervisor cycles through other phases between denials, so
        a marker rewritten on every denial would never appear to age."""
        body = function_body(self.source, "run_one")
        tail = body[body.index(BOOT_CALL):]
        guard = re.search(
            r'\[ -n "\$SERVING_BLOCKED_SINCE" \]\s*\\?\s*\n?\s*\|\| SERVING_BLOCKED_SINCE=',
            tail,
        )
        self.assertIsNotNone(
            guard, "the streak start must only be set when not already set"
        )

    def test_a_granted_lease_does_not_clear_the_streak(self) -> None:
        """A granted lease is one step, not service.

        This path used to clear the marker the moment the lease was granted,
        which is correct only if losing the lease is the sole way to fail. It
        is not: a lane could take the lease, clone, and be refused at admission
        every cycle for hours, clearing its own evidence each time. The clear
        now lives in the loop and fires only after a job is assigned, so the
        denial path here sets the marker and nothing here unsets it.
        """
        body = function_body(self.source, "run_one")
        tail = body[body.index(BOOT_CALL):]
        denial_end = tail.index('return "$lease_rc"')
        self.assertNotIn('SERVING_BLOCKED_SINCE=""', tail[denial_end:])

    def test_an_empty_queue_clears_the_streak(self) -> None:
        """Nothing is being denied when there is no work, so an idle pass must
        not keep inflating a streak that started under contention. A lane with
        no VMs and no demand is the designed resting state of an ephemeral
        fleet, and must never read as blocked."""
        self.assertRegex(
            self.source,
            r'(?m)^      if \[ "\$\{q:-0\}" -le 0 \]; then\n'
            r'        SERVING_BLOCKED_SINCE=""$',
        )

    def test_the_heartbeat_publishes_the_streak(self) -> None:
        body = function_body(self.source, "heartbeat")
        self.assertIn('"serving_blocked_since":"$(json_sanitize "$SERVING_BLOCKED_SINCE")"', body)

    def test_the_streak_marker_is_initialised(self) -> None:
        """`set -u` is in force, so an unset marker would abort the supervisor
        on its first heartbeat rather than degrade."""
        self.assertRegex(self.source, r'(?m)^SERVING_BLOCKED_SINCE=""$')


if __name__ == "__main__":
    unittest.main()
