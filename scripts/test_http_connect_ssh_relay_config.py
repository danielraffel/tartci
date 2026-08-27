#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
import tomllib
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
    def test_launchd_covers_protected_macos_bootstrap_host_contract(self) -> None:
        template = (
            ROOT / "launchd/com.danielraffel.tartci.http-connect-ssh-relay.plist.template"
        ).read_text()
        contract = tomllib.loads(
            (
                ROOT / "profiles/pulp-protected-macos-bootstrap-hosts.toml"
            ).read_text()
        )
        self.assertEqual(contract["schema"], 1)
        self.assertEqual(contract["repo"], "Generous-Corp/pulp")
        self.assertEqual(contract["workflow"], ".github/workflows/build.yml")
        suffixes = tuple(contract["literal_hosts"] + contract["transitive_hosts"])
        self.assertEqual(
            set(suffixes),
            {
                "api.vcvrack.com",
                "formulae.brew.sh",
                "ghcr.io",
                "registry.npmjs.org",
                "storage.googleapis.com",
            },
        )
        for suffix in suffixes:
            self.assertIn(f"<string>{suffix}</string>", template)
            self.assertTrue(relay.host_is_allowed(suffix, suffixes))
            self.assertFalse(relay.host_is_allowed(f"evil{suffix}", suffixes))
            self.assertFalse(
                relay.host_is_allowed(f"{suffix}.evil.example", suffixes)
            )
        self.assertNotIn("<string>brew.sh</string>", template)
        self.assertNotIn("<string>homebrew.org</string>", template)

    def test_launchd_covers_release_node_bootstrap_host_contract(self) -> None:
        template = (
            ROOT / "launchd/com.danielraffel.tartci.http-connect-ssh-relay.plist.template"
        ).read_text()
        contract = tomllib.loads(
            (ROOT / "profiles/pulp-release-macos-bootstrap-hosts.toml").read_text()
        )
        self.assertEqual(contract["schema"], 1)
        self.assertEqual(contract["repo"], "Generous-Corp/pulp")
        self.assertEqual(contract["workflow"], ".github/workflows/release-cli.yml")
        self.assertEqual(
            contract["bootstrap_entrypoint"],
            "tools/scripts/prepare_node_runtime.py",
        )
        suffixes = tuple(contract["literal_hosts"] + contract["transitive_hosts"])
        self.assertEqual(suffixes, ("nodejs.org",))
        self.assertIn("<string>nodejs.org</string>", template)
        self.assertTrue(relay.host_is_allowed("nodejs.org", suffixes))
        self.assertTrue(relay.host_is_allowed("www.nodejs.org", suffixes))
        self.assertFalse(relay.host_is_allowed("evilnodejs.org", suffixes))
        self.assertFalse(relay.host_is_allowed("nodejs.org.evil.example", suffixes))

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
