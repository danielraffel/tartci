#!/usr/bin/env python3
"""Build output inside agent checkouts: the opt-in short-idle tier of disk_reclaim.

Why this exists: on m1 (2026-11-06 to 11-08) about 277 GB of agent build
output under ~/Code took the data volume from hundreds of GiB free to about
41 GiB. Every macOS VM lease then failed its disk axis (24 GiB growth plus a
25 GiB floor): 703 of the pulp-gate lane's 1,215 lease attempts and 653 of
slot 2's 1,085 were `disk_capacity_exceeded`, and the host served almost
nothing for three days while jobs queued. disk_reclaim already removes CMake
build trees, but only after 30 days idle (7 under pressure), and it never
looked at Rust `target/`, `node_modules/` or Xcode `DerivedData/`. Agent
worktrees produce all of them and are abandoned within hours, not weeks.

With `[reclaim] checkout_build_outputs = true` in the installed fleet profile,
and only on a volume that is under pressure (the same test the rest of the
pass uses), disk_reclaim also:

  * considers `target/`, `node_modules/` and `DerivedData/` directories, each
    only with its own generated-tree marker (OUTPUT_KINDS);
  * judges them, and the CMake build trees it already knew, against a short
    idle gate in HOURS (`checkout_build_output_idle_hours`, default 4) instead
    of days.

A directory taken by this tier must also pass the CHECKOUT gates here, on top
of every gate disk_reclaim already applies (no source tree inside it, no live
build naming it, nothing inside it written within the gate, re-read just
before the delete):

  * it lies inside a git checkout (its nearest ancestor with a `.git`), and
    git can answer for that checkout;
  * git tracks no file under it (`git ls-files` is empty there), so nothing
    removed is something a commit would restore differently;
  * the checkout is clean (`git status --porcelain --untracked-files=no` is
    empty) or stale (nothing at its top level or in its index/HEAD moved for
    `STALE_CHECKOUT_HOURS`). A dirty checkout someone touched today keeps its
    build output, whatever its age.

Anything git cannot answer in time keeps the directory. Off by default: a host
opts in, and with the key absent the pass is exactly what it was.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import time
from typing import Any, Callable

import tmp_checkouts

ENABLE_KEY = "checkout_build_outputs"
IDLE_KEY = "checkout_build_output_idle_hours"
DEFAULT_IDLE_HOURS = 4
MIN_IDLE_HOURS = 1
MAX_IDLE_HOURS = 168
#: A checkout untouched this long is abandoned for this tier's purpose, dirty
#: or not: its build output is regenerable and the source is left alone.
STALE_CHECKOUT_HOURS = 48
#: How far above a candidate the enclosing checkout is looked for.
CHECKOUT_SEARCH_DEPTH = 6

#: name -> (generated-tree markers, any one proves it; nested markers that mean
#: a real source checkout inside it; subdirectories whose own `.git` checkouts
#: are generated). A `node_modules` package carries a package.json and a
#: packaged crate under `target/package/` a Cargo.toml, so for these kinds only
#: a `.git` below marks a checkout worth keeping. SwiftPM clones its
#: dependencies under DerivedData/SourcePackages/checkouts, re-fetched on the
#: next build, so a `.git` there is not one.
OUTPUT_KINDS: dict[str, dict[str, tuple[str, ...]]] = {
    "target": {"markers": ("CACHEDIR.TAG", ".rustc_info.json"),
               "nested": (".git",), "generated": ()},
    "node_modules": {"markers": (".package-lock.json", ".modules.yaml",
                                 ".yarn-integrity", ".yarn-state.yml"),
                     "nested": (".git",), "generated": ()},
    "DerivedData": {"markers": ("info.plist",),
                    "nested": (".git",), "generated": ("SourcePackages",)},
}

Runner = Callable[..., subprocess.CompletedProcess]


def validate(table: dict[str, Any]) -> list[str]:
    """Problems with this module's keys of a `[reclaim]` table."""
    problems = []
    if type(table.get(ENABLE_KEY, False)) is not bool:
        problems.append(f"reclaim.{ENABLE_KEY} must be a boolean")
    idle = table.get(IDLE_KEY, DEFAULT_IDLE_HOURS)
    if type(idle) is not int or not MIN_IDLE_HOURS <= idle <= MAX_IDLE_HOURS:
        problems.append(f"reclaim.{IDLE_KEY} must be an integer from "
                        f"{MIN_IDLE_HOURS} through {MAX_IDLE_HOURS}")
    return problems


def settings(table: Any) -> dict[str, Any] | None:
    """{"idle_hours": N} when the tier is on and valid, else None."""
    if not isinstance(table, dict) or table.get(ENABLE_KEY) is not True:
        return None
    if validate(table):
        return None
    return {"idle_hours": int(table.get(IDLE_KEY, DEFAULT_IDLE_HOURS))}


def kind_of(name: str) -> str | None:
    return name if name in OUTPUT_KINDS else None


def enclosing_checkout(path: pathlib.Path) -> pathlib.Path | None:
    """The nearest ancestor of `path` that holds a `.git`, within the bound."""
    current = path.parent
    for _ in range(CHECKOUT_SEARCH_DEPTH):
        if os.path.lexists(current / ".git"):
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def checkout_gate(path: pathlib.Path, *, now: float | None = None,
                  runner: Runner = subprocess.run) -> str | None:
    """None when `path` may be taken by the short tier, else the reason to keep it."""
    now = time.time() if now is None else now
    checkout = enclosing_checkout(path)
    if checkout is None:
        return "not_in_checkout"
    kind, gitdir = tmp_checkouts.gitdir_of(checkout)
    if gitdir is None:
        return f"checkout_{kind}"
    relative = os.path.relpath(path, checkout)
    try:
        tracked = tmp_checkouts._git(checkout, "ls-files", "-z", "--", relative,
                                     runner=runner)
        if tracked.returncode != 0:
            return "git_unanswered"
        if tracked.stdout.strip("\0"):
            return "tracked_files"
        status = tmp_checkouts._git(checkout, "status", "--porcelain",
                                    "--untracked-files=no", runner=runner)
    except (OSError, subprocess.SubprocessError):
        return "git_unanswered"
    if status.returncode != 0:
        return "git_unanswered"
    if not status.stdout.strip():
        return None
    newest = tmp_checkouts.newest_mtime(checkout, gitdir)
    if newest is None:
        return "checkout_unmeasured"
    if now - newest < STALE_CHECKOUT_HOURS * 3600:
        return "checkout_dirty_and_recent"
    return None
