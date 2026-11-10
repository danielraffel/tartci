#!/usr/bin/env python3
"""Slot priority: who takes the next free macOS VM slot on a host.

Properties covered, each from the side that could break:

* priority order: a merge_group job of any repository takes a free slot ahead
  of a pull_request job, and `other` events sit between them;
* starvation-freedom across repositories: within a class the longest-waiting
  job wins whatever repository it is from and whoever asked first, and a job
  of a lower class that has waited past the promotion bound ranks as
  merge-group;
* work conservation: a lane whose VM lease would not be admitted now holds
  nothing back, and neither do dead or stale intents;
* non-preemption: deciding never touches a lease, and with every slot taken it
  only records the intent;
* modes: off writes nothing, shadow always claims and names the lane that
  would have won, on defers;
* release first: with `slot_priority_release_first`, a lane about to serve a
  queued tagged release outranks merge-group and pull_request, only on
  evidence, and with no release queued the order is unchanged;
* the scanners' detail files carry the event class and age with no extra API
  call;
* the bash glue runs under macOS /bin/bash 3.2 with `set -euo pipefail`.

Compatible with the hosts' Python 3.9.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SLOT = SCRIPTS / "slot_priority.py"
LIB = ROOT / "providers/tart-macos/slot-priority.lib.sh"
RUNNER = ROOT / "providers/tart-macos/runner.sh"
QUEUE_SCAN = SCRIPTS / "queue_scan.py"
ASSIGNMENT_SCAN = SCRIPTS / "assignment_scan.py"
sys.path.insert(0, str(SCRIPTS))

import slot_demand  # noqa: E402
import slot_priority  # noqa: E402

BASH32 = "/bin/bash"


def ago(seconds: int) -> str:
    return slot_demand.iso(
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    )


class Owner:
    """A live process to own an intent, so liveness is real, not mocked."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(["sleep", "120"])

    def stop(self) -> None:
        self.proc.kill()
        self.proc.wait()


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = self.root / "leases"
        self.profile = self.root / "fleet.toml"
        self.profile.write_text('schema = 1\n[host]\nid = "test"\n', encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("TARTCI_SLOT_PRIORITY")
                    and k not in ("TARTCI_RANK_VM_WAITERS", "TARTCI_VM_WAITER_FRESH_SECS")}
        self.env["TARTCI_FLEET_PROFILE"] = str(self.profile)
        self.env["TARTCI_LEASE_DIR"] = str(self.store)
        self.owners: list = []

    def tearDown(self) -> None:
        for owner in self.owners:
            owner.stop()
        self.tmp.cleanup()

    def owner(self) -> int:
        owner = Owner()
        self.owners.append(owner)
        return owner.proc.pid

    def decide(self, lane: str, *, free: int = 1, event_class: str = "pull_request",
               queued_ago: int | None = None, priority: str = "100", cores: int = 4,
               mode: str = "on", pid: int | None = None, repo: str = "o/r",
               capacity: int = 24, reserved: int = 12, extra: tuple = ()) -> dict:
        argv = [sys.executable, str(SLOT), "decide", "--store-dir", str(self.store),
                "--lane", lane, "--repo", repo, "--free-slots", str(free),
                "--cores", str(cores), "--priority", priority,
                "--pid", str(pid if pid is not None else self.owner()),
                "--event-class", event_class,
                "--capacity", str(capacity), "--reserved-gate-cores", str(reserved),
                "--capacity-mem-mb", "0", "--json", *extra]
        if mode:
            argv += ["--mode", mode]
        if queued_ago is not None:
            argv += ["--queued-at", ago(queued_ago)]
        proc = subprocess.run(argv, text=True, capture_output=True, check=False, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def write_leases(self, records: list) -> bytes:
        self.store.mkdir(parents=True, exist_ok=True)
        body = (json.dumps(records, indent=2, sort_keys=True) + "\n").encode()
        (self.store / "leases.json").write_bytes(body)
        return body


class PriorityOrderTests(Base):
    def test_merge_group_of_another_repo_outranks_an_older_pull_request(self) -> None:
        # Vellum's merge-group job asked first and is younger; Pulp's PR-head
        # job has waited longer. The class decides, not the age or the repo.
        self.decide("vellum-gate", event_class="merge_group", queued_ago=60,
                    priority="vm", repo="Generous-Corp/vellum", free=0)
        pulp = self.decide("pulp-gate", event_class="pull_request", queued_ago=1800,
                           repo="Generous-Corp/pulp")
        self.assertEqual(pulp["decision"], "defer")
        self.assertEqual(pulp["winner"]["lane"], "vellum-gate")
        vellum = self.decide("vellum-gate", event_class="merge_group", queued_ago=60,
                             priority="vm", repo="Generous-Corp/vellum")
        self.assertEqual(vellum["decision"], "claim")

    def test_other_events_sit_between_merge_group_and_pull_request(self) -> None:
        self.assertLess(slot_priority.CLASS_RANK["merge_group"], slot_priority.CLASS_RANK["other"])
        self.assertLess(slot_priority.CLASS_RANK["other"], slot_priority.CLASS_RANK["pull_request"])
        self.decide("release", event_class="other", queued_ago=10, free=0)
        pr = self.decide("pr", event_class="pull_request", queued_ago=900)
        self.assertEqual(pr["decision"], "defer")
        mg = self.decide("mg", event_class="merge_group", queued_ago=5)
        self.assertEqual(mg["decision"], "claim")

    def test_two_free_slots_admit_the_top_two(self) -> None:
        self.decide("a", event_class="merge_group", queued_ago=100, free=0)
        self.decide("b", event_class="merge_group", queued_ago=50, free=0)
        third = self.decide("c", event_class="pull_request", queued_ago=999, free=2)
        self.assertEqual(third["decision"], "defer")
        second = self.decide("b", event_class="merge_group", queued_ago=50, free=2)
        self.assertEqual(second["decision"], "claim")
        self.assertEqual(len(second["ahead"]), 1)

    def test_unknown_evidence_never_jumps_ahead(self) -> None:
        self.decide("known-pr", event_class="pull_request", queued_ago=30, free=0)
        unknown = self.decide("unknown", event_class="unknown", free=1)
        self.assertEqual(unknown["decision"], "defer")


RELEASE_LABELS = "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm,pulp-release-tagged"
GATE_MG_LABELS = "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm,pulp-build-merge-group"
GATE_PR_LABELS = "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm,pulp-build-pr-head"


class ReleaseFirstTests(Base):
    """A queued tagged release takes the next free slot ahead of gate work.

    A release run's jobs come from a tag `push`, so their scan class is
    `other`; the release lane is recognised by the class label its
    registration carries, which only a release job can match.
    """

    def queue_gates(self, release_first: str) -> None:
        # Older gate work, so age alone would put both ahead of the release.
        flag = ("--release-first", release_first)
        self.decide("pulp-gate-mg", event_class="merge_group", queued_ago=1800, free=0,
                    priority="110", extra=("--labels", GATE_MG_LABELS, *flag))
        self.decide("pulp-gate-pr", event_class="pull_request", queued_ago=2400, free=0,
                    priority="100", extra=("--labels", GATE_PR_LABELS, *flag))

    def release(self, release_first: str, *, free: int = 1,
                event_class: str = "other") -> dict:
        return self.decide("pulp-release-slot", event_class=event_class, queued_ago=30,
                           free=free, priority="120",
                           extra=("--labels", RELEASE_LABELS,
                                  "--release-first", release_first))

    def test_a_queued_release_outranks_merge_group_and_pull_request(self) -> None:
        self.queue_gates("1")
        rel = self.release("1")
        self.assertEqual(rel["decision"], "claim", rel)
        self.assertEqual(rel["reason"], "ranked_first")
        self.assertEqual(rel["self"]["event_class"], "release")
        self.assertEqual(rel["self"]["effective_rank"], -1)
        self.assertEqual(rel["ahead"], [])
        # The gate lanes, asking for the same free slot, yield to it.
        for lane, cls, labels in (("pulp-gate-mg", "merge_group", GATE_MG_LABELS),
                                  ("pulp-gate-pr", "pull_request", GATE_PR_LABELS)):
            with self.subTest(lane=lane):
                gate = self.decide(lane, event_class=cls, queued_ago=1800, priority="110",
                                   extra=("--labels", labels, "--release-first", "1"))
                self.assertEqual(gate["decision"], "defer", gate)
                self.assertEqual(gate["winner"]["lane"], "pulp-release-slot")
                self.assertEqual(gate["winner"]["event_class"], "release")

    def test_negative_control_without_the_rule_the_release_waits(self) -> None:
        # Same instrument, same lanes, rule off: the release ranks as `other`
        # and loses the slot to merge-group, which is the 2026-11-10 incident.
        self.queue_gates("0")
        rel = self.release("0")
        self.assertEqual(rel["self"]["event_class"], "other")
        self.assertEqual(rel["decision"], "defer", rel)
        self.assertEqual(rel["winner"]["lane"], "pulp-gate-mg")

    def test_a_promoted_old_pull_request_still_ranks_behind_a_release(self) -> None:
        self.decide("old-pr", event_class="pull_request", queued_ago=9000, free=0,
                    extra=("--labels", GATE_PR_LABELS, "--release-first", "1",
                           "--promote-after-secs", "2700"))
        rel = self.decide("pulp-release-slot", event_class="other", queued_ago=10,
                          priority="120",
                          extra=("--labels", RELEASE_LABELS, "--release-first", "1",
                                 "--promote-after-secs", "2700"))
        self.assertEqual(rel["decision"], "claim", rel)
        self.assertLess(slot_priority.CLASS_RANK["release"], 0)

    def test_with_no_release_queued_ordering_is_unchanged(self) -> None:
        # Release-first on, no release lane: merge-group > other > pull_request,
        # exactly the order the rule-off path gives, round for round.
        def order(release_first: str) -> list:
            self.store_reset()
            flag = ("--release-first", release_first)
            self.decide("pr", event_class="pull_request", queued_ago=900, free=0,
                        extra=("--labels", GATE_PR_LABELS, *flag))
            self.decide("push", event_class="other", queued_ago=600, free=0,
                        extra=("--labels", "self-hosted,x", *flag))
            self.decide("mg", event_class="merge_group", queued_ago=60, free=0,
                        extra=("--labels", GATE_MG_LABELS, *flag))
            out = self.decide("probe", event_class="pull_request", queued_ago=1, free=3,
                              extra=("--labels", GATE_PR_LABELS, *flag))
            return [(row["lane"], row["event_class"], row["effective_rank"])
                    for row in out["ahead"]]

        on, off = order("1"), order("0")
        self.assertEqual([lane for lane, _, _ in on], ["mg", "push", "pr"])
        self.assertEqual(on, off)

    def test_a_release_lane_moves_ahead_only_on_evidence(self) -> None:
        # Unknown or stale detail on the release tier stays `unknown`.
        self.decide("pulp-gate-pr", event_class="pull_request", queued_ago=60, free=0,
                    extra=("--labels", GATE_PR_LABELS, "--release-first", "1"))
        rel = self.release("1", event_class="unknown")
        self.assertEqual(rel["self"]["event_class"], "unknown")
        self.assertEqual(rel["decision"], "defer")

    def test_release_class_needs_the_class_label_not_a_substring(self) -> None:
        self.assertTrue(slot_priority.carries_release_label(RELEASE_LABELS))
        self.assertFalse(slot_priority.carries_release_label(GATE_MG_LABELS))
        self.assertFalse(slot_priority.carries_release_label("self-hosted,pulp-release-tagged-x"))
        self.assertFalse(slot_priority.carries_release_label("self-hosted,pulp-build-vm-release"))
        # Release-first off maps an explicit `release` back to `other`.
        self.assertEqual(slot_priority.ranked_class("release", RELEASE_LABELS, False), "other")
        self.assertEqual(slot_priority.ranked_class("merge_group", GATE_MG_LABELS, True),
                         "merge_group")
        # An explicit `release` without the class label is not believed.
        self.assertEqual(slot_priority.ranked_class("release", GATE_MG_LABELS, True), "other")

    def test_profile_knob_default_off_env_override_and_validator(self) -> None:
        import host_profile
        self.assertFalse(host_profile.lease_policy_settings(str(self.profile))
                         ["slot_priority_release_first"])
        self.profile.write_text('schema = 1\n[host]\nid = "test"\n[leases]\n'
                                'slot_priority = "on"\nslot_priority_release_first = true\n',
                                encoding="utf-8")
        self.assertTrue(host_profile.lease_policy_settings(str(self.profile))
                        ["slot_priority_release_first"])
        # The 3.9 fallback parser reads the boolean too.
        table = host_profile._parse_leases_table(self.profile.read_text())
        self.assertIs(table.get("slot_priority_release_first"), True)
        # With the profile on, a release lane ranks first through the profile.
        self.queue_gates_from_profile()
        rel = self.decide("pulp-release-slot", event_class="other", queued_ago=30,
                          priority="120", extra=("--labels", RELEASE_LABELS))
        self.assertEqual(rel["self"]["event_class"], "release")
        old = os.environ.get("TARTCI_SLOT_PRIORITY_RELEASE_FIRST")
        os.environ["TARTCI_SLOT_PRIORITY_RELEASE_FIRST"] = "0"
        try:
            self.assertFalse(slot_priority.release_first_setting())
        finally:
            if old is None:
                os.environ.pop("TARTCI_SLOT_PRIORITY_RELEASE_FIRST", None)
            else:
                os.environ["TARTCI_SLOT_PRIORITY_RELEASE_FIRST"] = old

    def queue_gates_from_profile(self) -> None:
        self.decide("pulp-gate-mg", event_class="merge_group", queued_ago=1800, free=0,
                    priority="110", extra=("--labels", GATE_MG_LABELS))

    def store_reset(self) -> None:
        path = slot_priority.intents_file(self.store)
        if path.exists():
            path.unlink()


class StarvationTests(Base):
    def test_older_job_wins_within_a_class_whatever_the_repo_or_arrival(self) -> None:
        # Pulp's lane asks first; Vellum's merge-group job is older.
        self.decide("pulp-gate", event_class="merge_group", queued_ago=120,
                    repo="Generous-Corp/pulp", free=0)
        self.decide("vellum-gate", event_class="merge_group", queued_ago=1800,
                    repo="Generous-Corp/vellum", priority="vm", free=0)
        pulp = self.decide("pulp-gate", event_class="merge_group", queued_ago=120,
                           repo="Generous-Corp/pulp")
        self.assertEqual(pulp["decision"], "defer")
        self.assertEqual(pulp["winner"]["repo"], "Generous-Corp/vellum")

    def test_a_stream_of_fresh_jobs_cannot_pass_one_waiting_job(self) -> None:
        # Repo A sees a new merge-group job each round (always 30 s old) and
        # asks first every time. Repo B's one job keeps ageing. B wins every
        # round it is present, so A cannot starve it.
        for round_index in range(4):
            with self.subTest(round=round_index):
                self.decide("b-lane", event_class="merge_group",
                            queued_ago=600 + 60 * round_index, repo="b/b", free=0)
                a = self.decide("a-lane", event_class="merge_group", queued_ago=30,
                                repo="a/a")
                self.assertEqual(a["decision"], "defer")
                self.assertEqual(a["winner"]["lane"], "b-lane")

    def test_a_pull_request_past_the_promotion_bound_ranks_as_merge_group(self) -> None:
        self.decide("old-pr", event_class="pull_request", queued_ago=3000, free=0)
        fresh_mg = self.decide("fresh-mg", event_class="merge_group", queued_ago=10,
                               extra=("--promote-after-secs", "2700"))
        self.assertEqual(fresh_mg["decision"], "defer")
        self.assertEqual(fresh_mg["winner"]["lane"], "old-pr")
        self.assertEqual(fresh_mg["winner"]["effective_rank"], 0)
        # Below the bound the merge-group job wins.
        self.decide("young-pr", event_class="pull_request", queued_ago=600, free=0)
        mg = self.decide("mg2", event_class="merge_group", queued_ago=10,
                         extra=("--promote-after-secs", "2700"))
        ahead = [row["lane"] for row in mg["ahead"]]
        self.assertEqual(ahead[0], "old-pr")
        self.assertNotIn("young-pr", ahead)

    def test_rank_key_is_a_strict_total_order(self) -> None:
        now = 1_000_000.0
        rows = [{"lane": name, "event_class": "merge_group", "queued_at": "2026-11-09T00:00:00Z",
                 "waiting_since": "2026-11-09T00:01:00Z"} for name in ("b", "a", "c")]
        keys = [slot_priority.rank_key(row, 2700, now) for row in rows]
        self.assertEqual(len(set(keys)), 3)
        self.assertEqual([k[-1] for k in sorted(keys)], ["a", "b", "c"])


class AgeFloorTests(Base):
    def test_a_long_refused_lane_with_a_young_witness_still_ages(self) -> None:
        # The lane's scan saw only a fresh PR job (a witness), but the lane has
        # wanted a slot without a break for an hour: it is promoted.
        pid = self.owner()
        self.decide("starved-pr", event_class="pull_request", queued_ago=30, free=0, pid=pid)
        intents = json.loads((self.store / "slot-intents.json").read_text())
        for row in intents:
            row["waiting_since"] = ago(3600)
        (self.store / "slot-intents.json").write_text(json.dumps(intents))
        mg = self.decide("fresh-mg", event_class="merge_group", queued_ago=10,
                         extra=("--promote-after-secs", "2700"))
        self.assertEqual(mg["decision"], "defer")
        self.assertEqual(mg["winner"]["lane"], "starved-pr")


class LeaseWaiterInterplayTests(Base):
    def knob(self) -> None:
        self.profile.write_text('schema = 1\n[host]\nid = "test"\n[leases]\n'
                                'rank_vm_waiters = true\n', encoding="utf-8")

    def waiter(self, lane: str, priority: str, cores: int) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "leases.py"), "wait", "--store-dir", str(self.store),
             "--id", f"waiter-{lane}", "--cores", str(cores), "--mem-mb", "0",
             "--priority", priority, "--kind", "tart-macos-vm", "--pid", str(self.owner()),
             "--lane", lane, "--label", "x", "--capacity", "24", "--reserved-gate-cores", "12",
             "--capacity-mem-mb", "0", "--json"],
            text=True, capture_output=True, check=False, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(json.loads(proc.stdout).get("registered"), proc.stdout)

    def test_an_intent_the_lease_store_would_defer_holds_nothing_back(self) -> None:
        self.knob()
        # 24 cores, 18 in use: a gate lane (110) waits for 6. A vm-class
        # merge-group intent wanting 6 fits, but its acquire would be deferred
        # to that waiter, so it cannot boot and must not hold the slot back.
        self.write_leases([{"id": "busy", "lease_size_cores": 18, "priority": 100,
                            "lease_size_mem_mb": 0, "command_kind": "tart-macos-vm",
                            "pid": os.getpid()}])
        self.waiter("gate-lane", "110", 6)
        self.decide("vellum-gate", event_class="merge_group", queued_ago=900, priority="vm",
                    cores=6, free=0, reserved=0)
        me = self.decide("pulp-pr", event_class="pull_request", queued_ago=10, priority="100",
                         cores=6, reserved=0)
        self.assertEqual(me["decision"], "claim", me)
        self.assertEqual(me["ahead"], [])

    def test_the_deciding_lanes_own_waiter_does_not_count(self) -> None:
        self.knob()
        self.write_leases([{"id": "busy", "lease_size_cores": 18, "priority": 100,
                            "lease_size_mem_mb": 0, "command_kind": "tart-macos-vm",
                            "pid": os.getpid()}])
        # My own waiter (I will withdraw it when I defer) must not shield the
        # higher-ranked intent.
        self.waiter("pulp-pr", "110", 6)
        self.decide("vellum-gate", event_class="merge_group", queued_ago=900, priority="vm",
                    cores=6, free=0, reserved=0)
        me = self.decide("pulp-pr", event_class="pull_request", queued_ago=10, priority="110",
                         cores=6, reserved=0)
        self.assertEqual(me["decision"], "defer", me)


