#!/usr/bin/env python3
"""Host-scoped weighted core leases for tartci."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid
from typing import Any, Iterator

import host_profile
import lease_cli
from lease_disk import (
    disk_capacity,
    disk_identity_conflicts,
    disk_probe,
    record_disk_bytes,
    record_has_complete_disk_accounting,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - tartci hosts are POSIX.
    fcntl = None  # type: ignore[assignment]


PRIORITY_CLASSES = {
    "background": 10,
    "build": 40,
    "vm": 60,
    "runner": 80,
    "gate": 100,
}


def is_vm_kind(value: Any) -> bool:
    return str(value or "").endswith("-vm")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S %z"):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            pass
    return None


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a system binary, PATH-independently. Never raises.

    Resolved through host_profile so a launchd agent's minimal PATH (no
    /usr/sbin, where `sysctl` lives) cannot turn a probe into an exception that
    denies every lease. A missing binary reports as rc=127 with empty output,
    which every caller here already treats as "probe unavailable".
    """
    resolved = host_profile.resolve_system_binary(argv[0])
    if resolved is None:
        return subprocess.CompletedProcess(argv, 127, "", "")
    env = dict(os.environ)
    env["PATH"] = host_profile.system_path()
    try:
        return subprocess.run(
            [resolved, *argv[1:]],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def pid_start(pid: int) -> str:
    proc = run(["ps", "-p", str(pid), "-o", "lstart="])
    if proc.returncode != 0:
        return ""
    return " ".join(proc.stdout.strip().split())


def host_boot_time() -> str:
    proc = run(["sysctl", "-n", "kern.boottime"])
    if proc.returncode == 0 and proc.stdout.strip():
        return " ".join(proc.stdout.strip().split())
    boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id")
    if boot_id.exists():
        return boot_id.read_text(encoding="utf-8").strip()
    stat = pathlib.Path("/proc/stat")
    if stat.exists():
        for line in stat.read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return line.strip()
    return "unknown"


def process_identity(pid: int) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "pid": pid,
        "process_start_time": pid_start(pid),
        "host_boot_time": host_boot_time(),
        "process_group_id": None,
        "session_id": None,
    }
    try:
        identity["process_group_id"] = os.getpgid(pid)
    except OSError:
        pass
    try:
        identity["session_id"] = os.getsid(pid)
    except OSError:
        pass
    return identity


def identity_matches(
    record: dict[str, Any],
    *,
    pid_key: str,
    start_key: str,
    boot_key: str,
    current_boot: str,
    require_start: bool = False,
) -> bool:
    try:
        pid = int(record.get(pid_key))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    record_boot = str(record.get(boot_key) or "")
    if (
        record_boot
        and record_boot != "unknown"
        and current_boot != "unknown"
        and record_boot != current_boot
    ):
        return False
    current_start = pid_start(pid)
    if not current_start:
        return False
    expected_start = str(record.get(start_key) or "").strip()
    if expected_start:
        return current_start == " ".join(expected_start.split())
    return not require_start


def owner_matches(record: dict[str, Any], current_boot: str | None = None) -> bool:
    boot = current_boot if current_boot is not None else host_boot_time()
    # A committed guardian is an ownership transfer, not a fallback. Otherwise
    # a still-live supervisor can mask a crashed Tart/QEMU writer forever (most
    # visibly for Windows KEEP_FAILED jobs). A finite guard-run wrapper also
    # records its exact writer child, which remains authoritative if the wrapper
    # dies while that child is still modifying the clone/overlay.
    if is_vm_kind(record.get("command_kind")) and "guardian_pid" in record:
        if identity_matches(
            record,
            pid_key="guardian_pid",
            start_key="guardian_process_start_time",
            boot_key="guardian_host_boot_time",
            current_boot=boot,
            require_start=True,
        ):
            return True
        return record.get("guardian_mode") == "managed-child" and identity_matches(
            record,
            pid_key="guardian_writer_pid",
            start_key="guardian_writer_process_start_time",
            boot_key="guardian_writer_host_boot_time",
            current_boot=boot,
            require_start=True,
        )
    return identity_matches(
        record,
        pid_key="pid",
        start_key="process_start_time",
        boot_key="host_boot_time",
        current_boot=boot,
    )


