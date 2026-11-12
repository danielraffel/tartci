#!/usr/bin/env python3
"""Measure the CI and agent data on the boot volume, and say which path grows.

Why this exists: on 2026-10-05 m3's boot data volume held about 190 GiB of
CI, build and agent data (DerivedData, the gate ccache, agent session stores,
leaked test scratch) although everything m3 builds is meant to live on
Workshop. The boot-volume floor (disk_reclaim.py) reports free space, which
says the disk is filling but not what is filling it. This names the path.

Once a day the hourly reclaim pass measures each path below that sits on the
boot data volume (a path moved to another volume, or a symlink to one, is
recorded as off_boot and not counted) and appends one sample to
<state>/boot-usage/history.jsonl. It warns, naming paths, when

  * the measured total exceeds `boot_usage_warn_gb` (default 150), or
  * one path grew more than `boot_usage_growth_gb_per_day` (default 10)
    against the oldest sample from the last BASELINE_MAX_DAYS days that is at
    least BASELINE_MIN_HOURS old.

It only reads: nothing here moves or deletes anything. It runs only on a host
with an installed fleet profile, and is off with `boot_usage = false` in that
profile's [reclaim] table, or for one pass with TARTCI_BOOT_USAGE=0.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - launchd hosts run 3.11+
    tomllib = None  # type: ignore[assignment]

GIB = 1024 ** 3
DEFAULT_WARN_GB = 150.0
DEFAULT_GROWTH_GB_PER_DAY = 10.0
SAMPLE_INTERVAL_S = 24 * 3600
BASELINE_MIN_HOURS = 20
BASELINE_MAX_DAYS = 8
HISTORY_KEEP = 45
DU_TIMEOUT_S = 600
TOP = 3

# Home-relative paths that hold CI, build or agent data, never personal data.
HOME_PATHS = (
    ".cache/pulp-ci",
    ".cache/gh",
    ".codex",
    ".claude",
    ".agentsview",
    ".pulp",
    ".tartci",
    "Code",
    "Library/Caches/Pulp",
    "Library/Caches/go-build",
    "Library/Caches/pip-tools",
    "Library/Caches/org.swift.swiftpm",
    "Library/Developer/CoreSimulator",
    "Library/Developer/Xcode/DerivedData",
    "Library/Logs/tartci",
    "Library/Caches/ccache",   # host (non-gate) ccache: 96 GiB on m5
    ".tart",                   # Tart's default store, stale once TART_HOME moved
    "pulp-trace-work",
    "VM-Recovery",
)
# Globs under HOME, for families of directories (host Actions runners).
HOME_GLOBS = ("actions-runner*", "actions-ci")

Runner = Callable[..., subprocess.CompletedProcess]


def default_paths(user_tmp: pathlib.Path | None) -> list[pathlib.Path]:
    home = pathlib.Path.home()
    paths = [home / relative for relative in HOME_PATHS]
    for pattern in HOME_GLOBS:
        paths.extend(p for p in sorted(home.glob(pattern)) if p not in paths)
    paths.append(pathlib.Path("/private/tmp"))
    if user_tmp is not None:
        paths.append(user_tmp)
    return paths


def validate(table: dict[str, Any]) -> list[str]:
    problems = []
    if type(table.get("boot_usage", True)) is not bool:
        problems.append("reclaim.boot_usage must be a boolean")
    for key, low, high in (("boot_usage_warn_gb", 1, 10000),
                           ("boot_usage_growth_gb_per_day", 0.1, 1000)):
        value = table.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not low <= value <= high:
            problems.append(f"reclaim.{key} must be a number from {low:g} through {high:g}")
    return problems


def load_settings(profile: pathlib.Path) -> dict[str, Any]:
    """Thresholds from the profile's [reclaim] table; defaults when absent."""
    settings = {"enabled": True, "warn_gb": DEFAULT_WARN_GB,
                "growth_gb_per_day": DEFAULT_GROWTH_GB_PER_DAY, "reason": None}
    # A fleet host is one with an installed profile; anywhere else (a laptop,
    # a test) there is no CI data to watch and nothing is measured.
    if tomllib is None:
        return {**settings, "enabled": False, "reason": "no tomllib (needs Python 3.11+)"}
    try:
        with profile.open("rb") as handle:
            table = tomllib.load(handle).get("reclaim") or {}
    except FileNotFoundError:
        return {**settings, "enabled": False,
                "reason": f"no installed fleet profile at {profile}"}
    except (OSError, ValueError) as exc:
        return {**settings, "enabled": False, "reason": f"fleet profile unreadable: {exc}"}
    if not isinstance(table, dict) or validate(table):
        return settings
    if table.get("boot_usage") is False:
        return {**settings, "enabled": False, "reason": "boot_usage = false in the profile"}
    for key, name in (("boot_usage_warn_gb", "warn_gb"),
                      ("boot_usage_growth_gb_per_day", "growth_gb_per_day")):
        value = table.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            settings[name] = float(value)
    return settings


