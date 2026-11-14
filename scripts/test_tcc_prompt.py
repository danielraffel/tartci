#!/usr/bin/env python3
"""tcc_prompt: an unanswered privacy consent alert named as the cause of a store hang.

Every positive reading has a negative control: the same inputs with the alert
gone, the launchd hang gone, or the window server unreadable must not classify
as tcc_prompt_pending, must not page, and must not reach the heal.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bounded_subprocess  # noqa: E402
import fleet_doctor as fd  # noqa: E402
import tartci_launchd_watchdog as wd  # noqa: E402
import tcc_prompt as tp  # noqa: E402
import tcc_wedge as tw  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0
UNC = {"owner": tp.UNC_NAME, "pid": 58145, "path": tp.UNC_PATH, "window": 18645,
       "layer": 2147483631, "onscreen": True}
HUNG = {"outcome": "hung_on_root", "volume_root": "/Volumes/Example",
        "started_epoch": NOW - 12, "waited_s": 10.0}
ANSWERED = {"outcome": "answered", "volume_root": "/Volumes/Example", "root_latency_ms": 0.1}
FAST_ERROR = {"outcome": "root_error", "volume_root": "/Volumes/Example", "errno": 1,
              "error": "[Errno 1] Operation not permitted"}
PRESENT = {"state": "present", "since": "2027-01-15T08:00:00Z", "since_epoch": NOW - 300,
           "windows": [UNC], "total_windows": 140}


def ok_control(path, timeout_s):
    return {"ok": True, "path": path, "latency_ms": 0.05}


def no_errors(window_s, before=None):
    return {"ok": True, "window_s": window_s, "total": 0, "by_signature": {}}


class Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class WindowReadingTests(unittest.TestCase):
    def test_an_on_screen_notification_center_window_is_an_alert(self) -> None:
        reading = tp.judge_windows({"total": 140, "windows": [UNC]})
        self.assertEqual(reading["state"], "present")
        self.assertEqual(reading["onscreen"], 1)

    def test_an_ordered_out_window_or_none_is_absent(self) -> None:
        # The process stays resident for days and keeps ordered-out windows.
        for windows in ([], [dict(UNC, onscreen=False)]):
            with self.subTest(windows=windows):
                self.assertEqual(tp.judge_windows({"total": 140, "windows": windows})["state"],
                                 "absent")

    def test_a_listing_with_no_windows_is_unavailable_not_absent(self) -> None:
        # Negative control for the reader itself: a context that cannot see the
        # window server lists nothing, and that must never read as "no alert".
        for listing in ({"total": 0, "windows": []}, {}, {"total": "x"}):
            with self.subTest(listing=listing):
                self.assertEqual(tp.judge_windows(listing)["state"], "unavailable")

    def test_the_child_is_bounded_and_a_failure_is_unavailable(self) -> None:
        calls = []

        def runner(argv, timeout, operation):
            calls.append((argv, timeout, operation))
            return Completed(json.dumps({"total": 3, "windows": [UNC]}))
        reading = tp.read_windows(runner=runner, python="/usr/bin/python3")
        self.assertEqual(reading["state"], "present")
        argv, timeout, operation = calls[0]
        self.assertEqual(argv[:3], ["/usr/bin/python3", "-I", "-B"])
        self.assertEqual(argv[-1], "--windows-json")
        self.assertEqual(timeout, tp.WINDOW_READ_TIMEOUT_S)

        def hangs(argv, timeout, operation):
            raise bounded_subprocess.ObservationError("timeout", operation, "did not finish")
        self.assertEqual(tp.read_windows(runner=hangs)["state"], "unavailable")
        self.assertEqual(tp.read_windows(runner=lambda *a, **k: Completed("not json"))["state"],
                         "unavailable")
        self.assertEqual(tp.read_windows(runner=lambda *a, **k: Completed("", 1, "boom"))["state"],
                         "unavailable")

    @unittest.skipUnless(sys.platform == "darwin", "the window server is macOS only")
    def test_the_real_child_reads_the_window_server(self) -> None:
        reading = tp.read_windows()
        self.assertIn(reading["state"], ("present", "absent", "unavailable"), reading)
        if reading["state"] != "unavailable":
            self.assertGreater(reading["total_windows"], 0)


class ShipyardInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make(self, name: str, at: float) -> None:
        path = self.root / name
        path.mkdir()
        os.utime(path, (at, at))

    def test_generations_newest_first_and_other_entries_ignored(self) -> None:
        self.make("a" * 64, NOW - 3000)
        self.make("b" * 64, NOW - 100)
        self.make("rollback-v0.127.2", NOW)
        (self.root / ("c" * 64)).write_text("not a directory")
        installs = tp.shipyard_installs(self.root)
        self.assertEqual([row["generation"] for row in installs], ["b" * 64, "a" * 64])
        self.assertTrue(installs[0]["binary"].endswith("b" * 64 + "/shipyard"))
        # birth time where the filesystem keeps one; utime only moves mtime
        # (NOW is a fixed instant that may lie after the wall clock)
        self.assertLessEqual(installs[0]["installed_epoch"], max(time.time(), NOW) + 1)

    def test_an_alert_is_attributed_only_within_ten_minutes_after_an_install(self) -> None:
        installs = [{"generation": "b" * 64, "installed_epoch": NOW - 240,
                     "installed_at": "x", "binary": "y"}]
        hit = tp.install_before(NOW, installs)
        self.assertEqual(hit["seconds_before_alert"], 240)
        self.assertIsNotNone(tp.install_before(NOW - 240 + 600, installs))
        self.assertIsNone(tp.install_before(NOW - 240 + 601, installs), "too long after")
        self.assertIsNone(tp.install_before(NOW - 241, installs), "the alert came first")

    def test_a_missing_directory_is_no_installs(self) -> None:
        self.assertEqual(tp.shipyard_installs(self.root / "missing"), [])


class ObserveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def observe(self, state, now, installs=()):
        return tp.observe(now, state_dir=self.state,
                          windows=lambda: {"state": state, "windows": [UNC],
                                           "total_windows": 140},
                          installs=lambda: list(installs))

    def test_the_episode_starts_at_the_first_pass_and_ends_when_absent(self) -> None:
        first = self.observe("present", NOW)
        self.assertEqual(first["since_epoch"], NOW)
        later = self.observe("present", NOW + 300)
        self.assertEqual(later["since_epoch"], NOW, "the start is kept across passes")
        blind = self.observe("unavailable", NOW + 600)
        self.assertEqual(blind["since_epoch"], NOW, "an unreadable pass decides nothing")
        gone = self.observe("absent", NOW + 900)
        self.assertNotIn("since_epoch", gone)
        self.assertFalse((self.state / tp.STATE_NAME).exists())
        again = self.observe("present", NOW + 1200)
        self.assertEqual(again["since_epoch"], NOW + 1200, "a new alert is a new episode")

    def test_the_install_is_matched_against_the_episode_start_not_now(self) -> None:
        install = {"generation": "d" * 64, "installed_epoch": NOW - 120,
                   "installed_at": "x", "binary": "y"}
        self.observe("present", NOW, installs=[install])
        # Forty minutes later the alert is still the one that followed the install.
        later = self.observe("present", NOW + 2400, installs=[install])
        self.assertEqual(later["shipyard_install"]["generation"], "d" * 64)
        # Control: an install long before the alert is not its cause.
        self.observe("absent", NOW + 2700)
        unrelated = self.observe("present", NOW + 3000, installs=[install])
        self.assertNotIn("shipyard_install", unrelated)

    def test_with_an_alert_up_the_launchd_job_decides_the_hang(self) -> None:
        calls = []

        def run(outcome):
            return lambda: calls.append(outcome) or {"outcome": outcome}
        first = tp.observe(NOW, state_dir=self.state, installs=list,
                           windows=lambda: {"state": "present", "total_windows": 9},
                           launchd=run("hung_on_root"))
        self.assertTrue(first["hang"])
        self.assertEqual(first["pending_since_epoch"], NOW)
        later = tp.observe(NOW + 300, state_dir=self.state, installs=list,
                           windows=lambda: {"state": "present", "total_windows": 9},
                           launchd=run("hung_on_root"))
        self.assertEqual(later["pending_since_epoch"], NOW, "the hang keeps its start")
        answered = tp.observe(NOW + 600, state_dir=self.state, installs=list,
                              windows=lambda: {"state": "present", "total_windows": 9},
                              launchd=run("answered"))
        self.assertFalse(answered["hang"])
        self.assertNotIn("pending_since_epoch", answered, "an answering job ends the hang")
        # No alert: the launchd job is never submitted.
        tp.observe(NOW + 900, state_dir=self.state, installs=list,
                   windows=lambda: {"state": "absent", "total_windows": 9},
                   launchd=run("hung_on_root"))
        self.assertEqual(calls, ["hung_on_root", "hung_on_root", "answered"])

    def test_a_granted_context_that_hangs_too_is_not_the_alert(self) -> None:
        reading = tp.observe(NOW, state_dir=self.state, installs=list,
                             windows=lambda: {"state": "present", "total_windows": 9},
                             launchd=lambda: {"outcome": "hung_on_root"},
                             context=lambda: {"ok": False, "reason": "timeout"})
        self.assertFalse(reading["hang"])
        reading = tp.observe(NOW, state_dir=self.state, installs=list,
                             windows=lambda: {"state": "present", "total_windows": 9},
                             launchd=lambda: {"outcome": "hung_on_root"},
                             context=lambda: {"ok": True})
        self.assertTrue(reading["hang"])

    def test_a_status_pass_saves_nothing(self) -> None:
        tp.observe(NOW, state_dir=self.state, save=False,
                   windows=lambda: {"state": "present", "total_windows": 1}, installs=list)
        self.assertFalse((self.state / tp.STATE_NAME).exists())


def classify(alert, launchd, context=None, control=ok_control, disabled=False):
    calls = []

    def probe():
        calls.append(1)
        return dict(launchd)
    verdict = tw.classify(consecutive_failures=3, failure_threshold=2,
                          last_reason="timeout after 10s", failing_since=NOW - 900,
                          control_path="/internal/state", now=NOW, scan=no_errors,
                          control=control, context_probe=context,
                          launchd_probe=None if launchd is None else probe,
                          launchd_probe_disabled=disabled,
                          ipc_scan=lambda start, end: {"state": "absent"},
                          consent_alert=None if alert is None else (lambda: dict(alert)))
    return verdict, calls


class ClassifyPromptTests(unittest.TestCase):
    def test_alert_on_screen_and_a_launchd_hang_is_prompt_pending(self) -> None:
        verdict, calls = classify(PRESENT, HUNG)
        self.assertEqual(verdict["kind"], tw.PROMPT_KIND)
        self.assertEqual(verdict["path"], "consent_alert")
        self.assertEqual(calls, [1], "the launchd job runs once, even with no context probe")
        self.assertIn("/Volumes/Example", verdict["reason"])
        self.assertEqual(verdict["evidence"]["consent_alert"]["state"], "present")

    def test_a_shipyard_install_is_named_in_the_verdict(self) -> None:
        alert = dict(PRESENT, shipyard_install={"generation": "e" * 64,
                                                "seconds_before_alert": 42})
        verdict, _ = classify(alert, HUNG)
        self.assertEqual(verdict["kind"], tw.PROMPT_KIND)
        self.assertIn("42 s after Shipyard installed generation eeeeeeeeeeee", verdict["reason"])

    def test_a_granted_context_that_answers_still_allows_the_verdict(self) -> None:
        verdict, _ = classify(PRESENT, HUNG, context=lambda: {"ok": True})
        self.assertEqual(verdict["kind"], tw.PROMPT_KIND)

    def test_every_missing_or_disagreeing_reading_withholds_it(self) -> None:
        cases = {
            "no alert": ({"state": "absent", "total_windows": 140}, HUNG, None),
            "window server unreadable": ({"state": "unavailable"}, HUNG, None),
            "no alert reading at all": (None, HUNG, None),
            "launchd job opened the root": (PRESENT, ANSWERED, None),
            "launchd job failed fast": (PRESENT, FAST_ERROR, None),
            "launchd job did not start": (PRESENT, {"outcome": "did_not_start"}, None),
            "no launchd probe": (PRESENT, None, None),
            "granted context hangs too": (PRESENT, HUNG,
                                          lambda: {"ok": False, "reason": "timeout"}),
        }
        for name, (alert, launchd, context) in cases.items():
            with self.subTest(name):
                verdict, _ = classify(alert, launchd, context)
                self.assertNotEqual(verdict["kind"], tw.PROMPT_KIND, name)
                self.assertNotEqual(verdict["kind"], tw.KIND, name)

    def test_the_alert_pass_launchd_reading_is_reused_not_run_again(self) -> None:
        verdict, calls = classify(dict(PRESENT, launchd_probe=dict(HUNG)), HUNG)
        self.assertEqual(verdict["kind"], tw.PROMPT_KIND)
        self.assertEqual(calls, [], "one launchd job per pass")
        verdict, calls = classify(dict(PRESENT, launchd_probe=dict(ANSWERED)), HUNG)
        self.assertNotEqual(verdict["kind"], tw.PROMPT_KIND)
        self.assertEqual(calls, [])

    def test_a_disabled_launchd_probe_is_never_run_for_the_alert(self) -> None:
        verdict, calls = classify(PRESENT, HUNG, disabled=True)
        self.assertEqual(calls, [])
        self.assertEqual(verdict["kind"], "stalled_unclassified")

    def test_a_hanging_control_path_comes_first(self) -> None:
        verdict, calls = classify(PRESENT, HUNG,
                                  control=lambda path, t: {"ok": False, "reason": "timeout"})
        self.assertEqual(verdict["kind"], "host_unresponsive")
        self.assertEqual(calls, [])

    def test_without_an_alert_the_launchd_job_still_needs_a_context(self) -> None:
        # Negative control: the 841 rule is unchanged when no alert is up.
        verdict, calls = classify({"state": "absent", "total_windows": 140}, HUNG)
        self.assertEqual(calls, [])
        self.assertEqual(verdict["evidence"]["launchd_probe"]["outcome"], "skipped")


class PromptHealTests(unittest.TestCase):
    """The heal is the fallback: it waits for the page to be answered."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.healed: list = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def heal(self, **kw):
        self.healed.append(kw)
        return {"outcome": "ran", "at": tw._iso(kw["now"]), "rc": 0}

    def run_pass(self, now, reading=None, store_state=None, **kw):
        reading = reading or dict(PRESENT, hang=True, pending_since_epoch=NOW)
        return tp.alert_pass(reading, store_state, now=now, state_dir=self.state, host="m3",
                             issue=lambda t, b: (0, "7"), close=lambda n: (0, ""),
                             heal=self.heal, heal_after_s=900, **kw)

    def test_waits_for_an_answer_then_heals_once_per_episode(self) -> None:
        line = self.run_pass(NOW + 899)
        self.assertIn("heal waiting_for_answer", line)
        self.assertEqual(self.healed, [])
        line = self.run_pass(NOW + 900)
        self.assertIn("heal ran", line)
        self.assertEqual(len(self.healed), 1)
        line = self.run_pass(NOW + 1200)
        self.assertIn("already_ran_this_episode", line)
        self.assertEqual(len(self.healed), 1)
        # A new alert episode may heal again; the budget record is handed on.
        later = dict(PRESENT, since_epoch=NOW + 4000, hang=True, pending_since_epoch=NOW + 4000)
        self.run_pass(NOW + 5000, reading=later)
        self.assertEqual(len(self.healed), 2)
        self.assertEqual(self.healed[1]["last_heal"]["at_epoch"], NOW + 900)

    def test_the_store_pass_heal_record_is_the_same_budget(self) -> None:
        store = {"last_heal": {"at": "x", "at_epoch": NOW + 800, "outcome": "ran"}}
        self.run_pass(NOW + 900, store_state=store)
        self.assertEqual(self.healed[0]["last_heal"]["at_epoch"], NOW + 800)

    def test_no_heal_without_a_hang_or_when_disabled(self) -> None:
        # Paged for the Shipyard install alone: nothing waits on the alert yet.
        install = {"generation": "a" * 64, "installed_at": "t", "binary": "b",
                   "seconds_before_alert": 5}
        self.run_pass(NOW + 3600, reading=dict(PRESENT, shipyard_install=install))
        line = self.run_pass(NOW + 3600, heal_enabled=False)
        self.assertIn("heal disabled", line)
        self.assertEqual(self.healed, [])

    def test_the_watchdog_step_never_heals_a_prompt_itself(self) -> None:
        state = {"state": "draining", "consecutive_failures": 3, "drain_required": True,
                 "failure_threshold": 2, "path": "/Volumes/Example/VMs",
                 "last_result": {"ok": False, "reason": "timeout after 10s"}}
        healed: list = []
        tw.watchdog_step(state, {"failing_since": NOW - 3600}, control_path="/x",
                         store_timeout_s=10.0, dry_run=False, now=NOW,
                         classify_fn=lambda **kw: {"kind": tw.PROMPT_KIND, "reason": "r",
                                                   "evidence": {},
                                                   "classified_at": tw._iso(NOW)},
                         heal_fn=lambda **kw: healed.append(kw) or {"outcome": "ran"})
        self.assertEqual(healed, [])
        self.assertEqual(state["heal"]["outcome"], "paged")


class AlertPassTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.opened: list = []
        self.closed: list = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def issue(self, title, body):
        self.opened.append((title, body))
        return 0, str(100 + len(self.opened))

    def close(self, number):
        self.closed.append(number)
        return 0, "closed"

    def run_pass(self, reading, store_state=None, now=NOW):
        return tp.alert_pass(reading, store_state, now=now, state_dir=self.state,
                             host="m3", issue=self.issue, close=self.close,
                             heal=lambda **kw: self.fail("no heal in a paging test"))

    def verdict(self, at=NOW):
        return {"classification": {"kind": tw.PROMPT_KIND, "classified_at": tw._iso(at),
                                   "reason": "alert + launchd hang"}}

    def test_a_classified_prompt_pages_once_per_episode_and_closes_when_gone(self) -> None:
        line = self.run_pass(PRESENT, self.verdict())
        self.assertIn("WARN tcc-prompt", line)
        self.assertIn("issue #101", line)
        self.run_pass(PRESENT, self.verdict(), now=NOW + 300)
        self.assertEqual(len(self.opened), 1, "one issue per episode")
        title, body = self.opened[0]
        self.assertEqual(title, tp.TITLE.format(host="m3"))
        self.assertIn("click **Allow**", body)
        self.assertIn("Do not click Don't Allow", body)
        self.assertIn('"class":"tcc_prompt_pending"', body)
        self.assertIsNone(self.run_pass({"state": "absent", "total_windows": 140}, None,
                                        now=NOW + 600))
        self.assertEqual(self.closed, ["101"])

    def test_an_alert_right_after_a_shipyard_install_pages_before_any_hang(self) -> None:
        reading = dict(PRESENT, shipyard_install={
            "generation": "f" * 64, "installed_epoch": NOW - 400, "installed_at": "t",
            "binary": "/home/.local/share/shipyard/auth-generations/" + "f" * 64 + "/shipyard",
            "seconds_before_alert": 100})
        line = self.run_pass(reading, None)
        self.assertIn("after Shipyard install ffffffffffff", line)
        self.assertEqual(len(self.opened), 1)
        self.assertIn("auth-generations/" + "f" * 64 + "/shipyard", self.opened[0][1])

    def test_this_pass_launchd_hang_pages_without_the_watchdog_verdict(self) -> None:
        # The watchdog's own probe may hold a grant and answer; the fresh
        # launchd job is enough.
        line = self.run_pass(dict(PRESENT, hang=True, pending_since_epoch=NOW), None)
        self.assertIn("launchd store opens hang behind it", line)
        self.assertEqual(len(self.opened), 1)

    def test_no_page_without_a_hang_or_an_install(self) -> None:
        # Negative controls: an alert alone, a stale verdict, a blind reader.
        self.assertIsNone(self.run_pass(PRESENT, None))
        stale = self.verdict(at=NOW - tw.FRESH_S - 1)
        self.assertIsNone(self.run_pass(PRESENT, stale))
        other = {"classification": {"kind": tw.KIND, "classified_at": tw._iso(NOW)}}
        self.assertIsNone(self.run_pass(PRESENT, other))
        self.assertIsNone(self.run_pass({"state": "unavailable"}, self.verdict()))
        self.assertEqual(self.opened, [])

    def test_an_unreadable_window_server_does_not_close_the_issue(self) -> None:
        self.run_pass(PRESENT, self.verdict())
        self.run_pass({"state": "unavailable"}, self.verdict(), now=NOW + 300)
        self.assertEqual(self.closed, [])

    def test_the_issue_switch_stops_the_issue_not_the_warning(self) -> None:
        os.environ["TARTCI_TCC_PROMPT_ISSUE"] = "0"
        try:
            line = self.run_pass(PRESENT, self.verdict())
        finally:
            del os.environ["TARTCI_TCC_PROMPT_ISSUE"]
        self.assertIn("WARN tcc-prompt", line)
        self.assertEqual(self.opened, [])