class LockTimeoutTests(Base):
    def test_a_held_store_lock_claims_first_come(self) -> None:
        self.store.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl,sys,time\n"
             "fh=open(sys.argv[1],'a+')\n"
             "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
             "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
             "time.sleep(30)\n", str(self.store / "leases.lock")],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            result = self.decide("lane", event_class="merge_group",
                                 extra=("--lock-timeout-secs", "0.5"))
            self.assertEqual(result["decision"], "claim")
            self.assertEqual(result["reason"], "store_lock_timeout")
        finally:
            holder.kill()
            holder.wait()


class WorkConservationTests(Base):
    def test_a_lane_whose_lease_would_be_denied_holds_nothing_back(self) -> None:
        # The non-gate budget (24 - 12 = 12) is full, so a non-gate Vellum
        # lease cannot be admitted: its merge-group rank must not idle a slot.
        self.write_leases([{"id": "agent", "lease_size_cores": 12, "priority": 40,
                            "lease_size_mem_mb": 0, "command_kind": "pulp-governed-build",
                            "pid": os.getpid()}])
        self.decide("vellum-gate", event_class="merge_group", queued_ago=900,
                    priority="vm", free=0)
        pulp = self.decide("pulp-gate", event_class="pull_request", queued_ago=60,
                           priority="100")
        self.assertEqual(pulp["decision"], "claim")
        self.assertEqual(pulp["ahead"], [])

    def test_dead_and_stale_intents_rank_nobody(self) -> None:
        dead = Owner()
        dead_pid = dead.proc.pid
        self.decide("dead-lane", event_class="merge_group", queued_ago=900, free=0,
                    pid=dead_pid)
        dead.stop()
        stale_pid = self.owner()
        self.decide("stale-lane", event_class="merge_group", queued_ago=900, free=0,
                    pid=stale_pid)
        intents = json.loads((self.store / "slot-intents.json").read_text())
        for row in intents:
            if row["lane"] == "stale-lane":
                row["seen_at"] = ago(3600)
        (self.store / "slot-intents.json").write_text(json.dumps(intents))
        me = self.decide("me", event_class="pull_request", queued_ago=10)
        self.assertEqual(me["decision"], "claim")
        live = {row["lane"] for row in json.loads((self.store / "slot-intents.json").read_text())}
        self.assertEqual(live, {"me"})

    def test_a_refresh_keeps_the_original_waiting_since(self) -> None:
        pid = self.owner()
        first = self.decide("lane", event_class="unknown", free=0, pid=pid)
        again = self.decide("lane", event_class="unknown", free=0, pid=pid)
        self.assertEqual(first["self"]["waiting_since"], again["self"]["waiting_since"])

    def test_withdraw_removes_only_that_lane(self) -> None:
        self.decide("a", event_class="merge_group", free=0)
        self.decide("b", event_class="merge_group", free=0)
        proc = subprocess.run([sys.executable, str(SLOT), "withdraw", "--store-dir",
                               str(self.store), "--lane", "a", "--json"],
                              text=True, capture_output=True, check=True, env=self.env)
        self.assertEqual(json.loads(proc.stdout)["withdrawn"], "a")
        lanes = {row["lane"] for row in json.loads((self.store / "slot-intents.json").read_text())}
        self.assertEqual(lanes, {"b"})


