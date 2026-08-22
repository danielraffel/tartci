#!/usr/bin/env python3
"""Argument grammar for the tartci host lease CLI."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence


def parse_args(
    argv: list[str] | None,
    *,
    default_store_dir: str,
    priority_classes: Mapping[str, int],
    valid_roles: Sequence[str],
    stale_secs: int,
) -> argparse.Namespace:
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0].startswith("-"):
        raw = ["status", *raw]
    parser = argparse.ArgumentParser(prog="tartci leases")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--store-dir", default=default_store_dir)
        command_parser.add_argument("--capacity", type=int, help="override lease capacity (cores)")
        command_parser.add_argument(
            "--capacity-mem-mb",
            type=int,
            help="override memory lease capacity in MB (0 disables the memory axis)",
        )
        command_parser.add_argument(
            "--reserved-gate-cores", type=int, help="cores withheld from non-gate leases"
        )
        command_parser.add_argument("--gate-priority", type=int, default=priority_classes["gate"])
        command_parser.add_argument("--stale-secs", type=int, default=stale_secs)
        command_parser.add_argument("--role", choices=valid_roles)
        command_parser.add_argument("--role-file")
        command_parser.add_argument("--host-cores", type=int)
        command_parser.add_argument("--model")
        command_parser.add_argument("--json", action="store_true")

    status = sub.add_parser("status", aliases=["list"], help="show active leases")
    add_common(status)

    reap_parser = sub.add_parser("reap", help="reclaim dead-owner leases and show status")
    add_common(reap_parser)

    acquire_parser = sub.add_parser("acquire", help="acquire a core lease")
    add_common(acquire_parser)
    acquire_parser.add_argument("--cores", dest="cores_requested", type=int, required=True)
    acquire_parser.add_argument(
        "--mem-mb",
        type=int,
        help="memory this lease consumes in MB; omitted → cores * per-job estimate",
    )
    acquire_parser.add_argument("--priority", default="build")
    acquire_parser.add_argument("--pid", type=int)
    acquire_parser.add_argument("--id")
    acquire_parser.add_argument("--kind", default="unknown")
    acquire_parser.add_argument("--owner", default="")
    acquire_parser.add_argument("--label", default="")
    acquire_parser.add_argument("--job-id", default="")
    acquire_parser.add_argument("--vm-name", default="")
    acquire_parser.add_argument(
        "--disk-path",
        help="VM/overlay store path whose filesystem receives the growth reservation",
    )
    acquire_parser.add_argument(
        "--disk-growth-mb",
        type=int,
        default=0,
        help="worst-case disk growth to reserve on --disk-path's filesystem",
    )
    acquire_parser.add_argument(
        "--disk-floor-mb",
        type=int,
        default=0,
        help="free-space safety floor retained after all active/requested growth",
    )
    acquire_parser.add_argument(
        "--disk-expected-device-id",
        default="",
        help="optional persisted filesystem device identity for --disk-path",
    )
    acquire_parser.add_argument(
        "--disk-expected-mount-path",
        default="",
        help="optional expected mounted filesystem root for --disk-path",
    )

    release_parser = sub.add_parser("release", help="release a lease by id")
    add_common(release_parser)
    release_parser.add_argument("--id", required=True)

    heartbeat_parser = sub.add_parser("heartbeat", help="refresh a lease heartbeat")
    add_common(heartbeat_parser)
    heartbeat_parser.add_argument("--id", required=True)

    guard_parser = sub.add_parser(
        "guard-exec",
        help="atomically attach this process as a VM lease guardian, then exec a command",
    )
    add_common(guard_parser)
    guard_parser.add_argument("--id", required=True)
    guard_parser.add_argument("argv", nargs=argparse.REMAINDER)

    guard_run_parser = sub.add_parser(
        "guard-run",
        help="guard a finite VM disk writer and return ownership to its supervisor",
    )
    add_common(guard_run_parser)
    guard_run_parser.add_argument("--id", required=True)
    guard_run_parser.add_argument("argv", nargs=argparse.REMAINDER)

    return parser.parse_args(raw)
