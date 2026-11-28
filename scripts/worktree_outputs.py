#!/usr/bin/env python3
"""Reap regenerable build output that agent lane worktrees leave behind.

Why this exists: on 2026-11-28 a fleet host's /Volumes/Example reached 97% because lane
worktrees under /Volumes/Example/agent-worktrees kept their `build/` and
`target/` directories after the lane ended (about 350 GiB in 3 days). At over
92% used TartCI marks the host not fleet-ready (disk_pressure), which also
broke Pulp merge-group capacity accounting. The older tiers do not reach this:
Pulp's reaper needs a merged, lineage-proven worktree, and the checkout tier
(checkout_outputs.py) only acts in hours on a volume that is already under its
free-space floor.

With `[reclaim] worktree_roots = [...]` in the installed fleet profile (default
empty: nothing changes), this pass looks under each root, to `WALK_MAXDEPTH`
levels, for directories named target, build, .build or node_modules whose
parent holds a `.git` file or directory (so they sit in a git worktree), and
removes one only when:

  * nothing in it changed for `worktree_output_idle_days` (default 3), checked
    with a niced `find ... -print -quit` that stops at the first recent entry;
    a check that fails or times out keeps the directory;
  * it is not under a `w-*` lane or a `.fleet-pass-*` working dir, unless that
    is `PROTECTED_IDLE_DAYS` (7) idle;
  * no live process owns it: a pid or lock file naming a live pid, in the
    directory or its parent, or a running command naming either path.

Any error reading evidence (a marker, the process list, a modification time,
a size, the volume usage) keeps the directory, with the reason counted in the
report's `kept`. Nothing is deleted on missing evidence.

Removal is oldest first and bounded. While the volume is under
`worktree_output_target_used_pct` (default 85) nothing is removed. Above it,
directories go until the volume is back under the target. At or above
`worktree_output_pressure_used_pct` (default 92) every eligible directory goes.
Each pass also stops at `worktree_output_max_dirs` (25), `..._max_gib` (150)
and `..._budget_seconds` (900), so it never loads the host; the next hourly
pass continues. Everything removed is regenerable by a build.

    [reclaim]
    worktree_roots = ["/Volumes/Example/agent-worktrees"]

Rollback: delete the key. Nothing else depends on it.
"""

from __future__ import annotations

import os
import pathlib
import re
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch_dirs  # noqa: E402  (remove_tree, size_bytes)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - launchd hosts run 3.11+
    tomllib = None  # type: ignore[assignment]

GIB = 1024 ** 3
ROOTS_KEY = "worktree_roots"
NAMES = frozenset({"target", "build", ".build", "node_modules"})
WALK_MAXDEPTH = 4
WALK_MAX_ENTRIES = 200_000
DEFAULTS = {
    "idle_days": 3,
    "target_used_pct": 85,
    "pressure_used_pct": 92,
    "max_dirs": 25,
    "max_gib": 150,
    "budget_seconds": 900,
}
KEYS = {
    "idle_days": "worktree_output_idle_days",
    "target_used_pct": "worktree_output_target_used_pct",
    "pressure_used_pct": "worktree_output_pressure_used_pct",
    "max_dirs": "worktree_output_max_dirs",
    "max_gib": "worktree_output_max_gib",
    "budget_seconds": "worktree_output_budget_seconds",
}
BOUNDS = {
    "idle_days": (1, 60), "target_used_pct": (1, 99), "pressure_used_pct": (1, 99),
    "max_dirs": (1, 500), "max_gib": (1, 5000), "budget_seconds": (30, 3600),
}
SETTINGS_KEYS = frozenset({ROOTS_KEY, *KEYS.values()})
PROTECTED_IDLE_DAYS = 7
FIND_TIMEOUT_S = 120
PS_TIMEOUT_S = 60
LIST_LIMIT = 20
PROTECTED_RE = re.compile(r"^(w-.*|\.fleet-pass-.*)$")

Runner = Callable[..., Any]


def validate(table: dict[str, Any]) -> list[str]:
    problems = []
    roots = table.get(ROOTS_KEY, [])
    if not isinstance(roots, list) or not all(
            isinstance(r, str) and r.startswith("/") and ".." not in r.split("/")
            and r.rstrip("/") for r in roots):
        problems.append(f"reclaim.{ROOTS_KEY} must be a list of absolute paths")
    for name, key in KEYS.items():
        value = table.get(key, DEFAULTS[name])
        low, high = BOUNDS[name]
        if type(value) is not int or not low <= value <= high:
            problems.append(f"reclaim.{key} must be an integer from {low} through {high}")
    if (type(table.get(KEYS["target_used_pct"], 85)) is int
            and type(table.get(KEYS["pressure_used_pct"], 92)) is int
            and table.get(KEYS["target_used_pct"], 85) > table.get(KEYS["pressure_used_pct"], 92)):
        problems.append("reclaim.worktree_output_target_used_pct must not exceed "
                        "worktree_output_pressure_used_pct")
    return problems


