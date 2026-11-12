#!/usr/bin/env python3
"""Quarantine structurally invalid ccache direct-mode manifests in a shared cache.

The macOS gate VMs share one host ccache over virtio-fs. On 2026-09-26 m3's
cache accumulated thousands of direct-mode manifests that listed ZERO include
files and named the result of a DIFFERENT source (the object for
audio_doctor.cpp served for wav_bridge.cpp). A manifest with no include files
has nothing to re-check on lookup, so it matches every time, and the wrong
object was linked on every build on that host: the gate failed with an
undefined symbol whose definition was plainly compiled into another archive.
The writer mechanism is unproven; this guard does not depend on it. It
inspects what the cache holds before a job reads it.

What is flagged
---------------
Direct-mode manifests that list zero include files, the only shape that can
serve a foreign object unconditionally. Each is classified by the result it
names:

  zero_include_suspect     the result's dependency file lists headers (the
                           manifest cannot be describing that object), or the
                           result is missing, or it cannot be checked
  zero_include_consistent  the result's dependency file lists only a source,
                           which is what a legitimate include-less TU produces
                           (Pulp compiles about a hundred: generated
                           control-shipping markers, placeholder.cpp)

By default only suspects are quarantined; consistent ones stay and are
counted. A proven-consistent manifest is remembered in
<quarantine-root>/verdicts.json by (size, mtime), so it costs one
--extract-result once rather than on every boot.

Residual blind spot of the default: a poisoned manifest that names ANOTHER
include-less TU's object is indistinguishable from a legitimate one (a
real-ccache test reproduces it) and is kept. --all-zero-include closes that
by quarantining every zero-include manifest, at the cost of one
preprocessor-mode lookup per include-less TU on the next build; reset always
runs that way.

Which roots
-----------
The host cache directory itself, plus <cache>/tartci-layers-v1/shared when
per-job write isolation is in use. Only the single-hex-digit fan-out
directories of a root hold entries, so the per-job jobs/, green/ and discard/
layers (other VMs, possibly mid-build) are never scanned or moved.

Quarantine, never delete
------------------------
A flagged manifest is renamed (same filesystem, atomic) to
<quarantine-root>/<UTC stamp>/<path relative to the cache>; the batch gets a
report.json naming every file and its verdict, and one JSON line per run is
appended to <quarantine-root>/guard.log with the counts. A reader racing the
rename gets either the old file or a miss, both safe for ccache. Results are
never moved: a result is a real object for SOME source, and the fault is the
manifest that points at it. Batches older than --retain-days (default 30) are
pruned; guard.log is kept.

Budget and resume
-----------------
The runner gives the pre-boot quarantine a time budget. A run the budget cuts
short records the unit it stopped in (<quarantine-root>/cursor.json; a unit is
one fan-out directory's own files or one of its subdirectories), and the next
quarantine run starts there, walks to the end and wraps to the beginning. So
on a cache too large to walk inside one budget, successive boots still check
every directory in turn instead of rechecking the same head every time. A run
that covers the whole cache removes the cursor. scan and reset always walk
the whole cache from the start and neither read nor move the cursor.

Default-safe
------------
The guard reads entries through the host's own ccache --inspect and
--extract-result, so it never re-implements ccache's format. Anything it
cannot read is left alone and counted as uninspectable. A run that cannot
take its lock (another guard on the same cache) skips, unless it was given
--wait[=SECS]: then it waits up to SECS (bare --wait: 120) for the lock and
skips only if it is still held. The runner never waits; the operator CLI
(tartci ccache ...) waits 120 s by default, because an operator's quarantine
that lands while a pre-boot guard holds the lock otherwise skips and has to be
retried by hand. The runner calls it
fail-open: a guard failure is logged and never blocks a job. It takes the
cache directory as an argument, so it composes with any layering that decides
which directory a job reads.

Commands
--------
  ccache_guard.py scan       --cache DIR [--all-zero-include] [--json]
  ccache_guard.py quarantine --cache DIR [--quarantine-root DIR]
                             [--all-zero-include] [--budget SECS]
                             [--wait[=SECS]] [--json]
  ccache_guard.py reset      --cache DIR [--quarantine-root DIR] [--reset]
                             [--force] [--plan] [--json]

reset is the operator command (tartci ccache reset). It refuses while a
Tart VM runs or a VM lease is held on the host (exit 3) unless --force.
Without --reset it runs the quarantine in --all-zero-include mode; with --reset it also moves every
remaining cache entry into the quarantine batch, leaving an empty cache
(ccache's config and stats files stay). It logs before and after entry counts.
--plan reports what it would do and changes nothing.

Exit codes: 0 done (or nothing to do), 1 setup error, 3 refused busy,
4 skipped (lock held or no ccache binary), 5 budget exhausted (partial run).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAGIC = b"\xcc\xac"
ENTRY_TYPE_MANIFEST = 1
# A zero-include manifest naming one result is 84-89 bytes (all 5,287 on m5 on
# 2026-09-27); each further result adds a few dozen. Any manifest that lists an
# include path is far larger. The cap only limits which files the guard
# bothers to --inspect, which is most of its cost on a 450k-entry cache.
DEFAULT_SIZE_CAP = 1024
# ccache's entry fan-out: one hex digit per top-level directory.
FANOUT = set("0123456789abcdef")
# Per-job write isolation keeps its layers here, beside the fan-out.
LAYER_DIR = "tartci-layers-v1"
VERDICT_CACHE = "verdicts.json"
# Where a budget-exhausted quarantine run stopped (see iter_from).
CURSOR_FILE = "cursor.json"
INSPECT_TIMEOUT = 20
# Dependency-file inputs that are not includes (see result_verdict).
IMPLICIT_DEP_SUFFIXES = (".json", ".modulemap")
DEFAULT_RETAIN_DAYS = 30
STAMP_RE = re.compile(r"^\d{8}T\d{6}Z(-\d+)?$")

EXIT_OK, EXIT_SETUP, EXIT_BUSY, EXIT_SKIPPED, EXIT_BUDGET = 0, 1, 3, 4, 5
# The budget's clock. Tests replace it to exhaust a run at an exact entry.
CLOCK = time.monotonic
DEFAULT_LOCK_WAIT_S = 120.0
MAX_LOCK_WAIT_S = 3600.0


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def resolve_ccache(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    found = shutil.which("ccache")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ccache", "/usr/local/bin/ccache"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def iter_units(cache: Path):
    """The scan units of ONE ccache root, in walk order: (top, sub) pairs.

    Only the single-hex-digit fan-out directories hold entries, so anything
    else at the top (tmp, lock, and the per-job layer tree) is never walked.
    A unit is either the files directly in a fan-out directory (sub "") or
    one of its subdirectories walked recursively. With ccache's two-level
    layout that is about 272 units per root, small enough that one always
    fits a guard budget, which is what lets a cursor resume between them.
    """
    try:
        tops = sorted(os.scandir(cache), key=lambda e: e.name)
    except OSError:
        return
    for top in tops:
        if top.name not in FANOUT or not top.is_dir(follow_symlinks=False):
            continue
        yield top.name, ""
        try:
            subs = sorted(e.name for e in os.scandir(top.path)
                          if e.name != "tmp" and e.is_dir(follow_symlinks=False))
        except OSError:
            continue
        for sub in subs:
            yield top.name, sub


def iter_unit_entries(root: Path, top: str, sub: str):
    """Every cache entry file in one unit (see iter_units), in walk order."""
    def wanted(name: str) -> bool:
        return not name.startswith(".") and name not in ("stats", "CACHEDIR.TAG")

    if not sub:
        try:
            names = sorted(e.name for e in os.scandir(root / top)
                           if e.is_file(follow_symlinks=False))
        except OSError:
            return
        for name in names:
            if wanted(name):
                yield root / top / name
        return
    for dirpath, dirnames, filenames in os.walk(root / top / sub):
        dirnames[:] = sorted(d for d in dirnames if d != "tmp")
        for name in sorted(filenames):
            if wanted(name):
                yield Path(dirpath) / name


def iter_entries(cache: Path):
    """Every cache entry file under ONE ccache root's fan-out directories."""
    for top, sub in iter_units(cache):
        yield from iter_unit_entries(cache, top, sub)


