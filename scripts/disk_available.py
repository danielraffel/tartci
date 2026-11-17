#!/usr/bin/env python3
"""Available disk space as macOS counts it: free plus what it can purge.

statfs (`df`, shutil.disk_usage) counts only free blocks. On m5studio it read
49 GB free (98% used) on /System/Volumes/Data while
volumeAvailableCapacityForImportantUsage read 560 GB, so readiness flagged
disk_pressure, and a lease or reclaim check that trusts statfs refuses or
escalates on a volume with plenty of room.

"Available" is the larger of the two, never past the volume's size, with one
exception: raw free space under DISK_RAW_FLOOR_BYTES is taken as it is.
Purgeable space is reclaimed on demand and with lag, and a build that needs
the room now gets ENOSPC before the system purges. If the macOS reading is
unavailable (no swift, a non-APFS volume, a timeout) the raw number stands.
TARTCI_DISK_PURGEABLE=0 turns the macOS reading off.

CLI (for shell callers): `disk_available.py kb PATH` prints the available
KiB; the raw and important-usage figures go to stderr.

Python 3.9-safe: leases and shell callers run it under /usr/bin/python3.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

DISK_RAW_FLOOR_BYTES = 20 * 1024 ** 3
IMPORTANT_CAPACITY_SCRIPT = Path(__file__).with_name("disk_important_capacity.swift")
IMPORTANT_CAPACITY_TIMEOUT_S = 20
# The macOS reading is cached per volume this long. It is the slow part (a
# swift run, about 1 s) and moves slowly; the raw statfs free space is read on
# every call and never cached, so a real disk-full is not hidden.
CACHE_TTL_S = 60


def important_free_bytes(path: Path, timeout: float = IMPORTANT_CAPACITY_TIMEOUT_S) -> Optional[int]:
    """Bytes macOS says are available for important usage on `path`'s volume
    (free plus purgeable), or None when it cannot be read."""
    from bounded_subprocess import ObservationError, run_bounded  # noqa: PLC0415
    try:
        proc = run_bounded(["/usr/bin/swift", str(IMPORTANT_CAPACITY_SCRIPT), str(path)],
                           timeout=timeout, operation="important_capacity")
    except (OSError, ObservationError, ValueError):
        return None
    text = proc.stdout.strip()
    if proc.returncode != 0 or not text.isdigit():
        return None
    return int(text)


def effective_available(raw_free: int, total: int, important: Optional[int]) -> tuple:
    """(available_bytes, below_raw_floor) from the raw and important-usage numbers."""
    below_floor = raw_free < DISK_RAW_FLOOR_BYTES
    if important is None or below_floor:
        return raw_free, below_floor
    return min(max(raw_free, important), total), below_floor


def _cache_path() -> Path:
    explicit = os.environ.get("TARTCI_DISK_CACHE_FILE")
    if explicit:
        return Path(explicit)
    state = os.environ.get("TARTCI_STATE_DIR") or str(Path.home() / ".tartci" / "state")
    return Path(state) / "disk-important-cache.json"


def _volume_key(path: Any) -> Optional[str]:
    try:
        return str(os.stat(path).st_dev)
    except OSError:
        return None


def _cache_read(key: Optional[str], now: float) -> Optional[dict]:
    """The volume's entry when it is fresh, else None (also for any unreadable file)."""
    if key is None:
        return None
    try:
        entry = json.loads(_cache_path().read_text(encoding="utf-8")).get("volumes", {}).get(key)
        at = float(entry["at"])
        if not (0 <= now - at < CACHE_TTL_S):
            return None
        raw_then = int(entry["raw_free"])
        value = entry.get("important")
        return {"important": None if value is None else int(value), "raw_free": raw_then}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _cache_write(key: Optional[str], important: Optional[int], raw_free: int, now: float) -> None:
    if key is None:
        return
    path = _cache_path()
    try:
        try:
            volumes = json.loads(path.read_text(encoding="utf-8")).get("volumes", {})
            volumes = {k: v for k, v in volumes.items()
                       if isinstance(v, dict) and 0 <= now - float(v.get("at", 0)) < CACHE_TTL_S}
        except (OSError, ValueError, TypeError, AttributeError):
            volumes = {}
        volumes[key] = {"at": now, "important": important, "raw_free": raw_free}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"volumes": volumes}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # the cache only saves a swift run; never fail a probe over it


def measure(path: Any, raw_free: int, total: int,
            timeout: float = IMPORTANT_CAPACITY_TIMEOUT_S, now: Optional[float] = None) -> dict:
    """Available bytes for `path`'s volume, with both source numbers.

    `raw_free` is the caller's live statfs reading. The important-usage figure
    comes from a per-volume cache up to CACHE_TTL_S old; when raw free space
    has fallen since the cache was written, the same drop is taken off the
    cached figure, so a cache never overstates what is free now.
    """
    now = time.time() if now is None else now
    important = None
    cached = False
    if os.environ.get("TARTCI_DISK_PURGEABLE", "1") != "0":
        key = _volume_key(path)
        hit = _cache_read(key, now)
        if hit is not None:
            cached = True
            important = hit["important"]
            if important is not None:
                important = max(0, important - max(0, hit["raw_free"] - int(raw_free)))
        else:
            important = important_free_bytes(Path(path), timeout)
            _cache_write(key, important, int(raw_free), now)
    available, below_floor = effective_available(int(raw_free), int(total), important)
    return {"available_bytes": available, "raw_free_bytes": int(raw_free),
            "important_free_bytes": important, "below_raw_floor": below_floor,
            "important_cached": cached}


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "kb":
        print("usage: disk_available.py kb PATH", file=sys.stderr)
        return 64
    try:
        usage = shutil.disk_usage(args[1])
    except OSError as exc:
        print(f"disk_available: {exc}", file=sys.stderr)
        return 1
    value = measure(args[1], usage.free, usage.total)
    print(f"disk_available: raw_free_kb={value['raw_free_bytes'] // 1024} "
          f"important_free_kb={'' if value['important_free_bytes'] is None else value['important_free_bytes'] // 1024} "
          f"below_raw_floor={int(value['below_raw_floor'])}", file=sys.stderr)
    print(value["available_bytes"] // 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
