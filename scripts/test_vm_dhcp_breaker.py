#!/usr/bin/env python3
"""A host whose VM DHCP stopped answering stops cloning, probes, and recovers.

Pins: two `no_ip` within 15 min with no address between open the breaker, and
nothing less does; while open no lane clones except one probe per cadence (or
at once when bootpd's run counter moves), and two lanes in one cadence never
both probe; the first address closes it, as does a reboot after it opened; an
unreadable breaker reads closed; writes are atomic; the doctor names the root
remedy that tartci never runs; and run_one checks the breaker before the job
claim.

Run:  python3 scripts/test_vm_dhcp_breaker.py
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_doctor  # noqa: E402
import vm_dhcp_breaker as vb  # noqa: E402

BREAKER = ROOT / "scripts" / "vm_dhcp_breaker.py"
LIB = ROOT / "providers" / "tart-macos" / "vm-dhcp.lib.sh"
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
T0 = 2_000_000_000.0


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runs = self.tmp / "bootpd-runs"
        self.runs.write_text("3")
        launchctl = self.tmp / "launchctl"
        self.sharing = self.tmp / "sharing-pid"
        self.sharing.write_text("14504")
        # pfd's `launchctl print` lines; healthy by default.
        self.pfd = self.tmp / "pfd"
        self.pfd.write_text("\tstate = running\n\truns = 1\n\tlast exit code = (never exited)\n")
        launchctl.write_text(
            "#!/bin/bash\n"
            f"case \"$2\" in *com.apple.pfd) cat {str(self.pfd)!r}; exit 0 ;; esac\n"
            "case \"$2\" in *NetworkSharing)\n"
            f"  printf '\\tstate = running\\n\\tpid = %s\\n' \"$(cat {str(self.sharing)!r})\"; exit 0 ;;\n"
            "esac\n"
            "printf '\\tstate = not running\\n\\truns = %s\\n\\tlast exit code = 0\\n' "
            f"\"$(cat {str(self.runs)!r})\"\n")
        launchctl.chmod(0o755)
        # The VM network as `ifconfig -l` lists it, and /etc/bootpd.plist:
        # by default a healthy chain, so a no_ip reads as bootpd silent.
        self.ifaces = self.tmp / "ifaces"
        self.ifaces.write_text("lo0 en0 bridge0 bridge100 vmenet0")
        ifconfig = self.tmp / "ifconfig"
        ifconfig.write_text(f"#!/bin/bash\ncat {str(self.ifaces)!r}\n")
        ifconfig.chmod(0o755)
        self.plist = self.tmp / "bootpd.plist"
        self.write_plist(["bridge100"])
        self.env = {"TARTCI_VM_DHCP_DIR": str(self.tmp / "vm-dhcp"),
                    "TARTCI_VM_DHCP_IFCONFIG": str(ifconfig),
                    "TARTCI_VM_DHCP_BOOTPD_PLIST": str(self.plist),
                    "TARTCI_VM_DHCP_LAUNCHCTL": str(launchctl),
                    "TARTCI_VM_DHCP_BOOT_TIME": str(T0 - 86400)}
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.addCleanup(self._restore)
        # A host that has already proven its VM network since its last boot.
        (self.tmp / "vm-dhcp").mkdir()
        (self.tmp / "vm-dhcp" / "breaker.json").write_text(json.dumps(
            {"state": "closed", "streak": [], "boot_time": T0 - 86400}))

    def write_plist(self, enabled) -> None:
        import plistlib
        self.plist.write_bytes(plistlib.dumps({"bootp_enabled": False,
                                               "detect_other_dhcp_server": False,
                                               "dhcp_enabled": enabled}))

    def _restore(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def record(self, outcome: str, at: float, lane: str = "m5-pulp-gate", vm: str = "vm") -> dict:
        return vb.record(argparse.Namespace(outcome=outcome, lane=lane, vm=vm), now=at)

    def check(self, at: float, lane: str = "m5-pulp-gate") -> dict:
        return vb.check(argparse.Namespace(lane=lane), now=at)

    def names(self, result: dict) -> list[str]:
        return [name for name, _ in result["events"]]

    def state(self) -> dict:
        return vb.status(self.tmp / "vm-dhcp")


class Trigger(Case):
    def test_two_no_ip_within_the_window_open_it_with_the_evidence(self):
        self.assertEqual(self.names(self.record("no_ip", T0)), [])
        result = self.record("no_ip", T0 + 300, lane="m5-pulp-gate-slot2")
        self.assertEqual(self.names(result), ["vm_dhcp_unanswered"])
        detail = result["events"][0][1]
        for token in ("streak=2", "window_s=300", "lanes=m5-pulp-gate,m5-pulp-gate-slot2",
                      "bootpd_state=not running", "bootpd_runs=3", "bootpd_last_exit=0",
                      "last_no_ip=2033-05-18T03:33:20Z,2033-05-18T03:38:20Z"):
            self.assertIn(token, detail)
        self.assertEqual(self.state()["state"], "open")

    def test_two_no_ip_further_apart_do_not(self):
        self.record("no_ip", T0)
        self.assertEqual(self.names(self.record("no_ip", T0 + 20 * 60)), [])
        self.assertEqual(self.state()["state"], "closed")

    def test_an_address_between_them_resets_the_streak(self):
        self.record("no_ip", T0)
        self.record("ip", T0 + 60)
        self.assertEqual(self.names(self.record("no_ip", T0 + 120)), [])
        self.assertEqual(self.state()["state"], "closed")


class Open(Case):
    def open(self) -> None:
        self.record("no_ip", T0)
        self.record("no_ip", T0 + 60)

    def test_lanes_back_off_until_the_probe_cadence(self):
        self.open()
        self.assertEqual(self.check(T0 + 61), {"action": "backoff", "events": []})
        result = self.check(T0 + 60 + 300)
        self.assertEqual(result["action"], "probe")
        self.assertIn("trigger=cadence", result["events"][0][1])

    def test_one_probe_per_cadence_across_lanes(self):
        self.open()
        self.assertEqual(self.check(T0 + 400, lane="a")["action"], "probe")
        self.assertEqual(self.check(T0 + 401, lane="b"), {"action": "backoff", "events": []})

    def test_two_lanes_racing_for_the_probe_get_exactly_one(self):
        self.open()
        os.environ["TARTCI_VM_DHCP_PROBE_SECS"] = "1"
        self.addCleanup(os.environ.pop, "TARTCI_VM_DHCP_PROBE_SECS", None)
        state = json.loads((self.tmp / "vm-dhcp" / "breaker.json").read_text())
        state["last_probe_at"] = 0
        (self.tmp / "vm-dhcp" / "breaker.json").write_text(json.dumps(state))
        env = {**os.environ}
        procs = [subprocess.Popen([sys.executable, "-B", str(BREAKER), "check", "--lane", f"l{i}"],
                                  stdout=subprocess.PIPE, text=True, env=env) for i in range(4)]
        actions = [json.loads(p.communicate()[0]) for p in procs]
        self.assertEqual(sorted(a["action"] for a in actions), ["backoff", "backoff", "backoff", "probe"])
        for loser in (a for a in actions if a["action"] == "backoff"):
            self.assertEqual(loser["events"], [])

    def test_bootpd_run_counter_moving_probes_at_once(self):
        self.open()
        self.runs.write_text("4")
        result = self.check(T0 + 70)
        self.assertEqual(result["action"], "probe")
        self.assertIn("trigger=chain_changed", result["events"][0][1])

    def test_a_failed_probe_is_recorded_and_spent(self):
        self.open()
        self.check(T0 + 400, lane="a")
        result = self.record("no_ip", T0 + 600, lane="a")
        self.assertEqual(result["events"],
                         [["vm_dhcp_probe", "lane=a vm=vm result=no_ip cause=dhcp_silent"]])
        self.assertEqual(self.state()["vms_spent"], 3)
        self.assertEqual(self.state()["state"], "open")

    def test_the_probe_s_address_closes_it_with_the_counts(self):
        self.open()
        self.check(T0 + 400, lane="a")
        result = self.record("ip", T0 + 470, lane="a")
        self.assertEqual(self.names(result), ["vm_dhcp_probe", "vm_dhcp_recovered"])
        self.assertIn("result=ip", result["events"][0][1])
        recovered = result["events"][1][1]
        for token in ("reason=probe", "open_s=410", "vms_spent=2", "probes=1", "latency_s=70"):
            self.assertIn(token, recovered)
        self.assertEqual(self.check(T0 + 471)["action"], "clone")

    def test_any_address_on_the_host_closes_it(self):
        self.open()
        result = self.record("ip", T0 + 90, lane="other")
        self.assertEqual(self.names(result), ["vm_dhcp_recovered"])
        self.assertIn("reason=boot_ok", result["events"][0][1])

    def test_a_reboot_after_it_opened_verifies_and_keeps_the_cause(self):
        # The old rule closed it here, and every lane cloned again (m5,
        # 2026-10-07: about 56 VMs after the reboot).
        self.open()
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(T0 + 100)
        result = self.check(T0 + 120)
        self.assertEqual(result["action"], "probe")
        self.assertEqual(self.names(result), ["vm_dhcp_verifying", "vm_dhcp_probe_start"])
        self.assertIn("reason=host_reboot previous_cause=dhcp_silent", result["events"][0][1])
        self.assertEqual(self.state()["state"], "verifying")
        self.assertEqual(self.state()["previous_cause"], "dhcp_silent")


class FailOpen(Case):
    def test_a_corrupt_breaker_reads_closed(self):
        directory = self.tmp / "vm-dhcp"
        directory.mkdir(exist_ok=True)
        (directory / "breaker.json").write_text("{not json")
        self.assertEqual(self.check(T0)["action"], "clone")
        (directory / "breaker.json").write_text(json.dumps({"state": "weird"}))
        self.assertEqual(self.check(T0)["action"], "clone")

    def test_writes_are_atomic_under_concurrent_reads(self):
        stop = threading.Event()
        bad: list[str] = []
        path = self.tmp / "vm-dhcp" / "breaker.json"

        def reader() -> None:
            while not stop.is_set():
                if path.exists():
                    try:
                        json.loads(path.read_text())
                    except ValueError as exc:
                        bad.append(str(exc))
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            for n in range(200):
                self.record("no_ip" if n % 3 else "ip", T0 + n)
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertEqual(bad, [])
        self.assertEqual(list((self.tmp / "vm-dhcp").glob(".breaker.*.tmp")), [])


class Shell(Case):
    def run_lib(self, body: str, extra: dict | None = None) -> subprocess.CompletedProcess:
        script = (f"TARTCI_ROOT={str(ROOT)!r}\nsource {str(LIB)!r}\n"
                  f"event(){{ printf '%s\\t%s\\n' \"$1\" \"$2\" >> {str(self.tmp / 'events')!r}; }}\n"
                  + body)
        # Real clock here: the host booted long before.
        env = {**os.environ, "TARTCI_VM_DHCP_BOOT_TIME": "1", **(extra or {})}
        return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                              env=env, check=False)

    def test_open_backs_off_and_closed_clones(self):
        out = self.run_lib("tartci_vm_dhcp_check l && echo clone\n")
        self.assertIn("clone", out.stdout)
        out = self.run_lib("tartci_vm_dhcp_record l no_ip v1; tartci_vm_dhcp_record l no_ip v2\n"
                           "rc=0; tartci_vm_dhcp_check l || rc=$?; echo \"rc=$rc backoff=$VM_DHCP_BACKOFF\"\n")
        self.assertIn("rc=75 backoff=1", out.stdout, out.stderr)
        self.assertIn("vm_dhcp_unanswered", (self.tmp / "events").read_text())

    def test_knob_off_reads_and_writes_nothing(self):
        before = (self.tmp / "vm-dhcp" / "breaker.json").read_text()
        out = self.run_lib("tartci_vm_dhcp_record l no_ip v1; tartci_vm_dhcp_record l no_ip v2\n"
                           "tartci_vm_dhcp_check l && echo clone\n",
                           {"TARTCI_VM_DHCP_BREAKER": "0"})
        self.assertIn("clone", out.stdout)
        self.assertEqual((self.tmp / "vm-dhcp" / "breaker.json").read_text(), before)
        self.assertFalse((self.tmp / "events").exists())

    def test_knob_off_never_verifies_a_fresh_host(self):
        (self.tmp / "vm-dhcp" / "breaker.json").unlink()
        out = self.run_lib("tartci_vm_dhcp_check l && echo clone\n",
                           {"TARTCI_VM_DHCP_BREAKER": "0"})
        self.assertIn("clone", out.stdout)
        self.assertFalse((self.tmp / "vm-dhcp" / "breaker.json").exists())

    def test_the_runner_logs_the_address_before_recording_it(self):
        body = RUNNER.read_text()
        at = body.index('event boot_ip "ip=$ip clone_to_ip_s=$clone_to_ip_s" '
                        '"clone_to_ip_s=$clone_to_ip_s"')
        self.assertLess(body.index('CLONE_STARTED_AT="$(date +%s)"'), at)
        self.assertLess(body.index('event clone_start "golden=$GOLDEN"'),
                        body.index('CLONE_STARTED_AT="$(date +%s)"'))
        self.assertLess(at, body.index('tartci_vm_dhcp_record "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}" ip "$vm"'))
        self.assertGreater(at, body.index('CURRENT_IP="$ip"'))

    def test_a_breaker_failure_boots(self):
        out = self.run_lib("tartci_vm_dhcp_check l && echo clone\n",
                           {"TARTCI_VM_DHCP_DIR": "/dev/null/vm-dhcp"})
        self.assertIn("clone", out.stdout, out.stderr)


class Wiring(unittest.TestCase):
    def test_the_breaker_is_checked_before_the_job_claim(self):
        body = RUNNER.read_text()
        run_one = body[body.index("run_one(){"):]
        self.assertLess(run_one.index("tartci_vm_dhcp_check"), run_one.index("tartci_job_claim_acquire"))
        self.assertLess(run_one.index("tartci_job_claim_acquire"),
                        run_one.index("tartci_assignment_v2_pre_clone_skip"))

    def test_both_boot_outcomes_are_recorded(self):
        body = RUNNER.read_text()
        no_ip = body.index('event boot_failed "no_ip"')
        self.assertIn("tartci_vm_dhcp_record", body[no_ip:no_ip + 300])
        self.assertIn('no_ip "$vm"', body[no_ip:no_ip + 300])
        got = body.index('CURRENT_IP="$ip"')
        self.assertIn('ip "$vm"', body[got:got + 400])

    def test_a_breaker_backoff_is_an_idle_pass(self):
        body = RUNNER.read_text()
        self.assertIn('[ "${VM_DHCP_BACKOFF:-0}" = 1 ]', body)

    @unittest.skipUnless(tomllib, "macos_fleet_lanes needs tomllib (Python 3.11+)")
    def test_the_profile_key_turns_it_off_and_nothing_else(self):
        import macos_fleet_lanes as fleet  # noqa: PLC0415
        base = (ROOT / "profiles" / "m1-macos-fleet.toml").read_text()
        with tempfile.TemporaryDirectory() as td:
            for value, expect in (("false", "0"), ("true", None)):
                path = Path(td) / f"{value}.toml"
                path.write_text(base.replace("[host]\n", f"[host]\nvm_dhcp_breaker = {value}\n", 1))
                envs = [__import__("plistlib").loads(b)["EnvironmentVariables"]
                        for b in fleet.rendered_plists(fleet.load(path)).values()]
                self.assertTrue(envs)
                self.assertEqual({e.get("TARTCI_VM_DHCP_BREAKER") for e in envs}, {expect})
            bad = Path(td) / "bad.toml"
            bad.write_text(base.replace("[host]\n", '[host]\nvm_dhcp_breaker = "no"\n', 1))
            result = subprocess.run([str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("vm_dhcp_breaker", result.stderr)


class BootpdNotLoaded(Case):
    """launchctl's exit 113 is a job launchd does not have, not an unreadable one."""

    def launchctl(self, body: str) -> None:
        path = self.tmp / "launchctl"
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    def not_loaded(self) -> None:
        # Verbatim shape of `launchctl print` for a label launchd has not loaded.
        self.launchctl("echo 'Bad request.' >&2\n"
                       "echo 'Could not find service \"com.apple.bootpd\" in domain for system' >&2\n"
                       "exit 113\n")

    def test_a_loaded_job_reads_as_loaded_with_its_state(self):
        self.assertEqual(vb.bootpd_readout(),
                         {"loaded": True, "state": "not running", "runs": 3, "last_exit": "0"})

    def test_exit_113_reads_as_not_loaded(self):
        self.not_loaded()
        self.assertEqual(vb.bootpd_readout(), {"loaded": False, "state": "not_loaded"})

    def test_any_other_failure_stays_unreadable(self):
        self.launchctl("echo 'Operation not permitted' >&2\nexit 1\n")
        self.assertEqual(vb.bootpd_readout(), {})

    def test_the_open_event_and_the_doctor_name_the_not_loaded_job(self):
        self.not_loaded()
        self.record("no_ip", T0)
        result = self.record("no_ip", T0 + 300)
        self.assertIn("bootpd_state=not_loaded", result["events"][0][1])
        finding = fleet_doctor.check_vm_dhcp(self.state())
        self.assertEqual((finding.state, finding.code), ("problem", "vm_dhcp_bootpd_not_loaded"))

    def test_a_loaded_but_silent_job_stays_unanswered(self):
        self.record("no_ip", T0)
        self.record("no_ip", T0 + 300)
        finding = fleet_doctor.check_vm_dhcp(self.state())
        self.assertEqual(finding.code, "vm_dhcp_unanswered")

    def test_an_unreadable_launchctl_keeps_the_unanswered_code(self):
        self.launchctl("exit 1\n")
        self.record("no_ip", T0)
        self.record("no_ip", T0 + 300)
        self.assertEqual(fleet_doctor.check_vm_dhcp(self.state()).code, "vm_dhcp_unanswered")

    def test_a_closed_breaker_never_reads_launchd(self):
        self.record("no_ip", T0)
        self.record("no_ip", T0 + 300)
        self.record("ip", T0 + 600)
        self.not_loaded()
        closed = self.state()
        self.assertEqual(closed["state"], "closed")
        self.assertNotIn("bootpd", closed)


