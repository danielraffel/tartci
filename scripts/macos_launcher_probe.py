#!/usr/bin/env python3
"""Bounded launchd-context access proof for the signed macOS fleet launcher."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import tempfile
import time
from pathlib import Path


# The probe writes, reads back and removes one small file on the VM store
# (tart_home). On m3 on 2026-09-29 that volume stalled for hours (the hourly
# reclaim scan of it took 70 and 204 min instead of 1-2), the probe's write
# outlived a 10 s deadline, launchd's bootout interrupted it (EINTR in mktemp),
# and every `pool on` from 08:40Z to 17:05Z was refused. An access DENIAL fails
# at once with an exit code; only a slow volume runs into the deadline, so a
# longer deadline cannot turn a denial into a pass.
DEFAULT_TIMEOUT_SECONDS = 60.0
TIMEOUT_ENV = "TARTCI_LAUNCH_HELPER_PROBE_TIMEOUT_SECS"
# One small file operation: it must not queue behind the host's own heavy I/O
# as a throttled Background job would (macOS throttles Background disk I/O
# whenever other I/O is in flight).
PROCESS_TYPE = "Standard"


def fail(message: str) -> None:
    raise ValueError(message)


def timeout_from_env(default: float = DEFAULT_TIMEOUT_SECONDS) -> float:
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or not 10 <= int(raw) <= 300:
        fail(f"invalid {TIMEOUT_ENV}: expected 10-300")
    return float(raw)


def run(helper: dict, profile: dict, timeout_seconds: float | None = None) -> dict:
    if timeout_seconds is None:
        timeout_seconds = timeout_from_env()
    host = profile["host"]
    label = "com.danielraffel.tartci.launcher-volume-probe"
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{label}"
    initial = subprocess.run(
        ["launchctl", "print", target], text=True, capture_output=True,
        check=False, timeout=5,
    )
    if initial.returncode == 0:
        fail("launch helper probe refused a pre-existing launchd job")
    if "Could not find service" not in initial.stderr \
            and "service not found" not in initial.stderr:
        fail("launch helper probe could not prove its launchd label absent")
    log_root = Path(host["log_root"])
    log_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tartci-launch-probe.") as td:
        plist_path = Path(td) / f"{label}.plist"
        plist_path.write_bytes(plistlib.dumps({
            "Label": label,
            "ProgramArguments": [
                f"{helper['path']}/Contents/MacOS/tartci-launcher",
                "--probe-store",
            ],
            "WorkingDirectory": host["home"],
            "RunAtLoad": True,
            "ProcessType": PROCESS_TYPE,
            "StandardOutPath": str(log_root / "launcher-volume-probe.log"),
            "StandardErrorPath": str(log_root / "launcher-volume-probe.log"),
        }, sort_keys=False))
        bootstrapped = False
        outcome: dict | None = None
        probe_error: Exception | None = None
        cleanup_error: str | None = None
        try:
            probe_started = time.monotonic()
            result = subprocess.run(
                ["launchctl", "bootstrap", domain, str(plist_path)],
                text=True, capture_output=True, check=False, timeout=5,
            )
            if result.returncode != 0:
                fail("launch helper volume probe could not bootstrap")
            bootstrapped = True
            # A RunAtLoad launch is speculative and launchd can defer it
            # indefinitely on a busy host; kickstart makes it on-demand.
            subprocess.run(["launchctl", "kickstart", target], text=True,
                           capture_output=True, check=False, timeout=5)
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                result = subprocess.run(
                    ["launchctl", "print", target], text=True,
                    capture_output=True, check=False, timeout=5,
                )
                if result.returncode == 0:
                    match = re.search(
                        r"^\s*last exit code = (-?[0-9]+)\s*$",
                        result.stdout, re.MULTILINE,
                    )
                    if match is not None:
                        code = int(match.group(1))
                        if code == 0:
                            outcome = {
                                "schema": 1, "required": True, "passed": True,
                                "path": host["tart_home"],
                                "launcher_sha256": helper["sha256"],
                                "designated_requirement_sha256": helper[
                                    "designated_requirement_sha256"
                                ],
                                "verified_at_unix": int(time.time()),
                            }
                            break
                        fail(f"launch helper volume probe exited {code}")
                if outcome is not None:
                    break
                time.sleep(0.1)
            if outcome is None:
                wait_elapsed = time.monotonic() - probe_started
                fail(f"launch helper volume probe timed out after {timeout_seconds:g}s "
                     f"(probe wait elapsed {wait_elapsed:.3f}s; its write to {host['tart_home']} "
                     "did not finish: volume I/O slow, not an access denial)")
        except Exception as error:  # Preserve the primary probe diagnosis.
            probe_error = error
        finally:
            if bootstrapped:
                cleanup = subprocess.run(
                    ["launchctl", "bootout", target], text=True,
                    capture_output=True, check=False, timeout=5,
                )
                if cleanup.returncode != 0:
                    cleanup_error = "launch helper volume probe could not remove its launchd job"
        if probe_error is not None:
            if cleanup_error is not None:
                raise ValueError(f"{probe_error}; {cleanup_error}") from probe_error
            raise probe_error
        if cleanup_error is not None:
            fail(cleanup_error)
        if outcome is None:
            fail("launch helper volume probe produced no outcome")
        return outcome
