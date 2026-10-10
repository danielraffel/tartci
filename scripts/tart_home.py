#!/usr/bin/env python3
"""Which Tart store a tartci command reads, and why.

The gate lanes run with TART_HOME set by their LaunchAgent (on m1 and m5,
`/Users/<you>/VMs`). A shell over ssh has no TART_HOME, so `tart list` reads
Tart's default `~/.tart`, where the gate VMs are not. On 2026-10-09, over ssh
to m1, `tartci doctor --reap --json` reported 0 running VMs and 2 free slots
while two gate VMs ran; with TART_HOME set it reported 2 running and 0 free.
Shipyard's fleet health probe shells exactly that command.

Resolution order, the same one the launchd watchdog uses:
1. TART_HOME from the environment, when set;
2. `[host].tart_home` from the installed fleet profile
   (TARTCI_MACOS_FLEET_PROFILE, else ~/.config/tartci/macos-fleet-profile.toml);
3. Tart's default `~/.tart`, reported as such.

`--lines` prints the path and the source on two lines for the dispatcher.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # Apple's system Python 3.9
    tomllib = None  # type: ignore[assignment]


def profile_path(env: Mapping[str, str], home: Path) -> Path:
    value = env.get("TARTCI_MACOS_FLEET_PROFILE", "").strip()
    return Path(value).expanduser() if value else home / ".config/tartci/macos-fleet-profile.toml"


def profile_tart_home(path: Path) -> tuple[str | None, str]:
    """(declared store, why not) from the fleet profile."""
    if not path.is_file():
        return None, f"no fleet profile at {path}"
    if tomllib is None:
        return None, f"fleet profile {path} not read: this Python has no tomllib"
    try:
        with path.open("rb") as fh:
            parsed = tomllib.load(fh)
    except (OSError, ValueError) as exc:
        return None, f"fleet profile {path} unreadable: {exc}"
    host = parsed.get("host")
    value = host.get("tart_home") if isinstance(host, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None, f"fleet profile {path} declares no [host].tart_home"
    return value.strip(), ""


def resolve(env: Mapping[str, str] | None = None, home: Path | None = None) -> dict[str, Any]:
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    profile = profile_path(env, home)
    declared, why_not = profile_tart_home(profile)
    configured = env.get("TART_HOME", "").strip()
    out: dict[str, Any] = {"profile": str(profile), "profile_tart_home": declared}
    if configured and env.get("TARTCI_TART_HOME_SOURCE") == "profile" and declared \
            and Path(declared).expanduser() == Path(configured).expanduser():
        # The dispatcher exported the profile's store; say where it came from.
        out.update(path=str(Path(declared).expanduser()), source="profile",
                   detail=f"[host].tart_home from {profile}")
        return out
    if configured:
        out.update(path=str(Path(configured).expanduser()), source="env",
                   detail="TART_HOME from the environment")
        if declared and Path(declared).expanduser() != Path(configured).expanduser():
            out["warning"] = (f"TART_HOME={configured} differs from the fleet profile's "
                              f"[host].tart_home={declared}; the lanes use the profile's")
        return out
    if declared:
        out.update(path=str(Path(declared).expanduser()), source="profile",
                   detail=f"[host].tart_home from {profile}")
        return out
    out.update(path=str(home / ".tart"), source="default",
               detail=f"Tart's default store ({why_not})")
    return out


def describe(value: Mapping[str, Any]) -> str:
    line = f"{value['path']} ({value['detail']})"
    return line + (f" WARNING: {value['warning']}" if value.get("warning") else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tart_home")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true")
    mode.add_argument("--lines", action="store_true",
                      help="path, then source, one per line (for the dispatcher)")
    args = parser.parse_args(argv)
    value = resolve()
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
    elif args.lines:
        print(value["path"])
        print(value["source"])
    else:
        print(f"tart store: {describe(value)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