class Layers(Case):
    """Each no_ip names the layer that failed, read while its VM is still up."""

    def open_with(self, at: float = T0) -> dict:
        self.record("no_ip", at)
        return self.record("no_ip", at + 300)

    def test_the_diagnosis_matrix(self):
        up = {"bridges": ["bridge100"], "vmenet": 1}
        loaded = {"loaded": True}
        on = {"present": True, "dhcp_enabled": ["bridge100"]}
        for net, bootpd, config, cause in (
                ({}, loaded, on, "unknown"),
                ({"bridges": [], "vmenet": 0}, {"loaded": False}, {"present": False},
                 "vm_network_missing"),
                (up, {"loaded": False}, on, "bootpd_not_loaded"),
                (up, loaded, {"present": True, "dhcp_enabled": []}, "dhcp_config_disabled"),
                (up, loaded, {"present": False}, "dhcp_config_disabled"),
                (up, loaded, on, "dhcp_silent")):
            with self.subTest(cause=cause):
                self.assertEqual(vb.diagnose(net, bootpd, config), cause)

    def test_m5_on_2026_10_07_reads_as_the_vm_network_missing(self):
        # InternetSharing never answered: no bridge100 while the VM ran, and
        # the idle bootpd.plist says dhcp_enabled=false.
        self.ifaces.write_text("lo0 en0 bridge0 utun0")
        self.write_plist(False)
        result = self.open_with()
        self.assertIn("cause=vm_network_missing", result["events"][0][1])
        finding = fleet_doctor.check_vm_dhcp(self.state())
        self.assertEqual(finding.code, "vm_dhcp_vm_network_missing")

    def test_the_network_layer_outranks_bootpd_not_loaded(self):
        self.ifaces.write_text("lo0 en0")
        self.launchctl_missing_bootpd()
        self.open_with()
        self.assertEqual(fleet_doctor.check_vm_dhcp(self.state()).code,
                         "vm_dhcp_vm_network_missing")

    def test_a_running_vm_bridge_missing_from_the_config(self):
        self.write_plist(False)
        self.open_with()
        self.assertEqual(self.state()["cause"], "dhcp_config_disabled")
        self.assertEqual(fleet_doctor.check_vm_dhcp(self.state()).code, "vm_dhcp_config_disabled")

    def test_everything_in_place_stays_unanswered(self):
        self.open_with()
        self.assertEqual(self.state()["cause"], "dhcp_silent")
        self.assertEqual(fleet_doctor.check_vm_dhcp(self.state()).code, "vm_dhcp_unanswered")

    def launchctl_missing_bootpd(self) -> None:
        path = self.tmp / "launchctl"
        path.write_text("#!/bin/bash\n"
                        "case \"$2\" in *NetworkSharing) printf '\\tpid = 1\\n'; exit 0 ;; esac\n"
                        "echo 'Could not find service' >&2\nexit 113\n")
        path.chmod(0o755)


