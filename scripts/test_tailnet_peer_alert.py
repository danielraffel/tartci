#!/usr/bin/env python3
"""A fleet host off the tailnet, or with a dead route, is reported.

Fixtures (scripts/fixtures/tailnet/):

- status-*.json were cut from m3's real `tailscale status --json` on
  2026-10-14, keeping the fleet nodes and one unrelated offline phone, with
  node keys and the tailnet name replaced:
  - status-all-online.json: every fleet node online (the phone is offline);
  - status-m1-offline.json: the same, with m1 offline, last seen 20:30:12Z,
    as on 2026-10-14 when Tailscale was turned off on it;
  - status-stopped.json: this host's own tailscaled Stopped.
- hosts.tsv has the shape of fleet-ssh's fleet/hosts.tsv, with the real
  tailnet addresses and documentation (192.0.2.0/24) LAN and Wi-Fi ones.

The network is a fake (`Net`): it answers `tailscale status --json`,
`ssh-keyscan` per address, `ssh-keygen -F` per host key alias, `ssh -G` per
alias, and the read-only address listing.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import host_off
import tailnet_peer_alert as tna
import tartci_incident

FIX = Path(__file__).resolve().parent / "fixtures" / "tailnet"
TABLE = (FIX / "hosts.tsv").read_text()
HOSTS = ["m1", "m5", "m5studio", "studio"]  # published tartci host ids
OFFLINE_SINCE = tna.epoch("2026-10-14T20:30:12.4Z")
CLI = "/fake/tailscale"


def _owners() -> dict:
    """address -> the fleet-ssh name whose host key answers there."""
    owners = {}
    for name, row in tna.parse_table(TABLE).items():
        for kind in ("tailscale", "lan", "wifi"):
            if row[kind]:
                owners[row[kind]] = name
    return owners


class Net:
    """A fake fleet network. Every address answers with its owner's key
    unless listed in `down` (no answer) or `stolen` (another machine's key)."""

    def __init__(self, status: str = "status-all-online.json", down=(), stolen=(),
                 aliases: dict | None = None, addresses: str | None = "en0 192.0.2.76") -> None:
        self.status = (FIX / status).read_text() if status else None
        self.down, self.stolen = set(down), set(stolen)
        self.aliases = aliases or {}
        self.addresses = addresses
        self.calls: list = []
        self.owners = _owners()

    def __call__(self, argv):
        self.calls.append(list(argv))
        tool = argv[0]
        if tool == CLI:
            if self.status is None:
                return 1, "", "failed to connect to local Tailscale service"
            return 0, self.status, ""
        if tool == "ssh-keyscan":
            addr = argv[-1]
            if addr in self.down or addr not in self.owners:
                return 1, "", f"connect to {addr} port 22: Operation timed out"
            owner = "stranger" if addr in self.stolen else self.owners[addr]
            return 0, f"{addr} ssh-ed25519 KEY-{owner}\n", f"# {addr}:22 SSH-2.0-OpenSSH_9.9\n"
        if tool == "ssh-keygen":
            alias = argv[2]
            return 0, f"# Host {alias} found: line 3\n{alias} ssh-ed25519 KEY-{alias}\n", ""
        if tool == "ssh" and argv[1] == "-G":
            alias = argv[2]
            name, _, kind = alias.rpartition("-")
            addr = self.aliases.get(alias) or tna.parse_table(TABLE)[name][kind]
            return 0, f"user ci\nhostname {addr}\nport 22\n", ""
        if tool == "ssh":
            return (0, self.addresses + "\n", "") if self.addresses else (255, "", "timeout")
        raise AssertionError(f"unexpected command {argv}")


class Issues:
    def __init__(self) -> None:
        self.opened: list = []
        self.closed: list = []

    def open(self, title: str, body: str):
        self.opened.append((title, body))
        return 0, str(100 + len(self.opened))

    def close(self, number: str):
        self.closed.append(number)
        return 0, "closed"

    def titles(self) -> list:
        return [title for title, _ in self.opened]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.issues = Issues()
        patcher = mock.patch.dict("os.environ", {"TARTCI_TAILSCALE_CLI": CLI})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tick(self, net: Net, now: float, me: str = "m5", **kw) -> dict:
        return tna.alert_pass(now=now, directory=self.dir / me, hosts=HOSTS, me=me, run=net,
                              issue=self.issues.open, close=self.issues.close,
                              table_text=TABLE, **kw)


class OfflinePeerTests(Base):
    def test_configured_advertised_supply_is_used_without_checkout(self) -> None:
        supply = self.dir / "advertised-labels.json"
        supply.write_text(json.dumps({
            "schema": "tartci.advertised-labels/v1",
            "registrations": [],
            "hosts": [{"host_id": host} for host in HOSTS],
        }) + "\n")
        profile = self.dir / "macos-fleet-profile.toml"
        profile.write_text('[host]\nid = "m5"\nssh = "m5"\n')
        with (
            mock.patch.dict("os.environ", {
                "TARTCI_ADVERTISED_LABELS": str(supply),
                "TARTCI_FLEET_PROFILE": str(profile),
            }, clear=False),
            mock.patch.object(tna.pathlib.Path, "home", return_value=self.dir),
        ):
            out = tna.alert_pass(
                now=OFFLINE_SINCE + 3600,
                directory=self.dir / "configured",
                hosts=None,
                me=None,
                run=Net(),
                issue=self.issues.open,
                close=self.issues.close,
                table_text=TABLE,
            )
        self.assertFalse(out["skipped"])
        self.assertEqual(set(HOSTS), set(out["hosts"]).intersection(HOSTS))

    def test_a_peer_offline_ten_minutes_is_reported_once(self) -> None:
        now = OFFLINE_SINCE + 11 * 60
        out = self.tick(Net("status-m1-offline.json", down={"203.0.113.10"}), now)
        m1 = out["hosts"]["m1"]
        self.assertEqual((m1["state"], m1["active"], m1["acting"]), ("offline", True, True))
        self.assertEqual(m1["last_seen"], "2026-10-14T20:30:12Z")
        self.assertEqual(m1["minutes"], 11)
        self.assertEqual(self.issues.titles(), ["[tartci] m1 is off the tailnet"])
        self.tick(Net("status-m1-offline.json", down={"203.0.113.10"}), now + 300)
        self.assertEqual(len(self.issues.opened), 1)
        events = (self.dir / "m5" / "events.jsonl").read_text()
        self.assertEqual(events.count('"tailnet_offline"'), 1)

    def test_every_route_up_is_quiet(self) -> None:
        # Control, same instrument: every node online and every route
        # answering as its own host. The phone offline for days is not a
        # fleet host and says nothing.
        out = self.tick(Net(), OFFLINE_SINCE + 3600)
        self.assertEqual({h: v["state"] for h, v in out["hosts"].items()},
                         {"m1": "online", "studio": "online", "m5": "online",
                          "m5studio": "online", "macmini": "online", "macpro": "no_tailnet"})
        self.assertEqual(self.issues.opened, [])
        self.assertFalse((self.dir / "m5" / "events.jsonl").exists())
        routes = out["hosts"]["m1"]["routes"]
        self.assertEqual({k: (r["alias"], r["status"]) for k, r in routes.items()},
                         {"lan": ("m1-lan", "up"), "wifi": ("m1-wifi", "up")})

    def test_under_ten_minutes_is_not_yet_an_alert(self) -> None:
        out = self.tick(Net("status-m1-offline.json", down={"203.0.113.10"}),
                        OFFLINE_SINCE + 9 * 60)
        self.assertEqual(out["hosts"]["m1"]["state"], "offline")
        self.assertFalse(out["hosts"]["m1"]["active"])
        self.assertEqual(self.issues.opened, [])

    def test_one_host_acts_the_lowest_reachable_id_other_than_the_peer(self) -> None:
        now = OFFLINE_SINCE + 30 * 60
        acting = {}
        for me in ("studio", "m5", "m5studio"):
            before = len(self.issues.opened)
            out = self.tick(Net("status-m1-offline.json", down={"203.0.113.10"}), now, me=me)
            acting[me] = (out["hosts"]["m1"]["acting"], len(self.issues.opened) - before)
        self.assertEqual(acting, {"m5": (True, 1), "m5studio": (False, 0), "studio": (False, 0)})

    def test_the_peer_back_online_patches_fixed_at_then_closes(self) -> None:
        self.tick(Net("status-m1-offline.json", down={"203.0.113.10"}), OFFLINE_SINCE + 11 * 60)
        back = OFFLINE_SINCE + 4 * 3600
        calls: list = []
        bodies = {"101": self.issues.opened[0][1]}

        def ghapp(args, cwd):
            calls.append(("get", args[1]))
            return 0, bodies["101"]

        def patch(number, body):
            calls.append(("patch", number))
            bodies[number] = body
            return 0, number

        def close(number):
            calls.append(("close", number))
            return 0, "closed"

        with mock.patch.object(host_off, "_ghapp", ghapp), \
                mock.patch.object(host_off, "_patch_issue_body", patch), \
                mock.patch.object(host_off, "_close_issue", close), \
                mock.patch.object(host_off, "_scratch_home", lambda: False):
            out = tna.alert_pass(now=back, directory=self.dir / "m5", hosts=HOSTS, me="m5",
                                 run=Net(), issue=self.issues.open, table_text=TABLE)
        self.assertTrue(out["hosts"]["m1"]["closed"])
        self.assertEqual([kind for kind, _ in calls], ["get", "patch", "close"])
        marker = tartci_incident.parse(bodies["101"])
        self.assertEqual(marker["fixed_at"], tna.iso(back))
        self.assertEqual(marker["symptom_at"], "2026-10-14T20:30:12Z")
        self.assertFalse((self.dir / "m5" / "m1.json").exists())

    def test_a_failed_fix_close_keeps_the_issue_and_retries(self) -> None:
        self.tick(Net("status-m1-offline.json", down={"203.0.113.10"}), OFFLINE_SINCE + 11 * 60)
        failing = mock.Mock(return_value=(1, "HTTP 502"))
        out = tna.alert_pass(now=OFFLINE_SINCE + 3600, directory=self.dir / "m5", hosts=HOSTS,
                             me="m5", run=Net(), issue=self.issues.open, close=failing,
                             table_text=TABLE)
        self.assertTrue(out["hosts"]["m1"]["closed"])
        state = json.loads((self.dir / "m5" / "m1.json").read_text())
        self.assertEqual(state["stale_issues"], ["101"])

    def test_every_command_is_read_only(self) -> None:
        net = Net("status-m1-offline.json", down={"203.0.113.10", "192.0.2.50"})
        self.tick(net, OFFLINE_SINCE + 11 * 60)
        self.tick(net, OFFLINE_SINCE + 30 * 60)
        allowed = {(CLI, "status"), ("ssh-keyscan", "-T"), ("ssh-keygen", "-F"), ("ssh", "-G"),
                   ("ssh", "-n")}
        self.assertEqual({(argv[0], argv[1]) for argv in net.calls} - allowed, set())
        listing = [argv for argv in net.calls if argv[:2] == ["ssh", "-n"]]
        self.assertTrue(listing and all(argv[-1] == tna.ADDRESSES_CMD for argv in listing))


class RouteDownTests(Base):
    """Online in control, but the tailnet path is broken: the case a working
    LAN fallback hides."""

    def test_online_with_a_dead_tailnet_route_and_a_live_lan_alerts(self) -> None:
        net = Net("status-all-online.json", down={"203.0.113.10"})
        first = self.tick(net, OFFLINE_SINCE)
        m1 = first["hosts"]["m1"]
        self.assertEqual(m1["state"], "route_down")
        self.assertEqual(m1["routes"]["lan"]["status"], "up")  # the fallback still works
        self.assertFalse(m1["active"])
        out = self.tick(net, OFFLINE_SINCE + 11 * 60)
        self.assertTrue(out["hosts"]["m1"]["active"])
        self.assertEqual(out["hosts"]["m1"]["since"], tna.iso(OFFLINE_SINCE))
        self.assertEqual(self.issues.titles(), ["[tartci] m1 is off the tailnet"])
        body = self.issues.opened[0][1]
        self.assertIn("Tailscale is on but the tailnet route is down; SSH is falling back to LAN",
                      body)
        for cause in ("relay/DERP", "utun", "route", "ACL"):
            self.assertIn(cause, body)
        self.assertIn("Online per control, but the tailnet path failed", body)
        self.assertIn("`m1-lan` (192.0.2.50): up", body)

    def test_online_with_a_passing_tailnet_probe_is_quiet(self) -> None:
        # Control for the case above: the same node, the probe answering.
        net = Net("status-all-online.json")
        self.tick(net, OFFLINE_SINCE)
        out = self.tick(net, OFFLINE_SINCE + 11 * 60)
        self.assertEqual(out["hosts"]["m1"]["state"], "online")
        self.assertEqual(self.issues.opened, [])

    def test_the_tailnet_address_answering_as_another_host_is_a_route_down(self) -> None:
        net = Net("status-all-online.json", stolen={"203.0.113.10"})
        self.tick(net, OFFLINE_SINCE)
        out = self.tick(net, OFFLINE_SINCE + 11 * 60)
        self.assertEqual(out["hosts"]["m1"]["state"], "route_down")
        self.assertIn("not m1's", out["hosts"]["m1"]["detail"])

    def test_the_offline_case_stays_offline_not_route_down(self) -> None:
        out = self.tick(Net("status-m1-offline.json", down={"203.0.113.10"}),
                        OFFLINE_SINCE + 11 * 60)
        self.assertEqual(out["hosts"]["m1"]["state"], "offline")

    def test_a_host_that_reaches_no_peer_blames_itself_not_them(self) -> None:
        every = {row["tailscale"] for row in tna.parse_table(TABLE).values() if row["tailscale"]}
        net = Net("status-all-online.json", down=every)
        self.tick(net, OFFLINE_SINCE)
        out = self.tick(net, OFFLINE_SINCE + 11 * 60)
        self.assertEqual(out["hosts"]["m5"]["state"], "local_route_down")
        self.assertTrue(out["hosts"]["m5"]["active"])
        self.assertEqual({h: v["state"] for h, v in out["hosts"].items()
                          if h not in ("m5", "macpro")},
                         {h: "unknown" for h in ("m1", "studio", "m5studio", "macmini")})
        self.assertEqual(self.issues.titles(), ["[tartci] m5 is off the tailnet"])


class OptionalRouteTests(Base):
    """m5 is a laptop: its Ethernet exists only when it is docked."""

    def table(self, optional: str) -> str:
        return "\n".join(line + (f"|{optional}" if line.startswith("m5|") else "")
                         for line in TABLE.splitlines())

    def run_pair(self, optional: str) -> dict:
        net = Net(down={"192.0.2.81"})
        for at in (OFFLINE_SINCE, OFFLINE_SINCE + 11 * 60):
            out = tna.alert_pass(now=at, directory=self.dir / "m1", hosts=HOSTS, me="m1",
                                 run=net, issue=self.issues.open, close=self.issues.close,
                                 table_text=self.table(optional))
        return out

    def test_an_optional_route_down_opens_no_issue_but_shows_in_status(self) -> None:
        out = self.run_pair("lan")
        route = out["hosts"]["m5"]["routes"]["lan"]
        self.assertEqual((route["status"], route["optional"], route["active"]),
                         ("down", True, False))
        self.assertEqual(self.issues.opened, [])
        self.assertIn("m5: tailnet up, m5-lan down (optional), m5-wifi up",
                      tna.route_summary(out["hosts"]))

    def test_the_same_route_not_optional_alerts(self) -> None:
        out = self.run_pair("")
        self.assertTrue(out["hosts"]["m5"]["routes"]["lan"]["active"])
        self.assertEqual(self.issues.titles(), ["[tartci] m5-lan fallback route to m5 is broken"])

    def test_optional_never_covers_the_tailnet_route(self) -> None:
        rows = tna.parse_table(self.table("lan wifi tailscale"))
        self.assertEqual(rows["m5"]["optional"], ["lan", "wifi"])
        self.assertEqual(rows["m1"]["optional"], [])

    def test_a_route_made_optional_closes_its_open_issue(self) -> None:
        self.run_pair("")
        self.assertEqual(len(self.issues.opened), 1)
        tna.alert_pass(now=OFFLINE_SINCE + 20 * 60, directory=self.dir / "m1", hosts=HOSTS,
                       me="m1", run=Net(down={"192.0.2.81"}), issue=self.issues.open,
                       close=self.issues.close, table_text=self.table("lan"))
        self.assertEqual(self.issues.closed, ["101"])


class LanBlindTests(Base):
    """m5s, 2026-10-15: its sealed launcher reached every host over the tailnet
    and none over the LAN, while an interactive shell on it reached them all."""

    LAN = {row[kind] for row in tna.parse_table(TABLE).values()
           for kind in ("lan", "wifi") if row[kind]}

    def test_a_host_that_reaches_no_lan_route_blames_itself_not_the_routes(self) -> None:
        net = Net(down=self.LAN)
        self.tick(net, OFFLINE_SINCE, me="m1")
        out = self.tick(net, OFFLINE_SINCE + 11 * 60, me="m1")
        me = out["hosts"]["m1"]
        self.assertTrue(me["lan_blind"]["active"])
        self.assertEqual({r["status"] for v in out["hosts"].values()
                          for r in v["routes"].values()}, {"unchecked"})
        self.assertEqual(self.issues.titles(), ["[tartci] m1 cannot reach the LAN from tartci"])
        body = self.issues.opened[0][1]
        self.assertIn("If several hosts report this at once, suspect the LAN itself", body)
        self.assertIn("route -n get", body)
        self.assertEqual(tartci_incident.parse(body)["class"], "lan_blind")

    def test_a_first_or_short_blind_pass_opens_nothing(self) -> None:
        net = Net(down=self.LAN)
        for at in (OFFLINE_SINCE, OFFLINE_SINCE + 9 * 60):
            out = self.tick(net, at, me="m1")
            self.assertFalse(out["hosts"]["m1"]["lan_blind"]["active"])
            self.assertEqual(self.issues.titles(), [])
        self.tick(net, OFFLINE_SINCE + 11 * 60, me="m1")
        self.assertEqual(self.issues.titles(), ["[tartci] m1 cannot reach the LAN from tartci"])

    def test_one_other_host_with_a_dead_lan_is_its_fault_not_ours(self) -> None:
        # Exactly one other tailnet-online host has routes, and all of them
        # are down: that host's LAN died; this host is not blind.
        table = "\n".join(line for line in TABLE.splitlines()
                          if line.startswith(("#", "m1|", "m5|")))
        net = Net(down={"192.0.2.81", "192.0.2.60"})
        for at in (OFFLINE_SINCE, OFFLINE_SINCE + 11 * 60):
            out = tna.alert_pass(now=at, directory=self.dir / "m1", hosts=HOSTS, me="m1",
                                 run=net, issue=self.issues.open, close=self.issues.close,
                                 table_text=table)
        self.assertNotIn("lan_blind", out["hosts"]["m1"])
        self.assertEqual(sorted(self.issues.titles()),
                         ["[tartci] m5-lan fallback route to m5 is broken",
                          "[tartci] m5-wifi fallback route to m5 is broken"])

    def test_one_hosts_dead_lan_is_still_that_hosts_route(self) -> None:
        # Control, same instrument: only m5's routes are down, so the LAN is
        # reachable from here and m5's routes are reported as broken.
        net = Net(down={"192.0.2.81", "192.0.2.60"})
        self.tick(net, OFFLINE_SINCE, me="m1")
        out = self.tick(net, OFFLINE_SINCE + 11 * 60, me="m1")
        self.assertNotIn("lan_blind", out["hosts"]["m1"])
        self.assertEqual(sorted(self.issues.titles()),
                         ["[tartci] m5-lan fallback route to m5 is broken",
                          "[tartci] m5-wifi fallback route to m5 is broken"])

    def test_the_lan_coming_back_closes_the_blind_issue(self) -> None:
        self.tick(Net(down=self.LAN), OFFLINE_SINCE, me="m1")
        self.tick(Net(down=self.LAN), OFFLINE_SINCE + 11 * 60, me="m1")
        out = self.tick(Net(), OFFLINE_SINCE + 20 * 60, me="m1")
        self.assertNotIn("lan_blind", out["hosts"]["m1"])
        self.assertEqual(self.issues.closed, ["101"])
        self.assertFalse((self.dir / "m1" / "m1.lan-blind.json").exists())

    def test_the_watchdog_line_names_a_blind_host(self) -> None:
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        fake = {"skipped": False, "table": "", "hosts": {
            "m5studio": {"state": "online", "routes": {},
                         "lan_blind": {"active": True, "since": "2026-10-15T03:47:54Z"}}}}
        with mock.patch.object(tna, "alert_pass", return_value=fake):
            line = watchdog.tailnet_pass(now=OFFLINE_SINCE)
        self.assertIn("this host (m5studio) cannot reach the LAN since 2026-10-15T03:47:54Z", line)


class FallbackRouteTests(Base):
    def test_a_dead_lan_route_is_reported_with_the_hosts_real_addresses(self) -> None:
        net = Net(down={"192.0.2.50"}, addresses="en0 192.0.2.76\nen7 192.0.2.51")
        first = self.tick(net, OFFLINE_SINCE)
        # Under ten minutes: recorded, not yet an alert.
        self.assertEqual(first["hosts"]["m1"]["routes"]["lan"]["status"], "down")
        self.assertFalse(first["hosts"]["m1"]["routes"]["lan"]["active"])
        self.assertEqual(self.issues.opened, [])
        out = self.tick(net, OFFLINE_SINCE + 11 * 60)
        route = out["hosts"]["m1"]["routes"]["lan"]
        self.assertEqual((route["status"], route["active"]), ("down", True))
        self.assertEqual(out["hosts"]["m1"]["state"], "online")  # tailnet fine
        self.assertEqual(self.issues.titles(), ["[tartci] m1-lan fallback route to m1 is broken"])
        body = self.issues.opened[0][1]
        self.assertIn("`m1-lan` (192.0.2.50) has not answered", body)
        self.assertIn("m1 reports its addresses as: en0 192.0.2.76, en7 192.0.2.51", body)
        self.assertIn("No. tartci never edits ssh config", body)
        marker = tartci_incident.parse(body)
        self.assertEqual((marker["host"], marker["class"]), ("m1:lan", "fallback_route_broken"))

    def test_a_stale_address_now_owned_by_another_machine_is_wrong_host(self) -> None:
        net = Net(stolen={"192.0.2.76"})
        self.tick(net, OFFLINE_SINCE)
        out = self.tick(net, OFFLINE_SINCE + 11 * 60)
        self.assertEqual(out["hosts"]["m1"]["routes"]["wifi"]["status"], "wrong_host")
        self.assertEqual(self.issues.titles(), ["[tartci] m1-wifi fallback route to m1 is broken"])

    def test_an_ssh_alias_pointing_away_from_the_table_is_stale(self) -> None:
        # 2026-10-14: m1-lan still named an address m1 no longer had.
        net = Net(aliases={"m1-lan": "192.0.2.97"})
        self.tick(net, OFFLINE_SINCE)
        out = self.tick(net, OFFLINE_SINCE + 11 * 60)
        route = out["hosts"]["m1"]["routes"]["lan"]
        self.assertEqual((route["status"], route["alias_addr"]), ("stale_alias", "192.0.2.97"))
        body = self.issues.opened[0][1]
        self.assertIn("points at 192.0.2.97, but the fleet-ssh table lists m1's lan address "
                      "as 192.0.2.50", body)

    def test_a_route_back_up_closes_its_issue(self) -> None:
        self.tick(Net(down={"192.0.2.50"}), OFFLINE_SINCE)
        self.tick(Net(down={"192.0.2.50"}), OFFLINE_SINCE + 11 * 60)
        out = self.tick(Net(), OFFLINE_SINCE + 20 * 60)
        self.assertTrue(out["hosts"]["m1"]["routes"]["lan"]["closed"])
        self.assertEqual(self.issues.closed, ["101"])

    def test_hosts_without_a_tartci_profile_are_checked_too(self) -> None:
        # macpro publishes no tartci profile, so the lowest reachable published
        # host (m1) reports it; m5 only records it.
        net = Net(down={"192.0.2.43"})
        self.tick(net, OFFLINE_SINCE, me="m5")
        self.assertFalse(self.tick(net, OFFLINE_SINCE + 11 * 60, me="m5")["hosts"]["macpro"]["acting"])
        self.tick(net, OFFLINE_SINCE, me="m1")
        out = self.tick(net, OFFLINE_SINCE + 11 * 60, me="m1")
        self.assertEqual(out["hosts"]["macpro"]["state"], "no_tailnet")
        self.assertTrue(out["hosts"]["macpro"]["routes"]["lan"]["active"])
        self.assertEqual(self.issues.titles(), ["[tartci] macpro-lan fallback route to macpro is broken"])

    def test_a_reader_whose_tailnet_route_is_down_does_not_act(self) -> None:
        # From m5, m1 (the lowest id) reads route_down, so m5 reports macpro.
        net = Net(down={"203.0.113.10", "192.0.2.43"})
        self.tick(net, OFFLINE_SINCE, me="m5")
        out = self.tick(net, OFFLINE_SINCE + 11 * 60, me="m5")
        self.assertEqual(out["hosts"]["m1"]["state"], "route_down")
        self.assertTrue(out["hosts"]["macpro"]["acting"])
        self.assertIn("[tartci] macpro-lan fallback route to macpro is broken", self.issues.titles())

    def test_route_summary_reports_which_routes_are_up(self) -> None:
        out = self.tick(Net(down={"192.0.2.50"}), OFFLINE_SINCE)
        lines = tna.route_summary(out["hosts"])
        self.assertIn("m1: tailnet up, m1-lan down, m1-wifi up", lines)
        self.assertIn("macpro: tailnet none, macpro-lan up", lines)


class LocalTests(Base):
    def test_a_stopped_local_tailscaled_is_reported_by_the_host_itself(self) -> None:
        first = OFFLINE_SINCE
        out = self.tick(Net("status-stopped.json"), first, me="studio")
        me = out["hosts"]["studio"]
        self.assertEqual((me["state"], me["detail"], me["active"]), ("local", "Stopped", False))
        # A host off the tailnet says nothing about its peers' tailnet state.
        self.assertEqual({h: v["state"] for h, v in out["hosts"].items()
                          if h not in ("studio", "macpro")},
                         {"m1": "unknown", "m5": "unknown", "m5studio": "unknown",
                          "macmini": "unknown"})
        out = self.tick(Net("status-stopped.json"), first + 11 * 60, me="studio")
        self.assertTrue(out["hosts"]["studio"]["active"])
        self.assertEqual(out["hosts"]["studio"]["since"], tna.iso(first))
        self.assertEqual(self.issues.titles(), ["[tartci] studio is off the tailnet"])
        self.assertIn("Stopped", self.issues.opened[0][1])

    def test_an_unreachable_daemon_is_reported_too(self) -> None:
        self.tick(Net(status=""), OFFLINE_SINCE, me="studio")
        out = self.tick(Net(status=""), OFFLINE_SINCE + 11 * 60, me="studio")
        self.assertEqual(out["hosts"]["studio"]["state"], "local")
        self.assertIn("tailscaled unreadable", out["hosts"]["studio"]["detail"])
        self.assertEqual(len(self.issues.opened), 1)

    def test_a_logged_out_daemon_is_reported(self) -> None:
        net = Net("status-stopped.json")
        value = json.loads(net.status)
        value["BackendState"] = "NeedsLogin"
        net.status = json.dumps(value)
        self.tick(net, OFFLINE_SINCE, me="studio")
        out = self.tick(net, OFFLINE_SINCE + 11 * 60, me="studio")
        self.assertEqual(out["hosts"]["studio"]["detail"], "NeedsLogin")
        self.assertEqual(len(self.issues.opened), 1)


class BodyAndMappingTests(Base):
    def test_the_body_is_actionable_and_ends_with_the_marker(self) -> None:
        self.tick(Net("status-m1-offline.json", down={"203.0.113.10"}), OFFLINE_SINCE + 4 * 3600)
        title, body = self.issues.opened[0]
        for needle in ("## What happened", "offline on the tailnet for 240 min (4.0 h)",
                       "last seen 2026-10-14T20:30:12Z", "## Evidence",
                       "daniels-macbook-pro-1.example-tailnet.ts.net", "## Likely cause",
                       "Tailscale turned off", "asleep", "power", "## Fix",
                       "open -a Tailscale", "## Safe to auto-apply", "No. tartci never starts",
                       "## How to verify", "`m1-lan` (192.0.2.50): up"):
            self.assertIn(needle, body)
        marker = tartci_incident.parse(body)
        self.assertEqual((marker["host"], marker["class"], marker["fixed_at"]),
                         ("m1", "tailnet_offline", None))

    def test_a_published_host_without_a_node_is_reported_not_guessed(self) -> None:
        table = TABLE.replace("203.0.113.10", "203.0.113.16")
        out = tna.alert_pass(now=OFFLINE_SINCE, directory=self.dir / "m5", hosts=HOSTS, me="m5",
                             run=Net("status-m1-offline.json"), issue=self.issues.open,
                             close=self.issues.close, table_text=table)
        self.assertEqual(out["hosts"]["m1"]["state"], "unmapped")
        self.assertEqual(self.issues.opened, [])

    def test_a_published_host_the_table_omits_is_unmapped(self) -> None:
        table = "\n".join(line for line in TABLE.splitlines() if not line.startswith("m1|"))
        out = tna.alert_pass(now=OFFLINE_SINCE, directory=self.dir / "m5", hosts=HOSTS, me="m5",
                             run=Net(), issue=self.issues.open, close=self.issues.close,
                             table_text=table)
        self.assertEqual((out["hosts"]["m1"]["state"], out["hosts"]["m1"]["detail"]),
                         ("unmapped", "no row in the fleet-ssh table"))
        self.assertEqual(self.issues.opened, [])

    def test_this_repository_holds_no_fleet_address(self) -> None:
        # Addresses live only in the private fleet-ssh table; the published
        # supply and the profiles carry none.
        root = Path(__file__).resolve().parents[1]
        published = json.loads((root / "fleet" / "advertised-labels.json").read_text())
        self.assertTrue(all("tailnet_ip" not in row for row in published["hosts"]))
        for profile in (root / "profiles").glob("*-macos-fleet.toml"):
            self.assertNotIn("tailnet_ip", profile.read_text(), profile.name)

    def test_table_rows_map_to_published_ids(self) -> None:
        fleet = tna.fleet_routes(HOSTS, tna.parse_table(TABLE))
        self.assertEqual(fleet["studio"]["name"], "m3")
        self.assertEqual(fleet["m5studio"]["name"], "m5s")
        self.assertEqual(fleet["macpro"]["tailscale"], "")
        self.assertEqual(sorted(fleet), ["m1", "m5", "m5studio", "macmini", "macpro", "studio"])

    def test_go_zero_time_and_long_fractions_parse(self) -> None:
        self.assertIsNone(tna.epoch("0001-01-01T00:00:00Z"))
        self.assertEqual(tna.iso(tna.epoch("2026-10-14T20:30:12.4Z")), "2026-10-14T20:30:12Z")
        self.assertEqual(tna.iso(tna.epoch("2026-10-14T20:30:12.123456789Z")),
                         "2026-10-14T20:30:12Z")

    def test_an_unreadable_table_falls_back_to_the_last_copy(self) -> None:
        directory = self.dir / "t"
        directory.mkdir()
        table, note = tna.load_table(directory, OFFLINE_SINCE, fetch=lambda: TABLE)
        self.assertIn("m1", table)
        self.assertEqual(note, "")

        def fail():
            raise RuntimeError("HTTP 502")
        table, note = tna.load_table(directory, (directory / "hosts.tsv").stat().st_mtime + 7200,
                                     fetch=fail)
        self.assertIn("m1", table)
        self.assertIn("using a copy 120 min old", note)
        (self.dir / "empty").mkdir()
        table, note = tna.load_table(self.dir / "empty", OFFLINE_SINCE, fetch=fail)
        self.assertEqual(table, {})
        self.assertIn("fallback routes not checked", note)


class WatchdogTests(Base):
    def test_the_watchdog_line_names_each_problem(self) -> None:
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        fake = {"skipped": False, "table": "", "hosts": {
            "m1": {"state": "offline", "active": True, "since": "2026-10-14T20:30:12Z",
                   "minutes": 240, "routes": {}},
            "m5": {"state": "route_down", "active": True, "since": "2026-10-14T21:00:00Z",
                   "minutes": 15, "routes": {"lan": {"active": True, "alias": "m5-lan",
                                                     "addr": "192.0.2.81", "status": "down",
                                                     "since": "2026-10-14T21:00:00Z"}}},
            "m5studio": {"state": "unmapped", "detail": "no tailnet node has 203.0.113.16"}}}
        with mock.patch.object(tna, "alert_pass", return_value=fake):
            line = watchdog.tailnet_pass(now=OFFLINE_SINCE + 4 * 3600)
        self.assertIn("WARN tailnet: m1 offline since 2026-10-14T20:30:12Z (240 min)", line)
        self.assertIn("m5 online in control but its tailnet route is down", line)
        self.assertIn("m5-lan (192.0.2.81) down since 2026-10-14T21:00:00Z", line)
        self.assertIn("m5studio has no tailnet node", line)
        with mock.patch.object(tna, "alert_pass", return_value={"skipped": False, "hosts": {
                "m1": {"state": "online", "routes": {}}}}):
            self.assertIsNone(watchdog.tailnet_pass(now=OFFLINE_SINCE))



class ConfiguredFleetSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        import test_fleet_source  # noqa: PLC0415 - owns what a valid source is
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        env = test_fleet_source.configured_source(self.dir / "source", {
            "hosts": [{"host_id": host} for host in HOSTS]})
        patcher = mock.patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TARTCI_ADVERTISED_LABELS", None)

    @staticmethod
    def git(argv):
        raise AssertionError(f"read the public supply: {argv}")

    def test_a_configured_source_is_read_without_git(self) -> None:
        import vm_boot_alert
        with mock.patch.object(vm_boot_alert, "_alert_host", return_value=("m5", "m5")):
            hosts, me = tna.published_hosts(home=self.dir, run=self.git)
        self.assertEqual((sorted(HOSTS), "m5"), (hosts, me))

    def test_an_invalid_source_refuses_without_falling_back(self) -> None:
        (self.dir / "source" / "fleet" / "advertised-labels.json").write_text("{}\n")
        with self.assertRaisesRegex(RuntimeError, "published supply refused"):
            tna.published_hosts(home=self.dir, run=self.git)


if __name__ == "__main__":
    unittest.main()
