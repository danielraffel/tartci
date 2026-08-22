#!/usr/bin/env python3
"""Pure filesystem identity and reservation calculations for host leases."""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
from typing import Any


def _record_int(record: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(record.get(key, default))
    except (TypeError, ValueError):
        return default


def disk_probe(
    path_text: str,
    *,
    expected_device_id: str = "",
    expected_mount_path: str = "",
) -> dict[str, Any]:
    """Resolve an existing storage root to its device and current free space."""
    logical = pathlib.Path(path_text).expanduser()
    if not logical.is_absolute():
        logical = pathlib.Path.cwd() / logical
    logical = pathlib.Path(os.path.abspath(os.fspath(logical)))
    try:
        requested = logical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"configured disk reservation root does not exist: {path_text!r}"
        ) from exc
    if not requested.is_dir():
        raise ValueError(
            f"configured disk reservation root is not a directory: {path_text!r}"
        )
    device = requested.stat().st_dev
    mount = requested
    while mount != mount.parent:
        parent = mount.parent
        try:
            if parent.stat().st_dev != device:
                break
        except OSError:
            break
        mount = parent
    if expected_device_id and str(device) != str(expected_device_id):
        raise ValueError(
            f"disk device mismatch for {path_text!r}: expected {expected_device_id}, got {device}"
        )
    if expected_mount_path:
        try:
            expected_mount = pathlib.Path(expected_mount_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                f"expected disk mount does not exist: {expected_mount_path!r}"
            ) from exc
        if mount != expected_mount:
            raise ValueError(
                f"disk mount mismatch for {path_text!r}: expected {expected_mount}, got {mount}"
            )
    free = shutil.disk_usage(requested).free
    return {
        "device_id": str(device),
        "mount_path": str(mount),
        "reservation_path": str(requested),
        "logical_path": str(logical),
        "probe_path": str(requested),
        "free_bytes": int(free),
    }


def record_disk_bytes(record: dict[str, Any]) -> int:
    return max(0, _record_int(record, "disk_growth_bytes"))


def record_has_complete_disk_accounting(record: dict[str, Any]) -> bool:
    return bool(
        record.get("disk_device_id")
        and record.get("disk_reservation_path")
        and "disk_growth_bytes" in record
        and "disk_floor_bytes" in record
    )


def disk_identity_conflicts(
    records: list[dict[str, Any]], probe: dict[str, Any]
) -> list[dict[str, str]]:
    """Find live reservations whose known path/mount moved to another device."""
    conflicts = []
    for record in records:
        recorded_device = str(record.get("disk_device_id") or "")
        same_path = str(record.get("disk_reservation_path") or "") == str(
            probe["reservation_path"]
        )
        same_logical_path = str(record.get("disk_logical_path") or "") == str(
            probe.get("logical_path") or ""
        )
        same_mount = str(record.get("disk_mount_path") or "") == str(
            probe["mount_path"]
        )
        if recorded_device and recorded_device != probe["device_id"] and (
            same_path or same_logical_path or same_mount
        ):
            conflicts.append(
                {
                    "id": str(record.get("id") or ""),
                    "recorded_device_id": recorded_device,
                    "current_device_id": str(probe["device_id"]),
                    "reservation_path": str(record.get("disk_reservation_path") or ""),
                    "logical_path": str(record.get("disk_logical_path") or ""),
                    "mount_path": str(record.get("disk_mount_path") or ""),
                }
            )
    return sorted(conflicts, key=lambda row: row["id"])


def disk_capacity(
    records: list[dict[str, Any]],
    probe: dict[str, Any],
    requested_bytes: int,
    floor_bytes: int,
) -> dict[str, Any]:
    same_device = [
        record
        for record in records
        if str(record.get("disk_device_id") or "") == probe["device_id"]
    ]
    reserved = sum(record_disk_bytes(record) for record in same_device)
    active_floor = max(
        [_record_int(record, "disk_floor_bytes") for record in same_device] + [0]
    )
    effective_floor = max(floor_bytes, active_floor)
    required = effective_floor + reserved + requested_bytes
    return {
        "device_id": probe["device_id"],
        "mount_path": probe["mount_path"],
        "reservation_path": probe["reservation_path"],
        "free_bytes": probe["free_bytes"],
        "floor_bytes": effective_floor,
        "reserved_bytes": reserved,
        "requested_bytes": requested_bytes,
        "required_bytes": required,
        "available_after_reservations_bytes": max(0, probe["free_bytes"] - reserved),
        "reservation_count": len(same_device),
    }


