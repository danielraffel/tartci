#!/usr/bin/env python3
"""Hermetic tests for schedule_backstop.py (no network, no GitHub).

A fake GitHub CLI holds the manifest and each workflow's newest run, records
every call, and turns a dispatch into a new queued run, exactly as GitHub does.

Run:  python3 scripts/test_schedule_backstop.py
"""
from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import schedule_backstop as sb  # noqa: E402

REPO = "Generous-Corp/pulp"
NOW = sb.parse_time("2026-10-02T17:00:00Z")


def manifest(*rows, ref="main"):
    return {"schema_version": 1, "ref": ref,
            "workflows": [
                ({"file": f, "cadence_minutes": c}
                 if len(row) == 2 else
                 {"file": f, "cadence_minutes": c, "dispatch_inputs": row[2]})
                for row in rows
                for f, c in [row[:2]]
            ],
            "excluded": []}


class FakeGitHub:
    def __init__(self, manifest_value, runs=None, fail=()):
        self.manifest = manifest_value
        self.runs = dict(runs or {})
        self.fail = set(fail)
        self.calls = []
        self.dispatches = []

    def __call__(self, args):
        self.calls.append(args)
        path = args[1] if args[1] != "-X" else args[3]
        if path.endswith("/contents/.github/schedule-backstop.json"):
            if "manifest" in self.fail:
                raise sb.BackstopError("contents read failed")
            body = base64.b64encode(json.dumps(self.manifest).encode()).decode()
            return json.dumps({"content": body})
        workflow = path.split("/actions/workflows/")[1].split("/")[0]
        if workflow in self.fail:
            raise sb.BackstopError(f"{workflow} read failed")
        if args[1] == "-X":
            self.dispatches.append(workflow)
            self.runs[workflow] = {"created_at": sb.iso(self.clock), "status": "queued",
                                   "event": "workflow_dispatch"}
            return ""
        assert "branch=" not in path, "the server-side branch filter returns stale pages"
        run = self.runs.get(workflow)
        runs = [dict(run, head_branch=run.get("head_branch", "main"))] if run else []
        return json.dumps({"workflow_runs": self.noise.get(workflow, []) + runs})

    noise = {}

    clock = NOW


def run_at(minutes_ago, status="completed", event="schedule"):
    return {"created_at": sb.iso(NOW - minutes_ago * 60), "status": status, "event": event}


def fresh_state():
    return {"schema_version": 1, "workflows": {}}


class DecideTests(unittest.TestCase):
    def test_stale_completed_run_is_dispatched(self):
        self.assertEqual(sb.decide(run_at(31), 1800, None, NOW)["action"], "dispatch")

    def test_fresh_run_of_any_event_suppresses_dispatch(self):
        verdict = sb.decide(run_at(5, event="push"), 1800, None, NOW)
        self.assertEqual((verdict["action"], verdict["reason"]), ("skip", "fresh"))
        self.assertEqual(verdict["next_check"], NOW - 300 + 1800)

    def test_queued_run_is_never_stacked(self):
        verdict = sb.decide(run_at(300, status="queued"), 1800, None, NOW)
        self.assertEqual((verdict["action"], verdict["reason"]), ("skip", "newest_run_queued"))

    def test_own_recent_dispatch_bounds_a_stale_read(self):
        verdict = sb.decide(run_at(600), 1800, NOW - 60, NOW)
        self.assertEqual((verdict["action"], verdict["reason"]), ("skip", "dispatched_recently"))

    def test_workflow_with_no_runs_is_dispatched(self):
        self.assertEqual(sb.decide(None, 1800, None, NOW)["reason"], "no_runs")


