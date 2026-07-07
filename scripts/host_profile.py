#!/usr/bin/env python3
"""Derive a conservative host resource profile for tartci."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


VALID_ROLES = ("dedicated-builder", "dev-overflow", "light")

# Estimated resident memory of one concurrent C++ compile job. Used both to size
# the memory budget and — critically — to estimate the memory a legacy core-only
# lease record consumes when the store is mixed (see leases.py). Conservative on
# purpose: the axis exists to stop the compressor/OOM spiral, not to pack tightly.
PER_COMPILE_JOB_MEM_MB = 1536


@dataclass(frozen=True)
class RoleDefaults:
    headroom_cores: int
    agent_build_cap_cores: int
    vm_pool_cores: int
    qos: str
    watch_lock_limit: int = 1
    macos_vm_cap: int = 2
    # Memory axis (GiB). headroom is left for the OS + window server; the
    # link/LTO reserve is a FLAT per-host subtraction — link peak-RSS dwarfs the
    # compile average, and modelling it per-lease double-counts when many jobs
    # compile but only one links (Codex 2026-07-07). Both come off the cap.
    headroom_mem_gb: int = 6
    link_lto_reserve_mem_gb: int = 6


ROLE_DEFAULTS = {
    "dedicated-builder": RoleDefaults(
        headroom_cores=2,
        agent_build_cap_cores=12,
        vm_pool_cores=14,
        qos="normal",
        headroom_mem_gb=8,
        link_lto_reserve_mem_gb=8,
    ),
    "dev-overflow": RoleDefaults(
        headroom_cores=4,
        agent_build_cap_cores=6,
        # A dev-overflow host runs its VM lane at NON-gate priority (e.g. the
        # pulp-build-linux preamble VM), so it draws from the non-gate budget,
        # which is lease_capacity - reserved_gate_cores = agent_build_cap_cores.
        # vm_pool_cores must therefore stay <= agent_build_cap_cores or the VM
        # can never acquire a lease (capacity_exceeded) while the idle macOS
        # gate holds its reservation — which starves the required-gate preamble
        # fleet-wide. Keep this equal to agent_build_cap_cores.
        vm_pool_cores=6,
        qos="background",
        headroom_mem_gb=6,
        link_lto_reserve_mem_gb=6,
    ),
    "light": RoleDefaults(
        headroom_cores=4,
        agent_build_cap_cores=3,
        vm_pool_cores=3,
        qos="background",
        headroom_mem_gb=6,
        link_lto_reserve_mem_gb=4,
    ),
}


def _run_text(argv: list[str]) -> str:
    proc = subprocess.run(argv, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def detect_cores() -> int:
    env_value = os.environ.get("TARTCI_HOST_CORES")
    if env_value:
        try:
            cores = int(env_value)
            if cores > 0:
                return cores
        except ValueError:
            pass
    sysctl_value = _run_text(["sysctl", "-n", "hw.ncpu"])
    if sysctl_value:
        try:
            cores = int(sysctl_value)
            if cores > 0:
                return cores
        except ValueError:
            pass
    return max(1, os.cpu_count() or 1)


def detect_memory_mb() -> int:
    """Physical RAM in MB. TARTCI_HOST_MEM_MB overrides (tests + odd hosts);
    else sysctl hw.memsize (macOS/BSD) or _SC_PHYS_PAGES (Linux). 0 if unknown,
    which the caller treats as 'memory axis unavailable' (fail-open to cores)."""
    env_value = os.environ.get("TARTCI_HOST_MEM_MB")
    if env_value:
        try:
            mb = int(env_value)
            if mb > 0:
                return mb
        except ValueError:
            pass
    sysctl_value = _run_text(["sysctl", "-n", "hw.memsize"])
    if sysctl_value:
        try:
            byte_total = int(sysctl_value)
            if byte_total > 0:
                return byte_total // (1024 * 1024)
        except ValueError:
            pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return (pages * page_size) // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        pass
    return 0


def detect_model() -> str:
    return (
        os.environ.get("TARTCI_HOST_MODEL")
        or _run_text(["sysctl", "-n", "hw.model"])
        or platform.machine()
    )


def role_file_path(path: str | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    return Path(
        os.environ.get(
            "TARTCI_ROLE_FILE",
            str(Path.home() / ".config" / "tartci" / "role"),
        )
    ).expanduser()


def normalize_role(value: str) -> str:
    role = value.strip()
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role {role!r}; expected one of {', '.join(VALID_ROLES)}")
    return role


def resolve_role(
    *,
    explicit_role: str | None = None,
    role_file: str | None = None,
    cores: int | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    if explicit_role:
        return normalize_role(explicit_role), "argument"
    env_role = os.environ.get("TARTCI_ROLE")
    if env_role:
        return normalize_role(env_role), "environment"
    path = role_file_path(role_file)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        text = ""
    if text:
        return normalize_role(text.splitlines()[0].strip()), f"file:{path}"

    host_cores = cores if cores is not None else detect_cores()
    host_model = (model if model is not None else detect_model()).lower()
    if host_cores <= 10 or "macbook" in host_model:
        return "light", "default"
    return "dev-overflow", "default"


def _clamp_at_least(value: int, minimum: int, maximum: int) -> int:
    return min(max(value, minimum), max(minimum, maximum))


def build_profile(
    *,
    role: str | None = None,
    cores: int | None = None,
    model: str | None = None,
    role_file: str | None = None,
    memory_mb: int | None = None,
) -> dict[str, Any]:
    host_cores = cores if cores is not None else detect_cores()
    if host_cores <= 0:
        raise ValueError("cores must be positive")
    host_mem_mb = memory_mb if memory_mb is not None else detect_memory_mb()
    if host_mem_mb < 0:
        raise ValueError("memory_mb must be non-negative")
    host_model = model if model is not None else detect_model()
    resolved_role, role_source = resolve_role(
        explicit_role=role,
        role_file=role_file,
        cores=host_cores,
        model=host_model,
    )
    defaults = ROLE_DEFAULTS[resolved_role]

    headroom = _clamp_at_least(defaults.headroom_cores, 1, max(1, host_cores - 1))
    lease_capacity = max(1, host_cores - headroom)
    agent_cap = _clamp_at_least(defaults.agent_build_cap_cores, 1, lease_capacity)
    vm_pool = _clamp_at_least(defaults.vm_pool_cores, 1, lease_capacity)
    reserved_gate = max(0, lease_capacity - agent_cap)
    non_gate_capacity = max(1, lease_capacity - reserved_gate)
    runner_job_cores = agent_cap

    # Memory axis. lease_capacity_mem = physical - headroom - flat link/LTO
    # reserve. 0 when RAM can't be read → the axis stays off and admission is
    # core-only (fail-open). PULP_BUILD_MEM_BUDGET_MB feeds the pulp CLI's
    # tier-0 min(core, RAM) bound so a no-lease build is memory-bounded too.
    headroom_mem_mb = defaults.headroom_mem_gb * 1024
    link_lto_reserve_mem_mb = defaults.link_lto_reserve_mem_gb * 1024
    if host_mem_mb > 0:
        lease_capacity_mem_mb = max(
            PER_COMPILE_JOB_MEM_MB,
            host_mem_mb - headroom_mem_mb - link_lto_reserve_mem_mb,
        )
    else:
        lease_capacity_mem_mb = 0
    pulp_build_mem_budget_mb = lease_capacity_mem_mb

    return {
        "schema": 2,
        "host": {
            "hostname": platform.node(),
            "model": host_model,
        },
        "role": resolved_role,
        "role_source": role_source,
        "ncpu": host_cores,
        "headroom_cores": headroom,
        "lease_capacity_cores": lease_capacity,
        "reserved_gate_cores": reserved_gate,
        "non_gate_capacity_cores": non_gate_capacity,
        "vm_pool_cores": vm_pool,
        "runner_job_cores": runner_job_cores,
        "agent_build_cap_cores": agent_cap,
        "pulp_build_jobs": agent_cap,
        "mem_mb": host_mem_mb,
        "headroom_mem_mb": headroom_mem_mb,
        "link_lto_reserve_mem_mb": link_lto_reserve_mem_mb,
        "lease_capacity_mem_mb": lease_capacity_mem_mb,
        "per_compile_job_mem_mb": PER_COMPILE_JOB_MEM_MB,
        "pulp_build_mem_budget_mb": pulp_build_mem_budget_mb,
        "qos": defaults.qos,
        "watch_lock_limit": defaults.watch_lock_limit,
        "macos_vm_cap": defaults.macos_vm_cap,
        "notes": [
            "no mitigation yet",
            "lease_capacity_cores is the host-wide budget before any consumer opts in",
        ],
    }


def shell_exports(profile: dict[str, Any]) -> str:
    values = {
        "TARTCI_ROLE": profile["role"],
        "TARTCI_HOST_CORES": profile["ncpu"],
        "TARTCI_HEADROOM_CORES": profile["headroom_cores"],
        "TARTCI_LEASE_CAPACITY_CORES": profile["lease_capacity_cores"],
        "TARTCI_GATE_RESERVED_CORES": profile["reserved_gate_cores"],
        "TARTCI_NON_GATE_CAPACITY_CORES": profile["non_gate_capacity_cores"],
        "TARTCI_VM_POOL_CORES": profile["vm_pool_cores"],
        "TARTCI_RUNNER_JOB_CORES": profile["runner_job_cores"],
        "TARTCI_AGENT_BUILD_CAP_CORES": profile["agent_build_cap_cores"],
        "TARTCI_WATCH_LOCK_LIMIT": profile["watch_lock_limit"],
        "TARTCI_MACOS_VM_CAP": profile["macos_vm_cap"],
        "TARTCI_AGENT_QOS": profile["qos"],
        "PULP_BUILD_JOBS": profile["pulp_build_jobs"],
        "TARTCI_HOST_MEM_MB": profile["mem_mb"],
        "TARTCI_LEASE_CAPACITY_MEM_MB": profile["lease_capacity_mem_mb"],
        "TARTCI_LINK_LTO_RESERVE_MEM_MB": profile["link_lto_reserve_mem_mb"],
        "TARTCI_PER_JOB_MEM_MB": profile["per_compile_job_mem_mb"],
        "PULP_BUILD_MEM_BUDGET_MB": profile["pulp_build_mem_budget_mb"],
    }
    return "\n".join(f"{key}={value}" for key, value in values.items())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tartci host-profile")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of shell exports")
    parser.add_argument("--role", choices=VALID_ROLES, help="override host role")
    parser.add_argument("--role-file", help="role file path; defaults to ~/.config/tartci/role")
    parser.add_argument("--cores", type=int, help="override detected core count")
    parser.add_argument("--model", help="override detected host model")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    profile = build_profile(
        role=args.role,
        cores=args.cores,
        model=args.model,
        role_file=args.role_file,
    )
    if args.json:
        print(json.dumps(profile, indent=2, sort_keys=True))
    else:
        print(shell_exports(profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
