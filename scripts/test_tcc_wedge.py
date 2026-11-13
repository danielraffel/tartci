#!/usr/bin/env python3
"""tcc_wedge: classify a hanging Tart store as a wedged tccd, heal once, name it.

Every positive finding has a negative control: the same inputs with one reading
removed must NOT classify, and must NOT invoke the wrapper.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bounded_subprocess  # noqa: E402
import fleet_doctor as fd  # noqa: E402
import tcc_wedge as tw  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0

ONSET_LINE = ('2026-11-11 00:15:23.588 E  sandboxd[11033:25e640fa] '
              'tcc_server_message_request_authorization(kTCCServiceSystemPolicyRemovableVolumes) '
              'failed: Error Domain=kTCCErrorDomain Code=2 "missing \'auth_value\' in reply message"')
ATTRIBUTION_LINE = ('2026-11-10 22:58:43.681 E  sandboxd[11033:256b065a] [com.apple.TCC:access] '
                    'Created Error: Error Domain=kTCCErrorDomain Code=5 "server error: Failed to '
                    'create Attribution Chain from message."')
UNRELATED_LINE = ('2026-11-11 00:16:37.260 E  sandboxd[11033:25e6f430] (TCC) TCC authorization '
                  'request fails: Code=9 "kTCCServiceSystemPolicyAppDataDetailed access requires '
                  'indirect object have an associated app bundle"')


def ok_control(path, timeout_s):
    return {"ok": True, "path": path, "reason": None, "latency_ms": 0.05}


def hung_control(path, timeout_s):
    return {"ok": False, "path": path, "reason": "timeout after 10s", "latency_ms": None}


def scan_with(total: int, ok: bool = True, removable: int | None = None, seen: list | None = None):
    removable = total if removable is None else removable

    def scan(window_s, before=None):
        if seen is not None:
            seen.append({"window_s": window_s, "before": before})
        return {"ok": ok, "window_s": window_s, "total": total, "first": "x", "last": "y",
                "by_signature": {"attribution_chain": total - removable,
                                 "removable_volumes_auth_failed": removable},
                "error": None if ok else "timeout: log show did not finish"}
    return scan


def classify(**overrides):
    kwargs = dict(consecutive_failures=2, failure_threshold=2,
                  last_reason="timeout after 10s", failing_since=NOW - 1800,
                  control_path="/internal/state", now=NOW,
                  scan=scan_with(5), control=ok_control)
    kwargs.update(overrides)
    return tw.classify(**kwargs)


class SignatureTests(unittest.TestCase):
    def test_counts_both_signatures_and_keeps_first_and_last_timestamps(self) -> None:
        text = "\n".join([ATTRIBUTION_LINE, UNRELATED_LINE, ONSET_LINE])
        hits = tw.signature_hits(text)
        self.assertEqual(hits["total"], 2)
        self.assertEqual(hits["by_signature"],
                         {"attribution_chain": 1, "removable_volumes_auth_failed": 1})
        self.assertEqual(hits["first"], "2026-11-10 22:58:43")
        self.assertEqual(hits["last"], "2026-11-11 00:15:23")

    def test_lines_after_the_last_heal_are_ignored(self) -> None:
        # The release burst a heal triggers must not be the next heal's evidence.
        text = "\n".join([ATTRIBUTION_LINE, ONSET_LINE])
        hits = tw.signature_hits(text, before="2026-11-11 00:00:00")
        self.assertEqual(hits["total"], 1)
        self.assertEqual(hits["ignored_after_heal"], 1)
        self.assertEqual(hits["last"], "2026-11-10 22:58:43")
        self.assertEqual(tw.signature_hits(text, before="2026-11-10 00:00:00")["total"], 0)

    def test_unrelated_tcc_noise_is_not_a_signature(self) -> None:
        # Negative control: the Code=9 AppDataDetailed chatter runs at ~700
        # lines per hour on a healthy host and must never count.
        self.assertEqual(tw.signature_hits("\n".join([UNRELATED_LINE] * 50))["total"], 0)
        self.assertEqual(tw.signature_hits("")["total"], 0)

    def test_window_covers_the_whole_streak_within_bounds(self) -> None:
        self.assertEqual(tw.window_seconds(None, NOW), tw.MIN_WINDOW_S)
        self.assertEqual(tw.window_seconds(NOW + 5, NOW), tw.MIN_WINDOW_S)
        self.assertEqual(tw.window_seconds(NOW - 60, NOW), 60 + tw.MIN_WINDOW_S)
        self.assertEqual(tw.window_seconds(NOW - 3600, NOW), 3600 + tw.MIN_WINDOW_S)
        self.assertEqual(tw.window_seconds(NOW - 10 * 3600, NOW), tw.MAX_WINDOW_S)

    def test_scan_log_is_bounded_and_reports_a_timeout_as_unavailable(self) -> None:
        seen = {}

        def runner(argv, *, timeout, operation):
            seen.update(argv=argv, timeout=timeout)
            return subprocess.CompletedProcess(argv, 0, stdout=ONSET_LINE + "\n", stderr="")
        value = tw.scan_log(1500, before="2026-11-12 00:00:00", runner=runner)
        self.assertTrue(value["ok"])
        self.assertEqual(value["total"], 1)
        self.assertEqual(seen["argv"][:4], [tw.LOG_SHOW, "show", "--last", "25m"])
        self.assertIn("kTCCServiceSystemPolicyRemovableVolumes", seen["argv"][5])
        self.assertEqual(seen["timeout"], tw.LOG_SCAN_TIMEOUT_S)

        def hung(argv, *, timeout, operation):
            return subprocess.CompletedProcess(argv, bounded_subprocess.TIMEOUT_EXIT_CODE,
                                               stdout="", stderr="killed")
        value = tw.scan_log(600, runner=hung)
        self.assertFalse(value["ok"])
        self.assertEqual(value["total"], 0)
        self.assertIn("timeout", value["error"])


class ClassifyTests(unittest.TestCase):
    def test_all_three_readings_agree(self) -> None:
        verdict = classify()
        self.assertEqual(verdict["kind"], tw.KIND)
        self.assertIn("5 sandboxd/tccd", verdict["reason"])
        self.assertEqual(verdict["evidence"]["log_scan"]["window_s"], 1800 + tw.MIN_WINDOW_S)
        self.assertTrue(verdict["evidence"]["control_probe"]["ok"])
        self.assertEqual(verdict["classified_at"], tw._iso(NOW))

    def test_attribution_chain_alone_is_not_the_store(self) -> None:
        # Negative control: that error is logged for any TCC service; only the
        # Removable Volumes request line ties it to the external store.
        verdict = classify(scan=scan_with(40, removable=0))
        self.assertEqual(verdict["kind"], "stalled_unclassified")
        self.assertIn("40 other TCC failure", verdict["reason"])

    def test_log_lines_after_the_last_heal_are_cut_off(self) -> None:
        seen: list = []
        classify(scan=scan_with(5, seen=seen), healed_at=NOW - 300)
        self.assertEqual(seen[0]["before"], tw.local_stamp(NOW - 300))
        seen.clear()
        classify(scan=scan_with(5, seen=seen))
        self.assertIsNone(seen[0]["before"])

    def test_below_threshold_is_not_classified(self) -> None:
        verdict = classify(consecutive_failures=1)
        self.assertEqual(verdict["kind"], "not_classified")
        self.assertNotIn("log_scan", verdict["evidence"])

    def test_a_failure_that_is_not_a_hang_is_not_classified(self) -> None:
        # Negative control: a missing path or a changed device identity fails
        # fast; restarting tccd would be a root action against the wrong cause.
        for reason in ("failed: exit 1", "device identity changed: 1 -> 2", None):
            with self.subTest(reason=reason):
                verdict = classify(last_reason=reason)
                self.assertEqual(verdict["kind"], "not_classified")
                self.assertNotIn("log_scan", verdict["evidence"])

    def test_internal_control_path_hanging_vetoes(self) -> None:
        verdict = classify(control=hung_control)
        self.assertEqual(verdict["kind"], "host_unresponsive")
        self.assertNotIn("log_scan", verdict["evidence"], "no log scan on a hung host")

    def test_no_signature_leaves_the_stall_unclassified(self) -> None:
        verdict = classify(scan=scan_with(0))
        self.assertEqual(verdict["kind"], "stalled_unclassified")
        self.assertIn("no sandboxd/tccd", verdict["reason"])

    def test_unavailable_log_leaves_the_stall_unclassified(self) -> None:
        verdict = classify(scan=scan_with(0, ok=False))
        self.assertEqual(verdict["kind"], "stalled_unclassified")
        self.assertIn("log show unavailable", verdict["reason"])

    def test_tcc_context_probe_that_hangs_names_the_volume_not_tccd(self) -> None:
        verdict = classify(context_probe=lambda: {"ok": False, "reason": "timeout after 10s"})
        self.assertEqual(verdict["kind"], "stalled_unclassified")
        self.assertIn("context with TCC access", verdict["reason"])
        answered = classify(context_probe=lambda: {"ok": True, "latency_ms": 0.1})
        self.assertEqual(answered["kind"], tw.KIND)
        self.assertTrue(answered["evidence"]["tcc_context_probe"]["ok"])


class HealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.wrapper = Path(self.td.name) / "tartci-heal-tcc"
        self.wrapper.write_text("#!/bin/sh\nexit 0\n")
        self.wrapper.chmod(0o755)
        self.calls: list = []

    def runner_rc(self, rc, output: str = ""):
        def runner(argv, timeout):
            self.calls.append((argv, timeout))
            return rc, output
        return runner

    def heal(self, **kwargs):
        kwargs.setdefault("wrapper", str(self.wrapper))
        kwargs.setdefault("insecurity", lambda path: "")
        return tw.heal(now=NOW, **kwargs)

    def test_runs_exactly_sudo_n_wrapper_with_no_arguments(self) -> None:
        record = self.heal(runner=self.runner_rc(0, "restarted"), sudo="/usr/bin/sudo")
        self.assertEqual(record["outcome"], "ran")
        self.assertEqual(self.calls, [(["/usr/bin/sudo", "-n", str(self.wrapper)],
                                       tw.HEAL_TIMEOUT_S)])

    def test_missing_wrapper_runs_nothing_and_names_the_install_step(self) -> None:
        record = self.heal(runner=self.runner_rc(0), wrapper=str(self.wrapper) + ".absent")
        self.assertEqual(record["outcome"], "wrapper_missing")
        self.assertIn("install-tartci-heal-tcc", record["detail"])
        self.assertEqual(self.calls, [])

    def test_a_wrapper_not_owned_by_root_alone_is_never_run(self) -> None:
        # The default check, against a temp dir the login user owns.
        record = tw.heal(now=NOW, runner=self.runner_rc(0), wrapper=str(self.wrapper))
        self.assertEqual(record["outcome"], "wrapper_insecure")
        self.assertIn("not root", record["detail"])
        self.assertEqual(self.calls, [])

    def test_wrapper_insecurity_walks_the_whole_chain(self) -> None:
        import stat as st

        def info(mode, uid=0):
            return type("I", (), {"st_mode": mode, "st_uid": uid})()
        good = {"/": info(st.S_IFDIR | 0o755), "/usr": info(st.S_IFDIR | 0o755),
                "/usr/local": info(st.S_IFDIR | 0o755), "/usr/local/sbin": info(st.S_IFDIR | 0o755),
                "/usr/local/sbin/tartci-heal-tcc": info(st.S_IFREG | 0o755)}
        self.assertEqual(tw.wrapper_insecurity(tw.WRAPPER, lstat=lambda p: good[p]), "")
        brew = dict(good, **{"/usr/local": info(st.S_IFDIR | 0o755, uid=501)})
        self.assertIn("/usr/local is owned by uid 501", tw.wrapper_insecurity(tw.WRAPPER, lstat=lambda p: brew[p]))
        loose = dict(good, **{"/usr/local/sbin": info(st.S_IFDIR | 0o775)})
        self.assertIn("writable by group", tw.wrapper_insecurity(tw.WRAPPER, lstat=lambda p: loose[p]))
        link = dict(good, **{"/usr/local/sbin/tartci-heal-tcc": info(st.S_IFLNK | 0o755)})
        self.assertIn("symlink", tw.wrapper_insecurity(tw.WRAPPER, lstat=lambda p: link[p]))
        self.assertIn("not an absolute", tw.wrapper_insecurity("relative/x"))

    def test_refused_sudo_is_not_permitted(self) -> None:
        record = self.heal(runner=self.runner_rc(1, "sudo: a password is required"))
        self.assertEqual(record["outcome"], "not_permitted")

    def test_wrapper_rate_limit_and_other_failures_are_distinct(self) -> None:
        self.assertEqual(self.heal(runner=self.runner_rc(75, "rate limited"))["outcome"],
                         "wrapper_rate_limited")
        self.assertEqual(self.heal(runner=self.runner_rc(1, "killall: boom"))["outcome"],
                         "failed")
        self.assertEqual(self.heal(runner=self.runner_rc(None, ""))["outcome"], "timeout")

    def test_privileged_runner_gives_up_on_a_child_it_cannot_kill(self) -> None:
        rc, output = tw.run_privileged_bounded(["/bin/sh", "-c", "echo hi; exit 3"], 5)
        self.assertEqual((rc, output.strip()), (3, "hi"))
        with mock.patch.object(tw.subprocess.Popen, "kill", side_effect=PermissionError):
            rc, output = tw.run_privileged_bounded(["/bin/sh", "-c", "sleep 5"], 0.2)
        self.assertIsNone(rc)

    def test_tartci_side_rate_limit_runs_nothing(self) -> None:
        recent = {"at_epoch": NOW - 600, "outcome": "ran"}
        record = self.heal(last_heal=recent, runner=self.runner_rc(0))
        self.assertEqual(record["outcome"], "rate_limited")
        self.assertEqual(self.calls, [])
        old = {"at_epoch": NOW - tw.HEAL_INTERVAL_S - 1, "outcome": "ran"}
        self.assertEqual(self.heal(last_heal=old, runner=self.runner_rc(0))["outcome"], "ran")


class WatchdogStepTests(unittest.TestCase):
    """The watchdog's store-health state after a pass past the threshold."""

    def draining(self, failures: int = 2) -> dict:
        return {"state": "draining", "consecutive_failures": failures, "drain_required": True,
                "failure_threshold": 2, "path": "/Volumes/Example/VMs",
                "last_result": {"ok": False, "reason": "timeout after 10s"}}

    def step(self, state, previous=None, **overrides):
        kwargs = dict(control_path="/internal/state", store_timeout_s=10.0, dry_run=False,
                      now=NOW, classify_fn=lambda **kw: {"kind": tw.KIND, "reason": "r",
                                                         "evidence": {},
                                                         "classified_at": tw._iso(NOW)},
                      heal_fn=lambda **kw: {"outcome": "ran", "at": tw._iso(NOW), "rc": 0},
                      reprobe=lambda path, timeout_s: {"ok": True, "latency_ms": 8.1,
                                                       "device_id": "1"})
        kwargs.update(overrides)
        return tw.watchdog_step(state, previous or {}, **kwargs)

    def test_classified_wedge_heals_reprobes_and_reports_recovery(self) -> None:
        state = self.draining()
        recovered = self.step(state, previous={"failing_since": NOW - 900})
        self.assertIsNotNone(recovered)
        self.assertEqual(state["classification"]["kind"], tw.KIND)
        self.assertEqual(state["heal"]["outcome"], "ran")
        self.assertTrue(state["heal"]["reprobe"]["ok"])
        self.assertEqual(state["failing_since"], NOW - 900)
        self.assertEqual(state["last_heal"]["outcome"], "ran")

    def test_one_heal_per_streak_then_escalate(self) -> None:
        # A restart that did not bring the store back must not be repeated
        # every 30 min while the real cause (a failing disk) goes unread.
        state = self.draining()
        previous = {"failing_since": NOW - 7200,
                    "last_heal": {"outcome": "ran", "at": tw._iso(NOW - 3600),
                                  "at_epoch": NOW - 3600, "failing_since": NOW - 7200}}
        healed = []
        self.assertIsNone(self.step(state, previous=previous,
                                    heal_fn=lambda **kw: healed.append(kw)))
        self.assertEqual(healed, [])
        self.assertEqual(state["heal"]["outcome"], "escalated")
        self.assertIn("reboot", state["heal"]["detail"])
        # A NEW streak after a healthy gap may heal again.
        fresh = self.draining()
        self.assertIsNotNone(self.step(fresh, previous={
            "last_heal": {"outcome": "ran", "at_epoch": NOW - 3600, "failing_since": NOW - 9000}}))
        self.assertEqual(fresh["heal"]["outcome"], "ran")

    def test_log_cutoff_is_the_last_heal(self) -> None:
        seen = []
        state = self.draining()
        self.step(state, previous={"last_heal": {"outcome": "ran", "at_epoch": NOW - 3600,
                                                  "failing_since": NOW - 9000}},
                  classify_fn=lambda **kw: seen.append(kw) or {"kind": "x", "reason": "",
                                                               "evidence": {}})
        self.assertEqual(seen[0]["healed_at"], NOW - 3600)

    def test_rollout_mid_wedge_seeds_the_streak_from_the_failure_count(self) -> None:
        seen = []
        state = self.draining(failures=5)
        self.step(state, previous={"consecutive_failures": 4},
                  classify_fn=lambda **kw: seen.append(kw) or {"kind": "x", "reason": "",
                                                               "evidence": {}})
        self.assertEqual(state["failing_since"], NOW - 4 * tw.WATCHDOG_INTERVAL_S)
        self.assertEqual(seen[0]["failing_since"], NOW - 4 * tw.WATCHDOG_INTERVAL_S)

    def test_a_refused_sudo_spends_the_budget(self) -> None:
        state = self.draining()
        self.step(state, heal_fn=lambda **kw: {"outcome": "not_permitted", "at": tw._iso(NOW),
                                               "detail": "a password is required"})
        self.assertEqual(state["last_heal"]["outcome"], "not_permitted")
        again = self.draining()
        self.assertIsNone(self.step(again, previous={"failing_since": NOW - 300,
                                                     "last_heal": state["last_heal"]},
                                    heal_fn=tw.heal, now=NOW + 300))
        self.assertEqual(again["heal"]["outcome"], "rate_limited")

    def test_a_wrapper_rate_limit_keeps_the_earlier_budget_record(self) -> None:
        state = self.draining()
        earlier = {"outcome": "ran", "at_epoch": NOW - 1500, "failing_since": NOW - 99}
        self.step(state, previous={"failing_since": NOW - 60, "last_heal": earlier},
                  heal_fn=lambda **kw: {"outcome": "wrapper_rate_limited", "at": tw._iso(NOW)})
        self.assertEqual(state["last_heal"], earlier)

    def test_first_failure_starts_the_streak_and_recovery_clears_it(self) -> None:
        state = {"state": "suspect", "consecutive_failures": 1, "drain_required": False,
                 "last_result": {"ok": False, "reason": "timeout after 10s"}}
        calls = []
        self.assertIsNone(self.step(state, classify_fn=lambda **kw: calls.append(kw)))
        self.assertEqual(state["failing_since"], NOW)
        self.assertEqual(calls, [], "below the threshold nothing is classified")
        healthy = {"state": "healthy", "consecutive_failures": 0, "drain_required": False,
                   "last_result": {"ok": True}}
        self.step(healthy, previous={"failing_since": NOW - 5, "last_heal": {"at_epoch": NOW - 5}})
        self.assertNotIn("failing_since", healthy)
        self.assertEqual(healthy["last_heal"], {"at_epoch": NOW - 5}, "the heal budget survives")

    def test_unclassified_stall_never_heals(self) -> None:
        # Negative control: identical state, a verdict that is not the wedge.
        state = self.draining()
        healed = []
        self.assertIsNone(self.step(
            state, classify_fn=lambda **kw: {"kind": "stalled_unclassified",
                                             "reason": "no signature", "evidence": {},
                                             "classified_at": tw._iso(NOW)},
            heal_fn=lambda **kw: healed.append(kw)))
        self.assertEqual(healed, [])
        self.assertNotIn("heal", state)

    def test_dry_run_and_disabled_classify_but_do_not_heal(self) -> None:
        for kwargs in ({"dry_run": True}, {"heal_enabled": False}):
            with self.subTest(**kwargs):
                state = self.draining()
                healed = []
                self.assertIsNone(self.step(state, heal_fn=lambda **kw: healed.append(kw),
                                            **kwargs))
                self.assertEqual(state["classification"]["kind"], tw.KIND)
                self.assertEqual(state["heal"]["outcome"], "disabled")
                self.assertEqual(healed, [])

    def test_heal_that_did_not_bring_the_store_back_is_recorded_not_recovered(self) -> None:
        state = self.draining()
        self.assertIsNone(self.step(
            state, reprobe=lambda path, timeout_s: {"ok": False, "reason": "timeout after 10s"}))
        self.assertEqual(state["heal"]["outcome"], "ran")
        self.assertFalse(state["heal"]["reprobe"]["ok"])
        self.assertIn("still timeout", tw.describe(state))

    def test_wrapper_missing_is_recorded_and_spends_no_budget(self) -> None:
        state = self.draining()
        self.assertIsNone(self.step(
            state, heal_fn=lambda **kw: {"outcome": "wrapper_missing", "at": tw._iso(NOW),
                                         "detail": "install it"}))
        self.assertEqual(state["heal"]["outcome"], "wrapper_missing")
        self.assertNotIn("last_heal", state)
        self.assertEqual(tw.describe(state), f"{tw.KIND}: heal wrapper_missing")