def prepare_root(
    path_text: str,
    *,
    expected_mount_text: str = "",
    expected_device: str = "",
) -> None:
    """Create a provider scratch root beneath an existing authority parent."""
    target = pathlib.Path(path_text).expanduser()
    if not target.is_absolute():
        target = pathlib.Path.cwd() / target
    target = pathlib.Path(os.path.abspath(os.fspath(target)))

    parts = target.parts
    mount = None
    if expected_mount_text:
        mount = pathlib.Path(expected_mount_text).expanduser()
        if not mount.is_absolute():
            mount = pathlib.Path.cwd() / mount
        mount = pathlib.Path(os.path.abspath(os.fspath(mount)))
    elif len(parts) >= 3 and parts[1] == "Volumes":
        mount = pathlib.Path("/Volumes") / parts[2]
    if mount is not None:
        if not mount.is_dir():
            raise ValueError(
                f"refusing to create disk root through missing external mount: {mount}"
            )
        try:
            mount = mount.resolve(strict=True)
            if not os.path.ismount(mount):
                raise ValueError(
                    f"refusing to create disk root through unmounted volume path: {mount}"
                )
            mount_device = mount.stat().st_dev
            if expected_device and str(mount_device) != expected_device:
                raise ValueError(
                    f"disk device mismatch for configured root {target}: "
                    f"expected {expected_device}, got {mount_device}"
                )
        except OSError as exc:
            raise ValueError(f"cannot validate external mount {mount}: {exc}") from exc

    # A missing TMPDIR is a failed reboot/session prerequisite, not permission
    # to recreate it from /. A custom path may create only below its existing
    # immediate parent.
    home = pathlib.Path(os.path.abspath(os.path.expanduser("~")))
    tmp = pathlib.Path(os.path.abspath(os.environ.get("TMPDIR") or "/tmp"))
    if mount is not None:
        parent = mount
    else:
        try:
            target.relative_to(home)
            parent = home
        except ValueError:
            try:
                target.relative_to(tmp)
                parent = tmp
            except ValueError:
                parent = target.parent

    if not parent.is_dir():
        raise ValueError(
            f"configured disk-root authority parent is not an existing directory: {parent}"
        )
    try:
        relative_parts = target.relative_to(parent).parts
    except ValueError as exc:
        raise ValueError(
            f"configured disk root {target} is outside authority parent {parent}"
        ) from exc
    if not relative_parts:
        raise ValueError(
            f"configured disk root must be a child of its authority parent: {target}"
        )

    parent = parent.resolve(strict=True)
    physical_target = parent.joinpath(*relative_parts)
    if mount is not None:
        try:
            physical_target.relative_to(mount)
        except ValueError as exc:
            raise ValueError(
                f"configured disk root {target} is outside expected mount {mount}"
            ) from exc
    elif expected_device and str(parent.stat().st_dev) != expected_device:
        raise ValueError(
            f"disk device mismatch for configured root {target}: "
            f"expected {expected_device}, got {parent.stat().st_dev}"
        )

    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(parent, flags)
    try:
        device = os.fstat(fd).st_dev
        if mount is not None and device != mount_device:
            raise ValueError(
                f"disk device changed before creating configured root: {target}"
            )
        for name in relative_parts:
            try:
                os.mkdir(name, mode=0o755, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if os.fstat(fd).st_dev != device:
                raise ValueError(
                    f"disk device changed while creating configured root: {target}"
                )
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lease_disk.py")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare-root")
    prepare.add_argument("--path", required=True)
    prepare.add_argument("--expected-mount", default="")
    prepare.add_argument("--expected-device", default="")
    args = parser.parse_args(argv)
    try:
        prepare_root(
            args.path,
            expected_mount_text=args.expected_mount,
            expected_device=args.expected_device,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