class NonPreemptionTests(Base):
    def test_deciding_never_changes_a_lease(self) -> None:
        running = [
            {"id": "pulp-vm-1", "lease_size_cores": 6, "priority": 100, "lease_size_mem_mb": 0,
             "command_kind": "tart-macos-vm", "pid": os.getpid()},
            {"id": "pulp-vm-2", "lease_size_cores": 6, "priority": 100, "lease_size_mem_mb": 0,
             "command_kind": "tart-macos-vm", "pid": os.getpid()},
        ]
        before = self.write_leases(running)
        # Both slots are held by PR-head work and a merge-group job waits: the
        # answer is only `full`; nothing running is touched.
        mg = self.decide("vellum-gate", event_class="merge_group", queued_ago=1800,
                         priority="vm", free=0)
        self.assertEqual(mg["decision"], "full")
        self.decide("pulp-gate", event_class="pull_request", queued_ago=10, free=0)
        self.assertEqual((self.store / "leases.json").read_bytes(), before)

    def test_the_module_writes_no_lease_reservation_or_vm(self) -> None:
        import re
        source = SLOT.read_text(encoding="utf-8")
        for forbidden in (r"write_records", r"\btart\s", r"\bkill\(", r"resv\.",
                          r"taskpolicy", r"release\("):
            self.assertIsNone(re.search(forbidden, source), forbidden)


