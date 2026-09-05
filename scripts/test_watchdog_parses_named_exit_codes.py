#!/usr/bin/env python3
"""The watchdog must not read a dead service as healthy.

`launchctl print` reports a failed service two ways this parser used to lose:

  last exit code = 75: EX_TEMPFAIL   ← a NAMED sysexits code
        state = active               ← a NESTED block's state, not the job's

A bare `int()` raised on the named form and yielded None, which downstream reads
as "no non-zero exit recorded"; and because indentation is stripped, the last
`state =` line won, so a coalition's `active` masked the job's `not running`.
Both errors pointed the same way — toward healthy — and 75 is precisely the
code our runners exit with when they self-restart, so the one signal that
mattered was the one the watchdog could not see.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tartci_launchd_watchdog import parse_launchctl_print  # noqa: E402

# Shape taken from real `launchctl print gui/501/<label>` output.
DEAD_NAMED = """\tstate = not running
\tpid = 0
\tlast exit code = 75: EX_TEMPFAIL
\t\tstate = active
\t\tstate = active
"""
DEAD_BARE = "\tstate = not running\n\tlast exit code = 1\n\t\tstate = active\n"
HEALTHY = "\tstate = running\n\tpid = 4242\n\tlast exit code = (never exited)\n\t\tstate = active\n"


class WatchdogParsesFailedServices(unittest.TestCase):
    def test_named_sysexits_code_is_read(self) -> None:
        """75: EX_TEMPFAIL must parse as 75, not vanish into None."""
        state, code = parse_launchctl_print(DEAD_NAMED)
        self.assertEqual(code, 75)
        self.assertEqual(state, "not running")

    def test_nested_state_does_not_mask_the_job(self) -> None:
        """A coalition's `state = active` must not overwrite the job's state."""
        state, _ = parse_launchctl_print(DEAD_NAMED)
        self.assertNotEqual(state, "active")

    def test_bare_code_still_parses(self) -> None:
        """Negative control: the case that always worked must keep working."""
        self.assertEqual(parse_launchctl_print(DEAD_BARE)[1], 1)

    def test_never_exited_is_still_none(self) -> None:
        """Negative control: a healthy service must NOT gain a bogus exit code,
        or the watchdog would report every running service as failed."""
        state, code = parse_launchctl_print(HEALTHY)
        self.assertIsNone(code)
        self.assertEqual(state, "running")


if __name__ == "__main__":
    unittest.main()
