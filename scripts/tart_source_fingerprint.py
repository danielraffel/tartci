#!/usr/bin/env python3
"""Emit a bounded identity fingerprint for a stopped local Tart image."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tart-home", required=True, type=Path)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", args.name) or ".." in args.name:
        raise SystemExit("unsafe Tart image name")
    root = args.tart_home / "vms" / args.name
    if not root.is_dir() or root.is_symlink():
        raise SystemExit(f"Tart image directory unavailable or unsafe: {root}")
    rows = []
    for name in ("config.json", "disk.img", "nvram.bin"):
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"required Tart image file unavailable or unsafe: {path}")
        stat = path.stat()
        row = {"name": name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        if name != "disk.img":
            row["sha256"] = digest(path)
        rows.append(row)
    print(json.dumps({"schema": 1, "name": args.name, "files": rows}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
