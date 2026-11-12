#!/usr/bin/env python3
"""A non-canary host adopts a canary-reported fleet source commit only when it
is on the source repository's main; otherwise it stays on what it installed."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import testing_support  # noqa: E402,F401 - skips tomllib-only suites on 3.9
import fleet_self_update as su  # noqa: E402
import test_fleet_self_update as fixtures  # noqa: E402


class Routed:
    """The fixture's repository plus one commit held on an unmerged branch."""

    def __init__(self, repo, branch) -> None:
        self.repo, self.branch = repo, branch
        self.repository = repo.repository

    def __call__(self, path: str):
        if path.endswith(f"/commits/{self.branch.commit}"):
            self.repo.calls.append(path)
            return self.branch(path)
        return self.repo(path)


class CanaryCommitMustBeOnMain(fixtures.FleetSourceFixture):
    """The canary (studio) reports a commit; this host (m1) checks it is on main."""

    OFF_MAIN = "0f" * 20   # a commit the repository holds on an unmerged branch

    def setUp(self) -> None:
        # Base.setUp writes TARTCI_HOME and TARTCI_GH_CLI before the fixture's
        # own environ patch starts, so that patch restores them on cleanup.
        # Patch first, so every test leaves os.environ as it found it.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        self.cfg.fleet_source_canary = "studio"
        # main's head (COMMIT) contains OLDER; OFF_MAIN is a branch commit
        # whose bytes match, so only the ancestry check can tell them apart.
        self.branch = self.fixture.FakeRepository(
            self.repo.head.files, commit=self.OFF_MAIN, tree="b2" * 20)
        self.repo.head.ancestors = {self.OLDER}
        self.repo.head.off_main = {self.OFF_MAIN}
        self.sys.private = Routed(self.repo, self.branch)

    def installed_from(self, commit: str) -> None:
        receipt = json.loads(self.cfg.install_receipt.read_text())
        receipt["fleet_source"] = {
            "configured": True, "commit": commit, "repository": self.fixture.PRIVATE,
            "root": str(self.cfg.fleet_source_store / commit)}
        self.cfg.install_receipt.write_text(json.dumps(receipt))

    def canary(self, commit: str | None):
        record = None if commit is None else {
            "repository": self.fixture.PRIVATE, "commit": commit,
            "age": su.FLEET_SOURCE_SOAK_SECONDS}
        return mock.patch.object(su, "read_canary_verified", return_value=record)

    def test_a_canary_commit_off_main_is_refused_and_nothing_moves(self) -> None:
        self.installed_from(self.OLDER)
        with self.canary(self.OFF_MAIN), self.assertRaisesRegex(
                su.Refused, "is not on main.*staying on the installed"):
            su.sync_fleet_source(self.cfg, self.sys)
        self.assertFalse((self.cfg.fleet_source_store / self.OFF_MAIN).exists(),
                         "an off-main commit is never exported")
        self.assertEqual(self.OLDER, su.installed_fleet_source_commit(self.cfg))

    def test_a_canary_commit_on_main_is_adopted(self) -> None:
        # Control, same fixture: main's head passes the same check.
        self.installed_from(self.OLDER)
        with self.canary(self.fixture.COMMIT):
            selected = su.sync_fleet_source(self.cfg, self.sys)
        self.assertEqual(self.fixture.COMMIT, selected["commit"])
        self.assertTrue(any("/compare/" + self.fixture.COMMIT + "...main" in call
                            for call in self.repo.calls))

    def test_an_unreadable_compare_refuses_rather_than_adopting(self) -> None:
        self.installed_from(self.OLDER)
        self.repo.head.compare_fails = self.repo.older.compare_fails = True
        with self.canary(self.fixture.COMMIT), self.assertRaises(su.Refused):
            su.sync_fleet_source(self.cfg, self.sys)
        self.assertFalse((self.cfg.fleet_source_store / self.fixture.COMMIT).exists())

    def test_an_unreadable_canary_stays_on_the_installed_commit(self) -> None:
        # Tailscale down: the canary cannot be read, the installed commit (on
        # main) is kept, exactly as before the ancestry check.
        self.installed_from(self.OLDER)
        with self.canary(None):
            selected = su.sync_fleet_source(self.cfg, self.sys)
        self.assertEqual(self.OLDER, selected["commit"])
        self.assertIn("staying on the installed commit", selected["why"])


def load_tests(loader: unittest.TestLoader, tests, pattern):  # noqa: ARG001
    # Only this module's own tests: the imported fixture's base class carries
    # the whole self-update suite, which test_fleet_self_update already runs.
    suite = unittest.TestSuite()
    for name in sorted(vars(CanaryCommitMustBeOnMain)):
        if name.startswith("test_"):
            suite.addTest(CanaryCommitMustBeOnMain(name))
    return suite


if __name__ == "__main__":
    unittest.main()