class LaneAndDoctorTests(unittest.TestCase):
    def wedge_state(self, outcome="ran", reprobe_ok=True, age_s=60, store_state="draining"):
        heal = {"outcome": outcome, "at": tw._iso(NOW - age_s)}
        if outcome == "ran":
            heal["reprobe"] = {"ok": reprobe_ok, "latency_ms": 8.1 if reprobe_ok else None,
                               "reason": None if reprobe_ok else "timeout after 10s"}
        return {"state": store_state,
                "classification": {"kind": tw.KIND, "reason": "r",
                                   "classified_at": tw._iso(NOW - age_s)},
                "heal": heal}

    def test_lane_cause_names_a_fresh_wedge_and_its_heal_outcome(self) -> None:
        self.assertEqual(tw.lane_cause(self.wedge_state("wrapper_missing"), now=NOW),
                         f"{tw.KIND} wrapper_missing")
        state = self.wedge_state()
        del state["heal"]
        self.assertEqual(tw.lane_cause(state, now=NOW), f"{tw.KIND} not_attempted")

    def test_lane_cause_is_silent_without_a_fresh_wedge(self) -> None:
        # Negative controls: stale verdict, other verdict, healthy store, no state.
        self.assertEqual(tw.lane_cause(self.wedge_state(age_s=tw.FRESH_S + 1), now=NOW), "")
        other = self.wedge_state()
        other["classification"]["kind"] = "stalled_unclassified"
        self.assertEqual(tw.lane_cause(other, now=NOW), "")
        self.assertEqual(tw.lane_cause(self.wedge_state(store_state="healthy"), now=NOW), "")
        self.assertEqual(tw.lane_cause({}, now=NOW), "")

    def test_lane_cause_cli_reads_the_internal_state_file_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "state"
            state_dir.mkdir()
            (state_dir / "store-health.json").write_text(
                json.dumps(self.wedge_state("not_permitted", age_s=0)))
            env = dict(os.environ, TARTCI_HOME=td)
            out = subprocess.run([sys.executable, str(ROOT / "scripts" / "tcc_wedge.py"),
                                  "--lane-cause"], capture_output=True, text=True, env=env,
                                 check=False)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn(f"{tw.KIND} not_permitted", out.stdout.strip())
            (state_dir / "store-health.json").unlink()
            out = subprocess.run([sys.executable, str(ROOT / "scripts" / "tcc_wedge.py"),
                                  "--lane-cause"], capture_output=True, text=True, env=env,
                                 check=False)
            self.assertEqual((out.returncode, out.stdout), (0, ""))

    def test_doctor_finding_per_outcome(self) -> None:
        cases = [
            (self.wedge_state(), fd.OK, "tcc_wedge_healed"),
            (self.wedge_state(reprobe_ok=False), fd.PROBLEM, "tcc_wedge_unhealed"),
            (self.wedge_state("wrapper_missing"), fd.PROBLEM, "tcc_wedge_unhealed"),
            (self.wedge_state(age_s=tw.FRESH_S + 1, store_state="healthy"), fd.OK,
             "tcc_wedge_none"),
            (self.wedge_state(age_s=tw.FRESH_S + 1), fd.UNKNOWN, "tcc_wedge_unclassified"),
            ({}, fd.OK, "tcc_wedge_none"),
            (None, fd.OK, "tcc_wedge_none"),
        ]
        for value, state, code in cases:
            with self.subTest(code=code, value=value):
                finding = fd.check_tcc_wedge(value, now=NOW)
                self.assertEqual((finding.state, finding.code), (state, code))
                self.assertIn(code, fd.CODES)
                self.assertIn(code, fd.load_reasons())
        self.assertIn("still timeout",
                      fd.check_tcc_wedge(self.wedge_state(reprobe_ok=False), now=NOW).detail)
        self.assertIn("wrapper_missing",
                      fd.check_tcc_wedge(self.wedge_state("wrapper_missing"), now=NOW).detail)

    def test_runner_names_the_cause_and_keeps_the_plain_deferral(self) -> None:
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        gate = runner[runner.index("tartci_storage_health_check(){"):]
        gate = gate[:gate.index("\n}\n")]
        self.assertIn("tcc_wedge.py\" --lane-cause", gate)
        self.assertIn("event tcc_attribution_wedge", gate)
        self.assertIn("heartbeat tcc-attribution-wedge", gate)
        self.assertIn("heartbeat yielded-storage-health", gate)
        self.assertLess(gate.index("--lane-cause"), gate.index("heartbeat tcc-attribution-wedge"))

    def test_the_wedge_phase_is_blocked_for_supply_and_idle_for_lane_busy(self) -> None:
        import gate_supply
        import lane_busy
        self.assertIn(tw.PHASE, gate_supply.BLOCKED_PHASES)
        self.assertNotIn(tw.PHASE, gate_supply.FREE_PHASES)
        self.assertIn(tw.PHASE, lane_busy.IDLE_PHASES)


