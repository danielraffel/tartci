#!/usr/bin/env python3
"""Bounded Tart store I/O health probe and fail-closed state machine."""

from __future__ import annotations

import json
import os
import pathlib
import argparse
import sys
import time
from typing import Any

from bounded_subprocess import ObservationError, require_success, run_bounded
from python_child import child_python


DEFAULT_TIMEOUT_S = 10.0
DEFAULT_FAILURE_THRESHOLD = 2

_PROFILE_TART_HOME_CODE = (
    "import sys,tomllib; "
    "p=tomllib.load(open(sys.argv[1],'rb')); "
    "h=p.get('host',{}); v=h.get('tart_home') if isinstance(h,dict) else None; "
    "print(v.strip() if isinstance(v,str) and v.strip() else '')"
)


def _probe_body(root: str) -> dict[str, Any]:
    """Inspect one directory without creating or removing anything.

    This function runs in a private child process.  The parent kills that
    process group when an APFS/USB bridge call stops completing, so a blocked
    ``open`` cannot strand the watchdog.  Keep this operation deliberately
    metadata-only: an admission probe must never write, fsync, unlink, or
    recursively enumerate a potentially wedged external volume.
    """
    path = pathlib.Path(root).expanduser()
    started = time.monotonic()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path), flags)
    try:
        stat = os.fstat(fd)
    finally:
        os.close(fd)
    return {
        "ok": True,
        "path": str(path),
        "device_id": str(stat.st_dev),
        "latency_ms": round((time.monotonic() - started) * 1000, 3),
        "checked_at": int(time.time()),
    }


def probe(root: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Run the metadata probe behind a process-group deadline."""
    if timeout_s <= 0:
        raise ValueError("timeout must be positive")
    try:
        completed = run_bounded(
            [child_python(), __file__, "--probe-child", root],
            timeout=timeout_s,
            operation="tart_store_probe",
        )
        require_success(completed, operation="tart_store_probe")
    except ObservationError as exc:
        if exc.kind == "timeout":
            reason = f"timeout after {timeout_s:g}s"
        else:
            reason = f"{exc.kind}: {exc.detail}"
        return {"ok": False, "reason": reason, "path": root,
                "kind": exc.kind}
    except Exception as exc:  # noqa: BLE001 - unknown is fail-closed
        return {"ok": False, "reason": f"probe unavailable: {exc}",
                "path": root, "kind": "internal_error"}
    try:
        value = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "reason": f"invalid probe output: {exc}",
                "path": root, "kind": "invalid_output"}
    if not isinstance(value, dict) or value.get("ok") is not True:
        return {"ok": False, "reason": "invalid probe result", "path": root,
                "kind": "invalid_output"}
    return value


def profile_tart_home(profile_path: str, timeout_s: float = 2.0) -> str | None:
    """Read only the declared Tart path in a killable child process."""
    try:
        completed = run_bounded(
            [child_python(), "-c", _PROFILE_TART_HOME_CODE, profile_path],
            timeout=timeout_s,
            operation="tart_profile_read",
        )
        require_success(completed, operation="tart_profile_read")
    except (ObservationError, OSError, ValueError):
        return None
    value = completed.stdout.strip()
    return value or None


def next_state(previous: dict[str, Any], result: dict[str, Any],
               threshold: int = DEFAULT_FAILURE_THRESHOLD) -> dict[str, Any]:
    """Pure healthy/suspect/draining transition for deterministic tests."""
    prior_result = previous.get("last_result")
    if (result.get("ok") and isinstance(prior_result, dict)
            and prior_result.get("device_id")
            and result.get("device_id")
            and prior_result["device_id"] != result["device_id"]):
        result = {
            "ok": False,
            "reason": (f"device identity changed: {prior_result['device_id']} -> "
                       f"{result['device_id']}"),
            "path": result.get("path"),
            "device_id": result.get("device_id"),
        }
    failures = int(previous.get("consecutive_failures", 0) or 0)
    if result.get("ok"):
        return {"state": "healthy", "consecutive_failures": 0,
                "last_result": result, "drain_required": False}
    failures += 1
    state = "draining" if failures >= threshold else "suspect"
    return {"state": state, "consecutive_failures": failures,
            "last_result": result, "drain_required": state == "draining"}


def load(path: str) -> dict[str, Any]:
    try:
        with open(path) as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path: str, state: dict[str, Any]) -> None:
    destination = pathlib.Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with open(temporary, "w") as handle:
        json.dump(state, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-child", metavar="PATH", help=argparse.SUPPRESS)
    parser.add_argument("--path", help="declared Tart store directory")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    args = parser.parse_args(argv)
    if args.probe_child is not None:
        # This mode is intentionally tiny and side-effect free.  The parent
        # process owns the deadline and terminates the complete process group if
        # open/fstat/close blocks in a failing mount path.
        print(json.dumps(_probe_body(args.probe_child), sort_keys=True))
        return 0
    if not args.path:
        parser.error("--path is required")
    result = probe(args.path, timeout_s=args.timeout_seconds)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 75


if __name__ == "__main__":
    raise SystemExit(main())