class PfdLayer(Case):
    """pfd, which InternetSharing waits on, crash-looping is the deepest layer."""

    CRASHING = "\tstate = spawn scheduled\n\truns = 2621\n\tlast exit code = 3\n"

    def test_the_crash_loop_rule(self):
        for pfd, looping in (({"state": "spawn scheduled", "runs": 9, "last_exit": "3"}, True),
                             ({"state": "not running", "runs": 9, "last_exit": "-1"}, True),
                             ({"state": "running", "runs": 9, "last_exit": "3"}, False),
                             ({"state": "not running", "runs": 9, "last_exit": "0"}, False),
                             ({"state": "running", "last_exit": "(never exited)"}, False),
                             ({}, False)):
            with self.subTest(pfd=pfd):
                self.assertEqual(vb.pfd_crash_looping(pfd), looping)

    def test_no_bridge_with_pfd_crashing_names_pfd(self):
        self.assertEqual(vb.diagnose({"bridges": []}, {"loaded": True}, {"present": False},
                                     {"state": "spawn scheduled", "last_exit": "3"}),
                         "pfd_crash_loop")
        self.assertEqual(vb.diagnose({"bridges": []}, {"loaded": True}, {"present": False},
                                     {"state": "running", "last_exit": "0"}),
                         "vm_network_missing")
        self.assertEqual(vb.diagnose({"bridges": []}, {"loaded": True}, {"present": False}, {}),
                         "vm_network_missing")

    def test_a_crashing_pfd_with_a_bridge_up_is_not_the_cause(self):
        self.pfd.write_text(self.CRASHING)
        self.record("no_ip", T0)
        self.record("no_ip", T0 + 300)
        self.assertEqual(self.state()["cause"], "dhcp_silent")

    def test_m5_on_2026_10_07_reads_as_pfd_crash_looping(self):
        self.ifaces.write_text("lo0 en0 bridge0 utun0")
        self.write_plist(False)
        self.pfd.write_text(self.CRASHING)
        self.record("no_ip", T0)
        result = self.record("no_ip", T0 + 300)
        self.assertIn("cause=pfd_crash_loop", result["events"][0][1])
        finding = fleet_doctor.check_vm_dhcp(self.state())
        self.assertEqual(finding.code, "vm_dhcp_pfd_crash_loop")
        self.assertIn("last exit 3", finding.detail)
        self.assertIn("pf holds no enable reference", finding.detail)

    def test_another_pfd_exit_is_not_read_as_no_reference(self):
        detail = vb.doctor_code({"state": "open", "cause": "pfd_crash_loop",
                                 "pfd": {"state": "spawn scheduled", "last_exit": "1"}})[2]
        self.assertIn("pfd keeps exiting", detail)
        self.assertNotIn("no enable reference", detail)

    def test_an_ordinary_pfd_failure_does_not_claim_a_reboot(self):
        detail = vb.doctor_code({"state": "open", "cause": "pfd_crash_loop",
                                 "pfd": {"state": "spawn scheduled", "last_exit": "3"}})[2]
        self.assertIn("pf holds no enable reference", detail)
        self.assertNotIn("found by the post-boot probe", detail)

    def test_a_post_boot_pfd_failure_says_it_followed_a_reboot(self):
        self.ifaces.write_text("lo0 en0")
        self.pfd.write_text(self.CRASHING)
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(T0 + 100)
        self.check(T0 + 200, lane="a")
        self.record("no_ip", T0 + 400, lane="a")
        detail = fleet_doctor.check_vm_dhcp(self.state()).detail
        self.assertIn("pf holds no enable reference", detail)
        self.assertIn("found by the post-boot probe after host_reboot", detail)

    def test_pfd_not_loaded_reads_as_unknown_not_crash_looping(self):
        # launchctl print exits 113 for a job launchd does not have: the pfd
        # read is empty, so the layer above it is reported, never pfd.
        path = self.tmp / "launchctl"
        body = path.read_text().replace(
            f"case \"$2\" in *com.apple.pfd) cat {str(self.pfd)!r}; exit 0 ;; esac",
            "case \"$2\" in *com.apple.pfd) echo 'Could not find service' >&2; exit 113 ;; esac")
        self.assertNotEqual(body, path.read_text())
        path.write_text(body)
        self.assertEqual(vb.pfd_readout(), {})
        self.ifaces.write_text("lo0 en0")
        self.record("no_ip", T0)
        self.record("no_ip", T0 + 300)
        self.assertEqual(self.state()["cause"], "vm_network_missing")
        self.assertEqual(fleet_doctor.check_vm_dhcp(self.state()).code, "vm_dhcp_vm_network_missing")

    def test_a_healthy_pfd_reads_as_running(self):
        self.assertEqual(vb.pfd_readout(), {"state": "running", "runs": 1,
                                            "last_exit": "(never exited)"})
        self.assertFalse(vb.pfd_crash_looping(vb.pfd_readout()))

    def test_pfd_runs_climbing_alone_never_triggers_a_probe(self):
        self.pfd.write_text(self.CRASHING)
        self.record("no_ip", T0)
        self.record("no_ip", T0 + 60)
        self.check(T0 + 90)
        self.pfd.write_text(self.CRASHING.replace("2621", "2640"))
        self.assertEqual(self.check(T0 + 120)["action"], "backoff")

    def test_pfd_coming_back_triggers_a_probe(self):
        self.pfd.write_text(self.CRASHING)
        self.record("no_ip", T0)
        self.record("no_ip", T0 + 60)
        self.check(T0 + 90)
        self.pfd.write_text("\tstate = running\n\truns = 2650\n\tlast exit code = 3\n")
        result = self.check(T0 + 120)
        self.assertEqual(result["action"], "probe")
        self.assertIn("trigger=chain_changed", result["events"][0][1])