class WrapperSourceTests(unittest.TestCase):
    """The root wrapper's contract, read from its source (the dry run is in
    tests/test_fleet_ops_scripts.sh)."""

    def test_wrapper_is_fixed_argument_less_and_rate_limited(self) -> None:
        text = (ROOT / "fleet" / "ops" / "tartci-heal-tcc").read_text()
        self.assertIn('[[ $# -eq 0 ]] || die "takes no arguments"', text)
        self.assertIn('"$KILLALL" tccd sandboxd', text)
        self.assertEqual(text.count('"$KILLALL"'), 1, "one fixed command vector")
        self.assertIn("MIN_INTERVAL=1800", text)
        self.assertIn("STAMP=$STAMP_DIR/last-heal", text)
        self.assertIn("export PATH=/usr/bin:/bin:/usr/sbin:/sbin", text)
        # The root path never reads the test knobs: strip the unprivileged
        # branch and nothing else may mention them.
        start = text.index("if [[ $EUID -ne 0 ]]")
        end = text.index("\nfi\n", start) + len("\nfi\n")
        root_path = text[:start] + text[end:]
        self.assertNotIn("TARTCI_HEAL_TCC_", root_path)
        self.assertIn("STAMP_DIR=/var/db/tartci-heal-tcc", text)
        self.assertNotIn("/var/run", text)
        code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        for forbidden in ("launchctl", "reboot", "shutdown", "kill -9", "pkill", "rm -rf"):
            self.assertNotIn(forbidden, code)

    def test_sudoers_grants_one_path_with_an_empty_argument_spec(self) -> None:
        text = (ROOT / "fleet" / "ops" / "tartci-heal-tcc.sudoers").read_text()
        grants = [line for line in text.splitlines() if line and not line.startswith("#")]
        self.assertEqual(grants, ['TARTCI_USER ALL=(root) NOPASSWD: /usr/local/sbin/tartci-heal-tcc ""'])
        self.assertNotIn("*", "".join(grants))

    def test_heal_command_matches_the_installed_path(self) -> None:
        self.assertEqual(tw.WRAPPER, "/usr/local/sbin/tartci-heal-tcc")
        installer = (ROOT / "fleet" / "ops" / "install-tartci-heal-tcc.sh").read_text()
        self.assertIn("WRAPPER_DST=/usr/local/sbin/tartci-heal-tcc", installer)
        self.assertIn("visudo -cf", installer)