class ModeTests(Base):
    def test_off_answers_claim_and_writes_nothing(self) -> None:
        result = self.decide("lane", event_class="pull_request", mode="off")
        self.assertEqual(result["decision"], "claim")
        self.assertFalse((self.store / "slot-intents.json").exists())

    def test_off_is_the_profile_default(self) -> None:
        result = self.decide("lane", event_class="pull_request", mode="")
        self.assertEqual(result["mode"], "off")

    def test_shadow_claims_but_names_the_lane_that_would_have_won(self) -> None:
        self.decide("vellum-gate", event_class="merge_group", queued_ago=300,
                    priority="vm", free=0, mode="shadow", repo="Generous-Corp/vellum")
        pulp = self.decide("pulp-gate", event_class="pull_request", queued_ago=60,
                           mode="shadow")
        self.assertEqual(pulp["decision"], "claim")
        self.assertEqual(pulp["reason"], "shadow_would_defer")
        self.assertEqual(pulp["shadow_winner"]["lane"], "vellum-gate")
        line = slot_priority.line(pulp)
        self.assertTrue(line.startswith("claim "), line)
        self.assertIn("winner_lane=vellum-gate", line)

    def test_profile_mode_and_validator(self) -> None:
        self.profile.write_text('schema = 1\n[host]\nid = "test"\n[leases]\n'
                                'slot_priority = "shadow"\n', encoding="utf-8")
        result = self.decide("lane", event_class="pull_request", mode="")
        self.assertEqual(result["mode"], "shadow")
        import host_profile
        os.environ["TARTCI_FLEET_PROFILE"] = str(self.profile)
        try:
            # The 3.9 fallback parser (no tomllib) must read the quoted mode too.
            table = host_profile._parse_leases_table(self.profile.read_text())
            self.assertEqual(table.get("slot_priority"), "shadow")
        finally:
            os.environ.pop("TARTCI_FLEET_PROFILE", None)

    def test_fallback_parser_reads_a_quoted_mode_without_tomllib(self) -> None:
        import host_profile
        code = (
            "import sys, builtins\n"
            "real = builtins.__import__\n"
            "def fake(name, *a, **k):\n"
            "    if name == 'tomllib': raise ImportError('no tomllib')\n"
            "    return real(name, *a, **k)\n"
            "builtins.__import__ = fake\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import host_profile\n"
            "print(host_profile._parse_leases_table("
            "'[leases]\\nslot_priority = \"on\"\\nslot_priority_promote_after_secs = 900\\n'))\n"
        )
        out = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True,
                             check=True).stdout
        self.assertIn("'slot_priority': 'on'", out)
        self.assertIn("'slot_priority_promote_after_secs': 900", out)
        self.assertTrue(hasattr(host_profile, "SLOT_PRIORITY_MODES"))