class TickTests(unittest.TestCase):
    def test_dry_run_reports_but_never_posts(self):
        gh = FakeGitHub(manifest(("a.yml", 30)), {"a.yml": run_at(40)})
        report = sb.tick(gh, REPO, fresh_state(), NOW, apply=False)
        self.assertEqual(report["workflows"][0]["action"], "would_dispatch")
        self.assertEqual(gh.dispatches, [])

    def test_apply_dispatches_only_stale_workflows_on_the_manifest_ref(self):
        gh = FakeGitHub(manifest(("a.yml", 30), ("b.yml", 15)),
                        {"a.yml": run_at(40), "b.yml": run_at(3)})
        sb.tick(gh, REPO, fresh_state(), NOW, apply=True)
        self.assertEqual(gh.dispatches, ["a.yml"])
        post = [c for c in gh.calls if c[1] == "-X"][0]
        self.assertEqual(post[-2:], ["-f", "ref=main"])

    def test_apply_dispatches_declared_workflow_inputs(self):
        gh = FakeGitHub(
            manifest(("shipyard-merge-steward.yml", 10, {"apply": "true"})),
            {"shipyard-merge-steward.yml": run_at(11)},
        )
        sb.tick(gh, REPO, fresh_state(), NOW, apply=True)
        post = [c for c in gh.calls if c[1] == "-X"][0]
        self.assertEqual(
            post[-4:], ["-f", "ref=main", "-f", "inputs[apply]=true"]
        )

    def test_cadence_is_kept_across_ticks_without_extra_reads(self):
        gh = FakeGitHub(manifest(("a.yml", 30)), {"a.yml": run_at(40)})
        state = fresh_state()
        for minute in range(0, 120, 5):
            gh.clock = NOW + minute * 60
            if gh.runs["a.yml"]["status"] == "queued" and minute % 10 == 5:
                gh.runs["a.yml"]["status"] = "completed"
            sb.tick(gh, REPO, state, gh.clock, apply=True)
        # 120 minutes at a 30-minute cadence: exactly four dispatches.
        self.assertEqual(len(gh.dispatches), 4)
        reads = [c for c in gh.calls if c[1] != "-X" and "/runs?" in c[1]]
        self.assertLessEqual(len(reads), 8)

    def test_a_second_host_sees_the_first_hosts_dispatch(self):
        gh = FakeGitHub(manifest(("a.yml", 30)), {"a.yml": run_at(40)})
        sb.tick(gh, REPO, fresh_state(), NOW, apply=True)
        sb.tick(gh, REPO, fresh_state(), NOW + 60, apply=True)
        self.assertEqual(gh.dispatches, ["a.yml"])

    def test_one_failing_read_does_not_block_the_others(self):
        gh = FakeGitHub(manifest(("a.yml", 30), ("b.yml", 30)),
                        {"a.yml": run_at(40), "b.yml": run_at(40)}, fail={"a.yml"})
        report = sb.tick(gh, REPO, fresh_state(), NOW, apply=True)
        self.assertEqual(gh.dispatches, ["b.yml"])
        self.assertEqual(report["errors"], 1)

    def test_unreadable_manifest_dispatches_nothing(self):
        gh = FakeGitHub(manifest(("a.yml", 30)), {"a.yml": run_at(40)}, fail={"manifest"})
        with self.assertRaises(sb.BackstopError):
            sb.tick(gh, REPO, fresh_state(), NOW, apply=True)
        self.assertEqual(gh.dispatches, [])

    def test_newest_run_ignores_other_branches(self):
        gh = FakeGitHub(manifest(("a.yml", 30)), {"a.yml": run_at(40)})
        gh.noise = {"a.yml": [dict(run_at(1, event="pull_request"), head_branch="feature/x")]}
        sb.tick(gh, REPO, fresh_state(), NOW, apply=True)
        self.assertEqual(gh.dispatches, ["a.yml"])

    def test_dispatch_cap_bounds_one_tick(self):
        rows = [(f"w{i}.yml", 30) for i in range(5)]
        gh = FakeGitHub(manifest(*rows), {f: run_at(40) for f, _ in rows})
        sb.tick(gh, REPO, fresh_state(), NOW, apply=True, max_dispatches=2)
        self.assertEqual(len(gh.dispatches), 2)


