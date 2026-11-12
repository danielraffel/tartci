#!/usr/bin/env python3
"""Verify a Windows qcow2 against its sidecar before creating a clone.

The first verification streams the image. Later checks reuse a private cache
only while the file identity, size, mtime, and ctime are unchanged. A changed
sidecar or image normally forces a full verification. The per-job sealed
snapshot always receives a full hash before QEMU can read it.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


SHA_LINE = re.compile(r"^([0-9a-fA-F]{64})[ \t]+.+$")


def identity(path: Path) -> dict[str, int]:
    value = path.stat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError(f"golden is not a regular file: {path}")
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def sidecar_digest(path: Path) -> str:
    sidecar = Path(f"{path}.sha256")
    if sidecar.is_symlink() or not sidecar.is_file():
        raise ValueError(f"SHA-256 sidecar must be a regular file: {sidecar}")
    lines = sidecar.read_text(encoding="ascii").splitlines()
    if len(lines) != 1 or not (match := SHA_LINE.fullmatch(lines[0])):
        raise ValueError(f"invalid SHA-256 sidecar: {sidecar}")
    return match.group(1).lower()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify(image: Path, cache_dir: Path, no_cache: bool = False) -> str:
    if image.is_symlink():
        raise ValueError(f"golden path must not be a symlink: {image}")
    image = image.resolve(strict=True)
    expected = sidecar_digest(image)
    before = identity(image)
    record = {"schema": 1, "image": str(image), "sha256": expected, "identity": before}
    if not no_cache:
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if cache_dir.is_symlink() or cache_dir.stat().st_mode & 0o077:
            raise ValueError(f"cache directory must be private: {cache_dir}")
        cache_name = hashlib.sha256(os.fsencode(image)).hexdigest() + ".json"
        cache_path = cache_dir / cache_name
        try:
            fd = os.open(cache_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r", encoding="utf-8") as cached:
                info = os.fstat(cached.fileno())
                if (stat.S_ISREG(info.st_mode) and info.st_mode & 0o077 == 0
                        and json.load(cached) == record):
                    return "cached"
        except (OSError, ValueError, TypeError):
            pass

    actual = file_digest(image)
    if identity(image) != before or sidecar_digest(image) != expected:
        raise ValueError("golden or sidecar changed during verification")
    if actual != expected:
        raise ValueError(f"golden SHA-256 mismatch: expected {expected}, actual {actual}")
    if not no_cache:
        fd, temp_name = tempfile.mkstemp(prefix=".windows-golden-", dir=cache_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(record, output, sort_keys=True)
                output.write("\n")
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, cache_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    return "hashed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--cache-dir", type=Path,
                        default=Path.home() / ".cache/tartci/windows-golden")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    try:
        result = verify(args.image, args.cache_dir, args.no_cache)
    except (OSError, ValueError) as error:
        print(f"Windows golden admission refused: {error}", file=sys.stderr)
        return 1
    print(f"Windows golden SHA-256 verified ({result}): {args.image}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
