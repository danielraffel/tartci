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
    def test_launchd_allows_proven_homebrew_portable_ruby_registry_only(self) -> None:
        template = (
            ROOT / "launchd/com.danielraffel.tartci.http-connect-ssh-relay.plist.template"
        ).read_text()
        self.assertIn("<string>ghcr.io</string>", template)
        suffixes = ("ghcr.io",)
        self.assertTrue(relay.host_is_allowed("ghcr.io", suffixes))
        self.assertFalse(relay.host_is_allowed("evilghcr.io", suffixes))
        self.assertNotIn("<string>brew.sh</string>", template)
        self.assertNotIn("<string>homebrew.org</string>", template)

    def test_launchd_allows_bounded_cargo_registry_endpoints(self) -> None:
        template = (
            ROOT / "launchd/com.danielraffel.tartci.http-connect-ssh-relay.plist.template"
        ).read_text()
        self.assertIn("<string>crates.io</string>", template)
        suffixes = ("crates.io",)
        self.assertTrue(relay.host_is_allowed("index.crates.io", suffixes))
        self.assertTrue(relay.host_is_allowed("static.crates.io", suffixes))
        self.assertFalse(relay.host_is_allowed("evilcrates.io", suffixes))

    def test_launchd_allows_bounded_sigstore_attestation_endpoints(self) -> None:
        template = (
            ROOT / "launchd/com.danielraffel.tartci.http-connect-ssh-relay.plist.template"
        ).read_text()
        self.assertIn("<string>sigstore.dev</string>", template)
        suffixes = ("sigstore.dev",)
        self.assertTrue(relay.host_is_allowed("fulcio.sigstore.dev", suffixes))
        self.assertTrue(relay.host_is_allowed("rekor.sigstore.dev", suffixes))
        self.assertTrue(relay.host_is_allowed("search.sigstore.dev", suffixes))
        self.assertFalse(relay.host_is_allowed("evilsigstore.dev", suffixes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
