#!/usr/bin/env python3
"""Behavioral tests for the cross-host build disagreement alarm.

Every rule is exercised on both sides: the incident shape must fire, and each
look-alike that is NOT host poisoning (a flaky single red, reds on different
commits, a host that is the only one building, a starved host, an unreadable
log) must stay quiet or report unknown -- never problem.

Run:  python3 scripts/test_build_disagreement.py
"""
from __future__ import annotations

import testing_support  # noqa: E402
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_disagreement as bd  # noqa: E402

FIXTURES = HERE.parent / "tests" / "fixtures" / "build-disagreement"

LINK_LOG = """\
2026-09-26T08:39:14.4270620Z ld: warning: ignoring duplicate libraries: 'core/timeline/libpulp-timeline.a'
2026-09-26T08:39:15.4283660Z Undefined symbols for architecture arm64:
2026-09-26T08:39:15.4284000Z   "pulp::test::write_scenario_wav(std::__1::basic_string<char> const&, Result const&)", referenced from:
2026-09-26T08:39:15.4285460Z ld: symbol(s) not found for architecture arm64
2026-09-26T08:39:15.4285460Z clang++: error: linker command failed with exit code 1 (use -v to see invocation)
"""
OTHER_LINK_LOG = LINK_LOG.replace("write_scenario_wav", "render_offline_block")
GREEN_LOG = "ld: warning: ignoring duplicate libraries: 'core/audio/libpulp-audio.a'\n"
RESOURCE_LOG = LINK_LOG + "clang++: error: unable to execute command: Killed: 9\n"
TEST_ONLY_LOG = "The following tests FAILED:\n  12 - pulp-test-state (Failed)\n"

T0 = dt.datetime(2026, 9, 26, 8, 0, tzinfo=dt.timezone.utc)


def rec(job_id: int, host: str, outcome: str, sha: str, minute: int, *,
        event: str = "merge_group", base: str = "", tree: str = "") -> dict:
    run = {"id": job_id * 10, "event": event, "head_sha": sha,
           "head_commit": {"tree_id": tree} if tree else {},
           "pull_requests": [{"base": {"sha": base}}] if base else []}
    done = T0 + dt.timedelta(minutes=minute)
    return {
        "job_id": job_id, "run_id": job_id * 10, "event": event, "head_sha": sha,
        "head_branch": f"b-{sha}", "identities": bd.build_identities(run),
        "runner_name": {"m3": "studio-pulp-gate-01", "m5": "m5-pulp-gate-01",
                        "m1": "m1-pulp-gate-01"}[host] + f"-{job_id}",
        "host": host, "outcome": outcome,
        "started_at": bd.iso(done - dt.timedelta(minutes=40)),
        "completed_at": bd.iso(done),
        "html_url": f"https://example.invalid/job/{job_id}",
    }


def evaluate(records, logs=None, *, minute=600, hours=12, streak=3):
    logs = logs or {}
    ev = bd.Evaluation(records=records, now=T0 + dt.timedelta(minutes=minute),
                       window=dt.timedelta(hours=hours), streak=streak,
                       log_for=lambda r: logs.get(r["job_id"]))
    return bd.evaluate(ev)


class ClassifyLog(unittest.TestCase):
    def test_link_failure_is_compile_link_with_symbol_fingerprint(self):
        verdict = bd.classify_log(LINK_LOG)
        self.assertEqual(verdict.state, "compile_link")
        self.assertIn("undefined_symbols", verdict.signatures)
        self.assertIn("write_scenario_wav", verdict.fingerprint)

    def test_duplicate_library_warning_alone_is_not_a_failure(self):
        self.assertEqual(bd.classify_log(GREEN_LOG).state, "other")

    def test_killed_compiler_is_resource_not_poisoning(self):
        self.assertEqual(bd.classify_log(RESOURCE_LOG).state, "resource")

    def test_unreadable_log_is_unknown(self):
        self.assertEqual(bd.classify_log(None).state, "unknown")

    def test_fingerprint_ignores_paths_and_line_numbers(self):
        a = bd.classify_log("/Users/a/w1/core/x.cpp:12:3: error: no member named 'y'\n")
        b = bd.classify_log("/Users/b/w2/core/x.cpp:99:7: error: no member named 'y'\n")
        self.assertEqual(a.state, "compile_link")
        self.assertEqual(a.fingerprint, b.fingerprint)


