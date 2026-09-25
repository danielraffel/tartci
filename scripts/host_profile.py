#!/usr/bin/env python3
"""Derive a conservative host resource profile for tartci."""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import shutil
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


# Where system binaries live. `sysctl` is in /usr/sbin, which a launchd agent's
# minimal inherited PATH routinely omits — so resolving system binaries through
# the caller's PATH makes host detection depend on who happens to invoke it. It
# must not: every consumer of this profile (the lease governor above all) treats
# a failure here as "no host budget", which denies every lease and boots no VMs.
_SYSTEM_BIN_DIRS = ("/usr/sbin", "/sbin", "/usr/bin", "/bin")


def system_path() -> str:
    """PATH for subprocesses, with the system dirs guaranteed present."""
    parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    parts.extend(dir_ for dir_ in _SYSTEM_BIN_DIRS if dir_ not in parts)
    return os.pathsep.join(parts)


def resolve_system_binary(name: str) -> str | None:
    """Absolute path to a system binary, independent of the caller's PATH.

    Shared with leases.py: any tartci code that shells out to a system binary
    from a launchd context must resolve it through here, not through PATH.
    """
    if os.path.sep in name:
        return name
    for directory in _SYSTEM_BIN_DIRS:
        candidate = os.path.join(directory, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(name, path=system_path())


def _run_text(argv: list[str]) -> str:
    """Run a system binary and return stripped stdout; "" on any failure.

    Never raises: a missing binary, a non-zero exit, or an OS error all read as
    "this probe is unavailable" so callers fall through to their next source.
    """
    resolved = resolve_system_binary(argv[0])
    if resolved is None:
        return ""
    env = dict(os.environ)
    env["PATH"] = system_path()
    try:
        proc = subprocess.run(
            [resolved, *argv[1:]],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
    except OSError:
        return ""
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


# --- agent core floor --------------------------------------------------------
#
# When the non-gate core budget is exhausted, a build lease is denied and the
# caller (Pulp's governed-build.sh) falls back to a leaseless -j2. On m3 on
# 2026-09-24 a single 12-core governed build held the whole non-gate budget
# while the 14-core gate reserve sat idle, so every other agent build on the
# host ran at -j2. The floor is an OPT-IN, per-host knob that lets such a build
# instead take a small "floor" lease that runs at background QoS and is NOT
# charged against any other lease's admission: gate and VM leases admit
# exactly as if it did not exist. CPU is the only oversubscribed axis, which is
# what background QoS arbitrates. Memory cannot be arbitrated by QoS, so the
# pool is clamped to the host's unleased memory headroom (OS headroom + the flat
# link/LTO reserve) and each floor lease is still checked against the non-gate
# memory limit. 0 (the default) keeps today's behaviour.
FLEET_PROFILE_ENV = "TARTCI_FLEET_PROFILE"
AGENT_FLOOR_KEYS = ("agent_floor_cores", "agent_floor_pool_cores")


def fleet_profile_path(path: str | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    return Path(
        os.environ.get(
            FLEET_PROFILE_ENV,
            str(Path.home() / ".config" / "tartci" / "macos-fleet-profile.toml"),
        )
    ).expanduser()


def _parse_host_ints(text: str, keys: tuple[str, ...]) -> dict[str, int]:
    """Read integer keys from the [host] table.

    tomllib is used when present; the lease store is also launched by a stock
    python3 (3.9 on these hosts), where a two-key scan of the [host] table is
    enough. A malformed value is ignored rather than raised: this runs on every
    lease admission, and a typo in an optional knob must never deny a lease.
    """
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - exercised on 3.9 hosts
        tomllib = None  # type: ignore[assignment]
    values: dict[str, Any] = {}
    if tomllib is not None:
        try:
            values = dict((tomllib.loads(text).get("host") or {}))
        except (tomllib.TOMLDecodeError, AttributeError):
            values = {}
    else:
        in_host = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if line.startswith("["):
                in_host = line == "[host]"
                continue
            if in_host and "=" in line:
                key, _, value = line.partition("=")
                if key.strip() in keys and value.strip().isdigit():
                    values[key.strip()] = int(value.strip())
    return {
        key: values[key]
        for key in keys
        if type(values.get(key)) is int and values[key] >= 0
    }


def agent_floor_settings(fleet_profile: str | None = None) -> tuple[dict[str, int], str]:
    """Return ({agent_floor_cores, agent_floor_pool_cores}, source).

    Environment variables win over the installed fleet profile so an operator
    can try the floor on one shell without reinstalling the fleet.
    """
    settings: dict[str, int] = {}
    source = "default"
    path = fleet_profile_path(fleet_profile)
    try:
        settings = _parse_host_ints(path.read_text(encoding="utf-8"), AGENT_FLOOR_KEYS)
        if settings:
            source = f"file:{path}"
    except OSError:
        settings = {}
    for key in AGENT_FLOOR_KEYS:
        raw = os.environ.get(f"TARTCI_{key.upper()}")
        if raw is not None and raw.strip().isdigit():
            settings[key] = int(raw.strip())
            source = "environment"
    return settings, source


def _clamp_at_least(value: int, minimum: int, maximum: int) -> int:
    return min(max(value, minimum), max(minimum, maximum))


def build_profile(
    *,
    role: str | None = None,
    cores: int | None = None,
    model: str | None = None,
    role_file: str | None = None,
    memory_mb: int | None = None,
    fleet_profile: str | None = None,
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

    # Memory mirror of reserved_gate_cores: the slice of the memory budget a
    # non-gate lease may not consume. Held proportional to the core reserve —
    # the gate's reserved share of the host is the same share on both axes.
    # Without it the memory axis carries no priority term at all, so a non-gate
    # build can fill the budget a gate VM needs and darken a required-gate slot
    # while the gate's reserved CORES sit idle. Clamped so non-gate work always
    # keeps at least one compile job's worth, mirroring the core reserve's own
    # "non-gate never drops below 1" clamp.
    if lease_capacity_mem_mb > PER_COMPILE_JOB_MEM_MB and reserved_gate > 0:
        reserved_gate_mem_mb = min(
            lease_capacity_mem_mb * reserved_gate // lease_capacity,
            lease_capacity_mem_mb - PER_COMPILE_JOB_MEM_MB,
        )
    else:
        reserved_gate_mem_mb = 0
    non_gate_capacity_mem_mb = max(0, lease_capacity_mem_mb - reserved_gate_mem_mb)

    floor_settings, floor_source = agent_floor_settings(fleet_profile)
    agent_floor = min(max(0, floor_settings.get("agent_floor_cores", 0)), lease_capacity)
    agent_floor_pool = floor_settings.get("agent_floor_pool_cores", agent_floor)
    agent_floor_pool = min(max(agent_floor, agent_floor_pool), lease_capacity)
    if agent_floor and host_mem_mb > 0:
        # Floor leases are invisible to other leases' admission, so their memory
        # can only come out of what no lease is ever granted.
        unleased_mem_mb = min(host_mem_mb, headroom_mem_mb + link_lto_reserve_mem_mb)
        agent_floor_pool = min(agent_floor_pool, unleased_mem_mb // PER_COMPILE_JOB_MEM_MB)
        agent_floor = min(agent_floor, agent_floor_pool)
    if agent_floor <= 0:
        agent_floor = agent_floor_pool = 0

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
        "reserved_gate_mem_mb": reserved_gate_mem_mb,
        "non_gate_capacity_mem_mb": non_gate_capacity_mem_mb,
        "per_compile_job_mem_mb": PER_COMPILE_JOB_MEM_MB,
        "pulp_build_mem_budget_mb": pulp_build_mem_budget_mb,
        "qos": defaults.qos,
        "agent_floor_cores": agent_floor,
        "agent_floor_pool_cores": agent_floor_pool,
        "agent_floor_qos": "background",
        "agent_floor_source": floor_source,
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
        "TARTCI_AGENT_FLOOR_CORES": profile["agent_floor_cores"],
        "TARTCI_AGENT_FLOOR_POOL_CORES": profile["agent_floor_pool_cores"],
        "TARTCI_AGENT_FLOOR_QOS": profile["agent_floor_qos"],
        "TARTCI_HOST_MEM_MB": profile["mem_mb"],
        "TARTCI_LEASE_CAPACITY_MEM_MB": profile["lease_capacity_mem_mb"],
        "TARTCI_GATE_RESERVED_MEM_MB": profile["reserved_gate_mem_mb"],
        "TARTCI_NON_GATE_CAPACITY_MEM_MB": profile["non_gate_capacity_mem_mb"],
        "TARTCI_LINK_LTO_RESERVE_MEM_MB": profile["link_lto_reserve_mem_mb"],
        "TARTCI_PER_JOB_MEM_MB": profile["per_compile_job_mem_mb"],
        "PULP_BUILD_MEM_BUDGET_MB": profile["pulp_build_mem_budget_mb"],
    }
    return "\n".join(f"{key}={value}" for key, value in values.items())


# --- how code actually reaches this host ------------------------------------
#
# Two hosts can run the same lane from entirely different artifacts. A
# generation host execs a staged, write-stripped copy of the repo; a sealed
# host execs a Developer ID bundle that carries its own copy of the repo
# inside its signed seal. They are installed by different commands and they
# go stale independently, and nothing on either host said which it was --
# so "deploy to m3" was a reasonable conclusion to reach twice about a host
# where `fleet-macos install --apply` writes a generation nothing ever execs.
#
# Everything below is derived from the live plist. A hostname is not evidence.

FLEET_LABEL_PREFIX = "com.danielraffel.tartci.tart-runner-macos-fleet."
GENERATIONS_SEGMENT = "/.local/share/tartci-generations/"
LAUNCHER_SUFFIX = "/Contents/MacOS/tartci-launcher"
GENERATION_MANIFEST = ".tartci-support-manifest.json"
SEALED_BUNDLE_MARKER = "Contents/Resources/bundle.json"


def agents_dir() -> Path:
    return Path(os.environ.get("TARTCI_AGENTS_DIR") or Path.home() / "Library/LaunchAgents")


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def classify_delivery(arguments: list[str]) -> dict:
    """Name the delivery mechanism from a plist's ProgramArguments alone.

    The two shapes are structurally distinct, so neither needs a hostname:
    a sealed lane execs `<bundle>.app/Contents/MacOS/tartci-launcher`, a
    generation lane execs `<generations-root>/<GEN>/.tartci-launch`.
    """
    for argument in arguments:
        if argument.endswith(LAUNCHER_SUFFIX):
            return {
                "delivery": "sealed-bundle",
                "root": argument[: -len(LAUNCHER_SUFFIX)],
                "entrypoint": argument,
            }
    for argument in arguments:
        if GENERATIONS_SEGMENT in argument and argument.endswith("/.tartci-launch"):
            return {
                "delivery": "generation",
                "root": str(Path(argument).parent),
                "entrypoint": argument,
            }
    return {"delivery": "unknown", "root": None, "entrypoint": None}


def in_force_identity(classified: dict) -> dict:
    """Read the version marker out of the artifact the plist actually execs."""
    root = classified.get("root")
    if root is None:
        return {"source_commit": None, "detail": "no recognised launch entrypoint"}
    root_path = Path(root)
    if classified["delivery"] == "sealed-bundle":
        bundle = _read_json(root_path / SEALED_BUNDLE_MARKER)
        if bundle is None:
            return {
                "source_commit": None,
                "detail": f"unreadable sealed marker at {root_path / SEALED_BUNDLE_MARKER}",
            }
        lanes = _read_json(root_path / "Contents/Resources/lanes.json") or {}
        return {
            "source_commit": bundle.get("source_commit"),
            "support_manifest_sha256": bundle.get("support_manifest_sha256"),
            "profile_policy_sha256": bundle.get("profile_policy_sha256"),
            "sealed_lane_ids": sorted((lanes.get("lanes") or {}).keys()) or None,
            "detail": None,
        }
    manifest = _read_json(root_path / GENERATION_MANIFEST)
    if manifest is None:
        return {
            "source_commit": None,
            "detail": f"unreadable generation manifest at {root_path / GENERATION_MANIFEST}",
        }
    return {
        "source_commit": manifest.get("source_commit"),
        "repository": manifest.get("repository"),
        "generation": root_path.name,
        "detail": None,
    }


def repo_head(repo_root: Path) -> str | None:
    value = _run_text(["git", "-C", str(repo_root), "rev-parse", "HEAD"])
    return value or None


def staleness(in_force_commit: str | None, head: str | None, repo_root: Path) -> dict:
    """Never guess. An unknown commit and an up-to-date one are not the same."""
    if head is None:
        return {"stale": None, "detail": "repo HEAD unavailable from this checkout"}
    if in_force_commit is None:
        return {"stale": None, "detail": "no commit recorded in the running artifact"}
    if in_force_commit == head:
        return {"stale": False, "detail": None, "repo_head": head}
    behind = _run_text([
        "git", "-C", str(repo_root), "rev-list", "--count",
        f"{in_force_commit}..{head}",
    ])
    return {
        "stale": True,
        "repo_head": head,
        "behind_by": int(behind) if behind.isdigit() else None,
        "detail": (
            None if behind.isdigit()
            else "the running commit is not present in this checkout"
        ),
    }


def build_delivery_report(
    *, agents: Path | None = None, repo_root: Path | None = None
) -> dict:
    """Per-lane delivery mechanism, version in force, and staleness."""
    agents = agents or agents_dir()
    repo_root = repo_root or Path(__file__).resolve().parents[1]
    head = repo_head(repo_root)
    try:
        all_plists = sorted(path for path in agents.glob("*.plist"))
    except OSError:
        all_plists = []
    fleet_plists = [
        path for path in all_plists if path.name.startswith(FLEET_LABEL_PREFIX)
    ]
    lanes = []
    for path in fleet_plists:
        label = path.name.removesuffix(".plist")
        try:
            plist = plistlib.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            lanes.append({
                "label": label, "delivery": "unknown",
                "detail": f"unreadable plist: {exc}",
            })
            continue
        arguments = [str(item) for item in (plist.get("ProgramArguments") or [])]
        classified = classify_delivery(arguments)
        identity = in_force_identity(classified)
        sealed = classified["delivery"] == "sealed-bundle"
        lanes.append({
            "label": label,
            "delivery": classified["delivery"],
            "program_arguments": arguments,
            "artifact_root": classified["root"],
            "in_force": identity,
            "staleness": staleness(identity.get("source_commit"), head, repo_root),
            # The whole reason this report exists.
            "accepts_generation_install": None if classified["delivery"] == "unknown" else not sealed,
            "how_to_update": (
                "rebuild and re-sign the launcher bundle "
                "(scripts/build_macos_launcher.sh), then re-approve and reinstall; "
                "`tartci fleet-macos install --apply` stages a generation this "
                "lane never execs"
                if sealed else
                "`tartci fleet-macos install --apply <profile>` stages a new "
                "generation and repoints this lane at it"
                if classified["delivery"] == "generation" else
                "unrecognised launch entrypoint; inspect ProgramArguments"
            ),
        })
    return {
        "schema": 1,
        "hostname": _run_text(["hostname", "-s"]) or None,
        "agents_dir": str(agents),
        "repo_root": str(repo_root),
        "repo_head": head,
        "lanes": lanes,
        # The control for the zero case: no fleet lanes beside a nonzero plist
        # count is a host with no lanes; beside a zero count it is a directory
        # this process could not read. They must not render identically.
        "plists_seen": len(all_plists),
        "fleet_plists_seen": len(fleet_plists),
    }


def delivery_report_text(report: dict) -> str:
    lines = [
        f"host: {report['hostname'] or '?'}",
        f"repo: {report['repo_root']} @ {(report['repo_head'] or 'unknown')[:12]}",
        f"launch agents: {report['agents_dir']} "
        f"({report['fleet_plists_seen']} fleet of {report['plists_seen']} plists)",
    ]
    if not report["lanes"]:
        lines.append(
            "  no fleet lanes on this host"
            if report["plists_seen"]
            else "  BLIND: no plists readable at all in that directory"
        )
    for lane in report["lanes"]:
        identity = lane.get("in_force") or {}
        stale = lane.get("staleness") or {}
        commit = identity.get("source_commit")
        if stale.get("stale") is None:
            verdict = f"staleness unknown ({stale.get('detail')})"
        elif stale["stale"]:
            behind = stale.get("behind_by")
            verdict = (
                f"STALE, behind repo by {behind} commit(s)" if behind is not None
                else f"STALE ({stale.get('detail')})"
            )
        else:
            verdict = "current with repo HEAD"
        lines.append("")
        lines.append(f"lane {lane['label']}")
        lines.append(f"  delivery: {lane['delivery']}")
        lines.append(f"  artifact: {lane.get('artifact_root') or '-'}")
        lines.append(f"  in force: {commit or 'unknown'} ({verdict})")
        accepts = lane.get("accepts_generation_install")
        lines.append(
            "  fleet-macos install --apply: "
            + ("updates this lane" if accepts
               else "NO-OP for this lane" if accepts is False
               else "unknown")
        )
        lines.append(f"  to update: {lane['how_to_update']}")
        if identity.get("detail"):
            lines.append(f"  note: {identity['detail']}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tartci host-profile")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of shell exports")
    parser.add_argument("--role", choices=VALID_ROLES, help="override host role")
    parser.add_argument("--role-file", help="role file path; defaults to ~/.config/tartci/role")
    parser.add_argument("--cores", type=int, help="override detected core count")
    parser.add_argument("--model", help="override detected host model")
    parser.add_argument(
        "--delivery", action="store_true",
        help="report how code reaches each lane on this host, and whether it is stale",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.delivery:
        report = build_delivery_report()
        print(
            json.dumps(report, indent=2, sort_keys=True) if args.json
            else delivery_report_text(report)
        )
        return 0
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