class ProbeTriggers(Case):
    """Probe at once when an operator's fix changes the chain, or on request."""

    def open(self) -> None:
        self.record("no_ip", T0)
        self.record("no_ip", T0 + 60)

    def test_an_unchanged_chain_waits_for_the_cadence(self):
        self.open()
        self.assertEqual(self.check(T0 + 120)["action"], "backoff")

    def test_bootpd_loading_after_it_opened_probes_at_once(self):
        # m5: bootpd was not loaded when the breaker opened, so its run count
        # was None and a run-count comparison could never fire.
        self.launchctl_missing_bootpd()
        self.open()
        self.assertIsNone(self.state()["bootpd_runs"])
        self.restore_launchctl()
        result = self.check(T0 + 120)
        self.assertEqual(result["action"], "probe")
        self.assertIn("trigger=chain_changed", result["events"][0][1])

    def test_the_config_file_changing_probes_at_once(self):
        self.open()
        self.check(T0 + 90)
        os.utime(self.plist, (T0 + 100, T0 + 100))
        self.assertEqual(self.check(T0 + 120)["action"], "probe")

    def test_internetsharing_restarting_probes_at_once(self):
        self.open()
        self.sharing.write_text("99999")
        self.assertEqual(self.check(T0 + 120)["action"], "probe")

    def test_probe_now_probes_once_then_waits(self):
        self.open()
        requested = vb.probe_now(now=T0 + 100)
        self.assertEqual(requested["action"], "requested")
        self.assertEqual(requested["events"][0][0], "vm_dhcp_probe_requested")
        result = self.check(T0 + 110)
        self.assertEqual(result["action"], "probe")
        self.assertIn("trigger=operator", result["events"][0][1])
        self.record("no_ip", T0 + 200, lane="m5-pulp-gate")
        self.assertEqual(self.check(T0 + 220)["action"], "backoff")

    def test_probe_now_on_a_closed_breaker_does_nothing(self):
        self.assertEqual(vb.probe_now(now=T0)["action"], "closed")
        self.assertEqual(self.state()["state"], "closed")

    def test_the_tartci_verb_reaches_probe_now(self):
        self.open()
        out = subprocess.run([str(ROOT / "tartci"), "vm-dhcp", "probe-now"],
                             capture_output=True, text=True, check=True, env=dict(os.environ))
        self.assertEqual(json.loads(out.stdout)["action"], "requested")
        self.assertIsNotNone(self.state()["probe_requested_at"])

    def launchctl_missing_bootpd(self) -> None:
        Layers.launchctl_missing_bootpd(self)

    def restore_launchctl(self) -> None:
        self.setUp_launchctl()

    def setUp_launchctl(self) -> None:
        path = self.tmp / "launchctl"
        path.write_text(
            "#!/bin/bash\n"
            "case \"$2\" in *NetworkSharing)\n"
            f"  printf '\\tpid = %s\\n' \"$(cat {str(self.sharing)!r})\"; exit 0 ;;\n"
            "esac\n"
            "printf '\\tstate = running\\n\\truns = 4\\n\\tlast exit code = 0\\n'\n")
        path.chmod(0o755)


