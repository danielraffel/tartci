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
    def run_pair(self, resolver_ok: bool, tcp_ok: bool):
        def run(argv):
            if argv[0] == "dns-sd":
                return (0, "github.com 140.82.112.3\n", "") if resolver_ok else (1, "", "no answer")
            return (0, "", "") if tcp_ok else (1, "", "refused")
        return rh.probe(run=run)

    def test_the_four_probe_rows_are_classified(self):
        expected = {
            (False, True): "resolver_dead", (False, False): "network_down",
            (True, False): "upstream_unreachable", (True, True): "healthy",
        }
        for pair, condition in expected.items():
            with self.subTest(pair=pair):
                result = self.run_pair(*pair)
                self.assertEqual(result["condition"], condition)

    def test_resolver_dead_requires_three_consecutive_ticks(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        def run(argv):
            if argv[0] == "dns-sd":
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
            if argv[0] == "dns-sd":
                return (0, "github.com 140.82.112.3\n", "")
            return (1, "", "refused")
        for tick in range(1, 5):
            state = rh.tick(now=1_800_000_000 + tick, directory=root, run=run)
            self.assertNotEqual(state["condition"], "resolver_dead")
        self.assertEqual(state["condition"], "upstream_unreachable")

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