class ManifestTests(unittest.TestCase):
    def test_rejects_unknown_fields(self):
        bad = manifest(("a.yml", 30))
        bad["extra"] = 1
        with self.assertRaises(sb.BackstopError):
            sb.validate_manifest(bad)

    def test_accepts_and_normalizes_dispatch_inputs(self):
        value = sb.validate_manifest(
            manifest(("shipyard-merge-steward.yml", 10, {"apply": "true"}))
        )
        self.assertEqual(
            value["workflows"][0]["dispatch_inputs"], {"apply": "true"}
        )

    def test_rejects_non_string_dispatch_inputs(self):
        bad = manifest(("shipyard-merge-steward.yml", 10, {"apply": True}))
        with self.assertRaises(sb.BackstopError):
            sb.validate_manifest(bad)

    def test_rejects_path_like_workflow_names(self):
        for name in ("../a.yml", "-X.yml", "a.sh"):
            with self.assertRaises(sb.BackstopError, msg=name):
                sb.validate_manifest(manifest((name, 30)))

    def test_rejects_out_of_range_cadence(self):
        for cadence in (0, 61, 1439, 1441, 2880, "30", True):
            with self.assertRaises(sb.BackstopError, msg=repr(cadence)):
                sb.validate_manifest(manifest(("a.yml", cadence)))

    def test_a_daily_workflow_is_dispatched_only_after_a_day_without_a_run(self):
        self.assertEqual(sb.validate_manifest(manifest(("nightly.yml", 1440)))["workflows"],
                         [{"file": "nightly.yml", "cadence_minutes": 1440}])
        day = 1440 * 60
        now = 1_800_000_000.0
        run = {"created_at": sb.iso(now - day + 600), "status": "completed"}
        self.assertEqual(sb.decide(run, day, None, now)["action"], "skip")      # 23h50m old
        run = {"created_at": sb.iso(now - day - 600), "status": "completed"}
        self.assertEqual(sb.decide(run, day, None, now)["action"], "dispatch")  # dropped cron
        # Its own dispatch is paced by the day, not by the tick.
        self.assertEqual(sb.decide(run, day, now - 3600, now)["action"], "skip")


class CommandLineTests(unittest.TestCase):
    def _run(self, env_extra, *args):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp, **env_extra}
            done = subprocess.run([sys.executable, str(HERE / "schedule_backstop.py"),
                                   "--state", os.path.join(tmp, "s.json"), *args],
                                  capture_output=True, text=True, env=env)
            return done

    def test_apply_without_authority_refuses_and_fails_open(self):
        done = self._run({"TARTCI_BACKSTOP_GH_CLI": "/usr/bin/true"}, "--apply")
        self.assertEqual(done.returncode, 2)
        self.assertIn("TARTCI_BACKSTOP_AUTHORITY=1", done.stdout)

    def test_ambient_gh_is_refused(self):
        done = self._run({"TARTCI_BACKSTOP_GH_CLI": "gh"})
        self.assertEqual(done.returncode, 2)
        self.assertIn("refuses ambient gh", done.stdout)

    def test_dry_run_end_to_end_through_a_stub_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = base64.b64encode(json.dumps(manifest(("a.yml", 30))).encode()).decode()
            stub = Path(tmp) / "ghapp-stub"
            stub.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                f"  *contents*) echo '{{\"content\": \"{body}\"}}' ;;\n"
                "  *'-X POST'*) echo POSTED >&2; exit 9 ;;\n"
                "  *) echo '{\"workflow_runs\": [{\"created_at\": \"2020-01-01T00:00:00Z\","
                " \"head_branch\": \"main\", \"status\": \"completed\"}]}' ;;\n"
                "esac\n")
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            done = self._run({"TARTCI_BACKSTOP_GH_CLI": str(stub)}, "--json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        report = json.loads(done.stdout)
        self.assertEqual(report["workflows"][0]["action"], "would_dispatch")
        self.assertEqual(report["dispatched"], 0)


if __name__ == "__main__":
    unittest.main()
