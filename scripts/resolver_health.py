#!/usr/bin/env python3
"""Host resolver and TCP health probe used by the watchdog's R1 alert.

The resolver probe uses a name lookup while the TCP probe deliberately uses an
IP literal.  Their four-way result separates a wedged system resolver from a
real network or upstream outage.  A non-healthy result must repeat for three
ticks before it becomes durable peer-visible state.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import time
from typing import Any, Callable, Dict, Optional, Tuple

PROBE_NAME = "github.com"
DEFAULT_TCP_IP = "140.82.112.3"
HYSTERESIS_TICKS = 3
TIMEOUT_SECONDS = 5
IPV4_LINE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

Runner = Callable[[list[str]], Tuple[int, str, str]]


def state_dir() -> pathlib.Path:
    home = os.environ.get("TARTCI_HOME") or str(pathlib.Path.home() / ".tartci")
    return pathlib.Path(home) / "state" / "resolver-health"


def _read(path: pathlib.Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: pathlib.Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _run(argv: list[str]) -> Tuple[int, str, str]:
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        try:
            out, err = proc.communicate(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            return 124, out or "", err or "timeout"
        return proc.returncode, out or "", err or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)


def probe(run: Optional[Runner] = None, tcp_ip: Optional[str] = None) -> Dict[str, Any]:
    """Return resolver/tcp booleans and one of the four classifications."""
    run = run or _run
    tcp_ip = tcp_ip or os.environ.get("TARTCI_RESOLVER_PROBE_IP", DEFAULT_TCP_IP)
    # dscacheutil exits after one answer. dns-sd can print an answer and then
    # wait forever, so _run may return 124 after killing it; that is still a
    # successful resolver probe when an address was printed first.
    resolver_rc, resolver_out, resolver_err = run(
        ["dscacheutil", "-q", "host", "-a", "name", PROBE_NAME])
    tcp_rc, tcp_out, tcp_err = run(["nc", "-z", "-G", "3", tcp_ip, "443"])
    resolver_ok = resolver_rc in (0, 124) and bool(IPV4_LINE.search(resolver_out))
    tcp_ok = tcp_rc == 0
    if not resolver_ok and tcp_ok:
        condition = "resolver_dead"
    elif not resolver_ok and not tcp_ok:
        condition = "network_down"
    elif resolver_ok and not tcp_ok:
        condition = "upstream_unreachable"
    else:
        condition = "healthy"
    return {"resolver_ok": resolver_ok, "tcp_ok": tcp_ok, "condition": condition,
            "resolver_error": resolver_err[:200], "tcp_error": tcp_err[:200],
            "resolver_output": resolver_out[:200], "tcp_output": tcp_out[:200]}


def tick(now: Optional[float] = None, directory: Optional[pathlib.Path] = None,
         run: Optional[Runner] = None) -> Dict[str, Any]:
    """Probe once, apply three-tick hysteresis, and publish peer-readable state."""
    now = time.time() if now is None else now
    directory = directory or state_dir()
    path = directory / "state.json"
    previous = _read(path)
    result = probe(run=run)
    condition = result["condition"]
    candidate = previous.get("candidate")
    streak = int(previous.get("candidate_streak") or 0) if candidate == condition else 0
    streak += 1
    active = previous.get("condition")
    if condition == "healthy":
        active = "healthy"
    elif streak >= HYSTERESIS_TICKS:
        active = condition
    since = previous.get("since")
    if active != previous.get("condition"):
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    state = {**result, "condition": active, "candidate": condition,
             "candidate_streak": streak, "since": since,
             "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
    _write(path, state)
    return state


if __name__ == "__main__":
    print(json.dumps(tick(), sort_keys=True))