class SameBuildRule(unittest.TestCase):
    def test_fires_on_the_incident_shape(self):
        records = [rec(1, "m5", "green", "aaa", 100), rec(2, "m3", "build_failure", "aaa", 110)]
        result = evaluate(records, {2: LINK_LOG})
        self.assertEqual(result["state"], "problem")
        [finding] = result["findings"]
        self.assertEqual((finding["rule"], finding["host"], finding["commit"]), ("same_build", "m3", "aaa"))
        self.assertEqual(finding["green_counterpart"]["job_id"], 1)
        self.assertEqual(finding["failing_step"], "Build")
        self.assertIn("ccache", finding["remedy"])

    def test_matches_by_tree_across_different_head_shas(self):
        records = [rec(1, "m5", "green", "aaa", 100, event="push", tree="t1"),
                   rec(2, "m3", "build_failure", "bbb", 110, tree="t1")]
        self.assertEqual(evaluate(records, {2: LINK_LOG}, streak=9)["state"], "problem")

    def test_a_rerun_attempt_on_another_host_is_the_same_build(self):
        a = rec(1, "m5", "green", "aaa", 100, event="pull_request")
        b = rec(2, "m3", "build_failure", "aaa", 110, event="pull_request")
        a["identities"] = b["identities"] = ["run:77"]
        self.assertEqual(evaluate([a, b], {2: LINK_LOG})["state"], "problem")

    def test_quiet_when_failures_differ_in_sha(self):
        records = [rec(1, "m5", "green", "aaa", 100), rec(2, "m3", "build_failure", "bbb", 110)]
        self.assertEqual(evaluate(records, {2: LINK_LOG})["state"], "ok")

    def test_pull_request_needs_the_same_base_to_compare(self):
        records = [rec(1, "m5", "green", "aaa", 100, event="pull_request", base="base1"),
                   rec(2, "m3", "build_failure", "aaa", 110, event="pull_request", base="base2")]
        self.assertEqual(evaluate(records, {2: LINK_LOG})["state"], "ok")
        records[1] = rec(2, "m3", "build_failure", "aaa", 110, event="pull_request", base="base1")
        self.assertEqual(evaluate(records, {2: LINK_LOG})["state"], "problem")

    def test_quiet_when_the_failing_host_also_built_it_green(self):
        records = [rec(1, "m5", "green", "aaa", 100), rec(2, "m3", "build_failure", "aaa", 110),
                   rec(3, "m3", "green", "aaa", 150)]
        self.assertEqual(evaluate(records, {2: LINK_LOG})["state"], "ok")

    def test_quiet_when_the_failure_is_a_starved_host(self):
        records = [rec(1, "m5", "green", "aaa", 100), rec(2, "m3", "build_failure", "aaa", 110)]
        self.assertEqual(evaluate(records, {2: RESOURCE_LOG})["state"], "ok")

    def test_unreadable_log_reports_unknown_never_problem(self):
        records = [rec(1, "m5", "green", "aaa", 100), rec(2, "m3", "build_failure", "aaa", 110)]
        result = evaluate(records, {})
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["findings"][0]["code"], "disagreement_log_unreadable")

    def test_outside_the_window_is_not_evidence(self):
        records = [rec(1, "m5", "green", "aaa", 100), rec(2, "m3", "build_failure", "aaa", 110)]
        self.assertEqual(evaluate(records, {2: LINK_LOG}, minute=600, hours=1)["state"], "ok")


