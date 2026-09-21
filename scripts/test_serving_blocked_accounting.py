#!/usr/bin/env python3
"""Behavioural guards for the serve-less streak the supervisor keeps.

A lane can clone a VM every two minutes for three hours, refuse every one of
them at admission, and still report a fresh heartbeat, a running supervisor and
a loaded service the whole time. Every signal the fleet had was a signal about
liveness, and liveness is not service.

The counter that closes that gap only works if exactly one thing clears it.
A granted host reservation, a granted VM lease, a booted VM and a registered
runner each prove a step; only an assigned job proves the lane served. These
tests execute the shipped accounting out of `runner.sh` rather than asserting
its text, so a rewrite that keeps the wording and loses the property fails.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"

# The accounting the loop runs after every work entry, and the one it runs on an
# idle pass. Anchored to their surroundings so they cannot be matched anywhere
# else in the file.
ACCOUNTING_RE = re.compile(
    r'run_one "\$i" "\$selected_labels" "\$selected_tier" \|\| run_rc=\$\?\n'
    r'(?P<block>(?:.*?\n)*?      fi\n)',
)
IDLE_RE = re.compile(
    r'(?P<block>^      if \[ "\$\{q:-0\}" -le 0 \]; then\n(?:.*?\n)*?^      fi$)',
    re.MULTILINE,
)


def function_source(source: str, name: str) -> str:
    """Return a whole `name(){...}` definition, one-line or multi-line."""
    one_line = re.search(rf"(?m)^{re.escape(name)}\(\)\{{.*\}}$", source)
    if one_line is not None:
        return one_line.group(0)
    multi = re.search(
        rf"(?ms)^{re.escape(name)}\(\)\{{\n.*?^\}}$", source
    )
    if multi is None:
        raise AssertionError(f"missing function {name}")
    return multi.group(0)


def function_body(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\{{\n(.*?)^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group(1)


class ShippedAccounting:
    """Runs the real blocks out of runner.sh against a scripted work sequence."""

    def __init__(self) -> None:
        self.source = RUNNER.read_text()
        accounting = ACCOUNTING_RE.search(self.source)
        assert accounting is not None, (
            "the per-work-entry accounting no longer follows the run_one call"
        )
        self.accounting = accounting.group("block")
        idle = IDLE_RE.search(self.source)
        assert idle is not None, "the idle pass no longer clears the streak"
        self.idle = idle.group("block")

    def run(self, entries: list[str]) -> dict:
        """`entries` is a script of 'served', 'unserved' and 'idle' passes."""
        steps = []
        for entry in entries:
            if entry == "idle":
                steps.append('q=0\n' + self.idle)
                continue
            served = "1" if entry == "served" else "0"
            steps.append(
                f'CURRENT_SERVED={served}\n'
                'run_rc=0\n'
                + self.accounting
            )
        script = (
            "set -euo pipefail\n"
            'SERVING_BLOCKED_SINCE=""\n'
            "SERVING_BLOCKED_STREAK=0\n"
            'SERVING_BLOCKED_LAST_PHASE=""\n'
            'LAST_HEARTBEAT_PHASE="admission-error"\n'
            "CURRENT_SERVED=0\n"
            "q=1\n"
            + "\n".join(steps)
            + "\n"
            'printf \'{"since":"%s","streak":%s,"last_phase":"%s"}\\n\''
            ' "$SERVING_BLOCKED_SINCE" "$SERVING_BLOCKED_STREAK"'
            ' "$SERVING_BLOCKED_LAST_PHASE"\n'
        )
        result = subprocess.run(
            ["/bin/bash", "-c", script], text=True, capture_output=True,
            check=False, timeout=120,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"accounting harness failed rc={result.returncode}: {result.stderr}"
            )
        return json.loads(result.stdout.strip().splitlines()[-1])


class ServeLessStreakTests(unittest.TestCase):
    def setUp(self) -> None:
        self.accounting = ShippedAccounting()
        self.source = self.accounting.source

    # -- counting ------------------------------------------------------------

    def test_an_unserved_work_entry_increments_the_streak(self) -> None:
        value = self.accounting.run(["unserved"])
        self.assertEqual(value["streak"], 1)
        self.assertNotEqual(value["since"], "")

    def test_a_served_work_entry_clears_the_streak(self) -> None:
        value = self.accounting.run(["unserved", "unserved", "served"])
        self.assertEqual(value["streak"], 0)
        self.assertEqual(value["since"], "")
        self.assertEqual(value["last_phase"], "")

    def test_the_streak_start_is_the_first_failure_not_the_latest(self) -> None:
        """A marker rewritten on every failure would never appear to age, so a
        three-hour outage would read as a one-cycle blip forever."""
        first = self.accounting.run(["unserved"])["since"]
        later = self.accounting.run(["unserved"] * 8)["since"]
        # Same harness start, so an un-rewritten marker is byte-identical.
        self.assertEqual(first, later)

    def test_the_last_phase_travels_with_the_streak(self) -> None:
        value = self.accounting.run(["unserved"])
        self.assertEqual(value["last_phase"], "admission-error")

    # -- the ratio property --------------------------------------------------

    def test_interleaved_service_never_accumulates(self) -> None:
        """Why this is a streak and not an error count.

        A lane at the healthy cadence -- roughly 1.6 work entries an hour at
        about one job per mint -- logs occasional failures forever. An error
        count would climb past any fixed threshold given enough uptime; a
        streak cannot, because service resets it.
        """
        value = self.accounting.run(["unserved", "served"] * 40)
        self.assertEqual(value["streak"], 0)
        self.assertEqual(value["since"], "")

    def test_a_pure_failure_loop_accumulates_every_entry(self) -> None:
        """The measured outage ran about 35 work entries an hour and served
        nothing, so an hour of it clears a threshold of 6 by a wide margin."""
        value = self.accounting.run(["unserved"] * 35)
        self.assertEqual(value["streak"], 35)

    def test_the_thresholds_separate_the_two_cadences(self) -> None:
        healthy = self.accounting.run(["unserved", "served"] * 5)["streak"]
        failing = self.accounting.run(["unserved"] * 35)["streak"]
        self.assertLess(healthy, 6)
        self.assertGreaterEqual(failing, 6 * 5)

    # -- the idle false positive --------------------------------------------

    def test_an_idle_pass_clears_the_streak(self) -> None:
        """Zero VMs at rest is the designed state of an ephemeral on-demand
        fleet. Nothing is being refused when nothing is queued, so an idle pass
        must end a streak rather than keep inflating it."""
        value = self.accounting.run(["unserved", "unserved", "unserved", "idle"])
        self.assertEqual(value["streak"], 0)
        self.assertEqual(value["since"], "")

    def test_a_lane_that_is_only_ever_idle_never_blocks(self) -> None:
        value = self.accounting.run(["idle"] * 50)
        self.assertEqual(value["streak"], 0)
        self.assertEqual(value["since"], "")

    # -- only an assignment counts as service --------------------------------

    def test_run_one_never_clears_the_streak_itself(self) -> None:
        """The defect that made the outage invisible.

        `run_one` used to clear the marker the moment the VM lease was granted.
        A lease is one step of many, so a lane that took a lease, cloned, and
        was then refused at admission cleared its own evidence on every single
        cycle. Nothing inside `run_one` may clear it; the loop clears it, and
        only after an assignment.
        """
        body = function_body(self.source, "run_one")
        clears = [
            line for line in body.splitlines()
            if re.search(r'SERVING_BLOCKED_\w+=("" *|0 *)$', line)
        ]
        self.assertEqual(
            clears, [],
            "run_one must not clear the serve-less streak; only an assigned "
            f"job ends it. Found: {clears}",
        )

    def test_only_an_assignment_marks_the_entry_served(self) -> None:
        marks = [
            index for index, line in enumerate(self.source.splitlines())
            if line.strip() == "CURRENT_SERVED=1"
        ]
        self.assertEqual(len(marks), 1, "exactly one thing may prove service")
        lines = self.source.splitlines()
        self.assertIn("event job_assigned", lines[marks[0] + 1])

    def test_the_entry_flag_resets_before_the_first_early_return(self) -> None:
        """`run_one` can return before it reaches the other CURRENT_* resets,
        so a flag reset beside them would let one served entry mask every
        refusal that followed it."""
        body = function_body(self.source, "run_one").splitlines()
        # Statements only. Matching the word anywhere would also match the
        # prose of a comment that happens to describe a return.
        reset = next(
            index for index, line in enumerate(body)
            if line.strip() == "CURRENT_SERVED=0"
        )
        returns = [
            index for index, line in enumerate(body)
            if re.match(r"^\s*(return\b|\|\| \{ .*return\b)", line)
            or re.search(r"(?<!\S)return \S", line.split("#", 1)[0])
        ]
        self.assertTrue(returns, "run_one has no early return to guard against")
        self.assertLess(
            reset, min(returns),
            "the served flag must reset above every early return, or one "
            "served entry masks every refusal that follows it",
        )

    # -- publication ---------------------------------------------------------

    def test_the_heartbeat_publishes_the_streak(self) -> None:
        """Executed, not grepped: the state file is what every reader parses,
        so an unquoted or mistyped field has to fail here."""
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            script = "\n".join([
                "set -euo pipefail",
                function_source(self.source, "json_sanitize"),
                function_source(self.source, "heartbeat"),
                f'STATE_DIR={str(state_dir)!r}',
                'RUNNER_NAME="lane-01"',
                'HOST_NAME="studio"',
                'REPO="owner/repo"',
                'CURRENT_VM=""',
                'CURRENT_IP=""',
                'CURRENT_LABELS="pulp-build-vm"',
                'CURRENT_RUN_ID=""',
                'CURRENT_JOB_ID=""',
                'CURRENT_JOB_CAPTURE_STATUS="not-attempted"',
                'CURRENT_ASSIGNMENT_QUARANTINE="none"',
                'SUPERVISOR_PID="101"',
                'SUPERVISOR_PID_STARTED_AT="Mon Sep  1 00:00:00 2026"',
                'SERVING_BLOCKED_SINCE="2026-09-20T00:00:00Z"',
                "SERVING_BLOCKED_STREAK=143",
                'SERVING_BLOCKED_LAST_PHASE="admission-error"',
                "heartbeat waiting",
            ])
            result = subprocess.run(
                ["/bin/bash", "-c", script], text=True, capture_output=True,
                check=False, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads((state_dir / "lane-01.state.json").read_text())
        self.assertEqual(state["serving_blocked_since"], "2026-09-20T00:00:00Z")
        self.assertEqual(state["serving_blocked_streak"], 143)
        self.assertIsInstance(state["serving_blocked_streak"], int)
        self.assertEqual(state["serving_blocked_last_phase"], "admission-error")
        self.assertEqual(state["phase"], "waiting")

    def test_the_heartbeat_records_the_phase_the_streak_will_quote(self) -> None:
        body = function_body(self.source, "heartbeat")
        self.assertIn('LAST_HEARTBEAT_PHASE="$phase"', body)

    def test_the_markers_are_initialised(self) -> None:
        """`set -u` is in force, so an unset marker would abort the supervisor
        on its first heartbeat rather than degrade."""
        for line in (
            'SERVING_BLOCKED_SINCE=""',
            "SERVING_BLOCKED_STREAK=0",
            'SERVING_BLOCKED_LAST_PHASE=""',
            "CURRENT_SERVED=0",
            'LAST_HEARTBEAT_PHASE=""',
        ):
            with self.subTest(line=line):
                self.assertRegex(self.source, rf"(?m)^{re.escape(line)}$")


class PoolStatusRenderingTests(unittest.TestCase):
    """`tartci pool status` is where a human looks first, so it has to say it.

    The fleet reported `state: on`, `fleet ready: yes` and `5/5` verified
    supervisors for three hours while serving nothing. Those four lines were
    all true. The serving line is the one that was missing.
    """

    @staticmethod
    def render(fleet: dict) -> str:
        source = (ROOT / "tartci").read_text()
        start = source.index('python3 - "$fleet_readiness" <<\'PY\'\n')
        start += len('python3 - "$fleet_readiness" <<\'PY\'\n')
        block = source[start:source.index("\nPY\n", start)]
        result = subprocess.run(
            ["python3", "-c", block, json.dumps(fleet)],
            text=True, capture_output=True, check=False, timeout=60,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout

    @staticmethod
    def _fleet(serving: dict) -> dict:
        return {
            "managed": True, "fleet_ready": True,
            "verified_running_supervisors": 5, "expected_supervisors": 5,
            "problems": [], "serving": serving,
        }

    def test_a_blocked_lane_is_printed_beside_the_green_lines(self) -> None:
        out = self.render(self._fleet({
            "blocked": True,
            "blocked_lanes": [{
                "label": "studio.pulp-gate", "blocked_seconds": 10800,
                "streak": 143, "last_phase": "admission-error",
            }],
            "unmeasurable_lanes": [],
        }))
        self.assertIn("fleet ready: yes", out)
        self.assertIn("verified running supervisors: 5/5", out)
        self.assertIn("serving: BLOCKED", out)
        self.assertIn("studio.pulp-gate", out)
        self.assertIn("streak=143", out)
        self.assertIn("last_phase=admission-error", out)

    def test_a_serving_lane_prints_ok(self) -> None:
        out = self.render(self._fleet({
            "blocked": False, "blocked_lanes": [], "unmeasurable_lanes": [],
        }))
        self.assertIn("serving: ok", out)
        self.assertNotIn("BLOCKED", out)

    def test_an_unmeasurable_lane_never_prints_as_ok_without_saying_so(self) -> None:
        out = self.render(self._fleet({
            "blocked": False, "blocked_lanes": [], "unmeasurable_lanes": ["studio.old"],
        }))
        self.assertIn("serving unmeasurable: studio.old", out)

    def test_an_unchecked_host_prints_unknown_not_ok(self) -> None:
        out = self.render(self._fleet({
            "blocked": None, "blocked_lanes": [], "unmeasurable_lanes": [],
        }))
        self.assertIn("serving: unknown", out)
        self.assertNotIn("serving: ok", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
