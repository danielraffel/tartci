#!/usr/bin/env python3
"""Run one exact command with TartCI's bounded process-group semantics."""

from __future__ import annotations

import argparse
import json
import sys
import time

from bounded_subprocess import run_bounded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bounded-command")
    parser.add_argument("--timeout", required=True, type=float)
    parser.add_argument("--operation", default="command")
    parser.add_argument("--status-file",
                        help="write {returncode, timed_out, elapsed_ms} here as JSON")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("an exact command is required after --")
    started = time.monotonic()
    result = run_bounded(command, timeout=args.timeout, operation=args.operation)
    if args.status_file:
        with open(args.status_file, "w", encoding="utf-8") as fh:
            json.dump({
                "returncode": result.returncode,
                "timed_out": bool(getattr(result, "timed_out", False)),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }, fh)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
