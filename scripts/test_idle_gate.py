#!/usr/bin/env python3
"""Behavioral tests for the priority-aware idle gate in providers/tart-macos/runner.sh.

The idle gate lets a SECONDARY macOS lane (e.g. a sanitizer or coverage VM)
yield its VM slot to a higher-priority lane (the required build gate) so it can
never starve it on a shared-cap host — the failure that backed out the Pulp
coverage VM lane. The decision hinges on `priority_demand()`, which counts how
many priority-lane jobs are waiting/running by checking whether each job's
requested labels are a SUBSET of the priority lane's advertised labels (GitHub's
assignment rule: a runner serves a job iff it advertises every requested label).

Rather than assert the source contains a string (which gave false confidence
before — see the inverted-subset bug fixed in Pulp #4087), we EXTRACT the exact
pure matcher snippet from the script and run it against synthetic job data, so
an inverted/incorrect subset check is caught by behavior. No gh/tart needed, so
this runs on any platform in CI.

Run:  python3 scripts/test_idle_gate.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "providers" / "tart-macos" / "runner.sh"

# The priority-demand matcher is the only block shaped `import json, os, sys` /
# `want = {s.lower() ...}` (queued_work() uses a different multi-line import),
# so this regex pins exactly that snippet.
MATCHER_RE = re.compile(
    r"(import json, os, sys\nwant = \{s\.lower.*?print\(n\)\n)", re.S
)


class IdleGateMatcherTests(unittest.TestCase):
    """Behavioral test of the priority_demand subset matcher."""

    def setUp(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        m = MATCHER_RE.search(body)
        self.assertIsNotNone(
            m, "priority-demand matcher snippet not found in runner.sh"
        )
        self.snippet = m.group(1)

    def _count(self, priority_labels: list[str], jobs: list[list[str]]) -> int:
        """Run the extracted matcher: how many `jobs` would land on a priority
        runner advertising `priority_labels` (i.e. count as priority demand)."""
        env = {**os.environ, "LABEL_JSON": json.dumps(priority_labels)}
        stdin = "".join(json.dumps(j) + "\n" for j in jobs)
        r = subprocess.run(
            ["python3", "-c", self.snippet],
            input=stdin, capture_output=True, text=True, check=True, env=env,
        )
        return int(r.stdout.strip())

    def test_priority_job_subset_counts_as_demand(self) -> None:
        # A required-gate job requesting the gate pool labels lands on the gate
        # runner → it is real demand the secondary lane must yield to.
        gate = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]
        job = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]
        self.assertEqual(self._count(gate, [job]), 1)

    def test_extra_priority_runner_label_still_matches(self) -> None:
        # The gate runner may advertise an extra per-host label; a job requesting
        # only the shared pool set is still satisfiable there → still demand.
        gate = ["self-hosted", "macOS", "ARM64", "pulp-build",
                "pulp-build-vm", "pulp-build-vm-secondary"]
        job = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]
        self.assertEqual(self._count(gate, [job]), 1)

    def test_unrelated_job_is_not_demand(self) -> None:
        # The secondary lane's OWN job (needs pulp-sanitizer-vm-macos, which the
        # gate runner does NOT advertise) is not priority demand — otherwise the
        # gate would never let the secondary boot at all.
        gate = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]
        job = ["self-hosted", "macOS", "ARM64", "pulp-sanitizer-vm-macos"]
        self.assertEqual(self._count(gate, [job]), 0)

    def test_match_is_case_insensitive(self) -> None:
        gate = ["self-hosted", "macos", "arm64", "pulp-build", "pulp-build-vm"]
        job = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]
        self.assertEqual(self._count(gate, [job]), 1)

    def test_counts_multiple_demanding_jobs(self) -> None:
        gate = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]
        job = ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]
        self.assertEqual(self._count(gate, [job, job, job]), 3)

    def test_empty_job_labels_are_not_demand(self) -> None:
        # A job with no requested labels must not be counted (the `labels and`
        # guard) — an empty set is a vacuous subset of everything.
        gate = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]
        self.assertEqual(self._count(gate, [[]]), 0)

    def test_malformed_lines_are_skipped_not_counted(self) -> None:
        gate = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]
        good = ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]
        env = {**os.environ, "LABEL_JSON": json.dumps(gate)}
        stdin = "not json\n" + json.dumps(good) + "\n{ bad\n"
        r = subprocess.run(
            ["python3", "-c", self.snippet],
            input=stdin, capture_output=True, text=True, check=True, env=env,
        )
        self.assertEqual(int(r.stdout.strip()), 1)


class IdleGateWiringTests(unittest.TestCase):
    """Pin the gate-decision contract: the loop must consult priority_demand and
    the feature must default OFF so the primary gate runner is unaffected."""

    def setUp(self) -> None:
        self.body = SCRIPT.read_text(encoding="utf-8")

    def test_script_syntax_is_valid(self) -> None:
        r = subprocess.run(["bash", "-n", str(SCRIPT)],
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_feature_defaults_off(self) -> None:
        # YIELD_WORKFLOW empty by default → priority_demand short-circuits to 0
        # → the boot condition `p -eq 0` is always true → no behavior change for
        # the primary gate runner / release lane.
        self.assertIn('YIELD_WORKFLOW="${TARTCI_YIELD_TO_WORKFLOW_NAME:-}"', self.body)
        self.assertIn('[ -n "$YIELD_WORKFLOW" ] || { printf \'%s\\n\' 0; return 0; }',
                      self.body)

    def test_loop_gate_consults_priority_demand(self) -> None:
        # The boot decision must include the priority-demand==0 clause.
        self.assertIn('p="$(priority_demand)"', self.body)
        self.assertIn('[ "${p:-0}" -eq 0 ]', self.body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
