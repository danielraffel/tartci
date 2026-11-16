#!/usr/bin/env python3
"""Slot priority's short-job-first rule.

A lane whose scan saw one of its declared short jobs queued (Vellum's
sterile-consumer, about 1.5 min) takes the next free macOS VM slot ahead of
the other lanes of its own effective class. Properties covered:

* a short job goes first when a short and a long job of the same class are
  both queued, and the class order (merge_group over pull_request) is kept;
* the starvation bound: a lane is passed over at most
  short_job_first_max_passes times (default 1), and never once it was first
  passed short_job_first_max_pass_secs ago; a new wait starts a new count;
* with no short job queued, or the switch off, the order is the plain one;
* shadow leaves the order unchanged, records no pass, and reports
  `would-boot-short-first` from the short lane only;
* the scanner counts declared short jobs per class with no extra API call;
* the profile validator and renderer.

Compatible with the hosts' Python 3.9 (the validator tests need tomllib).
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import testing_support
from test_slot_priority import BASH32, LIB, QUEUE_SCAN, SLOT, Base, _write_exec, ago

import slot_demand  # noqa: E402
import slot_priority  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
VELLUM = "Generous-Corp/vellum"
PULP = "Generous-Corp/pulp"
STERILE = "Locked macOS GPU proof|sterile-consumer"


class ShortFirstBase(Base):
    def long(self, lane: str = "pulp-gate", *, free: int = 0, queued_ago: int = 1200,
             event_class: str = "merge_group", pid: int | None = None,
             extra: tuple = ()) -> dict:
        return self.decide(lane, free=free, event_class=event_class, queued_ago=queued_ago,
                           priority="110", repo=PULP, pid=pid, extra=extra)

    def short(self, lane: str = "vellum-gate", *, free: int = 1, mode: str = "on",
              short_ago: int | None = 30, event_class: str = "merge_group",
              queued_ago: int = 60, extra: tuple = ()) -> dict:
        flags = ["--short-job-first", mode]
        if short_ago is not None:
            flags += ["--short-queued-at", ago(short_ago)]
        return self.decide(lane, free=free, event_class=event_class, queued_ago=queued_ago,
                           priority="vm", repo=VELLUM, extra=(*flags, *extra))

    def withdraw(self, lane: str) -> None:
        """The short lane took its slot: it no longer wants one."""
        proc = subprocess.run([sys.executable, str(SLOT), "withdraw", "--store-dir",
                               str(self.store), "--lane", lane, "--json"],
                              text=True, capture_output=True, check=False, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def intents(self) -> dict:
        rows, _ = slot_priority.load_intents(self.store)
        return {row["lane"]: row for row in rows}


class ShortOverLongTests(ShortFirstBase):
    def test_a_short_job_takes_the_free_slot_ahead_of_an_older_long_job(self) -> None:
        self.long()
        result = self.short()
        self.assertEqual(result["decision"], "claim", result)
        self.assertEqual(result["short_first"], "boosted")
        self.assertEqual(result["short_passed"], ["pulp-gate"])
        line = slot_priority.line(result)
        self.assertIn("short_first=boosted", line)
        self.assertIn("short_passed=pulp-gate", line)
        # The pass is recorded on the lane that was passed over.
        self.assertEqual(self.intents()["pulp-gate"]["short_passes"], 1)

    def test_the_long_lane_yields_the_slot_to_a_waiting_short_lane(self) -> None:
        self.short(free=0)
        pulp = self.long(free=1)
        self.assertEqual(pulp["decision"], "defer", pulp)
        self.assertEqual(pulp["winner"]["lane"], "vellum-gate")

    def test_negative_control_without_the_rule_the_older_long_job_wins(self) -> None:
        self.long()
        result = self.short(mode="off")
        self.assertEqual(result["decision"], "defer", result)
        self.assertEqual(result["winner"]["lane"], "pulp-gate")
        self.assertNotIn("short_first", result)

    def test_the_class_order_is_kept(self) -> None:
        # A short pull_request job never passes a merge-group job.
        self.long()
        result = self.short(event_class="pull_request")
        self.assertEqual(result["decision"], "defer", result)
        self.assertNotIn("short_first", result)
        # Nor does a short merge-group job pass a tagged release.
        self.store_reset()
        labels = "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm,pulp-release-tagged"
        self.decide("release", free=0, event_class="other", queued_ago=10, priority="120",
                    extra=("--labels", labels, "--release-first", "1"))
        result = self.short(extra=("--release-first", "1"))
        self.assertEqual(result["decision"], "defer", result)

    def store_reset(self) -> None:
        path = slot_priority.intents_file(self.store)
        if path.exists():
            path.unlink()

    def test_the_short_flag_only_reorders_within_one_effective_class(self) -> None:
        now = 1_000_000.0
        short = {"lane": "s", "event_class": "pull_request", "short_job_first": "on",
                 "short_queued_at": "2026-11-09T00:00:00Z",
                 "queued_at": "2026-11-09T00:00:00Z"}
        mg = {"lane": "m", "event_class": "merge_group", "queued_at": "2026-11-09T00:00:00Z"}
        keys = sorted([short, mg], key=lambda r: slot_priority.rank_key(
            r, 0, now, slot_priority.ON_VIEW))
        self.assertEqual([row["lane"] for row in keys], ["m", "s"])


class StarvationBoundTests(ShortFirstBase):
    def test_a_lane_is_passed_over_at_most_once_by_default(self) -> None:
        pulp_pid = self.owner()
        self.long(pid=pulp_pid)
        first = self.short("vellum-gate")
        self.assertEqual(first["short_first"], "boosted")
        self.withdraw("vellum-gate")
        # The long lane refreshes its intent: the pass count survives the rebuild.
        self.long(pid=pulp_pid)
        self.assertEqual(self.intents()["pulp-gate"]["short_passes"], 1)
        # A second short job now waits its turn behind the lane already passed.
        second = self.short("vellum-gate-2")
        self.assertEqual(second["decision"], "defer", second)
        self.assertEqual(second["winner"]["lane"], "pulp-gate")
        self.assertNotIn("short_first", second)
        # And the passed lane, asking for a slot, is ranked first.
        pulp = self.long(pid=pulp_pid, free=1)
        self.assertEqual(pulp["decision"], "claim", pulp)

    def test_more_passes_when_the_profile_allows_them(self) -> None:
        bound = ("--short-max-passes", "2")
        pulp_pid = self.owner()
        self.long(pid=pulp_pid, extra=bound)
        self.assertEqual(self.short("v1", extra=bound)["short_first"], "boosted")
        self.withdraw("v1")
        self.assertEqual(self.short("v2", extra=bound)["short_first"], "boosted")
        self.withdraw("v2")
        third = self.short("v3", extra=bound)
        self.assertEqual(third["decision"], "defer", third)
        self.assertEqual(self.intents()["pulp-gate"]["short_passes"], 2)

    def test_no_pass_once_the_lane_was_first_passed_long_enough_ago(self) -> None:
        bound = ("--short-max-passes", "5", "--short-max-pass-secs", "600")
        pulp_pid = self.owner()
        self.long(pid=pulp_pid, extra=bound)
        self.assertEqual(self.short("v1", extra=bound)["short_first"], "boosted")
        self.withdraw("v1")
        # Ten minutes after the first pass the lane may not be passed again.
        rows, _ = slot_priority.load_intents(self.store)
        for row in rows:
            if row["lane"] == "pulp-gate":
                row["short_passed_since"] = ago(601)
        slot_priority.write_intents(self.store, rows)
        late = self.short("v2", extra=bound)
        self.assertEqual(late["decision"], "defer", late)

    def test_a_new_wait_starts_a_new_count(self) -> None:
        self.long(pid=self.owner())
        self.short("v1")
        self.withdraw("v1")
        # The passed lane got its slot and a new supervisor process asks again.
        self.long(pid=self.owner())
        self.assertNotIn("short_passes", self.intents()["pulp-gate"])
        self.assertEqual(self.short("v2")["short_first"], "boosted")

    def test_profile_bound_is_read_from_the_leases_table(self) -> None:
        self.profile.write_text('schema = 1\n[host]\nid = "test"\n[leases]\n'
                                'slot_priority = "on"\nshort_job_first_max_passes = 3\n'
                                'short_job_first_max_pass_secs = 900\n', encoding="utf-8")
        import host_profile
        settings = host_profile.lease_policy_settings(str(self.profile))
        self.assertEqual(settings["short_job_first_max_passes"], 3)
        self.assertEqual(settings["short_job_first_max_pass_secs"], 900)
        # The 3.9 fallback parser reads the integers too.
        table = host_profile._parse_leases_table(self.profile.read_text())
        self.assertEqual(table.get("short_job_first_max_passes"), 3)
        bare = Path(self.tmp.name) / "bare.toml"
        bare.write_text("[host]\n")
        settings = host_profile.lease_policy_settings(str(bare))
        self.assertEqual(settings["short_job_first_max_passes"], 1)
        self.assertEqual(settings["short_job_first_max_pass_secs"], 600)


class NoShortJobTests(ShortFirstBase):
    def test_no_short_job_queued_leaves_the_order_unchanged(self) -> None:
        self.long()
        result = self.short(short_ago=None)
        self.assertEqual(result["decision"], "defer", result)
        self.assertEqual(result["winner"]["lane"], "pulp-gate")
        self.assertNotIn("short_first", result)
        self.assertNotIn("short_passes", self.intents()["pulp-gate"])

    def test_the_on_view_equals_the_plain_order_without_a_short_job(self) -> None:
        now = 1_000_000.0
        rows = [
            {"lane": "a", "event_class": "merge_group", "queued_at": "2026-11-09T00:05:00Z"},
            {"lane": "b", "event_class": "pull_request", "queued_at": "2026-11-09T00:00:00Z"},
            {"lane": "c", "event_class": "merge_group", "queued_at": "2026-11-09T00:01:00Z",
             "short_job_first": "on"},
            # Passed before, now with no short job anywhere: still plain order.
            {"lane": "d", "event_class": "merge_group", "queued_at": "2026-11-09T00:03:00Z",
             "short_passes": 1},
        ]
        bound = (1, 600)
        suspended = slot_priority.suspended_ranks(rows, 0, now, bound)

        def order(view: str) -> list:
            return [r["lane"] for r in sorted(rows, key=lambda r: slot_priority.rank_key(
                r, 0, now, view, bound, suspended))]

        self.assertEqual(order(slot_priority.ON_VIEW), order(slot_priority.PLAIN_VIEW))
        self.assertEqual(order(slot_priority.PLAIN_VIEW), ["c", "d", "a", "b"])

    def test_the_switch_defaults_off_without_the_variable(self) -> None:
        env = os.environ.pop("TARTCI_SHORT_JOB_FIRST", None)
        try:
            self.assertEqual(slot_priority.short_mode_setting(), "off")
            os.environ["TARTCI_SHORT_JOB_FIRST"] = "shadow"
            self.assertEqual(slot_priority.short_mode_setting(), "shadow")
            os.environ["TARTCI_SHORT_JOB_FIRST"] = "bogus"
            self.assertEqual(slot_priority.short_mode_setting(), "off")
        finally:
            os.environ.pop("TARTCI_SHORT_JOB_FIRST", None)
            if env is not None:
                os.environ["TARTCI_SHORT_JOB_FIRST"] = env


class ShadowTests(ShortFirstBase):
    def test_shadow_keeps_the_order_and_reports_from_the_short_lane(self) -> None:
        pulp_pid = self.owner()
        self.long(pid=pulp_pid)
        result = self.short(mode="shadow")
        # Order unchanged: the older long job still wins under slot priority on.
        self.assertEqual(result["decision"], "defer", result)
        self.assertEqual(result["winner"]["lane"], "pulp-gate")
        self.assertEqual(result["short_first"], "would_boot")
        self.assertEqual(result["short_passed"], ["pulp-gate"])
        self.assertIn("short_first=would_boot", slot_priority.line(result))
        # No pass is recorded, so shadow changes nothing later either.
        self.assertNotIn("short_passes", self.intents()["pulp-gate"])
        # The long lane does not report it: each would-be boot is counted once.
        pulp = self.long(pid=pulp_pid, free=1)
        self.assertEqual(pulp["decision"], "claim", pulp)
        self.assertNotIn("short_first", pulp)

    def test_shadow_is_silent_when_the_short_lane_wins_anyway(self) -> None:
        self.long(queued_ago=10)
        result = self.short(mode="shadow", queued_ago=600)
        self.assertEqual(result["decision"], "claim", result)
        self.assertNotIn("short_first", result)

    def test_the_bash_glue_logs_would_boot_short_first_once(self) -> None:
        line = ("defer mode=on reason=outranked class=merge_group rank=0 "
                "queued_at=2026-11-16T00:00:00Z free=1 ahead=1 short_first=would_boot "
                "short_job=sterile-consumer short_job_id=7 short_run_id=9 "
                "short_queued_at=2026-11-16T00:01:00Z short_passed=pulp-gate")
        script = (
            "set -euo pipefail\n"
            f"source {LIB}\n"
            'event(){ printf "%s|%s\\n" "$1" "$2"; }\n'
            'tartci_slot_priority_short_event "$1" labels\n'
            'tartci_slot_priority_short_event "${1/would_boot/boosted}" labels\n'
            'tartci_slot_priority_short_event "claim mode=on reason=ranked_first" labels\n'
        )
        proc = subprocess.run([BASH32, "-c", script, "x", line], text=True,
                              capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = proc.stdout.splitlines()
        self.assertEqual(len(rows), 2, rows)
        self.assertTrue(rows[0].startswith("short_job_first_shadow|would-boot-short-first: "
                                           "short job sterile-consumer"), rows[0])
        self.assertIn("ahead of pulp-gate", rows[0])
        self.assertTrue(rows[1].startswith("short_job_first_granted|"), rows[1])


    def test_the_outcome_names_the_job_the_vm_ran(self) -> None:
        line = ("defer mode=on reason=outranked class=merge_group rank=0 free=1 ahead=1 "
                "short_first=would_boot short_job=sterile-consumer short_job_id=7 "
                "short_run_id=9 short_queued_at=x short_passed=pulp-gate")
        script = (
            "set -euo pipefail\n"
            f"source {LIB}\n"
            'event(){ [ "$1" = short_job_first_outcome ] && printf "%s\\n" "$*"; return 0; }\n'
            # The boot after the event ran the short job.
            'tartci_slot_priority_short_event "$1" labels\n'
            'tartci_slot_priority_short_outcome 9 7\n'
            # Logged once: the loop's call after run_one is then a no-op.
            'tartci_slot_priority_short_outcome "" "" no_job\n'
            # The boot ran a long job that shares the label.
            'tartci_slot_priority_short_event "$1" labels\n'
            'tartci_slot_priority_short_outcome 8 3\n'
            # The boot ran no job at all.
            'tartci_slot_priority_short_event "$1" labels\n'
            'tartci_slot_priority_short_outcome "" "" no_job\n'
            # With no short-job event pending nothing is logged.
            'tartci_slot_priority_short_outcome 8 3\n'
        )
        proc = subprocess.run([BASH32, "-c", script, "x", line], text=True,
                              capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = proc.stdout.splitlines()
        self.assertEqual(len(rows), 3, rows)
        self.assertIn("served_short=yes", rows[0])
        self.assertIn("ran_job_id=7", rows[0])
        self.assertIn("short_first=would_boot", rows[0])
        self.assertIn("served_short=no ", rows[1] + " ")
        self.assertIn("ran_job_id=3", rows[1])
        self.assertIn("ran_run_id=8", rows[1])
        self.assertIn("served_short=no_job", rows[2])

    def test_the_runner_reports_the_outcome_after_assignment_and_after_each_boot(self) -> None:
        body = (ROOT / "providers/tart-macos/runner.sh").read_text()
        assigned = body.index('event job_assigned "$(grep')
        self.assertIn('tartci_slot_priority_short_outcome "${CURRENT_RUN_ID:-}" '
                      '"${CURRENT_JOB_ID:-}"', body[assigned:assigned + 400])
        boot = body.index('run_one "$i" "$selected_labels" "$selected_tier" || run_rc=$?')
        self.assertIn('tartci_slot_priority_short_outcome "" "" no_job',
                      body[boot:boot + 300])


class DetailTests(unittest.TestCase):
    def test_parse_short_jobs(self) -> None:
        self.assertEqual(slot_demand.parse_short_jobs(STERILE + "\n|lint\n\nbare\n|\n"),
                         [("Locked macOS GPU proof", "sterile-consumer"), ("", "lint"),
                          ("", "bare")])
        self.assertEqual(slot_demand.parse_short_jobs(None), [])

    def test_the_detail_counts_declared_short_jobs_per_class(self) -> None:
        detail = slot_demand.DemandDetail(short_jobs=slot_demand.parse_short_jobs(STERILE))
        gpu = {"id": 1, "event": "merge_group", "name": "Locked macOS GPU proof"}
        other = {"id": 2, "event": "merge_group", "name": "Some other workflow"}
        detail.add(gpu, {"id": 10, "name": "gpu-macos-arm64", "created_at": ago(1800)})
        detail.add(gpu, {"id": 11, "name": "sterile-consumer", "created_at": ago(120)})
        detail.add(gpu, {"id": 12, "name": "sterile-consumer", "created_at": ago(60)})
        # The same job name in another workflow is not the declared job.
        detail.add(other, {"id": 13, "name": "sterile-consumer", "created_at": ago(900)})
        data = json.loads(json.dumps(detail.as_dict()))
        row = data["classes"]["merge_group"]
        self.assertEqual(row["count"], 4)
        self.assertEqual(row["short"]["count"], 2)
        self.assertEqual(row["short"]["job_id"], 11)
        self.assertEqual(row["short"]["run_id"], 1)
        self.assertEqual(row["short"]["name"], "sterile-consumer")
        self.assertEqual(data["schema"], slot_demand.SCHEMA)
        short = slot_priority.short_from_detail(data, "merge_group")
        self.assertEqual(short["job_id"], 11)
        self.assertIsNone(slot_priority.short_from_detail(data, "pull_request"))
        # Without declared short jobs there is no row: an old file means none.
        plain = slot_demand.DemandDetail()
        plain.add(gpu, {"id": 11, "name": "sterile-consumer", "created_at": ago(120)})
        self.assertNotIn("short", plain.as_dict()["classes"]["merge_group"])
        self.assertIsNone(slot_priority.short_from_detail(plain.as_dict(), "merge_group"))


class DecideFromDetailTests(ShortFirstBase):
    def test_decide_reads_the_short_job_from_the_lanes_detail_file(self) -> None:
        self.long()
        detail = slot_demand.DemandDetail(short_jobs=slot_demand.parse_short_jobs(STERILE))
        run = {"id": 5, "event": "merge_group", "name": "Locked macOS GPU proof"}
        detail.add(run, {"id": 50, "name": "gpu-macos-arm64", "created_at": ago(300)})
        detail.add(run, {"id": 51, "name": "sterile-consumer", "created_at": ago(60)})
        path = self.root / "detail.json"
        detail.write(str(path))
        argv = [sys.executable, str(SLOT), "decide", "--store-dir", str(self.store),
                "--lane", "vellum-gate", "--repo", VELLUM, "--free-slots", "1",
                "--cores", "4", "--priority", "vm", "--pid", str(self.owner()),
                "--demand-file", str(path), "--capacity", "24",
                "--reserved-gate-cores", "12", "--capacity-mem-mb", "0", "--mode", "on",
                "--json"]
        env = {**self.env, "TARTCI_SHORT_JOB_FIRST": "on"}
        proc = subprocess.run(argv, text=True, capture_output=True, check=False, env=env)
        result = json.loads(proc.stdout)
        self.assertEqual(result["decision"], "claim", result)
        self.assertEqual(result["short_first"], "boosted")
        self.assertEqual(result["self"]["short_job"],
                         {"name": "sterile-consumer", "job_id": 51, "run_id": 5})
        line = slot_priority.line(result)
        self.assertIn("short_job_id=51", line)
        self.assertIn("short_run_id=5", line)


FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from urllib.parse import parse_qs, urlparse
path = sys.argv[-1]
with open(os.environ["FAKE_GH_LEDGER"], "a", encoding="utf-8") as ledger:
    ledger.write(path + "\n")
if path == "rate_limit":
    print(json.dumps({"resources": {"core": {"limit": 15000, "remaining": 14999}}}))
    raise SystemExit(0)
p = urlparse("https://x/" + path); q = parse_qs(p.query)
runs = [{"id": 2, "name": "Locked macOS GPU proof", "event": "merge_group", "status": "queued",
         "created_at": "2026-11-09T23:16:00Z", "updated_at": "2026-11-09T23:16:00Z",
         "head_repository": {"full_name": "o/r"}}]
labels = os.environ["FAKE_GH_LABELS"].split(",")
if p.path.endswith("/actions/workflows"):
    print(json.dumps({"total_count": 1, "workflows": [{"id": 99, "name": "Locked macOS GPU proof"}]}))
elif "/runs" in p.path and not p.path.endswith("/jobs"):
    rows = runs if q.get("status", [""])[0] == "queued" else []
    print(json.dumps({"total_count": len(rows), "workflow_runs": rows}))
elif p.path.endswith("/jobs"):
    print(json.dumps({"total_count": 2, "jobs": [
        {"id": 201, "name": "gpu-macos-arm64", "status": "queued", "labels": labels,
         "created_at": "2026-11-09T23:16:05Z"},
        {"id": 202, "name": "sterile-consumer", "status": "queued", "labels": labels,
         "created_at": "2026-11-09T23:40:05Z"}]}))
else:
    raise SystemExit("unexpected API path: " + path)
'''


class QueueScanTests(unittest.TestCase):
    def test_queue_scan_counts_short_jobs_with_no_extra_call(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_exec(root / "fake-gh", FAKE_GH)
            ledger = root / "ledger"
            labels = "self-hosted,macOS,ARM64,vellum-build-macos-merge-group"

            def scan(name: str, short_jobs: str) -> tuple:
                ledger.write_text("")
                state = root / name
                (state / "home").mkdir(parents=True)
                env = {**os.environ, "HOME": str(state / "home"),
                       "FAKE_GH_LEDGER": str(ledger), "FAKE_GH_LABELS": labels,
                       "TARTCI_QUEUE_STAGGER_MAX_SECS": "0",
                       "TARTCI_SHORT_JOBS": short_jobs}
                proc = subprocess.run(
                    [sys.executable, str(QUEUE_SCAN), "--repo", "o/r",
                     "--workflow", "Locked macOS GPU proof", "--labels", labels,
                     "--provider", "tart-macos", "--lane-id", "t",
                     "--state-file", str(state / "queue-scan.json"),
                     "--shared-cache-file", str(state / "shared.json"),
                     "--observation-lock-file", str(state / "obs.lock"),
                     "--gh-cli", str(root / "fake-gh"), "--match-labels", "1",
                     "--force-refresh", "--detail-file", str(state / "detail.json")],
                    text=True, capture_output=True, check=False, env=env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return (proc.stdout.strip(), sorted(ledger.read_text().splitlines()),
                        json.loads((state / "detail.json").read_text()))

            plain_count, plain_calls, plain = scan("plain", "")
            count, calls, data = scan("short", STERILE)
            self.assertEqual(count, plain_count)
            self.assertEqual(count, "2")
            self.assertEqual(calls, plain_calls)
            self.assertNotIn("short", plain["classes"]["merge_group"])
            short = data["classes"]["merge_group"]["short"]
            self.assertEqual(short["count"], 1)
            self.assertEqual(short["job_id"], 202)
            self.assertEqual(short["oldest_created_at"], "2026-11-09T23:40:05Z")


@testing_support.requires_tomllib
class ProfileTests(unittest.TestCase):
    PROFILES = {
        "m1": ROOT / "profiles" / "m1-macos-fleet.toml",
        "studio": ROOT / "profiles" / "m3-macos-fleet.toml",
        "m5": ROOT / "profiles" / "m5-macos-fleet.toml",
        "m5studio": ROOT / "profiles" / "m5studio-macos-fleet.toml",
    }
    SHORT_LINE = ('short_jobs = [{ workflow = "Locked macOS GPU proof", '
                  'job = "sterile-consumer" }]\n')

    def load(self, body: str) -> dict:
        import macos_fleet_lanes as fleet
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m3.toml"
            path.write_text(body)
            return fleet.load(path)

    def m3(self) -> str:
        return self.PROFILES["studio"].read_text()

    def test_only_the_vellum_gate_lanes_render_the_short_jobs(self) -> None:
        import macos_fleet_lanes as fleet
        for host, path in self.PROFILES.items():
            data = fleet.load(path)
            seen = 0
            for body in fleet.rendered_plists(data).values():
                env = plistlib.loads(body)["EnvironmentVariables"]
                lane = env.get("TARTCI_QUEUE_LANE_ID", "")
                if env.get("TARTCI_RUNNER_REPO") == VELLUM and "-vellum-gate" in lane:
                    seen += 1
                    self.assertEqual(env["TARTCI_SHORT_JOBS"], STERILE, host)
                    self.assertIn(env["TARTCI_SHORT_JOB_FIRST"], ("shadow", "on"), host)
                else:
                    self.assertNotIn("TARTCI_SHORT_JOBS", env, (host, lane))
                    self.assertNotIn("TARTCI_SHORT_JOB_FIRST", env, (host, lane))
            self.assertGreaterEqual(seen, 1, host)

    def test_the_switch_defaults_to_shadow(self) -> None:
        import macos_fleet_lanes as fleet
        body = self.m3().replace('short_job_first = "on"\n', "")
        data = self.load(body)
        lane = next(row for row in data["lane"] if row["id"] == "vellum-gate")
        env = fleet.lane_plist(data, lane)["EnvironmentVariables"]
        self.assertEqual(env["TARTCI_SHORT_JOB_FIRST"], "shadow")

    def test_the_validator_rejects_bad_short_job_config(self) -> None:
        body = self.m3()
        self.assertIn(self.SHORT_LINE, body)
        # The switch line, if the profile carries one, so a case can add its own.
        base = body.replace('short_job_first = "on"\n', "")
        cases = {
            "short_job_first needs short_jobs": base.replace(
                self.SHORT_LINE, 'short_job_first = "on"\n', 1),
            "short_job_first must be": base.replace(
                self.SHORT_LINE, self.SHORT_LINE + 'short_job_first = "maybe"\n', 1),
            "nonempty list": body.replace(self.SHORT_LINE, "short_jobs = []\n", 1),
            "a table with job": body.replace(
                self.SHORT_LINE, 'short_jobs = [{ workflow = "x" }]\n', 1),
            "without \\| or a newline": body.replace(
                self.SHORT_LINE, 'short_jobs = [{ job = "a|b" }]\n', 1),
            "twice": body.replace(
                self.SHORT_LINE,
                'short_jobs = [{ job = "sterile-consumer" }, { job = "sterile-consumer" }]\n',
                1),
            "slot_priority shadow or on": body.replace(
                'slot_priority = "on"', 'slot_priority = "off"', 1),
            "max_passes must be": body.replace(
                "[leases]\n", "[leases]\nshort_job_first_max_passes = 0\n", 1),
            "max_pass_secs must be": body.replace(
                "[leases]\n", "[leases]\nshort_job_first_max_pass_secs = 5\n", 1),
        }
        for message, mutated in cases.items():
            self.assertNotEqual(mutated, body, message)
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.load(mutated)
        # Control: the shipped profile and a plain job name both load.
        self.load(body)
        self.load(body.replace(self.SHORT_LINE, 'short_jobs = [{ job = "sterile-consumer" }]\n'))


if __name__ == "__main__":
    unittest.main()
