#!/usr/bin/env python3
"""The reuse canary runs Pulp's mac lane once per new main SHA a day, and says so.

Pins: every gate refuses before anything runs; the ledger runs a SHA once a day
but reruns a half-run up to its cap; the worktree never falls back to another
disk; the run is background class, bounded, and visibly alive; bindability
comes only from `shipyard reuse records`; one receipt and one terminal event per
pass; the doctor's stale / no-bindable bounds; the profile validator; and the
watchdog never heals the agent mid-run.

Run:  python3 scripts/test_reuse_canary.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_doctor  # noqa: E402
import reuse_canary as rc  # noqa: E402
import tartci_launchd_watchdog as watchdog  # noqa: E402

SHA = "a" * 40
SHA2 = "b" * 40
DAY = 86400.0


def records(sha: str = SHA, bindable: bool = True, target: str = "mac",
            mode: str = "shadow_compare", rows: Optional[list] = None) -> Dict[str, Any]:
    """`shipyard --json reuse records` output, in its confirmed schema."""
    row = {"sha": sha, "target": target, "run_id": "r1", "path": "/x",
           "filed_at": "2026-10-04T00:00:00Z", "bindable": bindable, "candidate": bindable}
    if not bindable:
        row["reason"] = "unusable: /toolchain/complete is false, not true"
    return {"schema_version": 1, "command": "reuse.records", "repository": "Generous-Corp/pulp",
            "target": "mac", "base_sha": SHA, "platform": "darwin-arm64",
            "changed_surface_execution_mode": mode, "bindable": int(bindable), "candidates": [],
            "records": [row] if rows is None else rows}


class FakeSystem(rc.System):
    def __init__(self) -> None:
        self.clock = 1_000_000.0
        self.day = "2026-10-04"
        self.calls: List[List[str]] = []
        self.lines: List[str] = []
        self.ls_remote: Tuple[int, str] = (0, f"{SHA}\trefs/heads/main\n")
        self.mounted = True
        self.run_result: Tuple[Optional[int], bool] = (0, False)
        self.records_result: Tuple[int, str] = (0, json.dumps(records()))
        self.mode_result: Tuple[int, str] = (0, json.dumps(records(rows=[])))
        self.records_cwds: List[Optional[str]] = []
        self.bounded: List[Dict[str, Any]] = []
        self.fail: Dict[str, Tuple[int, str]] = {}

    def now(self) -> float:
        return self.clock

    def today(self) -> str:
        return self.day

    def run(self, argv, cwd=None, timeout=120):
        argv = list(argv)
        self.calls.append(argv)
        for key, result in self.fail.items():
            if key in argv:
                return result
        if "ls-remote" in argv:
            return self.ls_remote
        if argv[:4] == ["shipyard", "--json", "reuse", "records"]:
            self.records_cwds.append(cwd)
            # The pre-run mode probe reads the primary checkout; after the run
            # the canary reads its worktree.
            return self.mode_result if cwd == "/repo" else self.records_result
        if "worktree" in argv and "add" in argv:
            path = Path(argv[argv.index("--detach") + 1])
            (path / "tools" / "scripts").mkdir(parents=True)
            (path / "tools" / "scripts" / "worktree_lineage.sh").write_text("")
            return 0, ""
        if "--git-common-dir" in argv:
            return 0, "/repo/.git\n"
        return 0, ""

    def run_bounded(self, argv, cwd, env, timeout, heartbeat):
        self.bounded.append({"argv": list(argv), "cwd": cwd, "env": env, "timeout": timeout})
        heartbeat(120.0)
        heartbeat(240.0)
        return self.run_result

    def is_mount(self, path):
        return self.mounted

    def emit(self, line):
        self.lines.append(line)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.worktrees = self.root / "worktrees"
        self.worktrees.mkdir()
        self.state = self.root / "state"
        self.participation = self.root / "participation"
        self.participation.write_text("1\n")
        self.sys = FakeSystem()

    def tearDown(self) -> None:
        self.td.cleanup()

    def canary(self, worktrees_root: Optional[str] = None) -> rc.Canary:
        settings = {"enabled": True, "repo": "/repo",
                    "worktrees_root": worktrees_root or str(self.worktrees)}
        return rc.Canary(settings, self.state, self.sys, self.participation, host="t")

    def events(self) -> List[Dict[str, Any]]:
        path = self.state / "events.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def receipt(self) -> Dict[str, Any]:
        return json.loads((self.state / "status.json").read_text())

    def terminal(self) -> List[str]:
        return [e["event"] for e in self.events() if e["event"] != "reuse_canary_started"]


class Gates(Case):
    def assert_refused(self, why: str, reads_head: bool = False) -> None:
        self.sys.calls.clear()
        (self.state / "events.jsonl").unlink(missing_ok=True)
        self.assertEqual(self.canary().run(), rc.EXIT_REFUSED)
        self.assertEqual(self.sys.bounded, [], "a refused pass must run nothing")
        self.assertFalse(any("worktree" in c or "fetch" in c for c in self.sys.calls))
        self.assertEqual(any("ls-remote" in c for c in self.sys.calls), reads_head)
        self.assertEqual(self.terminal(), ["reuse_canary_refused"])
        self.assertIn(why, self.receipt()["detail"])

    def test_participation_off_refuses(self):
        self.participation.write_text("0\n")
        self.assert_refused("participation")

    def test_draining_refuses(self):
        self.participation.write_text("draining\n")
        self.assert_refused("participation")

    def test_another_shipyard_mode_refuses(self):
        for mode in ("off", "authoritative"):
            with self.subTest(mode=mode):
                self.sys.mode_result = (0, json.dumps(records(mode=mode, rows=[])))
                self.assert_refused(f"'{mode}'", reads_head=True)

    def test_unreadable_shipyard_mode_refuses(self):
        for result in [(2, "trusted config invalid"), (1, "store"), (0, "not json"),
                       (0, json.dumps({"records": []}))]:
            with self.subTest(result=result):
                self.sys.mode_result = result
                self.assert_refused("cannot read", reads_head=True)

    def test_the_mode_is_read_from_the_primary_checkout_before_the_run(self):
        self.canary().run()
        self.assertEqual(self.sys.records_cwds[0], "/repo")
        self.assertEqual(self.sys.records_cwds[1], str(self.worktrees / rc.WORKTREE_NAME))

    def test_control_open_gates_run(self):
        self.assertEqual(self.canary().run(), rc.EXIT_OK)
        self.assertEqual(len(self.sys.bounded), 1)


class MainSha(Case):
    def test_unreadable_head_is_a_setup_failure(self):
        for result in [(0, "nothex\trefs/heads/main\n"), (0, ""), (128, "fatal"),
                       (0, f"{SHA[:39]}\trefs/heads/main\n")]:
            with self.subTest(result=result):
                self.sys.ls_remote = result
                self.assertEqual(self.canary().run(), rc.EXIT_SETUP)
        self.assertEqual(self.sys.bounded, [])


class Ledger(Case):
    def test_a_sha_runs_once_a_day(self):
        self.assertEqual(self.canary().run(), rc.EXIT_OK)
        self.assertEqual(self.canary().run(), rc.EXIT_OK)
        self.assertEqual(len(self.sys.bounded), 1)
        self.assertEqual(self.receipt()["outcome"], "not_due")

    def test_a_new_sha_runs(self):
        self.canary().run()
        self.sys.ls_remote = (0, f"{SHA2}\trefs/heads/main\n")
        self.sys.records_result = (0, json.dumps(records(SHA2)))
        self.canary().run()
        self.assertEqual(len(self.sys.bounded), 2)

    def test_a_new_day_runs_the_same_sha(self):
        self.canary().run()
        self.sys.day = "2026-10-05"
        self.canary().run()
        self.assertEqual(len(self.sys.bounded), 2)

    def test_a_failed_run_retries_once_within_the_cap(self):
        self.sys.run_result = (1, False)
        self.assertEqual(self.canary().run(), rc.EXIT_RUN_FAILED)
        self.sys.run_result = (0, False)
        self.assertEqual(self.canary().run(), rc.EXIT_OK)
        self.assertEqual(self.receipt()["outcome"], "ran")
        self.assertEqual(len(self.sys.bounded), 2)

    def test_ran_is_final_whatever_its_bindability(self):
        self.sys.records_result = (0, json.dumps(records(bindable=False)))
        self.canary().run()
        self.canary().run()
        self.assertEqual(len(self.sys.bounded), 1)
        self.assertEqual(self.receipt()["outcome"], "not_due")

    def test_two_starts_cap_whatever_the_outcomes(self):
        for first in [(1, False), (None, True)]:
            with self.subTest(first=first):
                self.sys.day = f"2026-10-{len(self.sys.bounded) + 10:02d}"
                before = len(self.sys.bounded)
                self.sys.run_result = first
                self.canary().run()
                self.sys.records_result = (1, "store")
                self.sys.run_result = (0, False)
                self.canary().run()
                self.sys.records_result = (0, json.dumps(records()))
                self.canary().run()
                self.assertEqual(len(self.sys.bounded) - before, rc.MAX_STARTS_PER_DAY)
                self.assertEqual(self.receipt()["outcome"], "not_due")

    def test_a_timed_out_run_retries_up_to_the_cap(self):
        self.sys.run_result = (None, True)
        for _ in range(rc.MAX_STARTS_PER_DAY + 2):
            self.canary().run()
        self.assertEqual(len(self.sys.bounded), rc.MAX_STARTS_PER_DAY)
        self.assertEqual(self.receipt()["outcome"], "not_due")
        self.assertIn("without a completed run", self.receipt()["detail"])

    def test_a_killed_pass_reads_as_a_half_run(self):
        # The pass died after marking its start: no finish, so the next pass runs.
        c = self.canary()
        ledger = c.load_ledger()
        c.mark(ledger, self.sys.day, SHA)
        self.canary().run()
        self.assertEqual(len(self.sys.bounded), 1)

    def test_old_days_are_pruned(self):
        c = self.canary()
        ledger = {f"2026-09-{d:02d}": {SHA: {"starts": [1]}} for d in range(1, 20)}
        c.mark(ledger, "2026-10-04", SHA)
        self.assertEqual(len(c.load_ledger()), rc.LEDGER_KEEP_DAYS)
        self.assertIn("2026-10-04", c.load_ledger())


class Worktree(Case):
    def test_created_once_then_checked_out(self):
        self.canary().run()
        self.sys.day = "2026-10-05"
        self.canary().run()
        adds = [c for c in self.sys.calls if "worktree" in c and "add" in c]
        checkouts = [c for c in self.sys.calls if "checkout" in c]
        self.assertEqual(len(adds), 1)
        self.assertEqual(len(checkouts), 1)
        self.assertIn(SHA, checkouts[0])
        self.assertEqual(self.sys.bounded[0]["cwd"], str(self.worktrees / rc.WORKTREE_NAME))

    def test_lineage_is_marked(self):
        self.canary().run()
        marks = [c for c in self.sys.calls if "mark" in c and "--status" in c]
        self.assertEqual(len(marks), 1)
        self.assertIn("tartci-reuse-canary", marks[0])

    def test_a_missing_root_refuses(self):
        self.assertEqual(self.canary(str(self.root / "absent")).run(), rc.EXIT_SETUP)
        self.assertEqual(self.sys.bounded, [])

    def test_an_unmounted_volume_never_falls_back(self):
        self.sys.mounted = False
        self.assertEqual(self.canary("/Volumes/Example/Code/agent-worktrees").run(),
                         rc.EXIT_SETUP)
        self.assertIn("not mounted", self.receipt()["detail"])
        self.assertFalse(any("worktree" in c for c in self.sys.calls))
        self.assertEqual(self.sys.bounded, [])

    def test_a_foreign_directory_is_refused(self):
        (self.worktrees / rc.WORKTREE_NAME).mkdir()
        self.sys.fail["--git-common-dir"] = (128, "not a git repository")
        self.assertEqual(self.canary().run(), rc.EXIT_SETUP)
        self.assertEqual(self.sys.bounded, [])

    def test_a_fetch_failure_is_a_setup_failure(self):
        self.sys.fail["fetch"] = (1, "network")
        self.assertEqual(self.canary().run(), rc.EXIT_SETUP)
        self.assertEqual(self.sys.bounded, [])


class Run(Case):
    def test_background_class_bounded_and_mac_only(self):
        self.canary().run()
        call = self.sys.bounded[0]
        self.assertEqual(call["argv"], ["shipyard", "run", "--targets", "mac"])
        self.assertEqual(call["env"]["PULP_BUILD_CLASS"], "background")
        self.assertEqual(call["env"]["PULP_WORKTREES_ROOT"], str(self.worktrees))
        self.assertEqual(call["timeout"], rc.RUN_TIMEOUT_S)

    def test_progress_lines_while_running(self):
        self.canary().run()
        beats = [x for x in self.sys.lines if x.startswith("reuse_canary progress")]
        self.assertEqual(len(beats), 2)
        self.assertIn("elapsed=240s", beats[1])

    def test_timeout_is_recorded_and_may_retry(self):
        self.sys.run_result = (None, True)
        self.assertEqual(self.canary().run(), rc.EXIT_RUN_FAILED)
        self.assertEqual(self.receipt()["outcome"], "timeout")
        ledger = self.canary().load_ledger()
        self.assertEqual(ledger[self.sys.day][SHA]["finishes"], ["timeout"])
        self.assertTrue(self.canary().due(ledger, self.sys.day, SHA)[0])

    def test_a_failed_run_still_records(self):
        self.sys.run_result = (2, False)
        self.sys.records_result = (0, json.dumps(records(bindable=False)))
        self.assertEqual(self.canary().run(), rc.EXIT_RUN_FAILED)
        self.assertIs(self.receipt()["bindable"], False)
        self.assertIsNotNone(self.receipt()["reuse_records"])


class Records(Case):
    def test_bindable_comes_from_the_records(self):
        self.canary().run()
        self.assertIs(self.receipt()["bindable"], True)
        self.assertEqual(self.receipt()["reuse_records"], records())

    def test_a_non_bindable_record_is_not_bindable(self):
        self.sys.records_result = (0, json.dumps(records(bindable=False)))
        self.assertEqual(self.canary().run(), rc.EXIT_OK)
        self.assertIs(self.receipt()["bindable"], False)

    def test_a_record_for_another_sha_or_target_does_not_count(self):
        for value in (records(SHA2), records(target="linux"), records(rows=[])):
            with self.subTest(value=value):
                self.sys.day = f"2026-10-{len(self.sys.bounded) + 5:02d}"
                self.sys.records_result = (0, json.dumps(value))
                self.canary().run()
                self.assertIs(self.receipt()["bindable"], False)

    def test_an_empty_list_after_a_pass_is_not_filed(self):
        self.sys.records_result = (0, json.dumps(records(rows=[])))
        self.assertEqual(self.canary().run(), rc.EXIT_OK)
        self.assertIs(self.receipt()["filed"], False)
        self.assertIs(self.receipt()["bindable"], False)
        self.sys.day = "2026-10-05"
        self.sys.records_result = (0, json.dumps(records(bindable=False)))
        self.canary().run()
        self.assertIs(self.receipt()["filed"], True)

    def test_unreadable_records_are_an_error_not_none(self):
        no_mode = records()
        no_mode.pop("changed_surface_execution_mode")
        for result in [(1, "boom"), (1, json.dumps(records())), (0, "not json"),
                       (0, json.dumps({"x": 1})), (0, json.dumps(no_mode))]:
            with self.subTest(result=result):
                self.sys.day = f"2026-10-{len(self.sys.bounded) + 5:02d}"
                self.sys.records_result = result
                self.assertEqual(self.canary().run(), rc.EXIT_RECORDS)
                self.assertEqual(self.receipt()["outcome"], "records_error")
                self.assertIsNone(self.receipt().get("bindable"))


class Receipts(Case):
    def test_one_terminal_event_and_receipt_per_pass(self):
        self.canary().run()
        self.canary().run()
        self.participation.write_text("0\n")
        self.canary().run()
        self.assertEqual(self.terminal(), ["reuse_canary_ran", "reuse_canary_not_due",
                                           "reuse_canary_refused"])
        self.assertEqual(len(list((self.state / "attempts").glob("*.json"))), 3)
        self.assertEqual([e["event"] for e in self.events()].count("reuse_canary_started"), 1)

    def test_no_temp_files_left(self):
        self.canary().run()
        self.assertEqual(list(self.state.rglob(".*.tmp")), [])


class Lock(Case):
    def test_a_held_lock_makes_the_pass_a_no_op(self):
        held = rc.acquire_lock(self.state / "lock")
        self.assertIsNotNone(held)
        self.assertIsNone(rc.acquire_lock(self.state / "lock"))
        held.close()
        again = rc.acquire_lock(self.state / "lock")
        self.assertIsNotNone(again)
        again.close()


class Settings(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(rc.validate_table({}), [])
        self.assertEqual(rc.validate_table({"enabled": False}), [])
        good = {"enabled": True, "repo": "/r", "worktrees_root": "/w"}
        self.assertEqual(rc.validate_table(good), [])
        self.assertTrue(rc.validate_table({"enabled": True, "repo": "/r"}))
        self.assertTrue(rc.validate_table(dict(good, enabled="yes")))
        self.assertTrue(rc.validate_table(dict(good, repo="relative")))
        self.assertTrue(rc.validate_table(dict(good, repo="/a/../b")))
        self.assertTrue(rc.validate_table(dict(good, extra=1)))
        self.assertTrue(rc.validate_table([]))

    def test_repo_profiles_enable_only_the_shadow_compare_hosts(self):
        if rc.tomllib is None:
            self.skipTest("needs tomllib")
        profiles = sorted((ROOT / "profiles").glob("*-macos-fleet.toml"))
        self.assertTrue(profiles, "control: no fleet profiles found")
        on = {p.name for p in profiles if (rc.load_settings(p)[0] or {}).get("enabled")}
        self.assertEqual(on, {"m3-macos-fleet.toml", "m1-macos-fleet.toml"})

    def test_fleet_validator_rejects_a_bad_table(self):
        if rc.tomllib is None:
            self.skipTest("needs tomllib")
        import subprocess
        source = (ROOT / "profiles" / "m3-macos-fleet.toml").read_text()
        self.assertIn("[reuse_canary]\nenabled = true", source)
        with tempfile.TemporaryDirectory() as td:
            good = subprocess.run([sys.executable, "scripts/macos_fleet_lanes.py", "validate",
                                   str(ROOT / "profiles" / "m3-macos-fleet.toml")],
                                  cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stderr)
            bad = Path(td) / "bad.toml"
            bad.write_text(source.replace("[reuse_canary]\nenabled = true",
                                          "[reuse_canary]\nenabled = \"yes\""))
            res = subprocess.run([sys.executable, "scripts/macos_fleet_lanes.py", "validate",
                                  str(bad)], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("reuse_canary.enabled must be a boolean", res.stderr)

    def test_an_unreadable_profile_is_unknown_not_off(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "p.toml"
            path.write_text("not toml [")
            settings, _ = rc.load_settings(path)
            self.assertIsNone(settings)
            self.assertEqual(rc.load_settings(Path(td) / "absent.toml")[0], {"enabled": False})


class Status(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.state = Path(self.td.name) / "state"
        self.plist = Path(self.td.name) / "agent.plist"
        self.plist.write_text("x")
        self.now = 10 * DAY

    def tearDown(self) -> None:
        self.td.cleanup()

    def write(self, finished_at: float, bindable: Optional[bool], outcome: str = "ran") -> None:
        attempts = self.state / "attempts"
        attempts.mkdir(parents=True, exist_ok=True)
        (attempts / f"{int(finished_at)}.json").write_text(json.dumps(
            {"finished_at": finished_at, "bindable": bindable, "outcome": outcome, "sha": SHA}))

    def status(self, enabled: bool = True) -> Dict[str, Any]:
        return rc.status(self.state, {"enabled": enabled}, plist=self.plist, now=self.now)

    def test_off_not_installed_never_unreadable(self):
        self.assertEqual(self.status(enabled=False)["state"], "off")
        self.assertEqual(rc.status(self.state, None, plist=self.plist)["state"], "unreadable")
        self.assertEqual(self.status()["state"], "never")
        self.plist.unlink()
        self.assertEqual(self.status()["state"], "not_installed")

    def test_a_broken_receipt_is_unreadable(self):
        (self.state / "attempts").mkdir(parents=True)
        (self.state / "attempts" / "x.json").write_text("{")
        self.assertEqual(self.status()["state"], "unreadable")

    def test_stale_at_thirteen_hours(self):
        self.write(self.now - rc.STALE_AFTER_S + 60, True)
        self.assertEqual(self.status()["state"], "ok")
        self.write(self.now - rc.STALE_AFTER_S - 60, True)
        self.state.joinpath("attempts", f"{int(self.now - rc.STALE_AFTER_S + 60)}.json").unlink()
        self.assertEqual(self.status()["state"], "stale")

    def test_no_bindable_at_thirty_six_hours(self):
        self.write(self.now - rc.NO_BINDABLE_AFTER_S + 60, True)
        self.write(self.now - 60, False, "not_due")
        self.assertEqual(self.status()["state"], "ok")
        self.state.joinpath("attempts", f"{int(self.now - rc.NO_BINDABLE_AFTER_S + 60)}.json").unlink()
        self.write(self.now - rc.NO_BINDABLE_AFTER_S - 60, True)
        self.assertEqual(self.status()["state"], "no_bindable")

    def test_no_bindable_ever_counts_from_the_first_receipt(self):
        self.write(self.now - rc.NO_BINDABLE_AFTER_S + 60, False)
        self.write(self.now - 60, False)
        self.assertEqual(self.status()["state"], "ok")
        self.write(self.now - rc.NO_BINDABLE_AFTER_S - 60, False)
        self.assertEqual(self.status()["state"], "no_bindable")


class Doctor(unittest.TestCase):
    def test_each_state_maps_to_a_documented_code(self):
        reasons = fleet_doctor.load_reasons()
        expected = {"off": ("ok", "reuse_canary_off"),
                    "not_installed": ("problem", "reuse_canary_not_installed"),
                    "never": ("unknown", "reuse_canary_never"),
                    "stale": ("problem", "reuse_canary_stale"),
                    "no_bindable": ("problem", "reuse_canary_no_bindable"),
                    "ok": ("ok", "reuse_canary_ok"),
                    "unreadable": ("unknown", "reuse_canary_unreadable")}
        for state, (verdict, code) in expected.items():
            with self.subTest(state=state):
                finding = fleet_doctor.check_reuse_canary({"state": state, "now": 100.0})
                self.assertEqual((finding.state, finding.code), (verdict, code))
                self.assertIn(code, fleet_doctor.CODES)
                self.assertIn(code, reasons)
        self.assertEqual(fleet_doctor.check_reuse_canary(None).code, "reuse_canary_unreadable")


class DoctorCollects(unittest.TestCase):
    def test_collect_reads_the_profile_and_the_receipts(self):
        if rc.tomllib is None:
            self.skipTest("needs tomllib")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            config = home / ".config" / "tartci"
            config.mkdir(parents=True)
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)

            def finding() -> fleet_doctor.Finding:
                rows = fleet_doctor.collect(home=home, skip_census=True,
                                            probe=lambda root: {"error": "stub"})
                return next(row for row in rows if row.check == "reuse_canary")

            self.assertEqual(finding().code, "reuse_canary_off")
            (config / "macos-fleet-profile.toml").write_text(
                (ROOT / "profiles" / "m3-macos-fleet.toml").read_text())
            self.assertEqual(finding().code, "reuse_canary_not_installed")
            (agents / f"{rc.LABEL}.plist").write_text("x")
            self.assertEqual(finding().code, "reuse_canary_never")


class Watchdog(unittest.TestCase):
    def test_the_canary_is_never_healed_mid_run(self):
        self.assertIn(rc.LABEL, watchdog.UNINTERRUPTIBLE_AGENTS)

    def test_its_exit_codes_are_described(self):
        codes = watchdog.APPLICATION_EXIT_CODES[rc.LABEL]
        self.assertEqual(set(codes), {rc.EXIT_REFUSED, rc.EXIT_RUN_FAILED, rc.EXIT_RECORDS,
                                      rc.EXIT_SETUP})


class Main(Case):
    def test_off_runs_nothing(self):
        if rc.tomllib is None:
            self.skipTest("needs tomllib; without it the profile is unknown, not off")
        profile = self.root / "p.toml"
        profile.write_text('schema = 1\n[host]\nid = "t"\n')
        code = rc.main(["run", "--profile-file", str(profile), "--state-dir", str(self.state),
                        "--participation-file", str(self.participation)], self.sys)
        self.assertEqual(code, rc.EXIT_OK)
        self.assertEqual(self.sys.calls, [])
        self.assertTrue(any("off" in x for x in self.sys.lines))


if __name__ == "__main__":
    unittest.main()
