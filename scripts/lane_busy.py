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

The process tree alone misses the start of a job. A tartci supervisor takes
its VM lease and clones the golden BEFORE `tart run` exists, and after boot it
spends minutes minting and launching the JIT runner; on 2026-09-22 the lane
that was killed had its lease and had logged "launching JIT runner" with no
job line yet. So a lane is also BUSY when, for the supervisor launchd is
running now:

  * it (or a descendant) holds a VM lease in the host lease store, or
  * its own heartbeat (`$TARTCI_STATE_DIR/<runner>.state.json`, written by
    providers/tart-macos/runner.sh `heartbeat`) is fresh and names a phase
    past waiting (BUSY_PHASES below), or
  * a descendant is `tart clone` (a clone in progress).

Idle heartbeat phases are IDLE_PHASES. A phase in neither set is unknown.

Staleness: a heartbeat is stale when older than max(600 s, 10 x the lane's
TARTCI_VM_POLL). A stale heartbeat is ignored, so a supervisor that crashed or
wedged after writing a busy phase does not refuse forever: with no work
descendant and no lease it reads idle ("stale heartbeat"). A lane whose plist
declares a state dir but has no heartbeat from the running supervisor (a
supervisor that has not heartbeated yet, or a state dir that cannot be read)
is unknown.

Every indeterminate reading is `unknown`, never `idle`: a launchctl error other
than launchd's own "Could not find service", an unreadable process table, an
unreadable lease store, or an unreadable or unrecognised heartbeat. Callers
refuse on `busy` and on `unknown`.

CLI (read-only):
    lane_busy.py [--json] [--agents-dir DIR] LABEL...
exit 0 every lane idle/absent, 1 some lane busy, 2 some lane unknown.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path
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
    # A clone in progress: the lease is already held and no `tart run` exists yet.
    ("tart clone", re.compile(r"(?:^|[\s/])tart\s+clone(?:\s|$)")),
)

# providers/tart-macos/runner.sh `heartbeat` phases. Past waiting: the
# supervisor has admitted work and owns (or is about to own) a VM or a runner.
BUSY_PHASES = frozenset({
    "admission-precheck",          # immediately precedes the clone
    "booting", "ensuring-runner", "aqua-preflight", "chrome-preflight",
    "admission-check", "admission-deferred", "admission-error",
    "minting-jit", "idle-wait", "idle-retarget-check",
    "job-running", "cancel-pending-terminal",
})
# Waiting for work, backing off, or refused before any VM exists.
IDLE_PHASES = frozenset({
    "waiting", "loop", "yielding", "draining", "stopped",
    "scan_blind", "scan_blind_escalated", "jit-admission-denied",
    "vm-lease-denied", "admission-precheck-deferred", "admission-precheck-error",
})
STALE_FLOOR_SECONDS = 600
STALE_POLL_MULTIPLE = 10
DEFAULT_POLL_SECONDS = 20
VM_LEASE_KINDS = ("tart-macos-vm", "tart-linux-vm", "qemu-windows-vm")
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


def tree(pid: int, table: "dict[int, tuple[int, str]]") -> "set[int]":
    """PID and every descendant."""
    children: dict[int, list[int]] = {}
    for child, (parent, _) in table.items():
        children.setdefault(parent, []).append(child)
    seen, stack = {pid}, list(children.get(pid, []))
    while stack:
        child = stack.pop()
        if child not in seen:
            seen.add(child)
            stack.extend(children.get(child, []))
    return seen


def default_agents_dir() -> Path:
    return Path(os.environ.get("TARTCI_LANE_AGENTS_DIR")
                or Path.home() / "Library" / "LaunchAgents")


def lease_records(path: "Path | None" = None) -> "list[dict] | None":
    """The host lease store, read without its lock (writes are atomic replaces).

    Never `leases.py status`: that reaps and rewrites the store, and this is a
    read-only probe. None when the store is unreadable.
    """
    if path is None:
        root = os.environ.get("TARTCI_LEASE_DIR") or str(Path.home() / ".tartci/state/leases")
        path = Path(root).expanduser() / "leases.json"
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text() or "[]")
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        return None
    return value