class StreakRule(unittest.TestCase):
    def test_fires_on_a_same_error_streak_while_another_host_is_green(self):
        records = [rec(1, "m3", "build_failure", "a1", 100), rec(2, "m3", "build_failure", "a2", 150),
                   rec(3, "m3", "build_failure", "a3", 200), rec(4, "m5", "green", "b1", 180)]
        result = evaluate(records, {1: LINK_LOG, 2: LINK_LOG, 3: LINK_LOG})
        self.assertEqual(result["state"], "problem")
        [finding] = result["findings"]
        self.assertEqual((finding["rule"], finding["host"], finding["streak"]), ("streak", "m3", 3))

    def test_quiet_on_a_flaky_single_failure(self):
        records = [rec(1, "m3", "green", "a1", 100), rec(2, "m3", "build_failure", "a2", 150),
                   rec(3, "m5", "green", "b1", 160)]
        self.assertEqual(evaluate(records, {2: LINK_LOG})["state"], "ok")

    def test_quiet_when_only_one_host_built(self):
        records = [rec(i, "m3", "build_failure", f"a{i}", 100 + 30 * i) for i in range(1, 5)]
        self.assertEqual(evaluate(records, {i: LINK_LOG for i in range(1, 5)})["state"], "ok")

    def test_quiet_when_streak_errors_differ(self):
        records = [rec(1, "m3", "build_failure", "a1", 100), rec(2, "m3", "build_failure", "a2", 150),
                   rec(3, "m3", "build_failure", "a3", 200), rec(4, "m5", "green", "b1", 180)]
        logs = {1: LINK_LOG, 2: OTHER_LINK_LOG, 3: LINK_LOG}
        self.assertEqual(evaluate(records, logs)["state"], "ok")

    def test_quiet_when_the_streak_is_one_commit_retried(self):
        records = [rec(1, "m3", "build_failure", "a1", 100), rec(2, "m3", "build_failure", "a1", 150),
                   rec(3, "m3", "build_failure", "a1", 200), rec(4, "m5", "green", "b1", 180)]
        self.assertEqual(evaluate(records, {1: LINK_LOG, 2: LINK_LOG, 3: LINK_LOG})["state"], "ok")

    def test_quiet_when_another_host_fails_with_the_same_error(self):
        # 2026-09-26 16:44-17:35: one broken PR failed identically on m5 and m3.
        records = [rec(1, "m5", "build_failure", "a1", 100), rec(2, "m5", "build_failure", "a2", 150),
                   rec(3, "m5", "build_failure", "a3", 200), rec(4, "m3", "green", "b1", 180),
                   rec(5, "m3", "build_failure", "b2", 190)]
        logs = {i: LINK_LOG for i in (1, 2, 3, 5)}
        self.assertEqual(evaluate(records, logs)["state"], "ok")
        logs[5] = OTHER_LINK_LOG
        self.assertEqual(evaluate(records, logs)["state"], "problem")

    def test_a_retried_commit_inside_the_streak_does_not_reset_it(self):
        records = [rec(1, "m3", "build_failure", "a1", 100), rec(2, "m3", "build_failure", "a2", 150),
                   rec(3, "m3", "build_failure", "a2", 170), rec(4, "m3", "build_failure", "a3", 200),
                   rec(9, "m5", "green", "b1", 180)]
        self.assertEqual(evaluate(records, {i: LINK_LOG for i in (1, 2, 3, 4)})["state"], "problem")

    def test_green_on_the_other_host_must_overlap_the_streak(self):
        records = [rec(4, "m5", "green", "b1", 10), rec(1, "m3", "build_failure", "a1", 100),
                   rec(2, "m3", "build_failure", "a2", 150), rec(3, "m3", "build_failure", "a3", 200)]
        self.assertEqual(evaluate(records, {1: LINK_LOG, 2: LINK_LOG, 3: LINK_LOG})["state"], "ok")

    def test_log_fetches_are_bounded(self):
        records = [rec(i, "m3", "build_failure", f"a{i}", 100 + i) for i in range(1, 30)]
        records += [rec(100 + i, "m5", "green", f"a{i}", 100 + i) for i in range(1, 30)]
        ev = bd.Evaluation(records=records, now=T0 + dt.timedelta(minutes=600),
                           window=dt.timedelta(hours=12), log_for=lambda r: LINK_LOG,
                           max_log_fetches=5)
        bd.evaluate(ev)
        self.assertEqual(ev.log_fetches, 5)