def settings_from_table(table: Any) -> tuple[Optional[dict[str, Any]], str]:
    if not isinstance(table, dict) or not table.get(ROOTS_KEY):
        return None, f"[reclaim] {ROOTS_KEY} is empty or absent"
    problems = validate(table)
    if problems:
        return None, "; ".join(problems)
    settings: dict[str, Any] = {"roots": [pathlib.Path(r) for r in table[ROOTS_KEY]]}
    for name, key in KEYS.items():
        settings[name] = table.get(key, DEFAULTS[name])
    return settings, "enabled"


def load_settings(profile: pathlib.Path) -> tuple[Optional[dict[str, Any]], str]:
    if tomllib is None:
        return None, "no tomllib (needs Python 3.11+)"
    try:
        with profile.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return None, f"no installed fleet profile at {profile}"
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"fleet profile unreadable: {exc}"
    return settings_from_table(data.get("reclaim"))


def used_percent(path: pathlib.Path) -> Optional[float]:
    """Percent of the volume in use, as df reports it; None when unknown."""
    try:
        info = os.statvfs(path)
    except OSError:
        return None
    if not info.f_blocks:
        return None
    used = info.f_blocks - info.f_bfree
    return 100.0 * used / (used + info.f_bavail) if used + info.f_bavail else None


def find_candidates(root: pathlib.Path, maxdepth: int = WALK_MAXDEPTH) -> list[pathlib.Path]:
    """Output directories under `root` whose parent holds a `.git`."""
    found: list[pathlib.Path] = []
    seen = 0
    frontier = [(root, 0)]
    while frontier:
        directory, depth = frontier.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        in_git = any(e.name == ".git" for e in entries)
        for entry in entries:
            seen += 1
            if seen > WALK_MAX_ENTRIES:
                return found
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name == ".git":
                continue
            if entry.name in NAMES:
                if in_git:
                    found.append(pathlib.Path(entry.path))
                continue  # never look inside an output directory
            if depth + 1 < maxdepth:
                frontier.append((pathlib.Path(entry.path), depth + 1))
    return found