def lane_environment(label: str, agents_dir: Path) -> dict:
    try:
        value = plistlib.loads((agents_dir / f"{label}.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}
    env = value.get("EnvironmentVariables") if isinstance(value, dict) else None
    return env if isinstance(env, dict) else {}


def _age(ts: str, now: float) -> "float | None":
    try:
        return now - dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def heartbeat_state(env: dict, pids: "set[int]", now: float) -> "tuple[str, str] | None":
    """(state, detail) from the running supervisor's heartbeat, or None if the
    lane declares no state dir (legacy lanes, persistent Actions services)."""
    state_dir = env.get("TARTCI_STATE_DIR")
    if not state_dir:
        return None
    poll = env.get("TARTCI_VM_POLL") or env.get("PULP_VM_POLL") or ""
    poll_s = int(poll) if poll.isdigit() and int(poll) > 0 else DEFAULT_POLL_SECONDS
    stale_after = max(STALE_FLOOR_SECONDS, STALE_POLL_MULTIPLE * poll_s)
    try:
        files = sorted(Path(state_dir).glob("*.state.json"))
    except OSError as exc:
        return UNKNOWN, f"state dir {state_dir} unreadable: {exc}"
    current = []
    for path in files:
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = str(value.get("supervisor_pid") or "")
        if pid.isdigit() and int(pid) in pids:
            current.append((path, value))
    if not current:
        return UNKNOWN, (f"no heartbeat from the running supervisor in {state_dir} "
                         "(not yet written, or unreadable)")
    path, value = max(current, key=lambda item: str(item[1].get("ts") or ""))
    phase = str(value.get("phase") or "")
    age = _age(str(value.get("ts") or ""), now)
    if age is None:
        return UNKNOWN, f"heartbeat {path.name} has no readable timestamp"
    if age > stale_after:
        return IDLE, f"stale heartbeat ({phase}, {int(age)}s old > {stale_after}s)"
    if phase in BUSY_PHASES:
        return BUSY, f"supervisor heartbeat phase {phase} ({int(age)}s ago, vm {value.get('vm') or '-'})"
    if phase in IDLE_PHASES:
        return IDLE, f"supervisor heartbeat phase {phase}"
    return UNKNOWN, f"unrecognised heartbeat phase {phase!r}"


def probe(labels: "list[str]", run: Runner = _run, *, agents_dir: "Path | None" = None,
          leases: "list[dict] | None | bool" = True,
          now: "float | None" = None) -> "list[LaneBusy]":
    agents_dir = agents_dir or default_agents_dir()
    now = time.time() if now is None else now
    lease_rows: "list[dict] | None | bool" = leases if leases is not True else True
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
        if worker is not None:
            worker_pid, kind, command = worker
            results.append(LaneBusy(
                label, BUSY, f"supervisor pid {pid} owns {kind} pid {worker_pid}",
                pid, worker_pid, kind, command[:300]))
            continue
        pids = tree(pid, table)
        if lease_rows is True:
            lease_rows = lease_records()
        if lease_rows is None:
            results.append(LaneBusy(label, UNKNOWN, "host lease store unreadable", pid))
            continue
        held = [row for row in lease_rows
                if row.get("command_kind") in VM_LEASE_KINDS
                and isinstance(row.get("pid"), int) and row["pid"] in pids]
        if held:
            row = held[0]
            results.append(LaneBusy(
                label, BUSY, f"supervisor pid {pid} holds VM lease {row.get('id')} "
                f"(vm {row.get('vm_name') or '-'}, no tart run yet)", pid,
                row["pid"], "vm-lease", str(row.get("vm_name") or "")))
            continue
        beat = heartbeat_state(lane_environment(label, agents_dir), pids, now)
        if beat is None:
            results.append(LaneBusy(label, IDLE, "no work descendant, no VM lease", pid))
            continue
        state, detail = beat
        results.append(LaneBusy(label, state, detail, pid,
                                worker_kind="heartbeat" if state == BUSY else None))
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
    parser.add_argument("--agents-dir", type=Path, default=None,
                        help="LaunchAgents dir holding each label's plist (its state dir)")
    args = parser.parse_args(argv)
    results = probe(args.labels, agents_dir=args.agents_dir)
    if args.json:
        print(json.dumps([row.as_dict() for row in results], indent=2))
    else:
        for row in results:
            print(f"{row.label}\t{row.state}\t{row.detail}")
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