class PostBoot(Case):
    """After a boot, one probe proves the VM network before lanes clone freely."""

    def reboot(self, at: float = T0 + 100) -> None:
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(at)

    def test_a_matching_boot_time_stays_closed(self):
        # The negative control: no boot since the breaker last proved itself.
        result = self.check(T0)
        self.assertEqual((result["action"], result["events"]), ("clone", []))
        self.assertEqual(self.state()["state"], "closed")

    def test_a_reboot_admits_one_probe_and_its_address_closes_it(self):
        self.reboot()
        first = self.check(T0 + 200, lane="a")
        self.assertEqual(first["action"], "probe")
        self.assertEqual(self.names(first), ["vm_dhcp_verifying", "vm_dhcp_probe_start"])
        self.assertIn("trigger=post_boot", first["events"][1][1])
        for lane in ("b", "c"):
            self.assertEqual(self.check(T0 + 210, lane=lane)["action"], "backoff")
        done = self.record("ip", T0 + 380, lane="a")
        self.assertEqual(self.names(done), ["vm_dhcp_probe", "vm_dhcp_verified"])
        self.assertIn("reason=host_reboot lane=a latency_s=180", done["events"][1][1])
        self.assertEqual(self.state()["state"], "closed")
        self.assertEqual(self.state()["boot_time"], T0 + 100)
        self.assertEqual(self.check(T0 + 390, lane="b")["action"], "clone")

    def test_a_post_boot_no_ip_opens_at_once_with_its_cause(self):
        self.reboot()
        self.check(T0 + 200, lane="a")
        result = self.record("no_ip", T0 + 400, lane="a")
        self.assertEqual(self.names(result), ["vm_dhcp_probe", "vm_dhcp_unanswered"])
        detail = result["events"][1][1]
        for token in ("streak=1", "trigger=post_boot", "alert=now", "cause=dhcp_silent"):
            self.assertIn(token, detail)
        self.assertEqual(self.state()["state"], "open")
        self.assertEqual(self.check(T0 + 410, lane="b")["action"], "backoff")

    def test_a_probe_that_never_reports_frees_the_slot(self):
        self.reboot()
        self.check(T0 + 200, lane="a")
        self.assertEqual(self.check(T0 + 200 + vb.VERIFY_REPORT_SECS - 1, lane="b")["action"],
                         "backoff")
        self.assertEqual(self.state()["state"], "verifying")
        result = self.check(T0 + 200 + vb.VERIFY_REPORT_SECS, lane="b")
        self.assertEqual(self.names(result), ["vm_dhcp_unanswered"])
        self.assertIn("cause=probe_unreported", result["events"][0][1])
        self.assertNotIn("alert=now", result["events"][0][1])
        self.assertEqual(self.state()["state"], "open")
        self.assertIsNone(self.state()["probe_lane"])
        # A slow probe that does report later is the verification that was owed.
        late = self.record("ip", T0 + 200 + vb.VERIFY_REPORT_SECS + 30, lane="a")
        self.assertEqual(self.names(late), ["vm_dhcp_verified"])
        self.assertIn("reason=host_reboot lane=a late=true", late["events"][0][1])
        self.assertEqual(self.state()["state"], "closed")
        self.assertEqual(self.state()["boot_time"], T0 + 100)

    def test_no_recorded_boot_time_verifies(self):
        (self.tmp / "vm-dhcp" / "breaker.json").write_text(json.dumps(
            {"state": "closed", "streak": []}))
        result = self.check(T0)
        self.assertEqual(result["action"], "probe")
        self.assertIn("reason=first_run", result["events"][0][1])

    def test_no_breaker_file_verifies(self):
        (self.tmp / "vm-dhcp" / "breaker.json").unlink()
        result = self.check(T0)
        self.assertEqual(result["action"], "probe")
        self.assertIn("reason=first_run", result["events"][0][1])

    def test_a_corrupt_breaker_clones_and_never_verifies(self):
        (self.tmp / "vm-dhcp" / "breaker.json").write_text("{not json")
        self.reboot()
        self.assertEqual(self.check(T0)["action"], "clone")
        self.assertEqual((self.tmp / "vm-dhcp" / "breaker.json").read_text(), "{not json")

    def test_the_verify_command_enters_verifying(self):
        result = vb.verify("self_update", now=T0)
        self.assertEqual(result["action"], "verifying")
        self.assertIn("reason=self_update", result["events"][0][1])
        self.assertEqual(self.check(T0 + 10, lane="a")["action"], "probe")
        self.assertEqual(self.check(T0 + 20, lane="b")["action"], "backoff")

    def test_the_doctor_says_whether_a_probe_is_in_flight(self):
        self.record("no_ip", T0)
        self.record("no_ip", T0 + 60)
        self.reboot()
        vb.verify("host_reboot", now=T0 + 120)
        waiting = fleet_doctor.check_vm_dhcp(self.state())
        self.assertEqual((waiting.state, waiting.code), ("ok", "vm_dhcp_verifying"))
        self.assertIn("no lane has probed yet", waiting.detail)
        self.assertIn("was open with dhcp_silent", waiting.detail)
        self.check(T0 + 130, lane="a")
        self.assertIn("a probe is in flight on a",
                      fleet_doctor.check_vm_dhcp(self.state()).detail)


