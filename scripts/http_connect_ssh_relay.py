#!/usr/bin/env python3
"""Bounded HTTP CONNECT relay for Tart guests and host controllers.

The listener accepts CONNECT only from explicitly allowed local networks and
carries each stream through the first healthy SSH relay. Connections do not
reuse an SSH control master: a live-but-wedged multiplex socket previously left
controllers pointing at a listener that could not complete TLS.

Opening a bridge is retried within a bounded budget: a small number of passes
over the relay hosts, a short backoff between passes, and a total deadline far
below any client's CONNECT timeout. Retries happen only before the client is
told "200 Connection Established", so no client byte is ever relayed twice.
Every failed attempt, retry, and exhaustion is written to stderr as one
timestamped tartci-relay-event JSON line.

The deployed interpreter is macOS's /usr/bin/python3 (3.9), where
socket.timeout is NOT a subclass of TimeoutError; socket errors are
therefore caught as OSError.
"""

from __future__ import annotations

import argparse
import base64
import errno
import ipaddress
import json
import select
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence


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
    connect_passes: int = 2
    connect_deadline: float = 30.0
    retry_backoff: float = 0.25


EVENT_PREFIX = "tartci-relay-event"
_event_lock = threading.Lock()


def emit_event(event: str, **fields: object) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        **fields,
    }
    line = f"{EVENT_PREFIX} {json.dumps(record, sort_keys=True)}\n"
    with _event_lock:
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except (OSError, ValueError):
            pass


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


REMOTE_BRIDGE_WRITE_ALL = """\
def write_all(fd, data):
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise BrokenPipeError("zero-byte write to relay stream")
        view = view[written:]
"""


