#!/usr/bin/env python3
"""Digest and shape check for a host-side Chrome for Testing directory.

The directory is mounted read-only into a guest, so it must hold nothing but
one `Google Chrome for Testing.app`, and its content must match a pinned
digest. The digest covers every path, its type, its executable bit, and either
the file bytes or the symlink target, so a changed, added or removed entry
changes it.

    cft_tree_digest.py digest DIR            print the digest
    cft_tree_digest.py verify DIR SHA256     exit 0 only on an exact match
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys

APP = "Google Chrome for Testing.app"
EXECUTABLE = f"{APP}/Contents/MacOS/Google Chrome for Testing"
SHA256 = re.compile(r"[0-9a-f]{64}")


class TreeError(Exception):
    pass


def _entries(root: str):
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + files):
            full = os.path.join(base, name)
            yield os.path.relpath(full, root), full


def digest(root: str) -> str:
    if os.path.islink(root) or not os.path.isdir(root):
        raise TreeError(f"not a real directory: {root}")
    if sorted(os.listdir(root)) != [APP]:
        raise TreeError(f"directory must contain exactly {APP!r}")
    outer = hashlib.sha256()
    for rel, full in sorted(_entries(root)):
        mode = os.lstat(full).st_mode
        if stat.S_ISLNK(mode):
            target = os.readlink(full)
            resolved = os.path.normpath(os.path.join(os.path.dirname(full), target))
            base = os.path.normpath(root) + os.sep
            if os.path.isabs(target) or not (resolved + os.sep).startswith(base):
                raise TreeError(f"symlink escapes the directory: {rel}")
            record = f"L\0{rel}\0{target}\n"
        elif stat.S_ISDIR(mode):
            record = f"D\0{rel}\n"
        elif stat.S_ISREG(mode):
            inner = hashlib.sha256()
            with open(full, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    inner.update(chunk)
            record = f"F\0{rel}\0{int(bool(mode & 0o111))}\0{inner.hexdigest()}\n"
        else:
            raise TreeError(f"unsupported entry type: {rel}")
        outer.update(record.encode("utf-8", "surrogateescape"))
    if not os.access(os.path.join(root, EXECUTABLE), os.X_OK):
        raise TreeError(f"missing executable: {EXECUTABLE}")
    return outer.hexdigest()


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[1] == "digest":
            print(digest(argv[2]))
            return 0
        if len(argv) == 4 and argv[1] == "verify":
            if not SHA256.fullmatch(argv[3]):
                raise TreeError("expected digest must be 64 lowercase hex characters")
            actual = digest(argv[2])
            if actual != argv[3]:
                raise TreeError(f"digest mismatch: expected {argv[3]} actual {actual}")
            return 0
    except (TreeError, OSError) as err:
        print(f"cft_tree_digest: {err}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
