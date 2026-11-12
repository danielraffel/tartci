#!/usr/bin/env python3
"""Evict gate ccache entries nobody has used for N days, from the reclaim pass.

Why this exists: the shared gate ccache (`$TARTCI_CI_CACHE/ccache`, mounted
read-write into every gate guest) has no working bound. The guests cap it at
`TARTCI_CCACHE_MAX_SIZE` (40G), which a 14 GB cache never reaches, and ccache
decides when to clean up from its per-directory size counters, which read
about 100x low on every host: m5 counted 690 files against 506,745 on disk on
2026-10-05. ccache's `StatsFile::read()` returns all-zero counters for a stats
file it cannot read and the next update writes zeros plus its delta back, and
the only recount runs inside an automatic cleanup that low counters never
start. So the cache only grows. On m5 that made the pre-boot ccache guard,
which stats every entry, take 87-185 s per boot (budget exhausted on 47% of
runs) for a cache 70% of which had not been used in 14 days.

What a pass does, when every gate holds:

    ccache -d <cache> --evict-older-than <N>d

ccache itself removes entries whose mtime (refreshed on every cache hit) is
older than N days, and recounts the files and size counters of every directory
from what is on disk, which also repairs the undercount above. Nothing else is
deleted and no tartci code removes a cache file.

Gates, all required:

  * opted in: `[reclaim] gate_ccache_trim = true` in the installed fleet
    profile (off by default);
  * no Tart VM is running and no VM lease is held (`ccache_guard.host_busy`,
    the rule `ccache_guard.py reset` and `artifact-cache compact` follow),
    because every gate guest mounts this cache read-write;
  * the pre-boot guard's lock is free: the trim takes
    `<cache>-quarantine/.guard.lock` without waiting and holds it for the
    run, so a guard never walks the cache mid-eviction (a guard that finds it
    held skips, which is its existing fail-open path);
  * at most one completed eviction per `gate_ccache_trim_interval_hours`
    (default 24). A pass that cannot run (busy host, held lock) retries on the
    next hourly pass.

Why age and not size: the measured win this cache carries is the gate's
99.5% per-job hit rate (pulp #8946's per-job line), and one full gate build
reads about 30k entries. m5s ran at 99.4-99.6% per job from a 25k-entry cache
after its old entries were removed on 2026-10-04, the same rate m3 (370k) and
m5 (507k) get. A 14-day window keeps 150k entries on m5 and 184k on m3, five to
six days' working sets, so only work idle for two weeks recompiles. A size cap
would act through the same counters that are 100x low.

The cache is the one the macOS runners mount: `TARTCI_CI_CACHE`, else
`PULP_CI_CACHE` (the same order as `providers/tart-macos/runner.sh`), else the
profile's `[host].cache_root` (what the fleet renders into the runner plists
as `TARTCI_CI_CACHE`), else `~/.cache/pulp-ci`, each plus `/ccache`. A host
whose cache moves only has to move it for the runners.

Each eviction logs the entries and bytes on disk before and after (one walk
of the fan-out directories each; ccache's own counters are not used for
"before", because they are the undercount described above).

Opt-in per host through the installed fleet profile:

    [reclaim]
    gate_ccache_trim = true
    gate_ccache_max_age_days = 14          # optional, 3..90
    gate_ccache_trim_interval_hours = 24   # optional, 1..168
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - launchd hosts run 3.11+
    tomllib = None  # type: ignore[assignment]

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ccache_guard  # noqa: E402

DEFAULT_MAX_AGE_DAYS = 14
MIN_MAX_AGE_DAYS = 3
MAX_MAX_AGE_DAYS = 90
DEFAULT_INTERVAL_HOURS = 24
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 168
DEFAULT_CACHE_ROOT = "~/.cache/pulp-ci"
# A full eviction walk stats every entry: about 60-120 s on m5's 506k entries.
EVICT_TIMEOUT_S = 1800
STAMP_FILE = "gate-ccache-trim.json"

Runner = Callable[..., subprocess.CompletedProcess]


def validate(table: dict[str, Any]) -> list[str]:
    problems = []
    if type(table.get("gate_ccache_trim", False)) is not bool:
        problems.append("reclaim.gate_ccache_trim must be a boolean")
    age = table.get("gate_ccache_max_age_days", DEFAULT_MAX_AGE_DAYS)
    if type(age) is not int or not MIN_MAX_AGE_DAYS <= age <= MAX_MAX_AGE_DAYS:
        problems.append("reclaim.gate_ccache_max_age_days must be an integer from "
                        f"{MIN_MAX_AGE_DAYS} through {MAX_MAX_AGE_DAYS}")
    interval = table.get("gate_ccache_trim_interval_hours", DEFAULT_INTERVAL_HOURS)
    if type(interval) is not int or not MIN_INTERVAL_HOURS <= interval <= MAX_INTERVAL_HOURS:
        problems.append("reclaim.gate_ccache_trim_interval_hours must be an integer from "
                        f"{MIN_INTERVAL_HOURS} through {MAX_INTERVAL_HOURS}")
    return problems


def load_settings(profile: pathlib.Path) -> tuple[dict[str, Any] | None, str]:
    if tomllib is None:
        return None, "no tomllib (needs Python 3.11+)"
    try:
        with profile.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return None, f"no installed fleet profile at {profile}"
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"fleet profile unreadable: {exc}"
    table = data.get("reclaim")
    if not isinstance(table, dict) or table.get("gate_ccache_trim") is not True:
        return None, f"[reclaim] gate_ccache_trim is not true in {profile}"
    problems = validate(table)
    if problems:
        return None, "; ".join(problems)
    host = data.get("host") if isinstance(data.get("host"), dict) else {}
    return {
        "max_age_days": table.get("gate_ccache_max_age_days", DEFAULT_MAX_AGE_DAYS),
        "interval_hours": table.get("gate_ccache_trim_interval_hours", DEFAULT_INTERVAL_HOURS),
        "cache": cache_dir(host.get("cache_root")),
    }, "enabled"


def cache_dir(profile_cache_root: Any = None, env: dict[str, str] | None = None) -> pathlib.Path:
    """The gate ccache the runners mount: `<TARTCI_CI_CACHE>/ccache`."""
    env = os.environ if env is None else env
    root = (env.get("TARTCI_CI_CACHE") or env.get("PULP_CI_CACHE")
            or (profile_cache_root if isinstance(profile_cache_root, str) else None)
            or DEFAULT_CACHE_ROOT)
    return pathlib.Path(root).expanduser() / "ccache"


def measure(cache: pathlib.Path) -> dict[str, int]:
    """Entries and bytes on disk, from the files themselves."""
    entries = size = 0
    for entry in ccache_guard.iter_entries(cache):
        try:
            size += os.lstat(entry).st_size
        except OSError:
            continue
        entries += 1
    return {"entries": entries, "bytes": size}


def read_stamp(state_dir: pathlib.Path) -> float | None:
    try:
        value = json.loads((state_dir / STAMP_FILE).read_text()).get("completed_at")
    except (OSError, ValueError, AttributeError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def write_stamp(state_dir: pathlib.Path, record: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / f".{STAMP_FILE}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(record, sort_keys=True))
    os.replace(tmp, state_dir / STAMP_FILE)


def counters(ccache: str, cache: pathlib.Path, runner: Runner) -> dict[str, int] | None:
    """ccache's own files and size counters for the cache (read-only)."""
    try:
        proc = runner([ccache, "-d", str(cache), "--print-stats"],
                      capture_output=True, text=True, timeout=120,
                      env=ccache_guard.host_ccache_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    values = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("\t")
        if key in ("files_in_cache", "cache_size_kibibyte") and value.strip().isdigit():
            values[key] = int(value)
    return values or None


def evict(*, cache: pathlib.Path, max_age_days: int, fix: bool, ccache: str | None,
          busy_probe: Callable[[], str | None], runner: Runner,
          timeout: float = EVICT_TIMEOUT_S, measure_entries: bool = True) -> dict[str, Any]:
    """One eviction under every gate except the interval. Never raises.

    `timeout` bounds the ccache run (the lease-denial reclaim passes what is
    left of its budget). `measure_entries=False` skips the two full walks of
    the cache, which on a 400k-entry cache take minutes; that caller measures
    freed space from the volume instead.
    """
    report: dict[str, Any] = {"cache": str(cache), "max_age_days": max_age_days}
    if not cache.is_dir():
        return {**report, "status": "skipped", "reason": "cache directory does not exist"}
    if ccache is None:
        return {**report, "status": "skipped", "reason": "no ccache binary on this host"}
    busy = busy_probe()
    if busy:
        return {**report, "status": "skipped", "reason": busy}
    command = [ccache, "-d", str(cache), "--evict-older-than", f"{max_age_days}d"]
    report["command"] = command
    if not fix:
        return {**report, "status": "planned"}
    lock = ccache_guard.Lock(ccache_guard.default_quarantine_root(cache) / ".guard.lock")
    if not lock.acquire(0.0):
        return {**report, "status": "skipped", "reason": "the pre-boot ccache guard holds its lock"}
    try:
        if measure_entries:
            report["before"] = measure(cache)
        started = time.monotonic()
        try:
            proc = runner(command, capture_output=True, text=True, timeout=timeout,
                          env=ccache_guard.host_ccache_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {**report, "status": "error", "reason": f"ccache eviction failed: {exc}"}
        report["elapsed_s"] = round(time.monotonic() - started, 1)
        if proc.returncode != 0:
            return {**report, "status": "error",
                    "reason": f"ccache exited {proc.returncode}: {proc.stderr.strip()[:300]}"}
        if not measure_entries:
            return {**report, "status": "evicted"}
        report["after"] = measure(cache)
        # The eviction recounted every directory, so ccache's counters should
        # now match the walk; a mismatch here means they drifted again.
        report["counters_after"] = counters(ccache, cache, runner)
    finally:
        lock.release()
    return {**report, "status": "evicted"}


def run(*, fix: bool, profile: pathlib.Path, state_dir: pathlib.Path,
        now: float | None = None, ccache: str | None = None,
        busy_probe: Callable[[], str | None] | None = None,
        runner: Runner = subprocess.run) -> dict[str, Any]:
    """One opted-in reclaim pass. Never raises."""
    settings, why = load_settings(profile)
    if settings is None:
        return {"enabled": False, "reason": why}
    now = time.time() if now is None else now
    last = read_stamp(state_dir)
    if last is not None and now - last < settings["interval_hours"] * 3600:
        return {"enabled": True, "status": "not_due", "last_completed_at": last,
                "interval_hours": settings["interval_hours"]}
    try:
        report = evict(cache=settings["cache"], max_age_days=settings["max_age_days"],
                       fix=fix, ccache=ccache or ccache_guard.resolve_ccache(),
                       busy_probe=busy_probe or ccache_guard.host_busy, runner=runner)
    except Exception as exc:  # noqa: BLE001 - a janitor must not take the pass down
        return {"enabled": True, "status": "error", "reason": f"gate ccache trim failed: {exc}"}
    if report.get("status") == "evicted":
        write_stamp(state_dir, {"completed_at": now, "max_age_days": settings["max_age_days"],
                                "cache": str(settings["cache"]),
                                "before": report.get("before"), "after": report.get("after")})
    report["enabled"] = True
    report["interval_hours"] = settings["interval_hours"]
    return report