class ProfileValidatorTests(unittest.TestCase):
    """The fleet validator accepts the three modes and refuses anything else."""

    def test_validator(self) -> None:
        import re
        import testing_support
        if not testing_support.HAVE_TOMLLIB:
            self.skipTest(testing_support.REASON)
        import macos_fleet_lanes as fleet
        body = (ROOT / "profiles/m5-macos-fleet.toml").read_text()
        # The shipped profile carries slot_priority; drop it so each case sets its own.
        stripped = re.sub(r'(?m)^slot_priority = "on"\n', "", body, count=1)
        self.assertNotEqual(stripped, body, "control: the shipped key was found")
        body = stripped
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m5.toml"
            for extra, pattern in (('slot_priority = "shadow"', None),
                                   ('slot_priority = "on"\nslot_priority_promote_after_secs = 900', None),
                                   ('slot_priority = "yes"', "slot_priority must be"),
                                   ("slot_priority = true", "slot_priority must be"),
                                   ('slot_priority = "on"\nslot_priority_promote_after_secs = 5',
                                    "from 300 to 14400")):
                mutated = re.sub(r"(?m)^rank_vm_waiters = (\w+)$",
                                 lambda m: m.group(0) + "\n" + extra, body, count=1)
                self.assertNotEqual(mutated, body, "control: the leases table was found")
                path.write_text(mutated)
                if pattern is None:
                    self.assertEqual(fleet.load(path)["leases"]["slot_priority"],
                                     extra.split('"')[1])
                else:
                    with self.assertRaisesRegex(ValueError, pattern):
                        fleet.load(path)

    def test_the_four_host_profiles_turn_it_on_and_no_other_does(self) -> None:
        hosts = {"m1", "m3", "m5", "m5studio"}
        seen = set()
        for path in (ROOT / "profiles").glob("*.toml"):
            has = 'slot_priority = "on"' in path.read_text()
            host = path.name.replace("-macos-fleet.toml", "")
            self.assertEqual(has, host in hosts, path.name)
            if has:
                seen.add(host)
        self.assertEqual(seen, hosts, "control: all four host profiles were read")


    def test_only_the_m5studio_canary_turns_release_first_on(self) -> None:
        hosts = {"m5studio"}
        seen = set()
        for path in (ROOT / "profiles").glob("*.toml"):
            has = "slot_priority_release_first = true" in path.read_text()
            host = path.name.replace("-macos-fleet.toml", "")
            self.assertEqual(has, host in hosts, path.name)
            if has:
                seen.add(host)
        self.assertEqual(seen, hosts, "control: the canary profile was read")
        self.assertEqual(len(list((ROOT / "profiles").glob("*-macos-fleet.toml"))), 4,
                         "control: all four host profiles were read")

    def test_every_canary_pulp_gate_slot_consults_tagged_releases_first(self) -> None:
        import testing_support
        if not testing_support.HAVE_TOMLLIB:
            self.skipTest(testing_support.REASON)
        import macos_fleet_lanes as fleet
        slots = 0
        for host in ("m5studio",):
            data = fleet.load(ROOT / "profiles" / f"{host}-macos-fleet.toml")
            lane = next(l for l in data["lane"] if l["id"] == "pulp-gate")
            self.assertTrue(data["leases"]["slot_priority_release_first"], host)
            orders = lane["assignment_slot_tier_order"]
            self.assertEqual(sorted(orders), [str(n) for n in range(1, lane["supervisors"] + 1)],
                             host)
            for slot, order in orders.items():
                self.assertEqual(order[0], "pulp-release-tagged", f"{host} slot {slot}")
                slots += 1
        self.assertEqual(slots, 2, "control: both canary slots were read")

    def test_validator_refuses_a_non_boolean_release_first(self) -> None:
        import re
        import testing_support
        if not testing_support.HAVE_TOMLLIB:
            self.skipTest(testing_support.REASON)
        import macos_fleet_lanes as fleet
        body = (ROOT / "profiles/m5studio-macos-fleet.toml").read_text()
        mutated, n = re.subn(r"(?m)^slot_priority_release_first = true$",
                             'slot_priority_release_first = "yes"', body)
        self.assertEqual(n, 1, "control: the shipped key was found")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m5studio.toml"
            path.write_text(mutated)
            with self.assertRaisesRegex(ValueError, "slot_priority_release_first must be"):
                fleet.load(path)

