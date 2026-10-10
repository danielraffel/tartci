#!/usr/bin/env python3
"""A peer reports a host whose launchd stopped starting its timers (peer_stall_alert.py)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import host_off  # noqa: E402
import peer_stall_alert as psa  # noqa: E402

# m3 ("studio") on 2026-10-09: its guard's receipt, trimmed to three of its
# eighteen stalled agents. The episode began 2026-10-05T05:41:27Z.
STARTED = 1791178887.886657
RECEIPT_TS = 1791529245.2471092
M3_RECEIPT = {
    "agents_checked": 27,
    "episode": {"active": True, "started_ts": STARTED,
                "labels": ["com.danielraffel.tartci.launchd-watchdog",
                           "com.danielraffel.tartci.reap",
                           "com.danielraffel.tartci.self-update"]},
    "errors": [], "kicked": [], "owner_pid": 90773, "runner": "studio-forge-gate-01",
    "paused": [{"interval": 1800, "label": "com.danielraffel.tartci.self-update",
                "seconds_since_progress": 350718}],
    "stalled": [{"interval": 300, "label": "com.danielraffel.tartci.launchd-watchdog"},
                {"interval": 300, "label": "com.danielraffel.tartci.reap"},
                {"interval": 1800, "label": "com.danielraffel.tartci.self-update"}],
    "ts": RECEIPT_TS,
}
TITLE = "[tartci] studio launchd stalled / self-update paused since 2026-10-05T05:41:27Z"
PEERS = {"m1": "m1", "m5": "m5", "studio": "m3"}


class Fleet:
    """Every peer's SSH answer, by target."""

    def __init__(self) -> None:
        self.answers: dict[str, tuple[int, str, str]] = {}
        self.calls: list[str] = []

    def guard(self, target: str, receipt: dict | None, clock: float = RECEIPT_TS + 60,
              last: dict | None = None) -> None:
        body = json.dumps(receipt) if receipt is not None else ""
        tail = json.dumps(last) if last is not None else ""
        self.answers[target] = (0, f"{int(clock)}\n{psa.SEPARATOR}\n{body}\n"
                                   f"{psa.LAST_SEPARATOR}\n{tail}\n", "")

    def healthy(self, target: str) -> None:
        self.guard(target, {**M3_RECEIPT, "episode": {"active": False}, "stalled": [],
                            "paused": []})

    def run(self, argv: list[str]) -> tuple[int, str, str]:
        target = argv[argv.index("ConnectTimeout=10") + 1]
        self.calls.append(target)
        return self.answers.get(target, (255, "", "ssh: connect to host: Operation timed out"))