class SurfaceTests(unittest.TestCase):
    def test_lane_cause_names_the_prompt_and_its_heal_outcome(self) -> None:
        state = {"state": "draining", "heal": {"outcome": "paged"},
                 "classification": {"kind": tw.PROMPT_KIND, "classified_at": tw._iso(NOW)}}
        self.assertEqual(tw.lane_cause(state, now=NOW), f"{tw.PROMPT_KIND} paged")
        state["state"] = "healthy"
        self.assertEqual(tw.lane_cause(state, now=NOW), "")

    def test_doctor_reports_a_problem_with_a_reason_row(self) -> None:
        state = {"state": "draining", "heal": {"outcome": "paged"},
                 "classification": {"kind": tw.PROMPT_KIND, "classified_at": tw._iso(NOW),
                                    "reason": "alert"}}
        finding = fd.check_tcc_wedge(state, now=NOW)
        self.assertEqual(finding.code, "tcc_prompt_pending")
        self.assertEqual(finding.state, fd.PROBLEM)
        self.assertIn("paged", finding.detail)
        self.assertIn("tcc_prompt_pending", fd.CODES)
        self.assertIn("tcc_prompt_pending", fd.load_reasons())
        # Control: a stale verdict is not reported as pending.
        stale = dict(state, classification=dict(state["classification"],
                                                classified_at=tw._iso(NOW - tw.FRESH_S - 1)))
        self.assertNotEqual(fd.check_tcc_wedge(stale, now=NOW).code, "tcc_prompt_pending")

    def test_runner_names_the_prompt_with_the_blocked_phase(self) -> None:
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        gate = runner[runner.index("tartci_storage_health_check(){"):]
        gate = gate[:gate.index("\n}\n")]
        branch = gate[gate.index('"tcc_prompt_pending "*)'):]
        branch = branch[:branch.index(";;")]
        self.assertIn("event tcc_prompt_pending", branch)
        self.assertIn("heartbeat tcc-attribution-wedge", branch)

    def test_the_watchdog_reads_once_feeds_the_classifier_and_pages_early(self) -> None:
        source = (ROOT / "scripts" / "tartci_launchd_watchdog.py").read_text()
        main = source[source.index("def main("):]
        self.assertLess(main.index("consent_alert_reading("), main.index("store_health_pass("))
        self.assertLess(main.index("store_health_pass("), main.index("tcc_prompt_pass("))
        self.assertLess(main.index("tcc_prompt_pass("), main.index("peer_stall_pass()"))
        self.assertIn("consent_alert=consent_alert", main)

    def store_passes(self, alert):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "state-home"
            probes = [{"ok": False, "reason": "timeout after 10s"}] * 2
            env = {k: v for k, v in os.environ.items() if k != "TARTCI_TCC_CONTEXT_PROBE"}
            env["TARTCI_HOME"] = str(home)
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(wd, "resolve_tart_home",
                                      return_value=("/Volumes/Example/VMs", "test")), \
                    mock.patch.object(wd.store_health, "probe", side_effect=probes), \
                    mock.patch.object(tw, "launchd_probe",
                                      lambda store, internal, timeout_s: dict(HUNG)), \
                    mock.patch.object(tw, "scan_log", no_errors), \
                    mock.patch.object(tw, "control_probe", ok_control), \
                    mock.patch.object(tw, "heal", side_effect=AssertionError("no heal yet")):
                return [wd.store_health_pass(
                    participation_file=str(Path(td) / "participation"), status_only=False,
                    dry_run=False, failure_threshold=2, consent_alert=alert)
                    for _ in range(2)][-1]

    def test_the_store_pass_classifies_a_prompt_from_this_pass_reading(self) -> None:
        second = self.store_passes(PRESENT)
        self.assertEqual(second["classification"]["kind"], tw.PROMPT_KIND)
        self.assertEqual(second["heal"]["outcome"], "paged")
        # Control: the same hang with no alert reading is the 841 verdict.
        self.assertEqual(self.store_passes(None)["classification"]["kind"],
                         "stalled_unclassified")

    def test_the_watchdog_reading_carries_the_store_launchd_probe(self) -> None:
        from unittest import mock
        seen = {}
        with mock.patch.object(wd, "resolve_tart_home",
                               return_value=("/Volumes/Example/VMs", "test")), \
                mock.patch.dict(os.environ, {"TARTCI_HOME": "/internal/home",
                                             "TARTCI_TCC_LAUNCHD_PROBE": "1"}):
            wd.consent_alert_reading(observe=lambda **kw: seen.update(kw) or {}, timeout_s=7.0)
        self.assertTrue(callable(seen["launchd"]))
        with mock.patch.object(tw, "launchd_probe",
                               lambda store, internal, timeout_s: {
                                   "args": (store, internal, timeout_s)}):
            self.assertEqual(seen["launchd"]()["args"],
                             ("/Volumes/Example/VMs", "/internal/home/state", 7.0))
        # Control: the probe switch turns it off.
        with mock.patch.object(wd, "resolve_tart_home",
                               return_value=("/Volumes/Example/VMs", "test")), \
                mock.patch.dict(os.environ, {"TARTCI_TCC_LAUNCHD_PROBE": "0"}):
            wd.consent_alert_reading(observe=lambda **kw: seen.update(kw) or {})
        self.assertIsNone(seen["launchd"])

    def test_the_watchdog_wrappers_never_raise(self) -> None:
        def boom(**kw):
            raise RuntimeError("no window server")
        self.assertIsNone(wd.consent_alert_reading(observe=boom))
        self.assertIsNone(wd.tcc_prompt_pass(None, {}))
        line = wd.tcc_prompt_pass({"state": "present"}, {"classification": "not a dict"},
                                  issue=lambda *a: (1, ""), close=lambda *a: (1, ""))
        self.assertTrue(line is None or "tcc-prompt" in line)


if __name__ == "__main__":
    unittest.main()
