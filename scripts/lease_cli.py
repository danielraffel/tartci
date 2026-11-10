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
        command_parser.add_argument(
            "--reserved-gate-mem-mb",
            type=int,
            help="memory in MB withheld from non-gate leases",
        )
        command_parser.add_argument(
            "--agent-floor-cores",
            type=int,
            help="override the per-lease agent floor (0 disables floor leases)",
        )
        command_parser.add_argument(
            "--agent-floor-pool-cores",
            type=int,
            help="override the host-wide cap on concurrent floor-lease cores",
        )
        command_parser.add_argument(
            "--agent-floor-qos",
            choices=("utility", "background"),
            help="override the QoS a floor lease runs at",
        )
        command_parser.add_argument("--gate-priority", type=int, default=priority_classes["gate"])
        command_parser.add_argument("--stale-secs", type=int, default=stale_secs)
        command_parser.add_argument("--role", choices=valid_roles)
        command_parser.add_argument("--role-file")
        command_parser.add_argument("--host-cores", type=int)
        command_parser.add_argument("--model")
        command_parser.add_argument(
            "--rank-vm-waiters",
            choices=("on", "off"),
            help="override the fleet profile's [leases] rank_vm_waiters knob",
        )
        command_parser.add_argument(
            "--waiter-fresh-secs",
            type=int,
            help="override how long an unrefreshed VM lease waiter still counts",
        )
        command_parser.add_argument(
            "--dynamic-lending",
            choices=("on", "off"),
            help="override the governor's dynamic_lending knob",
        )
        command_parser.add_argument(
            "--vm-slot-lending",
            choices=("on", "off"),
            help="override the fleet profile's [leases] vm_slot_lending knob",
        )
        command_parser.add_argument(
            "--macos-vm-cap",
            type=int,
            help="macOS guests this host can run at once (default: the role's cap)",
        )
        command_parser.add_argument(
            "--gate-prompt-reserve-cores",
            type=int,
            help="override cores kept free for the next gate job while lending",
        )
        command_parser.add_argument(
            "--interactive-share-cores",
            type=int,
            help="override the cap on all interactive build leases together",
        )
        command_parser.add_argument(
            "--background-share-cores",
            type=int,
            help="override the cap on all background build leases together",
        )
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
    acquire_parser.add_argument(
        "--allow-floor",
        action="store_true",
        help=(
            "when a non-gate, non-VM lease is denied on cores, accept a smaller "
            "floor lease that runs at background QoS and is not charged against "
            "other leases; the caller must honour the granted size and qos"
        ),
    )
    acquire_parser.add_argument(
        "--class",
        dest="build_class",
        choices=("interactive", "background"),
        help=(
            "build class: interactive (someone is waiting; normal QoS, may borrow "
            "idle gate cores when lending is on, never floored) or background "
            "(validations/opportunistic; capped at the background share). "
            "Omitted: the class-less rule every caller had before classes"
        ),
    )
    acquire_parser.add_argument(
        "--min-cores",
        type=int,
        default=0,
        help=(
            "accept a partial lease of at least this many cores when the full "
            "request does not fit; the caller must honour the granted size"
        ),
    )
    acquire_parser.add_argument(
        "--wait-secs",
        type=int,
        default=0,
        help="retry a denied acquire (locally, every 2 s) for up to this long",
    )
    acquire_parser.add_argument(
        "--memory-only",
        action="store_true",
        help=(
            "hold memory (and disk) but no cores, for a parked pre-booted VM; "
            "requires --cores 0 and --mem-mb; upgrade later with `resize`"
        ),
    )
    acquire_parser.add_argument("--pid", type=int)
    acquire_parser.add_argument("--id")
    acquire_parser.add_argument("--kind", default="unknown")
    acquire_parser.add_argument("--owner", default="")
    acquire_parser.add_argument("--label", default="")
    acquire_parser.add_argument("--job-id", default="")
    acquire_parser.add_argument("--vm-name", default="")
    acquire_parser.add_argument(
        "--waiter-id",
        default="",
        help="this lane's VM lease waiter: never ranked against itself, refreshed "
             "on denial, withdrawn on grant",
    )
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
        "--home-floor-path",
        default="",
        help="with --disk-path: also judge this path's volume (the home volume) "
             "against a per-host free-space floor when it is not --disk-path's "
             "device; a NEW VM lease below it is refused (home_volume_floor.py)",
    )
    acquire_parser.add_argument(
        "--home-floor-mode",
        choices=("report", "refuse"),
        default="report",
        help="report (default): admit and mark the grant would_refuse; refuse: deny it",
    )
    acquire_parser.add_argument(
        "--home-floor-hours",
        type=float,
        default=1.0,
        help="hours until the next reclaim pass; the floor covers that much fill",
    )
    acquire_parser.add_argument(
        "--disk-expected-mount-path",
        default="",
        help="optional expected mounted filesystem root for --disk-path",
    )

    release_parser = sub.add_parser("release", help="release a lease by id")
    add_common(release_parser)
    release_parser.add_argument("--id", required=True)

    resize_parser = sub.add_parser(
        "resize",
        help="atomically change an existing lease's cores/memory/priority (denied: unchanged)",
    )
    add_common(resize_parser)
    resize_parser.add_argument("--id", required=True)
    resize_parser.add_argument("--cores", dest="cores_requested", type=int, required=True)
    resize_parser.add_argument("--mem-mb", type=int)
    resize_parser.add_argument("--priority")
    resize_parser.add_argument("--label", default="")
    resize_parser.add_argument("--waiter-id", default="")

    wait_parser = sub.add_parser(
        "wait",
        help="register or refresh a VM lane as a lease waiter (no-op unless rank_vm_waiters)",
    )
    add_common(wait_parser)
    wait_parser.add_argument("--id", required=True)
    wait_parser.add_argument("--cores", dest="cores_requested", type=int, required=True)
    wait_parser.add_argument("--mem-mb", type=int)
    wait_parser.add_argument("--priority", default="vm")
    wait_parser.add_argument("--kind", required=True)
    wait_parser.add_argument("--pid", type=int)
    wait_parser.add_argument("--lane", default="")
    wait_parser.add_argument("--label", default="")

    withdraw_parser = sub.add_parser("withdraw", help="remove a VM lease waiter by id")
    add_common(withdraw_parser)
    withdraw_parser.add_argument("--id", required=True)

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
