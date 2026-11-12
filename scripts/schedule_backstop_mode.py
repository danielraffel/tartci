#!/usr/bin/env python3
"""Which schedule-backstop mode this host's fleet profile asks for.

The schedule backstop (scripts/schedule_backstop.py) must dispatch from exactly
ONE host. Which host that is lives in the fleet profile, so a host is a
dispatcher because its reviewed profile says so, not because someone once
hand-edited a plist on it:

    schedule_backstop = "live"      # dispatch (APPLY=1, AUTHORITY=1)
    schedule_backstop = "dry-run"   # log what it would dispatch, never dispatch
    schedule_backstop = "off"       # the default: install nothing

The key is top-level, so it must sit above the profile's first table.
scripts/install_schedule_backstop_agent.sh turns the mode into the agent's
environment; macos_fleet_lanes.py rejects any other value at install time.

    schedule_backstop_mode.py [--profile-file PATH]

prints the mode and exits 0, or names why it cannot tell and exits 3 (no
tomllib, an unreadable profile, an invalid value). "Cannot tell" is not "off":
the installer leaves an existing agent strictly alone rather than guess.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # Python < 3.11 (/usr/bin/python3 on macOS is 3.9)
    tomllib = None  # type: ignore[assignment]

KEY = "schedule_backstop"
MODES = ("live", "dry-run", "off")
DEFAULT_MODE = "off"
# The agent's environment for each mode that installs one.
ENVIRONMENT: Dict[str, Dict[str, str]] = {
    "live": {"TARTCI_BACKSTOP_APPLY": "1", "TARTCI_BACKSTOP_AUTHORITY": "1"},
    "dry-run": {"TARTCI_BACKSTOP_APPLY": "0", "TARTCI_BACKSTOP_AUTHORITY": "0"},
}


def default_profile_path() -> Path:
    return Path(os.environ.get(
        "TARTCI_FLEET_PROFILE",
        str(Path.home() / ".config" / "tartci" / "macos-fleet-profile.toml"),
    )).expanduser()


def validate(value: Any) -> List[str]:
    """Problems with a `schedule_backstop` value; empty when it is acceptable."""
    if value is None:
        return []
    if not isinstance(value, str) or value not in MODES:
        return [f"{KEY} must be one of {', '.join(repr(m) for m in MODES)}"]
    return []


def mode_of(data: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """The mode a parsed profile asks for, or (None, why) when it is invalid."""
    value = data.get(KEY)
    problems = validate(value)
    if problems:
        return None, "; ".join(problems)
    if value is None:
        return DEFAULT_MODE, f"{KEY} not set in the fleet profile"
    return value, f"{KEY} = {value!r} in the fleet profile"


def mode_from_profile(path: Path) -> Tuple[Optional[str], str]:
    """The mode the profile at `path` asks for, or (None, why) when unknown."""
    if not path.exists():
        # A host with no fleet profile is not a fleet host: it dispatches nothing.
        return DEFAULT_MODE, f"no fleet profile at {path}"
    if tomllib is None:
        return None, "no tomllib (needs Python 3.11+); cannot read the fleet profile"
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"fleet profile unreadable: {exc}"
    return mode_of(data)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="schedule_backstop_mode.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile-file", type=Path, default=None)
    args = parser.parse_args(argv)
    mode, why = mode_from_profile(args.profile_file or default_profile_path())
    if mode is None:
        print(f"schedule-backstop: cannot tell the mode: {why}", file=sys.stderr)
        return 3
    print(mode)
    print(f"schedule-backstop: {why}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