class Normalize(unittest.TestCase):
    def test_maps_runner_to_host_and_reads_the_build_step(self):
        run = {"id": 9, "event": "merge_group", "head_sha": "abc", "head_commit": {"tree_id": "t"}}
        job = {"id": 1, "name": "macos", "status": "completed", "runner_name": "studio-pulp-gate-01-1-2",
               "steps": [{"name": "Configure", "conclusion": "success"},
                         {"name": "Build", "conclusion": "failure"}]}
        record = bd.normalize(run, job)
        self.assertEqual((record["host"], record["outcome"]), ("m3", "build_failure"))
        self.assertEqual(record["identities"], ["run:9", "sha:abc", "tree:t"])

    def test_hosted_and_foreign_jobs_are_out_of_scope(self):
        run = {"id": 9, "event": "push", "head_sha": "abc"}
        self.assertIsNone(bd.normalize(run, {"name": "macos", "status": "completed",
                                             "runner_name": "GitHub Actions 1000"}))
        self.assertIsNone(bd.normalize(run, {"name": "Linux (x64) [github-hosted]",
                                             "status": "completed", "runner_name": "m5-x"}))

    def test_failure_after_the_build_step_counts_as_a_green_build(self):
        run = {"id": 9, "event": "push", "head_sha": "abc"}
        job = {"id": 1, "name": "macos", "status": "completed", "runner_name": "m5-pulp-gate-01-3",
               "conclusion": "failure", "steps": [{"name": "Build", "conclusion": "success"},
                                                  {"name": "Test (non-Windows)", "conclusion": "failure"}]}
        self.assertEqual(bd.normalize(run, job)["outcome"], "green")


class FakeGitHub(bd.GitHub):
    def __init__(self, pages):
        super().__init__("gh", "gh", 5, 50)
        self.pages = pages
        self.paths = []

    def api(self, path):
        self.calls += 1
        self.paths.append(path)
        return self.pages(path)


class Fetch(unittest.TestCase):
    def test_lists_runs_through_the_workflow_endpoint_and_paginates(self):
        runs1 = [{"id": i, "status": "completed", "event": "push", "head_sha": f"s{i}"} for i in range(100)]
        runs2 = [{"id": 100, "status": "in_progress", "event": "push", "head_sha": "s100"}]

        def pages(path):
            if "/workflows/build.yml/runs" in path:
                return {"total_count": 101, "workflow_runs": runs1 if "page=1&" in path else runs2}
            return {"jobs": [{"id": 1, "name": "macos", "status": "completed",
                              "runner_name": "m5-pulp-gate-01-1",
                              "steps": [{"name": "Build", "conclusion": "success"}]}]}

        gh = FakeGitHub(pages)
        records = bd.fetch_records(gh, "o/r", "build.yml", T0, T0 + dt.timedelta(hours=1))
        run_paths = [p for p in gh.paths if "/runs?" in p and "/jobs" not in p]
        self.assertEqual(len(run_paths), 2)
        self.assertTrue(all(p.startswith("repos/o/r/actions/workflows/build.yml/runs") for p in run_paths))
        self.assertTrue(all("workflow_id" not in p for p in gh.paths))
        self.assertEqual(len(records), 100)  # the in-progress run is not fetched

    def test_api_budget_is_enforced(self):
        gh = bd.GitHub("definitely-not-a-cli", "x", 1, 0)
        with self.assertRaises(RuntimeError):
            gh.api("rate_limit")


