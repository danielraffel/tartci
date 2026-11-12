"""Regression tests for the Tart boot/admission wedge watchdog."""

from __future__ import annotations

import json
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tartci_launchd_watchdog as wd  # noqa: E402


class StaleBootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.plist = self.root / "lane.plist"
        self.plist.write_bytes(plistlib.dumps({
            "Label": "com.danielraffel.tartci.tart-runner-macos-fleet.m1.lane",
            "EnvironmentVariables": {
                "TARTCI_STATE_DIR": str(self.state),
                "TARTCI_RUNNER_NAME": "lane",
            },
        }))
        self.original_run = wd._run
        def process_run(cmd: list[str]) -> tuple[int, str, str]:
            if cmd[:2] == ["ps", "-o"] and "lstart=" in cmd:
                return 0, "Mon Jan  1 00:00:00 2024\n", ""
            if cmd[:2] == ["ps", "-o"] and "ppid=" in cmd:
                return 0, "41\n", ""
            return self.original_run(cmd)
        wd._run = process_run
        self.addCleanup(setattr, wd, "_run", self.original_run)

    def write_state(self, **updates: object) -> None:
        state = {
            "ts": 1_000.0, "phase": "booting", "supervisor_pid": "42",
            "supervisor_pid_started_at": "Mon Jan  1 00:00:00 2024",
        }
        state.update(updates)
        (self.state / "lane.state.json").write_text(json.dumps(state))

    def test_stale_boot_requires_matching_launchd_pid(self) -> None:
        self.write_state()
        probe = wd.probe_stale_boot(str(self.plist), 42, now=2_000.0)
        self.assertTrue(probe.stale, probe.reason)
        mismatch = wd.probe_stale_boot(str(self.plist), 43, now=2_000.0)
        self.assertFalse(mismatch.stale, mismatch.reason)

    def test_stale_boot_without_supervisor_start_identity_is_unknown(self) -> None:
        self.write_state()
        path = self.state / "lane.state.json"
        state = json.loads(path.read_text())
        del state["supervisor_pid_started_at"]
        path.write_text(json.dumps(state))
        probe = wd.probe_stale_boot(str(self.plist), 42, now=2_000.0)
        self.assertIsNone(probe.stale, probe.reason)

    def test_sealed_launch_helper_child_pid_is_owned_by_launchd_parent(self) -> None:
        self.write_state()
        original = wd._run
        def fake_run(cmd: list[str]) -> tuple[int, str, str]:
            if cmd[:2] == ["ps", "-o"] and "lstart=" in cmd:
                return 0, "Mon Jan  1 00:00:00 2024\n", ""
            if cmd[:2] == ["ps", "-o"] and "ppid=" in cmd:
                return 0, "41\n", ""
            return original(cmd)
        wd._run = fake_run
        self.addCleanup(setattr, wd, "_run", original)
        probe = wd.probe_stale_boot(str(self.plist), 41, now=2_000.0)
        self.assertTrue(probe.stale, probe.reason)

    def test_recorded_supervisor_start_time_blocks_pid_reuse(self) -> None:
        self.write_state(supervisor_pid_started_at="Mon Jan  1 00:00:00 2024")
        original = wd._run
        def fake_run(cmd: list[str]) -> tuple[int, str, str]:
            if cmd[:2] == ["ps", "-o"] and "ppid=" in cmd:
                return 0, "41\n", ""
            if cmd[:2] == ["ps", "-o"] and "lstart=" in cmd:
                return 0, "Tue Jan  2 00:00:00 2024\n", ""
            return original(cmd)
        wd._run = fake_run
        self.addCleanup(setattr, wd, "_run", original)
        probe = wd.probe_stale_boot(str(self.plist), 41, now=2_000.0)
        self.assertFalse(probe.stale, probe.reason)

    def test_runner_iso_heartbeat_timestamp_is_supported(self) -> None:
        self.write_state(ts="1970-01-01T00:16:40Z")
        probe = wd.probe_stale_boot(str(self.plist), 42, now=2_000.0)
        self.assertTrue(probe.stale, probe.reason)

    def test_invalid_numeric_timestamps_fail_closed(self) -> None:
        self.write_state(ts=True)
        self.assertIsNone(wd.probe_stale_boot(str(self.plist), 42, now=2_000.0).stale)
        self.write_state(ts=10**400)
        self.assertIsNone(wd.probe_stale_boot(str(self.plist), 42, now=2_000.0).stale)
        for value in (float("nan"), float("inf"), float("-inf"), "1970-01-01T00:16:40"):
            self.write_state(ts=value)
            self.assertIsNone(wd.probe_stale_boot(str(self.plist), 42, now=2_000.0).stale)

    def test_qemu_lane_never_uses_tart_idle_for_recovery(self) -> None:
        qemu = self.root / "qemu.plist"
        log = self.root / "qemu.log"
        log.write_text("quiet\n")
        import os
        os.utime(log, (1.0, 1.0))
        qemu.write_bytes(plistlib.dumps({
            "Label": "com.danielraffel.tartci.qemu-runner-windows",
            "EnvironmentVariables": {
                "TARTCI_STATE_DIR": str(self.state), "TARTCI_RUNNER_NAME": "lane",
            },
            "StandardOutPath": str(log),
        }))
        self.write_state(ts="1970-01-01T00:16:40Z")
        original = wd._run
        wd._run = self.launchctl_run(42)
        self.addCleanup(setattr, wd, "_run", original)
        health = wd.gather_health(
            "com.danielraffel.tartci.qemu-runner-windows", str(qemu), 4_500,
            vm_running=False, pool_participating=True,
        )
        self.assertEqual(health.verdict, "unknown", health.reason)
        self.assertIn("refusing alive-but-frozen", health.reason)

    def test_linux_tart_lane_never_uses_macos_identity_or_tart_idle(self) -> None:
        linux = self.root / "linux.plist"
        log = self.root / "linux.log"
        log.write_text("quiet\n")
        import os
        os.utime(log, (1.0, 1.0))
        linux.write_bytes(plistlib.dumps({
            "Label": "com.danielraffel.tartci.tart-runner-linux",
            "StandardOutPath": str(log),
        }))
        original = wd._run
        wd._run = self.launchctl_run(42)
        self.addCleanup(setattr, wd, "_run", original)
        health = wd.gather_health(
            "com.danielraffel.tartci.tart-runner-linux", str(linux), 4_500,
            vm_running=False, pool_participating=True,
        )
        self.assertEqual(health.verdict, "unknown", health.reason)

    def test_rendered_fleet_prefix_and_slot_identity_is_supported(self) -> None:
        rendered = self.root / "rendered.plist"
        rendered.write_bytes(plistlib.dumps({
            "Label": "com.danielraffel.tartci.tart-runner-macos-fleet.m1.lane",
            "EnvironmentVariables": {
                "TARTCI_STATE_DIR": str(self.state),
                "TARTCI_RUNNER_NAME_PREFIX": "m1-lane",
                "TARTCI_RUNNER_SLOT": "1",
            },
        }))
        (self.state / "m1-lane-01.state.json").write_text(json.dumps({
            "ts": "1970-01-01T00:16:40Z", "phase": "booting",
            "supervisor_pid": "42",
            "supervisor_pid_started_at": "Mon Jan  1 00:00:00 2024",
        }))
        probe = wd.probe_stale_boot(str(rendered), 42, now=2_000.0)
        self.assertTrue(probe.stale, probe.reason)

    def test_active_job_and_fresh_heartbeat_are_safe(self) -> None:
        self.write_state(run_id="run-1")
        active = wd.probe_stale_boot(str(self.plist), 42, now=2_000.0)
        self.assertFalse(active.stale, active.reason)
        self.write_state(run_id="")
        fresh = wd.probe_stale_boot(str(self.plist), 42, now=1_100.0)
        self.assertFalse(fresh.stale, fresh.reason)

    def test_corrupt_state_is_unknown_and_missing_state_is_not_a_wedge(self) -> None:
        (self.state / "lane.state.json").write_text("not json")
        corrupt = wd.probe_stale_boot(str(self.plist), 42, now=2_000.0)
        self.assertIsNone(corrupt.stale, corrupt.reason)
        (self.state / "lane.state.json").unlink()
        missing = wd.probe_stale_boot(str(self.plist), 42, now=2_000.0)
        self.assertFalse(missing.stale, missing.reason)

    def test_gather_health_surfaces_stale_boot_as_healable_wedge(self) -> None:
        self.write_state(ts="1970-01-01T00:16:40Z")
        original = wd._run
        wd._run = self.launchctl_run(42)
        self.addCleanup(setattr, wd, "_run", original)
        health = wd.gather_health(
            "com.danielraffel.tartci.tart-runner-macos-fleet.m1.lane",
            str(self.plist), stale_log_s=4_500, vm_running=False,
            pool_participating=True,
        )
        self.assertEqual(health.verdict, "wedged", health.reason)
        self.assertIn("no Tart VM is running", health.reason)

    def test_gather_health_accepts_sealed_launch_helper_parent(self) -> None:
        self.write_state()
        original = wd._run
        def fake_run(cmd: list[str]) -> tuple[int, str, str]:
            if cmd[:2] == ["ps", "-o"] and "lstart=" in cmd:
                return 0, "Mon Jan  1 00:00:00 2024\n", ""
            if cmd[:2] == ["ps", "-o"] and "ppid=" in cmd:
                return 0, "41\n", ""
            return 0, "state = running\npid = 41\nlast exit code = 0\n", ""
        wd._run = fake_run
        self.addCleanup(setattr, wd, "_run", original)
        health = wd.gather_health(
            "com.danielraffel.tartci.tart-runner-macos-fleet.m1.lane",
            str(self.plist), stale_log_s=4_500, vm_running=False,
            pool_participating=True,
        )
        self.assertEqual(health.verdict, "wedged", health.reason)

    def test_gather_health_defers_when_any_tart_vm_is_running(self) -> None:
        self.write_state()
        original = wd._run
        wd._run = self.launchctl_run(42)
        self.addCleanup(setattr, wd, "_run", original)
        health = wd.gather_health(
            "com.danielraffel.tartci.tart-runner-macos-fleet.m1.lane",
            str(self.plist), stale_log_s=4_500, vm_running=True,
            pool_participating=True,
        )
        self.assertNotEqual(health.verdict, "wedged", health.reason)

    def test_gather_health_defers_when_tart_inventory_is_unknown(self) -> None:
        self.write_state()
        original = wd._run
        wd._run = self.launchctl_run(42)
        self.addCleanup(setattr, wd, "_run", original)
        health = wd.gather_health(
            "com.danielraffel.tartci.tart-runner-macos-fleet.m1.lane",
            str(self.plist), stale_log_s=4_500, vm_running=None,
            pool_participating=True,
        )
        self.assertNotEqual(health.verdict, "wedged", health.reason)

    def test_gather_health_fails_closed_on_corrupt_lane_state(self) -> None:
        (self.state / "lane.state.json").write_text("not json")
        original = wd._run
        wd._run = lambda cmd: (0, "state = running\npid = 42\nlast exit code = 0\n", "")
        self.addCleanup(setattr, wd, "_run", original)
        health = wd.gather_health(
            "com.danielraffel.tartci.tart-runner-macos-fleet.m1.lane",
            str(self.plist), stale_log_s=4_500, vm_running=False,
            pool_participating=True,
        )
        self.assertEqual(health.verdict, "unknown", health.reason)

    def launchctl_run(self, launchd_pid: int):
        def run(cmd: list[str]) -> tuple[int, str, str]:
            if cmd[:2] == ["ps", "-o"] and "lstart=" in cmd:
                return 0, "Mon Jan  1 00:00:00 2024\n", ""
            if cmd[:2] == ["ps", "-o"] and "ppid=" in cmd:
                return 0, f"{launchd_pid}\n", ""
            return 0, f"state = running\npid = {launchd_pid}\nlast exit code = 0\n", ""
        return run


if __name__ == "__main__":
    unittest.main()
