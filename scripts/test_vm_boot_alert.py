#!/usr/bin/env python3
"""One GitHub issue per outage when a host cannot boot VMs (vm_boot_alert.py)."""
from __future__ import annotations

import calendar
import json
import os
import sys
import time
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

    def test_an_outage_across_a_reboot_keeps_one_issue(self):
        self.open_at(T0)
        self.watch(T0 + 200 + vb.PROBE_SECS)               # issue 77
        self.assertEqual(len(self.opened), 1)
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(T0 + 1000)
        self.check(T0 + 1100, lane="a")                    # verifying: 77 stays open
        self.assertFalse(self.watch(T0 + 1150)["due"])
        self.assertEqual(self.closed, [])
        self.record("no_ip", T0 + 1300, lane="a")          # reopened with a fresh opened_at
        self.watch(T0 + 1310)
        self.assertEqual(self.closed, [], "a reopened breaker is the same outage")
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.state_file()["since"], vb.iso(T0 + 200))
        self.record("ip", T0 + 1500, lane="b")
        self.watch(T0 + 1600)
        self.assertEqual(self.closed, ["77"])
        self.assertEqual(self.names(), ["host_vm_boot_down", "host_vm_boot_up"])

    def state_file(self) -> dict:
        path = self.tmp / "vm-dhcp" / "alert.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def comment(self, number: str, body: str) -> tuple[int, str]:
        if getattr(self, "comment_fails", 0):
            self.comment_fails -= 1
            return 1, "rate limited"
        self.comments.append((number, body))
        return 0, "1"

    def replay(self, at: float) -> dict:
        return vba.alert_pass(now=at, directory=self.tmp / "vm-dhcp", issue=self.issue,
                              close=self.close, comment=self.comment, host="m5", target="m5")

    def test_m5_on_2026_10_09_is_one_issue(self):
        """m5's outage of 2026-10-09 (UTC), event for event from its lane
        logs, became issues #427, #428 and #430. It is one episode: a
        reclassified cause, a reboot and a self-update re-verify in between,
        and recovery at 06:18:30 when a probe got an address."""
        def t(hms: str) -> float:
            return float(calendar.timegm(
                time.strptime(f"2026-10-09T{hms}Z", "%Y-%m-%dT%H:%M:%SZ")))
        self.comments: list[tuple[str, str]] = []
        no_bridge = "lo0 en0 bridge0"
        crashing = "\tstate = spawn scheduled\n\truns = 751\n\tlast exit code = 3\n"
        passes: list[float] = []

        def watch_until(end: float) -> None:
            at = (passes[-1] if passes else t("01:20:00")) + 300
            while at <= end:
                self.replay(at)
                passes.append(at)
                at += 300

        # 01:29-01:33: two no_ips with a healthy-looking chain open it (dhcp_silent).
        self.record("no_ip", t("01:29:10"), lane="m5-pulp-gate-slot2")
        self.record("no_ip", t("01:33:48"), lane="m5-pulp-gate-slot2")
        self.assertEqual(self.state()["cause"], "dhcp_silent")
        # 01:34-02:01: bootpd moves (chain_changed) and probes read a missing
        # VM network; the first probe, on slot2, never reports.
        self.ifaces.write_text(no_bridge)
        self.runs.write_text("4")
        self.check(t("01:34:51"), lane="m5-pulp-gate-slot2")
        self.runs.write_text("5")
        self.check(t("01:37:36"), lane="m5-pulp-gate")
        watch_until(t("01:40:00"))
        self.record("no_ip", t("01:41:36"), lane="m5-pulp-gate")
        watch_until(t("02:02:00"))
        self.assertEqual(len(self.opened), 1, "#427 opens")
        self.assertEqual(self.state_file()["cause"], "vm_dhcp_vm_network_missing")
        # 03:57 reboot; 04:10 post-boot probe; pfd now crash-loops (exit 3).
        self.pfd.write_text(crashing)
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(t("03:57:21"))
        passes.append(t("04:05:00"))           # the host was down: no passes
        self.check(t("04:10:11"), lane="m5-forge-gate")
        self.assertEqual(self.state()["state"], "verifying")
        watch_until(t("04:14:00"))
        self.record("no_ip", t("04:14:16"), lane="m5-forge-gate")
        watch_until(t("05:35:00"))
        # 05:36 a self-update re-verifies; 05:41 its probe gets no address.
        vb.verify("self_update", now=t("05:36:00"))
        self.check(t("05:36:01"), lane="m5-pulp-gate")
        watch_until(t("05:40:00"))
        self.record("no_ip", t("05:41:09"), lane="m5-pulp-gate")
        watch_until(t("06:17:00"))
        # 06:16 the pf reference is held; 06:18:30 a probe gets an address.
        self.pfd.write_text("\tstate = running\n\truns = 752\n\tlast exit code = (never exited)\n")
        self.ifaces.write_text("lo0 en0 bridge0 bridge100 vmenet0")
        self.check(t("06:16:58"), lane="m5-pulp-gate")
        self.record("ip", t("06:18:30"), lane="m5-pulp-gate")
        watch_until(t("06:30:00"))

        self.assertEqual(len(self.opened), 1, "exactly one issue for the whole outage")
        self.assertEqual(self.closed, ["77"], "closed once, on recovery")
        self.assertTrue(self.opened[0][0].startswith(
            "[tartci] m5: cannot boot VMs since 2026-10-09T01:33:48Z"), self.opened[0][0])
        self.assertEqual([body.splitlines()[0] for _, body in self.comments],
                         ["Cause changed: vm_dhcp_vm_network_missing -> vm_dhcp_pfd_crash_loop."])
        self.assertEqual(self.names(), ["host_vm_boot_down", "host_vm_boot_up"])
        up = [json.loads(l) for l in
              (self.tmp / "vm-dhcp" / "events.jsonl").read_text().splitlines()][-1]
        self.assertEqual(up["fields"]["down_s"], int(t("06:20:00") - t("01:33:48")))
        self.assertFalse((self.tmp / "vm-dhcp" / "alert.json").exists())

    def test_a_cause_change_is_one_comment_and_a_failed_one_is_retried(self):
        self.comments = []
        self.open_at(T0)                                    # dhcp_silent
        self.replay(T0 + 200 + vb.PROBE_SECS)               # issue 77
        self.assertEqual(self.state_file()["cause"], "vm_dhcp_unanswered")
        self.ifaces.write_text("lo0 en0")
        self.check(T0 + 800, lane="p")
        self.record("no_ip", T0 + 900, lane="p")            # vm_network_missing
        self.comment_fails = 1
        out = self.replay(T0 + 1000)
        self.assertFalse(out["commented"])
        self.assertEqual(self.state_file()["comment_error"], "rate limited")
        out = self.replay(T0 + 1300)
        self.assertTrue(out["commented"])
        self.replay(T0 + 1600)
        self.assertEqual(len(self.comments), 1, "one comment per change")
        self.assertEqual(self.comments[0][0], "77")
        self.assertIn("vm_dhcp_unanswered -> vm_dhcp_vm_network_missing", self.comments[0][1])
        self.assertNotIn("comment_error", self.state_file())

    def test_a_closed_breaker_without_an_address_does_not_close_the_issue(self):
        self.open_at(T0)
        self.watch(T0 + 200 + vb.PROBE_SECS)                 # issue 77
        # The breaker file is replaced by a closed one that never saw an address.
        (self.tmp / "vm-dhcp" / "breaker.json").write_text(json.dumps(
            {"state": "closed", "streak": [], "boot_time": T0 - 86400,
             "last_ip_at": T0 - 100}))
        self.watch(T0 + 900)
        self.assertEqual(self.closed, [])
        self.assertEqual(self.state_file()["issue"], "77")
        # Control: an address after the episode began closes it.
        self.record("ip", T0 + 1000, lane="q")
        self.watch(T0 + 1100)
        self.assertEqual(self.closed, ["77"])

    def test_a_failed_issue_open_is_retried_while_the_breaker_verifies(self):
        self.comments = []
        self.open_at(T0)                                    # opens at T0 + 200
        self.issue_rc = 1
        out = self.replay(T0 + 200 + vb.PROBE_SECS)         # due; the open fails
        self.assertTrue(out["due"])
        self.assertIsNone(self.state_file().get("issue"))
        self.assertIn("issue_error", self.state_file())
        self.assertEqual(self.names(), ["host_vm_boot_down"])
        # The cause changes while the open keeps failing: there is no issue to
        # comment on, so nothing is posted (never to issues/None).
        self.ifaces.write_text("lo0 en0")
        self.check(T0 + 600, lane="p")
        self.record("no_ip", T0 + 650, lane="p")
        self.assertEqual(self.state()["cause"], "vm_network_missing")
        self.replay(T0 + 700)
        self.assertEqual(self.comments, [])
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(T0 + 1000)
        self.check(T0 + 1100, lane="a")                     # reboot: verifying
        self.assertEqual(self.state()["state"], "verifying")
        self.issue_rc = 0
        out = self.replay(T0 + 1150)                        # not due, still inside the outage
        self.assertFalse(out["due"])
        self.assertEqual(self.state_file().get("issue"), "79",   # the fixture numbers attempts
                         "the retry must not wait for the breaker to reopen")
        self.assertNotIn("issue_error", self.state_file())
        self.assertEqual(self.names(), ["host_vm_boot_down"], "the event is raised once")
        down = json.loads((self.tmp / "vm-dhcp" / "events.jsonl").read_text().splitlines()[0])
        self.assertEqual(down["fields"]["since"], vb.iso(T0 + 200))
        self.assertTrue(self.opened[-1][0].startswith(
            f"[tartci] m5: cannot boot VMs since {vb.iso(T0 + 200)}"), self.opened[-1][0])
        self.record("no_ip", T0 + 1300, lane="a")           # reopened, fresh opened_at
        self.replay(T0 + 1310)
        self.assertEqual(self.closed, [])
        self.assertEqual(self.state_file()["issue"], "79")
        self.record("ip", T0 + 1500, lane="b")
        self.replay(T0 + 1600)
        self.assertEqual(self.closed, ["79"])
        self.assertEqual(self.names(), ["host_vm_boot_down", "host_vm_boot_up"])

    def test_an_issue_first_opened_after_a_reboot_starts_at_the_first_open(self):
        self.open_at(T0)                                    # opens at T0 + 200; not yet due
        self.assertFalse(self.watch(T0 + 300)["due"])
        self.assertEqual(self.state_file(), {})
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(T0 + 350)
        self.check(T0 + 400, lane="a")                      # reboot: verifying
        self.record("no_ip", T0 + 500, lane="a")            # post-boot probe fails: reopened
        out = self.watch(T0 + 510)
        self.assertEqual((out["due"], out["why"]), (True, "a post-boot probe got no address"))
        self.assertEqual(self.state_file()["since"], vb.iso(T0 + 200),
                         "the episode began at the first open, which the reboot carried")
        self.assertTrue(self.opened[0][0].endswith(vb.iso(T0 + 200)), self.opened[0][0])
        self.record("ip", T0 + 900, lane="b")
        self.watch(T0 + 1000)
        up = [json.loads(l) for l in
              (self.tmp / "vm-dhcp" / "events.jsonl").read_text().splitlines()][-1]
        self.assertEqual(up["fields"]["down_s"], 800)

    def test_a_scratch_breaker_never_comments_on_github_unstubbed(self):
        import host_off
        self.comments = []
        self.open_at(T0)
        self.replay(T0 + 200 + vb.PROBE_SECS)               # issue 77, cause recorded
        self.ifaces.write_text("lo0 en0")
        self.check(T0 + 600, lane="p")
        self.record("no_ip", T0 + 650, lane="p")            # the cause changes
        with mock.patch.object(host_off, "_ghapp", return_value=(0, "1")) as real:
            out = vba.alert_pass(now=T0 + 700, directory=self.tmp / "vm-dhcp",
                                 issue=self.issue, close=self.close, host="m5", target="m5")
        real.assert_not_called()
        self.assertFalse(out["commented"])
        # Control: the stubbed comment on the same state is posted.
        self.assertTrue(self.replay(T0 + 1000)["commented"])

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
        self.assertNotIn("vm_dhcp", title, "the cause can change; the title cannot")
        self.assertIn("Cause: vm_dhcp_vm_network_missing", body)
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
