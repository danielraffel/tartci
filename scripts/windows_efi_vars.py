#!/usr/bin/env python3
"""Copy a Windows golden's EFI variable store into a job, proving its bytes.

A Windows golden whose boot entry lives in its own UEFI variable store does not
boot from the stock edk2 vars template. Such a golden ships a vars file beside
it, and the providers copy that file into each job instead. The copy is what
QEMU writes to, so the template itself is never modified; this verifies the
COPY against the pinned SHA-256, which also catches a template that changed
between the check and the copy.

  windows_efi_vars.py --template T --sha256 HEX --dest D   # copy, verify, exit 0
  windows_efi_vars.py --template T --sha256 HEX --check    # verify the template only

Exit 0 on success, 1 when the bytes do not match or a path is unsafe (the
partial destination is removed), 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a regular file, not a link or directory: {path}")


def _copy_verified(template: Path, dest: Path, expected: str) -> None:
    """Create dest exclusively, copy, and verify; any failure removes dest."""
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as exc:
        raise ValueError(f"destination already exists: {dest}") from exc
    try:
        with os.fdopen(fd, "wb") as out, template.open("rb") as src:
            shutil.copyfileobj(src, out, 1 << 20)
        actual = _digest(dest)
        if actual != expected:
            raise ValueError(f"EFI vars copy SHA-256 mismatch: expected {expected}, got {actual}")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", required=True)
    parser.add_argument("--sha256", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dest")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    expected = args.sha256.strip().lower()
    if not HEX64.match(expected):
        print("windows-efi-vars: --sha256 must be 64 hex digits", file=sys.stderr)
        return 2
    template = Path(args.template)
    try:
        _regular_file(template, "EFI vars template")
        if args.check:
            actual = _digest(template)
            if actual != expected:
                raise ValueError(f"EFI vars template SHA-256 mismatch: expected {expected}, got {actual}")
            print(f"windows-efi-vars: template verified {expected}")
            return 0
        dest = Path(args.dest)
        _copy_verified(template, dest, expected)
    except (OSError, ValueError) as exc:
        print(f"windows-efi-vars: {exc}", file=sys.stderr)
        return 1
    print(f"windows-efi-vars: copied verified template to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
