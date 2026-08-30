#!/usr/bin/env python3
"""Best-effort, per-runner disk admission receipt observer.

This is deliberately not part of lease authority.  It observes one completed
acquisition attempt and atomically replaces one exact runner's receipt.  Any
I/O or decoding failure is reported only through this process's exit status;
callers must preserve the already-decided lease status.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import signal
import sys
import tempfile
from typing import Any


MAX_INPUT_BYTES = 1024 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def _identity(value: str, field: str) -> str:
    if not value or not SAFE_ID.fullmatch(value):
        raise ValueError(f"invalid {field}: {value!r}")
    return value


def _read_attempt(args: argparse.Namespace) -> dict[str, Any]:
    if args.reason:
        attempt: dict[str, Any] = {"ok": False, "reason": args.reason, "disk_path": args.disk_path}
        return attempt
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("lease attempt JSON exceeds observer input limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("lease attempt JSON must be an object")
    return value


def _receipt(attempt: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    reason = str(attempt.get("reason") or "")
    disk = attempt.get("disk") if isinstance(attempt.get("disk"), dict) else {}
    status = "resolved"
    receipt_reason = "lease_acquired" if attempt.get("ok") is True else "non_disk_denial"
    exceeded_axis = attempt.get("exceeded_axis")
    denied_reasons = {
        "disk_growth_misconfigured",
        "legacy_vm_disk_accounting_unknown",
        "disk_device_changed_with_live_reservations",
    }
    if isinstance(exceeded_axis, dict) and exceeded_axis.get("disk") is True:
        status = "denied"
        receipt_reason = "disk_capacity_insufficient"
    elif reason in {"disk_root_unavailable", "disk_probe_failed"}:
        status = "denied"
        receipt_reason = "disk_probe_failed"
    elif reason == "disk_floor_misconfigured":
        status = "denied"
        receipt_reason = reason
    elif reason in denied_reasons:
        status = "denied"
        receipt_reason = reason

    return {
        "schema_version": 1,
        "kind": "tartci.disk-admission",
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        "reason": receipt_reason,
        "host": args.host,
        "provider": args.provider,
        "lane": args.lane,
        "runner": args.runner,
        "lease_reason": reason,
        "probe_path": disk.get("reservation_path") or disk.get("probe_path") or attempt.get("disk_path"),
        "device_id": disk.get("device_id"),
        "free_bytes": disk.get("free_bytes"),
        "reserved_bytes": disk.get("reserved_bytes"),
        "available_after_reservations_bytes": disk.get("available_after_reservations_bytes"),
        "floor_bytes": disk.get("floor_bytes", attempt.get("disk_floor_bytes")),
        "required_bytes": disk.get("required_bytes"),
        "required_after_reservations_bytes": (
            disk.get("floor_bytes", 0) + disk.get("requested_bytes", 0)
            if isinstance(disk.get("floor_bytes"), int) and isinstance(disk.get("requested_bytes"), int)
            else None
        ),
        "requested_growth_bytes": disk.get("requested_bytes"),
        "error": attempt.get("error") if receipt_reason == "disk_probe_failed" else None,
    }


def _atomic_write(directory: pathlib.Path, runner: str, body: dict[str, Any]) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / f"{runner}.disk-admission.json"
    fd, temporary = tempfile.mkstemp(prefix=f".{runner}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt-dir", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--runner", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--reason", default="")
    parser.add_argument("--disk-path", default="")
    parser.add_argument("--timeout-seconds", type=float, default=2.0)
    args = parser.parse_args()
    try:
        if not 0 < args.timeout_seconds <= 10:
            raise ValueError("timeout must be greater than zero and at most 10 seconds")
        def deadline(_signum: int, _frame: object) -> None:
            raise TimeoutError("receipt publication deadline exceeded")

        signal.signal(signal.SIGALRM, deadline)
        signal.setitimer(signal.ITIMER_REAL, args.timeout_seconds)
        args.provider = _identity(args.provider, "provider")
        args.lane = _identity(args.lane, "lane")
        args.runner = _identity(args.runner, "runner")
        args.host = _identity(args.host, "host")
        attempt = _read_attempt(args)  # Parse the authoritative result exactly once.
        _atomic_write(pathlib.Path(args.receipt_dir).expanduser(), args.runner, _receipt(attempt, args))
    except (OSError, ValueError, TypeError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"disk receipt observer failed: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
