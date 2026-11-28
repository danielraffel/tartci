#!/usr/bin/env python3
"""PR-class yield to a queued merge group, with a bounded grace hold.

Properties covered, each from the side that could break:

* yield and hold: with a merge-group run queued, a PR-class claim yields and a
  placeholder reservation holds the slot, which other lanes' slot claims count;
* no yield when none is queued, and no GitHub call when the knob is off;
* fail open: a read error, a timeout, a bad payload all claim, and drop a hold;
* the hold ends early when the run is claimed (left the queue, or this
  repository's merge-group tier claims here) and when the grace period ends;
* one hold per run id per host: an expired run never holds again, a new run does;
* other repositories and other tiers are unaffected;
* the bash glue runs under macOS /bin/bash 3.2 with `set -euo pipefail`;
* the profile validator and renderer.

Compatible with the hosts' Python 3.9 (the validator tests need tomllib).
"""
from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import testing_support

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
YIELD = SCRIPTS / "pr_class_yield.py"
LIB = ROOT / "providers/tart-macos/pr-class-yield.lib.sh"
CAPLIB = ROOT / "providers/tart-macos/macos-vm-cap.lib.sh"
sys.path.insert(0, str(SCRIPTS))

import pr_class_yield  # noqa: E402

BASH32 = "/bin/bash"
VELLUM = "Generous-Corp/vellum"
PULP = "Generous-Corp/pulp"
WORKFLOW = "Locked macOS GPU proof"
T0 = 1_800_000_000.0


