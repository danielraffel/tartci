#!/usr/bin/env python3
"""Emit host-local tartci status for fleet planners."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import leases


ROOT = Path(__file__).resolve().parents[1]


def run(argv: list[str], timeout: int = 10) -> dict[str, Any]:
    try:
        proc = subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except FileNotFoundError:
        return {"ok": False, "returncode": 127, "stdout": "", "stderr": f"{argv[0]} not found"}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "returncode": 124, "stdout": exc.stdout or "", "stderr": f"timeout after {timeout}s"}


def run_json(argv: list[str], timeout: int = 10) -> Any:
    result = run(argv, timeout=timeout)
    if not result["ok"]:
        return {"error": result["stderr"].strip(), "returncode": result["returncode"]}
    try:
        return json.loads(result["stdout"] or "null")
    except json.JSONDecodeError as exc:
        return {"error": str(exc), "raw": result["stdout"]}


def command_presence(names: list[str]) -> dict[str, str | None]:
    return {name: shutil.which(name) for name in names}


def tart_vms() -> Any:
    if not shutil.which("tart"):
        return {"error": "tart not found"}
    return run_json(["tart", "list", "--format", "json"], timeout=20)


def qemu_processes() -> list[dict[str, str]]:
    result = run(["pgrep", "-af", "qemu-system-aarch64"], timeout=5)
    if not result["ok"]:
        return []
    rows = []
    for line in result["stdout"].splitlines():
        parts = line.split(maxsplit=1)
        if parts:
            rows.append({"pid": parts[0], "command": parts[1] if len(parts) > 1 else ""})
    return rows


def profile_names() -> list[str]:
    profile_dir = ROOT / "profiles"
    return sorted(path.stem for path in profile_dir.glob("*.toml"))


def lease_status() -> dict[str, Any]:
    try:
        return leases.status_digest()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci status")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    home = Path(os.environ.get("TARTCI_HOME", Path.home() / ".tartci"))
    data = {
        "schema": 1,
        "ts": int(time.time()),
        "host": {
            "hostname": platform.node(),
            "machine": platform.machine(),
            "system": platform.system(),
            "release": platform.release(),
        },
        "paths": {
            "root": str(ROOT),
            "tartci_home": str(home),
            "goldens": os.environ.get("TARTCI_GOLDENS", str(home / "goldens")),
            "windows": os.environ.get("TARTCI_WIN", str(home / "windows")),
        },
        "commands": command_presence(["tart", "qemu-system-aarch64", "qemu-img", "gh", "git", "jq", "ssh"]),
        "profiles": profile_names(),
        "providers": {
            "tart": {"vms": tart_vms()},
            "qemu_windows": {"processes": qemu_processes()},
        },
        "leases": lease_status(),
        "notes": [
            "status is host-local and does not acquire provider capacity",
            "lease status may take the host lease lock and reap dead-owner records",
            "fleet-aware placement should be resolved by Shipyard using all host statuses",
        ],
    }

    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(f"host: {data['host']['hostname']} ({data['host']['machine']})")
        print(f"profiles: {', '.join(data['profiles']) or '-'}")
        qemu_count = len(data["providers"]["qemu_windows"]["processes"])
        print(f"qemu-windows processes: {qemu_count}")
        lease_capacity = (data.get("leases") or {}).get("capacity") or {}
        if lease_capacity:
            print(
                "leases: "
                f"{lease_capacity.get('used_cores', 0)}/"
                f"{lease_capacity.get('total_cores', '?')} cores used"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