def size_bytes(path: pathlib.Path, runner: Runner = subprocess.run) -> int | None:
    """Bytes under `path` on its own filesystem (du -x), or None if unknown."""
    try:
        proc = runner(["du", "-skx", str(path)], capture_output=True, text=True,
                      timeout=DU_TIMEOUT_S, check=False)
        return int(proc.stdout.split()[0]) * 1024
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def measure(paths: list[pathlib.Path], boot_device: int,
            runner: Runner = subprocess.run, now: float | None = None) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    for path in paths:
        try:
            device = path.resolve().stat().st_dev
        except OSError:
            continue  # absent here: nothing to measure
        if device != boot_device:
            entries[str(path)] = {"state": "off_boot"}
            continue
        size = size_bytes(path.resolve(), runner)
        entries[str(path)] = ({"state": "unmeasured"} if size is None
                              else {"state": "measured", "bytes": size})
    total = sum(e.get("bytes", 0) for e in entries.values())
    return {"ts": time.time() if now is None else now, "total_bytes": total,
            "paths": entries}


def read_history(path: pathlib.Path) -> list[dict[str, Any]]:
    samples = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            sample = json.loads(line)
        except ValueError:
            continue
        if isinstance(sample, dict) and isinstance(sample.get("ts"), (int, float)):
            samples.append(sample)
    return samples


def write_history(path: pathlib.Path, samples: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text("".join(json.dumps(s, sort_keys=True) + "\n"
                           for s in samples[-HISTORY_KEEP:]))
    os.replace(tmp, path)


def baseline(samples: list[dict[str, Any]], latest: dict[str, Any]) -> dict[str, Any] | None:
    """The oldest sample old enough to give a daily rate, and young enough to matter."""
    for sample in samples:
        age = latest["ts"] - sample["ts"]
        if BASELINE_MIN_HOURS * 3600 <= age <= BASELINE_MAX_DAYS * 86400:
            return sample
    return None


def evaluate(samples: list[dict[str, Any]], warn_gb: float,
             growth_gb_per_day: float) -> list[str]:
    """Warnings for the newest sample, each naming the paths responsible."""
    if not samples:
        return []
    latest = samples[-1]
    measured = {path: entry["bytes"] for path, entry in latest["paths"].items()
                if entry.get("state") == "measured"}
    out = []
    total = latest.get("total_bytes", 0)
    if total > warn_gb * GIB:
        top = sorted(measured.items(), key=lambda kv: kv[1], reverse=True)[:TOP]
        named = ", ".join(f"{path} {size / GIB:.1f} GiB" for path, size in top)
        out.append(f"boot volume: CI/agent data {total / GIB:.0f} GiB > "
                   f"{warn_gb:g} GiB (largest: {named})")
    base = baseline(samples[:-1], latest)
    if base is not None:
        days = (latest["ts"] - base["ts"]) / 86400
        grew = []
        for path, size in measured.items():
            before = (base["paths"].get(path) or {}).get("bytes")
            if before is None:
                continue
            rate = (size - before) / days
            if rate > growth_gb_per_day * GIB:
                grew.append((rate, path))
        for rate, path in sorted(grew, reverse=True)[:TOP]:
            out.append(f"boot volume: {path} grew {rate / GIB:.1f} GiB/day "
                       f"(> {growth_gb_per_day:g})")
    return out


def run(*, state_dir: pathlib.Path, profile: pathlib.Path, boot_volume: pathlib.Path,
        user_tmp: pathlib.Path | None, runner: Runner = subprocess.run,
        now: float | None = None, paths: list[pathlib.Path] | None = None) -> dict[str, Any]:
    """Sample at most once a day, then evaluate. Never raises."""
    if os.environ.get("TARTCI_BOOT_USAGE") == "0":
        return {"enabled": False, "reason": "TARTCI_BOOT_USAGE=0"}
    settings = load_settings(profile)
    if not settings["enabled"]:
        return {"enabled": False, "reason": settings["reason"]}
    history_path = state_dir / "boot-usage" / "history.jsonl"
    now = time.time() if now is None else now
    try:
        samples = read_history(history_path)
        sampled = False
        if not samples or now - samples[-1]["ts"] >= SAMPLE_INTERVAL_S:
            device = boot_volume.stat().st_dev
            samples.append(measure(paths if paths is not None else default_paths(user_tmp),
                                   device, runner, now))
            write_history(history_path, samples)
            sampled = True
        latest = samples[-1]
        return {"enabled": True, "sampled": sampled, "sample_ts": latest["ts"],
                "total_bytes": latest.get("total_bytes"),
                "warn_gb": settings["warn_gb"],
                "growth_gb_per_day": settings["growth_gb_per_day"],
                "warnings": evaluate(samples, settings["warn_gb"],
                                     settings["growth_gb_per_day"])}
    except Exception as exc:  # noqa: BLE001 - a sensor must not take the pass down
        return {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}
