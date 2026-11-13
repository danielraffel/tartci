#!/usr/bin/env python3
"""The body of tcc_wedge's fresh-launchd-job probe. Standard library only.

tcc_wedge.launchd_probe submits this file as a one-off launchd job
(`launchctl submit`), so it runs in a NEW launchd context with no cached
Removable Volumes decision: exactly the context the lanes and the watchdog
open the store from. It records its progress in a result file on the internal
volume, one phase at a time, so the parent can tell where it stopped:

  internal_ok  the internal control directory opened (this process can do I/O)
  root_ok      the external volume root opened too
  root_error   the volume root open failed fast with an error

A process still at internal_ok when the parent's deadline passes hung on the
volume root. Python retries an open() interrupted by a signal (PEP 475), so a
sandbox authorization wait that surfaces as EINTR in C keeps this process
waiting, which reads as the hang it is.

After the last phase the process sleeps for --hold seconds instead of exiting:
`launchctl submit` keeps a job alive, and an exit would rerun it and overwrite
the result. The parent removes the job when it has read the result.
Metadata only: nothing is created, written or listed on the volume.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

FINAL = ("root_ok", "root_error")


def write_result(path: str, value: dict) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(value, handle, sort_keys=True)
    os.replace(temporary, path)


def open_dir(path: str) -> float:
    started = time.monotonic()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fstat(fd)
    finally:
        os.close(fd)
    return round((time.monotonic() - started) * 1000, 3)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True)
    parser.add_argument("--internal", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--hold", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        with open(args.result) as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        previous = {}
    if isinstance(previous, dict) and previous.get("phase") in FINAL:
        # A relaunch of a job that already answered: keep that answer.
        time.sleep(max(0.0, args.hold))
        return 0
    record = {"pid": os.getpid(), "phase": "internal_ok",
              "internal_latency_ms": open_dir(args.internal)}
    write_result(args.result, record)
    try:
        record["root_latency_ms"] = open_dir(args.root)
        record["phase"] = "root_ok"
    except OSError as exc:
        record.update(phase="root_error", errno=exc.errno, error=str(exc)[:200])
    write_result(args.result, record)
    time.sleep(max(0.0, args.hold))
    return 0


if __name__ == "__main__":
    sys.exit(main())
