#!/usr/bin/env python3
"""Tests for the periodic, report-only build disagreement watch.

The recorded 2026-09-26 incident is replayed through the real detector
(`--from-jobs` / `--logs-dir`) so an alarm here is the detector's own verdict,
not a stub's.

Run:  python3 scripts/test_build_disagreement_watch.py
"""
from __future__ import annotations

import testing_support  # noqa: E402
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_disagreement_watch as bdw  # noqa: E402
import tartci_launchd_watchdog as wd  # noqa: E402

FIXTURES = HERE.parent / "tests" / "fixtures" / "build-disagreement"
INCIDENT = FIXTURES / "incident-2026-09-26.json"
CONTROL = FIXTURES / "control-2026-09-25.json"
LOGS = FIXTURES / "logs"
# Inside the incident: m3's streak is established and m1 builds green.
INCIDENT_NOW = "2026-09-26T14:00:00Z"
T0 = 2_000_000_000.0
ENABLED = 'schema = 1\nname = "t"\n\n[build_disagreement]\nenabled = true\n'


def replay(jobs: Path = INCIDENT, logs: Path = LOGS, now: str = INCIDENT_NOW) -> list[str]:
    return ["--from-jobs", str(jobs), "--logs-dir", str(logs), "--now", now]


class Recorder:
    """subprocess.run that records every argv it is asked to execute."""

    def __init__(self, fake: subprocess.CompletedProcess | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fake = fake

    def __call__(self, argv, **kwargs):
        self.calls.append([str(a) for a in argv])
        if self.fake is not None:
            return self.fake
        return subprocess.run(argv, **kwargs)


class WatchCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = self.root / "macos-fleet-profile.toml"
        self.state = self.root / "state" / "build-disagreement-watch.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cycle(self, now: float, runner=None, **kwargs):
        runner = runner or Recorder()
        report = bdw.cycle(self.profile, self.state, now=now, runner=runner,
                           python=sys.executable, **kwargs)
        return report, runner


@unittest.skipUnless(INCIDENT.exists(), "incident fixture absent")
class EnabledIncident(WatchCase):
    @testing_support.requires_tomllib
    def test_one_alarm_deduplicated_across_two_cycles(self):
        self.profile.write_text(ENABLED)
        first, r1 = self.cycle(T0, replay=replay())
        second, r2 = self.cycle(T0 + 16 * 60, replay=replay())
        self.assertEqual((first["state"], second["state"]), ("problem", "problem"))
        self.assertTrue(first["ran"] and second["ran"], "control: the detector ran both cycles")
        self.assertEqual(len(r1.calls) + len(r2.calls), 2)
        self.assertEqual(len(first["alarms"]), 1)
        [alarm] = first["alarms"]
        self.assertEqual(alarm["host"], "m3")
        self.assertIn("write_scenario_wav", alarm["fingerprint"])
        self.assertIn("tartci ccache reset", alarm["remedy"])
        self.assertEqual(second["alarms"], [], "the same (host, fingerprint) must not re-alarm")
        self.assertEqual([d["key"] for d in second["deduplicated"]], [alarm["key"]])
        lines = bdw.render(first, T0) + bdw.render(second, T0 + 16 * 60)
        self.assertEqual(sum(" ALARM " in line for line in lines), 1)
        self.assertEqual(sum("build-disagreement: ran " in line for line in lines), 2)
        self.assertTrue(any("tartci ccache reset" in line and "report only" in line for line in lines))
        state = json.loads(self.state.read_text())
        self.assertEqual(state["runs"], 2)

    @testing_support.requires_tomllib
    def test_a_cycle_inside_fifteen_minutes_does_not_run(self):
        self.profile.write_text(ENABLED + "interval_minutes = 1\n")  # floored at 15
        self.cycle(T0, replay=replay())
        report, runner = self.cycle(T0 + 14 * 60, replay=replay())
        self.assertEqual((report["state"], runner.calls), ("skipped", []))
        report, runner = self.cycle(T0 + 15 * 60, replay=replay())
        self.assertEqual(len(runner.calls), 1, "control: due again after 15 minutes")

    @testing_support.requires_tomllib
    def test_realerts_after_the_pair_has_been_absent(self):
        self.profile.write_text(ENABLED)
        self.cycle(T0, replay=replay())
        later, _ = self.cycle(T0 + 25 * 3600, replay=replay())
        self.assertEqual(len(later["alarms"]), 1)

    @testing_support.requires_tomllib
    @unittest.skipUnless(CONTROL.exists(), "control fixture absent")
    def test_clean_window_is_zero_alarms(self):
        self.profile.write_text(ENABLED)
        stamps = sorted({r["completed_at"] for r in json.loads(CONTROL.read_text())})
        report, runner = self.cycle(T0, replay=replay(CONTROL, now=stamps[-1]))
        self.assertEqual(len(runner.calls), 1, "control: the detector ran")
        self.assertGreater(report["jobs_considered"], 0, "control: the window held jobs")
        self.assertEqual(report["alarms"], [])

    @testing_support.requires_tomllib
    def test_never_invokes_a_reset_or_any_other_command(self):
        self.profile.write_text(ENABLED)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        marker = self.root / "invoked"
        for name in ("tartci", "ccache", "gh", "ghapp", "launchctl"):
            path = bin_dir / name
            path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{marker}"\nexit 0\n')
            path.chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
        runner = Recorder()
        report = bdw.cycle(self.profile, self.state, now=T0, runner=runner, env=env,
                           python=sys.executable, replay=replay())
        self.assertEqual(len(report["alarms"]), 1, "control: an alarm was raised")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(Path(runner.calls[0][1]).name, "build_disagreement.py")
        # Compare argv words (path arguments by basename), not a substring of
        # the whole command line: the checkout path may itself contain "ccache".
        words = {Path(arg).name if os.sep in arg else arg for arg in runner.calls[0]}
        self.assertTrue(words.isdisjoint({"reset", "ccache", "quarantine"}), sorted(words))
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")


@unittest.skipUnless(INCIDENT.exists(), "incident fixture absent")
class Disabled(WatchCase):
    def assert_no_run(self):
        report, runner = self.cycle(T0, replay=replay())
        self.assertEqual(report["state"], "disabled")
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.state.exists())
        self.assertEqual(bdw.render(report, T0), [])

    def test_missing_profile(self):
        self.assert_no_run()

    def test_profile_without_the_table(self):
        self.profile.write_text('schema = 1\nname = "t"\n')
        self.assert_no_run()

    def test_enabled_false(self):
        self.profile.write_text(ENABLED.replace("true", "false"))
        self.assert_no_run()

    def test_truthy_string_is_not_enabled(self):
        self.profile.write_text(ENABLED.replace("true", '"true"'))
        self.assert_no_run()

    @testing_support.requires_tomllib
    def test_control_enabled_runs(self):
        self.profile.write_text(ENABLED)
        report, runner = self.cycle(T0, replay=replay())
        self.assertEqual(len(runner.calls), 1)


