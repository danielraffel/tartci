#!/usr/bin/env python3
"""One GitHub issue per outage when a host cannot boot VMs (vm_boot_alert.py)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import testing_support  # noqa: E402
import vm_boot_alert as vba  # noqa: E402
import vm_dhcp_breaker as vb  # noqa: E402
from test_vm_dhcp_breaker import ROOT, T0, Case  # noqa: E402


class Alert(Case):
    """One GitHub issue per outage, readable from a phone, closed on recovery."""

    def setUp(self) -> None:
        super().setUp()
        profile = self.tmp / "profile.toml"
        profile.write_text('schema = 1\n[host]\nid = "m5"\nssh = "m5"\n')
        os.environ["TARTCI_FLEET_PROFILE"] = str(profile)
        self.addCleanup(os.environ.pop, "TARTCI_FLEET_PROFILE", None)
        self.opened: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.issue_rc = 0
        self.close_fails = 0

    def issue(self, title: str, body: str) -> tuple[int, str]:
        self.opened.append((title, body))
        return (0, str(76 + len(self.opened))) if self.issue_rc == 0 else (1, "rate limited")

    def close(self, number: str) -> tuple[int, str]:
        if self.close_fails:
            self.close_fails -= 1
            return 1, "rate limited"
        self.closed.append(number)
        return 0, "closed"

    def watch(self, at: float) -> dict:
        return vba.alert_pass(now=at, directory=self.tmp / "vm-dhcp",
                              issue=self.issue, close=self.close, host="m5", target="m5")

    def names(self) -> list[str]:  # type: ignore[override]
        path = self.tmp / "vm-dhcp" / "events.jsonl"
        return [json.loads(l)["event"] for l in path.read_text().splitlines()] \
            if path.exists() else []

    def open_at(self, at: float) -> None:
        self.record("no_ip", at)
        self.record("no_ip", at + 200)

    def test_a_closed_breaker_raises_nothing(self):
        self.assertFalse(self.watch(T0)["due"])
        self.assertEqual(self.opened, [])

    def test_first_no_ip_to_issue_within_the_stated_latency(self):
        self.open_at(T0)                       # opens at T0 + 200
        self.assertFalse(self.watch(T0 + 300)["due"])
        self.check(T0 + 500, lane="p")         # cadence probe
        self.record("no_ip", T0 + 700, lane="p")
        out = self.watch(T0 + 800)             # next 300 s pass
        self.assertTrue(out["due"])
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.names(), ["host_vm_boot_down"])
        self.watch(T0 + 1100)
        self.assertEqual(len(self.opened), 1, "one issue per episode")
        self.record("ip", T0 + 1200, lane="q")
        closed = self.watch(T0 + 1400)
        self.assertTrue(closed.get("closed"))
        self.assertEqual(self.closed, ["77"])
        self.assertEqual(self.names(), ["host_vm_boot_down", "host_vm_boot_up"])

    def test_an_idle_broken_host_still_alerts(self):
        # No lane calls check(), so no probe is ever granted.
        self.open_at(T0)
        self.assertFalse(self.watch(T0 + 300)["due"])
        out = self.watch(T0 + 200 + vb.PROBE_SECS)
        self.assertEqual((out["due"], out["why"]), (True, "no lane has probed since it opened"))

    def test_a_probe_in_flight_is_waited_for(self):
        self.open_at(T0)
        self.check(T0 + 500, lane="p")
        self.assertFalse(self.watch(T0 + 600)["due"])
        self.record("ip", T0 + 650, lane="p")
        self.assertFalse(self.watch(T0 + 900)["due"])
        self.assertEqual(self.opened, [])

    def test_a_post_boot_no_ip_alerts_at_once(self):
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(T0 + 100)
        self.check(T0 + 200, lane="a")
        self.record("no_ip", T0 + 400, lane="a")
        out = self.watch(T0 + 410)
        self.assertEqual((out["due"], out["why"]), (True, "a post-boot probe got no address"))

    def test_one_unreported_probe_is_quiet_and_two_alert(self):
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(T0 + 100)
        self.check(T0 + 200, lane="a")
        self.check(T0 + 200 + vb.VERIFY_REPORT_SECS, lane="b")   # opens: probe_unreported
        self.assertFalse(self.watch(T0 + 200 + vb.VERIFY_REPORT_SECS + 600)["due"])
        self.check(T0 + 200 + vb.VERIFY_REPORT_SECS + 300, lane="c")  # first cadence probe
        self.check(T0 + 200 + vb.VERIFY_REPORT_SECS + 600, lane="d")  # c never reported
        out = self.watch(T0 + 200 + vb.VERIFY_REPORT_SECS + 610)
        self.assertEqual((out["due"], out["why"]), (True, "two consecutive probes never reported"))

    def test_probes_that_never_report_alert_whatever_opened_it(self):
        # Opened on a real cause; each cadence probe is superseded unreported.
        self.open_at(T0)
        self.assertEqual(vb.status(self.tmp / "vm-dhcp")["cause"], "dhcp_silent")
        self.check(T0 + 500, lane="p1")
        self.check(T0 + 800, lane="p2")            # p1 never reported
        self.assertFalse(self.watch(T0 + 810)["due"], "one unreported probe is a slow boot")
        self.check(T0 + 1100, lane="p3")           # p2 never reported either
        out = self.watch(T0 + 1110)
        self.assertEqual((out["due"], out["why"]), (True, "two consecutive probes never reported"))

    def test_an_outage_across_a_reboot_never_orphans_its_issue(self):
        self.open_at(T0)
        self.watch(T0 + 200 + vb.PROBE_SECS)               # issue 77
        self.assertEqual(len(self.opened), 1)
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(T0 + 1000)
        self.check(T0 + 1100, lane="a")                    # verifying: 77 stays open
        self.assertFalse(self.watch(T0 + 1150)["due"])
        self.assertEqual(self.closed, [])
        self.record("no_ip", T0 + 1300, lane="a")          # a new outage, a new since
        self.watch(T0 + 1310)
        self.assertEqual(self.closed, ["77"], "the first issue is closed, not forgotten")
        self.assertEqual(len(self.opened), 2)              # issue 78 is the open one
        self.record("ip", T0 + 1500, lane="b")
        self.watch(T0 + 1600)
        self.assertEqual(self.closed, ["77", "78"])

    def state_file(self) -> dict:
        path = self.tmp / "vm-dhcp" / "alert.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def test_a_failed_close_on_a_new_episode_is_retried(self):
        self.open_at(T0)
        self.watch(T0 + 200 + vb.PROBE_SECS)                 # issue 77
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(T0 + 1000)
        self.check(T0 + 1100, lane="a")
        self.record("no_ip", T0 + 1300, lane="a")            # new episode
        self.close_fails = 1
        self.watch(T0 + 1310)
        self.assertEqual(self.state_file()["stale_issues"], ["77"])
        self.assertEqual(self.state_file()["issue"], "78")
        self.watch(T0 + 1610)                                 # retried
        self.assertEqual(self.closed, ["77"])
        self.assertNotIn("stale_issues", self.state_file())

    def test_a_failed_close_at_recovery_is_retried_without_a_second_up_event(self):
        self.open_at(T0)
        self.watch(T0 + 200 + vb.PROBE_SECS)                 # issue 77
        self.record("ip", T0 + 800, lane="q")
        self.close_fails = 1
        self.watch(T0 + 900)
        self.assertEqual(self.state_file(), {"stale_issues": ["77"]})
        self.watch(T0 + 1200)
        self.assertEqual(self.closed, ["77"])
        self.assertFalse((self.tmp / "vm-dhcp" / "alert.json").exists())
        self.assertEqual(self.names().count("host_vm_boot_up"), 1)

    def test_a_verifying_breaker_raises_nothing(self):
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(T0 + 100)
        self.check(T0 + 200, lane="a")
        self.assertFalse(self.watch(T0 + 900)["due"])
        self.assertEqual(self.opened, [])

    def test_the_issue_reads_from_a_phone(self):
        self.ifaces.write_text("lo0 en0")
        self.open_at(T0)
        self.watch(T0 + 200 + vb.PROBE_SECS)
        title, body = self.opened[0]
        self.assertTrue(title.startswith("[tartci] m5: cannot boot VMs since "), title)
        self.assertIn("(vm_dhcp_vm_network_missing)", title)
        first = body.splitlines()[:3]
        self.assertTrue(first[0].startswith("m5 has booted no VM since "), first[0])
        self.assertEqual(first[1], "Run: ssh m5 'tartci doctor fleet'")
        remedy = vba.reasons()["vm_dhcp_vm_network_missing"]["remedy"]
        self.assertEqual(first[2], f"Fix: {remedy}")

    def test_a_failed_issue_open_is_retried_next_pass(self):
        self.open_at(T0)
        self.issue_rc = 1
        self.watch(T0 + 200 + vb.PROBE_SECS)
        state = json.loads((self.tmp / "vm-dhcp" / "alert.json").read_text())
        self.assertEqual(state["issue_error"], "rate limited")
        self.issue_rc = 0
        self.watch(T0 + 200 + 2 * vb.PROBE_SECS)
        self.assertEqual(len(self.opened), 2)
        self.assertEqual(self.names(), ["host_vm_boot_down"], "the event fires once")

    def test_a_scratch_breaker_never_reaches_github_unstubbed(self):
        import host_off
        self.open_at(T0)
        at = T0 + 200 + vb.PROBE_SECS
        with mock.patch.object(host_off, "_open_issue", return_value=(0, "9")) as real:
            vba.alert_pass(now=at, directory=self.tmp / "vm-dhcp", host="m5", target="m5")
        real.assert_not_called()
        state = json.loads((self.tmp / "vm-dhcp" / "alert.json").read_text())
        self.assertNotIn("issue", state)
        self.assertEqual(self.names(), ["host_vm_boot_down"], "the event still fires")
        # Control: the temp-dir guard is the only thing that stopped it.
        (self.tmp / "vm-dhcp" / "alert.json").unlink()
        with mock.patch.object(vba, "_scratch", return_value=False), \
                mock.patch.object(host_off, "_open_issue", return_value=(0, "9")) as real:
            vba.alert_pass(now=at, directory=self.tmp / "vm-dhcp", host="m5", target="m5")
        real.assert_called_once()

    @testing_support.requires_tomllib
    def test_the_host_and_ssh_target_come_from_the_profile(self):
        self.assertEqual(vba._alert_host(), ("m5", "m5"))
        (self.tmp / "profile.toml").write_text('schema = 1\n[host]\nid = "m1"\n')
        self.assertEqual(vba._alert_host(), ("m1", "tartci-m1"))

    def test_without_a_profile_the_node_name_names_it(self):
        os.environ["TARTCI_FLEET_PROFILE"] = str(self.tmp / "missing.toml")
        node = os.uname().nodename.split(".")[0]
        self.assertEqual(vba._alert_host(), (node, f"tartci-{node}"))

    def test_the_reasons_are_the_doctors(self):
        import fleet_doctor
        self.assertEqual(vba.reasons(), fleet_doctor.load_reasons())
        self.assertEqual(vb.doctor_code({"state": "closed"})[1],
                         fleet_doctor.check_vm_dhcp({"state": "closed"}).code)

    def test_the_watchdog_runs_the_pass_and_never_raises(self):
        import tartci_launchd_watchdog as wd
        with mock.patch.object(vba, "alert_pass", side_effect=OSError("disk")):
            self.assertIn("WARN vm-boot check FAILED", wd.vm_boot_pass(now=T0))
        with mock.patch.object(vba, "alert_pass",
                               return_value={"due": True, "why": "a probe got no address"}):
            self.assertIn("WARN vm-boot: this host cannot boot VMs",
                          wd.vm_boot_pass(now=T0))
        main = (ROOT / "scripts" / "tartci_launchd_watchdog.py").read_text()
        main = main[main.index("def main("):]
        self.assertLess(main.index("host_off_pass("), main.index("vm_boot_pass("))
        self.assertLess(main.index("vm_boot_pass("), main.index("discover_agents("))


if __name__ == "__main__":
    unittest.main()
