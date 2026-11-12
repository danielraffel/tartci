#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
import socket
import stat
import sys
import tempfile
import threading
import time
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

    def test_half_close_drains_peer_before_shutdown(self) -> None:
        source = PATH.read_text()
        remote = source[source.index('REMOTE_BRIDGE = """') : source.index('def remote_bridge_command')]
        self.assertLess(remote.index("if peer in readable:"), remote.index("if 0 in readable:"))
        self.assertIn("peer.shutdown(socket.SHUT_WR)", remote)
        local = source[source.index("sockets = [self.request, local_stream]") :]
        self.assertLess(
            local.index("if local_stream in readable:"),
            local.index("if self.request in readable:"),
        )
        self.assertIn("local_stream.shutdown(socket.SHUT_WR)", local)

    def test_remote_write_all_survives_interrupts_and_partial_writes(self) -> None:
        class PartialOS:
            def __init__(self) -> None:
                self.calls = 0
                self.output = bytearray()

            def write(self, _fd: int, data: memoryview) -> int:
                self.calls += 1
                if self.calls == 1:
                    raise InterruptedError
                written = min(2, len(data))
                self.output.extend(data[:written])
                return written

        partial_os = PartialOS()
        namespace = {"os": partial_os}
        exec(relay.REMOTE_BRIDGE_WRITE_ALL, namespace)
        namespace["write_all"](1, b"READY\nTLS-payload")
        self.assertEqual(bytes(partial_os.output), b"READY\nTLS-payload")
        self.assertGreater(partial_os.calls, 2)

    def test_remote_write_all_rejects_zero_byte_progress(self) -> None:
        class ZeroOS:
            @staticmethod
            def write(_fd: int, _data: memoryview) -> int:
                return 0

        namespace = {"os": ZeroOS()}
        exec(relay.REMOTE_BRIDGE_WRITE_ALL, namespace)
        with self.assertRaises(BrokenPipeError):
            namespace["write_all"](1, b"payload")

    def test_remote_bridge_uses_write_all_for_ready_and_payload(self) -> None:
        self.assertIn('write_all(1, b"READY\\n")', relay.REMOTE_BRIDGE)
        self.assertIn("write_all(1, data)", relay.REMOTE_BRIDGE)
        self.assertNotIn("os.write(1,", relay.REMOTE_BRIDGE)

    def test_guest_proxy_is_bridge_only_and_replaces_stale_values(self) -> None:
        runner = (ROOT / "providers/tart-macos/runner.sh").read_text()
        self.assertIn("^http://192\\.168\\.64\\.1:", runner)
        self.assertIn("HTTP_PROXY|HTTPS_PROXY|NO_PROXY|http_proxy|https_proxy|no_proxy", runner)
        self.assertIn("HTTP_PROXY=$GUEST_HTTP_PROXY", runner)
        self.assertIn("NO_PROXY=127.0.0.1,localhost,::1", runner)
        self.assertIn(
            "\"bash -s -- '$RUNNER_VERSION' '$RUNNER_SHA256' '$GUEST_HTTP_PROXY' "
            "'$RUNNER_ARCHIVE_URL' '$runner_cache_dir'\"",
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


def make_config(ssh: str = "/usr/bin/false", **overrides: object) -> "relay.RelayConfig":
    values = dict(
        ssh=ssh,
        relay_hosts=("relay-a", "relay-b"),
        allowed_routes=(),
        allowed_host_suffixes=("github.com",),
        connect_timeout=1,
        header_timeout=1,
        tunnel_idle_timeout=5,
        write_timeout=5,
    )
    values.update(overrides)
    return relay.RelayConfig(**values)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class BridgeRetryTests(unittest.TestCase):
    def run_open(self, outcomes: list, **config_overrides: object):
        config = make_config(**config_overrides)
        clock = FakeClock()
        calls: list = []
        events: list = []
        sleeps: list = []

        def attempt(_config, relay_host, host, port, ready_timeout):
            calls.append((relay_host, ready_timeout))
            cost, result = outcomes.pop(0)
            # A hung attempt can never outlive the budget it was handed.
            clock.now += min(cost, ready_timeout)
            if result == "ok":
                return ("bridge", "stream"), {}
            return None, {"reason": result, "ssh_returncode": 255, "detail": "upstream"}

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.now += seconds

        opened = relay.open_bridge(
            config,
            "storage.googleapis.com",
            443,
            attempt=attempt,
            sleep=sleep,
            clock=clock,
            emit=lambda event, **fields: events.append((event, fields)),
        )
        return opened, calls, events, sleeps

    def test_first_success_needs_no_retry_and_emits_nothing(self) -> None:
        opened, calls, events, sleeps = self.run_open([(0.1, "ok")])
        self.assertEqual(opened, ("bridge", "stream"))
        self.assertEqual([c[0] for c in calls], ["relay-a"])
        self.assertEqual(events, [])
        self.assertEqual(sleeps, [])

    def test_transient_failure_on_every_host_is_retried_in_a_second_pass(self) -> None:
        opened, calls, events, sleeps = self.run_open(
            [(0.2, "eof"), (0.2, "timeout"), (0.2, "ok")]
        )
        self.assertEqual(opened, ("bridge", "stream"))
        self.assertEqual([c[0] for c in calls], ["relay-a", "relay-b", "relay-a"])
        self.assertEqual(sleeps, [0.25])
        names = [event for event, _ in events]
        self.assertEqual(
            names,
            [
                "bridge_attempt_failed",
                "bridge_attempt_failed",
                "bridge_retry_pass",
                "bridge_opened_after_retry",
            ],
        )
        self.assertEqual(events[0][1]["relay_host"], "relay-a")
        self.assertEqual(events[0][1]["reason"], "eof")
        self.assertEqual(events[0][1]["target"], "storage.googleapis.com:443")
        self.assertEqual(events[3][1]["attempts"], 3)

    def test_attempts_are_bounded_by_passes(self) -> None:
        opened, calls, events, _ = self.run_open([(0.1, "eof")] * 10)
        self.assertIsNone(opened)
        self.assertEqual(len(calls), 4)
        self.assertEqual(events[-1][0], "bridge_exhausted")
        self.assertEqual(events[-1][1]["attempts"], 4)

    def test_total_deadline_clips_and_stops_attempts(self) -> None:
        # Each hung attempt burns its whole 4 s ready budget (1*2+2).
        opened, calls, events, _ = self.run_open(
            [(4.0, "timeout")] * 10, connect_deadline=10.0
        )
        self.assertIsNone(opened)
        self.assertEqual([round(c[1], 3) for c in calls], [4, 4, 1.75])
        self.assertEqual(events[-1][0], "bridge_exhausted")
        self.assertLessEqual(events[-1][1]["elapsed_seconds"], 10.0)

    def test_last_attempt_is_clipped_to_remaining_deadline(self) -> None:
        opened, calls, _, _ = self.run_open(
            [(4.0, "timeout"), (4.0, "timeout"), (1.0, "eof"), (4.0, "timeout")],
            connect_deadline=11.0,
        )
        self.assertIsNone(opened)
        self.assertEqual(len(calls), 4)
        self.assertAlmostEqual(calls[2][1], 11.0 - 8.0 - 0.25)
        self.assertAlmostEqual(calls[3][1], 11.0 - 9.25 - 0.0)

    def test_single_pass_configuration_never_retries(self) -> None:
        opened, calls, events, sleeps = self.run_open(
            [(0.1, "eof"), (0.1, "eof")], connect_passes=1
        )
        self.assertIsNone(opened)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [])
        self.assertNotIn("bridge_retry_pass", [event for event, _ in events])

    def test_retry_lives_only_before_connection_established(self) -> None:
        source = PATH.read_text()
        relay_body = source[source.index("    def relay(self, request: bytes)") :]
        self.assertEqual(relay_body.count("open_bridge("), 1)
        self.assertLess(
            relay_body.index("open_bridge("),
            relay_body.index("200 Connection Established"),
        )
        tunnel = relay_body[relay_body.index("200 Connection Established") :]
        self.assertNotIn("open_bridge", tunnel)

    def test_events_are_single_timestamped_json_lines(self) -> None:
        captured = []

        class Sink:
            def write(self, text: str) -> None:
                captured.append(text)

            def flush(self) -> None:
                pass

        original = relay.sys.stderr
        relay.sys.stderr = Sink()
        try:
            relay.emit_event("bridge_attempt_failed", relay_host="m1", detail="x\ny")
        finally:
            relay.sys.stderr = original
        line = "".join(captured)
        self.assertTrue(line.startswith("tartci-relay-event {"))
        self.assertEqual(line.count("\n"), 1)
        record = relay.json.loads(line.split(" ", 1)[1])
        self.assertEqual(record["event"], "bridge_attempt_failed")
        self.assertRegex(record["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class ReadReadyTimeoutTests(unittest.TestCase):
    def test_socket_timeout_is_a_failed_ready_not_an_exception(self) -> None:
        # /usr/bin/python3 (3.9) raises socket.timeout, which is not a
        # TimeoutError there; an uncaught one killed the handler silently.
        class Timing:
            def recv(self, _size: int) -> bytes:
                raise socket.timeout("timed out")

        self.assertEqual(relay.read_ready_status(Timing()), "timeout")
        self.assertFalse(relay.read_ready(Timing()))

    def test_reset_is_a_failed_ready_not_an_exception(self) -> None:
        class Resetting:
            def recv(self, _size: int) -> bytes:
                raise ConnectionResetError(54, "reset")

        self.assertEqual(relay.read_ready_status(Resetting()), "error")


class FakeSshEndToEndTests(unittest.TestCase):
    """Drive the real attempt path with a fake ssh that fails, then bridges."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.counter = root / "count"
        self.counter.write_text("0")
        self.fake_ssh = root / "ssh"
        self.fake_ssh.write_text(
            "#!/bin/sh\n"
            f"n=$(cat '{self.counter}'); n=$((n+1)); echo $n > '{self.counter}'\n"
            f"if [ $n -le ${{FAIL_FIRST:-0}} ]; then\n"
            "  echo 'kex_exchange_identification: Connection closed by remote host' >&2\n"
            "  exit 255\n"
            "fi\n"
            "for last; do :; done\n"
            f"exec /bin/sh -c \"$(echo \"$last\" | sed 's#^/usr/bin/python3#{sys.executable}#')\"\n"
        )
        self.fake_ssh.chmod(self.fake_ssh.stat().st_mode | stat.S_IEXEC)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.addCleanup(self.server.close)
        self.port = self.server.getsockname()[1]

        def echo() -> None:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(1024)
                conn.sendall(b"echo:" + data)

        threading.Thread(target=echo, daemon=True).start()

    def open_with(self, fail_first: int, **overrides: object):
        os.environ["FAIL_FIRST"] = str(fail_first)
        self.addCleanup(os.environ.pop, "FAIL_FIRST", None)
        events: list = []
        config = make_config(ssh=str(self.fake_ssh), connect_timeout=2, **overrides)
        opened = relay.open_bridge(
            config,
            "127.0.0.1",
            self.port,
            emit=lambda event, **fields: events.append((event, fields)),
        )
        return opened, events

    def test_upstream_failures_are_retried_and_bridge_carries_data(self) -> None:
        opened, events = self.open_with(fail_first=2)
        self.assertIsNotNone(opened)
        bridge, stream = opened
        try:
            stream.settimeout(5)
            stream.sendall(b"hello")
            self.assertEqual(stream.recv(1024), b"echo:hello")
        finally:
            stream.close()
            relay.stop_bridge(bridge)
        names = [event for event, _ in events]
        self.assertEqual(
            names,
            [
                "bridge_attempt_failed",
                "bridge_attempt_failed",
                "bridge_retry_pass",
                "bridge_opened_after_retry",
            ],
        )
        failure = events[0][1]
        self.assertEqual(failure["reason"], "eof")
        self.assertEqual(failure["ssh_returncode"], 255)
        self.assertIn("Connection closed by remote host", failure["detail"])
        self.assertEqual(self.counter.read_text().strip(), "3")

    def test_persistent_failure_is_bounded_and_reported(self) -> None:
        started = time.monotonic()
        opened, events = self.open_with(fail_first=99)
        self.assertIsNone(opened)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.counter.read_text().strip(), "4")
        self.assertEqual(events[-1][0], "bridge_exhausted")



class PortInUseTests(unittest.TestCase):
    """A port held by another process refuses loudly instead of crash-looping."""

    def held_port(self) -> int:
        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        return holder.getsockname()[1]

    def run_main(self, argv: list[str]) -> tuple[int, str]:
        import contextlib
        import io
        err = io.StringIO()
        original = relay.port_holder
        relay.port_holder = lambda port, run=None: "pid 2518 (Python)"
        try:
            with contextlib.redirect_stderr(err):
                rc = relay.main(argv)
        finally:
            relay.port_holder = original
        return rc, err.getvalue()

    def test_a_held_port_exits_75_and_names_the_holder(self) -> None:
        # m3 on 2026-10-02: a legacy bridge held 49125 and the relay crash-looped.
        port = self.held_port()
        rc, err = self.run_main(["--listen-host", "127.0.0.1", "--listen-port", str(port),
                                 "--relay-host", "a", "--relay-host", "b", "--allow-route", "127.0.0.0/8=127.0.0.1", "--allow-host-suffix", "github.com"])
        self.assertEqual(rc, relay.EXIT_PORT_IN_USE)
        self.assertIn("REFUSING TO START", err)
        self.assertIn("pid 2518 (Python)", err)
        self.assertIn('"event": "listen_port_in_use"', err)

    def test_another_bind_error_still_raises(self) -> None:
        # Only a held port is a known, retryable condition.
        def fake_server(*args, **kwargs):
            raise OSError(13, "Permission denied")
        original = relay.ThreadingServer
        relay.ThreadingServer = fake_server
        try:
            with self.assertRaises(OSError):
                relay.serve(relay.parse_args(["--relay-host", "a", "--relay-host", "b", "--allow-route", "127.0.0.0/8=127.0.0.1", "--allow-host-suffix", "github.com"]),
                            holder=lambda port: "nobody")
        finally:
            relay.ThreadingServer = original

    def test_holder_is_parsed_from_lsof_field_output(self) -> None:
        import subprocess
        fake = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "p2518\ncPython\n", "")
        self.assertEqual(relay.port_holder(49125, run=fake), "pid 2518 (Python)")
        empty = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "")
        self.assertIn("no listener", relay.port_holder(49125, run=empty))

    def test_an_oversized_log_is_rotated_at_startup(self) -> None:
        port = self.held_port()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "relay.log"
            log.write_bytes(b"x" * 2048)
            rc, _ = self.run_main(["--listen-host", "127.0.0.1", "--listen-port", str(port),
                                   "--relay-host", "a", "--relay-host", "b", "--allow-route", "127.0.0.0/8=127.0.0.1", "--allow-host-suffix", "github.com",
                                   "--log-path", str(log), "--log-max-bytes", "1024"])
            self.assertEqual(rc, relay.EXIT_PORT_IN_USE)
            self.assertEqual((Path(f"{log}.1")).stat().st_size, 2048)
            self.assertEqual(log.stat().st_size, 0)

    def test_the_launchd_plist_throttles_respawns(self) -> None:
        import plistlib
        import re
        template = (ROOT / "launchd/com.danielraffel.tartci.http-connect-ssh-relay.plist.template")
        value = plistlib.loads(re.sub(rb"<!--.*?-->", b"", template.read_bytes(), flags=re.DOTALL))
        self.assertGreaterEqual(value.get("ThrottleInterval", 0), 30)

    def test_the_plist_runs_the_installed_relay_with_arguments_it_accepts(self) -> None:
        # The relay used to run a copy self-update never refreshed; a template
        # flag that copy rejected rolled back m5's update on 2026-10-02. It now
        # runs this generation's script, so this parser is the one that reads
        # the arguments.
        import plistlib
        import re
        template = (ROOT / "launchd/com.danielraffel.tartci.http-connect-ssh-relay.plist.template")
        value = plistlib.loads(re.sub(rb"<!--.*?-->", b"", template.read_bytes(), flags=re.DOTALL))
        args = [a.replace("$HOME", "/Users/x").replace("$TARTCI_HTTP_RELAY_PRIMARY", "a")
                .replace("$TARTCI_HTTP_RELAY_SECONDARY", "b") for a in value["ProgramArguments"]]
        self.assertEqual(args[:3], ["/bin/bash", "/Users/x/.local/bin/tartci", "network-relay"])
        parsed = relay.parse_args(args[3:])
        self.assertEqual(parsed.log_path, value["StandardErrorPath"].replace("$HOME", "/Users/x"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