REMOTE_BRIDGE = """\
import os, select, socket, sys
""" + REMOTE_BRIDGE_WRITE_ALL + """\
peer = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=int(sys.argv[3]))
peer.settimeout(None)
write_all(1, b"READY\\n")
inputs = [0, peer]
while True:
    readable, _, _ = select.select(inputs, [], [])
    if peer in readable:
        data = peer.recv(65536)
        if not data:
            break
        write_all(1, data)
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


def read_ready_status(stream: socket.socket) -> str:
    """Return "ready", "eof", "timeout", "error", or "bad-marker"."""
    marker = b""
    try:
        while len(marker) < 6:
            chunk = stream.recv(6 - len(marker))
            if not chunk:
                return "eof"
            marker += chunk
    except (TimeoutError, socket.timeout):
        return "timeout"
    except OSError:
        return "error"
    return "ready" if marker == b"READY\n" else "bad-marker"


def read_ready(stream: socket.socket) -> bool:
    return read_ready_status(stream) == "ready"


def stop_bridge(bridge: subprocess.Popen[bytes]) -> int | None:
    bridge.terminate()
    try:
        return bridge.wait(timeout=2)
    except subprocess.TimeoutExpired:
        bridge.kill()
        try:
            return bridge.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return None


def stderr_tail(sink: object, limit: int = 300) -> str:
    try:
        sink.seek(0)  # type: ignore[attr-defined]
        text = sink.read().decode("utf-8", "replace")  # type: ignore[attr-defined]
    except (OSError, ValueError):
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[-1] if lines else "")[:limit]


def attempt_bridge(
    config: RelayConfig, relay_host: str, host: str, port: int, ready_timeout: float
) -> tuple[tuple[subprocess.Popen[bytes], socket.socket] | None, dict[str, object]]:
    """Try one relay host once; return the bridge or a failure description."""
    local_stream, ssh_stream = socket.socketpair()
    stderr_sink = tempfile.TemporaryFile()
    try:
        bridge = subprocess.Popen(
            [  # ssh-stdin: stdin is the bridge socket, forwarded on purpose
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
            stderr=stderr_sink.fileno(),
            bufsize=0,
        )
    except OSError as error:
        local_stream.close()
        ssh_stream.close()
        stderr_sink.close()
        return None, {"reason": "spawn-failed", "detail": str(error)}
    ssh_stream.close()
    local_stream.settimeout(ready_timeout)
    status = read_ready_status(local_stream)
    if status == "ready" and bridge.poll() is None:
        local_stream.settimeout(None)
        # The child holds its own descriptor; later stderr stays bounded and
        # is discarded with the unlinked file.
        stderr_sink.close()
        return (bridge, local_stream), {}
    local_stream.close()
    returncode = stop_bridge(bridge)
    failure: dict[str, object] = {
        "reason": status if status != "ready" else "exited-after-ready",
        "ssh_returncode": returncode,
        "detail": stderr_tail(stderr_sink),
    }
    stderr_sink.close()
    return None, failure


def open_bridge(
    config: RelayConfig,
    host: str,
    port: int,
    *,
    attempt: Callable[..., tuple[object, dict[str, object]]] = attempt_bridge,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    emit: Callable[..., None] = emit_event,
) -> tuple[subprocess.Popen[bytes], socket.socket] | None:
    """Open a bridge, retrying connect-phase failures within a bounded budget.

    Nothing has been sent to the client yet, so a retry cannot duplicate data.
    """
    started = clock()
    deadline = started + config.connect_deadline
    per_attempt = config.connect_timeout * 2 + 2
    attempts = 0
    for pass_index in range(max(1, config.connect_passes)):
        if pass_index:
            remaining = deadline - clock()
            if remaining <= config.retry_backoff + 1:
                break
            backoff = config.retry_backoff * pass_index
            emit(
                "bridge_retry_pass",
                target=f"{host}:{port}",
                pass_number=pass_index + 1,
                backoff_seconds=backoff,
                attempts_so_far=attempts,
            )
            sleep(backoff)
        for relay_host in config.relay_hosts:
            remaining = deadline - clock()
            # SSH establishment and the remote destination connect are
            # sequential; each owns the configured timeout budget, clipped to
            # what is left of the overall deadline.
            if remaining < 1:
                break
            attempts += 1
            opened, failure = attempt(
                config, relay_host, host, port, min(per_attempt, remaining)
            )
            if opened is not None:
                if attempts > 1:
                    emit(
                        "bridge_opened_after_retry",
                        target=f"{host}:{port}",
                        relay_host=relay_host,
                        attempts=attempts,
                        elapsed_seconds=round(clock() - started, 3),
                    )
                return opened  # type: ignore[return-value]
            emit(
                "bridge_attempt_failed",
                target=f"{host}:{port}",
                relay_host=relay_host,
                attempt=attempts,
                pass_number=pass_index + 1,
                elapsed_seconds=round(clock() - started, 3),
                **failure,
            )
    emit(
        "bridge_exhausted",
        target=f"{host}:{port}",
        attempts=attempts,
        elapsed_seconds=round(clock() - started, 3),
    )
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
        except OSError:
            # Includes socket.timeout, which is not a TimeoutError on 3.9.
            return
        finally:
            self.request.settimeout(None)

        try:
            self.relay(request)
        except OSError:
            # The client went away before or while being answered.
            return

    def relay(self, request: bytes) -> None:
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
        sockets = [self.request, local_stream]
        last_activity = time.monotonic()
        try:
            self.request.settimeout(self.config.write_timeout)
            local_stream.settimeout(self.config.write_timeout)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
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
        except OSError:
            # Reset, broken pipe, or write timeout on either side ends the
            # tunnel; it is never re-opened once data has flowed.
            pass
        finally:
            local_stream.close()
            stop_bridge(bridge)


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
        "--log-path",
        default="",
        help="launchd log to rotate aside at startup once it reaches --log-max-bytes",
    )
    parser.add_argument("--log-max-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--log-generations", type=int, default=3)
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
    parser.add_argument("--connect-passes", type=int, default=2)
    parser.add_argument("--connect-deadline", type=float, default=30.0)
    parser.add_argument("--retry-backoff", type=float, default=0.25)
    return parser.parse_args(argv)


# EX_TEMPFAIL: another process holds the listen port. launchd's KeepAlive
# respawns on any exit, so the plist's ThrottleInterval is what spaces retries.
EXIT_PORT_IN_USE = 75


def port_holder(port: int, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """Who listens on port, as "pid N (command)", or why that is unknown."""
    try:
        result = run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"unknown (lsof failed: {error})"
    holders, pid = [], None
    for line in (result.stdout or "").splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c") and pid is not None:
            holders.append(f"pid {pid} ({line[1:]})")
            pid = None
    return ", ".join(holders) or "unknown (lsof found no listener)"


def serve(args: argparse.Namespace,
          holder: Callable[[int], str] | None = None) -> int:
    """Bind and serve, or refuse loudly when another process holds the port.

    A port held by another process does not free itself on a respawn: on
    2026-10-02 m3's relay lost 49125 to a hand-installed legacy bridge and
    launchd respawned it every ~10 s for days, 64,728 runs and a 220 MB log
    of identical tracebacks, while the attestation only said "last exit 1".
    """
    try:
        server = ThreadingServer(
            (args.listen_host, args.listen_port),
            ConnectHandler,
            max_handlers=args.max_handlers,
        )
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        who = (holder or port_holder)(args.listen_port)
        emit_event("listen_port_in_use", port=args.listen_port, holder=who)
        print(
            f"http-connect-ssh-relay: REFUSING TO START: {args.listen_host}:{args.listen_port} "
            f"is already held by {who}; stop that listener or move this relay's port",
            file=sys.stderr,
        )
        return EXIT_PORT_IN_USE
    with server:
        server.serve_forever()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.log_path:
        import pathlib  # noqa: PLC0415 - only the launchd entry point rotates

        from disk_reclaim import rotate_log  # noqa: PLC0415 - sibling module

        rotate_log(pathlib.Path(args.log_path).expanduser(),
                   args.log_max_bytes, args.log_generations)
    if (
        not 1 <= args.listen_port <= 65535
        or args.connect_timeout < 1
        or args.header_timeout < 1
        or args.tunnel_idle_timeout < 1
        or args.write_timeout < 1
        or args.max_handlers < 1
        or not 1 <= args.connect_passes <= 5
        or not 1 <= args.connect_deadline <= 120
        or not 0 <= args.retry_backoff <= 5
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
        connect_passes=args.connect_passes,
        connect_deadline=args.connect_deadline,
        retry_backoff=args.retry_backoff,
    )
    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
