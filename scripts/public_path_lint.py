#!/usr/bin/env python3
"""Keep real fleet paths and host names out of this public repository.

tartci is public. A real home directory (/Users/<account>), a real external
volume (/Volumes/<name>) or a Mac's host name tells a reader the fleet's
layout; none belongs in a tracked file. This lint fails on:

  * /Users/<name> unless <name> is a placeholder (PLACEHOLDER_USERS);
  * /Volumes/<name> unless <name> is a placeholder (PLACEHOLDER_VOLUMES);
  * a Mac host name: `<Owner>s-Mac-Studio`, `<Owner>s-MacBook-Pro`,
    `<Owner>s-Mac-mini`, `<Owner>s-M5-Studio` and the like.

Write examples with a placeholder (`/Users/example`, `/Volumes/Example`,
`<home>`, `<checkout>`, `<store>`). A file that must carry a real path for
now is listed, with its reason, in scripts/public_path_allowlist.txt; the
host-specific profiles move to the private fleet source and leave that list.

    public_path_lint.py [--root DIR]     exit 0 clean, 1 with findings
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PLACEHOLDER_USERS = frozenset({
    "Shared", "example", "runner", "ci", "x", "a", "b", "me", "test", "tester", "admin",
    "someone", "user", "you", "name",
})
PLACEHOLDER_VOLUMES = frozenset({
    "Example", "Example2", "My", "Store", "Data", "Preboot", "A", "B", "W", "X",
    "NoSuchVolume", "Builds", "External", "Recovery", "VM", "VMs",
})
USERS = re.compile(r"/Users/([A-Za-z0-9_.-]+)")
VOLUMES = re.compile(r"/Volumes/([A-Za-z0-9_.-]+)")
# Host names in any case: a possessive Mac host name (its hostname, its
# .local mDNS name, or a tailnet DNS label) and the fleet's own machine
# names that carry no possessive.
HOST = re.compile(r"\b[a-z]+s-(?:mac-studio|macbook-pro|macbook-air|mac-mini|mac-pro|imac|"
                  r"m[0-9]+-studio|m[0-9]+-max)\b|\b" + "black" + r"book\b", re.IGNORECASE)
ALLOWLIST = "scripts/public_path_allowlist.txt"


def allowlisted(root: Path) -> dict[str, str]:
    """{tracked path: reason}; a line is `path<TAB or 2+ spaces>reason`."""
    out: dict[str, str] = {}
    try:
        lines = (root / ALLOWLIST).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = re.split(r"\t| {2,}", line.strip(), maxsplit=1)
        out[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
    return out


def findings_in(text: str) -> list[tuple[int, str]]:
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in USERS.finditer(line):
            if match.group(1) not in PLACEHOLDER_USERS and not match.group(1).startswith("."):
                hits.append((number, match.group(0)))
        for match in VOLUMES.finditer(line):
            if match.group(1) not in PLACEHOLDER_VOLUMES:
                hits.append((number, match.group(0)))
        for match in HOST.finditer(line):
            hits.append((number, match.group(0)))
    return hits


def tracked(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True,
                         check=True).stdout.decode()
    return [p for p in out.split("\0") if p]


def scan(root: Path, files: list[str]) -> list[str]:
    allowed = allowlisted(root)
    problems = []
    for rel in files:
        if rel in allowed or rel == ALLOWLIST:
            continue
        try:
            text = (root / rel).read_text(errors="replace")
        except (OSError, IsADirectoryError):
            continue
        if "\0" in text[:4096]:
            continue
        for number, hit in findings_in(text):
            problems.append(f"{rel}:{number}: {hit}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    problems = scan(args.root, tracked(args.root))
    for problem in problems:
        print(f"public-path: {problem}", file=sys.stderr)
    if problems:
        print(f"public-path: {len(problems)} real path(s) or host name(s) in tracked files; "
              "use a placeholder (/Users/example, /Volumes/Example, <home>, <checkout>) "
              f"or, with a reason, list the file in {ALLOWLIST}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
