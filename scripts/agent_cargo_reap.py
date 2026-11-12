#!/usr/bin/env python3
"""Report, and on an opted-in host delete, agents' stale Cargo target dirs.

On m1 on 2026-10-16 agents' Cargo target dirs under ~/agent-scratch reached
246 GB and took the host to 150 GB free before anyone looked. A target dir is
a build cache: deleting an idle one costs a rebuild, never work.

A directory is a candidate only when all of these hold:

  * it is under the agent scratch root (~/agent-scratch), at most
    MAX_DEPTH levels down, reached without following a symlink, and not
    under a kept subtree (KEEP: `rcg`, an agent's live checkouts);
  * Cargo made it: its CACHEDIR.TAG has the cache-dir signature AND says it
    was created by cargo (pytest and ccache write the same signature);
  * it is stale: its newest build activity (debug/, release/, their deps/,
    and every .fingerprint entry, since .fingerprint itself keeps its
    first-build mtime) is at least STALE_DAYS old;
  * no process has it open: no cwd and no open file inside it (lsof). If
    the open-file list cannot be read, nothing is deleted.

The watchdog runs this about hourly. It always logs a WARN line naming the
stale dirs and their size. It deletes them only when
~/.config/tartci/reap-agent-cargo.enabled exists on that host; enable one host
at a time, after a dry run (`scripts/agent_cargo_reap.py --dry-run`).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STALE_DAYS = 2
MAX_DEPTH = 3
KEEP = ("rcg",)
INTERVAL_SECONDS = 3600
MEASURE_DEADLINE_SECONDS = 300
CARGO_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55"
GB = 1000 ** 3


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def scratch_root(user_home: Path) -> Path:
    return Path(os.environ.get("TARTCI_AGENT_SCRATCH") or user_home / "agent-scratch")


def enabled(user_home: Path) -> bool:
    flag = os.environ.get("TARTCI_REAP_AGENT_CARGO_ENABLE_FILE") or str(
        user_home / ".config" / "tartci" / "reap-agent-cargo.enabled")
    return Path(flag).is_file()


def is_cargo_target(path: Path) -> bool:
    """CACHEDIR.TAG's signature is the generic cache-dir standard, which pytest
    and ccache also write (m1's only matches on 2026-10-16 were two
    .pytest_cache dirs); Cargo's own tag also says it was created by cargo."""
    try:
        tag = (path / "CACHEDIR.TAG").read_text(errors="replace")[:400]
    except OSError:
        return False
    return CARGO_SIGNATURE in tag and "created by cargo" in tag


def cargo_targets(root: Path) -> list[Path]:
    """Every Cargo target dir under `root`, not following symlinks, not under KEEP."""
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if depth == 1 and entry.name in KEEP:
                continue
            path = Path(entry.path)
            if is_cargo_target(path):
                found.append(path)
            elif depth < MAX_DEPTH:
                walk(path, depth + 1)

    walk(root, 1)
    return found


