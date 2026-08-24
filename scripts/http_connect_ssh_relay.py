#!/usr/bin/env python3
"""Bounded HTTP CONNECT relay for Tart guests and host controllers.

The listener accepts CONNECT only from explicitly allowed local networks and
carries each stream through the first healthy SSH relay. Connections do not
reuse an SSH control master: a live-but-wedged multiplex socket previously left
controllers pointing at a listener that could not complete TLS.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import select
import socket
import socketserver
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class RelayConfig:
    ssh: str
    relay_hosts: tuple[str, ...]
    allowed_routes: tuple[
        tuple[
            ipaddress.IPv4Network | ipaddress.IPv6Network,
            ipaddress.IPv4Address | ipaddress.IPv6Address,
        ], ...
    ]
    allowed_host_suffixes: tuple[str, ...]
    connect_timeout: int
    header_timeout: int
    tunnel_idle_timeout: int
    write_timeout: int


def parse_connect_target(request: bytes) -> tuple[str, int] | None:
    first_line = request.split(b"\r\n", 1)[0].decode("ascii", "replace")
    parts = first_line.split()
    if len(parts) != 3 or parts[0].upper() != "CONNECT":
        return None
    host, separator, port_text = parts[1].rpartition(":")
    if (
        not separator
        or not host
        or host.startswith("-")
        or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_" for ch in host)
        or not port_text.isdigit()
    ):
        return None
    port = int(port_text)
    return (host, port) if 1 <= port <= 65535 else None


REMOTE_BRIDGE = """\
import os, select, socket, sys
peer = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=int(sys.argv[3]))
peer.settimeout(None)
os.write(1, b"READY\\n")
inputs = [0, peer]
while True:
    readable, _, _ = select.select(inputs, [], [])
    if peer in readable:
        data = peer.recv(65536)
        if not data:
            break
        os.write(1, data)
    if 0 in readable:
        data = os.read(0, 65536)
        if data:
            peer.sendall(data)
        else:
            peer.shutdown(socket.SHUT_WR)
            inputs.remove(0)
