#!/usr/bin/env python3
"""Reclaim regenerable build directories so a CI host cannot fill its disk.

Why this exists: tartci admits work by lease, and the lease has a disk axis.
When a host's data volume fills, every lease is denied `disk_capacity_exceeded`
and the host stops serving. That is not a slow host, it is a dead one, and the
load silently moves to whatever host is left. Build directories are the thing
that fills it: they are large, regenerable, and nothing has ever removed them.

The janitor is conservative in the same shape as vm_reap.py. A directory is
deleted only when EVERY positive check passes:

  * its basename is a build-directory name (`build`, `build-<key>`,
    `build-cov*`, `build-coverage*`),
  * it carries a generated-tree marker (`CMakeCache.txt`, `build.ninja`, or
    `CMakeFiles/`), so a source directory that merely has the name is skipped,
  * it carries no source marker (`.git`, `CMakeLists.txt`) of its own,
  * nor one anywhere in the few levels below it, because the tree is removed
    whole and a fetched dependency at `_deps/<name>-src/.git` is a real
    checkout,
  * no live build process references its path,
  * nothing inside it has been modified inside the age gate.

Two age tiers. `--min-age-days` always applies. Under disk pressure (free space
below `--pressure-free-gb`) the shorter `--pressure-min-age-days` applies too,
so an idle host keeps recent build dirs warm and a full host reclaims harder.

`--fail-below-gb` closes the escalation half: a host still below the floor after
a reclaim pass exits non-zero, so launchd records it and a supervisor can see a
full disk instead of only seeing refused leases.

Exit codes:

  0  the pass ran and the host is above its floor,
  2  no scan roots resolved, so nothing was examined,
  3  the pass ran and the host is STILL below `--fail-below-gb`; the reclaim
     could not free enough and a human needs to look,
  4  a measurement the decision depends on could not be taken (the process
     table or the free-space figure). Nothing was deleted. This is distinct
     from 3 on purpose: 3 means the host is full, 4 means we do not know.
"""

from __future__ import annotations

import argparse
import errno
import functools
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Iterable

BUILD_DIR_PREFIXES = ("build-",)
BUILD_DIR_EXACT = ("build",)

# Positive proof the directory is generator output rather than a source tree
# that happens to be called "build".
GENERATED_MARKERS = ("CMakeCache.txt", "build.ninja", "CMakeFiles")

# Presence of any of these means a human's tree, never a reclaim candidate.
SOURCE_MARKERS = (".git", "CMakeLists.txt", "Cargo.toml", "package.json")

# Command names whose live command lines are scanned for candidate paths.
BUILD_PROCESS_PATTERN = r"cmake|ctest|ninja|make|clang|cc1|c\+\+|xcodebuild|swift"

GIB = 1024**3

# A --fix pass can run for minutes with nothing to say, and the watchdog reads
# this agent's liveness from the mtime of its log. A pass that is working but
# silent is indistinguishable from one that is wedged, so emit a bounded
# heartbeat instead of leaving the log frozen for the whole run.
PROGRESS_INTERVAL_S = 300.0


class Progress:
    """Rate-limited heartbeat, written to stderr.

    stderr and not stdout on purpose: under --json stdout carries one machine
    read document, and a heartbeat interleaved into it would break every
    parser. The launchd plist points BOTH streams at the same log file, so a
    line written here still refreshes the mtime the watchdog reads.

    Every write flushes. A buffered line does not move the file's mtime, which
    would leave the log exactly as stale as it was before.
    """

    def __init__(self, interval_s: float = PROGRESS_INTERVAL_S,
                 stream: Any = None) -> None:
        self.interval_s = interval_s
        self.stream = sys.stderr if stream is None else stream
        self._last = time.time()

    def emit(self, message: str, force: bool = False) -> bool:
        """Write `message` unless the rate limit is still holding it back."""
        now = time.time()
        if not force and now - self._last < self.interval_s:
            return False
        self._last = now
        print(f"disk_reclaim: {message}", file=self.stream, flush=True)
        return True


def is_build_dir_name(name: str) -> bool:
    """True when `name` is a build-directory name we are willing to consider."""
    if name in BUILD_DIR_EXACT:
        return True
    return any(name.startswith(prefix) and len(name) > len(prefix)
               for prefix in BUILD_DIR_PREFIXES)


