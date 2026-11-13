#!/usr/bin/env python3
"""Bounded Tart inventory queries for macOS runner admission."""

from __future__ import annotations

import argparse
import json
import math
import os
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


def vm_is_present(name: str, timeout_seconds: float, tart: str = "tart") -> bool:
    """Whether a local Tart VM with exactly this name still exists.

    Only a successful, well-formed listing can say "absent". A timeout, a
    failed command or malformed output raises, so a caller never mistakes an
    inventory it could not read for proof that a VM is gone.
    """
    deadline = time.monotonic() + timeout_seconds
    payload: Any = json.loads(
        run_bounded([tart, "list", "--format", "json", "--source", "local"], deadline)
    )
    if not isinstance(payload, list):
        raise ValueError("tart list did not return an array")
    for vm in payload:
        if not isinstance(vm, dict):
            raise ValueError("tart list contains a non-object entry")
        if (vm.get("Name") or vm.get("name")) == name:
            return True
    return False


def vm_dir_absent(name: str, tart_home: str) -> bool | None:
    """Whether NAME's directory is gone from the Tart store at TART_HOME.

    Tart keeps each local VM in TART_HOME/vms/<name>, and `tart delete`
    removes that directory last, so its absence proves the VM is gone without
    asking `tart` (which is what times out on a loaded host). The store root is
    the control: it must be a readable directory with at least one entry (a
    real store always holds its goldens). An unreadable, missing or empty root
    returns None, so a wrong TART_HOME can never read as "absent".
    """
    if not name or "/" in name or name in (".", ".."):
        return None
    root = os.path.join(tart_home, "vms")
    try:
        with os.scandir(root) as entries:
            if next(entries, None) is None:
                return None
    except OSError:
        return None
    try:
        os.lstat(os.path.join(root, name))
    except FileNotFoundError:
        return True
    except OSError:
        return None
    return False


def prove_vm_absent(
    name: str,
    timeout_seconds: float,
    tart: str = "tart",
    tart_home: str | None = None,
    attempts: int = 1,
    backoff_seconds: float = 0.0,
    sleep=time.sleep,
) -> str:
    """Re-check NAME's absence with backoff; return absent, present or unreadable.

    One readable listing without NAME, or (when the listing cannot be read) a
    missing VM directory under a readable store, is proof. A listed VM, or a VM
    directory that still exists, is "present" for that attempt. Every attempt
    re-checks, because a load spike that times out one listing is usually gone
    a few seconds later. The verdict is that of the last attempt.
    """
    verdict = "unreadable"
    for attempt in range(max(1, attempts)):
        if attempt:
            sleep(backoff_seconds * (2 ** (attempt - 1)))
        try:
            listed = vm_is_present(name, timeout_seconds, tart)
        except (json.JSONDecodeError, ObservationError, OSError, subprocess.SubprocessError, ValueError) as exc:
            print(f"tart inventory failed: {exc}", file=sys.stderr)
            listed = None
        if listed is False:
            return "absent"
        if listed is None and tart_home:
            gone = vm_dir_absent(name, tart_home)
            if gone is True:
                return "absent"
            verdict = "present" if gone is False else "unreadable"
        else:
            verdict = "present" if listed else "unreadable"
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--tart", default="tart")
    parser.add_argument(
        "--vm-absent",
        metavar="NAME",
        help="exit 0 only when a readable local inventory proves NAME is gone; "
        "exit 1 when it is still listed, 75 when the inventory cannot be read",
    )
    parser.add_argument(
        "--tart-home",
        help="with --vm-absent: when the listing cannot be read, a missing "
        "TART_HOME/vms/NAME directory under a readable, non-empty store also proves absence",
    )
    parser.add_argument("--attempts", type=int, default=1,
                        help="with --vm-absent: how many times to re-check before giving up")
    parser.add_argument("--backoff-seconds", type=float, default=1.0,
                        help="with --vm-absent: first wait between re-checks; doubles each time")
    args = parser.parse_args()
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if not 1 <= args.attempts <= 10:
        parser.error("--attempts must be 1-10")
    if not math.isfinite(args.backoff_seconds) or not 0 <= args.backoff_seconds <= 30:
        parser.error("--backoff-seconds must be 0-30")
    if args.vm_absent is not None:
        if not args.vm_absent:
            parser.error("--vm-absent needs a VM name")
        verdict = prove_vm_absent(
            args.vm_absent, args.timeout_seconds, args.tart, args.tart_home,
            args.attempts, args.backoff_seconds,
        )
        print(verdict)
        return {"absent": 0, "present": 1}.get(verdict, 75)
    try:
        print(count_running_macos(args.timeout_seconds, args.tart))
    except (json.JSONDecodeError, ObservationError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"tart inventory failed: {exc}", file=sys.stderr)
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
