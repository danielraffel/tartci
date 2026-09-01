#!/usr/bin/env python3
"""Run one exact command with TartCI's bounded process-group semantics."""

from __future__ import annotations

import argparse
import sys

from bounded_subprocess import run_bounded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bounded-command")
    parser.add_argument("--timeout", required=True, type=float)
    parser.add_argument("--operation", default="command")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("an exact command is required after --")
    result = run_bounded(command, timeout=args.timeout, operation=args.operation)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
