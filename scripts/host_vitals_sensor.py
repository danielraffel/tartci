#!/usr/bin/env python3
"""Keep Pulp's host-vitals sensor on each host the same as Pulp's origin/main.

Pulp's installer (tools/scripts/install_host_vitals_sensor.sh) COPIES
host_vitals.sh and host_vitals_sensor.sh into ~/.local/bin, so a change to
the sensor never reaches a host unless someone reinstalls it. On 2026-09-29
m1, m3 and m5 all ran the 2026-09-25 copy, which predates the fseventsd
reading, so `pool status` read `fseventsd: UNKNOWN` everywhere and the
monitor for m5's runaway fseventsd was blind.

The hourly reclaim pass already materializes Pulp's origin/main tools/scripts
(scripts/pulp_reapers.py). refresh() compares the installed copies with that
checkout by SHA-256 and, in fix mode, re-runs Pulp's own installer from it
when they differ, and installs it on a fleet host that has none (m5studio
joined the fleet without it). The sensor is observation-only, so installing
it never touches a runner. drift() is the read-only half, for `pool status`.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

SENSOR_FILES = ("host_vitals.sh", "host_vitals_sensor.sh")
INSTALLER = "install_host_vitals_sensor.sh"
LABEL = "com.pulp.host-vitals"
INSTALL_TIMEOUT_S = 120

Runner = Callable[..., subprocess.CompletedProcess]


def default_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def default_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def default_source_dir() -> Path:
    """The reclaim pass's origin/main checkout of Pulp's tools/scripts."""
    home = os.environ.get("TARTCI_HOME", str(Path.home() / ".tartci"))
    return Path(home).expanduser() / "state" / "reclaim" / "pulp-reapers" / "tools" / "scripts"


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def drift(source_dir: Path | None = None, bin_dir: Path | None = None,
          plist: Path | None = None) -> dict[str, Any]:
    """Installed sensor vs Pulp origin/main. Read-only.

    state: current | drift | not_installed | source_missing | not_applicable
    """
    source_dir = source_dir or default_source_dir()
    bin_dir = bin_dir or default_bin_dir()
    plist = plist or default_plist()
    if not source_dir.is_dir():
        # This host does not run the reclaim pass that keeps the sensor current
        # (no fleet profile, or not a macOS fleet host): nothing to compare.
        return {"state": "not_applicable", "detail": f"no {source_dir}"}
    if not plist.exists():
        return {"state": "not_installed", "detail": f"no {plist}"}
    differing, missing_source = [], []
    for name in SENSOR_FILES:
        want = _sha(source_dir / name)
        if want is None:
            missing_source.append(name)
            continue
        if _sha(bin_dir / name) != want:
            differing.append(name)
    if missing_source:
        return {"state": "source_missing",
                "detail": f"{', '.join(missing_source)} not in {source_dir}"}
    if differing:
        return {"state": "drift", "files": differing,
                "detail": f"{', '.join(differing)} in {bin_dir} differ from Pulp origin/main"}
    return {"state": "current"}


def refresh(source_dir: Path, fix: bool, runner: Runner = subprocess.run,
            bin_dir: Path | None = None, plist: Path | None = None) -> dict[str, Any]:
    """Reinstall the sensor from `source_dir` when it drifted. Never raises."""
    before = drift(source_dir, bin_dir, plist)
    if before["state"] not in ("drift", "not_installed"):
        return before
    before.setdefault("files", list(SENSOR_FILES))
    installer = source_dir / INSTALLER
    if not installer.is_file():
        return {"state": "drift", "files": before["files"],
                "detail": f"{before['detail']}; no {INSTALLER} to reinstall with"}
    if not fix:
        return {"state": "would_refresh", "files": before["files"], "detail": before["detail"]}
    try:
        proc = runner(["/bin/bash", str(installer)], capture_output=True, text=True,
                      timeout=INSTALL_TIMEOUT_S, check=False, cwd=str(Path.home()))
        rc, text = proc.returncode, (proc.stderr or proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        rc, text = 1, str(exc)
    after = drift(source_dir, bin_dir, plist)
    if rc == 0 and after["state"] == "current":
        return {"state": "refreshed", "files": before["files"]}
    return {"state": "refresh_failed", "files": before["files"],
            "detail": f"installer exit {rc}: {text[-300:]}; now {after['state']}"}


def status_line(value: dict[str, Any] | None = None) -> str | None:
    """One `pool status` line when the installed sensor is not origin/main's."""
    value = value if value is not None else drift()
    if value.get("state") == "drift":
        return (f"host-vitals sensor: DRIFT ({value['detail']}; the hourly reclaim pass "
                "reinstalls it)")
    if value.get("state") == "not_installed":
        return ("host-vitals sensor: NOT INSTALLED (no fseventsd or memory reading on this "
                "host; the hourly reclaim pass installs it)")
    if value.get("state") == "source_missing":
        return f"host-vitals sensor: UNVERIFIED ({value['detail']})"
    return None