class DemandDetailTests(unittest.TestCase):
    def test_a_lane_ranks_by_the_job_it_will_likely_get_the_oldest(self) -> None:
        # GitHub hands a new runner whichever matching job it picks, likeliest
        # the oldest; a fresh merge-group job must not win the slot for an old
        # PR job on the same labels.
        detail = slot_demand.DemandDetail()
        detail.add({"event": "pull_request"}, {"created_at": ago(3000)})
        detail.add({"event": "merge_group"}, {"created_at": ago(100)})
        detail.add({"event": "merge_group"}, {"created_at": ago(400)})
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        cls, queued = slot_priority.demand_from_detail(detail.as_dict(), 0, now)
        self.assertEqual(cls, "pull_request")
        self.assertAlmostEqual(slot_demand.parse_ts(queued).timestamp(), now - 3000, delta=5)
        only_mg = slot_demand.DemandDetail()
        only_mg.add({"event": "merge_group"}, {"created_at": ago(100)})
        only_mg.add({"event": "merge_group"}, {"created_at": ago(400)})
        cls, queued = slot_priority.demand_from_detail(only_mg.as_dict(), 0, now)
        self.assertEqual(cls, "merge_group")
        self.assertAlmostEqual(slot_demand.parse_ts(queued).timestamp(), now - 400, delta=5)

    def test_stale_or_missing_detail_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "d.json"
            self.assertIsNone(slot_demand.read_detail(str(path), 900))
            detail = slot_demand.DemandDetail()
            detail.add({"event": "merge_group"}, {"created_at": ago(10)})
            detail.write(str(path))
            self.assertIsNotNone(slot_demand.read_detail(str(path), 900))
            data = json.loads(path.read_text())
            data["written_at"] = ago(2000)
            path.write_text(json.dumps(data))
            self.assertIsNone(slot_demand.read_detail(str(path), 900))

    def test_event_classes(self) -> None:
        self.assertEqual(slot_demand.event_class("merge_group"), "merge_group")
        self.assertEqual(slot_demand.event_class("pull_request_target"), "pull_request")
        self.assertEqual(slot_demand.event_class("push"), "other")
        self.assertEqual(slot_demand.event_class(None), "other")


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
runs = [
    {"id": 1, "name": "Build and Test", "event": "pull_request", "status": "queued",
     "created_at": "2026-11-09T22:00:00Z", "updated_at": "2026-11-09T22:00:00Z",
     "head_repository": {"full_name": "o/r"}},
    {"id": 2, "name": "Build and Test", "event": "merge_group", "status": "queued",
     "created_at": "2026-11-09T23:16:00Z", "updated_at": "2026-11-09T23:16:00Z",
     "head_repository": {"full_name": "o/r"}},
]
labels = os.environ["FAKE_GH_LABELS"].split(",")
if p.path.endswith("/actions/workflows"):
    print(json.dumps({"total_count": 1, "workflows": [{"id": 99, "name": "Build and Test"}]}))
elif "/runs" in p.path and not p.path.endswith("/jobs"):
    status = q.get("status", [""])[0]
    rows = [r for r in runs if status == "queued"]
    print(json.dumps({"total_count": len(rows), "workflow_runs": rows}))
elif p.path.endswith("/jobs"):
    run_id = int(p.path.split("/actions/runs/", 1)[1].split("/", 1)[0])
    created = {1: "2026-11-09T22:00:05Z", 2: "2026-11-09T23:16:05Z"}[run_id]
    print(json.dumps({"total_count": 1, "jobs": [
        {"id": 100 + run_id, "status": "queued", "labels": labels, "created_at": created}]}))
else:
    raise SystemExit("unexpected API path: " + path)