class PeerStall(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fleet = Fleet()
        for target in ("m1", "m5"):
            self.fleet.healthy(target)
        self.fleet.guard("m3", M3_RECEIPT)
        self.opened: list[tuple[str, str]] = []
        self.closed: list[str] = []

    def issue(self, title: str, body: str) -> tuple[int, str]:
        self.opened.append((title, body))
        return 0, str(500 + len(self.opened))

    def close(self, number: str) -> tuple[int, str]:
        self.closed.append(number)
        return 0, "closed"

    def run_pass(self, at: float, host: str = "m1", **kw) -> dict:
        kw.setdefault("issue", self.issue)
        return psa.alert_pass(now=at, directory=self.tmp / host, peers=PEERS, me=host,
                              run=self.fleet.run, close=self.close, **kw)

    def test_m3_stalled_97_h_is_one_issue_named_by_a_peer(self):
        out = self.run_pass(RECEIPT_TS + 60)
        self.assertTrue(out["peers"]["studio"]["active"])
        self.assertEqual(out["peers"]["studio"]["hours"], 97.3)
        self.assertEqual([t for t, _ in self.opened], [TITLE])
        body = self.opened[0][1].splitlines()
        self.assertIn("The guard lists self-update as paused", body[0])
        self.assertEqual(body[1], "Run: ssh m3 'tartci doctor fleet'")
        self.assertTrue(body[2].startswith("Fix: reboot studio when its lanes are idle"))
        self.assertFalse(out["peers"]["m5"]["active"])
        self.assertEqual(out["peers"]["studio"]["why"], "primary")
        self.assertNotIn("m1", out["peers"], "a host never reads itself")
        self.assertNotIn("m1", self.fleet.calls)

    def test_a_short_stall_is_not_reported(self):
        # Negative control: the same receipt, an episode two hours old.
        self.fleet.guard("m3", {**M3_RECEIPT, "episode": {
            **M3_RECEIPT["episode"], "started_ts": RECEIPT_TS - 7200}})
        out = self.run_pass(RECEIPT_TS + 60)
        self.assertFalse(out["peers"]["studio"]["active"])
        self.assertEqual(out["peers"]["studio"]["hours"], 2.0)
        self.assertEqual(self.opened, [])

    def test_the_peer_is_judged_on_its_own_clock(self):
        # This host's clock is a day ahead; the peer's receipt is fresh on its own.
        out = self.run_pass(RECEIPT_TS + 86400)
        self.assertEqual(out["peers"]["studio"]["state"], "stalled")
        self.assertEqual(len(self.opened), 1)

    def test_a_reboot_that_ends_the_stall_closes_the_issue(self):
        self.run_pass(RECEIPT_TS + 60)
        self.fleet.healthy("m3")
        out = self.run_pass(RECEIPT_TS + 60 + psa.READ_SECS)
        self.assertTrue(out["peers"]["studio"]["closed"])
        self.assertEqual(self.closed, ["501"])
        self.assertFalse((self.tmp / "m1" / "studio.json").exists())

    def test_unreachable_stale_or_never_decides_nothing(self):
        self.run_pass(RECEIPT_TS + 60)
        at = RECEIPT_TS + 60
        for answer in ((255, "", "ssh: Operation timed out"),         # rebooting
                       None,                                           # stale receipt
                       (0, f"{int(RECEIPT_TS)}\n{psa.SEPARATOR}\n\n", "")):  # never ran
            at += psa.READ_SECS
            if answer is None:
                self.fleet.guard("m3", M3_RECEIPT, clock=RECEIPT_TS + 3600)
            else:
                self.fleet.answers["m3"] = answer
            out = self.run_pass(at)
            self.assertFalse(out["peers"]["studio"]["closed"])
            self.assertFalse(out["peers"]["studio"]["active"])
        self.assertEqual(self.closed, [], "the issue stays open until a read shows the stall over")
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(json.loads((self.tmp / "m1" / "studio.json").read_text())["issue"], "501")

    def fresh_primary_pass(self, ok: bool = True) -> dict:
        """m1's own last pass as m5 would read it over SSH."""
        return {"ts": RECEIPT_TS, "peers": {"studio": {"state": "stalled",
                                                       "error": None if ok else "exit 255"}}}

    def test_only_the_primary_acts_while_it_reads_the_peer(self):
        self.fleet.guard("m1", {**M3_RECEIPT, "episode": {"active": False}, "stalled": [],
                                "paused": []}, last=self.fresh_primary_pass())
        for k in range(3):
            out = self.run_pass(RECEIPT_TS + 60 + k * psa.READ_SECS, host="m5")
            self.assertFalse(out["peers"]["studio"]["acting"])
            self.assertEqual(out["peers"]["studio"]["why"], "m1 is the primary reader")
        self.assertEqual(self.opened, [])
        self.assertTrue(out["peers"]["studio"]["active"], "it still sees the stall")

    def test_a_fallback_acts_after_the_primary_misses_two_reads(self):
        for answer in ((255, "", "ssh: Operation timed out"), None):
            self.setUp()
            if answer is None:     # reachable, but its own pass could not read studio
                self.fleet.guard("m1", {**M3_RECEIPT, "episode": {"active": False},
                                        "stalled": [], "paused": []},
                                 last=self.fresh_primary_pass(ok=False))
            else:
                self.fleet.answers["m1"] = answer
            first = self.run_pass(RECEIPT_TS + 60, host="m5")
            self.assertFalse(first["peers"]["studio"]["acting"])
            self.assertEqual(self.opened, [], "one miss is not enough")
            second = self.run_pass(RECEIPT_TS + 60 + psa.READ_SECS, host="m5")
            self.assertTrue(second["peers"]["studio"]["acting"])
            self.assertEqual([t for t, _ in self.opened], [TITLE])

    def test_a_stale_primary_pass_counts_as_a_miss(self):
        stale = {**self.fresh_primary_pass(), "ts": RECEIPT_TS - 3 * psa.READ_SECS}
        self.fleet.guard("m1", {**M3_RECEIPT, "episode": {"active": False}, "stalled": [],
                                "paused": []}, last=stale)
        self.run_pass(RECEIPT_TS + 60, host="m5")
        out = self.run_pass(RECEIPT_TS + 60 + psa.READ_SECS, host="m5")
        self.assertTrue(out["peers"]["studio"]["acting"])

    def test_the_primary_is_the_lowest_published_id_other_than_the_peer(self):
        hosts = ["m1", "m5", "m5studio", "studio"]
        self.assertEqual(psa.primary_reader("studio", hosts), "m1")
        self.assertEqual(psa.primary_reader("m1", hosts), "m5")
        self.assertIsNone(psa.primary_reader("m1", ["m1"]))

    def test_a_stale_ok_receipt_does_not_close(self):
        self.run_pass(RECEIPT_TS + 60)
        ok = {**M3_RECEIPT, "episode": {"active": False}, "stalled": [], "paused": []}
        self.fleet.guard("m3", ok, clock=RECEIPT_TS + 3600)        # written 60 min ago
        out = self.run_pass(RECEIPT_TS + 60 + psa.READ_SECS)
        self.assertFalse(out["peers"]["studio"]["closed"])
        self.assertEqual(self.closed, [])

    def test_an_ok_receipt_written_before_the_episode_does_not_close(self):
        self.run_pass(RECEIPT_TS + 60)
        # The peer's clock went back: an "ok" receipt that reads fresh but was
        # written before the episode this issue is about began.
        ok = {**M3_RECEIPT, "ts": STARTED - 600, "episode": {"active": False},
              "stalled": [], "paused": []}
        self.fleet.guard("m3", ok, clock=STARTED - 540)
        out = self.run_pass(RECEIPT_TS + 60 + psa.READ_SECS)
        self.assertFalse(out["peers"]["studio"]["closed"])
        # Control: the same receipt written after the start closes it.
        self.fleet.guard("m3", {**ok, "ts": RECEIPT_TS + 100}, clock=RECEIPT_TS + 160)
        out = self.run_pass(RECEIPT_TS + 60 + 2 * psa.READ_SECS)
        self.assertTrue(out["peers"]["studio"]["closed"])

    def test_reads_at_most_once_per_interval(self):
        self.run_pass(RECEIPT_TS + 60)
        calls = len(self.fleet.calls)
        out = self.run_pass(RECEIPT_TS + 60 + psa.READ_SECS - 1)
        self.assertTrue(out["skipped"])
        self.assertEqual(len(self.fleet.calls), calls)
        self.run_pass(RECEIPT_TS + 60 + psa.READ_SECS)
        self.assertEqual(len(self.fleet.calls), 2 * calls)

    def test_every_reader_adopts_the_same_issue(self):
        """Three healthy hosts read m3; GitHub ends with one open issue."""
        github: dict[str, dict] = {}

        def ghapp(args: list[str], cwd) -> tuple[int, str]:
            if "-X" in args and "POST" in args:
                number = str(600 + len(github))
                title = next(a[len("title="):] for a in args if a.startswith("title="))
                github[number] = {"title": title, "state": "open"}
                return 0, number
            if "-X" in args and "PATCH" in args:
                github[args[args.index("PATCH") + 1].rsplit("/", 1)[1]]["state"] = "closed"
                return 0, "closed"
            query = args[args.index("--jq") + 1]
            return 0, "\n".join(n for n, v in github.items()
                                if v["state"] == "open" and f'"{v["title"]}"' in query)

        with mock.patch.object(host_off, "_ghapp", side_effect=ghapp), \
                mock.patch.object(host_off, "_scratch_home", return_value=False), \
                mock.patch.object(psa, "_scratch", return_value=False):
            # m1 is studio's primary. The fleet double serves no m1 pass, so
            # m5 and m5studio count misses and fall back on their second read:
            # they adopt m1's issue by title rather than open another.
            for at in (RECEIPT_TS + 60, RECEIPT_TS + 60 + psa.READ_SECS):
                for host in ("m1", "m5", "m5studio"):
                    psa.alert_pass(now=at, directory=self.tmp / host, peers=PEERS,
                                   me=host, run=self.fleet.run)
            self.assertEqual([v["title"] for v in github.values()], [TITLE])
            self.assertEqual({json.loads((self.tmp / h / "studio.json").read_text())["issue"]
                              for h in ("m1", "m5", "m5studio")}, {"600"})
            # Recovery read by one host closes the fleet's one issue.
            self.fleet.healthy("m3")
            psa.alert_pass(now=RECEIPT_TS + 60 + 2 * psa.READ_SECS, directory=self.tmp / "m1",
                           peers=PEERS, me="m1", run=self.fleet.run)
            self.assertEqual(github["600"]["state"], "closed")

    def test_a_scratch_directory_never_reaches_github_unstubbed(self):
        with mock.patch.object(psa, "_open_or_adopt") as real:
            psa.alert_pass(now=RECEIPT_TS + 60, directory=self.tmp / "m1", peers=PEERS,
                           me="m1", run=self.fleet.run)
        real.assert_not_called()
        self.assertTrue(json.loads((self.tmp / "m1" / "studio.json").read_text())["evented"])

    def test_peers_are_the_published_supply_as_self_update_reads_them(self):
        import fleet_self_update as fsu
        supply = {"hosts": [{"host_id": "studio", "ssh": "m3"}, {"host_id": "m1"}],
                  "registrations": [{"host_id": "m5"}, {"host_id": "studio"}]}

        def git(argv: list[str]) -> tuple[int, str, str]:
            return 0, json.dumps(supply), ""
        import vm_boot_alert
        with mock.patch.object(vm_boot_alert, "_alert_host", return_value=("m5", "m5")):
            peers, me = psa.published_peers(home=self.tmp, run=git)
        expected = {"m1": "tartci-m1", "m5": "tartci-m5", "studio": "m3"}
        self.assertEqual((peers, me), (expected, "m5"))
        # A node name that names no published host (no profile read): read every host.
        with mock.patch.object(vm_boot_alert, "_alert_host",
                               return_value=("Daniels-Mac-Studio", "x")):
            self.assertIsNone(psa.published_peers(home=self.tmp, run=git)[1])
        cfg = fsu.Config(home=self.tmp)
        with mock.patch.object(fsu, "published_supply", return_value=supply):
            self.assertEqual(fsu.published_peers(cfg, fsu.System()), expected,
                             "the same list self-update's turn reads")
        with self.assertRaises(RuntimeError):
            psa.published_peers(home=self.tmp, run=lambda argv: (128, "", "fatal: bad object"))

    def test_a_host_without_a_self_update_checkout_is_quiet(self):
        home = self.tmp / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch.object(psa, "published_peers") as peers:
            out = psa.alert_pass(now=RECEIPT_TS, directory=self.tmp / "quiet")
        peers.assert_not_called()
        self.assertEqual((out["skipped"], out["reason"]), (True, "no self-update checkout"))
        import tartci_launchd_watchdog as wd
        with mock.patch.dict(os.environ, {"HOME": str(home),
                                          "TARTCI_PEER_STALL_DIR": str(self.tmp / "quiet")}):
            self.assertIsNone(wd.peer_stall_pass(now=RECEIPT_TS))
        # Control: with a checkout, the peer list is read.
        (home / ".local" / "share" / "tartci" / "update-checkout" / ".git").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch.object(psa, "published_peers", return_value=({}, None)) as peers:
            psa.alert_pass(now=RECEIPT_TS + 10 * psa.READ_SECS, directory=self.tmp / "quiet2")
        peers.assert_called_once()

    def test_the_watchdog_runs_the_pass_and_never_raises(self):
        import tartci_launchd_watchdog as wd
        with mock.patch.object(psa, "alert_pass", side_effect=OSError("ssh")):
            self.assertIn("WARN peer-stall check FAILED", wd.peer_stall_pass(now=RECEIPT_TS))
        with mock.patch.object(psa, "alert_pass", return_value={
                "skipped": False, "peers": {"studio": {"active": True, "hours": 97.3,
                                                       "since": "2026-10-05T05:41:27Z"}}}):
            self.assertIn("WARN peer-stall: launchd stalled on studio since "
                          "2026-10-05T05:41:27Z (97.3 h)", wd.peer_stall_pass(now=RECEIPT_TS))
        with mock.patch.object(psa, "alert_pass", return_value={"skipped": True, "peers": {}}):
            self.assertIsNone(wd.peer_stall_pass(now=RECEIPT_TS))
        main = (Path(__file__).resolve().parent / "tartci_launchd_watchdog.py").read_text()
        main = main[main.index("def main("):]
        self.assertLess(main.index("for h in health:"), main.index("peer_stall_pass()"),
                        "the peer read runs after the heal work")


if __name__ == "__main__":
    unittest.main()
