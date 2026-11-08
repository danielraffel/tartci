#!/usr/bin/env python3
"""How close this host's Tart store is to refusing the next VM for disk.

Why this exists: on 2026-11-08 m1 refused every VM lease
`disk_capacity_exceeded` for hours (27 GiB free against a 49 GiB per-VM need:
the 25 GiB floor plus 24 GiB growth) and nothing said so until the queue
backed up. After a manual reclaim it sat at about 56 GiB: one VM away from
the same refusal, and still "ok" on every check. This judges the store the
way a VM lease does (scripts/lease_disk.py: floor + live reservations + one
VM's growth) and reports:

  refusing  free space is below what one more VM needs: the next lease is
            refused for disk;
  low       free space admits one more VM, but not two: within one VM's
            growth of refusing;
  ok        more headroom than that.

Read-only: it reads the lease store without its lock, never reaps, and never
probes anything but statvfs on the store. Floor and growth come from the
installed lane plists' environment (the values the lanes lease with), else
the lease helper's defaults (25 GiB and 24 GiB).
"""

from __future__ import annotations

import os
import pathlib
import plistlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

GIB = 1024 ** 3
DEFAULT_FLOOR_GB = 25
DEFAULT_GROWTH_GB = 24
LANE_LABEL_PREFIX = "com.danielraffel.tartci.tart-runner-macos-fleet."


def _gb(env: dict[str, Any], *keys: str, default: int) -> int:
    for key in keys:
        raw = env.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = int(str(raw).strip())
        except ValueError:
            continue
        if value >= 0:
            return value
    return default


def lane_environment(agents_dir: pathlib.Path) -> dict[str, Any]:
    """The EnvironmentVariables of the first installed macOS lane plist that has them."""
    for path in sorted(agents_dir.glob(f"{LANE_LABEL_PREFIX}*.plist")):
        try:
            with path.open("rb") as handle:
                env = plistlib.load(handle).get("EnvironmentVariables") or {}
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        if isinstance(env, dict) and env:
            return env
    return {}


def judge(free_bytes: int, required_bytes: int, growth_bytes: int) -> str:
    """refusing | low | ok, from the lease's own required figure."""
    if free_bytes < required_bytes:
        return "refusing"
    if free_bytes < required_bytes + growth_bytes:
        return "low"
    return "ok"


def status(*, agents_dir: pathlib.Path, tart_home: str | None,
           store_dir: pathlib.Path | None = None) -> dict[str, Any]:
    """The store's headroom; {"state": "unknown", "error": ...} when it cannot be measured."""
    import lease_disk  # noqa: PLC0415 - sibling module
    import leases  # noqa: PLC0415 - sibling module

    env = lane_environment(agents_dir)
    home = env.get("TART_HOME") or tart_home
    if not home:
        return {"state": "not_applicable", "reason": "no Tart store declared"}
    floor_gb = _gb(env, "TARTCI_VM_DISK_FREE_FLOOR_GB", default=DEFAULT_FLOOR_GB)
    growth_gb = _gb(env, "TARTCI_MACOS_VM_DISK_GROWTH_GB", "TARTCI_VM_DISK_GROWTH_GB",
                    default=DEFAULT_GROWTH_GB)
    if floor_gb == 0 or growth_gb == 0:
        return {"state": "not_applicable", "reason": "the disk axis is disabled",
                "tart_home": home}
    try:
        probe = lease_disk.disk_probe(str(home))
    except Exception as exc:  # noqa: BLE001 - reported as unmeasured
        return {"state": "unknown", "error": f"{type(exc).__name__}: {exc}", "tart_home": home}
    records: list[dict[str, Any]] = []
    try:
        loaded = leases.load_records(store_dir or leases.default_store_dir())
        records = [r for r in loaded if leases.is_vm_kind(r.get("command_kind"))
                   and leases.owner_matches(r)]
    except Exception:  # noqa: BLE001 - no reservations read is the conservative low side
        records = []
    capacity = lease_disk.disk_capacity(records, probe, growth_gb * GIB, floor_gb * GIB)
    state = judge(capacity["free_bytes"], capacity["required_bytes"], growth_gb * GIB)
    return {"state": state, "tart_home": home, "floor_gb": floor_gb, "growth_gb": growth_gb,
            **{k: capacity[k] for k in ("free_bytes", "reserved_bytes", "required_bytes",
                                        "reservation_count", "device_id")}}


def detail(value: dict[str, Any]) -> str:
    free = int(value.get("free_bytes") or 0) / GIB
    need = int(value.get("required_bytes") or 0) / GIB
    return (f"{value.get('tart_home')}: {free:.0f} GiB free, the next VM needs {need:.0f} GiB "
            f"(floor {value.get('floor_gb')} + growth {value.get('growth_gb')} + "
            f"{int(value.get('reserved_bytes') or 0) / GIB:.0f} reserved by "
            f"{value.get('reservation_count')} live VM(s))")


if __name__ == "__main__":
    import json

    print(json.dumps(status(agents_dir=pathlib.Path.home() / "Library/LaunchAgents",
                            tart_home=os.environ.get("TART_HOME")), sort_keys=True))
