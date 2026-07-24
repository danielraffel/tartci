#!/usr/bin/env python3
"""Regression coverage for the Linux Actions runner's inherited file mode.

The GitHub Actions runner must start under the canonical ``0022`` umask. A
group-writable ambient umask changes archive fixtures and install receipts,
which makes clean Linux validation depend on how the golden launched the
runner. The provider also logs and asserts the effective value immediately
before ``run.sh`` so every job retains direct smoke evidence.
"""
from __future__ import annotations

import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "providers" / "tart-linux" / "runner.sh"


class LinuxRunnerUmaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.body = SCRIPT.read_text(encoding="utf-8")

    def test_canonical_umask_is_set_logged_and_asserted_before_runner(self) -> None:
        set_pos = self.body.index("umask 0022")
        read_pos = self.body.index('runner_umask=\\"\\$(umask)\\"', set_pos)
        log_pos = self.body.index("TARTCI_DIAG runner_umask=%s", read_pos)
        assert_pos = self.body.index('[ \\"\\$runner_umask\\" = 0022 ]', log_pos)
        launch_pos = self.body.index("./run.sh --jitconfig", assert_pos)

        self.assertLess(set_pos, read_pos)
        self.assertLess(read_pos, log_pos)
        self.assertLess(log_pos, assert_pos)
        self.assertLess(assert_pos, launch_pos)

    def test_runner_launch_has_only_one_explicit_umask_policy(self) -> None:
        self.assertEqual(self.body.count("umask 0022"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
