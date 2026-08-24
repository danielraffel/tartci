#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
RELAY_PATH = Path(__file__).with_name("http_connect_ssh_relay.py")
SPEC = importlib.util.spec_from_file_location("http_connect_ssh_relay_config", RELAY_PATH)
assert SPEC and SPEC.loader
relay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = relay
SPEC.loader.exec_module(relay)


class HttpConnectSshRelayConfigTests(unittest.TestCase):
    def test_launchd_allows_bounded_cargo_registry_endpoints(self) -> None:
        template = (
            ROOT / "launchd/com.danielraffel.tartci.http-connect-ssh-relay.plist.template"
        ).read_text()
        self.assertIn("<string>crates.io</string>", template)
        suffixes = ("crates.io",)
        self.assertTrue(relay.host_is_allowed("index.crates.io", suffixes))
        self.assertTrue(relay.host_is_allowed("static.crates.io", suffixes))
        self.assertFalse(relay.host_is_allowed("evilcrates.io", suffixes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
