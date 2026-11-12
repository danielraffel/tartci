#!/usr/bin/env python3
"""The TTL half of `tartci scratch-vm`: read the records, warn, stop and delete.

The watchdog imports this every pass, and the watchdog is reachable from a
Python 3.9 caller, so this half needs nothing beyond the standard library
(no tomllib, no store resolution). Creating a scratch VM, which resolves the
VM store, lives in scratch_vm.py. Only VMs with a record named `scratch-*`
under ~/.tartci/state/scratch-vms are ever touched.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

WARN_BEFORE_SECONDS = 30 * 60
NAME_RE = re.compile(r"^scratch-[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")

Runner = Callable[[list, dict], "subprocess.CompletedProcess[str]"]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def records_dir() -> Path:
    base = os.environ.get("TARTCI_HOME") or os.path.expanduser("~/.tartci")
    return Path(base) / "state" / "scratch-vms"


def _tart() -> str:
    return shutil.which("tart") or "/opt/homebrew/bin/tart"


def run_tart(argv: list, env_store: dict) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([_tart(), *argv], capture_output=True, text=True, timeout=600,
                          check=False, env={**os.environ, **env_store})


def read(name: str) -> Optional[dict]:
    try:
        value = json.loads((records_dir() / f"{name}.json").read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write(record: dict) -> None:
    path = records_dir() / f"{record['name']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def records() -> list[dict]:
    out = []
    for path in sorted(records_dir().glob("scratch-*.json")):
        value = read(path.stem)
        if value and NAME_RE.match(str(value.get("name") or "")):
            out.append(value)
    return out


def remove(record: dict, tart: Runner = run_tart) -> tuple[bool, str]:
    """Stop and delete the VM, then its record. False keeps the record for a retry."""
    env = {"TART_HOME": record["store"]}
    tart(["stop", record["name"]], env)
    gone = tart(["delete", record["name"]], env)
    text = (gone.stderr or gone.stdout).strip()
    if gone.returncode != 0 and "does not exist" not in text.lower():
        return False, f"tart delete {record['name']} failed: {text[:200]}"
    (records_dir() / f"{record['name']}.json").unlink(missing_ok=True)
    return True, f"deleted scratch VM {record['name']} (owner {record['owner']})"


def enforce(now: float, tart: Runner = run_tart, dry_run: bool = False) -> list[str]:
    """Warn at TTL-30m, then stop and delete at the TTL. One line per action.
    `dry_run` prints what it would do and changes nothing."""
    lines = []
    for record in records():
        expires = float(record.get("expires_at") or 0)
        if dry_run:
            if now >= expires:
                lines.append(f"would stop and delete scratch VM {record['name']} "
                             f"(owner {record['owner']}, TTL ended {_iso(expires)})")
            elif now >= expires - WARN_BEFORE_SECONDS:
                lines.append(f"would warn: scratch VM {record['name']} (owner "
                             f"{record['owner']}) is deleted at {_iso(expires)}")
            continue
        if now >= expires:
            ok, text = remove(record, tart)
            lines.append(f"{_iso(now)} launchd-watchdog: {'' if ok else 'WARN '}scratch-vm: "
                         f"{text} (TTL ended {_iso(expires)})")
        elif now >= expires - WARN_BEFORE_SECONDS and not record.get("warned_at"):
            record["warned_at"] = now
            write(record)
            lines.append(f"{_iso(now)} launchd-watchdog: WARN scratch-vm: {record['name']} "
                         f"(owner {record['owner']}) is stopped and deleted at {_iso(expires)}; "
                         f"keep it with `tartci scratch-vm extend {record['name']} --ttl <hours>`")
    return lines
