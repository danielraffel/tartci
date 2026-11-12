#!/usr/bin/env python3
"""A host a failed self-update left OFF is detected, recovered and reported.

m3, 2026-09-29: two failed updates left the host OFF from 08:40Z to 17:16Z,
the same-target guard refused every scheduled run before it looked at the
pool, and the only signal was a watchdog log line.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host_off  # noqa: E402
import macos_launcher_probe  # noqa: E402
import tartci_launchd_watchdog as wd  # noqa: E402
import testing_support  # noqa: E402

LEFT_AT = 1_790_000_000.0


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sdir = self.tmp / "state" / "self-update"
        self.sdir.mkdir(parents=True)
        self.pool = self.tmp / "pool-state"

    def left_off(self, pool: str = "off", pool_mtime: float | None = None) -> None:
        (self.sdir / "last.json").write_text(json.dumps({
            "status": "failed", "target": "c" * 40, "host_off": True, "pool_state": "off",
            "at": host_off._iso(LEFT_AT),
            "error": "pool on failed after install; ROLLBACK FAILED"}))
        self.pool.write_text(pool + "\n")
        stamp = LEFT_AT - 30 if pool_mtime is None else pool_mtime
        os.utime(self.pool, (stamp, stamp))

    def events(self) -> list[str]:
        path = self.sdir / "events.jsonl"
        return [json.loads(line)["event"] for line in path.read_text().splitlines()] \
            if path.exists() else []


class StatusTests(Fixture):
    def test_left_off_is_unexpected_and_loud_after_fifteen_minutes(self) -> None:
        self.left_off()
        early = host_off.status(self.sdir, self.pool, LEFT_AT + 600)
        self.assertTrue(early["unexpected"])
        self.assertFalse(early["loud"])
        late = host_off.status(self.sdir, self.pool, LEFT_AT + 16 * 60)
        self.assertTrue(late["loud"])
        self.assertEqual(late["minutes"], 16)
        self.assertIn("ROLLBACK FAILED", late["detail"])

    def test_a_deliberate_pool_off_is_not_unexpected(self) -> None:
        self.left_off(pool_mtime=LEFT_AT + 3600)
        self.assertFalse(host_off.status(self.sdir, self.pool, LEFT_AT + 7200)["unexpected"])

    def test_pool_on_again_or_no_record_is_quiet(self) -> None:
        self.left_off(pool="on")
        self.assertFalse(host_off.status(self.sdir, self.pool, LEFT_AT + 7200)["unexpected"])
        (self.sdir / "last.json").unlink()
        self.assertFalse(host_off.status(self.sdir, self.pool, LEFT_AT + 7200)["unexpected"])


class RecoverTests(Fixture):
    def test_pool_on_is_tried_and_success_clears_the_record(self) -> None:
        self.left_off()
        calls = []

        def pool_on():
            calls.append(1)
            self.pool.write_text("on\n")
            return 0, "state: on"
        out = host_off.recover(self.sdir, self.pool, pool_on, now=LEFT_AT + 1800, who="t")
        self.assertEqual((out["attempted"], out["ok"], len(calls)), (True, True, 1))
        last = json.loads((self.sdir / "last.json").read_text())
        self.assertFalse(last["host_off"])
        self.assertIn("host_off_recovered", self.events())

    def test_nothing_is_tried_on_a_healthy_host(self) -> None:
        calls = []
        out = host_off.recover(self.sdir, self.pool, lambda: calls.append(1) or (0, ""),
                               now=LEFT_AT)
        self.assertEqual((out["attempted"], calls), (False, []))

    def test_backoff_between_attempts(self) -> None:
        self.left_off()
        calls = []

        def failing():
            calls.append(1)
            return 9, "launch helper volume probe timed out"
        host_off.recover(self.sdir, self.pool, failing, now=LEFT_AT + 60)
        host_off.recover(self.sdir, self.pool, failing, now=LEFT_AT + 200)
        self.assertEqual(len(calls), 1)
        host_off.recover(self.sdir, self.pool, failing, now=LEFT_AT + 60 + 300)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.events().count("host_off_recovery_failed"), 2)

    def test_our_own_failed_pool_on_rewriting_the_state_is_not_deliberate(self) -> None:
        self.left_off()

        def failing_and_rewriting():
            self.pool.write_text("off\n")
            os.utime(self.pool, (LEFT_AT + 3000, LEFT_AT + 3000))
            return 7, "rolled back"
        host_off.recover(self.sdir, self.pool, failing_and_rewriting, now=LEFT_AT + 3000)
        self.assertTrue(host_off.status(self.sdir, self.pool, LEFT_AT + 3600)["unexpected"])

    def test_a_running_self_update_is_left_alone(self) -> None:
        self.left_off()
        (self.sdir / "active.json").write_text(json.dumps({"pid": os.getpid()}))
        calls = []
        out = host_off.recover(self.sdir, self.pool, lambda: calls.append(1) or (0, ""),
                               now=LEFT_AT + 1800)
        self.assertEqual((out["attempted"], calls), (False, []))


class AlertTests(Fixture):
    def test_one_event_and_one_issue_per_episode_then_closed_on_recovery(self) -> None:
        self.left_off()
        opened, closed = [], []

        def issue(title, body):
            opened.append(title)
            return 0, "42"
        host_off.alert(self.sdir, self.pool, "studio", LEFT_AT + 300, issue=issue,
                       close=closed.append)
        self.assertEqual((self.events(), opened), ([], []))   # not loud yet
        host_off.alert(self.sdir, self.pool, "studio", LEFT_AT + 16 * 60, issue=issue,
                       close=lambda n: closed.append(n) or (0, "closed"))
        host_off.alert(self.sdir, self.pool, "studio", LEFT_AT + 21 * 60, issue=issue,
                       close=lambda n: closed.append(n) or (0, "closed"))
        self.assertEqual(self.events().count("host_off_unexpected"), 1)
        self.assertEqual(len(opened), 1)
        self.assertIn("studio", opened[0])
        self.pool.write_text("on\n")
        host_off.alert(self.sdir, self.pool, "studio", LEFT_AT + 30 * 60, issue=issue,
                       close=lambda n: closed.append(n) or (0, "closed"))
        self.assertEqual(closed, ["42"])
        self.assertFalse((self.sdir / "host-off-alert.json").exists())

    def test_immediate_alert_opens_before_the_loudness_deadline(self) -> None:
        self.left_off()
        opened = []
        host_off.alert(self.sdir, self.pool, "studio", LEFT_AT + 30,
                       issue=lambda title, body: opened.append((title, body)) or (0, "9"),
                       immediate=True)
        self.assertEqual(len(opened), 1)
        self.assertIn("studio", opened[0][0])
        self.assertIn("Remedy:", opened[0][1])

    def test_a_failed_issue_is_retried_next_pass(self) -> None:
        self.left_off()
        results = iter([(1, "provenance required"), (0, "7")])
        host_off.alert(self.sdir, self.pool, "m1", LEFT_AT + 20 * 60,
                       issue=lambda t, b: next(results))
        out = host_off.alert(self.sdir, self.pool, "m1", LEFT_AT + 25 * 60,
                             issue=lambda t, b: next(results))
        self.assertEqual(out["issue"], "7")


class SurfaceTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.tmp),
                                                "TARTCI_POOL_STATE_FILE": str(self.pool)})
        self.env.start()
        self.addCleanup(self.env.stop)

    @testing_support.requires_tomllib
    def test_pool_status_names_the_reason(self) -> None:
        import macos_fleet_lanes as fleet
        self.left_off()
        problem = fleet.host_off_problem("off")
        self.assertEqual(problem["code"], "host_off_unexpected")
        self.assertIn("failed self-update left this host OFF", problem["detail"])
        self.pool.write_text("on\n")
        self.assertIsNone(fleet.host_off_problem("on"))

    @testing_support.requires_tomllib
    def test_a_check_that_raises_is_a_problem_not_a_clean_bill(self) -> None:
        import macos_fleet_lanes as fleet
        with mock.patch.object(host_off, "status", side_effect=OSError("state dir unreadable")):
            problem = fleet.host_off_problem("off")
        self.assertEqual(problem["code"], "host_off_unverified")
        self.assertIn("state dir unreadable", problem["detail"])

    def test_a_check_that_raises_warns_in_the_watchdog(self) -> None:
        with mock.patch.object(host_off, "status", side_effect=OSError("state dir unreadable")):
            line = wd.host_off_pass(now=time.time())
        self.assertIn("WARN host-off check FAILED", line)

    def test_the_watchdog_recovers_and_warns_every_pass(self) -> None:
        self.left_off()
        with mock.patch.object(wd, "_pool_on", return_value=(9, "volume probe timed out")), \
                mock.patch.object(host_off, "_open_issue", return_value=(0, "5")):
            line = wd.host_off_pass(now=time.time())
        self.assertIn("WARN host-off", line)
        self.assertIn("pool on failed", line)
        self.assertIn("host_off_recovery_failed", self.events())
        self.assertIn("host_off_unexpected", self.events())

        def recovered():
            self.pool.write_text("on\n")
            return 0, "state: on"
        (self.sdir / "recovery.json").unlink()
        with mock.patch.object(wd, "_pool_on", side_effect=recovered), \
                mock.patch.object(host_off, "_close_issue", return_value=(0, "closed")) as close:
            line = wd.host_off_pass(now=time.time())
        self.assertIn("pool on succeeded", line)
        close.assert_called_once_with("5")

    def test_status_only_never_acts(self) -> None:
        self.left_off()
        with mock.patch.object(wd, "_pool_on") as pool_on:
            line = wd.host_off_pass(status_only=True, now=time.time())
        pool_on.assert_not_called()
        self.assertIn("WARN host-off", line)


class ScratchHomeGuardTests(unittest.TestCase):
    def test_a_test_run_never_writes_an_issue_whatever_it_forgot_to_stub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "ghapp-calls"
            stub = Path(tmp) / "bin" / "ghapp"
            stub.parent.mkdir()
            stub.write_text(f"#!/bin/sh\necho \"$*\" >> {calls}\necho 99\n")
            stub.chmod(0o755)
            env = {"TARTCI_HOME": str(Path(tmp) / ".tartci"),
                   "PATH": f"{stub.parent}:{os.environ.get('PATH', '')}"}
            with mock.patch.dict(os.environ, env):
                rc, text = host_off._open_issue("t", "b")
                close_rc, _ = host_off._close_issue("5")
            self.assertEqual((rc, close_rc), (1, 1))
            self.assertIn("scratch TARTCI_HOME", text)
            self.assertFalse(calls.exists())

    def test_a_real_home_is_not_scratch(self) -> None:
        # Control: the guard keys on the temp dir, not on every override.
        with mock.patch.dict(os.environ, {"TARTCI_HOME": "/Users/someone/.tartci"}):
            self.assertFalse(host_off._scratch_home())


class WiringTests(unittest.TestCase):
    def test_readiness_and_the_watchdog_pass_consult_it(self) -> None:
        here = Path(__file__).resolve().parent
        lanes = (here / "macos_fleet_lanes.py").read_text()
        body = lanes[lanes.index("def fleet_readiness("):]
        body = body[:body.index("\ndef ")]
        self.assertRegex(body, r"(?s)left_off = host_off_problem\(pool_state\)\n\s+if left_off is not None:\n\s+problems.append\(left_off\)")
        watchdog = (here / "tartci_launchd_watchdog.py").read_text()
        main = watchdog[watchdog.index("def main("):]
        self.assertLess(main.index("host_off_pass("), main.index("discover_agents("))


class VolumeProbeTests(unittest.TestCase):
    helper = {"path": "/x/TartCILauncher.app", "sha256": "s", "designated_requirement_sha256": "d"}
    profile = {"host": {"log_root": tempfile.gettempdir(), "home": "/Users/x",
                        "tart_home": "/Volumes/Example/VMs"}}

    def fake_run(self, plists: list[dict]):
        missing = subprocess.CompletedProcess([], 113, "", "Could not find service")
        running = subprocess.CompletedProcess([], 0, "state = running\n", "")
        ok = subprocess.CompletedProcess([], 0, "", "")

        def run(argv, **kwargs):
            if argv[:2] == ["launchctl", "bootstrap"]:
                import plistlib
                plists.append(plistlib.loads(Path(argv[3]).read_bytes()))
                return ok
            if argv[:2] == ["launchctl", "bootout"]:
                return ok
            return missing if not plists else running
        return run

    def test_the_probe_is_not_background_throttled_and_says_why_it_timed_out(self) -> None:
        plists: list[dict] = []
        slow = {"ms": 4200.0}
        with mock.patch.object(macos_launcher_probe.subprocess, "run", self.fake_run(plists)), \
                mock.patch.object(macos_launcher_probe, "parent_write_ms", return_value=slow) as control:
            with self.assertRaisesRegex(ValueError, r"probe wait elapsed [0-9.]+s;.*volume I/O slow; "
                                        r"same-dir write from parent took 4200 ms"):
                macos_launcher_probe.run(self.helper, self.profile, timeout_seconds=0.3)
        control.assert_called_once_with(self.profile["host"]["tart_home"])
        self.assertEqual(plists[0]["ProcessType"], "Standard")

    def test_a_fast_control_write_blames_the_helper_not_the_volume(self) -> None:
        # m3: the helper sat in its own open() while a shell write to the same
        # directory took 32 ms.
        plists: list[dict] = []
        with mock.patch.object(macos_launcher_probe.subprocess, "run", self.fake_run(plists)), \
                mock.patch.object(macos_launcher_probe, "parent_write_ms", return_value={"ms": 32.4}):
            with self.assertRaises(ValueError) as raised:
                macos_launcher_probe.run(self.helper, self.profile, timeout_seconds=0.3)
        message = str(raised.exception)
        self.assertIn("helper blocked in open(); same-dir write from parent took 32 ms", message)
        self.assertIn("Grant Removable Volumes to TartCI Launcher", message)
        self.assertNotIn("volume I/O slow", message)

    def test_the_timeout_message_names_each_control_outcome(self) -> None:
        message = macos_launcher_probe.timeout_message
        self.assertIn("did not finish within 10s", message(60, 60.1, "/v", {"timed_out": True, "timeout": 10.0}))
        self.assertIn("volume I/O slow", message(60, 60.1, "/v", {"timed_out": True, "timeout": 10.0}))
        self.assertIn("failed too (PermissionError: x)", message(60, 60.1, "/v", {"error": "PermissionError: x"}))
        self.assertIn("helper blocked in open()", message(60, 60.1, "/v", {"ms": 999.0}))
        self.assertIn("volume I/O slow", message(60, 60.1, "/v", {"ms": 1000.0}))

    def test_the_control_write_is_real_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            measured = macos_launcher_probe.parent_write_ms(td)
            self.assertIn("ms", measured)
            self.assertEqual([], os.listdir(td))
            self.assertIn("error", macos_launcher_probe.parent_write_ms(os.path.join(td, "missing")))

            def stuck(*args, **kwargs):
                time.sleep(5)
                raise OSError("late")
            with mock.patch.object(macos_launcher_probe.tempfile, "mkstemp", stuck):
                started = time.monotonic()
                self.assertEqual({"timed_out": True, "timeout": 0.2},
                                 macos_launcher_probe.parent_write_ms(td, timeout=0.2))
                self.assertLess(time.monotonic() - started, 2)

    def test_the_default_deadline_outlasts_a_slow_volume(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(macos_launcher_probe.TIMEOUT_ENV, None)
            self.assertEqual(macos_launcher_probe.timeout_from_env(), 60.0)
        with mock.patch.dict(os.environ, {macos_launcher_probe.TIMEOUT_ENV: "120"}):
            self.assertEqual(macos_launcher_probe.timeout_from_env(), 120.0)
        for bad in ("5", "301", "x"):
            with mock.patch.dict(os.environ, {macos_launcher_probe.TIMEOUT_ENV: bad}):
                with self.assertRaises(ValueError):
                    macos_launcher_probe.timeout_from_env()


if __name__ == "__main__":
    unittest.main()
