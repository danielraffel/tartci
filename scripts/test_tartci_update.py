#!/usr/bin/env python3
"""tartci update: the agent-facing front of the self-update engine."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import testing_support

testing_support.skip_module_without_tomllib()

import fleet_self_update as engine  # noqa: E402
import tartci_update as tu  # noqa: E402

INSTALLED = "a" * 40
MAIN = "b" * 40


class UpdateFront(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.home = Path(self.td.name)
        self.state = engine.state_dir_for(self.home)
        (self.state / "attempts").mkdir(parents=True)
        engine._write_json(self.state / "skew.json",
                           {"installed": INSTALLED, "main": MAIN, "behind": 2,
                            "state": "behind", "measured_at": "2027-01-01T00:00:00Z"})

    def attempt(self, name: str, status: str, **extra) -> None:
        engine._write_json(self.state / "attempts" / f"{name}.json",
                           {"mode": "apply", "status": status, "target": MAIN,
                            "started_at": "2027-01-01T00:00:00Z", "steps": [], **extra})

    def run_main(self, *argv: str, plan: str = "", plan_rc: int = 0,
                 apply_rc: int = 0, tty: bool = False, answer: str | None = "n"):
        calls: list[list[str]] = []

        def fake_engine(args: list[str]) -> int:
            calls.append(args)
            if "--plan" in args:
                print(plan)
                return plan_rc
            if "--apply" in args:
                print("self-update: install: applied")
                return apply_rc
            return 0

        out = io.StringIO()
        with mock.patch.object(engine, "main", fake_engine), \
                mock.patch.object(tu, "interactive", return_value=tty), \
                mock.patch("builtins.input", return_value=answer,
                           side_effect=None if answer is not None else AssertionError("asked")), \
                contextlib.redirect_stdout(out):
            rc = tu.main([*argv, "--home", str(self.home)])
        return rc, out.getvalue(), calls

    # --status
    def test_status_is_one_line(self) -> None:
        self.attempt("20270101T000000Z-x", "succeeded")
        rc, out, _ = self.run_main("--status")
        self.assertEqual(0, rc)
        self.assertEqual(
            "tartci aaaaaaa 2 behind main bbbbbbb | last succeeded bbbbbbb 2027-01-01T00:00:00Z",
            out.strip())

    def test_status_names_the_live_step(self) -> None:
        engine._write_json(self.state / "active.json", {"pid": os.getpid(), "target": MAIN})
        self.attempt("20270101T000000Z-x", "running",
                     steps=[{"at": "2027-01-01T00:00:00Z", "step": "wait-idle",
                             "detail": "waiting on lane: busy; more"}])
        self.assertIn("UPDATING to bbbbbbb: wait-idle", tu.one_line(tu.snapshot(self.state)))
        self.assertIn("(waiting on lane: busy)", tu.one_line(tu.snapshot(self.state)))

    def test_status_json_parses(self) -> None:
        rc, out, _ = self.run_main("--status", "--json")
        self.assertEqual(2, json.loads(out)["behind"])

    # --wait
    def test_wait_returns_the_outcome_as_its_exit(self) -> None:
        for status, code in (("succeeded", 0), ("refused", 3), ("failed", 4)):
            with self.subTest(status=status):
                self.attempt(f"2027010{code}T000000Z-x", status, error="boom")
                rc, out, _ = self.run_main("--wait")
                self.assertEqual(code, rc)
                self.assertIn(f"last {status}", out)

    def test_wait_blocks_until_the_update_ends_and_times_out(self) -> None:
        engine._write_json(self.state / "active.json", {"pid": os.getpid(), "target": MAIN})
        self.attempt("20270101T000000Z-x", "running")
        rc, out, _ = self.run_main("--wait", "--timeout", "0.05", "--poll", "0.01")
        self.assertEqual(tu.EXIT_TIMED_OUT, rc)
        self.assertTrue(out.startswith("timed out waiting"))

    # applying
    WOULD = "self-update: tartci: 2 commits behind\nself-update: plan: would snapshot ..."

    def test_without_a_terminal_it_never_applies(self) -> None:
        rc, out, calls = self.run_main(plan=self.WOULD)
        self.assertEqual(0, rc)
        self.assertNotIn("--apply", sum(calls, []))
        self.assertIn("pass --yes", out)

    def test_yes_applies_and_prints_one_line(self) -> None:
        rc, out, calls = self.run_main("--yes", plan=self.WOULD)
        self.assertEqual(0, rc)
        self.assertIn("--apply", calls[-1])
        self.assertIn("full output:", out.splitlines()[-1])
        self.assertIn("install: applied",
                      (self.state / "last-update-output.log").read_text())

    def test_a_terminal_asks_and_no_means_no(self) -> None:
        rc, out, calls = self.run_main(plan=self.WOULD, tty=True, answer="n")
        self.assertNotIn("--apply", sum(calls, []))
        self.assertIn("not applied", out)
        self.assertNotIn("pass --yes", out)
        rc, out, calls = self.run_main(plan=self.WOULD, tty=True, answer="y")
        self.assertIn("--apply", calls[-1])

    def test_nothing_to_apply_never_asks(self) -> None:
        rc, out, calls = self.run_main(
            plan="self-update: REFUSED launcher change needs consent", plan_rc=3, tty=True,
            answer=None)
        self.assertEqual(3, rc)
        self.assertNotIn("--apply", sum(calls, []))
        self.assertIn("REFUSED", out)

    def test_other_options_pass_through(self) -> None:
        rc, out, calls = self.run_main("--verify")
        self.assertEqual([["--verify", "--home", str(self.home)]], calls)


if __name__ == "__main__":
    unittest.main()