@unittest.skipUnless(INCIDENT.exists(), "incident fixture absent")
class Unreadable(WatchCase):
    @testing_support.requires_tomllib
    def test_unreadable_logs_are_unknown_without_alarm(self):
        self.profile.write_text(ENABLED)
        empty = self.root / "no-logs"
        empty.mkdir()
        report, runner = self.cycle(T0, replay=replay(logs=empty))
        self.assertEqual(len(runner.calls), 1, "control: the detector ran")
        self.assertEqual(report["state"], "unknown")
        self.assertEqual(report["alarms"], [])
        self.assertFalse(any(" ALARM " in line for line in bdw.render(report, T0)))

    @testing_support.requires_tomllib
    def test_github_unreadable_exit_3_is_unknown_not_a_failure(self):
        self.profile.write_text(ENABLED)
        out = json.dumps({"state": "unknown", "code": "github_unreadable", "detail": "HTTP 502"})
        runner = Recorder(subprocess.CompletedProcess([], 3, out, ""))
        report, _ = self.cycle(T0, runner=runner, replay=replay())
        self.assertEqual((report["state"], report["code"], report["exit_code"]),
                         ("unknown", "github_unreadable", 3))
        self.assertEqual(report["alarms"], [])

    @testing_support.requires_tomllib
    def test_findings_from_an_abnormal_exit_never_alarm(self):
        self.profile.write_text(ENABLED)
        finding = {"state": "problem", "host": "m3", "rule": "streak",
                   "log": {"fingerprint": "fp"}, "remedy": "r"}
        out = json.dumps({"state": "problem", "findings": [finding]})
        report, _ = self.cycle(T0, runner=Recorder(subprocess.CompletedProcess([], 5, out, "")),
                               replay=replay())
        self.assertEqual((report["state"], report["alarms"]), ("unknown", []))
        control, _ = self.cycle(T0 + 3600, runner=Recorder(subprocess.CompletedProcess([], 1, out, "")),
                                replay=replay())
        self.assertEqual(len(control["alarms"]), 1, "control: the same JSON at exit 1 alarms")

    @testing_support.requires_tomllib
    def test_unparseable_and_timeout_are_unknown(self):
        self.profile.write_text(ENABLED)
        runner = Recorder(subprocess.CompletedProcess([], 2, "Traceback", "boom"))
        report, _ = self.cycle(T0, runner=runner, replay=replay())
        self.assertEqual(report["state"], "unknown")

        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

        report, _ = self.cycle(T0 + 3600, runner=slow, replay=replay())
        self.assertEqual((report["state"], report["code"]), ("unknown", "detector_timeout"))

    @testing_support.requires_tomllib
    def test_no_plain_gh_is_unknown_and_the_detector_is_not_run(self):
        self.profile.write_text(ENABLED)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        wrapper = bin_dir / "gh"
        wrapper.write_text("#!/bin/sh\necho 'ghapp: command is outside privileged grammar'\nexit 1\n")
        wrapper.chmod(0o755)
        runner = Recorder()
        with mock.patch.object(bdw, "PLAIN_GH_CANDIDATES", ()):
            report = bdw.cycle(self.profile, self.state, now=T0, runner=runner,
                               env={"PATH": str(bin_dir)}, python=sys.executable)
        self.assertEqual((report["state"], report["code"]), ("unknown", "log_cli_unavailable"))
        self.assertEqual([c for c in runner.calls if "build_disagreement.py" in " ".join(c)], [])
        self.assertIn("unknown", bdw.render(report, T0)[0])


