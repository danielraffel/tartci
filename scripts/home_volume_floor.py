#!/usr/bin/env python3
"""A per-host free-space floor for the home volume, judged at VM admission.

The lease disk axis judges the volume that holds the Tart store. On a host
whose store is on another volume, the home (boot) volume is judged by nothing
at admission, yet it holds every supervisor's temp files, the build trees and
each tool's state. m5studio, 2026-10-04: the store was on /Volumes/<data-volume>,
the boot Data volume filled to 99% with coverage build dirs, ENOSPC killed a
merge-group runner at 19:35Z and every lane supervisor exited 1 (`cannot
create temp file for here document`), while VM leases kept being granted.

So a VM lease also judges the home volume when it is a different device from
the store, against a floor computed from this host's own numbers:

    floor = clamp(max(MIN_FLOOR, fill_rate x hours_to_next_reclaim_pass),
                  MIN_FLOOR, MAX_FRACTION x volume size)

`fill_rate` is how fast this volume has been losing free space, measured over
at least MIN_WINDOW_S of samples (a transient spike cannot inflate it), and the
reclaim pass is what frees space, so the floor is the space the host needs to
survive until the next pass. A volume smaller than MIN_FLOOR / MAX_FRACTION
gets MAX_FRACTION of its size. Below the floor a NEW clone is refused; running
jobs and supervisors are never touched. A volume that cannot be read admits
the lease and says so (`unread`), because a probe failure must never become a
capacity outage.

State lives beside the lease records (`home-volume.json` in the store dir):
the free-space samples, the consecutive-denial count and when reads started
failing, which tartci doctor fleet reads.

Python 3.9-safe: the launchd python on fleet hosts is /usr/bin/python3.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

GIB = 1024 ** 3
MIN_FLOOR = 30 * GIB
MAX_FRACTION = 0.20
MIN_WINDOW_S = 6 * 3600
KEEP_S = 24 * 3600
SAMPLE_EVERY_S = 300
STATE_FILE = "home-volume.json"


def floor_bytes(total: int, rate_bps: float, hours_to_next_pass: float) -> int:
    """The floor for a volume of `total` bytes losing `rate_bps` bytes/s."""
    wanted = max(float(MIN_FLOOR), max(0.0, rate_bps) * hours_to_next_pass * 3600)
    return int(min(MAX_FRACTION * total, wanted))


def fill_rate(samples: List[Tuple[float, int]], now: float, free_now: int) -> Optional[float]:
    """Bytes/s lost since the newest sample at least MIN_WINDOW_S old; None before then."""
    old = [(ts, free) for ts, free in samples if now - ts >= MIN_WINDOW_S]
    if not old:
        return None
    ts, free = max(old)
    return max(0.0, (free - free_now) / (now - ts))


def _read(path: pathlib.Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: pathlib.Path, value: Dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, sort_keys=True)
    os.replace(tmp, path)


def device_of(path: str) -> str:
    return str(os.stat(path).st_dev)


def volume_usage(path: str) -> Any:
    return shutil.disk_usage(path)


def judge(home: str, store_dir: pathlib.Path, now: float, *, store_device: str,
          hours_to_next_pass: float = 1.0,
          usage: Optional[Callable[[str], Any]] = None,
          device: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """Judge the home volume for one VM admission and record the sample.

    state is `ok`, `below` (refuse the clone), `unread` (admit, report), or
    `same_device` (the store's own disk axis already judges this volume).
    """
    path = store_dir / STATE_FILE
    saved = _read(path)
    result: Dict[str, Any] = {"volume": "home", "path": home}
    usage = usage or volume_usage
    try:
        dev = (device or device_of)(home)
        if dev == str(store_device):
            return dict(result, state="same_device")
        disk = usage(home)
        total, free = int(disk.total), int(disk.free)
    except (OSError, ValueError) as exc:
        saved["unread_since"] = saved.get("unread_since") or now
        saved["unread_reason"] = f"{type(exc).__name__}: {exc}"
        _write(path, saved)
        return dict(result, state="unread", reason=saved["unread_reason"],
                    unread_since=saved["unread_since"])
    samples = [(float(ts), int(fr)) for ts, fr in saved.get("samples", [])
               if isinstance(ts, (int, float)) and now - float(ts) <= KEEP_S]
    rate = fill_rate(samples, now, free)
    floor = floor_bytes(total, rate or 0.0, hours_to_next_pass)
    if not samples or now - samples[-1][0] >= SAMPLE_EVERY_S:
        samples.append((now, free))
    below = free < floor
    saved.update(samples=samples, unread_since=None, unread_reason=None,
                 consecutive_denials=(int(saved.get("consecutive_denials") or 0) + 1) if below else 0,
                 last={"at": now, "free_bytes": free, "floor_bytes": floor,
                       "total_bytes": total, "fill_rate_bps": rate, "below": below})
    _write(path, saved)
    return dict(result, state="below" if below else "ok", free_bytes=free, floor_bytes=floor,
                total_bytes=total, fill_rate_bps=rate, device_id=dev,
                consecutive_denials=saved["consecutive_denials"])


def status(store_dir: pathlib.Path) -> Dict[str, Any]:
    """The recorded state for doctor; empty when no VM admission has judged it yet."""
    return _read(store_dir / STATE_FILE)