class Cli(unittest.TestCase):
    def run_main(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = bd.main(argv)
        return rc, out.getvalue()

    def test_disabled_by_default(self):
        rc, out = self.run_main(["--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["state"], "disabled")

    @testing_support.requires_tomllib
    def test_only_the_m3_canary_profile_enables_it(self):
        # The watch reads GitHub fleet-wide, so one host is enough; m3 is the canary.
        enabled = sorted(path.stem for path in (HERE.parent / "profiles").glob("*.toml")
                         if bd.profile_settings(path.stem).get("enabled", False))
        self.assertEqual(enabled, ["m3-macos-fleet"])

    def test_replay_fires_and_exits_nonzero(self):
        records = [rec(1, "m5", "green", "aaa", 100), rec(2, "m3", "build_failure", "aaa", 110)]
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.json"
            jobs.write_text(json.dumps(records))
            (Path(tmp) / "2.log").write_text(LINK_LOG)
            rc, out = self.run_main(["--enable", "--json", "--from-jobs", str(jobs), "--logs-dir", tmp,
                                     "--now", bd.iso(T0 + dt.timedelta(minutes=200))])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["findings"][0]["host"], "m3")


class Backtest(unittest.TestCase):
    """Replays recorded gate jobs from the 2026-09-26 m3 incident and a healthy day."""

    def replay(self, name, now, hours=6.0, streak=bd.DEFAULT_STREAK):
        records = json.loads((FIXTURES / f"{name}.json").read_text())
        logs = FIXTURES / "logs"

        def log_for(record):
            path = logs / f"{record['job_id']}.txt"
            return path.read_text() if path.exists() else None

        ev = bd.Evaluation(records=records, now=bd.parse_time(now),
                           window=dt.timedelta(hours=hours), log_for=log_for, streak=streak)
        return bd.evaluate(ev)

    @unittest.skipUnless((FIXTURES / "incident-2026-09-26.json").exists(), "fixture absent")
    def test_incident_fires_on_m3_within_half_an_hour(self):
        # The first poisoned m3 build started 08:23:08Z and failed at 08:32:35Z; the
        # third distinct commit to fail the same way completed at 08:47:48Z.
        self.assertEqual(self.replay("incident-2026-09-26", "2026-09-26T08:47:00Z")["state"], "ok")
        result = self.replay("incident-2026-09-26", "2026-09-26T08:47:48Z")
        self.assertEqual(result["state"], "problem")
        [finding] = result["findings"]
        self.assertEqual((finding["rule"], finding["host"]), ("streak", "m3"))
        self.assertIn("write_scenario_wav", finding["log"]["fingerprint"])
        self.assertEqual(finding["green_counterpart"]["host"], "m1")
        self.assertEqual(finding["commit"][:8], "dd8d519d")  # newest failure: the one to re-run
        self.assertIn("tartci ccache", finding["remedy"])

    @unittest.skipUnless((FIXTURES / "incident-2026-09-26.json").exists(), "fixture absent")
    def test_cross_host_pr_breakage_the_same_evening_is_quiet(self):
        # 16:44-18:26Z one PR's compile error failed on m5 AND m3: not a host fault.
        records = json.loads((FIXTURES / "incident-2026-09-26.json").read_text())
        stamps = sorted({r["completed_at"] for r in records
                         if "2026-09-26T16:00" <= r["completed_at"] <= "2026-09-26T20:00"})
        self.assertGreater(len(stamps), 5)
        failures = [r for r in records if r["outcome"] == "build_failure"
                    and "2026-09-26T16:00" <= r["completed_at"] <= "2026-09-26T20:00"]
        self.assertEqual({r["host"] for r in failures}, {"m1", "m3", "m5"})  # control: look-alike present
        # K=2 is the setting where m5's two same-error reds plus m3's later
        # green would fire without the "no other host fails the same way" guard.
        for streak in (2, 3):
            for stamp in stamps:
                result = self.replay("incident-2026-09-26", stamp, streak=streak)
                self.assertNotEqual(result["state"], "problem", (streak, stamp))

    @unittest.skipUnless((FIXTURES / "control-2026-09-25.json").exists(), "fixture absent")
    def test_healthy_day_is_quiet_at_every_step(self):
        records = json.loads((FIXTURES / "control-2026-09-25.json").read_text())
        stamps = sorted({r["completed_at"] for r in records})
        self.assertGreater(len(stamps), 10)  # control: the fixture really has jobs
        for stamp in stamps:
            result = self.replay("control-2026-09-25", stamp)
            self.assertNotEqual(result["state"], "problem", stamp)


if __name__ == "__main__":
    unittest.main()