class LogCli(WatchCase):
    def fake(self, name: str, output: str) -> str:
        path = self.root / name
        path.write_text(f"#!/bin/sh\necho '{output}'\n")
        path.chmod(0o755)
        return str(path)

    def test_plain_gh_on_path_is_found_and_wrappers_skipped(self):
        wrap_dir, plain_dir = self.root / "w", self.root / "p"
        wrap_dir.mkdir(), plain_dir.mkdir()
        for directory, output in ((wrap_dir, "ghapp: outside grammar"), (plain_dir, "gh version 2.101.0")):
            path = directory / "gh"
            path.write_text(f"#!/bin/sh\necho '{output}'\n")
            path.chmod(0o755)
        with mock.patch.object(bdw, "PLAIN_GH_CANDIDATES", ()):
            found, _ = bdw.resolve_log_cli({"PATH": f"{wrap_dir}{os.pathsep}{plain_dir}"}, subprocess.run)
        self.assertEqual(found, str(plain_dir / "gh"))

    def test_explicit_env_wins_and_is_verified(self):
        good = self.fake("mygh", "gh version 2.0.0")
        found, source = bdw.resolve_log_cli({"TARTCI_GH_LOG_CLI": good, "PATH": ""}, subprocess.run)
        self.assertEqual((found, source), (good, "TARTCI_GH_LOG_CLI"))
        bad = self.fake("notgh", "nope")
        found, _ = bdw.resolve_log_cli({"TARTCI_GH_LOG_CLI": bad, "PATH": ""}, subprocess.run)
        self.assertIsNone(found)


class Budgets(unittest.TestCase):
    def test_budgets_are_capped_at_the_detector_defaults(self):
        args = bdw.detector_args({"max_api_calls": 5000, "max_log_fetches": 99, "hours": 4})
        self.assertEqual(args[args.index("--max-api-calls") + 1], "200")
        self.assertEqual(args[args.index("--max-log-fetches") + 1], "12")
        self.assertEqual(args[args.index("--hours") + 1], "4")
        lower = bdw.detector_args({"max_api_calls": 50})
        self.assertEqual(lower[lower.index("--max-api-calls") + 1], "50")


class WatchdogWiring(unittest.TestCase):
    """The launchd watchdog runs the cycle in a heal pass and never in --status."""

    def run_main(self, argv: list[str], report: dict) -> tuple[int, str, mock.Mock]:
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "LaunchAgents"
            agents.mkdir()
            buf = io.StringIO()
            with mock.patch.object(wd, "build_disagreement_pass", return_value=report) as bd_pass, \
                 mock.patch.object(wd, "probe_tart_vm_running",
                                   return_value=wd.TartVMProbe(False, "idle", "/bin/true", td)), \
                 mock.patch.object(wd, "disabled_services", return_value=set()), \
                 mock.patch.object(wd, "refresh_skew"), \
                 mock.patch.object(wd, "config_verdicts", return_value={}), \
                 mock.patch.dict(os.environ, {"TARTCI_HOME": td}), \
                 redirect_stdout(buf):
                rc = wd.main([*argv, "--launch-agents-dir", str(agents),
                              "--participation-file", str(Path(td) / "participate"),
                              "--fleet-config", str(Path(td) / "profile.toml")])
        return rc, buf.getvalue(), bd_pass

    ALARM = {"state": "problem", "ran": True, "exit_code": 1, "jobs_considered": 3,
             "hosts_seen": ["m1", "m3"], "api_calls": 4, "log_fetches": 3,
             "deduplicated": [],
             "alarms": [{"key": "m3:x", "host": "m3", "rule": "streak", "commit": "abc",
                         "failing_url": "u1", "green_url": "u2", "fingerprint": "fp",
                         "remedy": "run `tartci ccache reset` on m3"}]}

    def test_heal_pass_runs_the_cycle_and_logs_the_alarm_with_exit_zero(self):
        rc, out, bd_pass = self.run_main([], self.ALARM)
        self.assertEqual(rc, 0, "a finding is not a watchdog failure")
        bd_pass.assert_called_once()
        self.assertIn("build-disagreement: ALARM host=m3", out)
        self.assertIn("tartci ccache reset", out)

    def test_status_and_dry_run_never_run_the_cycle(self):
        for flag in ("--status", "--dry-run"):
            _, _, bd_pass = self.run_main([flag], self.ALARM)
            bd_pass.assert_not_called()

    def test_disabled_prints_nothing(self):
        _, out, _ = self.run_main([], {"state": "disabled", "ran": False})
        self.assertNotIn("build-disagreement", out)

    def test_pass_without_an_installed_profile_is_disabled(self):
        self.assertEqual(wd.build_disagreement_pass("/nonexistent/profile.toml")["state"], "disabled")


if __name__ == "__main__":
    unittest.main()
