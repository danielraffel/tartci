#!/usr/bin/env python3
"""Plan, then remove, expired bench clones with opt-in sidecar markers."""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

Runner = Callable[..., subprocess.CompletedProcess]
MARKER = ".tartci-bench.json"
GIB = 1024 ** 3


def size_bytes(path: pathlib.Path) -> int:
    """Measure without following links outside the bench store."""
    if path.is_symlink():
        return path.lstat().st_size
    if path.is_file():
        return path.stat().st_size
    total = 0
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            try:
                total += (pathlib.Path(base) / name).lstat().st_size
            except OSError:
                pass
    return total


def marker_created(path: pathlib.Path) -> tuple[datetime, float]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("marker is not a regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("owner"), str) \
            or not value["owner"]:
        raise ValueError("invalid owner")
    ttl = value.get("ttl_days")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) \
            or not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("invalid ttl_days")
    created = datetime.fromisoformat(value["created"].replace("Z", "+00:00"))
    if created.tzinfo is None or created.utcoffset() != timedelta(0):
        raise ValueError("created must be UTC")
    return created, ttl


def in_use(path: pathlib.Path, runner: Runner) -> bool:
    argv = ["lsof", "-t"] + (["+D"] if path.is_dir() else []) + [str(path)]
    try:
        result = runner(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return True
    # lsof returns 1 and no output when no file is open. Any other failure is blind.
    return bool(result.stdout.strip()) or result.returncode not in (0, 1) \
        or bool(result.stderr.strip())


def plan(store: pathlib.Path, runner: Runner = subprocess.run,
         now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    entries = []
    if not store.is_dir() or store.is_symlink():
        return {"store": str(store), "entries": entries, "delete_bytes": 0,
                "error": "bench store unavailable"}
    for path in sorted(store.iterdir()):
        if path.name.endswith(MARKER):
            continue
        marker = pathlib.Path(str(path) + MARKER)
        row: dict[str, Any] = {"name": path.name, "path": str(path),
                               "size_bytes": size_bytes(path)}
        if not marker.exists() and not marker.is_symlink():
            row.update(verdict="unmanaged", reason="no marker")
        elif path.is_symlink():
            row.update(verdict="keep", reason="clone is a symlink")
        else:
            try:
                created, ttl = marker_created(marker)
                if now < created + timedelta(days=ttl):
                    row.update(verdict="keep", reason="within TTL")
                elif in_use(path, runner):
                    row.update(verdict="keep", reason="in use or lsof unavailable")
                else:
                    row.update(verdict="delete", reason="expired and not open")
            except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
                row.update(verdict="keep", reason=f"unreadable marker: {exc}")
        entries.append(row)
    return {"store": str(store), "entries": entries,
            "delete_bytes": sum(row["size_bytes"] for row in entries
                                if row["verdict"] == "delete")}


def apply(report: dict[str, Any], runner: Runner = subprocess.run) -> list[str]:
    errors = []
    store = pathlib.Path(report["store"])
    if report.get("error") or store.is_symlink():
        return [report.get("error", "bench store is a symlink")]
    for row in report["entries"]:
        if row["verdict"] != "delete":
            continue
        path = store / row["name"]
        marker = pathlib.Path(str(path) + MARKER)
        try:
            # Recheck the two deletion gates after planning.
            created, ttl = marker_created(marker)
            if datetime.now(timezone.utc) < created + timedelta(days=ttl) \
                    or path.is_symlink() or in_use(path, runner):
                errors.append(f"{path}: changed or in use since plan")
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            marker.unlink()
            row["deleted"] = True
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"{path}: {exc}")
    return errors


def main(argv: list[str] | None = None, runner: Runner = subprocess.run,
         now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete what the plan names")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    home = pathlib.Path(os.environ.get("TARTCI_HOME", pathlib.Path.home() / ".tartci"))
    store = pathlib.Path(os.environ.get("TARTCI_BENCH", home / "bench"))
    report = plan(store, runner, now)
    report["mode"] = "apply" if args.apply else "plan"
    if args.apply and not report.get("error"):
        report["errors"] = apply(report, runner)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for row in report["entries"]:
            print(f"{row['verdict']:9} {row['size_bytes'] / GIB:6.1f} GiB  "
                  f"{row['name']}  ({row['reason']})")
        print(f"would delete {report['delete_bytes'] / GIB:.1f} GiB" if not args.apply
              else f"planned deletion: {report['delete_bytes'] / GIB:.1f} GiB")
        for error in report.get("errors", []):
            print(f"error: {error}")
        if report.get("error"):
            print(f"error: {report['error']}")
    return 2 if report.get("error") else 1 if report.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
