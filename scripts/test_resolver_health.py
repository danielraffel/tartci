#!/usr/bin/env python3
"""Resolver/TCP classification and three-tick hysteresis fixtures."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolver_health as rh  # noqa: E402


class ResolverHealth(unittest.TestCase):
    def run_pair(self, resolver_rc: int, resolver_output: str, tcp_ok: bool):
        def run(argv):
            if argv[0] == "dscacheutil":
                return resolver_rc, resolver_output, "no answer" if not resolver_output else ""
            return (0, "", "") if tcp_ok else (1, "", "refused")
        return rh.probe(run=run)

    def test_the_four_probe_rows_are_classified(self):
        expected = {
            (1, "", True): "resolver_dead", (1, "", False): "network_down",
            (0, "ip_address: 140.82.112.3\n", False): "upstream_unreachable",
            (0, "ip_address: 140.82.112.3\n", True): "healthy",
            # dns-sd prints an address and then is killed by the hard timeout.
            (124, "2026-10-10  A  140.82.112.3\n", True): "healthy",
        }
        for pair, condition in expected.items():
            with self.subTest(pair=pair):
                result = self.run_pair(*pair)
                self.assertEqual(result["condition"], condition)

        self.assertEqual(self.run_pair(124, "", True)["condition"], "resolver_dead")
        self.assertEqual(self.run_pair(1, "", True)["condition"], "resolver_dead")
        self.assertEqual(self.run_pair(0, "name: github.com\n", True)["condition"], "resolver_dead")
        self.assertEqual(self.run_pair(0, "", True)["condition"], "resolver_dead")

    def test_resolver_dead_requires_three_consecutive_ticks(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        def run(argv):
            if argv[0] == "dscacheutil":
                return (1, "", "no answer")
            return (0, "", "")
        for tick in range(1, 4):
            state = rh.tick(now=1_800_000_000 + tick, directory=root, run=run)
            if tick < 3:
                self.assertNotEqual(state["condition"], "resolver_dead")
            else:
                self.assertEqual(state["condition"], "resolver_dead")
                self.assertEqual(state["candidate_streak"], 3)
        self.assertEqual(json.loads((root / "state.json").read_text())["condition"],
                         "resolver_dead")

    def test_resolver_ok_tcp_failure_never_emits_resolver_dead(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        def run(argv):
            if argv[0] == "dscacheutil":
                return (0, "ip_address: 140.82.112.3\n", "")
            return (1, "", "refused")
        for tick in range(1, 5):
            state = rh.tick(now=1_800_000_000 + tick, directory=root, run=run)
            self.assertNotEqual(state["condition"], "resolver_dead")
        self.assertEqual(state["condition"], "upstream_unreachable")

    def test_a_healthy_tick_resets_the_bad_streak_before_a_new_outage(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        outcomes = iter([
            (1, "", "no answer"), (1, "", "no answer"),
            (0, "ip_address: 140.82.112.3\n", ""),
            (1, "", "no answer"), (1, "", "no answer"),
        ])
        def run(argv):
            return next(outcomes) if argv[0] == "dscacheutil" else (0, "", "")
        states = [rh.tick(now=1_800_000_000 + i, directory=root, run=run)
                  for i in range(5)]
        self.assertEqual([s["condition"] for s in states],
                         [None, None, "healthy", "healthy", "healthy"])
        self.assertEqual(states[-1]["candidate_streak"], 2)

    def test_watchdog_publishes_the_probe_warning_only_after_state_is_active(self):
        import tartci_launchd_watchdog as watchdog
        from unittest import mock
        with mock.patch.object(rh, "tick", return_value={"condition": "healthy"}):
            self.assertIsNone(watchdog.resolver_health_pass(now=1_800_000_000))
        with mock.patch.object(rh, "tick", return_value={"condition": "resolver_dead"}):
            self.assertIn("resolver dead while TCP alive",
                          watchdog.resolver_health_pass(now=1_800_000_000))


if __name__ == "__main__":
    unittest.main()