def run_row(run_id: int, name: str = WORKFLOW, created: str = "2026-11-28T03:46:57Z") -> dict:
    return {"id": run_id, "name": name, "status": "queued", "event": "merge_group",
            "created_at": created}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.resv = self.root / "resv"
        self.stub = self.root / "stub"
        self.stub.mkdir()
        gh = self.stub / "gh"
        gh.write_text(
            "#!/bin/bash\n"
            f'echo "$2" >> "{self.stub}/calls.log"\n'
            f'[ -f "{self.stub}/sleep" ] && sleep "$(cat "{self.stub}/sleep")"\n'
            f'[ -f "{self.stub}/fail" ] && {{ echo "rate limited" >&2; exit 1; }}\n'
            'case "$2" in\n'
            f'  *"/jobs"*) [ -f "{self.stub}/jobs_sleep" ] && sleep "$(cat "{self.stub}/jobs_sleep")"; cat "{self.stub}/jobs.json" ;;\n'
            f'  *) cat "{self.stub}/runs.json" ;;\n'
            "esac\n")
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.gh = gh
        self.queue()
        (self.stub / "jobs.json").write_text(json.dumps(
            {"jobs": [{"id": 777, "status": "queued"}, {"id": 778, "status": "completed"}]}))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def queue(self, *rows: dict) -> None:
        (self.stub / "runs.json").write_text(json.dumps({"workflow_runs": list(rows)}))

    def calls(self) -> int:
        log = self.stub / "calls.log"
        return len(log.read_text().splitlines()) if log.exists() else 0

    def argv(self, command: str = "check", repo: str = VELLUM, now: float = T0, **kw) -> list:
        out = [command, "--repo", repo, "--host", "m3", "--state-dir", str(self.state),
               "--resv-dir", str(self.resv), "--gh-cli", str(self.gh), "--now", str(now),
               "--owner-pid", str(os.getpid()), "--workflow", WORKFLOW, "--gh-timeout", "5"]
        for key, value in kw.items():
            out += [f"--{key.replace('_', '-')}", str(value)]
        return out

    def run_cli(self, *args, **kw) -> str:
        out = subprocess.run([sys.executable, str(YIELD), *self.argv(*args, **kw)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def holder(self, repo: str = VELLUM) -> Path:
        return pr_class_yield.placeholder_path(self.resv, repo)

    def reservations(self, now: float) -> int:
        """Reservations counted by macos-vm-cap.lib.sh at `now` (bash 3.2, set -u)."""
        script = (f'set -euo pipefail\nsource {CAPLIB}\n'
                  f'date(){{ [ "${{1:-}}" = "+%s" ] && echo {int(now)} || command date "$@"; }}\n'
                  "tartci_count_reservations\n")
        env = {**os.environ, "TARTCI_MACOS_RESV_DIR": str(self.resv)}
        return int(subprocess.run([BASH32, "-c", script], capture_output=True, text=True,
                                  env=env).stdout.strip() or -1)


class YieldTests(Base):
    def test_yields_and_holds_the_slot_when_a_merge_group_run_is_queued(self) -> None:
        self.queue(run_row(101))
        line = self.run_cli(grace_secs=120)
        self.assertTrue(line.startswith("yield "), line)
        for token in (f"repo={VELLUM}", "host=m3", "run_id=101", "job_ids=777", "grace=120"):
            self.assertIn(token, line)
        self.assertTrue(self.holder().exists())
        # The placeholder counts as a reservation for every lane until the grace ends...
        self.assertEqual(self.reservations(T0 + 119), 1)
        # ...and stops counting at exactly the end of it, with nobody removing it.
        self.assertEqual(self.reservations(T0 + 121), 0)
        # A second PR-class poll inside the grace keeps yielding, with a fresh read.
        before = self.calls()
        self.assertTrue(self.run_cli(now=T0 + 30).startswith("hold "))
        self.assertEqual(self.calls(), before + 1)

    def test_no_yield_when_nothing_is_queued(self) -> None:
        self.queue()
        line = self.run_cli()
        self.assertTrue(line.startswith("claim "), line)
        self.assertFalse(self.holder().exists())
        # Another workflow's queued run, or a non-queued one, is not this tier's.
        self.queue(run_row(5, name="Build and Test"))
        self.assertTrue(self.run_cli().startswith("claim "))

    def test_fail_open_on_a_read_error(self) -> None:
        self.queue(run_row(101))
        (self.stub / "fail").write_text("1")
        line = self.run_cli()
        self.assertTrue(line.startswith("claim "), line)
        self.assertIn("reason=read_error", line)
        (self.stub / "fail").unlink()
        (self.stub / "runs.json").write_text("not json")
        self.assertIn("reason=read_error", self.run_cli())
        (self.stub / "runs.json").write_text(json.dumps({"unexpected": []}))
        self.assertIn("reason=read_error", self.run_cli())

    def test_fail_open_on_a_timeout(self) -> None:
        self.queue(run_row(101))
        (self.stub / "sleep").write_text("20")
        out = subprocess.run([sys.executable, str(YIELD), *self.argv(gh_timeout=1)],
                             capture_output=True, text=True, timeout=30)
        self.assertTrue(out.stdout.startswith("claim "), out.stdout)
        self.assertFalse(self.holder().exists())

    def test_a_read_error_during_a_hold_releases_it(self) -> None:
        self.queue(run_row(101))
        self.run_cli()
        self.assertTrue(self.holder().exists())
        (self.stub / "fail").write_text("1")
        self.assertIn("reason=read_error", self.run_cli(now=T0 + 10))
        self.assertFalse(self.holder().exists())

    def test_the_hold_ends_early_when_the_run_is_claimed(self) -> None:
        self.queue(run_row(101))
        self.run_cli()
        self.queue()  # claimed on some host: no longer queued
        line = self.run_cli(now=T0 + 40)
        self.assertTrue(line.startswith("claim "), line)
        self.assertIn("ended=served", line)
        self.assertFalse(self.holder().exists())
        self.assertEqual(self.reservations(T0 + 41), 0)

    def test_the_hold_ends_early_when_the_merge_group_tier_claims_here(self) -> None:
        self.queue(run_row(101))
        self.run_cli()
        line = self.run_cli("release", now=T0 + 20)
        self.assertIn("ended=served", line)
        self.assertIn("via=merge_group_claim", line)
        self.assertFalse(self.holder().exists())
        # Nothing to release is a quiet no-op.
        self.assertIn("reason=no_hold", self.run_cli("release", now=T0 + 21))

    def test_the_hold_expires_and_the_run_never_holds_again(self) -> None:
        self.queue(run_row(101))
        self.run_cli(grace_secs=120)
        line = self.run_cli(now=T0 + 121)  # still queued, grace over
        self.assertTrue(line.startswith("claim "), line)
        self.assertIn("ended=expired", line)
        self.assertFalse(self.holder().exists())
        # Once per run id per host: later polls with the same run claim.
        for later in (T0 + 130, T0 + 4000):
            line = self.run_cli(now=later)
            self.assertTrue(line.startswith("claim "), line)
            self.assertFalse(self.holder().exists())
        # A different run id is a different job and holds once.
        self.queue(run_row(101), run_row(102, created="2026-11-28T04:46:57Z"))
        line = self.run_cli(now=T0 + 4100)
        self.assertTrue(line.startswith("yield "), line)
        self.assertIn("run_id=102", line)

    def test_other_repositories_are_unaffected(self) -> None:
        self.queue(run_row(101))
        self.run_cli()
        self.assertTrue(self.holder().exists())
        self.assertFalse(self.holder(PULP).exists())
        self.assertEqual(self.holder(PULP).name, "resv.yield.Generous-Corp_pulp")
        # Pulp's own state is separate: its hold history does not see Vellum's.
        self.queue()
        self.assertTrue(self.run_cli(repo=PULP).startswith("claim "))
        self.assertTrue(self.holder().exists())
        self.assertEqual(self.reservations(T0 + 5), 1)

    def test_the_grace_is_bounded(self) -> None:
        self.assertEqual([pr_class_yield.clamp_grace(v) for v in (1, 30, 120, 600, 9999, "x")],
                         [30, 30, 120, 600, 600, 120])
        self.queue(run_row(101))
        self.assertIn("grace=30", self.run_cli(grace_secs=5))

    def test_one_hold_covers_every_run_queued_at_that_moment(self) -> None:
        self.queue(run_row(101), run_row(102, created="2026-11-28T03:50:00Z"),
                   run_row(103, created="2026-11-28T03:55:00Z"))
        line = self.run_cli(grace_secs=120)
        self.assertTrue(line.startswith("yield "), line)
        self.assertIn("covered_runs=101,102,103", line)
        # The hold runs out, and the other two queued runs do not start another.
        self.assertIn("ended=expired", self.run_cli(now=T0 + 121))
        for later in (T0 + 125, T0 + 250, T0 + 400):
            line = self.run_cli(now=later)
            self.assertTrue(line.startswith("claim "), line)
            self.assertFalse(self.holder().exists())
        # A run created after the hold started is new and holds once.
        self.queue(run_row(101), run_row(104, created="2026-11-28T04:30:00Z"))
        self.assertIn("run_id=104", self.run_cli(now=T0 + 500))

    def test_the_job_id_read_is_short_and_never_blocks_the_hold(self) -> None:
        import time
        self.queue(run_row(101))
        (self.stub / "jobs_sleep").write_text("30")
        started = time.monotonic()
        out = subprocess.run([sys.executable, str(YIELD), *self.argv(gh_timeout=25)],
                             capture_output=True, text=True, timeout=40)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(out.stdout.startswith("yield "), out.stdout)
        self.assertIn("job_ids=unknown", out.stdout)
        self.assertTrue(self.holder().exists())

    def test_a_read_failure_drops_the_placeholder_whatever_the_state_says(self) -> None:
        self.queue(run_row(101))
        state_file = self.state / "Generous-Corp_vellum.json"
        (self.stub / "fail").write_text("1")
        for label, write in (("missing", lambda: state_file.unlink()),
                             ("malformed", lambda: state_file.write_text("{not json")),
                             ("wrong shape", lambda: state_file.write_text("[1, 2]"))):
            with self.subTest(state=label):
                (self.stub / "fail").unlink()
                state_file.unlink() if state_file.exists() else None
                self.run_cli(now=T0)  # a real hold: placeholder plus state
                (self.stub / "fail").write_text("1")
                self.assertTrue(self.holder().exists())
                write()
                line = self.run_cli(now=T0 + 5)
                self.assertIn("reason=read_error", line)
                self.assertFalse(self.holder().exists(), label)
                (self.stub / "fail").unlink()
                state_file.unlink() if state_file.exists() else None
                (self.state / "x").touch()
                (self.stub / "fail").write_text("1")

    def test_every_read_attempt_is_counted(self) -> None:
        self.queue(run_row(101))
        self.assertIn("reads=2", self.run_cli())  # the queue read plus the job-id read
        self.assertIn("reads=1", self.run_cli(now=T0 + 10))  # a continuing hold: one read
        (self.stub / "fail").write_text("1")
        self.assertIn("reads=1", self.run_cli(now=T0 + 20))  # a failed read still counts
        self.assertEqual(self.calls(), 4)

    def test_a_dead_owner_frees_the_slot_at_once(self) -> None:
        proc = subprocess.Popen(["sleep", "30"])
        pid = proc.pid
        try:
            real_now = int(__import__("time").time())
            pr_class_yield.write_placeholder(self.holder(), pid, real_now + 600, 7200)
            self.assertEqual(self.reservations(real_now), 1)
        finally:
            proc.kill()
            proc.wait()
        self.assertEqual(self.reservations(int(__import__("time").time())), 0)

    def test_a_broken_helper_claims(self) -> None:
        out = subprocess.run([sys.executable, str(YIELD), "check"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertTrue(out.stdout.startswith("claim "), out.stdout)


class GlueTests(Base):
    """providers/tart-macos/pr-class-yield.lib.sh under /bin/bash 3.2, set -euo pipefail."""

    TIERS = f"vellum-sterile-consumer\nvellum-build-macos-merge-group\nvellum-build-macos"

    def gate(self, tier: int, *, enabled: str = "1", running: int = 1, cap: int = 2,
             tiers: str | None = None, reserved: int = 0, root: str | None = None) -> tuple:
        log = self.root / "events.log"
        script = f"""set -euo pipefail
TARTCI_ROOT={ROOT}
REPO={VELLUM}
WORKFLOW_TIERS=x
ASSIGNMENT_MODE=legacy
TIER_LABELS_CONFIG='{tiers if tiers is not None else self.TIERS}'
TARTCI_PR_CLASS_YIELD={enabled}
TARTCI_MACOS_RESV_DIR={self.resv}
event(){{ printf '%s\\n' "$*" >> {log}; }}
tartci_claim_delay_class(){{
  local want="$1" i=0 l
  while IFS= read -r l; do
    [ "$i" = "$want" ] && {{ printf '%s\\n' "$l"; return 0; }}; i=$((i+1))
  done <<< "$TIER_LABELS_CONFIG"; return 1
}}
tier_workflow_args(){{ printf '%s\\n' '{WORKFLOW}'; }}
tartci_count_reservations(){{ printf '%s' {reserved}; }}
tartci_slot_priority_token(){{
  local key="$1" token words=()
  read -r -a words <<< "$2"
  for token in ${{words[@]+"${{words[@]}}"}}; do
    case "$token" in "$key="*) printf '%s\\n' "${{token#*=}}"; return 0 ;; esac
  done
  printf 'unknown\\n'
}}
STATE_DIR={self.root}
SCAN_REFRESH_NEXT=0; SCAN_REFRESH_RUN=""; SCAN_REFRESH_CLASS=""
source {LIB}
rc=0
tartci_pr_class_yield_gate "self-hosted,x" {tier} {cap} {running} || rc=$?
echo "rc=$rc"
echo "refresh=$SCAN_REFRESH_NEXT run=$SCAN_REFRESH_RUN class=$SCAN_REFRESH_CLASS"
"""
        env = {**os.environ, "TARTCI_GH_CLI": str(self.gh), "TARTCI_PR_CLASS_YIELD_DIR": str(self.state),
               "TARTCI_PR_CLASS_YIELD_GRACE_SECS": "90"}
        if root is not None:
            script = script.replace(f"TARTCI_ROOT={ROOT}", f"TARTCI_ROOT={root}")
        out = subprocess.run([BASH32, "-c", script], capture_output=True, text=True, env=env)
        events = log.read_text() if log.exists() else ""
        self.refresh = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else "", out.stderr, events

    def test_a_pr_class_claim_yields_and_logs(self) -> None:
        self.queue(run_row(101))
        out, err, events = self.gate(2)
        self.assertEqual(out, "rc=75", err)
        self.assertIn("pr_class_yield ", events)
        self.assertIn("run_id=101", events)
        self.assertIn("grace=90", events)
        self.assertTrue(self.holder().exists())
        # While held, the next PR-class poll yields without needing a free slot.
        out, _, _ = self.gate(2, running=2, reserved=2)
        self.assertEqual(out, "rc=75")

    def test_every_read_attempt_logs_a_counted_event(self) -> None:
        self.queue(run_row(101))
        events = self.gate(2)[2]
        self.assertIn("pr_class_yield_read ", events)
        self.assertIn("reads=2", events)
        (self.stub / "fail").write_text("1")
        (self.root / "events.log").unlink()
        self.gate(2)
        events = (self.root / "events.log").read_text()
        self.assertIn("pr_class_yield_read ", events)
        self.assertIn("reads=1", events)

    def test_a_yield_asks_for_an_uncached_scan_of_the_held_run(self) -> None:
        self.queue(run_row(101))
        self.assertEqual(self.gate(2)[0], "rc=75")
        self.assertEqual(self.refresh, "refresh=1 run=101 class=pr_class_yield")
        # A continuing hold, and a claim, leave the lane's refresh flag alone.
        self.assertEqual(self.gate(2)[0], "rc=75")
        self.assertEqual(self.refresh, "refresh=0 run= class=")
        self.queue()
        self.assertEqual(self.gate(2)[0], "rc=0")
        self.assertEqual(self.refresh, "refresh=0 run= class=")

    def test_a_failed_read_is_logged_once_per_throttle_window(self) -> None:
        self.queue(run_row(101))
        (self.stub / "fail").write_text("1")
        out, _, events = self.gate(2)
        self.assertEqual(out, "rc=0")
        self.assertIn("pr_class_yield_read_error", events)
        self.assertIn("reason=read_error", events)
        before = events.count("pr_class_yield_read_error")
        self.assertEqual(self.gate(2)[2].count("pr_class_yield_read_error"), before)
        # A helper that dies without output is a logged failure too.
        (self.root / "pr-class-yield-read-error.stamp").unlink()
        events = self.gate(2, root="/nonexistent-root")[2]
        self.assertIn("helper_failed", events)

    def test_any_tier_above_pr_class_releases_a_hold(self) -> None:
        self.queue(run_row(101))
        self.gate(2)
        self.assertTrue(self.holder().exists())
        # A tier ranked ABOVE merge-group (a future sterile-consumer label).
        out, _, events = self.gate(0)
        self.assertEqual(out, "rc=0")
        self.assertIn("pr_class_yield_served", events)
        self.assertFalse(self.holder().exists())

    def test_off_by_default_makes_no_github_call(self) -> None:
        self.queue(run_row(101))
        out, err, events = self.gate(2, enabled="0")
        self.assertEqual((out, self.calls(), events), ("rc=0", 0, ""), err)
        self.assertFalse(self.holder().exists())

    def test_no_call_without_a_free_slot_and_no_yield_when_none_queued(self) -> None:
        self.queue(run_row(101))
        self.assertEqual(self.gate(2, running=2, cap=2)[0], "rc=0")
        self.assertEqual(self.calls(), 0)
        self.queue()
        self.assertEqual(self.gate(2)[0], "rc=0")
        self.assertEqual(self.calls(), 1)

    def test_other_tiers_are_never_yielded(self) -> None:
        self.queue(run_row(101))
        self.assertEqual(self.gate(0)[0], "rc=0")  # sterile
        self.assertEqual(self.gate(1)[0], "rc=0")  # merge-group
        self.assertEqual(self.calls(), 0)
        self.assertEqual(self.gate(2, tiers="vellum-build-macos")[0], "rc=0")  # no merge-group tier

    def test_the_merge_group_claim_releases_the_hold_and_logs_served(self) -> None:
        self.queue(run_row(101))
        self.gate(2)
        out, err, events = self.gate(1)
        self.assertEqual(out, "rc=0", err)
        self.assertIn("pr_class_yield_served", events)
        self.assertFalse(self.holder().exists())

    def test_an_expired_hold_logs_expired_and_claims(self) -> None:
        self.queue(run_row(101))
        self.gate(2)
        state = self.state / "Generous-Corp_vellum.json"
        data = json.loads(state.read_text())
        data["active"]["until"] = 1.0  # long past
        state.write_text(json.dumps(data))
        out, _, events = self.gate(2)
        self.assertEqual(out, "rc=0")
        self.assertIn("pr_class_yield_expired", events)

    def test_a_read_error_claims(self) -> None:
        self.queue(run_row(101))
        (self.stub / "fail").write_text("1")
        out, _, events = self.gate(2)
        self.assertEqual(out, "rc=0")
        self.assertIn("pr_class_yield_read_error", events)
        self.assertNotIn("pr_class_yield ", events)


class RunnerWiringTests(unittest.TestCase):
    def test_the_runner_gates_the_claim_on_the_yield(self) -> None:
        text = (ROOT / "providers/tart-macos/runner.sh").read_text()
        self.assertIn('source "$TARTCI_ROOT/providers/tart-macos/pr-class-yield.lib.sh"', text)
        self.assertIn('tartci_pr_class_yield_gate "$selected_labels" "$selected_tier" "$cap" "$r" || pc=1', text)
        self.assertIn('[ "$pc" -eq 0 ] && [ "$sp" -eq 0 ] && resv="$(tartci_warm_or_claim_slot', text)
        self.assertIn("reason=pr_class_yield", text)
        self.assertNotIn("pr_class_cap", text)


@testing_support.requires_tomllib
class ProfileTests(unittest.TestCase):
    PATH = ROOT / "profiles" / "m5studio-macos-fleet.toml"
    KEY = "pr_class_yield_to_merge_group"

    def load(self, body: str) -> dict:
        import macos_fleet_lanes as fleet
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m5studio.toml"
            path.write_text(body)
            return fleet.load(path)

    def with_keys(self, lines: str) -> str:
        body = self.PATH.read_text()
        anchor = 'priority = "vm"\n'
        self.assertEqual(body.count(anchor), 1)
        return body.replace(anchor, anchor + lines, 1)

    # Owner decision 2026-11-28: only m3 enables it.
    ENABLED_PROFILES = ("m3-macos-fleet.toml",)

    def test_only_the_approved_profiles_enable_it(self) -> None:
        paths = sorted((ROOT / "profiles").glob("*-macos-fleet.toml"))
        self.assertTrue(paths)
        for path in paths:
            if path.name in self.ENABLED_PROFILES:
                self.assertIn(f"{self.KEY} = true", path.read_text(), path.name)
            else:
                self.assertNotIn(self.KEY, path.read_text(), path.name)

    def test_the_lane_renders_the_env_and_others_do_not(self) -> None:
        import macos_fleet_lanes as fleet
        data = self.load(self.with_keys(f"{self.KEY} = true\nmerge_group_grace_secs = 90\n"))
        seen = 0
        for body in fleet.rendered_plists(data).values():
            env = plistlib.loads(body)["EnvironmentVariables"]
            if env.get("TARTCI_RUNNER_REPO") == VELLUM:
                seen += 1
                self.assertEqual(env["TARTCI_PR_CLASS_YIELD"], "1")
                self.assertEqual(env["TARTCI_PR_CLASS_YIELD_GRACE_SECS"], "90")
            else:
                self.assertNotIn("TARTCI_PR_CLASS_YIELD", env)
        self.assertGreaterEqual(seen, 1)
        for body in fleet.rendered_plists(self.load(self.PATH.read_text())).values():
            self.assertNotIn("TARTCI_PR_CLASS_YIELD", plistlib.loads(body)["EnvironmentVariables"])
        data = self.load(self.with_keys(f"{self.KEY} = true\n"))
        for body in fleet.rendered_plists(data).values():
            env = plistlib.loads(body)["EnvironmentVariables"]
            if env.get("TARTCI_RUNNER_REPO") == VELLUM:
                self.assertEqual(env["TARTCI_PR_CLASS_YIELD_GRACE_SECS"], "120")

    def test_a_yield_only_lane_renders_the_forced_discovery_budget(self) -> None:
        import macos_fleet_lanes as fleet
        body = self.with_keys(f"{self.KEY} = true\n")
        for drop in ("refresh_after_every_serve = true\n",):
            self.assertIn(drop, body)
            body = body.replace(drop, "", 1)
        body = body.replace("forced_discoveries_per_hour = 8\n", "forced_discoveries_per_hour = 13\n")
        data = self.load(body)
        seen = 0
        for plist in fleet.rendered_plists(data).values():
            env = plistlib.loads(plist)["EnvironmentVariables"]
            if env.get("TARTCI_RUNNER_REPO") == VELLUM:
                seen += 1
                self.assertEqual(env["TARTCI_PR_CLASS_YIELD"], "1")
                self.assertNotIn("TARTCI_REFRESH_AFTER_EVERY_SERVE", env)
                self.assertEqual(env["TARTCI_QUEUE_FORCED_DISCOVERIES_PER_HOUR"], "13")
        self.assertGreaterEqual(seen, 1)

    def test_a_minimum_queue_age_or_a_missing_budget_share_is_refused(self) -> None:
        m1 = (ROOT / "profiles" / "m1-macos-fleet.toml").read_text()
        anchor = 'repo = "Generous-Corp/vellum"\n'
        self.assertEqual(m1.count(anchor), 1)
        aged = m1.replace(anchor, anchor + f"{self.KEY} = true\nforced_discoveries_per_hour = 8\n", 1)
        self.assertIn("min_queued_age_seconds = 600", aged)
        with self.assertRaisesRegex(ValueError, "needs min_queued_age_seconds = 0"):
            self.load(aged)
        # The budget share is required with the key (the held run is scanned uncached).
        no_share = self.with_keys(f"{self.KEY} = true\n").replace(
            "forced_discoveries_per_hour = 8\n", "")
        self.assertNotEqual(no_share, self.with_keys(f"{self.KEY} = true\n"))
        with self.assertRaisesRegex(ValueError, "forced_discoveries_per_hour"):
            self.load(no_share)
        self.load(self.with_keys(f"{self.KEY} = true\nmerge_group_grace_secs = 120\n"))

    def test_the_validator_rejects_bad_config(self) -> None:
        body = self.PATH.read_text()
        on = f"{self.KEY} = true\n"
        cases = {
            "must be a boolean": self.with_keys(f'{self.KEY} = "yes"\n'),
            "from 30 to 600": self.with_keys(on + "merge_group_grace_secs = 5\n"),
            "from 30 to 600 ": self.with_keys(on + "merge_group_grace_secs = 601\n"),
            "needs pr_class_yield_to_merge_group": self.with_keys("merge_group_grace_secs = 90\n"),
            "exactly one": self.with_keys(on).replace(
                'label = "vellum-build-macos-merge-group"', 'label = "vellum-build-macos-mg"'),
            "at least one tier after": self.with_keys(on).replace(
                '[[lane.tier]]\nlabel = "vellum-build-macos"\nworkflow = "Locked macOS GPU proof"\n'
                'runner_group_id = 8\n', ""),
        }
        for message, mutated in cases.items():
            self.assertNotEqual(mutated, body, message)
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message.strip()):
                    self.load(mutated)
        self.load(self.with_keys(on + "merge_group_grace_secs = 90\n"))


if __name__ == "__main__":
    unittest.main()