def last_built(target: Path) -> float:
    """The newest build activity: a profile dir, its deps, or any fingerprint
    entry. debug/.fingerprint itself keeps its first-build mtime, so a rebuild
    an hour ago shows only in the entries and in deps."""
    stamps = [target.stat().st_mtime]
    for profile in ("debug", "release"):
        for sub in (profile, f"{profile}/deps", f"{profile}/.fingerprint"):
            try:
                stamps.append((target / sub).stat().st_mtime)
            except OSError:
                continue
        try:
            with os.scandir(target / profile / ".fingerprint") as entries:
                for entry in entries:
                    try:
                        stamps.append(entry.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    return max(stamps)


def open_paths() -> Optional[list[str]]:
    """Every path a process has open or as its cwd; None when unreadable."""
    try:
        out = subprocess.run(["lsof", "-w", "-n", "-P", "-Fn"], capture_output=True, text=True,
                             timeout=180, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    paths = [line[1:] for line in out.stdout.splitlines() if line.startswith("n/")]
    return paths or None


def in_use(target: Path, opened: list[str]) -> bool:
    prefix = str(target)
    return any(p == prefix or p.startswith(prefix + os.sep) for p in opened)


def plan(root: Path, now: float, opened: Optional[list[str]],
         measure: Callable[..., Optional[int]], deadline: float = MEASURE_DEADLINE_SECONDS,
         clock: Callable[[], float] = time.monotonic) -> list[dict]:
    """One row per Cargo target: verdict `delete` or `keep` with the reason."""
    rows = []
    end = clock() + deadline
    for target in cargo_targets(root):
        try:
            age_days = (now - last_built(target)) / 86400
        except OSError:
            rows.append({"path": str(target), "verdict": "keep", "reason": "unreadable"})
            continue
        row = {"path": str(target), "age_days": round(age_days, 1)}
        if age_days < STALE_DAYS:
            row.update(verdict="keep", reason=f"built {age_days:.1f} d ago")
        elif opened is None:
            row.update(verdict="keep", reason="open files unknown")
        elif in_use(target, opened):
            row.update(verdict="keep", reason="in use")
        else:
            row.update(verdict="delete", reason=f"idle {age_days:.1f} d, nothing open")
            left = end - clock()
            row["bytes"] = measure(target, timeout=min(180, left)) if left > 1 else None
        rows.append(row)
    return rows


def _measure(path: Path, timeout: float = 180) -> Optional[int]:
    import host_hygiene  # noqa: PLC0415 - sibling module; one bounded du
    return host_hygiene.du_bytes(path, timeout=timeout)


def _gb(row: dict) -> str:
    return "? GB" if row.get("bytes") is None else f"{row['bytes'] / GB:.0f} GB"


def apply(rows: list[dict], remove: Callable[[Path], None] = shutil.rmtree) -> list[str]:
    done = []
    for row in rows:
        if row["verdict"] != "delete":
            continue
        try:
            remove(Path(row["path"]))
            done.append(f"deleted {row['path']} ({_gb(row)})")
        except OSError as exc:
            done.append(f"could not delete {row['path']}: {exc}")
    return done


def watchdog_pass(now: float, *, user_home: Optional[Path] = None,
                  opened: Callable[[], Optional[list[str]]] = open_paths,
                  measure: Callable[..., Optional[int]] = _measure,
                  remove: Callable[[Path], None] = shutil.rmtree) -> list[str]:
    """About hourly: the WARN line naming stale targets, and what was deleted."""
    user_home = user_home or Path(os.path.expanduser("~"))
    root = scratch_root(user_home)
    if not root.is_dir():
        return []
    state = Path(os.environ.get("TARTCI_HOME") or user_home / ".tartci") / "state" / \
        "agent-cargo-reap.json"
    try:
        last = float(json.loads(state.read_text()).get("checked_at", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        last = 0.0
    if 0 <= now - last < INTERVAL_SECONDS:
        return []
    rows = plan(root, now, opened(), measure)
    stale = [r for r in rows if r["verdict"] == "delete"]
    lines = []
    if stale:
        total = sum(r.get("bytes") or 0 for r in stale)
        on = enabled(user_home)
        lines.append(f"{_iso(now)} launchd-watchdog: WARN agent-cargo: {len(stale)} stale Cargo "
                     f"target dir(s), {total / GB:.0f} GB"
                     + ("" if on else " (report only: ~/.config/tartci/reap-agent-cargo.enabled "
                                      "is absent)") + ": "
                     + "; ".join(f"{r['path']} {_gb(r)} idle {r['age_days']} d" for r in stale))
        if on:
            lines += [f"{_iso(now)} launchd-watchdog: agent-cargo: {text}"
                      for text in apply(stale, remove)]
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"checked_at": now, "rows": rows}, indent=2) + "\n")
    return lines


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = parser.parse_args(argv)
    home = Path(os.path.expanduser("~"))
    rows = plan(scratch_root(home), time.time(), open_paths(), _measure)
    for row in rows:
        print(f"{row['verdict']:6} {_gb(row) if row['verdict'] == 'delete' else '':>7}  "
              f"{row['path']}  ({row['reason']})")
    if not args.dry_run and enabled(home):
        for line in apply(rows):
            print(line)
    elif not args.dry_run:
        print("report only: ~/.config/tartci/reap-agent-cargo.enabled is absent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