def idle_for(path: pathlib.Path, days: float, runner: Runner = subprocess.run
             ) -> Optional[bool]:
    """True when nothing in `path` changed for `days`; None when unknown."""
    minutes = max(1, int(days * 1440))
    try:
        proc = runner(["nice", "-n", "19", "find", str(path), "-mmin", f"-{minutes}",
                       "-print", "-quit"], capture_output=True, text=True,
                      timeout=FIND_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 and not proc.stdout:
        return None
    return not proc.stdout.strip()


def live_pid(value: str) -> bool:
    try:
        pid = int(value.strip().split()[0])
    except (ValueError, IndexError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, not ours
    return True


def marker_state(path: pathlib.Path) -> Optional[str]:
    """Why `path` must be kept because of its pid or lock files, else None.

    A pid or lock file, in the directory or its parent, naming a live pid
    gives "live_pid_or_lock". Any error listing those directories or reading
    a marker gives "marker_unreadable": missing evidence keeps the directory.
    """
    for directory in (path, path.parent):
        try:
            names = os.listdir(directory)
        except OSError:
            return "marker_unreadable"
        for name in names:
            if not (name.endswith((".pid", ".lock")) or name in ("pid", "lock", ".lock")):
                continue
            full = os.path.join(directory, name)
            try:
                if stat.S_ISDIR(os.lstat(full).st_mode):
                    continue  # a directory is not a marker file
                with open(full, "r", errors="replace") as handle:
                    if live_pid(handle.read(64)):
                        return "live_pid_or_lock"
            except OSError:
                return "marker_unreadable"
    return None


def has_live_marker(path: pathlib.Path) -> bool:
    return marker_state(path) is not None


def command_lines(runner: Runner = subprocess.run) -> Optional[str]:
    try:
        proc = runner(["ps", "-axo", "command="], capture_output=True, text=True,
                      timeout=PS_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def protected(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        return True
    return any(PROTECTED_RE.match(part) for part in parts)


def scan(settings: dict[str, Any], *, fix: bool, runner: Runner = subprocess.run,
         now: Optional[float] = None, log: Optional[Callable[[str], None]] = None,
         clock: Callable[[], float] = time.monotonic,
         measure: Callable[[pathlib.Path], Optional[float]] = used_percent
         ) -> dict[str, Any]:
    log = log or (lambda line: print(line, file=sys.stderr, flush=True))
    started = clock()
    report: dict[str, Any] = {
        "enabled": True, "mode": "fix" if fix else "dry-run",
        "idle_days": settings["idle_days"], "target_used_pct": settings["target_used_pct"],
        "pressure_used_pct": settings["pressure_used_pct"], "roots": [],
        "candidates": 0, "removed": 0, "removed_bytes": 0, "removed_paths": [],
        "kept": {}, "capped": None, "errors": [],
    }
    kept = report["kept"]

    def keep(reason: str) -> None:
        kept[reason] = kept.get(reason, 0) + 1

    commands = command_lines(runner)
    gib_cap = settings["max_gib"] * GIB
    over_cap = False
    for root in settings["roots"]:
        entry: dict[str, Any] = {"root": str(root), "used_pct_before": None,
                                 "used_pct_after": None, "state": None}
        report["roots"].append(entry)
        if not root.is_dir():
            entry["state"] = "missing"
            report["errors"].append(f"{root} is not a directory")
            continue
        before = measure(root)
        entry["used_pct_before"] = None if before is None else round(before, 1)
        entry["used_pct_after"] = entry["used_pct_before"]
        if before is None:
            entry["state"] = "unknown_usage"
            continue
        pressure = before >= settings["pressure_used_pct"]
        if not pressure and before <= settings["target_used_pct"]:
            entry["state"] = "under_target"
            continue
        entry["state"] = "pressure" if pressure else "over_target"
        candidates = find_candidates(root)
        report["candidates"] += len(candidates)

        def age_key(path: pathlib.Path) -> float:
            try:
                return path.lstat().st_mtime
            except OSError:
                return float("inf")

        current = before
        for path in sorted(candidates, key=age_key):
            if report["capped"]:
                break
            if not pressure and current <= settings["target_used_pct"]:
                entry["state"] = "target_reached"
                break
            if report["removed"] >= settings["max_dirs"]:
                report["capped"] = "max_dirs"
                break
            if report["removed_bytes"] >= gib_cap:
                report["capped"] = "max_gib"
                break
            if clock() - started > settings["budget_seconds"]:
                report["capped"] = "budget_seconds"
                break
            days = PROTECTED_IDLE_DAYS if protected(path, root) else settings["idle_days"]
            days = max(days, settings["idle_days"])
            try:
                is_link = stat.S_ISLNK(os.lstat(path).st_mode)
            except OSError:
                keep("stat_failed")
                continue
            if is_link:
                keep("symlink")
                continue
            reason = marker_state(path)
            if reason:
                keep(reason)
                continue
            if commands is None:
                keep("process_scan_failed")
                continue
            if str(path) in commands or str(path.parent) in commands:
                keep("in_use")
                continue
            idle = idle_for(path, days, runner)
            if idle is None:
                keep("idle_check_failed")
                continue
            if not idle:
                keep("protected_lane_recent" if days > settings["idle_days"] else "recent")
                continue
            size = scratch_dirs.size_bytes(path, runner)
            if size is None:
                keep("size_unknown")
                continue
            if report["removed_bytes"] + size > gib_cap:
                keep("over_gib_cap")
                over_cap = True
                continue
            if fix:
                # Re-read just before the delete: a build may have started.
                reason = marker_state(path)
                if reason:
                    keep(reason)
                    continue
                recheck = idle_for(path, days, runner)
                if recheck is None:
                    keep("idle_check_failed")
                    continue
                if not recheck:
                    keep("recent")
                    continue
                fresh = command_lines(runner)
                if fresh is None:
                    keep("process_scan_failed")
                    continue
                if str(path) in fresh or str(path.parent) in fresh:
                    keep("in_use")
                    continue
                error = scratch_dirs.remove_tree(path, os.geteuid())
                if error:
                    report["errors"].append(f"{path}: {error}")
                    keep("remove_failed")
                    continue
            report["removed"] += 1
            report["removed_bytes"] += size or 0
            if len(report["removed_paths"]) < LIST_LIMIT:
                report["removed_paths"].append(str(path))
            verb = "removed" if fix else "would remove"
            log(f"worktree_outputs: {verb} {path} ({(size or 0) / GIB:.1f} GiB, "
                f"idle >= {days}d, {entry['state']})")
            if fix:
                after = measure(root)
                if after is None:
                    # Cannot tell whether the target is met: stop, do not guess.
                    entry["state"] = "unknown_usage"
                    keep("usage_unknown_after_remove")
                    break
                current = after
            elif size:
                # A dry run cannot re-measure; estimate with the volume size.
                try:
                    info = os.statvfs(root)
                    total = info.f_blocks * info.f_frsize
                    current -= 100.0 * size / total if total else 0
                except OSError:
                    pass
        entry["used_pct_after"] = round(current, 1)
    if over_cap and not report["capped"]:
        report["capped"] = "max_gib"
    report["elapsed_s"] = round(clock() - started, 1)
    return report


def run(*, fix: bool, profile: pathlib.Path, runner: Runner = subprocess.run,
        **kwargs: Any) -> dict[str, Any]:
    """One pass. Off unless the profile names `worktree_roots`. Never raises."""
    settings, why = load_settings(profile)
    if settings is None:
        return {"enabled": False, "reason": why}
    try:
        return scan(settings, fix=fix, runner=runner, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a janitor must not take the pass down
        return {"enabled": True, "error": f"worktree output scan failed: {exc}"}
