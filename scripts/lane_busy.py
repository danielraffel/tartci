#!/usr/bin/env python3
"""Is a launchd-supervised runner lane mid-job right now?

A lane supervisor (a tartci fleet LaunchAgent, a legacy `tart-runner`, or a
persistent `actions.runner.*` service) is mid-job exactly when the process
launchd started for it has a live descendant doing the work:

  * `tart run ...`   the lane's VM, where an ephemeral runner executes a job
  * `qemu-system-*`  the Windows lane's VM (QEMU on the host)
  * `Runner.Worker`  a persistent Actions runner executing a job on the host

Stopping the supervisor (`launchctl bootout`, `kickstart -k`, `tartci pool off`)
kills that descendant with it. On 2026-09-22 a raw kickstart of a lane
supervisor killed a healthy, just-recovered VM mid-job.

This probe is per LABEL on purpose. The watchdog's host-wide "is any Tart VM
running" guard is right for an unattended heal, but on a host with several
lanes it would refuse an operator's reload of an idle lane because a sibling
lane is building.

Every indeterminate reading is `unknown`, never `idle`: a launchctl error other
than launchd's own "Could not find service", or an unreadable process table.
Callers refuse on `busy` and on `unknown`.

CLI (read-only):
    lane_busy.py [--json] LABEL...
exit 0 every lane idle/absent, 1 some lane busy, 2 some lane unknown.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Callable, NamedTuple, Optional

BUSY = "busy"
IDLE = "idle"
ABSENT = "absent"
UNKNOWN = "unknown"

# A VM a lane supervisor is holding, and a job a persistent runner is running.
# Matched anywhere in the argv so a lease wrapper exec'ing `tart run` counts.
WORK_PATTERNS = (
    ("tart run", re.compile(r"(?:^|[\s/])tart\s+run(?:\s|$)")),
    ("Runner.Worker", re.compile(r"(?:^|[\s/])Runner\.Worker(?:\s|$)")),
    # The Windows lane boots its VM directly on the host (qemu-windows/runner.sh).
    ("qemu-system", re.compile(r"(?:^|[\s/])qemu-system-[A-Za-z0-9_]+(?:\s|$)")),
)
_PID_LINE = re.compile(r"^\s*pid = (\d+)\s*$", re.M)

Runner = Callable[[list], "tuple[int, str, str]"]


class LaneBusy(NamedTuple):
    label: str
    state: str
    detail: str
    supervisor_pid: Optional[int] = None
    worker_pid: Optional[int] = None
    worker_kind: Optional[str] = None
    worker_command: Optional[str] = None

    def as_dict(self) -> dict:
        return self._asdict()


def _run(cmd: list) -> "tuple[int, str, str]":
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def domain() -> str:
    uid = os.environ.get("TARTCI_POOL_UID") or str(os.getuid())
    return f"gui/{uid}"


def launchctl_absent(rc: int, out: str, err: str) -> bool:
    """launchd's own proof that the service is not loaded (exit 113)."""
    return rc == 113 and "Could not find service" in (err + out)


def process_table(run: Runner = _run) -> "dict[int, tuple[int, str]] | None":
    rc, out, _ = run(["ps", "-A", "-ww", "-o", "pid=,ppid=,command="])
    if rc != 0 or not out.strip():
        return None
    table: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return table or None


def find_worker(pid: int, table: "dict[int, tuple[int, str]]") -> "tuple[int, str, str] | None":
    """First descendant of PID doing job work: (pid, kind, command)."""
    children: dict[int, list[int]] = {}
    for child, (parent, _) in table.items():
        children.setdefault(parent, []).append(child)
    stack = list(children.get(pid, []))
    seen: set[int] = set()
    while stack:
        child = stack.pop()
        if child in seen:
            continue
        seen.add(child)
        command = table[child][1]
        for kind, pattern in WORK_PATTERNS:
            if pattern.search(command):
                return child, kind, command
        stack.extend(children.get(child, []))
    return None


def probe(labels: "list[str]", run: Runner = _run) -> "list[LaneBusy]":
    results: list[LaneBusy] = []
    table: "dict[int, tuple[int, str]] | None" = None
    table_read = False
    for label in labels:
        rc, out, err = run(["launchctl", "print", f"{domain()}/{label}"])
        if launchctl_absent(rc, out, err):
            results.append(LaneBusy(label, ABSENT, "not loaded in launchd"))
            continue
        if rc != 0:
            results.append(LaneBusy(
                label, UNKNOWN,
                f"launchctl print failed (exit {rc}): {(err or out).strip()[:200]}"))
            continue
        match = _PID_LINE.search(out)
        if match is None:
            results.append(LaneBusy(label, IDLE, "loaded, no running process"))
            continue
        pid = int(match.group(1))
        if not table_read:
            table, table_read = process_table(run), True
        if table is None:
            results.append(LaneBusy(label, UNKNOWN, "process table unreadable", pid))
            continue
        worker = find_worker(pid, table)
        if worker is None:
            results.append(LaneBusy(label, IDLE, "no tart run / Runner.Worker descendant", pid))
            continue
        worker_pid, kind, command = worker
        results.append(LaneBusy(
            label, BUSY, f"supervisor pid {pid} owns {kind} pid {worker_pid}",
            pid, worker_pid, kind, command[:300]))
    return results


def exit_code(results: "list[LaneBusy]") -> int:
    states = {row.state for row in results}
    if UNKNOWN in states:
        return 2
    if BUSY in states:
        return 1
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("labels", nargs="+")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    results = probe(args.labels)
    if args.json:
        print(json.dumps([row.as_dict() for row in results], indent=2))
    else:
        for row in results:
            print(f"{row.label}\t{row.state}\t{row.detail}")
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