def has_marker(path: pathlib.Path, markers: Iterable[str]) -> str | None:
    """Return the first marker present directly inside `path`, else None."""
    for marker in markers:
        if (path / marker).exists():
            return marker
    return None


#: How far below a candidate a source checkout is looked for. A build tree
#: puts fetched dependencies two levels down (`_deps/<name>-src/.git`), and a
#: developer parking a worktree inside one puts it at depth one, so a bound of
#: four covers both with a sub-directory to spare.
NESTED_SOURCE_MAXDEPTH = 4

#: How far below a candidate the pre-delete age re-read looks. Deeper than the
#: scan pass on purpose: the process guard only sees an ABSOLUTE path in a
#: command line, so a build driven as `cd build && make` is invisible to it and
#: this walk is the only thing left. The Makefiles generator writes objects at
#: `CMakeFiles/<target>.dir/<nested/src>.o`, which a two-level walk never sees.
RECHECK_MAXDEPTH = 4


def nested_source_marker(
    path: pathlib.Path, maxdepth: int = NESTED_SOURCE_MAXDEPTH,
) -> tuple[str | None, bool]:
    """Look for a source checkout BELOW `path`. Returns (marker path, readable).

    The depth-0 marker check answers "is this directory itself a source tree".
    It does not answer the question that actually loses work, because a build
    tree is deleted whole: a fetched dependency at `_deps/<name>-src/.git`, or
    a worktree someone parked inside a scratch build dir, is a real checkout
    that no later pass can bring back. A pristine dependency is re-fetchable;
    one carrying local edits, or one with no `.git` of its own, is not.

    `readable` is the same refusal-versus-answer distinction the age scan and
    the process scan make. A subtree we were refused could hold a checkout, so
    "could not look" must never render as "nothing there".
    """
    stack: list[tuple[pathlib.Path, int]] = [(path, 0)]
    while stack:
        current, depth = stack.pop()
        if depth >= maxdepth:
            continue
        try:
            entries = list(os.scandir(current))
        except OSError as error:
            if error.errno in UNMEASURABLE_ERRNOS:
                return None, False
            continue
        for entry in entries:
            child = pathlib.Path(entry.path)
            if depth > 0 and entry.name in SOURCE_MARKERS:
                # A linked worktree's `.git` is a regular file, and a
                # `CMakeLists.txt` is never a directory, so this deliberately
                # does not test the entry's type.
                return str(child), True
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            stack.append((child, depth + 1))
    return None, True


