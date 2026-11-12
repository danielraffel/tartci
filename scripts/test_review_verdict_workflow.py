#!/usr/bin/env python3
"""The review-verdict workflow must keep its job honest.

The job is the required ``review-verdict`` check run, so it must not be able
to pass on a refused head: it runs the pinned poster (which exits non-zero on
a refusal or a read error) without swallowing its exit code, still re-runs on
every verdict path, and may re-run the head's own run.
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


class ReviewVerdictWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.verdict = (WORKFLOWS / "review-verdict.yml").read_text(encoding="utf-8")

    def test_the_poster_exit_code_is_not_swallowed(self) -> None:
        for forbidden in ("continue-on-error", "|| true", "|| exit 0", "set +e"):
            self.assertNotIn(forbidden, self.verdict)
        self.assertIn("scripts/review_verdict_status.py'", self.verdict)

    def test_the_rule_is_pinned_to_a_shipyard_commit(self) -> None:
        self.assertRegex(self.verdict, r"repository: danielraffel/Shipyard\n\s+(#.*\n\s+)*ref: [0-9a-f]{40}\n")

    def test_every_verdict_path_still_runs_the_job(self) -> None:
        for trigger in ("pull_request_target:", "issue_comment:", "workflow_run:"):
            self.assertIn(trigger, self.verdict)
        self.assertIn("workflows: [Review verdict trigger]", self.verdict)
        trigger = (WORKFLOWS / "review-verdict-trigger.yml").read_text(encoding="utf-8")
        self.assertIn("pull_request_review:", trigger)
        self.assertIn("permissions: {}", trigger)

    def test_the_job_may_re_run_the_heads_own_run(self) -> None:
        permissions = re.search(r"\npermissions:\n((?:  .*\n|\s*#.*\n)+)", self.verdict)
        self.assertIsNotNone(permissions)
        self.assertIn("actions: write", permissions.group(1))


if __name__ == "__main__":
    unittest.main()
