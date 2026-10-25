#!/usr/bin/env python3
"""The 3.9 test lane exists, and its tomllib guard skips only where it should.

A 3.9-only failure reached main four times on 2026-10-05 because no CI job ran
the tests under the hosts' /usr/bin/python3. These pin the job and the helper.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import testing_support  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class HelperTests(unittest.TestCase):
    def test_the_module_guard_skips_exactly_when_tomllib_is_missing(self) -> None:
        with mock.patch.object(testing_support, "HAVE_TOMLLIB", False):
            with self.assertRaises(unittest.SkipTest):
                testing_support.skip_module_without_tomllib()
        with mock.patch.object(testing_support, "HAVE_TOMLLIB", True):
            testing_support.skip_module_without_tomllib()

    def test_the_decorator_reflects_this_interpreter(self) -> None:
        self.assertEqual(testing_support.HAVE_TOMLLIB, sys.version_info >= (3, 11))


class CiLaneTests(unittest.TestCase):
    def test_ci_runs_every_test_module_under_python_3_9(self) -> None:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        job = text.split("  python-39-tests:", 1)[1].split("\n  python-floor:", 1)[0]
        self.assertIn('python-version: "3.9"', job)
        self.assertIn("raise SystemExit(0 if v == (3, 9) else 1)", job)
        # An explicit exit, not `! cmd`: set -e ignores a negated command, so
        # `!` would refuse only while it happened to be the step's last line.
        self.assertIn("if python3 -c 'import tomllib' 2>/dev/null; then", job)
        self.assertIn("exit 1", job[job.index("import tomllib' 2>/dev/null; then"):])
        self.assertNotIn("! python3 -c 'import tomllib'", job)
        self.assertIn("python3 -m unittest discover -s scripts -p 'test_*.py'", job)

    def test_the_3_9_lane_and_the_lint_job_pin_one_image_that_has_3_9(self) -> None:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        lint = text.split("  lint:", 1)[1].split("\n  python-39-tests:", 1)[0]
        job = text.split("  python-39-tests:", 1)[1].split("\n  python-floor:", 1)[0]
        # setup-python has no 3.9 for ubuntu-latest once that is 26.04.
        self.assertIn("runs-on: ubuntu-24.04", job)
        self.assertIn("runs-on: ubuntu-24.04", lint)
        self.assertNotIn("runs-on: ubuntu-latest", job + lint)

    def test_only_pull_request_runs_are_superseded(self) -> None:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        # A shared group would drop pending main runs even without
        # cancel-in-progress, so non-PR events must key on the run id.
        self.assertIn(
            "group: ${{ github.workflow }}-${{ github.event_name == 'pull_request'"
            " && github.ref || github.run_id }}",
            text,
        )
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name == 'pull_request' }}", text
        )


if __name__ == "__main__":
    unittest.main()
