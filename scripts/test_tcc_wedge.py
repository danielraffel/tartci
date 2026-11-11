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
            (self.wedge_state(age_s=tw.FRESH_S + 1), fd.OK, "tcc_wedge_none"),
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


if __name__ == "__main__":
    unittest.main()
