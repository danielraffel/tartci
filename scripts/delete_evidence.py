#!/usr/bin/env python3
"""Render the evidence of one unproved `tart delete` as event fields.

A lane's teardown bounds `tart delete` at TEARDOWN_STEP_TIMEOUT. When the VM
is then not proved gone, the lane records `delete_unproved`. Until this
existed the command's output went to /dev/null, so "the bound is too short
under load" and "tart refused" could not be told apart. The fields keep them
apart: `bounded` says whether the bound fired, separately from `rc` (both can
be 124); `elapsed_ms` and `load1` test the load hypothesis; `stderr` is the
command's own last lines, capped.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

STDERR_MAX_CHARS = 300


def _bounded(status: dict) -> str:
    # No status file means no information: "?" never reads as "the bound did
    # not fire", which would bias the split these events exist to measure.
    timed_out = status.get("timed_out")
    if timed_out is None:
        return "?"
    return "yes" if timed_out else "no"


def render(status: dict, stderr: str, load1: float | None) -> str:
    tail = " | ".join(line.strip() for line in stderr.strip().splitlines()[-3:] if line.strip())
    if len(tail) > STDERR_MAX_CHARS:
        tail = "…" + tail[-STDERR_MAX_CHARS:]
    tail = tail.replace("\\", "\\\\").replace('"', "'")
    return (
        f"rc={status.get('returncode', '?')} "
        f"elapsed_ms={status.get('elapsed_ms', '?')} "
        f"bounded={_bounded(status)} "
        f"load1={'?' if load1 is None else f'{load1:.2f}'} "
        f'stderr="{tail}"'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", required=True)
    parser.add_argument("--stderr", required=True)
    args = parser.parse_args(argv)
    try:
        with open(args.status, encoding="utf-8") as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        status = {}
    try:
        with open(args.stderr, encoding="utf-8", errors="replace") as fh:
            stderr = fh.read()
    except OSError:
        stderr = ""
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = None
    print(render(status, stderr, load1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