def default_store_dir() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "TARTCI_LEASE_DIR",
            str(pathlib.Path.home() / ".tartci" / "state" / "leases"),
        )
    ).expanduser()


def store_file(store_dir: pathlib.Path) -> pathlib.Path:
    return store_dir / "leases.json"


def parse_priority(value: str | int | None) -> tuple[int, str]:
    if value is None:
        return PRIORITY_CLASSES["build"], "build"
    if isinstance(value, int):
        return value, str(value)
    text = value.strip()
    if text in PRIORITY_CLASSES:
        return PRIORITY_CLASSES[text], text
    try:
        return int(text), text
    except ValueError as exc:
        raise ValueError(
            f"invalid priority {value!r}; use an integer or one of {', '.join(PRIORITY_CLASSES)}"
        ) from exc


@contextlib.contextmanager
def locked_store(store_dir: pathlib.Path) -> Iterator[None]:
    store_dir.mkdir(parents=True, exist_ok=True)
    lock_path = store_dir / "leases.lock"
    if fcntl is not None:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return

    lock_dir = store_dir / "leases.lock.d"
    deadline = time.time() + 15
    while True:
        try:
            lock_dir.mkdir()
            (lock_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
            break
        except FileExistsError:
            try:
                holder = int((lock_dir / "pid").read_text(encoding="utf-8").strip())
            except Exception:  # noqa: BLE001
                holder = 0
            if holder and not pid_start(holder):
                with contextlib.suppress(OSError):
                    (lock_dir / "pid").unlink()
                with contextlib.suppress(OSError):
                    lock_dir.rmdir()
                continue
            if time.time() >= deadline:
                raise TimeoutError(f"timed out waiting for lease lock {lock_dir}")
            time.sleep(0.1)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            (lock_dir / "pid").unlink()
        with contextlib.suppress(OSError):
            lock_dir.rmdir()


def load_records(store_dir: pathlib.Path) -> list[dict[str, Any]]:
    path = store_file(store_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid lease store JSON in {path}") from exc
    if not isinstance(data, list):
        raise ValueError(f"invalid lease store shape in {path}: expected a list")
    if not all(isinstance(record, dict) for record in data):
        raise ValueError(f"invalid lease record in {path}: expected objects")
    return data


def write_records(store_dir: pathlib.Path, records: list[dict[str, Any]]) -> None:
    path = store_file(store_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def record_int(record: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(record.get(key) or default)
    except (TypeError, ValueError):
        return default


def record_mem_mb(record: dict[str, Any], per_job_mem_mb: int) -> int:
    """Memory a lease consumes, in MB. A new record carries an explicit
    lease_size_mem_mb. A LEGACY core-only record (pre-memory-axis) has none —
    but it is NOT free: estimate it as cores * per-job memory so a mixed store
    can't admit a memory-heavy lease on top of unaccounted old work (the
    2026-07-07 mixed-store trap). Estimation, not a zero default, is the safe
    accounting rule."""
    explicit = record_int(record, "lease_size_mem_mb", -1)
    if explicit >= 0 and "lease_size_mem_mb" in record:
        return explicit
    return record_int(record, "lease_size_cores") * per_job_mem_mb


def record_has_explicit_mem(record: dict[str, Any]) -> bool:
    return "lease_size_mem_mb" in record


def reclaim(records: list[dict[str, Any]], stale_secs: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    now = utcnow()
    boot = host_boot_time()
    active: list[dict[str, Any]] = []
    reaped: list[dict[str, Any]] = []
    problems: list[str] = []
    for record in records:
        heartbeat = parse_ts(record.get("heartbeat_at"))
        stale = heartbeat is None or (now - heartbeat).total_seconds() >= stale_secs
        same_owner = owner_matches(record, boot)
        if not same_owner:
            reaped_record = dict(record)
            reaped_record["_reap_reason"] = "identity_mismatch"
            reaped.append(reaped_record)
            continue
        if stale:
            problems.append(f"stale_heartbeat_live_owner:{record.get('id')}")
        active.append(record)
    return active, reaped, problems


def profile_for_args(args: argparse.Namespace) -> dict[str, Any]:
    return host_profile.build_profile(
        role=getattr(args, "role", None),
        cores=getattr(args, "host_cores", None),
        model=getattr(args, "model", None),
        role_file=getattr(args, "role_file", None),
    )


def capacity_config(args: argparse.Namespace) -> dict[str, int]:
    profile = profile_for_args(args)
    total = int(args.capacity) if getattr(args, "capacity", None) else int(profile["lease_capacity_cores"])
    reserved = (
        int(args.reserved_gate_cores)
        if getattr(args, "reserved_gate_cores", None) is not None
        else int(profile["reserved_gate_cores"])
    )
    reserved = min(max(0, reserved), max(0, total - 1)) if total > 1 else 0
    gate_priority = int(getattr(args, "gate_priority", PRIORITY_CLASSES["gate"]))
    # Memory axis. 0 total_mem_mb means the axis is OFF (host RAM unknown, or an
    # old host-profile without a memory budget) → admission stays core-only.
    total_mem = (
        int(args.capacity_mem_mb)
        if getattr(args, "capacity_mem_mb", None) is not None
        else int(profile.get("lease_capacity_mem_mb", 0))
    )
    per_job_mem = int(
        profile.get("per_compile_job_mem_mb", host_profile.PER_COMPILE_JOB_MEM_MB)
    )
    return {
        "total": max(1, total),
        "reserved_gate_cores": reserved,
        "gate_priority": gate_priority,
        "total_mem_mb": max(0, total_mem),
        "per_job_mem_mb": max(1, per_job_mem),
    }


def usage(records: list[dict[str, Any]], cfg: dict[str, int]) -> dict[str, Any]:
    used = sum(record_int(record, "lease_size_cores") for record in records)
    non_gate_limit = max(1, cfg["total"] - cfg["reserved_gate_cores"])
    non_gate_used = sum(
        record_int(record, "lease_size_cores")
        for record in records
        if record_int(record, "priority") < cfg["gate_priority"]
    )
    result = {
        "total_cores": cfg["total"],
        "used_cores": used,
        "available_cores": max(0, cfg["total"] - used),
        "reserved_gate_cores": cfg["reserved_gate_cores"],
        "gate_priority": cfg["gate_priority"],
        "non_gate_limit_cores": non_gate_limit,
        "non_gate_used_cores": non_gate_used,
        "non_gate_available_cores": max(0, non_gate_limit - non_gate_used),
    }
    total_mem = int(cfg.get("total_mem_mb", 0))
    if total_mem > 0:
        per_job_mem = int(cfg.get("per_job_mem_mb", host_profile.PER_COMPILE_JOB_MEM_MB))
        used_mem = sum(record_mem_mb(record, per_job_mem) for record in records)
        legacy = any(not record_has_explicit_mem(record) for record in records)
        result.update(
            {
                "total_mem_mb": total_mem,
                "used_mem_mb": used_mem,
                "available_mem_mb": max(0, total_mem - used_mem),
                "per_job_mem_mb": per_job_mem,
                "memory_accounting": "estimated_legacy" if legacy else "explicit",
            }
        )
    return result


def sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda record: (
            -record_int(record, "priority"),
            str(record.get("created_at") or ""),
            str(record.get("id") or ""),
        ),
    )


def reaped_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": record.get("id"), "reason": record.get("_reap_reason")} for record in records]


def problem_summary(problems: list[str]) -> list[str]:
    return sorted(set(problems))


def status_digest(args: argparse.Namespace | None = None) -> dict[str, Any]:
    if args is None:
        args = parse_args(["status"])
    store_dir = pathlib.Path(args.store_dir).expanduser()
    cfg = capacity_config(args)
    with locked_store(store_dir):
        records = load_records(store_dir)
        active, reaped, problems = reclaim(records, int(args.stale_secs))
        if len(active) != len(records):
            write_records(store_dir, active)
        # Keep the record set and its disk probes in one transaction. Otherwise
        # a concurrent acquire/release can pair stale reservations with current
        # free bytes and publish a state that never existed.
        disk_volumes: list[dict[str, Any]] = []
        seen_devices: set[str] = set()
        for record in active:
            device_id = str(record.get("disk_device_id") or "")
            path = str(record.get("disk_reservation_path") or "")
            if is_vm_kind(record.get("command_kind")) and not record_has_complete_disk_accounting(
                record
            ):
                problems.append(f"legacy_vm_disk_accounting_unknown:{record.get('id')}")
            if not device_id or not path or device_id in seen_devices:
                continue
            seen_devices.add(device_id)
            try:
                probe = disk_probe(
                    path,
                    expected_device_id=device_id,
                    expected_mount_path=str(record.get("disk_mount_path") or ""),
                )
                disk_volumes.append(disk_capacity(active, probe, 0, 0))
            except (OSError, ValueError) as exc:
                problems.append(f"disk_probe_failed:{device_id}:{exc}")
        return {
            "schema": 3,
            "store_dir": str(store_dir),
            "mode": "provider VM runners atomically acquire host core, memory, and per-volume disk-growth leases when enabled",
            "capacity": usage(active, cfg),
            "disk_volumes": sorted(disk_volumes, key=lambda row: row["device_id"]),
            "leases": sort_records(active),
            "reaped": reaped_summary(reaped),
            "problems": problem_summary(problems),
        }


def acquire(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    store_dir = pathlib.Path(args.store_dir).expanduser()
    cfg = capacity_config(args)
    priority, priority_class = parse_priority(args.priority)
    pid = int(args.pid) if args.pid else os.getpid()
    lease_size = int(args.cores_requested)
    if lease_size <= 0:
        raise ValueError("lease cores must be positive")
    lease_id = args.id or str(uuid.uuid4())
    now = iso(utcnow())

    if is_vm_kind(args.kind) and not getattr(args, "disk_path", None):
        return {
            "ok": False,
            "reason": "vm_disk_path_required",
            "id": lease_id,
            "kind": args.kind,
        }, 75

    disk: dict[str, Any] | None = None
    requested_disk_bytes = 0
    disk_floor_bytes = 0
    if getattr(args, "disk_path", None):
        requested_disk_mb = int(getattr(args, "disk_growth_mb", 0) or 0)
        disk_floor_mb = int(getattr(args, "disk_floor_mb", 0) or 0)
        if requested_disk_mb < 0 or disk_floor_mb < 0:
            raise ValueError("disk growth and floor must be non-negative")
        requested_disk_bytes = requested_disk_mb * 1024 * 1024
        disk_floor_bytes = disk_floor_mb * 1024 * 1024

    with locked_store(store_dir):
        records = load_records(store_dir)
        active, reaped, problems = reclaim(records, int(args.stale_secs))
        # The free-space probe is deliberately inside the same host-state lock
        # as reservation accounting and record commit. Moving it above this
        # boundary recreates the race this axis exists to close.
        if getattr(args, "disk_path", None):
            try:
                disk = disk_probe(
                    args.disk_path,
                    expected_device_id=str(
                        getattr(args, "disk_expected_device_id", "") or ""
                    ),
                    expected_mount_path=str(
                        getattr(args, "disk_expected_mount_path", "") or ""
                    ),
                )
            except (OSError, ValueError) as exc:
                write_records(store_dir, active)
                return {
                    "ok": False,
                    "reason": "disk_root_unavailable",
                    "id": lease_id,
                    "error": str(exc),
                    "reaped": reaped_summary(reaped),
                    "problems": problem_summary(problems),
                }, 75
        if any(str(record.get("id")) == lease_id for record in active):
            write_records(store_dir, active)
            return {
                "ok": False,
                "reason": "duplicate_lease_id",
                "id": lease_id,
                "reaped": reaped_summary(reaped),
                "problems": problem_summary(problems),
            }, 73
        legacy_vm_ids = [
            str(record.get("id") or "")
            for record in active
            if is_vm_kind(record.get("command_kind"))
            and not record_has_complete_disk_accounting(record)
        ]
        if disk is not None and legacy_vm_ids:
            write_records(store_dir, active)
            return {
                "ok": False,
                "reason": "legacy_vm_disk_accounting_unknown",
                "id": lease_id,
                "legacy_vm_lease_ids": sorted(legacy_vm_ids),
                "reaped": reaped_summary(reaped),
                "problems": problem_summary(problems),
            }, 75
        disk_conflicts = disk_identity_conflicts(active, disk) if disk is not None else []
        if disk_conflicts:
            write_records(store_dir, active)
            return {
                "ok": False,
                "reason": "disk_device_changed_with_live_reservations",
                "id": lease_id,
                "disk": disk,
                "conflicting_leases": disk_conflicts,
                "reaped": reaped_summary(reaped),
                "problems": problem_summary(problems),
            }, 75
        current_usage = usage(active, cfg)
        limit = cfg["total"]
        used_for_limit = current_usage["used_cores"]
        if priority < cfg["gate_priority"]:
            limit = max(1, cfg["total"] - cfg["reserved_gate_cores"])
            used_for_limit = current_usage["non_gate_used_cores"]
        total_exceeded = current_usage["used_cores"] + lease_size > cfg["total"]
        class_exceeded = used_for_limit + lease_size > limit
        # Memory is the second admission axis. A build lease that omits --mem-mb
        # is charged its cores * per-job estimate so the axis engages even before
        # every caller passes memory explicitly. The axis is skipped entirely when
        # total_mem_mb is 0 (RAM unknown / old profile) → core-only, fail-open.
        req_mem = (
            int(args.mem_mb)
            if getattr(args, "mem_mb", None) is not None
            else lease_size * cfg["per_job_mem_mb"]
        )
        mem_exceeded = (
            cfg["total_mem_mb"] > 0
            and current_usage.get("used_mem_mb", 0) + req_mem > cfg["total_mem_mb"]
        )
        disk_state = (
            disk_capacity(active, disk, requested_disk_bytes, disk_floor_bytes)
            if disk is not None
            else None
        )
        disk_exceeded = bool(
            disk_state is not None and disk_state["free_bytes"] < disk_state["required_bytes"]
        )
        if total_exceeded or class_exceeded or mem_exceeded or disk_exceeded:
            write_records(store_dir, active)
            core_axis = total_exceeded or class_exceeded
            reason = (
                "capacity_exceeded"
                if core_axis
                else "memory_exceeded"
                if mem_exceeded
                else "disk_capacity_exceeded"
            )
            return {
                "ok": False,
                "reason": reason,
                "exceeded_axis": {
                    "cores": core_axis,
                    "memory": mem_exceeded,
                    "disk": disk_exceeded,
                },
                "requested_cores": lease_size,
                "requested_mem_mb": req_mem,
                "priority": priority,
                "priority_class": priority_class,
                "capacity": current_usage,
                "disk": disk_state,
                "reaped": reaped_summary(reaped),
                "problems": problem_summary(problems),
            }, 75

        identity = process_identity(pid)
        record = {
            "id": lease_id,
            "lease_size_cores": lease_size,
            "lease_size_mem_mb": req_mem,
            "priority": priority,
            "priority_class": priority_class,
            "pid": identity["pid"],
            "process_start_time": identity["process_start_time"],
            "host_boot_time": identity["host_boot_time"],
            "process_group_id": identity["process_group_id"],
            "session_id": identity["session_id"],
            "command_kind": args.kind,
            "owner": args.owner,
            "label": args.label,
            "job_id": args.job_id,
            "vm_name": args.vm_name,
            "created_at": now,
            "heartbeat_at": now,
        }
        if disk is not None:
            record.update(
                {
                    "disk_device_id": disk["device_id"],
                    "disk_mount_path": disk["mount_path"],
                    "disk_reservation_path": disk["reservation_path"],
                    "disk_logical_path": disk["logical_path"],
                    "disk_growth_bytes": requested_disk_bytes,
                    "disk_floor_bytes": disk_floor_bytes,
                    "disk_expected_device_id": str(
                        getattr(args, "disk_expected_device_id", "") or ""
                    ),
                    "disk_expected_mount_path": str(
                        getattr(args, "disk_expected_mount_path", "") or ""
                    ),
                }
            )
        active.append(record)
        write_records(store_dir, active)
        return {
            "ok": True,
            "lease": record,
            "capacity": usage(active, cfg),
            # Preserve the admission-time view: reserved is the pre-existing
            # commitment and requested is this lease. Status reports the
            # post-commit aggregate separately.
            "disk": disk_state,
            "reaped": reaped_summary(reaped),
            "problems": problem_summary(problems),
        }, 0


GUARDIAN_FIELDS = (
    "guardian_pid",
    "guardian_process_start_time",
    "guardian_host_boot_time",
    "guardian_process_group_id",
    "guardian_session_id",
    "guardian_mode",
    "guardian_writer_pid",
    "guardian_writer_process_start_time",
    "guardian_writer_host_boot_time",
    "guardian_writer_process_group_id",
    "guardian_writer_session_id",
)


def guarded_argv(args: argparse.Namespace, command: str) -> list[str]:
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise ValueError(f"{command} requires a command after --")
    return argv


def guardian_identity_matches(
    record: dict[str, Any], identity: dict[str, Any], *, writer: bool = False
) -> bool:
    prefix = "guardian_writer_" if writer else "guardian_"
    return (
        record.get(f"{prefix}pid") == identity["pid"]
        and record.get(f"{prefix}process_start_time") == identity["process_start_time"]
        and record.get(f"{prefix}host_boot_time") == identity["host_boot_time"]
    )


def attach_guardian(
    args: argparse.Namespace, identity: dict[str, Any], *, mode: str
) -> None:
    store_dir = pathlib.Path(args.store_dir).expanduser()
    if not identity["process_start_time"]:
        raise RuntimeError("cannot prove exact VM guardian process start identity")
    with locked_store(store_dir):
        records = load_records(store_dir)
        active, reaped, problems = reclaim(records, int(args.stale_secs))
        target = next((record for record in active if record.get("id") == args.id), None)
        if target is None:
            write_records(store_dir, active)
            raise ValueError(
                f"cannot guard missing lease {args.id}; "
                f"reaped={reaped_summary(reaped)} problems={problem_summary(problems)}"
            )
        if not is_vm_kind(target.get("command_kind")):
            raise ValueError(f"lease {args.id} is not a VM lease")
        if "guardian_pid" in target:
            raise ValueError(f"lease {args.id} already has an authoritative guardian")
        target.update(
            {
                "guardian_pid": identity["pid"],
                "guardian_process_start_time": identity["process_start_time"],
                "guardian_host_boot_time": identity["host_boot_time"],
                "guardian_process_group_id": identity["process_group_id"],
                "guardian_session_id": identity["session_id"],
                "guardian_mode": mode,
            }
        )
        write_records(store_dir, active)


def attach_guardian_writer(
    args: argparse.Namespace,
    guardian: dict[str, Any],
    writer: dict[str, Any],
) -> None:
    if not writer["process_start_time"]:
        raise RuntimeError("cannot prove exact guarded writer process start identity")
    store_dir = pathlib.Path(args.store_dir).expanduser()
    with locked_store(store_dir):
        records = load_records(store_dir)
        target = next((record for record in records if record.get("id") == args.id), None)
        if target is None or not guardian_identity_matches(target, guardian):
            raise ValueError(f"guardian lost ownership of lease {args.id} before writer start")
        if target.get("guardian_mode") != "managed-child":
            raise ValueError(f"lease {args.id} is not a managed-child guardian")
        target.update(
            {
                "guardian_writer_pid": writer["pid"],
                "guardian_writer_process_start_time": writer["process_start_time"],
                "guardian_writer_host_boot_time": writer["host_boot_time"],
                "guardian_writer_process_group_id": writer["process_group_id"],
                "guardian_writer_session_id": writer["session_id"],
            }
        )
        write_records(store_dir, records)


def finish_guard_run(
    args: argparse.Namespace,
    guardian: dict[str, Any],
    writer: dict[str, Any] | None,
) -> None:
    """Return ownership to a live supervisor, or remove the completed lease."""
    store_dir = pathlib.Path(args.store_dir).expanduser()
    with locked_store(store_dir):
        records = load_records(store_dir)
        target = next((record for record in records if record.get("id") == args.id), None)
        if target is None:
            return
        if not guardian_identity_matches(target, guardian):
            raise ValueError(f"guardian lost ownership of lease {args.id} during writer run")
        if writer is not None and not guardian_identity_matches(target, writer, writer=True):
            raise ValueError(f"guarded writer identity changed for lease {args.id}")
        if identity_matches(
            target,
            pid_key="pid",
            start_key="process_start_time",
            boot_key="host_boot_time",
            current_boot=host_boot_time(),
        ):
            for field in GUARDIAN_FIELDS:
                target.pop(field, None)
        else:
            records.remove(target)
        write_records(store_dir, records)


def guard_exec(args: argparse.Namespace) -> int:
    """Atomically make this process the lease guardian, then exec the VM writer."""
    argv = guarded_argv(args, "guard-exec")
    identity = process_identity(os.getpid())
    attach_guardian(args, identity, mode="exec")
    os.execvpe(argv[0], argv, dict(os.environ))
    return 127  # pragma: no cover - exec either replaces this process or raises.


def guard_run(args: argparse.Namespace) -> int:
    """Run a finite disk writer only after exact durable ownership is recorded."""
    argv = guarded_argv(args, "guard-run")
    guardian = process_identity(os.getpid())
    attach_guardian(args, guardian, mode="managed-child")

    read_fd, write_fd = os.pipe()
    writer_pid = os.fork()
    if writer_pid == 0:  # pragma: no branch - child either execs or exits.
        os.close(write_fd)
        try:
            token = os.read(read_fd, 1)
            os.close(read_fd)
            if token != b"1":
                os._exit(126)
            os.execvpe(argv[0], argv, dict(os.environ))
        except OSError as exc:
            os.write(2, f"guard-run exec failed: {exc}\n".encode())
            os._exit(127)

    os.close(read_fd)
    writer: dict[str, Any] | None = None
    try:
        writer = process_identity(writer_pid)
        attach_guardian_writer(args, guardian, writer)
        os.write(write_fd, b"1")
    except Exception:
        os.close(write_fd)
        os.waitpid(writer_pid, 0)
        finish_guard_run(args, guardian, writer=None)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.close(write_fd)

    _, status = os.waitpid(writer_pid, 0)
    finish_guard_run(args, guardian, writer)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def release(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    store_dir = pathlib.Path(args.store_dir).expanduser()
    cfg = capacity_config(args)
    with locked_store(store_dir):
        records = load_records(store_dir)
        active, reaped, problems = reclaim(records, int(args.stale_secs))
        kept = [record for record in active if record.get("id") != args.id]
        removed = len(kept) != len(active)
        write_records(store_dir, kept)
        return {
            "ok": removed,
            "released": args.id if removed else None,
            "capacity": usage(kept, cfg),
            "reaped": reaped_summary(reaped),
            "problems": problem_summary(problems),
        }, 0 if removed else 1


def heartbeat(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    store_dir = pathlib.Path(args.store_dir).expanduser()
    cfg = capacity_config(args)
    now = iso(utcnow())
    with locked_store(store_dir):
        records = load_records(store_dir)
        active, reaped, problems = reclaim(records, int(args.stale_secs))
        updated = False
        for record in active:
            if record.get("id") == args.id:
                record["heartbeat_at"] = now
                updated = True
                break
        write_records(store_dir, active)
        return {
            "ok": updated,
            "heartbeat": args.id if updated else None,
            "capacity": usage(active, cfg),
            "reaped": reaped_summary(reaped),
            "problems": problem_summary(problems),
        }, 0 if updated else 1


def emit(result: dict[str, Any], json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if "leases" in result:
        cap = result["capacity"]
        print(
            "leases: "
            f"{cap['used_cores']}/{cap['total_cores']} cores used "
            f"(reserved gate {cap['reserved_gate_cores']})"
        )
        for record in result["leases"]:
            print(
                f"  {record.get('id')} cores={record.get('lease_size_cores')} "
                f"priority={record.get('priority')} kind={record.get('command_kind')} "
                f"owner={record.get('owner') or '-'}"
            )
        gib = 1024**3
        for disk in result.get("disk_volumes", []):
            print(
                "  disk "
                f"device={disk['device_id']} mount={disk['mount_path']} "
                f"free={disk['free_bytes'] / gib:.1f}GiB "
                f"reserved={disk['reserved_bytes'] / gib:.1f}GiB "
                f"required={disk['required_bytes'] / gib:.1f}GiB"
            )
        return
    print(json.dumps(result, sort_keys=True))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return lease_cli.parse_args(
        argv,
        default_store_dir=str(default_store_dir()),
        priority_classes=PRIORITY_CLASSES,
        valid_roles=host_profile.VALID_ROLES,
        stale_secs=int(os.environ.get("TARTCI_LEASE_STALE_SECS", "300")),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command in ("status", "list", "reap"):
            result = status_digest(args)
            rc = 0 if not result["problems"] else 1
        elif args.command == "acquire":
            result, rc = acquire(args)
        elif args.command == "release":
            result, rc = release(args)
        elif args.command == "heartbeat":
            result, rc = heartbeat(args)
        elif args.command == "guard-exec":
            return guard_exec(args)
        elif args.command == "guard-run":
            return guard_run(args)
        else:
            raise ValueError(f"unknown command {args.command}")
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": str(exc)}
        rc = 2
    emit(result, bool(getattr(args, "json", False)))
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