def find_candidates(roots: list[pathlib.Path], maxdepth: int) -> list[pathlib.Path]:
    """Directories under `roots` whose name looks like a build directory.

    Does not descend into a candidate (a nested `build/` inside a build tree is
    reclaimed with its parent), does not follow symlinks, and never returns a
    root itself.
    """
    found: list[pathlib.Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        stack: list[tuple[pathlib.Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            if depth >= maxdepth:
                continue
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                child = pathlib.Path(entry.path)
                if is_build_dir_name(entry.name):
                    found.append(child)
                    continue
                stack.append((child, depth + 1))
    return sorted(found)


# Errnos that mean "this subtree exists and we were refused", which is the
# only case where an unreadable entry makes the age unknowable. ENOENT and
# ENOTDIR are deliberately absent: a live build tree creates and unlinks temp
# files under the scan, so an entry vanishing mid-walk is routine AND is
# positive evidence the tree is busy. Treating that as unmeasured would make
# the hourly pass report unknown on every busy host.
UNMEASURABLE_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EIO, errno.ELOOP})


def newest_mtime(path: pathlib.Path, maxdepth: int = 2) -> float | None:
    """Newest mtime at or shallowly below `path`, or None when unmeasurable.

    A build tree that ran recently has a fresh `CMakeCache.txt`, `.ninja_log`,
    or a fresh top-level subdirectory, so a bounded scan is enough and a full
    recursive walk of a 100 GB tree is not paid on every pass.

    None is the load-bearing part. Without it an unreadable subtree reads as
    mtime 0, which is maximally idle, so a tree written seconds ago is
    reported as decades old and deleted. A refusal has to stay distinct from
    an answer, exactly as it does for the process scan and for free space.
    """
    newest = 0.0
    measured = False
    stack: list[tuple[pathlib.Path, int]] = [(path, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            newest = max(newest, current.stat().st_mtime)
            measured = True
            entries = list(os.scandir(current))
        except OSError as exc:
            if exc.errno in UNMEASURABLE_ERRNOS:
                return None
            continue
        for entry in entries:
            try:
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                measured = True
            except OSError as exc:
                if exc.errno in UNMEASURABLE_ERRNOS:
                    return None
                continue
            if depth + 1 < maxdepth and entry.is_dir(follow_symlinks=False):
                stack.append((pathlib.Path(entry.path), depth + 1))
    return newest if measured else None


def active_command_lines(pattern: str = BUILD_PROCESS_PATTERN) -> str | None:
    """Live build command lines, or None when the process table cannot be read.

    The distinction matters more than it looks. An empty result means "pgrep
    ran and no build is in flight", which licenses deletion. A failed scan
    means we do not know, and reading that as an idle host would delete a
    live build. So the two cases must never collapse to the same value.
    `pgrep` exits 1 with empty output when nothing matches; anything above
    that is an error, not an answer.
    """
    try:
        proc = subprocess.run(
            ["pgrep", "-fl", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode > 1:
        return None
    return proc.stdout or ""


def dir_size_bytes(path: pathlib.Path) -> int:
    """Size of `path`, preferring `du` and falling back to a python walk."""
    try:
        proc = subprocess.run(
            ["du", "-sk", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return int(proc.stdout.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    total = 0
    for current, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                continue
    return total


def free_bytes(path: pathlib.Path) -> int | None:
    """Free bytes on `path`'s volume, or None when it cannot be read.

    Returning 0 here would be read as a full disk, which selects the SHORTER
    pressure age gate. An unreadable volume would then reclaim harder than a
    healthy one, so the unknown case has to stay distinct from zero.

    The failure branch looks dead, because main() refuses an unreadable scan
    root before any measurement is taken. It is not: the post-fix re-measure
    runs after a whole pass has walked and deleted, which on a loaded host is
    minutes after that check, and the volume it re-measures is a mount point
    (m3 scans /Volumes/Workshop). An unmount inside that window, or an EIO off
    a failing disk, reaches here. Deleting this branch would turn that into a
    traceback out of the janitor instead of an honest "unknown".
    """
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def path_spellings(path: pathlib.Path) -> set[str]:
    """Every absolute spelling of `path` a command line might carry.

    On macOS `/tmp` is a symlink to `/private/tmp` and a checkout can sit
    behind any number of symlinked parents, so a command line naming the
    unresolved spelling does not substring-match the resolved one. Comparing
    only one of them is a guard that silently does not fire.
    """
    spellings = {str(path)}
    try:
        spellings.add(str(path.resolve()))
    except OSError:
        pass
    return spellings


@functools.lru_cache(maxsize=8)
def resolved_command_paths(active: str) -> frozenset[str]:
    """Every absolute path token in `active`, plus its resolved spelling.

    The substring test above resolves the CANDIDATE; this resolves the other
    side. A command line records whichever spelling the shell handed it, so
    without this a build launched through a symlinked checkout names a path
    that no spelling of the candidate matches. A token that is not a path
    resolves to itself and matches nothing, so no argv parser is needed.
    """
    spellings: set[str] = set()
    for token in active.split():
        if not token.startswith("/"):
            continue
        spellings.add(token)
        try:
            spellings.add(str(pathlib.Path(token).resolve()))
        except OSError:
            continue
    return frozenset(spellings)


def names_candidate(active: str, spellings: set[str]) -> bool:
    """True when `active` names this candidate under either spelling.

    Deliberately generous in both directions: a command line naming a file
    INSIDE the tree protects the tree, because a compiler writing into it is
    exactly what the guard exists to catch.
    """
    if any(spelling in active for spelling in spellings):
        return True
    for token in resolved_command_paths(active):
        if any(token == spelling or token.startswith(spelling + os.sep)
               for spelling in spellings):
            return True
    return False


def classify(
    path: pathlib.Path,
    *,
    now: float,
    min_age_days: float,
    active: str | None,
) -> tuple[bool, str, float]:
    """Decide one candidate. Returns (delete, reason, age_days).

    `active` is the live build command lines, or None when that scan failed.
    None blocks every deletion: without a process table we cannot prove a
    build is not running, and the whole contract is that deletion requires
    positive proof rather than absence of evidence.
    """
    measured = newest_mtime(path)
    if measured is None:
        return False, "unmeasured", 0.0
    age_days = max(0.0, (now - measured) / 86400.0)

    if has_marker(path, SOURCE_MARKERS):
        return False, "source_tree", age_days
    marker = has_marker(path, GENERATED_MARKERS)
    if marker is None:
        return False, "not_a_build_tree", age_days
    nested, readable = nested_source_marker(path)
    if not readable:
        return False, "nested_scan_unreadable", age_days
    if nested is not None:
        return False, f"nested_source_tree ({nested})", age_days
    if active is None:
        return False, "process_scan_unavailable", age_days
    if active and names_candidate(active, path_spellings(path)):
        return False, "active_build", age_days
    if age_days < min_age_days:
        return False, "too_recent", age_days
    return True, f"reclaimable ({marker}, idle {age_days:.1f}d)", age_days


# Where build trees live when nothing declares TARTCI_RECLAIM_ROOTS. This is
# a list and not a single `~/Code` because a hand-set root that EXISTS but
# sits on the wrong volume passes every guard below and reports a clean exit 0
# forever while the volume it was installed to protect fills up. m3 keeps its
# code and worktrees on an external Workshop volume and still has a populated
# `~/Code` on the boot disk, so `~/Code` alone is exactly that failure.
# Discovery keeps whichever of these the host actually has, so a host is
# covered without per-host tuning of the rendered plist.
DEFAULT_ROOT_CANDIDATES = (
    "~/Code",
    "/Volumes/Workshop/Code",
)


def discover_roots() -> list[pathlib.Path]:
    """Existing default scan roots, in candidate order, deduped by device+path.

    A candidate that does not exist is skipped rather than reported, which is
    the opposite of how an explicitly declared root is treated: declaring a
    root that is absent is a configuration fault, while a default that does
    not apply to this host is simply not this host's layout.
    """
    found: list[pathlib.Path] = []
    seen: set[str] = set()
    for candidate in DEFAULT_ROOT_CANDIDATES:
        path = pathlib.Path(os.path.expanduser(candidate))
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_dir() or str(resolved) in seen:
            continue
        seen.add(str(resolved))
        found.append(resolved)
    return found


def parse_roots(raw: str | None) -> list[pathlib.Path]:
    if not raw:
        return discover_roots()
    return [pathlib.Path(os.path.expanduser(part)).resolve()
            for part in raw.split(":") if part]


def device_id(path: pathlib.Path) -> int | None:
    """The volume a path lives on, or None when it cannot be determined.

    Unknown must not collapse into a shared sentinel: two unreadable roots
    are not evidence of one volume, and folding them together would drop a
    measurement rather than take one.
    """
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def volumes_free_bytes(roots: list[pathlib.Path]) -> list[dict[str, Any]]:
    """Free space for every distinct volume the scan roots span.

    One entry per device, in root order. Measuring roots[0] alone is what
    lets a two-volume host report a healthy pass: m3 scans the boot disk and
    the Workshop volume, and only one of them is the one that fills.
    """
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for root in roots:
        device = device_id(root)
        if device is not None:
            if device in seen:
                continue
            seen.add(device)
        out.append({"root": str(root), "device": device,
                    "free_bytes": free_bytes(root)})
    return out


def tightest_free_bytes(volumes: list[dict[str, Any]]) -> int | None:
    """The smallest free figure across `volumes`, or None if any is unknown.

    None when ANY volume is unreadable, because the unreadable one could be
    the full one. An unknown has to stay distinct from a healthy reading for
    the same reason `free_bytes` never returns 0 on failure.
    """
    if not volumes:
        return None
    figures = [volume["free_bytes"] for volume in volumes]
    if any(figure is None for figure in figures):
        return None
    return min(figures)


def tightest_volume_root(volumes: list[dict[str, Any]]) -> str | None:
    """The root naming the volume `tightest_free_bytes` reported, if known."""
    known = [v for v in volumes if v["free_bytes"] is not None]
    if not known or len(known) != len(volumes):
        return None
    return min(known, key=lambda v: v["free_bytes"])["root"]


def rotate_log(path: pathlib.Path, max_bytes: int, generations: int,
               stream: Any = None) -> bool:
    """Rename an oversized log aside at startup, keeping `generations` of it.

    The reclaim log lives on the volume the reclaim exists to protect, and
    launchd appends every hourly pass to it forever. Nothing has ever bounded
    it: m3 was carrying 368M of tartci logs when this was written. A janitor
    whose own receipt fills the disk is the failure it was built to prevent.

    Rename rather than truncate, because launchd opens `StandardOutPath`
    fresh on every spawn of a `StartInterval` job. That was measured on a
    throwaway job, not assumed, and it has a visible consequence: the fd this
    process inherited still points at the renamed inode, so THIS pass's output
    lands in generation 1 and the next spawn creates the new file. The bound
    is therefore generations x (max_bytes + one pass's output), never
    generations x max_bytes exactly.

    Returns True when a rotation happened, and never raises. A log that cannot
    be rotated is a far smaller problem than a reclaim pass that refuses to
    run because of it.
    """
    if max_bytes <= 0 or generations <= 0:
        return False
    stream = sys.stderr if stream is None else stream
    try:
        metadata = path.lstat()
    except OSError:
        return False  # absent or unreadable: there is nothing to rotate yet
    try:
        # lstat, so a symlink is judged as a symlink. Following one would let
        # anything that can write this directory choose the file we rename.
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            print(f"disk_reclaim: refusing to rotate {path}: "
                  "not a user-owned regular file", file=stream)
            return False
        if metadata.st_size < max_bytes:
            return False
        oldest = pathlib.Path(f"{path}.{generations}")
        oldest.unlink(missing_ok=True)
        for index in range(generations - 1, 0, -1):
            source = pathlib.Path(f"{path}.{index}")
            if source.exists():
                os.replace(source, pathlib.Path(f"{path}.{index + 1}"))
        os.replace(path, pathlib.Path(f"{path}.1"))
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.close(descriptor)
    except OSError as error:
        print(f"disk_reclaim: could not rotate {path}: {error}", file=stream)
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reclaim regenerable build directories on a CI host.")
    parser.add_argument(
        "--roots",
        default=os.environ.get("TARTCI_RECLAIM_ROOTS"),
        help="colon-separated scan roots (default: $TARTCI_RECLAIM_ROOTS, else whichever of ~/Code and /Volumes/Workshop/Code exist)")
    parser.add_argument("--maxdepth", type=int,
                        default=int(os.environ.get("TARTCI_RECLAIM_MAXDEPTH", "5")),
                        help="directory depth below each root to scan (default 5)")
    parser.add_argument("--min-age-days", type=float,
                        default=float(os.environ.get("TARTCI_RECLAIM_MIN_AGE_DAYS", "30")),
                        help="always reclaim a build dir idle at least this long (default 30)")
    parser.add_argument("--pressure-free-gb", type=float,
                        default=float(os.environ.get("TARTCI_RECLAIM_PRESSURE_FREE_GB", "200")),
                        help="free space below which the shorter age gate applies (default 200)")
    parser.add_argument("--pressure-min-age-days", type=float,
                        default=float(os.environ.get("TARTCI_RECLAIM_PRESSURE_MIN_AGE_DAYS", "7")),
                        help="age gate used under disk pressure (default 7)")
    parser.add_argument("--fail-below-gb", type=float,
                        default=float(os.environ.get("TARTCI_RECLAIM_FAIL_BELOW_GB", "0")),
                        help="exit 3 when free space is still below this after the pass (0 disables)")
    parser.add_argument("--log-path",
                        default=os.environ.get("TARTCI_RECLAIM_LOG"),
                        help="log file to rotate aside at startup once it reaches --log-max-bytes (default: $TARTCI_RECLAIM_LOG; unset disables rotation)")
    parser.add_argument("--log-max-bytes", type=int,
                        default=int(os.environ.get("TARTCI_RECLAIM_LOG_MAX_BYTES", str(8 * 1024 * 1024))),
                        help="rotate the log once it reaches this size (default 8 MiB)")
    parser.add_argument("--log-generations", type=int,
                        default=int(os.environ.get("TARTCI_RECLAIM_LOG_GENERATIONS", "5")),
                        help="how many rotated generations to keep (default 5)")
    parser.add_argument("--fix", action="store_true",
                        help="actually delete; without it the pass is a dry run")
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Every other knob widens or narrows what is examined. These two decide
    # whether anything is examined at all: a zero age gate deletes every
    # generated tree the scan reaches the moment it reaches it, and a
    # non-positive depth makes `find_candidates` return nothing while still
    # exiting 0, which is a janitor that reports success for doing nothing.
    if args.maxdepth < 1:
        print("disk_reclaim: --maxdepth must be at least 1", file=sys.stderr)
        return 2
    for name, value in (("--min-age-days", args.min_age_days),
                        ("--pressure-min-age-days", args.pressure_min_age_days)):
        if value <= 0:
            print(f"disk_reclaim: {name} must be greater than zero",
                  file=sys.stderr)
            return 2
    # Before anything is printed: this pass appends to that file, and an
    # unbounded receipt on the volume we are protecting is the problem.
    if args.log_path:
        rotate_log(pathlib.Path(args.log_path).expanduser(),
                   args.log_max_bytes, args.log_generations)
    roots = parse_roots(args.roots)
    if not roots:
        print("disk_reclaim: no scan roots", file=sys.stderr)
        return 2

    # A root that does not exist or cannot be entered is a configuration
    # fault, not an empty host. Skipping it silently is what lets a rendered
    # plist point at the wrong volume and report a clean exit 0 forever while
    # the disk it was installed to protect fills up.
    unusable = []
    for root in roots:
        if not root.is_dir():
            unusable.append(f"{root} (not a directory)")
        elif not os.access(root, os.R_OK | os.X_OK):
            unusable.append(f"{root} (not readable)")
    if unusable:
        print("disk_reclaim: unusable scan root(s): " + ", ".join(unusable),
              file=sys.stderr)
        return 2

    now = time.time()
    volumes_before = volumes_free_bytes(roots)
    free_before = tightest_free_bytes(volumes_before)
    # An unknown free figure selects the LONGER gate. Guessing "full" here
    # would make an unreadable volume delete more aggressively than a healthy
    # one, which is exactly backwards.
    # Pressure is per volume, and so is the gate it selects. A low boot disk
    # is a real reason to reclaim the boot disk's build directories sooner; it
    # is not a reason to delete week-old builds off a Workshop volume that has
    # 1.6 TiB free, and the shorter gate is the one direction where being
    # wrong destroys work. An unknown free figure keeps the LONGER gate, the
    # same direction the single-volume code already took.
    short_gate = min(args.min_age_days, args.pressure_min_age_days)
    pressured_devices = {
        volume["device"] for volume in volumes_before
        if volume["device"] is not None
        and volume["free_bytes"] is not None
        and volume["free_bytes"] < args.pressure_free_gb * GIB
    }
    pressure = bool(pressured_devices)

    def min_age_for(path: pathlib.Path) -> float:
        device = device_id(path)
        if device is None or device not in pressured_devices:
            return args.min_age_days
        return short_gate

    # Reported as the tightest gate any candidate can meet, so the summary
    # line still names a single number; the per-volume detail is in the JSON.
    min_age = short_gate if pressure else args.min_age_days

    progress = Progress()
    progress.emit(
        f"pass starting: {len(roots)} root(s), mode "
        f"{'fix' if args.fix else 'dry-run'}, min age {min_age}d", force=True)

    active = active_command_lines()
    candidates = find_candidates(roots, args.maxdepth)
    progress.emit(f"scanned {len(candidates)} candidate(s)", force=True)

    deleted: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    reclaimed = 0

    for index, path in enumerate(candidates, start=1):
        progress.emit(
            f"{index}/{len(candidates)} examined, "
            f"{reclaimed / GIB:.1f} GiB so far")
        candidate_min_age = min_age_for(path)
        delete, reason, age_days = classify(
            path, now=now, min_age_days=candidate_min_age, active=active)
        if not delete:
            kept.append({"path": str(path), "reason": reason,
                         "age_days": round(age_days, 1)})
            continue
        size = dir_size_bytes(path)
        record = {"path": str(path), "reason": reason,
                  "age_days": round(age_days, 1), "size_bytes": size}
        if args.fix:
            # Liveness was sampled once, before the first `du -sk`, and this
            # pass can run for minutes. Re-read the age immediately before the
            # irreversible act: a build that started (or a relative command
            # line the argv guard cannot see) has touched this tree since, so
            # it no longer clears the gate. One bounded stat walk against an
            # operation that already pays for a recursive unlink.
            recheck = newest_mtime(path, RECHECK_MAXDEPTH)
            if recheck is None or \
                    (time.time() - recheck) / 86400.0 < candidate_min_age:
                record["reason"] = "touched_during_pass"
                kept.append(record)
                continue
            # A checkout can be created inside the tree between the scan and
            # here, and this is the last moment it can still be saved.
            nested_now, nested_readable = nested_source_marker(path)
            if not nested_readable:
                record["reason"] = "nested_scan_unreadable"
                kept.append(record)
                continue
            if nested_now is not None:
                record["reason"] = f"nested_source_tree ({nested_now})"
                kept.append(record)
                continue
            # Forced, never rate limited: an rmtree is the longest single
            # operation in the pass AND the only one that can leave a tree no
            # later pass can classify. If the process is killed mid-unlink,
            # this line is the sole record of which tree was half deleted.
            progress.emit(f"removing {path} ({size / GIB:.1f} GiB)", force=True)
            try:
                shutil.rmtree(path)
            except OSError as exc:
                record["error"] = str(exc)
                kept.append(record)
                continue
        # Counted in both modes: under --fix this is what was freed, in a dry
        # run it is what a --fix pass would free. Accumulating only under --fix
        # made every dry run report 0.0 GiB, which defeats the report-only
        # first step every rollout starts with.
        reclaimed += size
        deleted.append(record)

    volumes_after = volumes_free_bytes(roots) if args.fix else volumes_before
    free_after = tightest_free_bytes(volumes_after)
    report = {
        "process_scan_ok": active is not None,
        "roots": [str(root) for root in roots],
        "mode": "fix" if args.fix else "dry-run",
        "pressure": pressure,
        "min_age_days": min_age,
        # Recorded beside the count it produced: a receipt saying "165
        # candidates" cannot be read without knowing how deep the scan
        # went, and the depth that missed the worktree nest was invisible
        # in exactly this way until someone measured it by hand.
        "maxdepth": args.maxdepth,
        "candidates": len(candidates),
        "deleted": deleted,
        "kept": kept,
        "reclaimed_bytes": reclaimed,
        "free_bytes_before": free_before,
        "free_bytes_after": free_after,
        "free_bytes_by_volume_before": volumes_before,
        "free_bytes_by_volume_after": volumes_after,
    }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        verb = "removed" if args.fix else "would remove"
        freed_verb = "reclaimed" if args.fix else "would reclaim"
        for record in deleted:
            print(f"  {verb} {record['size_bytes'] / GIB:6.1f} GiB  {record['path']}")
        tightest_root = tightest_volume_root(volumes_after)
        free_text = "unknown" if free_after is None else (
            f"{free_after / GIB:.1f} GiB"
            + (f" on {tightest_root}" if tightest_root and len(volumes_after) > 1
               else ""))
        print(f"disk_reclaim: {len(candidates)} candidate(s) under "
              f"{', '.join(str(r) for r in roots)}; {verb} {len(deleted)}; "
              f"{freed_verb} {reclaimed / GIB:.1f} GiB; "
              f"free {free_text} "
              f"(pressure={'yes' if pressure else 'no'}, age gate {min_age:g}d)")
        if active is None:
            print("disk_reclaim: process scan unavailable, so nothing was "
                  "eligible for deletion this pass.", file=sys.stderr)
        if not args.fix:
            print("disk_reclaim: re-run with --fix to delete.")

    if active is None:
        print("disk_reclaim: could not read the process table (pgrep), so no "
              "build directory could be proven idle. Deleted nothing.",
              file=sys.stderr)
        return 4

    if args.fail_below_gb > 0 and free_after is None:
        print("disk_reclaim: could not read free space, so the "
              f"{args.fail_below_gb:g} GiB floor could not be checked.",
              file=sys.stderr)
        return 4

    if args.fail_below_gb > 0 and free_after < args.fail_below_gb * GIB:
        tightest_root = tightest_volume_root(volumes_after)
        where = f" on {tightest_root}" if tightest_root else ""
        print(f"disk_reclaim: FREE SPACE STILL LOW after reclaim: "
              f"{free_after / GIB:.1f} GiB{where} < {args.fail_below_gb:g} GiB floor. "
              f"This host will refuse leases (disk_capacity_exceeded).",
              file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
