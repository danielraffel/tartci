#!/usr/bin/env python3
"""Bounded Tart inventory queries for macOS runner admission."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from typing import Any

from bounded_subprocess import ObservationError, require_success, run_bounded as run_observation


def run_bounded(command: list[str], deadline: float) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(command, 0)
    result = run_observation(
        command,
        timeout=remaining,
        operation="tart_inventory",
    )
    return require_success(result, operation="tart_inventory").stdout


def count_running_macos(timeout_seconds: float, tart: str = "tart") -> int:
    deadline = time.monotonic() + timeout_seconds
    payload: Any = json.loads(run_bounded([tart, "list", "--format", "json"], deadline))
    if not isinstance(payload, list):
        raise ValueError("tart list did not return an array")

    count = 0
    for vm in payload:
        if not isinstance(vm, dict):
            raise ValueError("tart list contains a non-object entry")
        if not str(vm.get("State", vm.get("state", ""))).lower().startswith("run"):
            continue
        name = vm.get("Name") or vm.get("name")
        os_name = str(vm.get("OS", vm.get("os", ""))).lower()
        if name and not os_name:
            try:
                detail = json.loads(
                    run_bounded([tart, "get", str(name), "--format", "json"], deadline)
                )
                if isinstance(detail, dict):
                    os_name = str(detail.get("OS", "")).lower()
            except ObservationError as exc:
                if exc.kind in ("timeout", "descendant_leak"):
                    raise
                os_name = ""
            except subprocess.TimeoutExpired:
                raise
            except (json.JSONDecodeError, OSError, ValueError):
                os_name = ""
        # Missing OS is conservative: count it against the macOS cap.
        if os_name in ("", "darwin", "macos"):
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--tart", default="tart")
    args = parser.parse_args()
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    try:
        print(count_running_macos(args.timeout_seconds, args.tart))
    except (json.JSONDecodeError, ObservationError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"tart inventory failed: {exc}", file=sys.stderr)
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
