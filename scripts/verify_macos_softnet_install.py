#!/usr/bin/env python3
"""Fail-closed verifier for Tart's pinned macOS Softnet privilege boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


PINNED_PATH = Path("/usr/local/libexec/tartci/softnet")
PINNED_VERSION = "softnet 0.23.0-e5fd48c"
PINNED_SHA256 = "5982c8cde55cd039d4aa71add54356224b8b8a040df1a8786f16327b421f701d"


def verify(path: Path, expected_version: str, expected_sha256: str) -> dict[str, object]:
    if path.name != "softnet":
        raise RuntimeError("pinned Softnet executable must be named softnet")
    original = path.absolute()
    resolved = original.resolve(strict=True)
    if original != resolved:
        raise RuntimeError("pinned Softnet path must not contain symlink components")
    digest_builder = hashlib.sha256()
    with resolved.open("rb") as handle:
        info = os.fstat(handle.fileno())
        while chunk := handle.read(1024 * 1024):
            digest_builder.update(chunk)
    digest = digest_builder.hexdigest()
    failures: list[str] = []
    if not stat.S_ISREG(info.st_mode):
        failures.append("target is not a regular file")
    if info.st_uid != 0:
        failures.append("target is not owned by root")
    if not info.st_mode & stat.S_ISUID:
        failures.append("target does not have the setuid bit")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        failures.append("target is group/world writable")
    parent = resolved.parent
    while True:
        parent_info = parent.stat()
        if parent_info.st_uid != 0:
            failures.append(f"parent directory is not root-owned: {parent}")
        if parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            failures.append(f"parent directory is group/world writable: {parent}")
        if parent == parent.parent:
            break
        parent = parent.parent
    if digest != expected_sha256:
        failures.append(f"SHA-256 mismatch: {digest}")
    if failures:
        raise RuntimeError(json.dumps({
            "path": str(resolved),
            "sha256": digest,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": f"{stat.S_IMODE(info.st_mode):04o}",
            "verified": False,
            "failures": failures,
        }, sort_keys=True))
    # Only execute after a single opened inode has passed exact digest,
    # ownership, mode, and immutable-parent checks. The root-owned parent chain
    # prevents an unprivileged pathname swap between this check and execution.
    version = subprocess.run(
        [str(resolved), "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if version != expected_version:
        failures.append(f"version mismatch: {version!r}")
    result: dict[str, object] = {
        "path": str(original),
        "version": version,
        "sha256": digest,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": f"{stat.S_IMODE(info.st_mode):04o}",
        "verified": not failures,
        "failures": failures,
    }
    if failures:
        raise RuntimeError(json.dumps(result, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=PINNED_PATH)
    parser.add_argument("--expected-version", default=PINNED_VERSION)
    parser.add_argument("--expected-sha256", default=PINNED_SHA256)
    args = parser.parse_args()
    candidate = args.path
    try:
        result = verify(candidate, args.expected_version, args.expected_sha256)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        print(f"softnet install verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
