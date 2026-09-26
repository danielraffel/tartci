#!/usr/bin/env python3
"""Report this host's warm (pre-booted, parked) gate VM, if any.

A supervisor with TARTCI_WARM_VM=1 publishes its parked VM to
`~/.tartci/state/warm-vm/parked.json` (providers/tart-macos/warm-vm.lib.sh)
and refreshes it every loop pass. This reads that record for `tartci pool
status` and `tartci doctor`. It never acts: a stale record is reported, the
supervisor that wrote it (or the reaper) owns the VM.

States:
  none     no record: no warm VM is parked (the default, and the state of every
           host that has not opted in)
  parked   a live supervisor refreshed the record recently, inside its max age
  stale    the record's supervisor is gone or stopped refreshing it
  overdue  parked longer than its own max park age plus a grace period
  unreadable  the record exists but cannot be parsed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA = "tartci.warm-vm/v1"
# The supervisor refreshes the record every poll (20 s by default); allow a
# few missed passes, including one spent handing off or tearing down.
FRESH_SECONDS = 180
OVERDUE_GRACE_SECONDS = 180


def default_dir() -> Path:
    return Path(os.environ.get("TARTCI_WARM_VM_DIR")
                or Path.home() / ".tartci/state/warm-vm")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def status(warm_dir: Path | None = None, *, now: float | None = None,
           pid_alive: Callable[[int], bool] = _pid_alive) -> dict[str, Any]:
    warm_dir = warm_dir or default_dir()
    now = time.time() if now is None else now
    path = warm_dir / "parked.json"
    if not path.exists():
        return {"state": "none", "path": str(path)}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        pid = int(record["supervisor_pid"])
        ts = int(record["ts"])
        parked_at = int(record["parked_at"])
        max_park = int(record["max_park_seconds"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"state": "unreadable", "path": str(path), "error": str(exc)}
    result = {
        "path": str(path), "vm": record.get("vm"), "runner": record.get("runner"),
        "repo": record.get("repo"), "lane": record.get("lane"),
        "supervisor_pid": pid, "parked_seconds": int(now - parked_at),
        "max_park_seconds": max_park, "record_age_seconds": int(now - ts),
        "reserved_cores": record.get("reserved_cores", 0),
        "reserved_mem_mb": record.get("reserved_mem_mb"),
        "vm_cores": record.get("vm_cores"), "lease_id": record.get("lease_id"),
    }
    if not pid_alive(pid) or now - ts > FRESH_SECONDS:
        result["state"] = "stale"
    elif now - parked_at > max_park + OVERDUE_GRACE_SECONDS:
        result["state"] = "overdue"
    else:
        result["state"] = "parked"
    return result


def describe(value: dict[str, Any]) -> str:
    state = value.get("state")
    if state == "none":
        return "none parked"
    if state == "unreadable":
        return f"UNREADABLE ({value.get('error')})"
    text = (f"{value.get('vm')} for {value.get('lane')} parked {value.get('parked_seconds')}s "
            f"(max {value.get('max_park_seconds')}s), reserved cores=0 "
            f"mem_mb={value.get('reserved_mem_mb')}")
    if state == "parked":
        return f"parked: {text}"
    if state == "overdue":
        return f"OVERDUE: {text}"
    return (f"STALE: {text}; supervisor pid {value.get('supervisor_pid')} last refreshed "
            f"{value.get('record_age_seconds')}s ago")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    value = status(args.dir)
    print(json.dumps(value, sort_keys=True) if args.json else f"warm vm: {describe(value)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
