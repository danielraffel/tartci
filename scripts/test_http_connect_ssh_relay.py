#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


PATH = Path(__file__).with_name("http_connect_ssh_relay.py")
ROOT = PATH.parents[1]
SPEC = importlib.util.spec_from_file_location("http_connect_ssh_relay", PATH)
assert SPEC and SPEC.loader
relay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = relay
SPEC.loader.exec_module(relay)


class HttpConnectSshRelayTests(unittest.TestCase):
    def test_connect_parser_accepts_hostname_and_port(self) -> None:
        self.assertEqual(
            relay.parse_connect_target(b"CONNECT api.github.com:443 HTTP/1.1\r\n\r\n"),
            ("api.github.com", 443),
        )

    def test_connect_parser_rejects_methods_options_and_bad_ports(self) -> None:
        for request in (
            b"GET api.github.com:443 HTTP/1.1\r\n\r\n",
            b"CONNECT -oProxyCommand=x:443 HTTP/1.1\r\n\r\n",
            b"CONNECT api.github.com:0 HTTP/1.1\r\n\r\n",
            b"CONNECT api.github.com:nope HTTP/1.1\r\n\r\n",
        ):
            self.assertIsNone(relay.parse_connect_target(request))

    def test_remote_bridge_has_positive_destination_ready_marker(self) -> None:
        command = relay.remote_bridge_command("api.github.com", 443, 5)
        self.assertIn("/usr/bin/python3", command)
        self.assertIn("api.github.com 443 5", command)
        self.assertIn('marker == b"READY\\n"', PATH.read_text())
        self.assertIn("config.connect_timeout * 2 + 2", PATH.read_text())

    def test_ready_marker_accepts_fragmented_reads(self) -> None:
        class Fragmented:
            chunks = [b"R", b"EA", b"DY\n"]

            def recv(self, _size: int) -> bytes:
                return self.chunks.pop(0)

        self.assertTrue(relay.read_ready(Fragmented()))

    def test_tunnel_drains_socket_after_ssh_process_exit(self) -> None:
        source = PATH.read_text()
        tunnel = source[source.index("sockets = [self.request, local_stream]") :]
        self.assertIn("while True:", tunnel)
        self.assertNotIn("while bridge.poll() is None:", tunnel)

    def test_guest_proxy_is_bridge_only_and_replaces_stale_values(self) -> None:
        runner = (ROOT / "providers/tart-macos/runner.sh").read_text()
        self.assertIn("^http://192\\.168\\.64\\.1:", runner)
        self.assertIn("HTTP_PROXY|HTTPS_PROXY|NO_PROXY|http_proxy|https_proxy|no_proxy", runner)
        self.assertIn("HTTP_PROXY=$GUEST_HTTP_PROXY", runner)
        self.assertIn("NO_PROXY=127.0.0.1,localhost,::1", runner)
        self.assertIn(
            '"bash -s -- \'$RUNNER_VERSION\' \'$RUNNER_SHA256\' \'$GUEST_HTTP_PROXY\'"',
            runner,
        )
        self.assertLess(
            runner.index('export HTTP_PROXY="$guest_http_proxy"'),
            runner.index("curl -fsSL --retry 3"),
        )

    def test_destination_policy_is_suffix_bounded(self) -> None:
        suffixes = ("github.com", "githubusercontent.com")
        self.assertTrue(relay.host_is_allowed("api.github.com", suffixes))
        self.assertTrue(relay.host_is_allowed("github.com", suffixes))
        self.assertFalse(relay.host_is_allowed("evilgithub.com", suffixes))
        self.assertFalse(relay.host_is_allowed("127.0.0.1", suffixes))
        self.assertFalse(relay.host_is_allowed("localhost", suffixes))

    def test_route_parser_binds_client_network_to_local_address(self) -> None:
        network, destination = relay.parse_route("192.168.64.0/24=192.168.64.1")
        self.assertIn(relay.ipaddress.ip_address("192.168.64.3"), network)
        self.assertEqual(str(destination), "192.168.64.1")
        with self.assertRaises(relay.argparse.ArgumentTypeError):
            relay.parse_route("192.168.64.0/24")

    def test_relay_label_is_outside_runner_watchdog_namespace(self) -> None:
        template = (ROOT / "launchd/com.danielraffel.tartci.http-connect-ssh-relay.plist.template").read_text()
        self.assertIn("com.danielraffel.network.http-connect-ssh-relay", template)
        self.assertNotIn("<string>com.danielraffel.tartci.", template)

    def test_launchd_routes_bind_source_network_to_local_interface(self) -> None:
        template = (ROOT / "launchd/com.danielraffel.tartci.http-connect-ssh-relay.plist.template").read_text()
        self.assertIn("127.0.0.0/8=127.0.0.1", template)
        self.assertIn("192.168.64.0/24=192.168.64.1", template)


if __name__ == "__main__":
    unittest.main(verbosity=2)
