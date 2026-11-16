"""The unattended heal never boots out a lane that has a job in flight.

On m5s the watchdog plist carried a stale TART_HOME, so `tart list` read `[]`
and every pass saw no VM building. A Vellum GPU proof writes nothing to the
lane log for over 30 minutes, so the lane looked "alive but frozen" and the
heal killed in-flight jobs 19 times. These tests drive the real heal pass
with a faked launchd and process table.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lane_busy  # noqa: E402
import tartci_launchd_watchdog as wd  # noqa: E402

LABEL = "com.danielraffel.tartci.tart-runner-macos-fleet.m5studio.vellum-gate"
SUPERVISOR = 4242
TART_RUN = 4300


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class HealPass(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.agents = self.root / "LaunchAgents"
        self.agents.mkdir()
        self.state = self.root / "state" / "vellum-gate"
        self.state.mkdir(parents=True)
        self.leases = self.root / "leases"
        self.leases.mkdir()
        self.log = self.root / "vellum-gate.log"
        self.log.write_text("waiting for work\n")
        stale = time.time() - 2 * 3600
        os.utime(self.log, (stale, stale))
        self.plist = self.agents / f"{LABEL}.plist"
        self.plist.write_bytes(plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": ["/bin/true"],
            "StandardOutPath": str(self.log),
            "EnvironmentVariables": {
                "TARTCI_STATE_DIR": str(self.state),
                "TARTCI_RUNNER_NAME": "m5studio-vellum-gate-01",
            },
        }))
        self.ps_rows = [f"{SUPERVISOR} 1 /bin/bash runner.sh serve --loop"]
        self.ps_ok = True
        self.launchctl_print_rc = 0

        def fake_run(cmd: list[str]) -> tuple[int, str, str]:
            if cmd[:2] == ["launchctl", "print"]:
                if self.launchctl_print_rc != 0:
                    return self.launchctl_print_rc, "", "Operation not permitted"
                return 0, f"state = running\npid = {SUPERVISOR}\nlast exit code = 0\n", ""
            if cmd[:2] == ["ps", "-A"]:
                return (0, "\n".join(self.ps_rows) + "\n", "") if self.ps_ok else (1, "", "denied")
            return 1, "", f"unexpected command in test: {cmd}"

        original = wd._run
        wd._run = fake_run
        self.addCleanup(setattr, wd, "_run", original)

    def tart_run_in_flight(self) -> None:
        self.ps_rows.append(f"{TART_RUN} {SUPERVISOR} /opt/homebrew/bin/tart run "
                            "m5studio-vellum-gate-01-30424-1 --no-graphics")

    def write_lease(self, pid: int) -> None:
        (self.leases / "leases.json").write_text(json.dumps([{
            "id": "lease-1", "command_kind": "tart-macos-vm", "pid": pid,
            "vm_name": "m5studio-vellum-gate-01-30424-1",
        }]))

    def write_events(self, *rows: tuple[float, str]) -> None:
        lines = ["current-job-api path=repos/x/actions/jobs/1 elapsed_s=0.4"]
        for ts, event in rows:
            lines.append(json.dumps({"ts": _iso(ts), "event": event,
                                     "runner": "m5studio-vellum-gate-01",
                                     "vm": "m5studio-vellum-gate-01-30424-1",
                                     "detail": "Running job: gpu-macos-arm64"}))
        (self.state / "events.jsonl").write_text("\n".join(lines) + "\n")

    def run_pass(self, probe: wd.TartVMProbe) -> tuple[str, mock.Mock, dict]:
        buf = io.StringIO()
        env = {"TARTCI_HOME": str(self.root / "home"),
               "TARTCI_LEASE_DIR": str(self.leases)}
        with mock.patch.object(wd, "probe_tart_vm_running", return_value=probe), \
             mock.patch.object(wd, "store_health_pass", return_value={"state": "healthy"}), \
             mock.patch.object(wd, "consent_alert_reading", return_value=None), \
             mock.patch.object(wd, "tcc_prompt_pass", return_value=None), \
             mock.patch.object(wd, "host_off_pass", return_value=None), \
             mock.patch.object(wd, "vm_boot_pass", return_value=None), \
             mock.patch.object(wd, "lane_handoff_pass", return_value=None), \
             mock.patch.object(wd, "disabled_services", return_value=set()), \
             mock.patch.object(wd, "config_verdicts", return_value={}), \
             mock.patch.object(wd, "build_disagreement_pass", return_value={}), \
             mock.patch.object(wd, "reload_agent", return_value=True) as reload, \
             mock.patch.dict(os.environ, env), redirect_stdout(buf):
            wd.main(["--launch-agents-dir", str(self.agents),
                     "--participation-file", str(self.root / "participate"),
                     "--fleet-config", str(self.root / "absent-profile.toml")])
        heal_log = wd.load_heal_log(str(self.root / "home" / "state" / "launchd-watchdog.json"))
        return buf.getvalue(), reload, heal_log

    IDLE = wd.TartVMProbe(False, "Tart VM inventory is idle", "/bin/tart", "/Volumes/Example/VMs")
    EMPTY = wd.TartVMProbe(None, "Tart VM inventory is empty in /Users/x/VMs; check TART_HOME",
                           "/bin/tart", "/Users/x/VMs", True)

    def test_busy_lane_with_stale_log_and_empty_inventory_is_not_healed(self) -> None:
        self.tart_run_in_flight()
        self.write_lease(SUPERVISOR)
        out, reload, heal_log = self.run_pass(self.EMPTY)
        reload.assert_not_called()
        self.assertNotIn("[healed]", out)
        self.assertNotIn(LABEL, heal_log)

    def test_gate_defers_a_wedged_verdict_when_the_lane_is_mid_job(self) -> None:
        # The inventory reads idle (a wrong but non-empty store), so the verdict
        # is wedged and only the per-lane gate stands between it and a bootout.
        self.tart_run_in_flight()
        out, reload, heal_log = self.run_pass(self.IDLE)
        reload.assert_not_called()
        self.assertIn("heal-deferred: mid-job (", out)
        self.assertIn("owns tart run", out)
        self.assertIn("[heal-deferred]", out)
        self.assertNotIn(LABEL, heal_log, "a deferral must not count toward the rate limit")

    def test_live_lease_with_empty_inventory_logs_the_mismatch(self) -> None:
        self.write_lease(os.getpid())
        out, reload, _ = self.run_pass(self.EMPTY)
        reload.assert_not_called()
        mismatch = [line for line in out.splitlines() if "inventory-mismatch:" in line]
        self.assertEqual(len(mismatch), 1, out)
        self.assertIn("TART_HOME=/Users/x/VMs", mismatch[0])
        self.assertIn("m5studio-vellum-gate-01-30424-1", mismatch[0])

    def test_dead_lease_pid_is_not_a_mismatch(self) -> None:
        self.write_lease(2 ** 22 + 7)  # no such pid
        out, _, _ = self.run_pass(self.EMPTY)
        self.assertNotIn("inventory-mismatch", out)

    def test_open_job_assignment_defers_even_without_a_vm_process(self) -> None:
        now = time.time()
        self.write_events((now - 2400, "lease_acquired"), (now - 2100, "job_assigned"))
        out, reload, _ = self.run_pass(self.IDLE)
        reload.assert_not_called()
        self.assertIn("heal-deferred: mid-job (job_assigned", out)

    def test_idle_frozen_lane_still_heals(self) -> None:
        now = time.time()
        self.write_events((now - 9000, "job_assigned"), (now - 8000, "teardown"))
        out, reload, heal_log = self.run_pass(self.IDLE)
        reload.assert_called_once_with(LABEL, str(self.plist), dry_run=False)
        self.assertIn("alive but frozen", out)
        self.assertIn("[healed]", out)
        self.assertEqual(len(heal_log.get(LABEL, [])), 1)

    def test_unknown_busy_probe_defers(self) -> None:
        self.ps_ok = False
        out, reload, _ = self.run_pass(self.IDLE)
        reload.assert_not_called()
        self.assertIn("heal-deferred: mid-job (", out)
        self.assertIn("process table unreadable", out)


class Gate(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.events = self.root / "events.jsonl"

    def write(self, *rows: object) -> None:
        self.events.write_text("\n".join(
            r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n")

    def row(self, ts: float, event: str) -> dict:
        return {"ts": _iso(ts), "event": event, "vm": "vm-1", "detail": "d"}

    def test_closing_events_end_the_assignment(self) -> None:
        now = time.time()
        for closer in sorted(wd.JOB_CLOSING_EVENTS):
            self.write(self.row(now - 100, "job_assigned"), self.row(now - 50, closer))
            self.assertIsNone(wd.open_job_assignment(str(self.events), now), closer)

    def test_supervisor_signal_after_a_kill_closes_it(self) -> None:
        # What a killed job leaves behind: no teardown, a supervisor_signal.
        now = time.time()
        self.write(self.row(now - 3000, "job_assigned"), self.row(now - 900, "supervisor_signal"),
                   self.row(now - 800, "demand_waiting"))
        self.assertIsNone(wd.open_job_assignment(str(self.events), now))

    def test_assignment_older_than_the_bound_is_not_trusted(self) -> None:
        now = time.time()
        self.write(self.row(now - wd.JOB_ASSIGNMENT_MAX_AGE_S - 60, "job_assigned"))
        self.assertIsNone(wd.open_job_assignment(str(self.events), now))

    def test_garbage_lines_are_skipped(self) -> None:
        now = time.time()
        self.write(self.row(now - 100, "job_assigned"), "not json", "{broken",
                   json.dumps(["a list"]), self.row(now - 50, "storage_health"))
        self.assertIn("job_assigned", wd.open_job_assignment(str(self.events), now) or "")

    def test_only_the_tail_is_read(self) -> None:
        now = time.time()
        filler = json.dumps(self.row(now - 10, "storage_health"))
        n = wd.EVENTS_TAIL_BYTES // len(filler) + 10
        self.events.write_text(json.dumps(self.row(now - 50, "teardown")) + "\n"
                               + json.dumps(self.row(now - 40, "job_assigned")) + "\n"
                               + (filler + "\n") * n)
        # The job_assigned fell out of the tail window; nothing in the tail opens a job.
        self.assertIsNone(wd.open_job_assignment(str(self.events), now))

    def test_missing_log_is_no_evidence(self) -> None:
        self.assertIsNone(wd.open_job_assignment(str(self.root / "absent.jsonl")))

    def health(self, plist: str, signature: str = "") -> wd.AgentHealth:
        return wd.AgentHealth(LABEL, plist, None, "running", 0, 9999.0, "wedged", "r", signature)

    def test_stale_boot_skips_lane_busy_but_not_the_event_log(self) -> None:
        state = self.root / "state"
        state.mkdir()
        plist = self.root / "lane.plist"
        plist.write_bytes(plistlib.dumps({"Label": LABEL, "EnvironmentVariables": {
            "TARTCI_STATE_DIR": str(state), "TARTCI_RUNNER_NAME": "lane"}}))
        with mock.patch.object(wd, "mid_job_refusal", return_value="lease held") as busy:
            self.assertIsNone(wd.heal_deferral(self.health(str(plist), "stale-boot")))
            busy.assert_not_called()
            self.assertEqual(wd.heal_deferral(self.health(str(plist))), "lease held")
        (state / "events.jsonl").write_text(json.dumps(self.row(time.time() - 60, "job_assigned")))
        self.assertIn("job_assigned", wd.heal_deferral(self.health(str(plist), "stale-boot")) or "")

    def test_an_error_in_the_check_defers(self) -> None:
        with mock.patch.object(wd, "mid_job_refusal", side_effect=RuntimeError("boom")):
            reason = wd.heal_deferral(self.health(str(self.root / "absent.plist")))
        self.assertIn("busy check failed: boom", reason or "")

    def test_absent_lane_is_not_deferred(self) -> None:
        row = lane_busy.LaneBusy(LABEL, lane_busy.ABSENT, "not loaded in launchd")
        with mock.patch.object(lane_busy, "probe", return_value=[row]):
            self.assertIsNone(wd.heal_deferral(self.health(str(self.root / "absent.plist"))))


if __name__ == "__main__":
    unittest.main()
