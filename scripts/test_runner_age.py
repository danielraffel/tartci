#!/usr/bin/env python3
"""Hermetic tests for the golden runner age check. No network, no VMs."""

from __future__ import annotations

import testing_support  # noqa: E402,F401
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import runner_age as ra  # noqa: E402
import runner_version as rv  # noqa: E402
import tool_freshness as tf  # noqa: E402

NOW = 1790000000.0
REFUSAL = ("√ Connected to GitHub\n"
           "An error occurred: Runner version v2.336.0 is deprecated and cannot receive messages.\n")


class PolicyTests(unittest.TestCase):
    def test_current_version_is_ok(self):
        self.assertEqual(ra.classify("2.338.2", "2.338.2")["verdict"], "ok")

    def test_one_minor_behind_is_ok(self):
        self.assertEqual(ra.classify("2.337.0", "2.338.2")["verdict"], "ok")

    def test_two_minor_behind_is_warn(self):
        row = ra.classify("2.336.0", "2.338.2")
        self.assertEqual((row["verdict"], row["installed"], row["latest"]),
                         ("warn", "2.336.0", "2.338.2"))

    def test_refusal_of_the_installed_version_is_fail(self):
        row = ra.classify("2.336.0", "2.338.2", refused={"2.336.0"})
        self.assertEqual(row["verdict"], "fail")
        self.assertIn("refuses runner v2.336.0", row["reason"])

    def test_refusal_fails_even_when_the_version_is_unrecorded(self):
        self.assertEqual(ra.classify(None, None, refused={"2.336.0"})["verdict"], "fail")

    def test_installed_refusal_wins_among_several(self):
        row = ra.classify("2.336.0", "2.338.2", refused={"2.330.0", "2.336.0"})
        self.assertIn("v2.336.0", row["reason"])
        row = ra.classify(None, None, refused={"2.330.0", "2.336.0"})
        self.assertIn("v2.336.0", row["reason"])

    def test_any_live_refusal_fails_and_names_the_installed_version_first(self):
        row = ra.classify("2.338.2", "2.338.2", refused={"2.336.0"})
        self.assertEqual(row["verdict"], "fail")
        self.assertIn("v2.336.0", row["reason"])

    def test_unrecorded_version_is_unknown(self):
        self.assertEqual(ra.classify(None, "2.338.2")["verdict"], "unknown")

    def test_unreadable_latest_is_unknown(self):
        self.assertEqual(ra.classify("2.336.0", None)["verdict"], "unknown")

    def test_lag_is_shown_but_is_not_a_problem(self):
        row = dict(ra.classify("2.330.0", "2.338.2"), golden="g")
        self.assertEqual(row["verdict"], "warn")
        self.assertEqual(ra.problems([row]), [])

    def test_refusal_problem_text_names_only_the_refused_version(self):
        before = ra.problems([dict(ra.classify("2.336.0", "2.338.2", {"2.336.0"}), golden="g")])
        after = ra.problems([dict(ra.classify("2.336.0", "2.341.0", {"2.336.0"}), golden="g")])
        self.assertEqual(before, after)
        self.assertEqual(before, ["g: FAIL: GitHub refuses runner v2.336.0 as deprecated"])

    def test_agrees_with_the_pin_rule(self):
        names = {"problem": "fail", "warn": "warn", "ok": "ok", "unknown": "unknown"}
        for installed, latest in (("2.338.2", "2.338.2"), ("2.337.0", "2.338.2"),
                                  ("2.336.0", "2.338.2"), ("2.330.0", "2.338.2"),
                                  ("2.336.0", None), ("2.336.0", "3.0.0")):
            with self.subTest(installed=installed, latest=latest):
                self.assertEqual(ra.classify(installed, latest)["verdict"],
                                 names[rv.evaluate(installed, latest, [])[0]])
        hit = [{"version": "2.336.0", "log": "x"}]
        self.assertEqual(ra.classify("2.336.0", "2.338.2", refused={"2.336.0"})["verdict"],
                         names[rv.evaluate("2.336.0", "2.338.2", hit)[0]])


