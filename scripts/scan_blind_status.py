#!/usr/bin/env python3
"""Evaluate scan-blind streaks only for currently installed fleet lanes."""
from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path

PREFIX = "com.danielraffel.tartci.tart-runner-macos-fleet."


def lane_log_name(plist: Path) -> str | None:
    """The log a receipted launch agent writes, as its plist declares it.

    The plist's StandardOutPath is the source of truth: the plist name and
    the log name spell multi-part lanes differently (<host>.pulp-gate.slot2
    writes macos-fleet-pulp-gate-slot2.log).
    """
    try:
        with plist.open("rb") as handle:
            declared = plistlib.load(handle).get("StandardOutPath")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return Path(declared).name if isinstance(declared, str) and declared else None


def active_log_names(receipt: dict) -> set[str]:
    """Map receipted launch agents to their macos-fleet log basenames."""
    names: set[str] = set()
    rows = []
    for key in ("plists", "persistent_plists"):
        value = receipt.get(key)
        if isinstance(value, dict):
            rows.extend(value)
    agents_dir = receipt.get("agents_dir")
    for raw in rows:
        name = Path(str(raw)).name
        if isinstance(agents_dir, str) and agents_dir:
            declared = lane_log_name(Path(agents_dir) / name)
            if declared:
                names.add(declared)
                continue
        stem = name.removesuffix(".plist")
        if stem.startswith(PREFIX):
            # <host>.<lane>[.<slot>]: the lane identity joins the parts
            # after the host with `-`, as macos_fleet_lanes.py names logs.
            parts = stem[len(PREFIX):].split(".")
            lane = "-".join(parts[1:]) if len(parts) > 1 else parts[0]
            names.add(f"macos-fleet-{lane}.log")
        elif stem:
            # Test and legacy receipts may carry the already-short lane name.
            names.add(f"macos-fleet-{stem}.log")
    return names


def blind_problems(receipt: dict, log_paths: list[str], threshold: int) -> list[dict[str, object]]:
    active = active_log_names(receipt)
    problems: list[dict[str, object]] = []
    for raw in log_paths:
        path = Path(raw)
        if path.name not in active:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        count = 0
        for line in reversed(lines):
            if "SCAN BLIND" in line:
                count += 1
            elif line.strip():
                break
        if count >= threshold:
            problems.append({"code": "scan_blind", "label": str(path), "cycles": count})
    return problems


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: scan_blind_status.py RECEIPT THRESHOLD LOGS_FILE")
    receipt = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    logs = [line for line in Path(sys.argv[3]).read_text(encoding="utf-8").splitlines() if line]
    print(json.dumps(blind_problems(receipt, logs, int(sys.argv[2])), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