class AlertDue(unittest.TestCase):
    """The breaker's pure trigger; the issue itself is vm_boot_alert's."""

    def test_the_trigger(self):
        due = lambda **v: vb.alert_due({"state": "open", "opened_at": T0, **v}, T0 + 400)[0]  # noqa: E731
        self.assertTrue(due(alert_now=True))
        self.assertTrue(due(failed_probes=1, probes=1))
        self.assertTrue(due(probes=0))
        self.assertFalse(due(probes=1, probe_lane="p"))
        self.assertFalse(due(cause="probe_unreported", consecutive_unreported=1))
        self.assertTrue(due(cause="probe_unreported", consecutive_unreported=2))
        self.assertTrue(due(cause="dhcp_silent", consecutive_unreported=2, probes=3,
                            probe_lane="p"))
        self.assertFalse(vb.alert_due({"state": "open", "opened_at": T0}, T0 + 100)[0])
        for state in ("closed", "verifying"):
            self.assertFalse(vb.alert_due({"state": state, "alert_now": True}, T0 + 400)[0])


class BootTimes(unittest.TestCase):
    """The verify bound is re-derived from the lane logs, wherever they live."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def lane(self, rel: str, rows: list[tuple[str, str, str]]) -> None:
        path = self.root / rel / "events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps({"ts": ts, "event": ev, "runner": "r",
                                            "detail": detail}) + "\n"
                                for ts, ev, detail in rows))

    def test_nested_lanes_are_counted(self):
        # m5studio keeps its lanes at state/macos-fleet/<lane>/.
        self.lane("macos", [("2026-10-07T10:00:00Z", "clone_start", ""),
                            ("2026-10-07T10:00:40Z", "boot_ip", "ip=x")])
        self.lane("macos-fleet/pulp-gate", [("2026-10-07T11:00:00Z", "clone_start", ""),
                                            ("2026-10-07T11:03:20Z", "boot_failed", "no_ip")])
        out = vb.boot_times(self.root, 0)
        self.assertEqual((out["lane_logs"], out["clones"]), (2, 2))
        self.assertEqual(out["boot_ip"]["max"], 40)
        self.assertEqual(out["no_ip"]["max"], 200)
        self.assertEqual(out["suggested_verify_secs"], 420)

    def test_the_window_and_unpaired_reports(self):
        self.lane("a", [("2026-09-01T10:00:00Z", "clone_start", ""),
                        ("2026-09-01T10:09:00Z", "boot_failed", "no_ip"),
                        ("2026-10-07T10:00:00Z", "boot_ip", "ip=x"),
                        ("2026-10-07T10:01:00Z", "clone_start", ""),
                        ("2026-10-07T10:02:00Z", "boot_failed", "no_ssh")])
        out = vb.boot_times(self.root, calendar.timegm((2026, 10, 1, 0, 0, 0)))
        self.assertEqual(out["clones"], 1)
        self.assertEqual((out["boot_ip"]["n"], out["no_ip"]["n"]), (0, 0))
        self.assertIsNone(out["suggested_verify_secs"])

    def test_the_cli_reads_a_root(self):
        self.lane("x/y", [("2026-10-07T10:00:00Z", "clone_start", ""),
                          ("2026-10-07T10:01:00Z", "boot_ip", "ip=x")])
        out = subprocess.run([sys.executable, str(BREAKER), "boot-times", "--days", "100000",
                              "--root", str(self.root)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout)["boot_ip"]["n"], 1)


class Doctor(unittest.TestCase):
    def test_codes_and_the_root_remedy(self):
        reasons = fleet_doctor.load_reasons()
        for value, state, code in (({"state": "open", "opened_at": 1, "vms_spent": 2, "probes": 1},
                                    "problem", "vm_dhcp_unanswered"),
                                   ({"state": "closed"}, "ok", "vm_dhcp_ok"),
                                   ({"state": "unreadable", "error": "x"}, "unknown", "vm_dhcp_unreadable")):
            finding = fleet_doctor.check_vm_dhcp(value)
            self.assertEqual((finding.state, finding.code), (state, code))
            self.assertIn(code, fleet_doctor.CODES)
            self.assertIn(code, reasons)
        remedy = reasons["vm_dhcp_unanswered"]["remedy"]
        self.assertIn("sudo launchctl kickstart -k system/com.apple.bootpd", remedy)
        self.assertIn("tartci never runs it", remedy)
        # Only the pfd layer has a verified remedy (m5, 2026-10-07).
        for code in ("vm_dhcp_vm_network_missing", "vm_dhcp_config_disabled"):
            self.assertIn("no verified remedy yet", reasons[code]["remedy"].lower())
        for code in ("vm_dhcp_vm_network_missing", "vm_dhcp_config_disabled",
                     "vm_dhcp_pfd_crash_loop"):
            self.assertIn("tartci vm-dhcp probe-now", reasons[code]["remedy"])
            self.assertIn(code, fleet_doctor.CODES)
        pfd = reasons["vm_dhcp_pfd_crash_loop"]["remedy"]
        self.assertTrue(pfd.startswith("On the host (tartci never runs it): `sudo pfctl -E`."), pfd)
        self.assertIn("`sudo pfctl -X <token>`", pfd)
        self.assertIn("does not survive a reboot", pfd)
        self.assertNotIn("no verified remedy", pfd.lower())
        not_loaded = reasons["vm_dhcp_bootpd_not_loaded"]
        self.assertEqual(fleet_doctor.check_vm_dhcp(
            {"state": "open", "bootpd": {"loaded": False}}).code, "vm_dhcp_bootpd_not_loaded")
        self.assertIn("sudo launchctl bootstrap system /System/Library/LaunchDaemons/bootps.plist",
                      not_loaded["remedy"])
        self.assertIn("Could not find service", not_loaded["why"])

    def test_no_remedy_ever_suggests_the_sip_blocked_kickstart(self):
        # m5, 2026-10-07: "150: Operation not permitted while System Integrity
        # Protection is engaged".
        blocked = re.compile(r"kickstart\s+(-k\s+)?system/com\.apple\.NetworkSharing")
        for path in (ROOT / "scripts" / "fleet_reasons.json", ROOT / "docs" / "runbook.md",
                     ROOT / "scripts" / "fleet_doctor.py", ROOT / "scripts" / "vm_dhcp_breaker.py"):
            self.assertIsNone(blocked.search(path.read_text()), path.name)

    def test_pfctl_d_is_only_ever_named_as_never(self):
        # Disabling pf drops every holder's references; the undo for one's own
        # `pfctl -E` is `pfctl -X <token>`.
        for path in (ROOT / "scripts" / "fleet_reasons.json", ROOT / "docs" / "runbook.md"):
            text = path.read_text()
            for found in re.finditer(r"pfctl -d", text):
                self.assertIn("Never", text[max(0, found.start() - 12):found.start()],
                              f"{path.name}: {text[found.start() - 40:found.end() + 20]!r}")

    def test_no_remedy_names_the_restart_that_did_not_help(self):
        # m5, 2026-10-07: InternetSharing relaunched (pid 51580) and the next
        # probes still created no bridge100.
        tried = re.compile(r"killall\s+InternetSharing")
        for path in (ROOT / "scripts" / "fleet_reasons.json", ROOT / "docs" / "runbook.md",
                     ROOT / "scripts" / "fleet_doctor.py", ROOT / "scripts" / "vm_dhcp_breaker.py"):
            self.assertIsNone(tried.search(path.read_text()), path.name)


if __name__ == "__main__":
    unittest.main()