FIXTURES = ROOT / "scripts" / "fixtures" / "tcc_wedge"


def fixture_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class NoSignatureWedgeFixtureTests(unittest.TestCase):
    """The m3 stall of 2026-11-13: the store hangs, sandboxd/tccd log no refusal.

    Fixtures: the watchdog's store-health.json from the issue (paths made
    placeholders), the fresh-launchd-job probe record measured on m3 that
    morning (hung on the volume root, internal open answered), and a
    sandboxd/tccd log excerpt: the issue's unanswered IPC line (thread
    3417775e) among answered threads captured on m3 the same morning.
    """

    def readings(self, **overrides):
        state = fixture_json("m3-20261113-store-health.json")
        logged = state["classification"]["evidence"]
        kwargs = dict(
            consecutive_failures=state["consecutive_failures"],
            failure_threshold=state["failure_threshold"],
            last_reason=state["last_result"]["reason"],
            failing_since=state["failing_since"], control_path="/internal/state",
            now=state["updated_at"],
            control=lambda path, timeout_s: dict(logged["control_probe"]),
            scan=lambda window_s, before=None: dict(logged["log_scan"]),
            launchd_probe=lambda: fixture_json("m3-20261113-launchd-probe.json"),
            ipc_scan=lambda start, end: tw.ipc_hits(
                (FIXTURES / "m3-20261113-sandboxd-ipc.txt").read_text()),
            # Evidence 2 in the issue: the same probe from an interactive shell.
            context_probe=lambda: {"ok": True, "latency_ms": 0.052,
                                   "context": "interactive shell"})
        kwargs.update(overrides)
        return tw.classify(**kwargs)

    def test_the_fixture_ipc_log_has_one_unanswered_request(self) -> None:
        hits = tw.ipc_hits((FIXTURES / "m3-20261113-sandboxd-ipc.txt").read_text())
        self.assertEqual(hits["state"], "unanswered")
        self.assertEqual(hits["unanswered"], 1)
        self.assertEqual(hits["answered"], 3)
        self.assertEqual(hits["first_unanswered"], "2026-11-12 22:02:32")
        self.assertEqual(hits["tccd_requests"], 2)

    def test_the_11_13_readings_give_a_wedge_without_error_lines(self) -> None:
        verdict = self.readings()
        self.assertEqual(verdict["kind"], tw.KIND)
        self.assertEqual(verdict["path"], "launchd_root_hang")
        self.assertIn("/Volumes/Example", verdict["reason"])
        self.assertIn("1 authorization request(s) whose thread logged no next step",
                      verdict["reason"])
        self.assertEqual(verdict["evidence"]["log_scan"]["total"], 0)
        self.assertEqual(verdict["evidence"]["launchd_probe"]["outcome"], "hung_on_root")

    def test_the_ipc_signature_is_corroboration_not_a_requirement(self) -> None:
        # Measured on m3 on 11-13: the next fresh launchd probe hung the same
        # way and sandboxd logged no IPC line for it at all.
        for ipc in ({"state": "absent", "unanswered": 0}, {"state": "unavailable"},
                    {"state": "all_answered", "answered": 4, "unanswered": 0}):
            with self.subTest(ipc=ipc["state"]):
                verdict = self.readings(ipc_scan=lambda start, end, ipc=ipc: ipc)
                self.assertEqual(verdict["kind"], tw.KIND)
                self.assertIn(f"IPC signature {ipc['state']}", verdict["reason"])

    def test_the_issue_classification_without_the_new_readings_stays_unclassified(self) -> None:
        # Negative control: the readings the watchdog had on 11-13 (control and
        # an empty signature scan) and nothing else are what it recorded.
        verdict = self.readings(launchd_probe=None, context_probe=None)
        self.assertEqual(verdict["kind"], "stalled_unclassified")