'''


class ScannerDetailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write_exec(self.root / "fake-gh", FAKE_GH)
        self.ledger = self.root / "ledger"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def env(self, labels: str, home: Path) -> dict:
        home.mkdir(parents=True, exist_ok=True)
        return {**os.environ, "HOME": str(home), "FAKE_GH_LEDGER": str(self.ledger),
                "FAKE_GH_LABELS": labels, "TARTCI_QUEUE_STAGGER_MAX_SECS": "0"}

    def queue_scan(self, *extra: str) -> tuple:
        self.ledger.write_text("")
        labels = "self-hosted,macOS,ARM64,vellum-build-macos"
        # A fresh home and state per call, so no cache makes the calls differ.
        state = self.root / f"qstate{len(extra)}"
        proc = subprocess.run(
            [sys.executable, str(QUEUE_SCAN), "--repo", "o/r", "--workflow", "Build and Test",
             "--labels", labels, "--provider", "tart-macos", "--lane-id", "t",
             "--state-file", str(state / "queue-scan.json"),
             "--shared-cache-file", str(state / f"shared{len(extra)}.json"),
             "--observation-lock-file", str(state / "obs.lock"),
             "--gh-cli", str(self.root / "fake-gh"), "--match-labels", "1",
             "--force-refresh", *extra],
            text=True, capture_output=True, check=False, env=self.env(labels, state / "home"))
        return proc, self.ledger.read_text().splitlines()

    def test_queue_scan_detail_costs_no_extra_call(self) -> None:
        detail_path = self.root / "detail.json"
        plain, plain_calls = self.queue_scan()
        with_detail, detail_calls = self.queue_scan("--detail-file", str(detail_path))
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(with_detail.returncode, 0, with_detail.stderr)
        self.assertEqual(plain.stdout.strip(), with_detail.stdout.strip())
        self.assertEqual(plain.stdout.strip(), "2")
        self.assertEqual(sorted(plain_calls), sorted(detail_calls))
        data = json.loads(detail_path.read_text())
        self.assertEqual(data["classes"]["merge_group"]["count"], 1)
        self.assertEqual(data["classes"]["merge_group"]["oldest_created_at"],
                         "2026-11-09T23:16:05Z")
        self.assertEqual(data["classes"]["pull_request"]["oldest_created_at"],
                         "2026-11-09T22:00:05Z")
        self.assertFalse(data["witness_only"])

    def assignment_scan(self, *extra: str) -> tuple:
        self.ledger.write_text("")
        labels = "self-hosted,macOS,ARM64,pulp-build-vm,pulp-build-merge-group"
        state = self.root / f"astate{len(extra)}"
        state.mkdir(exist_ok=True)
        proc = subprocess.run(
            [sys.executable, str(ASSIGNMENT_SCAN), "--repo", "o/r",
             "--workflow", "Build and Test", "--labels", labels,
             "--require-label", "pulp-build-merge-group",
             "--class-labels", "pulp-build-merge-group,pulp-build-pr-head",
             "--gh-cli", str(self.root / "fake-gh"), "--max-workers", "1",
             "--observation-lock-file", str(state / "obs.lock"),
             "--workflow-id-cache-file", str(state / "ids.json"),
             "--job-cache-ttl", "0", *extra],
            text=True, capture_output=True, check=False, env=self.env(labels, state / "home"))
        return proc, self.ledger.read_text().splitlines()

    def test_assignment_scan_detail_records_the_witness_with_no_extra_call(self) -> None:
        detail_path = self.root / "a-detail.json"
        plain, plain_calls = self.assignment_scan()
        with_detail, detail_calls = self.assignment_scan("--detail-file", str(detail_path))
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(with_detail.returncode, 0, with_detail.stderr)
        self.assertEqual(plain.stdout.strip(), with_detail.stdout.strip())
        self.assertEqual(sorted(plain_calls), sorted(detail_calls))
        data = json.loads(detail_path.read_text())
        self.assertTrue(data["witness_only"])
        self.assertEqual(sum(row["count"] for row in data["classes"].values()), 1)


class Bash32GateTests(Base):
    """The lane glue under macOS /bin/bash 3.2, `set -euo pipefail`, empty arrays."""

    def gate(self, mode: str, *, cap: int = 2, running: int = 1, preset: str = "",
             max_defer: str = "300", rounds: int = 1, detail: dict | None = None,
             tail: str = "", labels: str = "self-hosted,x", tier: int = 0,
             detail_key: str = "lane") -> tuple:
        if not Path(BASH32).exists():
            self.skipTest("no /bin/bash")
        state = self.root / "state"
        state.mkdir(exist_ok=True)
        if detail is not None:
            (state / f"lane-r.slot-demand.{detail_key}.json").write_text(json.dumps(detail))
        events = self.root / "events.log"
        script = f"""
set -euo pipefail
TARTCI_ROOT={str(ROOT)!r}
STATE_DIR={str(state)!r}
RUNNER_NAME=lane-r
SLOT=1
REPO=o/r
WORKFLOW_TIERS=""
TARTCI_QUEUE_LANE_ID=me-lane
event(){{ printf '%s|%s\\n' "$1" "${{2:-}}" >> {str(events)!r}; }}
tartci_profile_value(){{ echo off; }}
tartci_count_reservations(){{ echo 0; }}
tartci_vm_lease_priority(){{ echo 100; }}
tartci_vm_lease_cores(){{ echo 1; }}
tartci_vm_lease_granted_cores(){{ echo "$1"; }}
tartci_vm_lease_mem_mb(){{ return 1; }}
tartci_positive_int_or_empty(){{ case "${{1:-}}" in ''|*[!0-9]*) return 1;; esac; [ "$1" -gt 0 ]; }}
tartci_claim_delay_class(){{ return 1; }}
source {str(LIB)!r}
{preset}
rc=0
for _ in $(seq 1 {rounds}); do
  rc=0
  tartci_slot_priority_gate {cap} {running} {labels!r} {tier} || rc=$?
done
{tail}
echo "rc=$rc"
"""
        env = {**self.env, "TARTCI_SLOT_PRIORITY_MAX_DEFER_SECS": max_defer}
        if mode:
            env["TARTCI_SLOT_PRIORITY"] = mode
        proc = subprocess.run([BASH32, "-c", script], text=True, capture_output=True,
                              check=False, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = events.read_text() if events.exists() else ""
        return proc.stdout.strip().splitlines()[-1], log

    def outrank(self) -> None:
        # A gate-class merge-group intent that fits (cores 1 on any host).
        self.decide("winner-lane", event_class="merge_group", queued_ago=900, cores=1,
                    free=0, priority="110", capacity=0, extra=())

    def decide(self, lane: str, **kw):  # type: ignore[override]
        # Real host capacity for the bash path; only a one-core gate lease.
        kw.setdefault("capacity", 0)
        capacity = kw.pop("capacity")
        argv_extra = kw.pop("extra", ())
        if capacity:
            return super().decide(lane, capacity=capacity, extra=argv_extra, **kw)
        pid = self.owner()
        argv = [sys.executable, str(SLOT), "decide", "--store-dir", str(self.store),
                "--lane", lane, "--free-slots", str(kw.get("free", 1)),
                "--cores", str(kw.get("cores", 1)), "--priority", kw.get("priority", "110"),
                "--pid", str(pid), "--event-class", kw.get("event_class", "merge_group"),
                "--queued-at", ago(kw.get("queued_ago", 0) or 0), "--mode", "on", "--json"]
        proc = subprocess.run(argv, text=True, capture_output=True, check=True, env=self.env)
        return json.loads(proc.stdout)

    def test_off_returns_at_once_and_records_nothing(self) -> None:
        self.outrank()
        rc, log = self.gate("off")
        self.assertEqual(rc, "rc=0")
        self.assertEqual(log, "")
        lanes = {row["lane"] for row in json.loads((self.store / "slot-intents.json").read_text())}
        self.assertNotIn("me-lane", lanes)

    def test_mode_is_read_once_even_when_callers_run_in_subshells(self) -> None:
        counter = self.root / "profile-reads"
        rc, _ = self.gate("", preset=(
            f"tartci_profile_value(){{ echo x >> {str(counter)!r}; echo shadow; }}\n"
            "tartci_slot_priority_load\n"
            "for _ in 1 2 3; do x=\"$(tartci_slot_priority_enabled && echo y)\"; done\n"
            "[ \"$x\" = y ]"), rounds=3)
        self.assertEqual(rc, "rc=0")
        self.assertEqual(counter.read_text().count("x"), 1)

    def test_lease_wait_keeps_the_intent_fresh_and_never_fit_withdraws_it(self) -> None:
        self.outrank()
        intents = self.store / "slot-intents.json"
        rc, _ = self.gate("on", running=2, preset="", tail=(
            "LEASE_FIT_LAST_VERDICT=not_now; tartci_slot_priority_lease_wait"))
        lanes = {row["lane"] for row in json.loads(intents.read_text())}
        self.assertIn("me-lane", lanes)
        rc, _ = self.gate("on", running=2, preset="", tail=(
            "LEASE_FIT_LAST_VERDICT=never; tartci_slot_priority_lease_wait"))
        lanes = {row["lane"] for row in json.loads(intents.read_text())}
        self.assertNotIn("me-lane", lanes)

    def test_a_lease_wait_never_creates_an_intent_after_a_withdraw(self) -> None:
        self.outrank()
        intents = self.store / "slot-intents.json"
        # Registered, then withdrawn (no demand), then a lease wait.
        self.gate("on", running=2, tail=(
            "tartci_slot_priority_withdraw; "
            "LEASE_FIT_LAST_VERDICT=not_now; tartci_slot_priority_lease_wait"))
        lanes = {row["lane"] for row in json.loads(intents.read_text())}
        self.assertNotIn("me-lane", lanes)

    def test_a_non_numeric_defer_bound_falls_back_to_300(self) -> None:
        self.outrank()
        rc, log = self.gate("on", max_defer="soon",
                            preset="SLOT_PRIORITY_DEFER_SINCE=$(( $(date +%s) - 400 ))")
        self.assertEqual(rc, "rc=0")
        self.assertIn("slot_priority_defer_expired|", log)

    def test_on_defers_to_a_higher_ranked_lane(self) -> None:
        self.outrank()
        rc, log = self.gate("on")
        self.assertEqual(rc, "rc=75")
        self.assertIn("slot_priority_deferred|", log)
        self.assertIn("winner_lane=winner-lane", log)

    def test_shadow_claims_and_logs_who_would_have_won(self) -> None:
        self.outrank()
        rc, log = self.gate("shadow")
        self.assertEqual(rc, "rc=0")
        self.assertIn("slot_priority_shadow|would have given slot to winner-lane", log)

    def test_a_merge_group_detail_wins_the_slot_in_bash(self) -> None:
        self.outrank()
        detail = slot_demand.DemandDetail()
        detail.add({"event": "merge_group"}, {"created_at": ago(5000)})
        rc, log = self.gate("on", detail=detail.as_dict())
        self.assertEqual(rc, "rc=0")
        self.assertNotIn("slot_priority_deferred", log)

    def test_a_release_tier_lane_wins_the_slot_from_merge_group_in_bash(self) -> None:
        # The lane's scan saw a tag-push job (class `other`) on its release
        # registration; a merge-group intent that fits is already waiting.
        self.outrank()
        detail = slot_demand.DemandDetail()
        detail.add({"event": "push"}, {"created_at": ago(30)})
        env_rf = "TARTCI_SLOT_PRIORITY_RELEASE_FIRST"
        for flag, expected in (("1", "rc=0"), ("0", "rc=75")):
            with self.subTest(release_first=flag):
                self.env[env_rf] = flag
                rc, log = self.gate("on", detail=detail.as_dict(),
                                    labels=RELEASE_LABELS)
                self.assertEqual(rc, expected, log)
        self.env.pop(env_rf, None)

    def test_a_v2_release_tier_reads_its_class_keyed_detail_in_bash(self) -> None:
        # Production event-class-v2 path: the lane has tiers, the gate is
        # given the CONFIGURED index of pulp-release-tagged (2 in the
        # deduplicated class config), and the real claim-delay lib maps it to
        # the class label that names the detail file the scan wrote.
        self.outrank()
        detail = slot_demand.DemandDetail(witness_only=True)
        detail.add({"event": "push"}, {"created_at": ago(30)})
        tiers = ("source " + repr(str(ROOT / "providers/tart-macos/claim-delay.lib.sh")) + "\n"
                 "WORKFLOW_TIERS=configured\n"
                 "TIER_LABELS_CONFIG=$'pulp-build-merge-group\\npulp-build-pr-head\\n"
                 "pulp-release-tagged\\npulp-release-pr-gate'")
        self.env["TARTCI_SLOT_PRIORITY_RELEASE_FIRST"] = "1"
        try:
            rc, log = self.gate("on", detail=detail.as_dict(), labels=RELEASE_LABELS,
                                tier=2, detail_key="pulp-release-tagged", preset=tiers)
            self.assertEqual(rc, "rc=0", log)
            self.assertNotIn("slot_priority_deferred", log)
            # Control, same instrument: the detail under the wrong key (the
            # single-label `lane` file) is not read, so the lane is `unknown`
            # and defers to the merge-group intent.
            for path in (self.root / "state").glob("*.slot-demand.*.json"):
                path.unlink()
            rc, log = self.gate("on", detail=detail.as_dict(), labels=RELEASE_LABELS,
                                tier=2, detail_key="lane", preset=tiers)
            self.assertEqual(rc, "rc=75", log)
            self.assertIn("class=unknown", log)
        finally:
            self.env.pop("TARTCI_SLOT_PRIORITY_RELEASE_FIRST", None)

    def test_full_host_only_refreshes_the_intent(self) -> None:
        self.outrank()
        rc, log = self.gate("on", running=2)
        self.assertEqual(rc, "rc=0")
        self.assertEqual(log, "")
        lanes = {row["lane"] for row in json.loads((self.store / "slot-intents.json").read_text())}
        self.assertIn("me-lane", lanes)

    def test_deferral_is_bounded(self) -> None:
        self.outrank()
        rc, log = self.gate("on", preset="SLOT_PRIORITY_DEFER_SINCE=$(( $(date +%s) - 400 ))")
        self.assertEqual(rc, "rc=0")
        self.assertIn("slot_priority_defer_expired|", log)

    def test_helper_failure_claims_first_come(self) -> None:
        self.outrank()
        rc, _ = self.gate("on", preset="TARTCI_ROOT=/nonexistent")
        self.assertEqual(rc, "rc=0")

    def test_runner_sources_the_lib_and_gates_the_claim(self) -> None:
        body = RUNNER.read_text(encoding="utf-8")
        self.assertIn('source "$TARTCI_ROOT/providers/tart-macos/slot-priority.lib.sh"', body)
        claim = body.index('resv="$(tartci_warm_or_claim_slot "$cap" "$r")"')
        gate = body.index('tartci_slot_priority_gate "$cap" "$r"')
        self.assertLess(gate, claim)
        self.assertIn('[ "$sp" -eq 0 ] && resv="$(tartci_warm_or_claim_slot', body)


class RunnerQueueDetailTests(unittest.TestCase):
    """--print-queue through the real runner: detail only when enabled, same calls."""

    def run_queue(self, mode: str) -> tuple:
        directory = tempfile.mkdtemp()
        root = Path(directory)
        try:
            _write_exec(root / "tart", "#!/usr/bin/env bash\nexit 0\n")
            _write_exec(root / "fake-gh", FAKE_GH)
            ledger = root / "ledger"
            ledger.write_text("")
            labels = "self-hosted,macOS,ARM64,vellum-build-macos"
            search = [str(root)] + [p for p in ("/bin", "/usr/bin", "/opt/homebrew/bin",
                                                "/usr/local/bin") if Path(p).exists()]
            env = {"HOME": str(root), "PATH": os.pathsep.join(search),
                   "TART_HOME": str(root / "vms"), "TARTCI_STATE_DIR": str(root / "state"),
                   "TARTCI_GH_CLI": "fake-gh", "TARTCI_RUNNER_LABELS": labels,
                   "TARTCI_RUNNER_WORKFLOW_NAMES": "Build and Test",
                   "TARTCI_QUEUE_STAGGER_MAX_SECS": "0", "FAKE_GH_LEDGER": str(ledger),
                   "FAKE_GH_LABELS": labels, "TARTCI_SLOT_PRIORITY": mode,
                   "TARTCI_FLEET_PROFILE": str(root / "none.toml")}
            proc = subprocess.run([BASH32, str(RUNNER), "--print-queue"], text=True,
                                  capture_output=True, check=False, env=env)
            details = sorted(p.name for p in (root / "state").rglob("*.slot-demand.*.json"))
            return proc, details, len(ledger.read_text().splitlines())
        finally:
            subprocess.run(["rm", "-rf", directory], check=False)

    def test_detail_file_only_when_enabled(self) -> None:
        off, off_details, off_calls = self.run_queue("off")
        shadow, shadow_details, shadow_calls = self.run_queue("shadow")
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertEqual(shadow.returncode, 0, shadow.stderr)
        self.assertEqual(off.stdout.strip(), shadow.stdout.strip())
        self.assertEqual(off_details, [])
        self.assertEqual(len(shadow_details), 1, shadow_details)
        self.assertEqual(off_calls, shadow_calls)


if __name__ == "__main__":
    unittest.main()
