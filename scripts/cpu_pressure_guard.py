#!/usr/bin/env python3
"""Report non-TartCI CPU hogs with a parent-process census."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

ALLOWLIST = re.compile(
    r"(?:^|/)(?:tartci|tart|qemu-system-[^ /]+|qemu-img|claude|codex|cmux)(?:$|[ /])",
    re.I,
)


def _seconds(value: str) -> float:
    parts = value.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def _elapsed(value: str) -> float:
    value = value.strip()
    days, _, rest = value.partition("-")
    base = int(days) * 86400 if rest else 0
    rest = rest or value
    parts = rest.split(":")
    try:
        if len(parts) == 3:
            return base + int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return base + int(parts[0]) * 60 + float(parts[1])
    except ValueError:
        pass
    return base + _seconds(rest)


def read_processes(ps: str | None = None) -> list[dict]:
    command = ps or os.environ.get("TARTCI_CPU_GUARD_PS", "ps")
    result = subprocess.run([command, "-axo", "pid=,ppid=,etime=,time=,%cpu=,command="],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"ps exited {result.returncode}")
    rows = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 5)
        if len(fields) != 6:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        try:
            cpu_percent = float(fields[4])
        except ValueError:
            continue
        rows.append({"pid": pid, "ppid": ppid, "elapsed": _elapsed(fields[2]),
                     "cpu_seconds": _seconds(fields[3]), "cpu_percent": cpu_percent,
                     "command": fields[5]})
    return rows


def _context(row: dict, by_pid: dict[int, dict]) -> dict:
    chain = []
    current = row
    seen = set()
    while current and current["pid"] not in seen and len(chain) < 16:
        seen.add(current["pid"])
        chain.append(current)
        current = by_pid.get(current["ppid"])
    text = " <- ".join(item["command"] for item in chain)
    paths = re.findall(r"(?:/Volumes/[^ ]+|/Users/[^ ]+|/tmp/[^ ]+)", text)
    worktree = next((p for p in paths if "worktree" in p.lower()), None)
    parent = chain[1]["command"] if len(chain) > 1 else "unknown parent"
    return {"parent_session": parent, "worktree": worktree, "process_tree": text}


def inspect(profile: Path, *, ps: str | None = None) -> dict:
    if tomllib is None:
        raise RuntimeError("Python 3.11+ with tomllib is required")
    config = tomllib.loads(profile.read_text()) if profile.is_file() else {}
    policy = config.get("cpu_pressure") or {}
    threshold = float(policy.get("pressure_fraction", 0.25))
    cores = int(policy.get("cores", os.cpu_count() or 1))
    rows = read_processes(ps)
    by_pid = {row["pid"]: row for row in rows}
    offenders = []
    for row in rows:
        row["pressure_fraction"] = row["cpu_percent"] / 100.0 / max(1, cores)
        if row["pressure_fraction"] < threshold or ALLOWLIST.search(row["command"]):
            continue
        row = {**row, **_context(row, by_pid)}
        row["action"] = "alert"
        offenders.append(row)
    return {"schema": "tartci.cpu-pressure/v1", "pressure_fraction": threshold,
            "cores": cores, "offenders": offenders,
            "observed_at": time.time()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci doctor cpu-pressure")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = inspect(args.profile)
    except (OSError, RuntimeError) as exc:
        print(f"cpu-pressure: UNKNOWN: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif result["offenders"]:
        for offender in result["offenders"]:
            print(f"cpu-pressure: ALERT pid={offender['pid']} pressure={offender['pressure_fraction']:.3f} "
                  f"parent={offender['parent_session']} worktree={offender['worktree'] or '-'} "
                  f"action={offender['action']} command={offender['command']}")
    else:
        print("cpu-pressure: OK")
    return 1 if result["offenders"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