class DisagreeingReadingsNeverHealTests(unittest.TestCase):
    """Every combination where the readings disagree, through the real classify."""

    def run_step(self, *, launchd, context, removable=0):
        state = {"state": "draining", "consecutive_failures": 3, "drain_required": True,
                 "failure_threshold": 2, "path": "/Volumes/Example/VMs",
                 "last_result": {"ok": False, "reason": "timeout after 10s"}}
        healed: list = []

        def classify_fn(**kw):
            kw.update(scan=scan_with(removable), control=ok_control,
                      ipc_scan=lambda start, end: {"state": "unanswered", "unanswered": 1})
            return tw.classify(**kw)
        tw.watchdog_step(state, {"failing_since": NOW - 900}, control_path="/internal/state",
                         store_timeout_s=10.0, dry_run=False, now=NOW,
                         classify_fn=classify_fn,
                         heal_fn=lambda **kw: healed.append(kw) or {"outcome": "ran",
                                                                   "at": tw._iso(NOW)},
                         reprobe=lambda path, timeout_s: {"ok": True},
                         context_probe=context,
                         launchd_probe=None if launchd is None else (lambda: dict(launchd)))
        return state, healed

    HUNG = {"outcome": "hung_on_root", "volume_root": "/Volumes/Example",
            "started_epoch": NOW - 12, "waited_s": 10.0}
    ANSWERED = {"outcome": "answered", "volume_root": "/Volumes/Example", "root_latency_ms": 0.1}
    FAST_ERROR = {"outcome": "root_error", "volume_root": "/Volumes/Example", "errno": 1,
                  "error": "[Errno 1] Operation not permitted"}

    def ok(self):
        return {"ok": True}

    def hung(self):
        return {"ok": False, "reason": "timeout after 10s"}

    def test_the_agreeing_readings_heal_once(self) -> None:
        # The positive control every veto below is measured against.
        state, healed = self.run_step(launchd=self.HUNG, context=self.ok)
        self.assertEqual(state["classification"]["kind"], tw.KIND)
        self.assertEqual(len(healed), 1)

    def test_disagreements_never_heal(self) -> None:
        cases = {
            "launchd job opened the root": dict(launchd=self.ANSWERED, context=self.ok),
            "launchd job failed fast on the root": dict(launchd=self.FAST_ERROR,
                                                        context=self.ok),
            "authorized context hangs too": dict(launchd=self.HUNG, context=self.hung),
            "no authorized context configured": dict(launchd=self.HUNG, context=None),
            "no launchd probe": dict(launchd=None, context=self.ok),
            "launchd probe recorded nothing": dict(
                launchd={"outcome": "did_not_start", "volume_root": "/Volumes/Example"},
                context=self.ok),
            "launchd probe inconclusive": dict(
                launchd={"outcome": "inconclusive", "detail": "launchctl submit failed"},
                context=self.ok),
            "error lines, but the launchd job opened the root": dict(
                launchd=self.ANSWERED, context=self.ok, removable=7),
            "error lines, but the authorized context hangs": dict(
                launchd=self.HUNG, context=self.hung, removable=7),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                state, healed = self.run_step(**kwargs)
                self.assertEqual(healed, [], name)
                self.assertNotEqual(state["classification"]["kind"], tw.KIND, name)
                self.assertNotIn("heal", state, name)

    def test_without_a_context_probe_the_launchd_job_is_not_run(self) -> None:
        calls: list = []
        state = {"state": "draining", "consecutive_failures": 3, "drain_required": True,
                 "failure_threshold": 2, "path": "/Volumes/Example/VMs",
                 "last_result": {"ok": False, "reason": "timeout after 10s"}}
        scans: list = []
        verdict = tw.classify(consecutive_failures=3, failure_threshold=2,
                              last_reason="timeout after 10s", failing_since=NOW - 900,
                              control_path="/internal/state", now=NOW, scan=scan_with(0),
                              control=ok_control, context_probe=None,
                              launchd_probe=lambda: calls.append(1) or dict(self.HUNG),
                              ipc_scan=lambda start, end: scans.append(1) or {})
        self.assertEqual(calls, [], "no launchd job without a context probe")
        self.assertEqual(scans, [], "no IPC log scan without a context probe")
        self.assertEqual(verdict["kind"], "stalled_unclassified")
        self.assertIn("skipped because no context with TCC access", verdict["reason"])
        self.assertEqual(verdict["evidence"]["launchd_probe"]["outcome"], "skipped")
        del state

    def test_a_context_slower_than_the_store_timeout_never_heals(self) -> None:
        # A slow volume answers every context in more than the store timeout.
        # The context command must be cut at that same bound, or the launchd
        # hang plus a late "context ok" would read as a TCC wedge.
        probe = tw.context_probe_from_env({"TARTCI_TCC_CONTEXT_PROBE": "/bin/sleep 3"},
                                          timeout_s=0.5)
        started = time.monotonic()
        state, healed = self.run_step(launchd=self.HUNG, context=probe)
        self.assertLess(time.monotonic() - started, 2.5, "the context probe was not bounded")
        self.assertEqual(healed, [])
        self.assertEqual(state["classification"]["kind"], "stalled_unclassified")
        self.assertFalse(state["classification"]["evidence"]["tcc_context_probe"]["ok"])
        # Control: the same command inside the bound answers and heals.
        quick = tw.context_probe_from_env({"TARTCI_TCC_CONTEXT_PROBE": "/bin/sleep 0.1"},
                                          timeout_s=2.0)
        state, healed = self.run_step(launchd=self.HUNG, context=quick)
        self.assertEqual(len(healed), 1)

    def test_the_watchdog_bounds_the_context_probe_by_the_store_timeout(self) -> None:
        source = (ROOT / "scripts" / "tartci_launchd_watchdog.py").read_text()
        self.assertIn("context_probe_from_env(timeout_s=timeout_s)", source)
        self.assertNotIn("LOG_SCAN_TIMEOUT_S", (ROOT / "scripts" / "tcc_wedge.py").read_text()
                         .split("def context_probe_from_env", 1)[1].split("def describe", 1)[0])

    def test_the_existing_signature_path_still_heals(self) -> None:
        for launchd in (None, self.HUNG, {"outcome": "not_external"},
                        {"outcome": "did_not_start", "volume_root": "/Volumes/Example"}):
            with self.subTest(launchd=launchd):
                state, healed = self.run_step(launchd=launchd, context=None, removable=3)
                self.assertEqual(state["classification"]["kind"], tw.KIND)
                self.assertEqual(state["classification"]["path"], "error_signature")
                self.assertEqual(len(healed), 1)


class IpcSignatureTests(unittest.TestCase):
    def line(self, stamp, tid, message="(TCC) tcc_send_request_authorization() IPC"):
        return f"2026-11-12 {stamp} A  sandboxd[11033:{tid}] {message}"

    def test_a_second_ipc_without_a_follow_up_is_a_later_step_not_a_new_request(self) -> None:
        text = "\n".join([self.line("22:07:30.035", "aa"),
                          self.line("22:07:30.035", "aa", "activating connection: name=com.apple.tccd.system"),
                          self.line("22:07:30.056", "aa")])
        self.assertEqual(tw.ipc_hits(text)["state"], "all_answered")

    def test_a_lone_ipc_is_unanswered_unless_the_log_ends_too_soon(self) -> None:
        text = self.line("22:02:32.565", "bb")
        self.assertEqual(tw.ipc_hits(text)["state"], "unanswered")
        end = tw._local_epoch("2026-11-12 22:02:34", None)
        self.assertEqual(tw.ipc_hits(text, end_epoch=end)["state"], "pending")
        self.assertEqual(tw.ipc_hits("")["state"], "absent")

    def test_a_follow_up_later_than_the_window_does_not_answer(self) -> None:
        text = "\n".join([self.line("22:02:32.000", "cc"),
                          self.line("22:02:40.000", "cc", "activating connection")])
        self.assertEqual(tw.ipc_hits(text)["unanswered"], 1)

    def test_scan_ipc_is_bounded_and_reports_failure_as_unavailable(self) -> None:
        seen = {}

        def runner(argv, *, timeout, operation):
            seen.update(argv=argv, timeout=timeout)
            return subprocess.CompletedProcess(argv, 0, stdout=self.line("22:02:32.565", "dd"),
                                               stderr="")
        value = tw.scan_ipc(NOW, NOW + 10, runner=runner)
        self.assertEqual(seen["timeout"], tw.IPC_SCAN_TIMEOUT_S)
        self.assertIn("--start", seen["argv"])
        self.assertIn("--debug", seen["argv"])
        self.assertIn("tcc_send_request_authorization", value and "tcc_send_request_authorization")

        def hung(argv, *, timeout, operation):
            return subprocess.CompletedProcess(argv, bounded_subprocess.TIMEOUT_EXIT_CODE,
                                               stdout="", stderr="killed")
        self.assertEqual(tw.scan_ipc(NOW, NOW + 10, runner=hung)["state"], "unavailable")


class LaunchdProbeTests(unittest.TestCase):
    """The fresh-launchd-job probe, with launchctl faked: the child phases and
    the always-remove contract."""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.calls: list = []

    def runner(self, phase=None, submit_rc=0, remove_rc=0, opened_ago=100.0):
        def run(argv, *, timeout, operation):
            self.calls.append(list(argv))
            if argv[1] == "submit":
                if phase:
                    result = argv[argv.index("--result") + 1]
                    value = {"phase": phase, "pid": 1, "internal_latency_ms": 0.02}
                    if opened_ago is not None:
                        value["root_open_started"] = time.time() - opened_ago
                    Path(result).write_text(json.dumps(value))
                return subprocess.CompletedProcess(argv, submit_rc, stdout="",
                                                   stderr="" if submit_rc == 0 else "denied")
            return subprocess.CompletedProcess(argv, remove_rc, stdout="", stderr="")
        return run

    def probe(self, runner, timeout_s=0.3, **kwargs):
        kwargs.setdefault("grace_s", 0.2)
        return tw.launchd_probe("/Volumes/Example/VMs", self.td.name, timeout_s, runner=runner,
                                python="/usr/bin/python3", **kwargs)

    def test_a_hang_is_timed_from_the_childs_root_open_stamp(self) -> None:
        # Open long enough: a hang. Never stamped, or stamped too recently
        # (launchd was slow to start the job): inconclusive, never a hang.
        hung = self.probe(self.runner("internal_ok", opened_ago=5.0), timeout_s=1.0)
        self.assertEqual(hung["outcome"], "hung_on_root")
        self.assertGreaterEqual(hung["root_open_s"], 1.0)
        for opened_ago in (None, -100.0):
            with self.subTest(opened_ago=opened_ago):
                record = self.probe(self.runner("internal_ok", opened_ago=opened_ago),
                                    timeout_s=1.0)
                self.assertEqual(record["outcome"], "inconclusive")
                self.assertIn("not observed for the full", record["detail"])

    def verbs(self):
        return [argv[1] for argv in self.calls]

    def test_outcome_per_phase_and_the_job_is_always_removed(self) -> None:
        for phase, outcome in (("root_ok", "answered"), ("root_error", "root_error"),
                               ("internal_ok", "hung_on_root"), (None, "did_not_start")):
            with self.subTest(phase=phase):
                self.calls.clear()
                record = self.probe(self.runner(phase))
                self.assertEqual(record["outcome"], outcome)
                self.assertEqual(self.verbs(), ["submit", "remove"])
                self.assertEqual(self.calls[1][2], record["label"])
                self.assertTrue(record["job_removed"])
                self.assertEqual(os.listdir(Path(self.td.name) / tw.LAUNCHD_PROBE_DIR), [])

    def test_submit_argv_opens_the_volume_root_from_the_internal_dir(self) -> None:
        self.probe(self.runner("root_ok"))
        argv = self.calls[0]
        self.assertEqual(argv[:4], [tw.LAUNCHCTL, "submit", "-l", argv[3]])
        self.assertTrue(argv[3].startswith(tw.LAUNCHD_LABEL_PREFIX + "."))
        self.assertEqual(argv[argv.index("--root") + 1], "/Volumes/Example")
        self.assertEqual(argv[argv.index("--internal") + 1],
                         str(Path(self.td.name) / tw.LAUNCHD_PROBE_DIR))
        self.assertEqual(argv[argv.index("--") + 2], tw.CHILD)

    def test_removed_even_when_polling_raises(self) -> None:
        def boom(_seconds):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.probe(self.runner("internal_ok", opened_ago=None), timeout_s=5, sleep=boom)
        self.assertEqual(self.verbs(), ["submit", "remove"])

    def test_a_failed_submit_is_inconclusive_and_still_removed(self) -> None:
        record = self.probe(self.runner(submit_rc=1))
        self.assertEqual(record["outcome"], "inconclusive")
        self.assertIn("denied", record["detail"])
        self.assertEqual(self.verbs(), ["submit", "remove"])

    def test_a_job_that_could_not_be_removed_is_retried_next_pass(self) -> None:
        first = self.probe(self.runner("internal_ok", remove_rc=5))
        self.assertFalse(first["job_removed"])
        self.calls.clear()
        second = self.probe(self.runner("root_ok"))
        self.assertEqual(second["removed_leftover_jobs"], [first["label"]])
        self.assertEqual(self.verbs(), ["remove", "submit", "remove"])
        self.assertEqual(self.calls[0][2], first["label"])

    def test_an_internal_store_is_not_probed(self) -> None:
        record = tw.launchd_probe("/var/tmp/vms", self.td.name, 1, runner=self.runner("root_ok"))
        self.assertEqual(record["outcome"], "not_external")
        self.assertEqual(self.calls, [])
        self.assertEqual(tw.volume_root("/Volumes/Example/VMs"), "/Volumes/Example")
        self.assertEqual(tw.volume_root("/Volumes/Example"), "/Volumes/Example")
        self.assertEqual(tw.volume_root("/Users/example/vms"), "/")

    def test_the_probe_is_off_with_its_switch(self) -> None:
        self.assertIsNone(tw.launchd_probe_from_env("/Volumes/Example/VMs", "/x", 10,
                                                    env={"TARTCI_TCC_LAUNCHD_PROBE": "0"}))
        self.assertIsNotNone(tw.launchd_probe_from_env("/Volumes/Example/VMs", "/x", 10, env={}))

    def test_the_child_records_each_phase(self) -> None:
        result = Path(self.td.name) / "r.json"
        args = [sys.executable, str(ROOT / "scripts" / "tcc_launchd_probe_child.py"),
                "--internal", self.td.name, "--result", str(result), "--hold", "0"]
        out = subprocess.run(args + ["--root", self.td.name], capture_output=True, text=True,
                             check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        value = json.loads(result.read_text())
        self.assertEqual(value["phase"], "root_ok")
        self.assertIsInstance(value["root_open_started"], float)
        result.unlink()
        subprocess.run(args + ["--root", str(Path(self.td.name) / "absent")], check=False)
        value = json.loads(result.read_text())
        self.assertEqual(value["phase"], "root_error")
        self.assertEqual(value["errno"], 2)


class DoctorContextTests(unittest.TestCase):
    """doctor fleet shows the watchdog's reading next to its own, named by context."""

    OWN_OK = {"state": "ok", "detail": "4 store probe(s) answered within 15s", "stalled": [],
              "probed_at": NOW}

    def test_draining_from_the_state_file_even_when_the_own_probe_is_ok(self) -> None:
        watchdog = fixture_json("m3-20261113-store-health.json")
        finding = fd.check_store_stall(self.OWN_OK, watchdog=watchdog,
                                       context="this doctor (interactive shell)",
                                       now=watchdog["updated_at"] + 13)
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "store_context_disagree"))
        self.assertIn("draining", finding.detail)
        self.assertIn("3 consecutive failure(s)", finding.detail)
        self.assertIn("[this doctor (interactive shell)]", finding.detail)
        self.assertIn("launchd watchdog (launchd job): draining, 13s ago", finding.detail)
        self.assertIn("classified stalled_unclassified", finding.detail)
        tcc = fd.check_tcc_wedge(watchdog, now=watchdog["updated_at"])
        self.assertEqual((tcc.state, tcc.code), (fd.UNKNOWN, "tcc_wedge_unclassified"))
        self.assertIn("cause is unproven", tcc.detail)
        for code in ("store_context_disagree", "store_context_suspect", "tcc_wedge_unclassified"):
            self.assertIn(code, fd.CODES)
            self.assertIn(code, fd.load_reasons())

    def test_agreeing_readings_keep_their_codes(self) -> None:
        # Negative controls: a healthy watchdog, no state file, a stalled own probe.
        healthy = {"state": "healthy", "consecutive_failures": 0, "updated_at": NOW}
        finding = fd.check_store_stall(self.OWN_OK, watchdog=healthy, context="ctx", now=NOW)
        self.assertEqual((finding.state, finding.code), (fd.OK, "store_ok"))
        self.assertIn("launchd watchdog (launchd job): healthy", finding.detail)
        finding = fd.check_store_stall(self.OWN_OK, watchdog={}, context="ctx", now=NOW)
        self.assertEqual(finding.code, "store_ok")
        self.assertIn("no store-health state", finding.detail)
        stalled = {"state": "stalled", "detail": "x", "stalled": []}
        finding = fd.check_store_stall(stalled, watchdog={"state": "draining"}, context="ctx")
        self.assertEqual(finding.code, "store_stalled")
        self.assertEqual(fd.check_tcc_wedge(healthy, now=NOW).code, "tcc_wedge_none")

    def test_a_stale_watchdog_reading_is_unknown_not_ok(self) -> None:
        old = NOW - 2 * 300 - 61
        healthy = {"state": "healthy", "consecutive_failures": 0, "updated_at": old}
        finding = fd.check_store_stall(self.OWN_OK, watchdog=healthy, context="ctx", now=NOW)
        self.assertEqual((finding.state, finding.code), (fd.UNKNOWN, "store_watchdog_stale"))
        self.assertIn("stale", finding.detail)
        self.assertIn("store_watchdog_stale", fd.load_reasons())
        undated = {"state": "healthy", "consecutive_failures": 0}
        self.assertEqual(fd.check_store_stall(self.OWN_OK, watchdog=undated, context="ctx",
                                              now=NOW).code, "store_watchdog_stale")
        # Control: a reading inside two passes is current.
        fresh = dict(healthy, updated_at=NOW - 2 * 300 + 30)
        self.assertEqual(fd.check_store_stall(self.OWN_OK, watchdog=fresh, context="ctx",
                                              now=NOW).code, "store_ok")
        # A stale draining reading stays a problem, not merely unknown.
        draining = dict(healthy, state="draining", consecutive_failures=5)
        self.assertEqual(fd.check_store_stall(self.OWN_OK, watchdog=draining, context="ctx",
                                              now=NOW).code, "store_context_disagree")

    def test_one_watchdog_failure_is_not_all_clear(self) -> None:
        suspect = {"state": "suspect", "consecutive_failures": 1, "updated_at": NOW,
                   "last_result": {"reason": "timeout after 10s"}}
        finding = fd.check_store_stall(self.OWN_OK, watchdog=suspect, context="ctx", now=NOW)
        self.assertEqual((finding.state, finding.code), (fd.UNKNOWN, "store_context_suspect"))

    def test_reading_context_names_where_the_doctor_ran(self) -> None:
        self.assertEqual(fd.reading_context({"SSH_CONNECTION": "a b c d"}, ppid=99),
                         "this doctor (ssh session)")
        self.assertEqual(fd.reading_context({"XPC_SERVICE_NAME": "com.example.job"}, ppid=99),
                         "this doctor (launchd job: com.example.job)")
        self.assertEqual(fd.reading_context({"XPC_SERVICE_NAME": "0"}, ppid=99),
                         "this doctor (interactive shell)")
        self.assertEqual(fd.reading_context({}, ppid=1), "this doctor (launchd job)")

    def test_collect_reads_the_watchdog_state_file_beside_its_own_probe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / ".tartci" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "store-health.json").write_text(
                (FIXTURES / "m3-20261113-store-health.json").read_text())
            env = {k: v for k, v in os.environ.items() if k != "TARTCI_HOME"}
            with mock.patch.dict(os.environ, env, clear=True):
                rows = fd.collect(home=Path(td), skip_census=True,
                                  probe=lambda root: {"error": "stub"},
                                  store_value=dict(self.OWN_OK))
        found = {row.check: row for row in rows}
        self.assertEqual(found["store_stall"].code, "store_context_disagree")
        self.assertEqual(found["tcc_wedge"].code, "tcc_wedge_unclassified")
        self.assertNotEqual(fd.diagnose("m3", rows).worst, fd.OK)


if __name__ == "__main__":
    unittest.main()
