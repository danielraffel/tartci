#!/usr/bin/env python3
"""Pre-mint outcomes per lane per day: how many booted VMs were retargeted to
another waiting class and how many were discarded unused.

Every pre-mint denial ends in exactly one of the two. A VM the pre-mint hold
kept for its own returning class was not discarded either, so it counts with
the retargets. Before retargeting existed every denial was a discard, so
discards are counted as denials minus retargets; that keeps older event logs
comparable.

    tartci pre-mint-outcomes [--state-root ~/.tartci/state] [--days N] [--json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

DENIED = "assignment_v2_pre_mint_denied"
RETARGET = "assignment_v2_pre_mint_retarget"
KEPT = "assignment_v2_pre_mint_kept"


def lane_logs(state_root: Path) -> dict[str, Path]:
    """{lane: events.jsonl} for every macOS fleet lane under the state root."""
    fleet = state_root / "macos-fleet"
    if not fleet.is_dir():
        return {}
    return {child.name: child / "events.jsonl"
            for child in sorted(fleet.iterdir()) if (child / "events.jsonl").is_file()}


def outcomes(logs: dict[str, Path], since: dt.date | None = None) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"denied": 0, "retargets": 0})
    for lane, path in logs.items():
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if DENIED not in line and RETARGET not in line and KEPT not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                day = str(record.get("ts", ""))[:10]
                if not day or (since and day < since.isoformat()):
                    continue
                if record.get("event") == DENIED:
                    counts[(lane, day)]["denied"] += 1
                elif record.get("event") in (RETARGET, KEPT):
                    counts[(lane, day)]["retargets"] += 1
    rows = []
    for (lane, day), value in sorted(counts.items(), key=lambda item: (item[0][1], item[0][0])):
        retargets = min(value["retargets"], value["denied"])
        rows.append({"day": day, "lane": lane, "denied": value["denied"],
                     "retargets": retargets, "discards": value["denied"] - retargets})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-root", type=Path,
                        default=Path(os.environ.get("TARTCI_HOME", Path.home() / ".tartci")) / "state")
    parser.add_argument("--days", type=int, default=7, help="most recent days to report (0 = all)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    since = None
    if args.days > 0:
        since = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=args.days - 1)
    rows = outcomes(lane_logs(args.state_root), since)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no pre-mint denials recorded")
        return 0
    print(f"{'day':<11} {'lane':<24} {'denied':>6} {'retargets':>9} {'discards':>8}")
    for row in rows:
        print(f"{row['day']:<11} {row['lane']:<24} {row['denied']:>6} {row['retargets']:>9} "
              f"{row['discards']:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
