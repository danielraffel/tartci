#!/usr/bin/env python3
"""Keep a host's rustup from checking for, or installing, its own updates.

Usage: rustup_settings.py apply PROFILE

A profile's [rustup] table names the host's rustup (`path`, absolute) and
`auto_self_update = "disable"`. rustup otherwise checks for a newer rustup on
every `rustup toolchain install` and `rustup update`, and with the default
setting installs it: a pinned toolchain must never query for updates or change
under a lane. `apply` runs `<path> set auto-self-update disable` and then
reads rustup's own settings file back; anything else refuses the install with
the expected and found values and the fix.

A profile without [rustup] changes nothing on the host. To undo the setting by
hand: `rustup set auto-self-update enable`.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

KEYS = {"path", "auto_self_update"}


def table_errors(table: object) -> list[str]:
    if not isinstance(table, dict) or set(table) != KEYS:
        return ["rustup must be a table with exactly path and auto_self_update"]
    errors = []
    path = table["path"]
    if not isinstance(path, str) or not path.startswith("/") or not path.endswith("/rustup") \
            or '"' in path or "\n" in path:
        errors.append("rustup.path must be the absolute path of the host's rustup binary")
    if table["auto_self_update"] != "disable":
        errors.append('rustup.auto_self_update must be "disable": a pinned toolchain never self-updates')
    return errors


def settings_file(env: dict[str, str], home: Path) -> Path:
    return Path(env.get("RUSTUP_HOME") or home / ".rustup") / "settings.toml"


def recorded_mode(settings: Path) -> str | None:
    """auto_self_update as rustup recorded it (None when unset or unreadable)."""
    try:
        text = settings.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'(?m)^auto_self_update\s*=\s*"([^"]*)"', text)
    return match.group(1) if match else None


def apply(table: dict, env: dict[str, str], home: Path) -> str | None:
    """None when rustup now records auto_self_update = "disable"; otherwise why not."""
    rustup = Path(table["path"])
    fix = f"fix: run `{rustup} set auto-self-update disable`, or correct [rustup].path"
    if not rustup.is_file() or not os.access(rustup, os.X_OK):
        return f"rustup is not installed at {rustup}; {fix}"
    try:
        out = subprocess.run([str(rustup), "set", "auto-self-update", "disable"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=30, check=False, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{rustup} set auto-self-update disable did not run: {exc}"
    if out.returncode != 0:
        return f"{rustup} set auto-self-update disable exited {out.returncode}: {out.stderr.strip()[:200]}"
    settings = settings_file(env, home)
    found = recorded_mode(settings)
    if found != "disable":
        return f"{settings} records auto_self_update = {found!r}, expected 'disable'; {fix}"
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "apply":
        print(__doc__.split("\n\n", 1)[0], file=sys.stderr)
        return 2
    import tomllib  # noqa: PLC0415 - the installer runs a 3.11+ interpreter

    try:
        with Path(argv[1]).open("rb") as handle:
            table = tomllib.load(handle).get("rustup")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"rustup settings: cannot read {argv[1]}: {exc}", file=sys.stderr)
        return 2
    if table is None:
        print("rustup settings: this profile manages no rustup")
        return 0
    errors = table_errors(table)
    if errors:
        print("rustup settings: " + "; ".join(errors), file=sys.stderr)
        return 2
    problem = apply(table, dict(os.environ), Path(os.path.expanduser("~")))
    if problem:
        print(f"rustup settings: {problem}", file=sys.stderr)
        return 1
    print(f"rustup settings: {table['path']} never self-updates (auto_self_update = \"disable\")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