class ProblemTests(unittest.TestCase):
    def test_healthy_host_has_no_problem(self):
        rows = ra.collect(("pulp-build-runner", "pulp-linux-build"), NOW, latest="2.338.2",
                          present=lambda: {"pulp-build-runner", "pulp-linux-build"},
                          read=lambda g: "2.338.2",
                          pin=lambda: "2.338.2", refusal=lambda g, v: set())
        self.assertEqual([r["verdict"] for r in rows], ["ok", "ok"])
        self.assertEqual(ra.problems(rows), [])

    def test_unknown_is_not_a_problem(self):
        rows = ra.collect(("pulp-build-runner", "pulp-linux-build"), NOW, latest=None,
                          present=lambda: {"pulp-build-runner", "pulp-linux-build"},
                          read=lambda g: None, pin=lambda: None, refusal=lambda g, v: set())
        self.assertEqual([r["verdict"] for r in rows], ["unknown", "unknown"])
        self.assertEqual(ra.problems(rows), [])

    def test_only_the_refusal_is_a_problem(self):
        rows = [dict(ra.classify("2.336.0", "2.338.2"), golden="a"),
                dict(ra.classify(None, None, refused={"2.336.0"}), golden="b")]
        self.assertEqual(ra.problems(rows),
                         ["b: FAIL: GitHub refuses runner v2.336.0 as deprecated"])


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = {"TARTCI_HOME": str(self.home / ".tartci"),
               "TARTCI_LINUX_LOGS": str(self.home / "linux-logs"),
               "TARTCI_STATE_DIR": str(self.home / "macos-state")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_log(self, path: Path, text: str, age: float = 60.0) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.utime(path, (NOW - age, NOW - age))

    def test_reads_the_record_and_skips_absent_goldens(self):
        ra.record("pulp-linux-build:latest", "2.336.0", now=NOW)
        rows = ra.collect(("pulp-build-runner", "pulp-linux-build"), NOW, latest="2.338.2",
                          present=lambda: {"pulp-linux-build"})
        self.assertEqual([(r["golden"], r["verdict"], r["source"]) for r in rows],
                         [("pulp-linux-build", "warn", "record")])

    def test_no_record_is_unknown(self):
        rows = ra.collect(("pulp-linux-build",), NOW, latest="2.338.2",
                          present=lambda: {"pulp-linux-build"})
        self.assertEqual(rows[0]["verdict"], "unknown")

    def test_linux_refusal_log_line_is_fail(self):
        self.write_log(self.home / "linux-logs" / "linux-ephr-1-1" / "runner-output.log", REFUSAL)
        rows = ra.collect(("pulp-linux-build",), NOW, latest="2.338.2",
                          present=lambda: {"pulp-linux-build"})
        self.assertEqual(rows[0]["verdict"], "fail")
        self.assertEqual(ra.problems(rows),
                         ["pulp-linux-build: FAIL: GitHub refuses runner v2.336.0 as deprecated"])

    def test_macos_refusal_log_line_is_fail(self):
        self.write_log(self.home / "macos-state" / "vm-1.actions-runner.log", REFUSAL)
        rows = ra.collect(("pulp-build-runner",), NOW, latest="2.338.2",
                          present=lambda: {"pulp-build-runner"}, pin=lambda: "2.336.0")
        self.assertEqual(rows[0]["verdict"], "fail")

    def test_refusal_outlives_its_log(self):
        # The watchdog pass sees the refusal and remembers it. A day later the
        # log is old and the lane has not run, and the host is still broken.
        log = self.home / "linux-logs" / "linux-ephr-1-1" / "runner-output.log"
        self.write_log(log, REFUSAL)
        kw = dict(latest="2.338.2", present=lambda: {"pulp-linux-build"})
        self.assertEqual(ra.collect(("pulp-linux-build",), NOW, persist=True, **kw)[0]["verdict"],
                         "fail")
        later = NOW + 3 * ra.LOG_SCAN_WINDOW_SECS
        self.assertEqual(ra.collect(("pulp-linux-build",), later, **kw)[0]["verdict"], "fail")
        log.unlink()
        self.assertEqual(ra.collect(("pulp-linux-build",), later, **kw)[0]["verdict"], "fail")

    def test_doctor_read_does_not_remember(self):
        self.write_log(self.home / "linux-logs" / "linux-ephr-1-1" / "runner-output.log", REFUSAL)
        ra.collect(("pulp-linux-build",), NOW, latest="2.338.2",
                   present=lambda: {"pulp-linux-build"})
        self.assertEqual(ra.read_refused("pulp-linux-build"), {})

    def test_old_log_alone_is_not_opened(self):
        self.write_log(self.home / "linux-logs" / "linux-ephr-1-1" / "runner-output.log",
                       REFUSAL, age=ra.LOG_SCAN_WINDOW_SECS + 60)
        rows = ra.collect(("pulp-linux-build",), NOW, latest="2.338.2",
                          present=lambda: {"pulp-linux-build"})
        self.assertEqual(rows[0]["verdict"], "unknown")

    def test_refusal_clears_once_a_newer_version_is_recorded(self):
        self.write_log(self.home / "linux-logs" / "linux-ephr-1-1" / "runner-output.log", REFUSAL)
        kw = dict(latest="2.338.2", present=lambda: {"pulp-linux-build"}, persist=True)
        self.assertEqual(ra.collect(("pulp-linux-build",), NOW, **kw)[0]["verdict"], "fail")
        ra.record("pulp-linux-build", "2.338.2", now=NOW, source="operator")
        self.assertEqual(ra.collect(("pulp-linux-build",), NOW, **kw)[0]["verdict"], "ok")
        self.assertEqual(ra.read_refused("pulp-linux-build"), {})

    def test_macos_golden_is_judged_by_the_pin_not_a_record(self):
        ra.record("pulp-build-runner", "2.336.0", now=NOW)
        rows = ra.collect(("pulp-build-runner",), NOW, latest="2.338.2",
                          present=lambda: {"pulp-build-runner"}, pin=lambda: "2.338.2")
        self.assertEqual((rows[0]["verdict"], rows[0]["installed"], rows[0]["source"]),
                         ("ok", "2.338.2", "pin"))

    def test_macos_pin_is_read_from_the_supervisor(self):
        self.assertRegex(ra.macos_pin() or "", r"^\d+\.\d+\.\d+$")
        self.assertIsNone(ra.macos_pin(Path("/nonexistent/runner.sh")))

    def test_unlistable_images_judge_nothing(self):
        # Without the image list this host may not hold the golden at all, so a
        # stale pin or record must not alert for it.
        rows = ra.collect(("pulp-build-runner", "pulp-linux-build"), NOW, latest="2.338.2",
                          present=lambda: None, read=lambda g: "2.330.0",
                          pin=lambda: "2.330.0", refusal=lambda g, v: {"2.330.0"})
        self.assertEqual([r["verdict"] for r in rows], ["unknown", "unknown"])
        self.assertEqual(ra.problems(rows), [])

    def test_macos_refusal_is_found_in_a_fleet_lane_state_dir(self):
        # Fleet lanes keep their runner logs under state/macos-fleet/<lane>/.
        with mock.patch.dict(os.environ):
            os.environ.pop("TARTCI_STATE_DIR")
            self.write_log(self.home / ".tartci" / "state" / "macos-fleet" / "pulp-gate"
                           / "vm-1.actions-runner.log", REFUSAL)
            rows = ra.collect(("pulp-build-runner",), NOW, latest="2.338.2",
                              present=lambda: {"pulp-build-runner"}, pin=lambda: "2.336.0")
        self.assertEqual(rows[0]["verdict"], "fail")

    def test_macos_refusal_of_an_older_pin_does_not_apply(self):
        self.write_log(self.home / "macos-state" / "vm-1.actions-runner.log", REFUSAL)
        rows = ra.collect(("pulp-build-runner",), NOW, latest="2.338.2",
                          present=lambda: {"pulp-build-runner"}, pin=lambda: "2.338.2")
        self.assertEqual(rows[0]["verdict"], "ok")

    def test_refusal_logged_after_the_record_fails_whatever_it_names(self):
        # The record says 2.338.2 but the lane is still being refused as
        # 2.336.0: the record is wrong about what the golden carries.
        record = ra.record("pulp-linux-build", "2.338.2", source="operator")
        after = os.path.getmtime(record) + 600
        log = self.home / "linux-logs" / "linux-ephr-1-2" / "runner-output.log"
        self.write_log(log, REFUSAL)
        os.utime(log, (after, after))
        rows = ra.collect(("pulp-linux-build",), after + 60, latest="2.338.2",
                          present=lambda: {"pulp-linux-build"})
        self.assertEqual(rows[0]["verdict"], "fail")

    def test_remembered_refusal_is_reported_when_images_cannot_be_listed(self):
        self.write_log(self.home / "linux-logs" / "linux-ephr-1-1" / "runner-output.log", REFUSAL)
        ra.collect(("pulp-linux-build",), NOW, latest="2.338.2", persist=True,
                   present=lambda: {"pulp-linux-build"})
        rows = ra.collect(("pulp-linux-build",), NOW, latest="2.338.2", present=lambda: None)
        self.assertEqual(rows[0]["verdict"], "fail")

    def test_refusal_past_the_log_head_is_not_read(self):
        self.write_log(self.home / "linux-logs" / "linux-ephr-1-1" / "runner-output.log",
                       "x" * (ra.LOG_HEAD_BYTES + 10) + REFUSAL)
        self.assertEqual(ra.scan_refusals("pulp-linux-build", NOW), {})

    def test_present_goldens_parses_names_and_tags(self):
        done = mock.Mock(returncode=0, stdout="pulp-linux-build:latest\nm5-gate-01\n\n")
        with mock.patch.object(ra.subprocess, "run", return_value=done):
            self.assertEqual(ra.present_goldens(), {"pulp-linux-build", "m5-gate-01"})
        with mock.patch.object(ra.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertIsNone(ra.present_goldens())
        with mock.patch.object(ra.subprocess, "run", side_effect=OSError("no tart")):
            self.assertIsNone(ra.present_goldens())

    def test_record_with_a_non_string_version_is_unknown(self):
        path = ra.record("pulp-linux-build", "2.338.2", now=NOW)
        path.write_text(json.dumps({"version": 2338}))
        self.assertIsNone(ra.read_record("pulp-linux-build"))

    def test_render_tolerates_a_truncated_row(self):
        self.assertEqual(ra.render({"golden": "x"}), "x: UNKNOWN: ")

    def test_record_refuses_garbage_and_accepts_listener_output(self):
        with self.assertRaises(ValueError):
            ra.record("pulp-linux-build", "not-a-version")
        ra.record("pulp-linux-build", "2.338.2\n", now=NOW)
        self.assertEqual(ra.read_record("pulp-linux-build"), "2.338.2")


class SurfaceTests(unittest.TestCase):
    """The doctor CLI (human and --json) and the alert path through tool freshness."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = {"TARTCI_HOME": str(self.home / ".tartci"),
               "TARTCI_LINUX_LOGS": str(self.home / "linux-logs"),
               "TARTCI_STATE_DIR": str(self.home / "macos-state")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args, refused=None):
        out = []
        with mock.patch.object(ra, "latest_release", lambda home=None: "2.338.2"), \
                mock.patch.object(ra, "present_goldens", lambda: {"pulp-linux-build"}), \
                mock.patch.object(ra, "scan_refusals",
                                  lambda g, now, home=None: {refused: now} if refused else {}), \
                mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            code = ra.main(["check", *args])
        return code, "\n".join(out)

    def test_json_names_the_refused_golden_and_exits_1(self):
        code, text = self.run_cli("--json", "--golden", "pulp-linux-build", refused="2.336.0")
        row = json.loads(text)["goldens"][0]
        self.assertEqual((code, row["golden"], row["verdict"]), (1, "pulp-linux-build", "fail"))

    def test_behind_is_shown_and_exits_0(self):
        ra.record("pulp-linux-build", "2.336.0", now=NOW)
        code, text = self.run_cli("--golden", "pulp-linux-build")
        self.assertEqual(code, 0)
        self.assertIn("pulp-linux-build: WARN: runner v2.336.0 is 2 minor versions behind", text)

    def test_unknown_is_shown_and_exits_0(self):
        code, text = self.run_cli("--golden", "pulp-linux-build")
        self.assertEqual(code, 0)
        self.assertIn("pulp-linux-build: UNKNOWN: runner version is not recorded", text)

    def summary_after_refresh(self, latest, refused=None, recorded=None):
        if recorded:
            ra.record("pulp-linux-build", recorded, now=NOW)
        settings = {"stale_hours": 12.0, "tools": {},
                    "runner_age": {"goldens": ["pulp-linux-build"]}}
        with mock.patch.object(ra, "present_goldens", lambda: {"pulp-linux-build"}), \
                mock.patch.object(ra, "latest_release", lambda home=None: latest), \
                mock.patch.object(ra, "scan_refusals",
                                  lambda g, now, home=None: {refused: now} if refused else {}):
            tf.refresh(self.home, NOW, settings=settings, feed=lambda repo: "")
        import state_age
        with mock.patch.object(state_age, "clock", lambda: NOW + 60):
            return tf.summary(self.home)

    def test_tool_freshness_healthy_host_has_no_problem(self):
        summary = self.summary_after_refresh("2.338.2", recorded="2.338.2")
        self.assertIsNone(summary["problem"])
        self.assertIn("runner age pulp-linux-build: OK", "\n".join(summary["lines"]))

    def test_tool_freshness_unknown_is_shown_but_not_a_problem(self):
        summary = self.summary_after_refresh(None)
        self.assertIsNone(summary["problem"])
        self.assertIn("runner age pulp-linux-build: UNKNOWN", "\n".join(summary["lines"]))

    def test_tool_freshness_refusal_is_a_problem(self):
        summary = self.summary_after_refresh("2.338.2", refused="2.336.0")
        self.assertIn("runner age pulp-linux-build: FAIL: GitHub refuses runner v2.336.0",
                      summary["problem"])

    def test_tool_freshness_behind_is_shown_but_not_a_problem(self):
        summary = self.summary_after_refresh("2.338.2", recorded="2.336.0")
        self.assertIsNone(summary["problem"])
        self.assertIn("runner age pulp-linux-build: WARN", "\n".join(summary["lines"]))

    def test_measure_failure_is_unknown_not_a_problem(self):
        with mock.patch.object(ra, "collect", side_effect=OSError("boom")):
            rows = tf.measure_runner_age({"goldens": ["x"]}, self.home, NOW)
        self.assertEqual(rows[0]["verdict"], "unknown")
        self.assertEqual(ra.problems(rows), [])

    def test_the_documented_remedy_commands_run(self):
        # Run the record command exactly as fleet_reasons.json prints it, from
        # the checkout root, and check it clears a remembered refusal.
        remedy = json.loads((HERE / "fleet_reasons.json").read_text())[
            "reasons"]["tool_freshness_stale"]["remedy"]
        match = re.search(r"`(python3 scripts/runner_age\.py record [^`]+)`", remedy)
        self.assertIsNotNone(match, remedy)
        command = match.group(1).replace("<name>", "pulp-linux-build").replace(
            "<version>", "2.338.2")
        self.assertNotIn("<", command)
        ra.remember_refused("pulp-linux-build", {"2.336.0": NOW - 3600})
        done = subprocess.run(command, shell=True, cwd=str(HERE.parent),
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(ra.read_record("pulp-linux-build"), "2.338.2")
        rows = ra.collect(("pulp-linux-build",), NOW, latest="2.338.2", persist=True,
                          present=lambda: {"pulp-linux-build"})
        self.assertEqual(rows[0]["verdict"], "ok")
        self.assertIn("`tartci fleet-macos tool-freshness --refresh`", remedy)
        dispatcher = (HERE.parent / "tartci").read_text()
        self.assertIn("tool-freshness", dispatcher)
        self.assertIn("DEFAULT_RUNNER_VERSION=",
                      (HERE.parent / "providers/tart-macos/runner.sh").read_text())
        self.assertIn("DEFAULT_RUNNER_VERSION", remedy)


class SupervisorTests(unittest.TestCase):
    def test_linux_runner_names_the_deprecated_runner_failure(self):
        text = (HERE.parent / "providers/tart-linux/runner.sh").read_text()
        self.assertIn("is deprecated and cannot receive messages", text)
        self.assertIn("runner_version_deprecated:", text)

    def refusal_block(self) -> str:
        """The supervisor's refusal check, cut out of runner.sh to be run."""
        text = (HERE.parent / "providers/tart-linux/runner.sh").read_text()
        start = text.index("  # Checked whatever the exit status")
        end = text.index("\n  fi\n", start) + len("\n  fi\n")
        return text[start:end]

    def run_block(self, log_text: str, run_status: int) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "runner-output.log").write_text(log_text)
            script = ('set -euo pipefail\nnote(){ printf "%s\\n" "$*"; }\n'
                      f'i=7; logdir="{tmp}"; run_status={run_status}\n' + self.refusal_block())
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_linux_runner_names_the_refusal_when_the_runner_exits_zero(self):
        # The runner's wrapper can exit 0 after the listener reports retirement.
        for status in (0, 1):
            with self.subTest(run_status=status):
                self.assertIn("[7] runner_version_deprecated:", self.run_block(REFUSAL, status))

    def test_linux_runner_says_nothing_for_an_ordinary_log(self):
        self.assertEqual(self.run_block("Listening for Jobs\n", 1), "")

    def test_linux_refusal_check_does_not_depend_on_the_exit_status(self):
        self.assertNotIn("run_status", self.refusal_block())

    def test_supervisor_wording_matches_the_log_pattern(self):
        self.assertEqual(rv.find_deprecated(REFUSAL), "2.336.0")


if __name__ == "__main__":
    unittest.main()
