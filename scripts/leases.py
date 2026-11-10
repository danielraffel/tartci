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


def is_floor(record: dict[str, Any]) -> bool:
    """A floor lease is visible for accounting but charged to nobody else.

    Gate, VM and ordinary build admission ignore it entirely, so it can never
    shrink or delay them; it runs at background QoS so the scheduler, not the
    store, arbitrates the CPU it oversubscribes.
    """
    return record.get("floor") is True


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


def run(
    argv: list[str],
    *,
    env_overrides: dict[str, str | None] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
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
    for key, value in (env_overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    try:
        return subprocess.run(
            [resolved, *argv[1:]],
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {exc.timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


# `ps -o lstart` and `sysctl kern.boottime` format dates through the caller's
# locale and time zone: `LC_ALL=en_GB` prints "Thu  1 Oct", `TZ=UTC` shifts the
# hour. A start time recorded by a launchd supervisor (no TZ, C locale) must
# compare equal when read back from any shell, so every probe pins the same
# rendering. Records written before this pin came from launchd in the same
# rendering, so they still compare equal.
_STABLE_TIME_ENV: dict[str, str | None] = {"LC_ALL": "C", "LANG": "C", "TZ": None}


def pid_start(pid: int) -> str:
    proc = run(["ps", "-p", str(pid), "-o", "lstart="], env_overrides=_STABLE_TIME_ENV)
    if proc.returncode != 0:
        return ""
    return " ".join(proc.stdout.strip().split())


def host_boot_time() -> str:
    proc = run(["sysctl", "-n", "kern.boottime"], env_overrides=_STABLE_TIME_ENV)
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
    """True only when the lease owner is positively confirmed alive.

    A committed guardian is an ownership transfer, not a fallback. Otherwise
    a still-live supervisor can mask a crashed Tart/QEMU writer forever (most
    visibly for Windows KEEP_FAILED jobs). A finite guard-run wrapper also
    records its exact writer child, which remains authoritative if the wrapper
    dies while that child is still modifying the clone/overlay. Reaping uses
    owner_verdict(), which separates "dead" from "could not tell".
    """
    return owner_verdict(record, current_boot)[0] == OWNER_ALIVE


# --- proving owner death -----------------------------------------------------
#
# A lease is released by its owner, or reaped only after its owner is PROVABLY
# gone. "The identity probe did not confirm the owner" is not proof: `ps` can
# fail (spawn error under load, a timeout) while the owner keeps running, and
# reaping on that silently returns a running gate VM's cores to the pool. On a
# host that lends idle gate capacity, the next admission then oversubscribes
# the host. So every owner probe answers alive / dead / unknown, and only
# `dead` reaps. Age never reaps: a stale heartbeat is reported, not acted on.

OWNER_ALIVE = "alive"
OWNER_DEAD = "dead"
OWNER_UNKNOWN = "unknown"


def _pid_exists(pid: int) -> bool | None:
    """Kernel answer without spawning anything: True, False, or None (unknown)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def identity_verdict(
    record: dict[str, Any],
    *,
    pid_key: str,
    start_key: str,
    boot_key: str,
    current_boot: str,
    require_start: bool = False,
) -> tuple[str, str]:
    """(verdict, evidence) for one recorded process identity."""
    try:
        pid = int(record.get(pid_key))
    except (TypeError, ValueError):
        return OWNER_DEAD, f"{pid_key}_invalid"
    if pid <= 0:
        return OWNER_DEAD, f"{pid_key}_invalid"
    expected_start = " ".join(str(record.get(start_key) or "").split())
    if require_start and not expected_start:
        # An identity that was never pinned can never be proven to be ours.
        return OWNER_DEAD, f"{pid_key}_start_unrecorded"
    exists = _pid_exists(pid)
    if exists is False:
        return OWNER_DEAD, f"{pid_key}={pid} no_such_process"
    current_start = pid_start(pid)
    if not current_start:
        # The kernel may still list it while `ps` failed; ask the kernel again
        # so an owner that exited in between is still provably dead.
        if _pid_exists(pid) is False:
            return OWNER_DEAD, f"{pid_key}={pid} no_such_process"
        return OWNER_UNKNOWN, f"{pid_key}={pid} exists but start-time probe failed"
    if expected_start:
        if current_start == expected_start:
            return OWNER_ALIVE, f"{pid_key}={pid} start matches"
        return OWNER_DEAD, (
            f"{pid_key}={pid} reused: start {current_start!r} != recorded {expected_start!r}"
        )
    record_boot = str(record.get(boot_key) or "")
    if (
        record_boot
        and record_boot != "unknown"
        and current_boot != "unknown"
        and record_boot != current_boot
    ):
        return OWNER_DEAD, f"{pid_key}={pid} recorded on another boot"
    return OWNER_ALIVE, f"{pid_key}={pid} exists"


def owner_verdict(record: dict[str, Any], current_boot: str | None = None) -> tuple[str, str]:
    """Owner liveness for a lease record, under the same ownership rules as
    owner_matches(): a committed VM guardian (or its managed-child writer) owns
    the lease outright; otherwise the acquiring process does."""
    boot = current_boot if current_boot is not None else host_boot_time()
    if is_vm_kind(record.get("command_kind")) and "guardian_pid" in record:
        verdict, evidence = identity_verdict(
            record,
            pid_key="guardian_pid",
            start_key="guardian_process_start_time",
            boot_key="guardian_host_boot_time",
            current_boot=boot,
            require_start=True,
        )
        if verdict != OWNER_DEAD or record.get("guardian_mode") != "managed-child":
            return verdict, evidence
        writer, writer_evidence = identity_verdict(
            record,
            pid_key="guardian_writer_pid",
            start_key="guardian_writer_process_start_time",
            boot_key="guardian_writer_host_boot_time",
            current_boot=boot,
            require_start=True,
        )
        return writer, f"{evidence}; {writer_evidence}"
    return identity_verdict(
        record,
        pid_key="pid",
        start_key="process_start_time",
        boot_key="host_boot_time",
        current_boot=boot,
    )


TART_VM_KINDS = ("tart-macos-vm", "tart-linux-vm")


def record_vm_name(record: dict[str, Any]) -> str:
    name = str(record.get("vm_name") or "")
    if name:
        return name
    prefix = f"vm-{record.get('command_kind')}-"
    lease_id = str(record.get("id") or "")
    return lease_id[len(prefix):] if lease_id.startswith(prefix) else ""


def running_tart_vms(timeout: float = 10.0) -> set[str] | None:
    """Names of running Tart VMs; empty when Tart is absent; None when unknown."""
    tart = os.environ.get("TARTCI_TART_BIN") or host_profile.resolve_system_binary("tart")
    if tart is None and os.access("/opt/homebrew/bin/tart", os.X_OK):
        tart = "/opt/homebrew/bin/tart"
    if tart is None:
        return set()
    proc = run([tart, "list", "--format", "json"], timeout=timeout)
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    running: set[str] = set()
    for vm in payload:
        if not isinstance(vm, dict):
            continue
        state = str(vm.get("State", vm.get("state", ""))).lower()
        name = vm.get("Name") or vm.get("name")
        if name and state.startswith("run"):
            running.add(str(name))
    return running


def lease_events_file(store_dir: pathlib.Path) -> pathlib.Path:
    return store_dir / "events.jsonl"


LEASE_EVENTS_MAX_BYTES = 4 * 1024 * 1024


def journal_lease_events(store_dir: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    """Append reap/keep decisions with the caller that made them. Best effort:
    an audit write never changes a lease decision. An unproven owner is logged
    on every reclaim pass, so the journal keeps one previous generation and is
    bounded at twice LEASE_EVENTS_MAX_BYTES."""
    if not rows:
        return
    caller = {"pid": os.getpid(), "ppid": os.getppid(), "argv": sys.argv[:6]}
    path = lease_events_file(store_dir)
    try:
        if path.stat().st_size >= LEASE_EVENTS_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass
    try:
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps({"ts": iso(utcnow()), **row, "caller": caller},
                                        sort_keys=True) + "\n")
    except OSError:
        pass


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


def reclaim(
    records: list[dict[str, Any]],
    stale_secs: int,
    *,
    store_dir: pathlib.Path | None = None,
    releasing_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Split records into (active, reaped, problems).

    A record is reaped only when its owner is provably dead and, for a Tart VM
    lease, its VM is provably not running. An unknown probe keeps the record.
    `releasing_id` is the lease its own owner is releasing right now: the
    caller removes it by id, so it skips the VM inventory probe.
    """
    now = utcnow()
    boot = host_boot_time()
    active: list[dict[str, Any]] = []
    reaped: list[dict[str, Any]] = []
    problems: list[str] = []
    journal: list[dict[str, Any]] = []
    vm_inventory: set[str] | None = None
    vm_inventory_read = False
    for record in records:
        heartbeat = parse_ts(record.get("heartbeat_at"))
        stale = heartbeat is None or (now - heartbeat).total_seconds() >= stale_secs
        verdict, evidence = owner_verdict(record, boot)
        lease_id = record.get("id")
        if (
            verdict == OWNER_DEAD
            and record.get("command_kind") in TART_VM_KINDS
            and lease_id != releasing_id
        ):
            vm_name = record_vm_name(record)
            if vm_name:
                if not vm_inventory_read:
                    vm_inventory = running_tart_vms()
                    vm_inventory_read = True
                if vm_inventory is None:
                    verdict, evidence = OWNER_UNKNOWN, f"{evidence}; vm {vm_name} state unknown"
                elif vm_name in vm_inventory:
                    verdict, evidence = OWNER_UNKNOWN, f"{evidence}; vm {vm_name} still running"
                else:
                    evidence = f"{evidence}; vm {vm_name} not running"
        if verdict == OWNER_DEAD:
            reaped_record = dict(record)
            reaped_record["_reap_reason"] = "identity_mismatch"
            reaped.append(reaped_record)
            journal.append({"event": "lease_reaped", "id": lease_id,
                            "reason": "identity_mismatch", "evidence": evidence})
            continue
        if verdict == OWNER_UNKNOWN:
            problems.append(f"owner_unproven_kept:{lease_id}")
            journal.append({"event": "lease_kept_unproven", "id": lease_id,
                            "evidence": evidence})
        if stale:
            problems.append(f"stale_heartbeat_live_owner:{lease_id}")
        active.append(record)
    if store_dir is not None:
        journal_lease_events(store_dir, journal)
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
    per_job_mem = max(1, per_job_mem)
    total_mem = max(0, total_mem)
    # Memory mirror of reserved_gate_cores. The core reserve alone does not
    # protect the gate: memory is a second, independent admission axis, so a
    # non-gate lease that fits in the non-gate CORE budget can still consume the
    # RAM a gate VM needs and darken a required-gate slot. Clamped like the core
    # reserve so the non-gate class always keeps at least one compile job.
    reserved_mem = (
        int(args.reserved_gate_mem_mb)
        if getattr(args, "reserved_gate_mem_mb", None) is not None
        else int(profile.get("reserved_gate_mem_mb", 0))
    )
    reserved_mem = (
        min(max(0, reserved_mem), total_mem - per_job_mem)
        if total_mem > per_job_mem
        else 0
    )
    floor_cores = (
        int(args.agent_floor_cores)
        if getattr(args, "agent_floor_cores", None) is not None
        else int(profile.get("agent_floor_cores", 0))
    )
    floor_pool = (
        int(args.agent_floor_pool_cores)
        if getattr(args, "agent_floor_pool_cores", None) is not None
        else int(profile.get("agent_floor_pool_cores", floor_cores))
    )
    qos_override = getattr(args, "agent_floor_qos", None)
    floor_qos = (
        qos_override
        if qos_override in host_profile.AGENT_FLOOR_QOS_VALUES
        else str(profile.get("agent_floor_qos", host_profile.AGENT_FLOOR_QOS_DEFAULT))
    )
    floor_cores = max(0, floor_cores)
    floor_pool = max(floor_cores, floor_pool) if floor_cores else 0
    rank_override = getattr(args, "rank_vm_waiters", None)
    rank_vm_waiters = (
        rank_override == "on"
        if rank_override in ("on", "off")
        else bool(profile.get("rank_vm_waiters", False))
    )
    fresh_override = getattr(args, "waiter_fresh_secs", None)
    waiter_fresh = (
        int(fresh_override)
        if fresh_override is not None and int(fresh_override) > 0
        else int(profile.get("vm_waiter_fresh_secs", host_profile.LEASE_POLICY_DEFAULT_FRESH_SECS))
    )
    slot_override = getattr(args, "vm_slot_lending", None)
    vm_slot_lending = (
        slot_override == "on"
        if slot_override in ("on", "off")
        else bool(profile.get("vm_slot_lending", False))
    )
    cap_override = getattr(args, "macos_vm_cap", None)
    macos_vm_cap = (
        int(cap_override)
        if cap_override is not None and int(cap_override) > 0
        else int(profile.get("macos_vm_cap", 2))
    )
    lending_override = getattr(args, "dynamic_lending", None)
    dynamic_lending = (
        lending_override == "on"
        if lending_override in ("on", "off")
        else bool(profile.get("dynamic_lending", False))
    )

    def _override(name: str, key: str, default: int) -> int:
        value = getattr(args, name, None)
        return max(0, int(value)) if value is not None else int(profile.get(key, default))

    prompt_override = getattr(args, "gate_prompt_reserve_cores", None)
    return {
        "total": max(1, total),
        "dynamic_lending": int(dynamic_lending),
        "serves_gate": int(bool(profile.get("serves_gate", False))),
        "gate_prompt_reserve_cores": min(
            _override("gate_prompt_reserve_cores", "gate_prompt_reserve_cores", 0), reserved
        ),
        "gate_prompt_reserve_auto": int(
            prompt_override is None and bool(profile.get("gate_prompt_reserve_auto", False))
        ),
        "interactive_share_cores": max(
            1, _override("interactive_share_cores", "interactive_share_cores", max(1, total))
        ),
        "background_share_cores": max(
            1,
            _override(
                "background_share_cores",
                "background_share_cores",
                max(1, max(1, total) - reserved),
            ),
        ),
        "rank_vm_waiters": int(rank_vm_waiters),
        "vm_slot_lending": int(vm_slot_lending),
        "macos_vm_cap": max(1, macos_vm_cap),
        "waiter_fresh_secs": max(1, waiter_fresh),
        "agent_floor_cores": floor_cores,
        "agent_floor_pool_cores": floor_pool,
        "agent_floor_qos": floor_qos,
        "reserved_gate_cores": reserved,
        "gate_priority": gate_priority,
        "total_mem_mb": total_mem,
        "reserved_gate_mem_mb": reserved_mem,
        "per_job_mem_mb": per_job_mem,
    }


# --- build classes and dynamic gate lending ----------------------------------
#
# The static model withholds `reserved_gate_cores` (S) from every non-gate
# lease whether or not any gate work exists, so on a host whose gate lanes are
# idle those cores sit unused while an awaited build crawls. Dynamic lending
# lets an INTERACTIVE build borrow them, under one invariant: a gate lease is
# never admitted less often than under the static model.
#
#   T = lease capacity, N = T - S (the guaranteed non-gate budget),
#   G = cores held by gate-priority leases, P = cores kept free for the NEXT
#   gate job (the prompt reserve; one gate VM while the host serves gate lanes,
#   plus any live gate-priority VM lease waiter = imminent demand).
#
#   * non-gate usage beyond N is LENT. Gate admission does not count lent
#     cores, so the gate always finds its S cores exactly as before.
#   * an interactive lease may grow non-gate usage to max(N, T - G - P);
#     background and class-less leases stay within N, as before, and
#     background leases together are further capped at the background share.
#   * when a gate grant overlaps lent cores (used > T), the store PREEMPTS the
#     newest borrowers: their process trees move to background QoS
#     (`taskpolicy -b -p`), nothing is killed, and new non-gate admissions are
#     already denied by the shrunken limit. The next acquire/release/heartbeat
#     after the overlap clears moves them back (`taskpolicy -B -p`).
#
# With dynamic_lending off (the default) and no borrowed lease in the store,
# lent is always 0 and every admission is exactly the class-less rule.
BUILD_CLASSES = host_profile.BUILD_CLASSES


def is_gate_record(record: dict[str, Any], cfg: dict[str, int]) -> bool:
    return record_int(record, "priority") >= cfg["gate_priority"]


def lending_active(records: list[dict[str, Any]], cfg: dict[str, int]) -> bool:
    """Lent cores exist only while lending is on or a borrower is still live.

    Turning lending off must not strand a borrower the gate could otherwise
    preempt, so a live `borrowed` record keeps the accounting on until it ends.
    """
    return bool(cfg.get("dynamic_lending")) or any(
        record.get("borrowed") is True for record in records if not is_floor(record)
    )


# --- VM slot lending -----------------------------------------------------------
#
# The gate reserve S exists so that gate guests find their cores. A gate guest
# also needs one of the host's `macos_vm_cap` guest slots, and a non-gate guest
# (a Vellum VM) holds one for as long as it runs. When non-gate guests hold
# EVERY slot, no gate guest can start until one of them ends, so no gate VM
# can use any part of S in that state. vm_slot_lending (opt-in, [leases] in the
# fleet profile) lets the non-gate VM lease that takes the last slot use S.
#
# What it never changes:
#   * the host total T binds every lease, as before (no oversubscription);
#   * agent builds and other non-VM non-gate leases stay within N = T - S;
#   * with any slot left for a gate guest, every non-gate lease is held to N,
#     so a gate guest of any size finds exactly the cores the static model
#     guarantees it (sizes differ per lane: forge-gate leases 6 on m5, Pulp 4);
#   * any live gate-priority lease turns lending off: one that holds no macOS
#     slot (a build, a Linux VM) owns part of S, and a gate guest means the
#     slots are not all non-gate. One that ARRIVES while lending is open finds
#     only T minus the lent guests: that is the one case lending admits a gate
#     lease less often than the static model, and it is why lending is opt-in
#     per host (m5 runs no gate-priority builds or Linux VMs). The lane-side
#     guard (last_slot_yield) keeps the lending guest off the last slot while
#     a gate guest's job is queued;
#   * only `tart-macos-vm` leases count as slot holders, and only a new one
#     can be lent to: Linux and Windows VMs do not take Apple's guest slots.
# Cores used inside the lent allowance are not "lent" in the dynamic-lending
# sense: no gate grant can need them back, so nothing is preempted for them.


#: The only lease kind that occupies one of Apple's macOS guest slots. Linux
#: and Windows VMs (`tart-linux-vm`, `qemu-windows-vm`) are VMs but hold none.
MACOS_GUEST_KIND = "tart-macos-vm"


def holds_macos_slot(kind: Any) -> bool:
    return str(kind or "") == MACOS_GUEST_KIND


def is_non_gate_vm(record: dict[str, Any], cfg: dict[str, int]) -> bool:
    """A non-gate lease that holds a macOS guest slot."""
    return (
        not is_floor(record)
        and holds_macos_slot(record.get("command_kind"))
        and not is_gate_record(record, cfg)
    )


def vm_slot_lending_open(records: list[dict[str, Any]], cfg: dict[str, int], *, adding: int = 0) -> bool:
    """True when non-gate guests hold every slot (counting `adding` new ones)."""
    if not cfg.get("vm_slot_lending"):
        return False
    live = [record for record in records if not is_floor(record)]
    # Any live gate lease turns it off: one without a slot holds part of S, and
    # one with a slot means the slots are not all non-gate (a record that has
    # not been released yet is treated as the guest it was).
    if any(is_gate_record(record, cfg) for record in live):
        return False
    held = sum(1 for record in live if is_non_gate_vm(record, cfg)) + adding
    return held >= int(cfg.get("macos_vm_cap", 2))


def vm_slot_allowance(records: list[dict[str, Any]], cfg: dict[str, int]) -> int:
    """Cores of S that the live non-gate guests may use beyond N right now."""
    return int(cfg["reserved_gate_cores"]) if vm_slot_lending_open(records, cfg) else 0


def class_usage(records: list[dict[str, Any]], cfg: dict[str, int]) -> dict[str, Any]:
    """Class and lending figures over non-floor records."""
    guaranteed_limit = max(1, cfg["total"] - cfg["reserved_gate_cores"])
    gate_used = 0
    non_gate_used = 0
    by_class = {name: 0 for name in BUILD_CLASSES}
    for record in records:
        cores = record_int(record, "lease_size_cores")
        if is_gate_record(record, cfg):
            gate_used += cores
            continue
        non_gate_used += cores
        build_class = record.get("build_class")
        if build_class in by_class:
            by_class[build_class] += cores
    lent = (
        max(0, non_gate_used - guaranteed_limit - vm_slot_allowance(records, cfg))
        if lending_active(records, cfg) else 0
    )
    return {
        "dynamic_lending": bool(cfg.get("dynamic_lending")),
        "gate_used_cores": gate_used,
        "lent_cores": lent,
        "interactive_used_cores": by_class["interactive"],
        "background_used_cores": by_class["background"],
        "interactive_share_cores": int(cfg.get("interactive_share_cores", cfg["total"])),
        "background_share_cores": int(cfg.get("background_share_cores", guaranteed_limit)),
        "preempted_lease_ids": sorted(
            str(record.get("id")) for record in records if record.get("preempted") is True
        ),
    }


def gate_hint_file(store_dir: pathlib.Path) -> pathlib.Path:
    return store_dir / "gate_hint.json"


def read_gate_hint(store_dir: pathlib.Path) -> int:
    """Cores of the last gate lease this host admitted, or 0 (fail-open)."""
    try:
        data = json.loads(gate_hint_file(store_dir).read_text(encoding="utf-8"))
        cores = int(data.get("cores") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0
    return max(0, cores)


def write_gate_hint(store_dir: pathlib.Path, cores: int) -> None:
    if cores <= 0 or cores == read_gate_hint(store_dir):
        return
    path = gate_hint_file(store_dir)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps({"cores": cores, "seen_at": iso(utcnow())}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


def prompt_reserve(
    store_dir: pathlib.Path | None, cfg: dict[str, int]
) -> tuple[int, str]:
    """Effective prompt reserve P and where it came from. Under the store lock.

    auto + gate host: the size of the last gate lease admitted here (falls back
    to the profile's one-VM default). Live gate-priority VM lease waiters are
    gate demand that is about to arrive, so P is at least their total.
    """
    reserve = int(cfg.get("gate_prompt_reserve_cores", 0))
    source = "configured"
    if store_dir is not None and cfg.get("gate_prompt_reserve_auto") and cfg.get("serves_gate"):
        hint = read_gate_hint(store_dir)
        if hint:
            reserve, source = hint, "last_gate_lease"
        else:
            source = "profile_default"
    if store_dir is not None and cfg.get("rank_vm_waiters"):
        waiters, _ = load_waiters(store_dir)
        live, _ = live_waiters(waiters, int(cfg.get("waiter_fresh_secs", 90)))
        waiting = sum(
            record_int(row, "lease_size_cores")
            for row in live
            if record_int(row, "priority") >= cfg["gate_priority"]
        )
        if waiting > reserve:
            reserve, source = waiting, "gate_waiters"
    return min(max(0, reserve), int(cfg["reserved_gate_cores"])), source


def interactive_limit(
    records: list[dict[str, Any]], cfg: dict[str, int], reserve: int
) -> int:
    """How far non-gate usage may grow for an interactive lease right now."""
    guaranteed_limit = max(1, cfg["total"] - cfg["reserved_gate_cores"])
    if not cfg.get("dynamic_lending"):
        return guaranteed_limit
    gate_used = sum(
        record_int(record, "lease_size_cores")
        for record in records
        if not is_floor(record) and is_gate_record(record, cfg)
    )
    return max(guaranteed_limit, cfg["total"] - gate_used - reserve)


def reconcile_borrowers(
    records: list[dict[str, Any]], cfg: dict[str, int]
) -> list[dict[str, Any]]:
    """Mark which borrowers must yield CPU to a gate, and which may resume.

    Called under the store lock after the record set changes. Lent cores are
    attributed to interactive leases newest first (the most recent borrower
    yields first). While the host is oversubscribed (used > T, which only a
    gate grant over lent cores can cause) the borrowers covering the overlap
    are marked `preempted`; once it clears they are unmarked. Returns the QoS
    changes to apply after the records are written. Never kills anything.
    """
    live = [record for record in records if not is_floor(record)]
    total = cfg["total"]
    guaranteed_limit = max(1, total - cfg["reserved_gate_cores"])
    used = sum(record_int(record, "lease_size_cores") for record in live)
    non_gate = [record for record in live if not is_gate_record(record, cfg)]
    non_gate_used = sum(record_int(record, "lease_size_cores") for record in non_gate)
    lent = (
        max(0, non_gate_used - guaranteed_limit - vm_slot_allowance(live, cfg))
        if lending_active(live, cfg) else 0
    )
    overlap = used - total
    # Store order is admission order (records are appended), which breaks
    # created_at ties inside one second.
    borrowers = [
        record
        for _, _, record in sorted(
            (
                (str(record.get("created_at") or ""), index, record)
                for index, record in enumerate(non_gate)
                if record.get("build_class") == "interactive"
            ),
            key=lambda row: (row[0], row[1]),
            reverse=True,
        )
    ]
    to_preempt: set[str] = set()
    for record in borrowers:
        if lent <= 0 or overlap <= 0:
            break
        share = min(record_int(record, "lease_size_cores"), lent)
        lent -= share
        if share > 0:
            to_preempt.add(str(record.get("id")))
            overlap -= share
    now = iso(utcnow())
    actions: list[dict[str, Any]] = []
    for record in live:
        lease_id = str(record.get("id"))
        wanted = lease_id in to_preempt
        current = record.get("preempted") is True
        if wanted and not current:
            record["preempted"] = True
            record["preempted_at"] = now
            qos = "background"
        elif current and not wanted:
            record.pop("preempted", None)
            record.pop("preempted_at", None)
            qos = "normal"
        else:
            continue
        actions.append(
            {
                "pid": record_int(record, "pid"),
                "qos": qos,
                "summary": {"id": lease_id, "qos": qos, "pid": record_int(record, "pid")},
            }
        )
    return actions


QOS_ACTION_LOG_ENV = "TARTCI_QOS_ACTION_LOG"


def process_tree(root: int) -> list[int]:
    """root and every live descendant (ps snapshot); [] if root is not live."""
    if root <= 0:
        return []
    proc = run(["ps", "-axo", "pid=,ppid="])
    children: dict[int, list[int]] = {}
    seen_root = False
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        seen_root = seen_root or pid == root
        children.setdefault(ppid, []).append(pid)
    if not seen_root:
        return []
    tree, queue = [], [root]
    while queue:
        pid = queue.pop()
        if pid in tree:
            continue
        tree.append(pid)
        queue.extend(children.get(pid, []))
    return tree


def apply_qos_actions(actions: list[dict[str, Any]]) -> None:
    """Move each action's process tree into (or out of) background QoS.

    Best-effort and never raises: a borrower that already exited needs
    nothing, and a host without taskpolicy (Linux) keeps its accounting but
    cannot re-prioritise. New children of a re-prioritised process (ninja's
    next clang) inherit its policy. TARTCI_QOS_ACTION_LOG records the intended
    calls instead of making them (tests, dry runs).
    """
    if not actions:
        return
    log_path = os.environ.get(QOS_ACTION_LOG_ENV)
    for action in actions:
        flag = "-b" if action["qos"] == "background" else "-B"
        if log_path:
            with contextlib.suppress(OSError):
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({**action["summary"], "flag": flag}) + "\n")
            continue
        if host_profile.resolve_system_binary("taskpolicy") is None:
            continue
        for pid in process_tree(int(action["pid"])):
            proc = run(["taskpolicy", flag, "-p", str(pid)])
            if proc.returncode != 0:
                # Logged, never fatal: a process that exited between the ps
                # snapshot and this call is the common case.
                detail = (proc.stderr or "").strip().splitlines()
                print(
                    f"leases: taskpolicy {flag} -p {pid} failed rc={proc.returncode}"
                    + (f": {detail[-1]}" if detail else "")
                    + f" (lease {action['summary']['id']})",
                    file=sys.stderr,
                )


def class_available(
    records: list[dict[str, Any]], cfg: dict[str, int], reserve: int
) -> dict[str, int]:
    """Cores a new interactive / background lease could get right now (cores
    axis only; memory can still bind). What governed builds probe."""
    live = [record for record in records if not is_floor(record)]
    current = usage(live, cfg)
    total = cfg["total"]
    host_free = max(0, total - current["used_cores"])
    guaranteed_limit = max(1, total - cfg["reserved_gate_cores"])
    non_gate_used = current["non_gate_used_cores"]
    interactive = min(
        host_free,
        max(0, interactive_limit(live, cfg, reserve) - non_gate_used),
        max(0, int(cfg.get("interactive_share_cores", total))
            - int(current.get("interactive_used_cores", 0))),
    )
    background = min(
        host_free,
        max(0, guaranteed_limit - non_gate_used),
        max(0, int(cfg.get("background_share_cores", guaranteed_limit))
            - int(current.get("background_used_cores", 0))),
    )
    return {
        "interactive_available_cores": interactive,
        "background_available_cores": background,
        "gate_prompt_reserve_cores": reserve,
    }


def usage(all_records: list[dict[str, Any]], cfg: dict[str, int]) -> dict[str, Any]:
    # Floor leases are reported separately and excluded from every figure other
    # admissions read, which is what keeps them from taking gate or VM capacity.
    records = [record for record in all_records if not is_floor(record)]
    floor_records = [record for record in all_records if is_floor(record)]
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
    if "dynamic_lending" in cfg:
        result.update(class_usage(records, cfg))
    floor_pool = int(cfg.get("agent_floor_pool_cores", 0))
    if floor_pool > 0 or floor_records:
        floor_used = sum(record_int(record, "lease_size_cores") for record in floor_records)
        result.update(
            {
                "agent_floor_cores": int(cfg.get("agent_floor_cores", 0)),
                "agent_floor_pool_cores": floor_pool,
                "floor_used_cores": floor_used,
                "floor_available_cores": max(0, floor_pool - floor_used),
            }
        )
    total_mem = int(cfg.get("total_mem_mb", 0))
    if total_mem > 0:
        per_job_mem = int(cfg.get("per_job_mem_mb", host_profile.PER_COMPILE_JOB_MEM_MB))
        used_mem = sum(record_mem_mb(record, per_job_mem) for record in records)
        legacy = any(not record_has_explicit_mem(record) for record in records)
        reserved_mem = int(cfg.get("reserved_gate_mem_mb", 0))
        non_gate_mem_limit = max(per_job_mem, total_mem - reserved_mem)
        non_gate_used_mem = sum(
            record_mem_mb(record, per_job_mem)
            for record in records
            if record_int(record, "priority") < cfg["gate_priority"]
        )
        result.update(
            {
                "total_mem_mb": total_mem,
                "used_mem_mb": used_mem,
                "available_mem_mb": max(0, total_mem - used_mem),
                "per_job_mem_mb": per_job_mem,
                "reserved_gate_mem_mb": reserved_mem,
                "non_gate_limit_mem_mb": non_gate_mem_limit,
                "non_gate_used_mem_mb": non_gate_used_mem,
                "non_gate_available_mem_mb": max(0, non_gate_mem_limit - non_gate_used_mem),
                "memory_accounting": "estimated_legacy" if legacy else "explicit",
            }
        )
        if floor_records:
            result["floor_used_mem_mb"] = sum(
                record_mem_mb(record, per_job_mem) for record in floor_records
            )
    return result


def floor_grant(
    args: argparse.Namespace,
    cfg: dict[str, int],
    active: list[dict[str, Any]],
    priority: int,
    lease_size: int,
    req_mem: int,
) -> tuple[int, int] | None:
    """Size of a floor lease for a denied build request, or None.

    Only an opted-in (--allow-floor), non-gate, non-VM request qualifies. The
    size is bounded by the per-lease floor and by what is left of the host-wide
    floor pool. Memory is not oversubscribed: counting every live lease,
    including other floor leases, the grant must still fit the non-gate memory
    limit, so the gate's memory reserve is never touched.
    """
    if not getattr(args, "allow_floor", False):
        return None
    if priority >= cfg["gate_priority"] or is_vm_kind(args.kind):
        return None
    floor_cores = int(cfg.get("agent_floor_cores", 0))
    floor_pool = int(cfg.get("agent_floor_pool_cores", 0))
    if floor_cores <= 0 or floor_pool <= 0:
        return None
    floor_used = sum(record_int(r, "lease_size_cores") for r in active if is_floor(r))
    size = min(lease_size, floor_cores, floor_pool - floor_used)
    if size < 1:
        return None
    mem = max(1, req_mem * size // lease_size)
    if cfg["total_mem_mb"] > 0:
        per_job = cfg["per_job_mem_mb"]
        used_all = sum(record_mem_mb(r, per_job) for r in active)
        non_gate_all = sum(
            record_mem_mb(r, per_job)
            for r in active
            if record_int(r, "priority") < cfg["gate_priority"]
        )
        non_gate_limit = max(per_job, cfg["total_mem_mb"] - cfg["reserved_gate_mem_mb"])
        if used_all + mem > cfg["total_mem_mb"] or non_gate_all + mem > non_gate_limit:
            return None
    return size, mem


# --- VM lease waiters ---------------------------------------------------------
#
# Opt-in ([leases] rank_vm_waiters in the fleet profile; off by default). Every
# VM lane on a host races the same acquire after its own admission precheck, so
# today the FIRST caller wins whatever its priority. A lane that wants a VM
# lease may register as a waiter (priority, cores, memory, timestamp). A VM
# acquisition is then deferred while a strictly higher-priority waiter that
# fits in the host now would no longer fit once this lease is granted:
#
#   * priorities are the ones the lanes already lease at; nothing new is ranked;
#   * equal priority is not ranked, so ties stay first-come (the first acquire);
#   * work-conserving: a waiter that cannot fit now anyway blocks nobody, and a
#     lease that leaves room for the waiter is never deferred;
#   * only VM leases are ranked or deferred. Agent and other governed builds
#     never register, never wait behind a waiter and keep their whole budget;
#   * a waiter counts only while its owner process is alive and it was refreshed
#     within waiter_fresh_secs, so a dead or wandering lane blocks nothing.
#
# Waiters live in their own file under the same store lock, so no reader of
# leases.json ever sees one, and with the knob off the file is never touched.


def waiters_file(store_dir: pathlib.Path) -> pathlib.Path:
    return store_dir / "waiters.json"


def load_waiters(store_dir: pathlib.Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Waiter records, fail-open: an unreadable file ranks nobody."""
    path = waiters_file(store_dir)
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "[]")
    except (OSError, json.JSONDecodeError):
        return [], [f"waiters_unreadable:{path}"]
    if not isinstance(data, list):
        return [], [f"waiters_unreadable:{path}"]
    return [row for row in data if isinstance(row, dict)], []


def write_waiters(store_dir: pathlib.Path, waiters: list[dict[str, Any]]) -> None:
    path = waiters_file(store_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(waiters, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def live_waiters(
    waiters: list[dict[str, Any]], fresh_secs: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(live, expired). Live = owner alive with its exact start time AND seen recently."""
    now = utcnow()
    boot = host_boot_time()
    live: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    for waiter in waiters:
        seen = parse_ts(waiter.get("seen_at"))
        fresh = seen is not None and (now - seen).total_seconds() < fresh_secs
        alive = identity_matches(
            waiter,
            pid_key="pid",
            start_key="process_start_time",
            boot_key="host_boot_time",
            current_boot=boot,
            require_start=True,
        )
        (live if fresh and alive else expired).append(waiter)
    return live, expired


def admits(
    cfg: dict[str, int], records: list[dict[str, Any]], priority: int, cores: int, mem_mb: int
) -> bool:
    verdict = core_and_memory_verdict(cfg, usage(records, cfg), priority, cores, mem_mb)
    return not (
        verdict["total_exceeded"] or verdict["class_exceeded"] or verdict["mem_exceeded"]
    )


def blocking_waiter(
    cfg: dict[str, int],
    waiters: list[dict[str, Any]],
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    priority: int,
    self_waiter_id: str,
) -> dict[str, Any] | None:
    """The highest-ranked waiter this grant would displace, or None.

    A waiter displaces the grant only if it outranks it, fits in `before` (the
    host without this grant) and does not fit in `after` (with it). Disk is not
    ranked: it is one volume-wide axis both leases are admitted against anyway.
    """
    ranked = sorted(
        waiters,
        key=lambda row: (
            -record_int(row, "priority"),
            str(row.get("waiting_since") or ""),
            str(row.get("id") or ""),
        ),
    )
    for waiter in ranked:
        if self_waiter_id and str(waiter.get("id")) == self_waiter_id:
            continue
        if not is_vm_kind(waiter.get("command_kind")):
            continue
        waiter_priority = record_int(waiter, "priority")
        if waiter_priority <= priority:
            continue
        cores = record_int(waiter, "lease_size_cores")
        mem = record_int(waiter, "lease_size_mem_mb")
        if not admits(cfg, before, waiter_priority, cores, mem):
            continue
        if admits(cfg, after, waiter_priority, cores, mem):
            continue
        return waiter
    return None


def waiter_summary(waiter: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": waiter.get("id"),
        "lane": waiter.get("lane"),
        "priority": record_int(waiter, "priority"),
        "priority_class": waiter.get("priority_class"),
        "cores": record_int(waiter, "lease_size_cores"),
        "mem_mb": record_int(waiter, "lease_size_mem_mb"),
        "waiting_since": waiter.get("waiting_since"),
        "seen_at": waiter.get("seen_at"),
    }


def rank_check(
    store_dir: pathlib.Path,
    cfg: dict[str, int],
    *,
    kind: Any,
    priority: int,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    waiter_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Called under the store lock. Prunes expired waiters; returns the blocker."""
    if not cfg.get("rank_vm_waiters") or not is_vm_kind(kind):
        return None, []
    waiters, problems = load_waiters(store_dir)
    live, expired = live_waiters(waiters, int(cfg["waiter_fresh_secs"]))
    if expired:
        write_waiters(store_dir, live)
    return blocking_waiter(cfg, live, before, after, priority, waiter_id), problems


def settle_waiter(
    store_dir: pathlib.Path, cfg: dict[str, int], waiter_id: str, *, granted: bool
) -> None:
    """Under the store lock: a grant withdraws the lane's waiter, a denial refreshes it."""
    if not waiter_id or not cfg.get("rank_vm_waiters"):
        return
    waiters, problems = load_waiters(store_dir)
    if problems:
        return
    if granted:
        kept = [row for row in waiters if str(row.get("id")) != waiter_id]
        if len(kept) != len(waiters):
            write_waiters(store_dir, kept)
        return
    for row in waiters:
        if str(row.get("id")) == waiter_id:
            row["seen_at"] = iso(utcnow())
            write_waiters(store_dir, waiters)
            return


def deferral_result(
    waiter: dict[str, Any],
    *,
    lease_id: str,
    lease_size: int,
    req_mem: int,
    priority: int,
    priority_class: str,
    capacity: dict[str, Any],
    reaped: list[dict[str, Any]],
    problems: list[str],
) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "deferred_to_waiter",
        "id": lease_id,
        "exceeded_axis": {"cores": False, "memory": False, "disk": False},
        "requested_cores": lease_size,
        "requested_mem_mb": req_mem,
        "priority": priority,
        "priority_class": priority_class,
        "waiter": waiter_summary(waiter),
        "capacity": capacity,
        "reaped": reaped_summary(reaped),
        "problems": problem_summary(problems),
    }


def register_waiter(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    store_dir = pathlib.Path(args.store_dir).expanduser()
    cfg = capacity_config(args)
    if not cfg["rank_vm_waiters"]:
        return {"ok": True, "registered": False, "reason": "rank_vm_waiters_off"}, 0
    if not is_vm_kind(args.kind):
        return {"ok": False, "reason": "waiter_requires_vm_kind", "kind": args.kind}, 64
    cores = int(args.cores_requested)
    if cores < 0:
        raise ValueError("waiter cores must be non-negative")
    priority, priority_class = parse_priority(args.priority)
    mem = int(args.mem_mb) if args.mem_mb is not None else cores * cfg["per_job_mem_mb"]
    identity = process_identity(int(args.pid) if args.pid else os.getpid())
    if not identity["process_start_time"]:
        return {"ok": False, "reason": "waiter_owner_not_alive", "pid": identity["pid"]}, 64
    now = iso(utcnow())
    with locked_store(store_dir):
        waiters, problems = load_waiters(store_dir)
        live, _ = live_waiters(waiters, int(cfg["waiter_fresh_secs"]))
        previous = next((row for row in live if str(row.get("id")) == args.id), None)
        same_owner = previous is not None and (
            previous.get("pid") == identity["pid"]
            and previous.get("process_start_time") == identity["process_start_time"]
        )
        record = {
            "id": args.id,
            "lane": args.lane,
            "label": args.label,
            "command_kind": args.kind,
            "lease_size_cores": cores,
            "lease_size_mem_mb": mem,
            "priority": priority,
            "priority_class": priority_class,
            "pid": identity["pid"],
            "process_start_time": identity["process_start_time"],
            "host_boot_time": identity["host_boot_time"],
            # First-come among equals is by when the lane started waiting, so a
            # refresh keeps the original timestamp.
            "waiting_since": previous.get("waiting_since") if same_owner else now,
            "seen_at": now,
        }
        kept = [row for row in live if str(row.get("id")) != args.id]
        kept.append(record)
        write_waiters(store_dir, kept)
        ahead = [
            waiter_summary(row)
            for row in kept
            if row is not record and record_int(row, "priority") > priority
        ]
        return {
            "ok": True,
            "registered": True,
            "waiter": waiter_summary(record),
            "outranked_by": ahead,
            "problems": problem_summary(problems),
        }, 0


def withdraw_waiter(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    store_dir = pathlib.Path(args.store_dir).expanduser()
    with locked_store(store_dir):
        if not waiters_file(store_dir).exists():
            return {"ok": True, "withdrawn": None}, 0
        waiters, problems = load_waiters(store_dir)
        kept = [row for row in waiters if str(row.get("id")) != args.id]
        removed = len(kept) != len(waiters)
        if removed:
            write_waiters(store_dir, kept)
        return {
            "ok": True,
            "withdrawn": args.id if removed else None,
            "problems": problem_summary(problems),
        }, 0


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
        active, reaped, problems = reclaim(records, int(args.stale_secs), store_dir=store_dir)
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
        waiter_rows: dict[str, Any] = {}
        if cfg.get("rank_vm_waiters"):
            waiters, waiter_problems = load_waiters(store_dir)
            problems.extend(waiter_problems)
            live, _ = live_waiters(waiters, int(cfg["waiter_fresh_secs"]))
            waiter_rows["waiters"] = [
                waiter_summary(row)
                for row in sorted(
                    live,
                    key=lambda row: (-record_int(row, "priority"),
                                     str(row.get("waiting_since") or "")),
                )
            ]
        capacity = usage(active, cfg)
        if "dynamic_lending" in cfg:
            reserve, _ = prompt_reserve(store_dir, cfg)
            capacity.update(class_available(active, cfg, reserve))
        return {
            **waiter_rows,
            "schema": 3,
            "store_dir": str(store_dir),
            "mode": "provider VM runners atomically acquire host core, memory, and per-volume disk-growth leases when enabled",
            "capacity": capacity,
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
    memory_only = bool(getattr(args, "memory_only", False))
    if memory_only:
        # A parked (pre-booted, not yet serving) VM holds its guest memory and
        # its VM slot but no cores. `resize` upgrades it to a full lease.
        if lease_size != 0 or getattr(args, "mem_mb", None) is None or int(args.mem_mb) <= 0:
            raise ValueError("a memory-only lease takes --cores 0 and a positive --mem-mb")
    elif lease_size <= 0:
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
        active, reaped, problems = reclaim(records, int(args.stale_secs), store_dir=store_dir)
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
                    "disk_path": args.disk_path,
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
        home_volume = None
        if disk is not None and getattr(args, "home_floor_path", ""):
            import home_volume_floor
            home_volume = home_volume_floor.judge(
                args.home_floor_path, store_dir, time.time(),
                store_device=str(disk.get("device_id")),
                hours_to_next_pass=float(getattr(args, "home_floor_hours", 1.0) or 1.0))
            home_volume["mode"] = getattr(args, "home_floor_mode", "report") or "report"
            if home_volume["state"] == "below" and home_volume["mode"] == "report":
                # Report mode: the floor is being proven on real data before it
                # may refuse anything. Admit, and say what refuse would have done.
                home_volume["would_refuse"] = True
            elif home_volume["state"] == "below":
                # Only a NEW clone is refused here; nothing running is touched.
                write_records(store_dir, active)
                settle_waiter(
                    store_dir, cfg, str(getattr(args, "waiter_id", "") or ""), granted=False
                )
                return {
                    "ok": False,
                    "reason": "home_volume_below_floor",
                    "exceeded_axis": {"cores": False, "memory": False, "disk": True},
                    "requested_cores": lease_size,
                    "priority": priority,
                    "priority_class": priority_class,
                    "disk": disk,
                    "home_volume": home_volume,
                    "reaped": reaped_summary(reaped),
                    "problems": problem_summary(problems),
                }, 75
        current_usage = usage(active, cfg)
        build_class = getattr(args, "build_class", None)
        if build_class is not None and priority >= cfg["gate_priority"]:
            raise ValueError("--class applies to non-gate build leases only")
        reserve, reserve_source = prompt_reserve(store_dir, cfg)
        vm_slot_open = (
            holds_macos_slot(args.kind)
            and priority < cfg["gate_priority"]
            and vm_slot_lending_open(active, cfg, adding=1)
        )
        # Memory is the second admission axis. A build lease that omits --mem-mb
        # is charged its cores * per-job estimate so the axis engages even before
        # every caller passes memory explicitly. The axis is skipped entirely when
        # total_mem_mb is 0 (RAM unknown / old profile) → core-only, fail-open.
        explicit_mem = getattr(args, "mem_mb", None) is not None
        req_mem = int(args.mem_mb) if explicit_mem else lease_size * cfg["per_job_mem_mb"]
        verdict = core_and_memory_verdict(
            cfg, current_usage, priority, lease_size, req_mem,
            build_class=build_class, reserve=reserve, vm_slot_open=vm_slot_open,
        )
        min_cores = int(getattr(args, "min_cores", 0) or 0)
        requested_full = lease_size
        if (
            min_cores > 0
            and lease_size > min_cores
            and not memory_only
            and (verdict["total_exceeded"] or verdict["class_exceeded"] or verdict["mem_exceeded"])
        ):
            # Partial grant: the largest size >= --min-cores that fits every
            # axis. Memory scales with the cores actually granted.
            for size in range(lease_size - 1, min_cores - 1, -1):
                size_mem = (
                    max(1, req_mem * size // lease_size) if explicit_mem
                    else size * cfg["per_job_mem_mb"]
                )
                trial = core_and_memory_verdict(
                    cfg, current_usage, priority, size, size_mem,
                    build_class=build_class, reserve=reserve, vm_slot_open=vm_slot_open,
                )
                if not (trial["total_exceeded"] or trial["class_exceeded"] or trial["mem_exceeded"]):
                    lease_size, req_mem, verdict = size, size_mem, trial
                    break
        total_exceeded = verdict["total_exceeded"]
        class_exceeded = verdict["class_exceeded"]
        mem_exceeded = verdict["mem_exceeded"]
        mem_axis_on = verdict["mem_axis_on"]
        mem_limit = verdict["mem_limit"]
        disk_state = (
            disk_capacity(active, disk, requested_disk_bytes, disk_floor_bytes)
            if disk is not None
            else None
        )
        disk_exceeded = bool(
            disk_state is not None and disk_state["free_bytes"] < disk_state["required_bytes"]
        )
        floor = None
        # An interactive build is never handed a background-QoS floor lease:
        # someone is waiting on it. Its caller decides what to do on denial.
        if (total_exceeded or class_exceeded) and not disk_exceeded and build_class != "interactive":
            floor = floor_grant(args, cfg, active, priority, lease_size, req_mem)
        if floor is not None:
            requested_cores = lease_size
            lease_size, req_mem = floor
        elif total_exceeded or class_exceeded or mem_exceeded or disk_exceeded:
            write_records(store_dir, active)
            settle_waiter(
                store_dir, cfg, str(getattr(args, "waiter_id", "") or ""), granted=False
            )
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
                "build_class": build_class,
                "core_limit": verdict["core_limit"],
                "core_limit_class": verdict["core_limit_class"],
                "class_share_exceeded": verdict["share_exceeded"],
                "gate_prompt_reserve_cores": reserve,
                "gate_prompt_reserve_source": reserve_source,
                "memory_limit_mb": mem_limit if mem_axis_on else 0,
                "memory_limit_class": (
                    "non_gate" if priority < cfg["gate_priority"] else "host"
                ),
                "priority": priority,
                "priority_class": priority_class,
                "capacity": current_usage,
                "disk": disk_state,
                "reaped": reaped_summary(reaped),
                "problems": problem_summary(problems),
            }, 75

        waiter_id = str(getattr(args, "waiter_id", "") or "")
        if floor is None:
            hypothetical = {
                "id": lease_id,
                "lease_size_cores": lease_size,
                "lease_size_mem_mb": req_mem,
                "priority": priority,
            }
            blocker, waiter_problems = rank_check(
                store_dir,
                cfg,
                kind=args.kind,
                priority=priority,
                before=active,
                after=[*active, hypothetical],
                waiter_id=waiter_id,
            )
            problems.extend(waiter_problems)
            if blocker is not None:
                write_records(store_dir, active)
                settle_waiter(store_dir, cfg, waiter_id, granted=False)
                return deferral_result(
                    blocker,
                    lease_id=lease_id,
                    lease_size=lease_size,
                    req_mem=req_mem,
                    priority=priority,
                    priority_class=priority_class,
                    capacity=current_usage,
                    reaped=reaped,
                    problems=problems,
                ), 75

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
        if floor is not None:
            record.update(
                {"floor": True, "qos": cfg["agent_floor_qos"], "requested_cores": requested_cores}
            )
        if memory_only:
            record["memory_only"] = True
        if vm_slot_open and (
                current_usage["non_gate_used_cores"] + lease_size
                > max(1, cfg["total"] - cfg["reserved_gate_cores"])):
            # Took the last guest slot under VM slot lending: status and the
            # doctor can tell this lease from one inside the non-gate budget.
            record["vm_slot_lending"] = True
        if build_class is not None and floor is None:
            record["build_class"] = build_class
            if lease_size < requested_full:
                record["requested_cores"] = requested_full
            guaranteed_limit = max(1, cfg["total"] - cfg["reserved_gate_cores"])
            if (
                build_class == "interactive"
                and current_usage["non_gate_used_cores"] + lease_size > guaranteed_limit
            ):
                # Some of this lease sits above the guaranteed non-gate budget:
                # it is borrowed, and a gate grant may preempt it.
                record["borrowed"] = True
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
        if priority >= cfg["gate_priority"] and lease_size > 0:
            write_gate_hint(store_dir, lease_size)
        qos_actions = reconcile_borrowers(active, cfg)
        write_records(store_dir, active)
        settle_waiter(store_dir, cfg, waiter_id, granted=True)
        apply_qos_actions(qos_actions)
        return {
            "ok": True,
            "floor": floor is not None,
            "qos": cfg["agent_floor_qos"] if floor is not None else None,
            "partial": lease_size < requested_full,
            "requested_cores": requested_full,
            "qos_actions": [action["summary"] for action in qos_actions],
            "lease": record,
            "capacity": usage(active, cfg),
            # Preserve the admission-time view: reserved is the pre-existing
            # commitment and requested is this lease. Status reports the
            # post-commit aggregate separately.
            "disk": disk_state,
            "home_volume": home_volume,
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
        active, reaped, problems = reclaim(records, int(args.stale_secs), store_dir=store_dir)
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
        supervisor, evidence = identity_verdict(
            target,
            pid_key="pid",
            start_key="process_start_time",
            boot_key="host_boot_time",
            current_boot=host_boot_time(),
        )
        if supervisor == OWNER_DEAD:
            records.remove(target)
            journal_lease_events(store_dir, [{
                "event": "lease_reaped", "id": args.id,
                "reason": "guard_run_finished_supervisor_dead", "evidence": evidence,
            }])
        else:
            # Alive, or unproven: hand ownership back to the supervisor. If it
            # really is gone, the next reclaim proves that and reaps the record.
            for field in GUARDIAN_FIELDS:
                target.pop(field, None)
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


def core_and_memory_verdict(
    cfg: dict[str, int],
    current_usage: dict[str, Any],
    priority: int,
    lease_size: int,
    req_mem: int,
    *,
    build_class: str | None = None,
    reserve: int = 0,
    vm_slot_open: bool = False,
) -> dict[str, Any]:
    """Whether a lease of this size, priority and class exceeds an axis.

    `vm_slot_open` (see "VM slot lending") holds a non-gate VM lease that takes
    the host's last guest slot to the host totals instead of the non-gate
    budgets; the caller decides it from the live records.

    Cores: the host-wide total binds every lease. A gate lease does not count
    lent cores (see "build classes and dynamic gate lending"), so its check is
    never stricter than the class-less rule. A non-gate lease is held to the
    guaranteed non-gate budget N, an interactive one to its lending limit,
    and a classed one additionally to its class share.

    Memory is unchanged by classes: host total for every lease, and the static
    non-gate memory limit for non-gate leases (QoS cannot arbitrate RAM).
    """
    total = cfg["total"]
    guaranteed_limit = max(1, total - cfg["reserved_gate_cores"])
    used = current_usage["used_cores"]
    non_gate_used = current_usage["non_gate_used_cores"]
    lent = int(current_usage.get("lent_cores", 0))
    gate = priority >= cfg["gate_priority"]
    share_exceeded = False
    if gate:
        core_limit = total
        core_limit_class = "host"
        total_exceeded = (used - lent) + lease_size > total
        class_exceeded = total_exceeded
    else:
        core_limit = guaranteed_limit
        core_limit_class = "non_gate"
        if vm_slot_open:
            core_limit = total
            core_limit_class = "vm_slot_lending"
        elif build_class == "interactive" and cfg.get("dynamic_lending"):
            gate_used = int(current_usage.get("gate_used_cores", 0))
            core_limit = max(guaranteed_limit, total - gate_used - reserve)
            core_limit_class = "interactive_lending"
        total_exceeded = used + lease_size > total
        class_exceeded = non_gate_used + lease_size > core_limit
        if build_class in BUILD_CLASSES:
            share = int(cfg.get(f"{build_class}_share_cores", total))
            class_used = int(current_usage.get(f"{build_class}_used_cores", 0))
            if class_used + lease_size > share:
                share_exceeded = True
                class_exceeded = True
    mem_axis_on = cfg["total_mem_mb"] > 0
    mem_limit = cfg["total_mem_mb"]
    used_mem_for_limit = current_usage.get("used_mem_mb", 0)
    if not gate and not vm_slot_open:
        mem_limit = max(
            cfg["per_job_mem_mb"],
            cfg["total_mem_mb"] - cfg["reserved_gate_mem_mb"],
        )
        used_mem_for_limit = current_usage.get("non_gate_used_mem_mb", 0)
    total_mem_exceeded = (
        mem_axis_on
        and current_usage.get("used_mem_mb", 0) + req_mem > cfg["total_mem_mb"]
    )
    class_mem_exceeded = mem_axis_on and used_mem_for_limit + req_mem > mem_limit
    return {
        "total_exceeded": total_exceeded,
        "class_exceeded": class_exceeded,
        "share_exceeded": share_exceeded,
        "core_limit": core_limit,
        "core_limit_class": core_limit_class,
        "mem_exceeded": total_mem_exceeded or class_mem_exceeded,
        "mem_axis_on": mem_axis_on,
        "mem_limit": mem_limit,
    }


def resize(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Change an existing lease's size in place, atomically.

    The upgrade path of a parked warm VM: its memory-only lease becomes a full
    core lease at hand-off. Admission is decided under the same store lock the
    record is rewritten in, against every OTHER lease, so there is no window in
    which the lease is released and a competing acquisition takes its memory,
    and the lease's guardian, disk reservation and identity are untouched. A
    denial leaves the record exactly as it was (the VM stays parked).
    """
    store_dir = pathlib.Path(args.store_dir).expanduser()
    cfg = capacity_config(args)
    lease_size = int(args.cores_requested)
    if lease_size <= 0:
        raise ValueError("resize takes positive --cores")
    with locked_store(store_dir):
        records = load_records(store_dir)
        active, reaped, problems = reclaim(records, int(args.stale_secs), store_dir=store_dir)
        record = next((row for row in active if row.get("id") == args.id), None)
        if record is None:
            write_records(store_dir, active)
            return {"ok": False, "reason": "unknown_lease", "id": args.id,
                    "reaped": reaped_summary(reaped),
                    "problems": problem_summary(problems)}, 1
        if getattr(args, "priority", None) is not None:
            priority, priority_class = parse_priority(args.priority)
        else:
            priority = record_int(record, "priority")
            priority_class = str(record.get("priority_class") or priority)
        req_mem = (
            int(args.mem_mb)
            if getattr(args, "mem_mb", None) is not None
            else record_mem_mb(record, cfg["per_job_mem_mb"])
        )
        others = [row for row in active if row.get("id") != args.id]
        current_usage = usage(others, cfg)
        vm_slot_open = (
            holds_macos_slot(record.get("command_kind"))
            and priority < cfg["gate_priority"]
            and vm_slot_lending_open(others, cfg, adding=1)
        )
        verdict = core_and_memory_verdict(cfg, current_usage, priority, lease_size, req_mem,
                                          vm_slot_open=vm_slot_open)
        core_axis = verdict["total_exceeded"] or verdict["class_exceeded"]
        if core_axis or verdict["mem_exceeded"]:
            write_records(store_dir, active)
            settle_waiter(
                store_dir, cfg, str(getattr(args, "waiter_id", "") or ""), granted=False
            )
            return {
                "ok": False,
                "reason": "capacity_exceeded" if core_axis else "memory_exceeded",
                "exceeded_axis": {"cores": core_axis, "memory": verdict["mem_exceeded"],
                                  "disk": False},
                "id": args.id,
                "requested_cores": lease_size,
                "requested_mem_mb": req_mem,
                "priority": priority,
                "capacity": current_usage,
                "reaped": reaped_summary(reaped),
                "problems": problem_summary(problems),
            }, 75
        waiter_id = str(getattr(args, "waiter_id", "") or "")
        resized = {**record, "lease_size_cores": lease_size, "lease_size_mem_mb": req_mem,
                   "priority": priority}
        blocker, waiter_problems = rank_check(
            store_dir, cfg, kind=record.get("command_kind"), priority=priority,
            before=active, after=[*others, resized], waiter_id=waiter_id,
        )
        problems.extend(waiter_problems)
        if blocker is not None:
            write_records(store_dir, active)
            settle_waiter(store_dir, cfg, waiter_id, granted=False)
            return deferral_result(
                blocker, lease_id=args.id, lease_size=lease_size, req_mem=req_mem,
                priority=priority, priority_class=priority_class,
                capacity=current_usage, reaped=reaped, problems=problems,
            ), 75
        previous = {"cores": record_int(record, "lease_size_cores"),
                    "mem_mb": record_mem_mb(record, cfg["per_job_mem_mb"])}
        record.update({
            "lease_size_cores": lease_size,
            "lease_size_mem_mb": req_mem,
            "priority": priority,
            "priority_class": priority_class,
            "heartbeat_at": iso(utcnow()),
        })
        record.pop("memory_only", None)
        if getattr(args, "label", None):
            record["label"] = args.label
        if priority >= cfg["gate_priority"]:
            write_gate_hint(store_dir, lease_size)
        qos_actions = reconcile_borrowers(active, cfg)
        write_records(store_dir, active)
        settle_waiter(store_dir, cfg, waiter_id, granted=True)
        apply_qos_actions(qos_actions)
        return {
            "ok": True,
            "lease": record,
            "previous": previous,
            "capacity": usage(active, cfg),
            "reaped": reaped_summary(reaped),
            "problems": problem_summary(problems),
        }, 0


def release(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    store_dir = pathlib.Path(args.store_dir).expanduser()
    cfg = capacity_config(args)
    with locked_store(store_dir):
        records = load_records(store_dir)
        active, reaped, problems = reclaim(
            records, int(args.stale_secs), store_dir=store_dir, releasing_id=str(args.id)
        )
        kept = [record for record in active if record.get("id") != args.id]
        removed = len(kept) != len(active)
        # A VM lease is owned by its guardian (the `tart run` it exec'd into).
        # Teardown stops that VM, so by the time the supervisor releases, the
        # reclaim above has already removed the record as `identity_mismatch`.
        # The lease is gone either way; only an id in neither set is an error.
        # Reporting that case as rc=1 made all 2,670 releases on the fleet read
        # as failures and hid any real one.
        already = next((row.get("_reap_reason") for row in reaped
                        if row.get("id") == args.id), None)
        qos_actions = reconcile_borrowers(kept, cfg)
        # A borrower released while preempted keeps running (a `pulp dev` loop
        # outlives each build's lease), so its tree must leave background QoS
        # here: reconcile only sees the records that remain.
        for record in active:
            if record.get("id") == args.id and record.get("preempted") is True:
                pid = record_int(record, "pid")
                qos_actions.append(
                    {"pid": pid, "qos": "normal",
                     "summary": {"id": str(args.id), "qos": "normal", "pid": pid}}
                )
        write_records(store_dir, kept)
        apply_qos_actions(qos_actions)
        return {
            "ok": removed or already is not None,
            "released": args.id if removed else None,
            "already_reclaimed": already,
            "capacity": usage(kept, cfg),
            "reaped": reaped_summary(reaped),
            "problems": problem_summary(problems),
        }, 0 if removed or already is not None else 1


def heartbeat(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    store_dir = pathlib.Path(args.store_dir).expanduser()
    cfg = capacity_config(args)
    now = iso(utcnow())
    with locked_store(store_dir):
        records = load_records(store_dir)
        active, reaped, problems = reclaim(records, int(args.stale_secs), store_dir=store_dir)
        updated = False
        for record in active:
            if record.get("id") == args.id:
                record["heartbeat_at"] = now
                updated = True
                break
        qos_actions = reconcile_borrowers(active, cfg)
        write_records(store_dir, active)
        apply_qos_actions(qos_actions)
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
        if cap.get("total_mem_mb"):
            print(
                "        "
                f"{cap['used_mem_mb']}/{cap['total_mem_mb']} MB used "
                f"(reserved gate {cap['reserved_gate_mem_mb']})"
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


WAIT_POLL_SECS = 2.0


def acquire_with_wait(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """`acquire`, retried locally until it is granted or --wait-secs elapses.

    Only a capacity-style denial (rc 75) is retried; the lock is released
    between attempts, so a waiting build never blocks a gate's acquire. No
    remote call is made: the store is local.
    """
    deadline = time.monotonic() + max(0, int(getattr(args, "wait_secs", 0) or 0))
    attempts = 0
    while True:
        attempts += 1
        result, rc = acquire(args)
        if rc != 75 or time.monotonic() + WAIT_POLL_SECS > deadline:
            if attempts > 1:
                result["wait_attempts"] = attempts
            return result, rc
        time.sleep(WAIT_POLL_SECS)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command in ("status", "list", "reap"):
            result = status_digest(args)
            rc = 0 if not result["problems"] else 1
        elif args.command == "acquire":
            result, rc = acquire_with_wait(args)
        elif args.command == "release":
            result, rc = release(args)
        elif args.command == "resize":
            result, rc = resize(args)
        elif args.command == "heartbeat":
            result, rc = heartbeat(args)
        elif args.command == "wait":
            result, rc = register_waiter(args)
        elif args.command == "withdraw":
            result, rc = withdraw_waiter(args)
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
