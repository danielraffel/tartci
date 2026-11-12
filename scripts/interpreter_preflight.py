#!/usr/bin/env python3
"""Prove a NEW lane interpreter can use the VM store before lanes run it.

Usage: interpreter_preflight.py PROFILE NEW_PYTHON

macOS grants (Full Disk Access, Files & Folders > Removable Volumes, Local
Network) belong to the binary. A Homebrew Python.app is its own responsible
process, so switching the lane interpreter to a binary that was never granted
makes every lane's first touch of an external-volume VM store hang on a
consent nobody answers (m5s, 2026-10-15, where the store is on
/Volumes/Store). The installer runs this before it changes the interpreter:
the new binary, started by launchd as the lanes will be, writes, reads back
and removes one file in the profile's tart_home within TIMEOUT_SECONDS. Any
other outcome keeps the previous interpreter.

Exit 0 when the new binary passed; otherwise exit 1 and one line on stderr
naming the binary, the store and the grant to check.
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LABEL = "com.danielraffel.tartci.interpreter-preflight"
TIMEOUT_SECONDS = 60.0
# Runs as the new interpreter: one file written, read back and removed.
PROBE = (
    "import os, sys, uuid\n"
    "path = os.path.join(sys.argv[1], '.tartci-interpreter-preflight-' + uuid.uuid4().hex)\n"
    "data = b'tartci-interpreter-preflight\\n'\n"
    "with open(path, 'wb') as handle:\n"
    "    handle.write(data)\n"
    "with open(path, 'rb') as handle:\n"
    "    ok = handle.read() == data\n"
    "os.remove(path)\n"
    "sys.exit(0 if ok else 74)\n"
)


def grant_hint(store: str, python: str) -> str:
    where = ("Full Disk Access, or Files & Folders > Removable Volumes"
             if store.startswith("/Volumes/") else "Full Disk Access")
    return (f"grant {where} to {python} (System Settings > Privacy & Security), "
            "then reinstall")


def load_profile(path: Path) -> dict:
    import tomllib  # noqa: PLC0415 - runs under the previous (3.11+) interpreter
    with path.open("rb") as handle:
        return tomllib.load(handle)


def launchctl(*args: str, timeout: float = 5) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True,
                          check=False, timeout=timeout)


def run(profile: dict, python: str, timeout: float = TIMEOUT_SECONDS,
        label: str = LABEL) -> str | None:
    """None when the interpreter passed, else the reason it did not. `label`
    names the one-shot launchd job; separate callers use separate labels."""
    host = profile.get("host") or {}
    store = str(host.get("tart_home") or "")
    if not store:
        return "the profile has no host.tart_home to prove"
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{label}"
    if launchctl("print", target).returncode == 0:
        launchctl("bootout", target)
    with tempfile.TemporaryDirectory(prefix="tartci-interpreter-preflight.") as td:
        plist = Path(td) / f"{label}.plist"
        log = Path(td) / "probe.log"
        plist.write_bytes(plistlib.dumps({
            "Label": label,
            "ProgramArguments": [python, "-c", PROBE, store],
            "RunAtLoad": True,
            "ProcessType": "Standard",
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }))
        if launchctl("bootstrap", domain, str(plist)).returncode != 0:
            return f"could not start a launchd job for {python}"
        try:
            launchctl("kickstart", target)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                shown = launchctl("print", target)
                found = re.search(r"^\s*last exit code = (-?\d+)", shown.stdout, re.M)
                if found:
                    code = int(found.group(1))
                    if code == 0:
                        return None
                    tail = log.read_text(errors="replace").strip().splitlines()[-1:] \
                        if log.exists() else []
                    return (f"{python} could not use {store} from launchd (exit {code}"
                            + (f": {tail[0][:200]}" if tail else "") + f"); "
                            + grant_hint(store, python))
                time.sleep(0.2)
            return (f"{python} did not finish a write to {store} within {timeout:g}s from "
                    f"launchd, which a missing consent does; " + grant_hint(store, python))
        finally:
            launchctl("bootout", target)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.splitlines()[2], file=sys.stderr)
        return 2
    reason = run(load_profile(Path(argv[1])), argv[2])
    if reason:
        print(f"interpreter preflight FAILED: {reason}", file=sys.stderr)
        return 1
    print(f"interpreter preflight: {argv[2]} can use the VM store from launchd")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
