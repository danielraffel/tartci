#!/usr/bin/env python3
"""The pinned mise binary a profile's [mise] table names.

Usage:
  mise_pin.py check PROFILE          verify the binary; exit 1 with the fix if not
  mise_pin.py write PROFILE PIN_FILE verify, then record it in PIN_FILE (JSON)

[mise] names one exact mise build: `version`, the absolute `path` it is
installed at, and its `sha256`. The binary is built from source once and copied
between hosts by digest; it is never installed by Homebrew and never
self-updates. tartci only verifies it: a missing binary, a different digest or
a different `--version` refuses the install before any LaunchAgent is written,
naming the expected and found values and the fix.

A profile without [mise] pins nothing, and `write` removes a pin a previous
install wrote, so deleting the table is the rollback.

Nothing here runs mise's tools or changes PATH; lanes keep the tools they run
today until a lane is moved to the pinned toolchain on purpose.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

KEYS = {"version", "path", "sha256"}
VERSION = re.compile(r"^[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,3}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def table_errors(table: object) -> list[str]:
    """Schema problems of a [mise] table (empty when valid)."""
    if not isinstance(table, dict) or set(table) != KEYS:
        return ["mise must be a table with exactly version, path and sha256"]
    errors = []
    if not isinstance(table["version"], str) or not VERSION.match(table["version"]):
        errors.append("mise.version must be an exact mise release such as 2026.10.8")
    path = table["path"]
    if (not isinstance(path, str) or not path.startswith("/") or '"' in path or "\n" in path
            or not path.endswith("/mise")):
        errors.append("mise.path must be the absolute path of the pinned mise binary")
    elif isinstance(table["version"], str) and table["version"] not in path:
        errors.append("mise.path must contain mise.version, so two pins never share a path")
    if not isinstance(table["sha256"], str) or not SHA256.match(table["sha256"]):
        errors.append("mise.sha256 must be a lowercase 64-hex sha256")
    return errors


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(table: dict) -> str | None:
    """None when the binary at table['path'] is the pinned one; otherwise why not."""
    path = Path(table["path"])
    fix = (f"fix: copy the pinned binary from a host that has it (scp <host>:{path} {path}), "
           f"then confirm `shasum -a 256 {path}` prints {table['sha256']}")
    if not path.is_file() or not os.access(path, os.X_OK):
        return f"mise {table['version']} is not installed at {path}; {fix}"
    found = sha256_file(path)
    if found != table["sha256"]:
        return (f"{path} has sha256 {found}, but the profile pins {table['sha256']} "
                f"(mise {table['version']}); {fix}")
    try:
        # MISE_DISABLE_UPDATE_WARNING: `--version` otherwise asks GitHub for a
        # newer mise, and the pinned binary never queries.
        out = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=20, check=False,
                             env=dict(os.environ, MISE_DISABLE_UPDATE_WARNING="1"))
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{path} --version did not run: {exc}"
    words = out.stdout.split()
    reported = words[0] if out.returncode == 0 and words else "nothing"
    if reported != table["version"]:
        return f"{path} --version reports {reported}, but the profile pins mise {table['version']}"
    return None


def load_table(profile: Path) -> dict | None:
    import tomllib  # noqa: PLC0415 - the installer runs a 3.11+ interpreter

    with profile.open("rb") as handle:
        data = tomllib.load(handle)
    table = data.get("mise")
    if table is None:
        return None
    errors = table_errors(table)
    if errors:
        raise ValueError("; ".join(errors))
    return table


def write_pin(pin_file: Path, table: dict, now: float | None = None) -> None:
    record = dict(table, verified_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))
    pin_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mise.pin.", dir=str(pin_file.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, pin_file)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("check", "write") or (argv[0] == "write") != (len(argv) == 3):
        print(__doc__.split("\n\n", 1)[0], file=sys.stderr)
        return 2
    try:
        table = load_table(Path(argv[1]))
    except (OSError, ValueError) as exc:
        print(f"mise pin: cannot read [mise] from {argv[1]}: {exc}", file=sys.stderr)
        return 2
    if table is None:
        if argv[0] == "write":
            Path(argv[2]).unlink(missing_ok=True)
        print("mise pin: this profile pins no mise")
        return 0
    problem = verify(table)
    if problem:
        print(f"mise pin: {problem}", file=sys.stderr)
        return 1
    if argv[0] == "write":
        write_pin(Path(argv[2]), table)
    print(f"mise pin: {table['path']} is mise {table['version']} (sha256 {table['sha256']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
