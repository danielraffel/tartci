#!/usr/bin/env python3
"""Emit host-local tartci status for fleet planners."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import plistlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import disk_reclaim
import leases


ROOT = Path(__file__).resolve().parents[1]
RECLAIM_LABEL = "com.danielraffel.tartci.reclaim"
REAP_LABEL = "com.danielraffel.tartci.reap"
# Two janitors, two different failures. The disk reclaimer removes idle build
# directories; the VM janitor removes stale VMs and overlays. A host can carry
# either, both, or neither, and until now nothing said which: m1 and m5 have no
# VM janitor at all. Reporting one of the two would leave the other silent.
JANITORS = (
    ("reclaim", RECLAIM_LABEL, "TARTCI_RECLAIM_", "disk reclaimer"),
    ("reap", REAP_LABEL, "TARTCI_REAP_", "VM janitor"),
)
GIB = 1024 ** 3


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


def janitor_agent(label: str = RECLAIM_LABEL,
                  settings_prefix: str = "TARTCI_RECLAIM_",
                  plist_path: Path | None = None) -> dict[str, Any]:
    """What one janitor is on THIS host, read from its installed plist.

    Read from the plist rather than this process's environment, because status
    is normally run from an interactive shell and launchd's job carries its
    own. The failure this exists to surface is a host that never got the agent,
    or got an older generation of it: m1 and m5 carry no VM janitor at all, and
    nothing in `tartci status` said so. The settings prefix is a parameter
    because each janitor carries its own (`TARTCI_RECLAIM_*`, `TARTCI_REAP_*`)
    and reporting one janitor's knobs under another's name would be worse than
    reporting none.

    `loaded` is deliberately three-valued. False means launchd was asked and
    does not hold the job; None means the question could not be put (no
    launchctl, as on the Linux CI host). Collapsing those would let an
    unanswerable question read as a definite "not running".
    """
    path = (Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            if plist_path is None else plist_path)
    out: dict[str, Any] = {"label": label, "plist": str(path),
                           "installed": path.is_file(), "loaded": None}
    probe = run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], timeout=5)
    if probe["returncode"] != 127:
        out["loaded"] = probe["ok"]
    if not out["installed"]:
        return out
    try:
        with path.open("rb") as handle:
            job = plistlib.load(handle)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"unreadable plist: {exc}"
        return out
    env = job.get("EnvironmentVariables") or {}
    out["start_interval_s"] = job.get("StartInterval")
    out["log_path"] = job.get("StandardOutPath")
    out["settings"] = {key: value for key, value in sorted(env.items())
                       if key.startswith(settings_prefix)}
    # The log is what the agent last wrote, so its mtime is when it last ran.
    # Absent is unknown rather than never: a log can be cleared by hand, and a
    # rotation moves the old generation aside without stopping the clock.
    out["last_pass_ts"] = None
    if out["log_path"]:
        try:
            out["last_pass_ts"] = int(Path(out["log_path"]).stat().st_mtime)
        except OSError:
            pass
    return out


def disk_space(settings: dict[str, Any] | None = None,
               timeout: float = 5.0) -> dict[str, Any]:
    """Free space on the volumes the reclaim janitor actually scans.

    Root discovery is disk_reclaim's own, not a second list, because a status
    that measures a volume the janitor never scans is the one fault nothing
    downstream can catch. That is not hypothetical: a `$HOME/Code` root
    reported a clean pass forever on a host that keeps its code on Workshop,
    while Workshop filled.

    For the same reason the installed plist's roots win over this shell's
    environment when the two disagree -- the janitor runs under the plist.

    Bounded, like every other outbound call here. Root discovery resolves the
    paths and then stats the volume each one sits on, and on this fleet one of
    those is external. A wedged or stale mount blocks those calls in the
    kernel with no timeout of their own, which would hang `tartci status`
    exactly during the incident someone runs it to understand. The worker is
    abandoned rather than joined on timeout, because a thread parked in an
    uninterruptible stat cannot be recalled -- only stopped waiting for.
    """
    declared = (settings or {}).get("TARTCI_RECLAIM_ROOTS") \
        or os.environ.get("TARTCI_RECLAIM_ROOTS")

    def measure() -> tuple[list[Any], dict[str, Any]]:
        found = disk_reclaim.parse_roots(declared)
        return found, disk_reclaim.volumes_free_bytes(found)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        roots, volumes = executor.submit(measure).result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        return {"error": f"volume scan did not answer within {timeout}s; "
                         "a scan root may be on a wedged mount"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    finally:
        executor.shutdown(wait=False)
    return {
        "roots": [str(root) for root in roots],
        "roots_declared": bool(declared),
        "volumes": volumes,
        "tightest_free_bytes": disk_reclaim.tightest_free_bytes(volumes),
        "tightest_root": disk_reclaim.tightest_volume_root(volumes),
    }


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
    janitors = {name: janitor_agent(label, prefix)
                for name, label, prefix, _ in JANITORS}
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
        "janitors": janitors,
        "disk": disk_space(janitors["reclaim"].get("settings")),
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
        disk = data["disk"]
        if disk.get("error"):
            print(f"disk: unavailable ({disk['error']})")
        elif not disk["roots"]:
            print("disk: no scan roots on this host")
        else:
            for volume in disk["volumes"]:
                free = volume["free_bytes"]
                figure = "unknown" if free is None else f"{free / GIB:.1f} GiB free"
                print(f"disk: {volume['root']}  {figure}")
        for name, _label, _prefix, description in JANITORS:
            agent = data["janitors"][name]
            if not agent["installed"]:
                state = "NOT INSTALLED"
            elif agent["loaded"] is None:
                state = "installed, launchd not reachable from here"
            else:
                state = "installed and loaded" if agent["loaded"] else \
                    "installed but NOT loaded"
            last = agent.get("last_pass_ts")
            when = "never observed" if last is None else \
                f"last pass {(time.time() - last) / 3600:.1f}h ago"
            print(f"{description}: {state}; {when}")
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
