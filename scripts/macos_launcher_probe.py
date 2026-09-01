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


def fail(message: str) -> None:
    raise ValueError(message)


def run(helper: dict, profile: dict, timeout_seconds: float = 10.0) -> dict:
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
            "ProcessType": "Background",
            "StandardOutPath": str(log_root / "launcher-volume-probe.log"),
            "StandardErrorPath": str(log_root / "launcher-volume-probe.log"),
        }, sort_keys=False))
        bootstrapped = False
        outcome: dict | None = None
        probe_error: Exception | None = None
        cleanup_error: str | None = None
        try:
            result = subprocess.run(
                ["launchctl", "bootstrap", domain, str(plist_path)],
                text=True, capture_output=True, check=False, timeout=5,
            )
            if result.returncode != 0:
                fail("launch helper volume probe could not bootstrap")
            bootstrapped = True
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
                fail("launch helper volume probe timed out")
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