def cache_roots(cache: Path) -> list[Path]:
    """The ccache roots a job can READ under a host cache directory.

    The legacy root itself, plus the promoted shared layer when per-job write
    isolation is in use (<cache>/tartci-layers-v1/shared). The jobs/, green/
    and discard/ layers belong to VMs that may be mid-build and are never
    scanned.
    """
    roots = [cache]
    shared = cache / LAYER_DIR / "shared"
    if shared.is_dir():
        roots.append(shared)
    return roots


def iter_all_entries(cache: Path):
    """(root, entry) for every entry in every readable root of a host cache."""
    for root in cache_roots(cache):
        for entry in iter_entries(root):
            yield root, entry


def unit_key(cache: Path, root: Path, top: str, sub: str) -> list[str]:
    """A unit's cursor form: [root relative to the cache, top, sub]."""
    rel = os.path.relpath(root, cache)
    return [rel, top, sub]


def iter_from(cache: Path, start: list[str] | None):
    """(unit key, root, entry) over every root, starting at the start unit.

    Units from start to the end come first, then the units before it, so a
    walk that resumes a cursor still covers the whole cache once. A cursor
    naming a unit that no longer exists starts at the next unit after it in
    walk order; one naming an unknown root starts at the beginning.
    """
    roots = cache_roots(cache)
    units = [(index, root, top, sub) for index, root in enumerate(roots)
             for top, sub in iter_units(root)]
    first = 0
    if start is not None:
        rels = [os.path.relpath(root, cache) for root in roots]
        if start[0] in rels:
            target = (rels.index(start[0]), start[1], start[2])
            first = next((i for i, (index, _r, top, sub) in enumerate(units)
                          if (index, top, sub) >= target), len(units))
    for index, root, top, sub in units[first:] + units[:first]:
        key = unit_key(cache, root, top, sub)
        for entry in iter_unit_entries(root, top, sub):
            yield key, root, entry


