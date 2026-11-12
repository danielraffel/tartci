#!/usr/bin/env python3
"""Refuse array expansions that macOS's /bin/bash 3.2 kills under `set -u`.

macOS ships bash 3.2, and the fleet's launchd lanes run tartci's scripts with
it. Under `set -u` bash 3.2 treats an empty array as unset, so
`"${args[@]}"` on an empty `args` aborts the script with "args[@]: unbound
variable". bash 4.4 and later expand it to nothing, so the Ubuntu lint host
never sees the failure: a storage-health call that passed CI stopped every
lane on one host this way.

In a shell script that turns on nounset (`set -u`, `set -euo pipefail`,
`set -o nounset`, ...) this lint flags every `${name[@]}`, `${name[*]}` and
`${name[@]:offset}` expansion of a named array unless it is written in the
3.2-safe form

    ${name[@]+"${name[@]}"}

which expands to nothing when the array is empty. `${#name[@]}` (a count) and
`${name[@]:-...}` / `${name[@]+...}` (which carry their own default) are safe
and pass. A line whose array is provably non-empty can say so instead:

    "${files[@]}"   # bash32-lint: nonempty <why it cannot be empty>

The reason is required. A sourced library (`*.lib.sh`) is checked even when
it does not turn on nounset itself, since it runs under its caller's.
Shell files are found the way scripts/lint.sh finds
them: `*.sh` plus extensionless files with a bash shebang, except the
verbatim copies of other repositories' scripts under tests/fixtures.

    bash32_array_lint.py [--root DIR] [FILE ...]   exit 0 clean, 1 with findings
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# `set` with a short-option cluster containing u, or `set -o nounset`.
NOUNSET = re.compile(r"^\s*set\s+(?:[^#\n]*\s)?(?:-[A-Za-z]*u[A-Za-z]*\b|-o\s+nounset\b)",
                     re.MULTILINE)
# The guarded form, `${a[@]+"${a[@]}"}` or `${a+"${a[@]}"}`; its inner
# expansion is safe.
GUARDED = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?:\[[@*]\])?\+"\$\{\1\[[@*]\]\}"\}')
# An expansion bash 3.2 refuses on an empty array under nounset: the whole
# array, or a slice of it. `${#a[@]}` never matches (the `#` precedes the
# name), and `${a[@]:-x}` / `${a[@]+x}` / `${a[@]-x}` carry their own default.
UNSAFE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\[[@*]\](?:\}|:(?![-=?+]))")
# Verbatim copies of another repository's scripts, kept as test fixtures and
# never run by a lane.
VENDORED = ("tests", "fixtures")
ESCAPE = re.compile(r"#\s*bash32-lint:\s*nonempty\s+\S")


def findings_in(text: str) -> list[tuple[int, str]]:
    """[(line number, array name)] for each unsafe expansion in ``text``, a
    script that turns on nounset. Comment lines and escaped lines pass."""
    out: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#") or ESCAPE.search(line):
            continue
        for match in UNSAFE.finditer(GUARDED.sub("", line)):
            out.append((number, match.group(1)))
    return out


def uses_nounset(text: str) -> bool:
    return NOUNSET.search(text) is not None


def is_sourced_library(path: Path) -> bool:
    """A `*.lib.sh` file is sourced into its caller and runs under the
    caller's `set -u`, so it is checked whether or not it sets nounset itself."""
    return path.name.endswith(".lib.sh")


def shell_files(root: Path) -> list[Path]:
    """`*.sh` plus extensionless files with a bash shebang, as lint.sh finds them."""
    out = []
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if ".git" in parts or parts[:2] == VENDORED or not path.is_file():
            continue
        if path.suffix == ".sh":
            out.append(path)
        elif "." not in path.name:
            try:
                with path.open("rb") as handle:
                    first = handle.readline()
            except OSError:
                continue
            if re.match(rb"^#!.*\bbash\b", first):
                out.append(path)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--root", default=Path(__file__).resolve().parent.parent, type=Path)
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)
    files = args.files or shell_files(args.root)
    total = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"{path}: cannot read: {exc}", file=sys.stderr)
            total += 1
            continue
        if not (uses_nounset(text) or is_sourced_library(path)):
            continue
        for number, name in findings_in(text):
            total += 1
            print(f"{path}:{number}: ${{{name}[@]}} fails on an empty array under bash 3.2 "
                  f"set -u; write ${{{name}[@]+\"${{{name}[@]}}\"}} or add "
                  f"`# bash32-lint: nonempty <reason>`", file=sys.stderr)
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