"""


def remote_bridge_command(host: str, port: int, timeout: int) -> str:
    encoded = base64.b64encode(REMOTE_BRIDGE.encode()).decode("ascii")
    return (
        "/usr/bin/python3 -c "
        f"'import base64;exec(base64.b64decode(\"{encoded}\"))' "
        f"{host} {port} {timeout}"
    )


def read_ready(stream: socket.socket) -> bool:
    marker = b""
    try:
        while len(marker) < 6:
            chunk = stream.recv(6 - len(marker))
            if not chunk:
                return False
            marker += chunk
    except TimeoutError:
        return False
    return marker == b"READY\n"


def open_bridge(
    config: RelayConfig, host: str, port: int
) -> tuple[subprocess.Popen[bytes], socket.socket] | None:
    for relay_host in config.relay_hosts:
        local_stream, ssh_stream = socket.socketpair()
        bridge = subprocess.Popen(
            [
                config.ssh,
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={config.connect_timeout}",
                "-o", "ConnectionAttempts=1",
                "-o", "ControlMaster=no",
                relay_host,
                remote_bridge_command(host, port, config.connect_timeout),
            ],
            stdin=ssh_stream.fileno(),
            stdout=ssh_stream.fileno(),
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        ssh_stream.close()
        # SSH establishment and the remote destination connect are sequential;
        # each owns the configured timeout budget.
        local_stream.settimeout(config.connect_timeout * 2 + 2)
        if read_ready(local_stream) and bridge.poll() is None:
            local_stream.settimeout(None)
            return bridge, local_stream
        local_stream.close()
        bridge.terminate()
        try:
            bridge.wait(timeout=2)
        except subprocess.TimeoutExpired:
            bridge.kill()
    return None


def host_is_allowed(host: str, suffixes: tuple[str, ...]) -> bool:
    normalized = host.lower().rstrip(".")
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in suffixes
    )


def parse_route(raw: str) -> tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network,
    ipaddress.IPv4Address | ipaddress.IPv6Address,
]:
    network_text, separator, destination_text = raw.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError("routes must use CLIENT_CIDR=LOCAL_ADDRESS")
    try:
        network = ipaddress.ip_network(network_text)
        destination = ipaddress.ip_address(destination_text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    if network.version != destination.version:
        raise argparse.ArgumentTypeError("route network and destination IP versions differ")
    return network, destination


class ConnectHandler(socketserver.BaseRequestHandler):
    config: RelayConfig

    def handle(self) -> None:
        client_ip = ipaddress.ip_address(self.client_address[0])
        local_ip = ipaddress.ip_address(self.request.getsockname()[0])
        if not any(
            client_ip in network and local_ip == destination
            for network, destination in self.config.allowed_routes
        ):
            return

        request = b""
        self.request.settimeout(self.config.header_timeout)
        try:
            while b"\r\n\r\n" not in request and len(request) < 16384:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                request += chunk
        except TimeoutError:
            return
        finally:
            self.request.settimeout(None)

        target = parse_connect_target(request)
        if target is None:
            self.request.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        if target[1] != 443 or not host_is_allowed(
            target[0], self.config.allowed_host_suffixes
        ):
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        host, port = target
        opened = open_bridge(self.config, host, port)
        if opened is None:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        bridge, local_stream = opened
        self.request.settimeout(self.config.write_timeout)
        local_stream.settimeout(self.config.write_timeout)
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        sockets = [self.request, local_stream]
        last_activity = time.monotonic()
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [], 1.0)
                if (
                    not readable
                    and time.monotonic() - last_activity
                    >= self.config.tunnel_idle_timeout
                ):
                    break
                if local_stream in readable:
                    data = local_stream.recv(65536)
                    if not data:
                        break
                    self.request.sendall(data)
                    last_activity = time.monotonic()
                if self.request in readable:
                    data = self.request.recv(65536)
                    if data:
                        local_stream.sendall(data)
                        last_activity = time.monotonic()
                    else:
                        local_stream.shutdown(socket.SHUT_WR)
                        sockets.remove(self.request)
        except TimeoutError:
            pass
        finally:
            local_stream.close()
            bridge.terminate()
            try:
                bridge.wait(timeout=2)
            except subprocess.TimeoutExpired:
                bridge.kill()


class ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args: object, max_handlers: int, **kwargs: object) -> None:
        self._handler_slots = threading.BoundedSemaphore(max_handlers)
        super().__init__(*args, **kwargs)

    def process_request(self, request: socket.socket, client_address: object) -> None:
        if not self._handler_slots.acquire(blocking=False):
            request.close()
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: socket.socket, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_slots.release()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=49125)
    parser.add_argument(
        "--allow-route",
        action="append",
        required=True,
        type=parse_route,
        metavar="CLIENT_CIDR=LOCAL_ADDRESS",
    )
    parser.add_argument("--relay-host", action="append", required=True)
    parser.add_argument("--allow-host-suffix", action="append", required=True)
    parser.add_argument("--ssh", default="/usr/bin/ssh")
    parser.add_argument("--connect-timeout", type=int, default=5)
    parser.add_argument("--header-timeout", type=int, default=5)
    parser.add_argument("--tunnel-idle-timeout", type=int, default=300)
    parser.add_argument("--write-timeout", type=int, default=30)
    parser.add_argument("--max-handlers", type=int, default=64)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if (
        not 1 <= args.listen_port <= 65535
        or args.connect_timeout < 1
        or args.header_timeout < 1
        or args.tunnel_idle_timeout < 1
        or args.write_timeout < 1
        or args.max_handlers < 1
    ):
        raise SystemExit("ports and timeouts must be positive and bounded")
    ConnectHandler.config = RelayConfig(
        ssh=args.ssh,
        relay_hosts=tuple(args.relay_host),
        allowed_routes=tuple(args.allow_route),
        allowed_host_suffixes=tuple(
            suffix.lower().strip(".") for suffix in args.allow_host_suffix
        ),
        connect_timeout=args.connect_timeout,
        header_timeout=args.header_timeout,
        tunnel_idle_timeout=args.tunnel_idle_timeout,
        write_timeout=args.write_timeout,
    )
    with ThreadingServer(
        (args.listen_host, args.listen_port),
        ConnectHandler,
        max_handlers=args.max_handlers,
    ) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