def load_cursor(qroot: Path) -> list[str] | None:
    try:
        value = json.loads((qroot / CURSOR_FILE).read_text()).get("unit")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(value, list) and len(value) == 3 and all(isinstance(v, str) for v in value):
        return value
    return None


def save_cursor(qroot: Path, unit: list[str] | None) -> None:
    """Record where an exhausted run stopped; a complete run removes it."""
    path = qroot / CURSOR_FILE
    if unit is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    qroot.mkdir(parents=True, exist_ok=True)
    tmp = qroot / (CURSOR_FILE + ".tmp")
    tmp.write_text(json.dumps({"unit": unit, "ts": utc_stamp()}, sort_keys=True))
    os.replace(tmp, path)


def is_manifest_header(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            header = handle.read(4)
    except OSError:
        return False
    return len(header) == 4 and header[:2] == MAGIC and header[3] == ENTRY_TYPE_MANIFEST


# Host-side ccache never reads the shared cache's own ccache.conf. Every gate
# VM mounts that directory read-write, so a guest can write it, and ccache
# would otherwise act on keys such as `prefix_command`. The host's calls only
# read entries and counters, so they take no configuration at all.
HOST_CCACHE_CONFIG = os.devnull


def host_ccache_env(**overrides: str) -> dict[str, str]:
    """The environment for a host ccache call against a guest-writable cache."""
    return {**os.environ, **overrides, "CCACHE_CONFIGPATH": HOST_CCACHE_CONFIG}


def inspect_manifest(ccache: str, path: Path) -> dict | None:
    """Parse ccache --inspect for a manifest. None when unreadable."""
    try:
        proc = subprocess.run([ccache, "--inspect", str(path)], capture_output=True,
                              text=True, timeout=INSPECT_TIMEOUT, env=host_ccache_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout
    if not re.search(r"^Entry type: 1\b", out, re.M):
        return None
    paths = re.search(r"^File paths \((\d+)\)", out, re.M)
    if not paths:
        return None
    created = re.search(r"^Creation time: (\d+)", out, re.M)
    return {
        "file_paths": int(paths.group(1)),
        "result_keys": re.findall(r"^\s+Key: ([0-9a-f]{8,})\s*$", out, re.M),
        "created": int(created.group(1)) if created else None,
    }


def result_file(cache: Path, key: str) -> Path | None:
    """ccache stores key K at K[0]/K[1]/K[2:] (older layouts add an R suffix)."""
    if len(key) < 3:
        return None
    base = cache / key[0] / key[1] / key[2:]
    for candidate in (base, base.with_name(base.name + "R")):
        if candidate.is_file():
            return candidate
    return None


def dep_inputs(text: str) -> list[str]:
    """Prerequisites of the first rule of a make-style dependency file."""
    joined = text.replace("\\\n", " ")
    first = joined.split("\n", 1)[0]
    _, sep, rest = first.partition(":")
    if not sep:
        return []
    return [tok for tok in rest.split() if tok]


def result_verdict(ccache: str, cache: Path, key: str) -> str:
    """consistent | has_headers | missing | unverifiable."""
    path = result_file(cache, key)
    if path is None:
        return "missing"
    with tempfile.TemporaryDirectory(prefix="ccache-guard-") as tmp:
        try:
            proc = subprocess.run([ccache, "--extract-result", str(path.resolve())], cwd=tmp,
                                  capture_output=True, text=True, timeout=INSPECT_TIMEOUT,
                                  env=host_ccache_env())
        except (OSError, subprocess.TimeoutExpired):
            return "unverifiable"
        if proc.returncode != 0:
            return "unverifiable"
        deps = sorted(p for p in Path(tmp).iterdir() if p.suffix == ".d")
        if not deps:
            return "unverifiable"
        inputs = dep_inputs(deps[0].read_text(errors="replace"))
    # Clang lists implicit inputs the preprocessor never includes
    # (SDKSettings.json on macOS, sometimes before the source); ccache does not
    # record those in a manifest either. What remains is the source plus every
    # include, so an include-less TU leaves exactly one.
    inputs = [p for p in inputs if not p.endswith(IMPLICIT_DEP_SUFFIXES)]
    if not inputs:
        return "unverifiable"
    return "consistent" if len(inputs) == 1 else "has_headers"


def classify(ccache: str, cache: Path, info: dict, all_zero: bool) -> tuple[str, dict]:
    """(action, detail): action is 'keep' or 'quarantine'."""
    if info["file_paths"] > 0:
        return "keep", {}
    verdicts = {key: result_verdict(ccache, cache, key) for key in info["result_keys"]}
    consistent = bool(verdicts) and all(v == "consistent" for v in verdicts.values())
    detail = {"verdict": "zero_include_consistent" if consistent else "zero_include_suspect",
              "results": verdicts}
    if consistent and not all_zero:
        return "keep", detail
    return "quarantine", detail


class Lock:
    """Non-blocking exclusive lock; two guards on one cache never interleave."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self, wait_s: float = 0.0, poll_s: float = 0.25) -> bool:
        """Take the lock, retrying for up to wait_s seconds (0: one try).

        Bounded polling rather than a blocking flock, so a holder that never
        lets go costs at most wait_s and the caller still reports a skip.
        """
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+")
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.handle.close()
                self.handle = None
                return False
            time.sleep(min(poll_s, remaining))

    def release(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def move_into(src: Path, cache: Path, dest_root: Path) -> Path:
    dest = dest_root / src.relative_to(cache)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dest)
    return dest


def load_verdicts(qroot: Path) -> dict:
    try:
        value = json.loads((qroot / VERDICT_CACHE).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_verdicts(qroot: Path, verdicts: dict) -> None:
    qroot.mkdir(parents=True, exist_ok=True)
    tmp = qroot / (VERDICT_CACHE + ".tmp")
    tmp.write_text(json.dumps(verdicts, sort_keys=True))
    os.replace(tmp, qroot / VERDICT_CACHE)


def run_scan(cache: Path, ccache: str, *, all_zero: bool, size_cap: int,
             quarantine_dir: Path | None, deadline: float | None,
             verdicts: dict | None = None, start: list[str] | None = None) -> dict:
    """Walk every readable root; quarantine what classify() flags.

    verdicts (relative path -> [size, mtime_ns]) remembers manifests already
    proven consistent, so a legitimate include-less TU's manifest is checked
    once, not on every boot. An entry is re-checked whenever its size or
    mtime changes. The dict is updated in place with what this run saw.

    start is a cursor unit to begin at (iter_from). A run the deadline cuts
    short reports the unit it was in as cursor, so the next run checks the
    rest of the cache first instead of rescanning the same head every time.
    """
    counts = {"entries": 0, "manifests_checked": 0, "zero_include": 0,
              "zero_include_consistent": 0, "zero_include_suspect": 0,
              "consistent_cached": 0, "flagged": 0, "quarantined": 0,
              "quarantine_errors": 0, "uninspectable": 0}
    flagged: list[dict] = []
    budget_exhausted = False
    seen: dict = {}
    cursor = None
    for unit, root, entry in iter_from(cache, start):
        if deadline is not None and CLOCK() > deadline:
            budget_exhausted = True
            cursor = unit
            break
        counts["entries"] += 1
        try:
            st = os.lstat(entry)
        except OSError:
            continue
        if st.st_size > size_cap:
            continue
        rel = str(entry.relative_to(cache))
        stamp = [st.st_size, st.st_mtime_ns]
        if not all_zero and verdicts is not None and verdicts.get(rel) == stamp:
            counts["zero_include"] += 1
            counts["zero_include_consistent"] += 1
            counts["consistent_cached"] += 1
            seen[rel] = stamp
            continue
        if not is_manifest_header(entry):
            continue
        counts["manifests_checked"] += 1
        info = inspect_manifest(ccache, entry)
        if info is None:
            counts["uninspectable"] += 1
            continue
        action, detail = classify(ccache, root, info, all_zero)
        if info["file_paths"] == 0:
            counts["zero_include"] += 1
            counts[detail["verdict"]] += 1
        if action != "quarantine":
            if detail.get("verdict") == "zero_include_consistent":
                seen[rel] = stamp
            continue
        counts["flagged"] += 1
        row = {"path": rel, "created": info["created"], **detail}
        if quarantine_dir is not None:
            try:
                move_into(entry, cache, quarantine_dir)
                counts["quarantined"] += 1
            except OSError as exc:
                counts["quarantine_errors"] += 1
                row["quarantine_error"] = str(exc)
        flagged.append(row)
    if verdicts is not None and not budget_exhausted:
        verdicts.clear()
        verdicts.update(seen)
    elif verdicts is not None:
        verdicts.update(seen)
    return {"counts": counts, "flagged": flagged, "budget_exhausted": budget_exhausted,
            "cursor": cursor}


def append_log(qroot: Path, record: dict) -> None:
    qroot.mkdir(parents=True, exist_ok=True)
    with open(qroot / "guard.log", "a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def prune_batches(qroot: Path, retain_days: float, now: float | None = None) -> int:
    """Remove quarantine batches older than retain_days. Returns batches removed."""
    if retain_days <= 0 or not qroot.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - retain_days * 86400
    removed = 0
    for child in qroot.iterdir():
        if not child.is_dir() or not STAMP_RE.match(child.name):
            continue
        stamp = dt.datetime.strptime(child.name[:16], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
        if stamp < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def new_batch_dir(qroot: Path) -> Path:
    stamp = utc_stamp()
    candidate = qroot / stamp
    n = 1
    while candidate.exists():
        candidate = qroot / f"{stamp}-{n}"
        n += 1
    return candidate


def same_filesystem(cache: Path, qroot: Path) -> bool:
    """True when a rename from the cache into qroot is atomic (same device)."""
    probe = qroot
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return os.stat(probe).st_dev == os.stat(cache).st_dev
    except OSError:
        return False


def default_quarantine_root(cache: Path) -> Path:
    return cache.parent / (cache.name + "-quarantine")


def host_busy() -> str | None:
    """Why the host's shared cache may be in use, or None when provably idle."""
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    try:
        import lane_busy
        import tartci_launchd_watchdog as watchdog
    except Exception as exc:  # an unimportable probe cannot prove idle
        return f"busy probe unavailable: {exc}"
    probe = watchdog.probe_tart_vm_running()
    if probe.running is None:
        return f"cannot prove the host idle: {probe.reason}"
    if probe.running:
        return "a Tart VM is running on this host"
    leases = lane_busy.lease_records()
    if leases is None:
        return "cannot read the host lease store"
    held = [r for r in leases if r.get("command_kind") in lane_busy.VM_LEASE_KINDS]
    if held:
        return f"{len(held)} VM lease(s) held on this host"
    return None


# Tests replace this to exercise the refusal without a real host.
BUSY_PROBE = host_busy


def emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, sort_keys=True))
        return
    counts = result.get("counts", {})
    print(f"{result.get('command')}: cache={result.get('cache')} "
          + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for key in ("status", "detail", "mode", "quarantine_dir", "before", "after",
                "reset_moved", "forced_over"):
        if key in result:
            print(f"  {key}: {result[key]}")
    for row in result.get("flagged", [])[:20]:
        print(f"  flagged {row['path']} {row.get('verdict')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("scan", "quarantine", "reset"))
    parser.add_argument("--cache", required=True, help="the ccache directory jobs read")
    parser.add_argument("--quarantine-root", help="default: <cache>-quarantine beside it")
    parser.add_argument("--ccache", help="ccache binary (default: PATH, then Homebrew)")
    parser.add_argument("--all-zero-include", action="store_true",
                        help="quarantine every zero-include manifest, consistent ones too")
    parser.add_argument("--size-cap", type=int, default=DEFAULT_SIZE_CAP)
    parser.add_argument("--budget", type=float, default=0.0,
                        help="stop after this many seconds (0: no limit)")
    parser.add_argument("--retain-days", type=float, default=DEFAULT_RETAIN_DAYS,
                        help="prune quarantine batches older than this (0: keep all)")
    parser.add_argument("--reset", action="store_true",
                        help="reset: also move every remaining entry into quarantine")
    parser.add_argument("--force", action="store_true",
                        help="reset: proceed even while the host is busy")
    parser.add_argument("--plan", action="store_true",
                        help="report what would happen, change nothing")
    parser.add_argument("--wait", nargs="?", type=float, const=DEFAULT_LOCK_WAIT_S,
                        default=0.0, metavar="SECS",
                        help="wait up to SECS for another guard's lock instead of skipping "
                             f"(bare --wait: {DEFAULT_LOCK_WAIT_S:g}; max {MAX_LOCK_WAIT_S:g})")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.wait <= MAX_LOCK_WAIT_S:
        parser.error(f"--wait must be from 0 through {MAX_LOCK_WAIT_S:g} seconds")

    cache = Path(args.cache).expanduser().absolute()
    result: dict = {"command": args.command, "cache": str(cache), "ts": utc_stamp()}
    if not cache.is_dir():
        result.update(status="setup_error", detail="cache directory does not exist")
        emit(result, args.json)
        return EXIT_SETUP
    ccache = resolve_ccache(args.ccache)
    if ccache is None:
        result.update(status="skipped", detail="no ccache binary on this host")
        emit(result, args.json)
        return EXIT_SKIPPED
    qroot = Path(args.quarantine_root).expanduser() if args.quarantine_root \
        else default_quarantine_root(cache)
    plan = args.plan or args.command == "scan"
    if not plan and not same_filesystem(cache, qroot):
        result.update(status="setup_error",
                      detail="quarantine root must be on the cache's filesystem (atomic rename)")
        emit(result, args.json)
        return EXIT_SETUP

    if args.command == "reset":
        busy = BUSY_PROBE()
        if busy and not args.force:
            result.update(status="refused_busy", detail=busy + " (pass --force to override)")
            emit(result, args.json)
            return EXIT_BUSY
        if busy:
            result["forced_over"] = busy

    lock = Lock(qroot / ".guard.lock")
    if not plan:
        waited_from = time.monotonic()
        acquired = lock.acquire(args.wait)
        if args.wait:
            result["lock_waited_s"] = round(time.monotonic() - waited_from, 2)
        if not acquired:
            result.update(status="skipped",
                          detail="another guard holds the lock on this cache"
                                 + (f" (waited {args.wait:g}s)" if args.wait else ""))
            emit(result, args.json)
            return EXIT_SKIPPED
    try:
        qdir = None if plan else new_batch_dir(qroot)
        deadline = CLOCK() + args.budget if args.budget > 0 else None
        all_zero = args.all_zero_include or args.command == "reset"
        result["mode"] = "all-zero-include" if all_zero else "suspect-only"
        if args.command == "reset":
            result["before"] = sum(1 for _ in iter_all_entries(cache))
        verdicts = load_verdicts(qroot)
        # Only the budgeted pre-boot quarantine resumes; scan and reset always
        # walk the whole cache from the start.
        resumes = args.command == "quarantine" and not plan
        start = load_cursor(qroot) if resumes else None
        if start is not None:
            result["resumed_from"] = start
        scan = run_scan(cache, ccache, all_zero=all_zero, size_cap=args.size_cap,
                        quarantine_dir=qdir, deadline=deadline, verdicts=verdicts,
                        start=start)
        result.update(scan)
        if resumes:
            save_cursor(qroot, scan["cursor"])
        if args.command == "reset" and args.reset:
            moved = errors = 0
            for _root, entry in list(iter_all_entries(cache)):
                if plan:
                    moved += 1
                    continue
                try:
                    move_into(entry, cache, qdir / "reset")
                    moved += 1
                except OSError:
                    errors += 1
            result["reset_moved"] = moved
            result["reset_errors"] = errors
        if args.command == "reset":
            result["after"] = sum(1 for _ in iter_all_entries(cache))
        if scan["budget_exhausted"]:
            result["status"] = "budget_exhausted"
        else:
            result["status"] = "planned" if (args.plan and args.command != "scan") else "ok"
        if not plan:
            if qdir.exists():
                result["quarantine_dir"] = str(qdir)
                (qdir / "report.json").write_text(json.dumps(result, sort_keys=True, indent=1))
            if not all_zero:
                save_verdicts(qroot, verdicts)
            result["pruned_batches"] = prune_batches(qroot, args.retain_days)
            append_log(qroot, {k: v for k, v in result.items() if k != "flagged"})
    finally:
        lock.release()
    emit(result, args.json)
    return EXIT_BUDGET if scan["budget_exhausted"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
