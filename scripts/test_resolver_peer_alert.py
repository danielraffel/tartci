#!/usr/bin/env python3
"""Peer-path alert for a planted resolver_dead state."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import peer_stall_alert as psa  # noqa: E402


class ResolverPeer(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.opened = []
        self.closed = []
        self.since = "2026-10-10T00:00:00Z"
        self.state = {"condition": "resolver_dead", "since": self.since,
                      "updated_at": "2026-10-10T03:00:00Z", "resolver_ok": False,
                      "tcp_ok": True}

    def ssh_run(self, argv):
        body = (f"1800000000\n{psa.SEPARATOR}\n\n{psa.RESOLVER_SEPARATOR}\n"
                f"{json.dumps(self.state)}\n{psa.LAST_SEPARATOR}\n")
        return 0, body, ""

    def issue(self, title, body):
        self.opened.append((title, body))
        return 0, "701"

    def close(self, number):
        self.closed.append(number)
        return 0, "closed"

    def pass_(self, at):
        return psa.alert_pass(now=at, directory=self.tmp, peers={"m5": "m5"}, me="m1",
                              run=self.ssh_run, issue=self.issue, close=self.close)

    def test_peer_reads_planted_resolver_dead_and_opens_one_issue(self):
        first = self.pass_(1800000100)
        self.assertTrue(first["peers"]["m5"]["resolver"]["active"])
        self.assertEqual(len(self.opened), 1)
        title, body = self.opened[0]
        self.assertEqual(title, f"[tartci] m5 resolver dead while TCP alive since {self.since}")
        self.assertIn("restart Tailscale via the LAN fallback", body)
        self.assertIn("scutil --nc stop/start Tailscale", body)
        self.pass_(1800000200)
        self.assertEqual(len(self.opened), 1)

    def test_peer_closes_after_planted_recovery(self):
        self.pass_(1800000100)
        self.state = {**self.state, "condition": "healthy", "updated_at": "2026-10-10T04:00:00Z"}
        out = self.pass_(1800000100 + psa.READ_SECS)
        self.assertTrue(out["peers"]["m5"]["resolver"]["closed"])
        self.assertEqual(self.closed, ["701"])


if __name__ == "__main__":
    unittest.main()
