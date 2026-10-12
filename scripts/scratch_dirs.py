#!/usr/bin/env python3
"""Remove stale test and validation scratch from the boot volume's temp roots.

Why this exists: on 2026-10-01 m3's boot data volume went from 57 GiB free to
2 GiB in a day while everything m3 builds is supposed to live on Workshop.
The space was scratch that test runs and Shipyard's local validation leave
in the two temp roots on the boot volume when they are killed or cannot
remove what they made:

  * /private/tmp/shipyard-validation-*: Shipyard's per-run TMPDIR. A run that
    is killed never drops it, and its own cleanup cannot remove a pack a Pulp
    test installed read-only (`pulp-fetch-install-*/<sha>/ui.js`). One held
    28 GiB.
  * $TMPDIR/tmp*/older-clean-clone: a Pulp test's full `git clone
    --no-hardlinks` of a 20 GiB repository, left whole or half-copied when the
    test is killed mid-clone.
  * $TMPDIR/pulp-* and shipyard-test-* prefixes named in PATTERNS below.
  * .../X/com.google.Chrome.code_sign_clone/code_sign_clone.*: the copy of
    Google Chrome.app Chrome makes at launch and removes only on an orderly
    exit; every killed headless Chrome leaves one (168 on m3). Chrome's own
    $TMPDIR/com.google.Chrome.* scratch is left the same way (932 on m3).
  * $TMPDIR/pulp-<words>-<mkdtemp suffix>: any Pulp test or tool scratch.
    1,505 had built up on m3 in ten days, from 30-odd tests that were killed
    or never cleaned up. A suffix must hold a digit, capital or underscore,
    so a fixed-name directory a tool reuses on purpose never matches.
  * ~/Library/Developer/Xcode/DerivedData/*: Xcode's build products, which an
    agent's xcodebuild leaves at 3 to 8 GiB a project (39 GiB on m3). They
    are rebuilt on the next build, and are removed only after
    DERIVED_DATA_IDLE_HOURS (14 days) with nothing inside modified.

/private/tmp and the per-user temp dir are outside every other reclaim root
(build dirs and checkouts), so nothing removed them.

An entry is removed only when every gate holds:

  * it is a direct child of its pattern's root, matches that pattern, is a
    real directory (not a symlink) and is owned by the user running the pass;
  * no process has a file open inside it (one `lsof` listing per pass) and no
    process command line or environment names it (`ps -E`; a live Shipyard
    validation exports it as TMPDIR). Either listing failing removes nothing;
  * nothing inside it, within a bounded walk, was modified inside the idle
    gate (`scratch_idle_hours`, default 12; `PRESSURE_IDLE_HOURS` while the
    boot volume is below its floor), re-read immediately before removal.

Removal makes the directories it owns writable first, because the leak this
exists for is a read-only tree nothing else could remove. Patterns are an
explicit list: anything not named here is never examined.

Opt-in per host through the installed fleet profile:

    [reclaim]
    scratch_dirs = true
    scratch_idle_hours = 12           # optional, 6..720
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - launchd hosts run 3.11+
    tomllib = None  # type: ignore[assignment]

DEFAULT_IDLE_HOURS = 12
MIN_IDLE_HOURS = 6
MAX_IDLE_HOURS = 720
# The gate while the boot volume is below its floor. Long enough that a live
# run has rewritten something inside its scratch, short enough to matter on a
# disk that lost 55 GiB in a day.
PRESSURE_IDLE_HOURS = 4
LSOF_TIMEOUT_S = 180
PS_TIMEOUT_S = 60
DU_TIMEOUT_S = 120
# The idle walk: deep enough to see a running test's files under
# shipyard-validation-X/<test-dir>/<sub>/, bounded so a 20 GiB clone is not
# fully stat'ed every hour. A walk that hits the cap keeps the entry.
WALK_MAXDEPTH = 4
WALK_MAX_ENTRIES = 50_000
PASS_BUDGET_S = 900
# DerivedData is a cache a developer may come back to; two weeks untouched
# means nobody is building that project here.
DERIVED_DATA_IDLE_HOURS = 14 * 24
LIST_LIMIT = 20

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Pattern:
    name: str
    root: str          # key into the roots mapping: private_tmp | user_tmp | chrome_clone | derived_data
    regex: str         # full match against the entry's basename
    only_child: str | None = None  # the entry must contain exactly this one name
    min_idle_hours: float = 0      # an idle gate this pattern never goes below


# pulp-<words>-<suffix> or pulp-<words>.<suffix> (BSD mktemp -t), the shape of
# a Pulp temp dir: a random suffix holding a digit, capital or underscore, or a
# -<pid>-<tick>-<n> counter. Fixed names such as pulp-audio-doctor, or
# pulp-control-501 (one per uid), never match.
PULP_MKDTEMP = (r"pulp-[a-z0-9]+(?:-[a-z0-9]+)*"
                r"(?:[-.](?=[A-Za-z0-9_]*[0-9A-Z_])[A-Za-z0-9_]{6,}|-[0-9]+(?:-[0-9]+){2,})")


PATTERNS: tuple[Pattern, ...] = (
    Pattern("shipyard-validation", "private_tmp", r"shipyard-validation-[A-Za-z0-9]{6}"),
    Pattern("older-clean-clone", "user_tmp", r"tmp[a-z0-9_]{8}", only_child="older-clean-clone"),
    Pattern("pulp-test:user_tmp", "user_tmp",
            r"(?:pulp-version-bump-proof-|pulp-generated-bump-test-)[a-z0-9_]{8}"
            r"|pulp-(?:authority-cold|fetch-install|fetch-src|fetch-fallback)-[0-9-]+"
            r"|" + PULP_MKDTEMP),
    Pattern("pulp-test:private_tmp", "private_tmp",
            r"(?:pulp-version-bump-proof-|pulp-generated-bump-test-)[a-z0-9_]{8}"
            r"|pulp-(?:authority-cold|fetch-install|fetch-src|fetch-fallback)-[0-9-]+"),
    Pattern("shipyard-test-codex", "user_tmp", r"shipyard-test-codex-[A-Za-z0-9]{6}"),
    Pattern("chrome-code-sign-clone", "chrome_clone", r"code_sign_clone\.[A-Za-z0-9]{6}"),
    Pattern("chrome-temp", "user_tmp",
            r"com\.google\.Chrome\.(?:[A-Za-z_]+\.)?[A-Za-z0-9]{6}"),
    Pattern("xcode-derived-data", "derived_data", r"[A-Za-z0-9][A-Za-z0-9_.+-]*",
            min_idle_hours=DERIVED_DATA_IDLE_HOURS),
)


def validate(table: dict[str, Any]) -> list[str]:
    problems = []
    if type(table.get("scratch_dirs", False)) is not bool:
        problems.append("reclaim.scratch_dirs must be a boolean")
    idle = table.get("scratch_idle_hours", DEFAULT_IDLE_HOURS)
    if type(idle) is not int or not MIN_IDLE_HOURS <= idle <= MAX_IDLE_HOURS:
        problems.append("reclaim.scratch_idle_hours must be an integer from "
                        f"{MIN_IDLE_HOURS} through {MAX_IDLE_HOURS}")
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
    if not isinstance(table, dict) or table.get("scratch_dirs") is not True:
        return None, f"[reclaim] scratch_dirs is not true in {profile}"
    problems = validate(table)
    if problems:
        return None, "; ".join(problems)
    return {"idle_hours": table.get("scratch_idle_hours", DEFAULT_IDLE_HOURS)}, "enabled"


def user_temp_dir(runner: Runner = subprocess.run) -> pathlib.Path | None:
    """The per-user temp dir (/var/folders/.../T), whatever TMPDIR says.

    A launchd agent's TMPDIR is normally this directory, but an operator shell
    running tartci reclaim may export another; the leak lives in the real one.
    """
    try:
        proc = runner(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True,
                      text=True, timeout=10, check=False)
        value = proc.stdout.strip()
        if proc.returncode == 0 and value:
            return pathlib.Path(value)
    except (OSError, subprocess.SubprocessError):
        pass
    value = os.environ.get("TMPDIR", "").strip()
    return pathlib.Path(value) if value else None


def default_roots(runner: Runner = subprocess.run) -> dict[str, pathlib.Path]:
    roots = {"private_tmp": pathlib.Path("/private/tmp")}
    user_tmp = user_temp_dir(runner)
    if user_tmp is not None:
        roots["user_tmp"] = user_tmp
        # Chrome clones itself into the per-user temp dir's sibling X/.
        roots["chrome_clone"] = (user_tmp.resolve().parent / "X"
                                 / "com.google.Chrome.code_sign_clone")
    roots["derived_data"] = pathlib.Path.home() / "Library/Developer/Xcode/DerivedData"
    return roots


def spellings(path: pathlib.Path) -> set[str]:
    """Every way a process may name `path`: /tmp vs /private/tmp, /var vs /private/var."""
    out = {str(path)}
    try:
        out.add(str(path.resolve()))
    except OSError:
        pass
    for spelling in list(out):
        for short, full in (("/tmp/", "/private/tmp/"), ("/var/", "/private/var/")):
            if spelling.startswith(full):
                out.add(spelling[len("/private"):])
            elif spelling.startswith(short):
                out.add("/private" + spelling)
    return out


def open_paths(runner: Runner = subprocess.run) -> list[str] | None:
    """Every path any process holds open (files, cwds, mapped executables).

    None when lsof could not answer: an empty listing is never real on a live
    host, because lsof itself has open files.
    """
    try:
        proc = runner(["lsof", "-w", "-n", "-P", "-Fn"], capture_output=True, text=True,
                      timeout=LSOF_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    paths = [line[1:] for line in proc.stdout.splitlines() if line.startswith("n/")]
    return paths or None


def process_texts(runner: Runner = subprocess.run) -> list[str] | None:
    """Every process's command line, with its environment where ps may show it.

    A running Shipyard validation exports its scratch as TMPDIR and may hold
    nothing open there between steps; its environment is what names it.
    """
    try:
        proc = runner(["ps", "-axwwE", "-o", "command="], capture_output=True,
                      text=True, timeout=PS_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.splitlines()
    if proc.returncode != 0 or not lines:
        return None
    return lines


def newest_mtime(path: pathlib.Path, maxdepth: int = WALK_MAXDEPTH,
                 max_entries: int = WALK_MAX_ENTRIES) -> float | None:
    """Newest mtime within `maxdepth` levels, or None when it cannot be known.

    A walk that hits `max_entries` returns None: it has not seen everything it
    was asked to, so it cannot vouch for the tree being idle.
    """
    try:
        newest = path.lstat().st_mtime
    except OSError:
        return None
    seen = 0
    frontier = [(path, 0)]
    while frontier:
        directory, depth = frontier.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > max_entries:
                        return None
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    newest = max(newest, info.st_mtime)
                    if depth + 1 < maxdepth and stat.S_ISDIR(info.st_mode):
                        frontier.append((pathlib.Path(entry.path), depth + 1))
        except PermissionError:
            # A read-only tree we own is still listable; one we cannot list is
            # one we cannot vouch for.
            return None
        except FileNotFoundError:
            continue
        except OSError:
            return None
    return newest


def size_bytes(path: pathlib.Path, runner: Runner = subprocess.run) -> int | None:
    try:
        proc = runner(["du", "-sk", str(path)], capture_output=True, text=True,
                      timeout=DU_TIMEOUT_S, check=False)
        return int(proc.stdout.split()[0]) * 1024
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def make_owned_tree_writable(path: pathlib.Path, uid: int) -> None:
    """Add owner rwx to every directory under `path` this user owns.

    Removal needs write permission on the directory holding an entry; a
    read-only installed pack (`dr-x------`) is exactly what blocked every
    earlier cleanup. Files need nothing, and nothing owned by another user is
    changed (chmod would fail anyway, and the rmtree then reports it).
    """
    for directory, subdirs, _files in os.walk(path, topdown=True, followlinks=False):
        for name in [directory, *(os.path.join(directory, d) for d in subdirs)]:
            try:
                info = os.lstat(name)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode) and info.st_uid == uid \
                    and info.st_mode & stat.S_IRWXU != stat.S_IRWXU:
                try:
                    os.chmod(name, info.st_mode | stat.S_IRWXU)
                except OSError:
                    pass


def remove_tree(path: pathlib.Path, uid: int) -> str | None:
    """Remove `path`; returns the error, or None when it is gone."""
    make_owned_tree_writable(path, uid)
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return str(exc)
    return None


def matches(pattern: Pattern, entry: os.DirEntry) -> bool:
    if not re.fullmatch(pattern.regex, entry.name):
        return False
    if pattern.only_child is None:
        return True
    try:
        return os.listdir(entry.path) == [pattern.only_child]
    except OSError:
        return False


def scan(roots: dict[str, pathlib.Path], *, fix: bool, idle_hours: float,
         runner: Runner = subprocess.run, now: float | None = None,
         uid: int | None = None, opened: list[str] | None | bool = True,
         texts: list[str] | None | bool = True,
         budget_s: float = PASS_BUDGET_S,
         remover: Callable[[pathlib.Path, int], str | None] = remove_tree,
         ) -> dict[str, Any]:
    """One pass over every pattern's root. `opened`/`texts` default to lsof/ps."""
    now = time.time() if now is None else now
    uid = os.geteuid() if uid is None else uid
    started = time.monotonic()
    report: dict[str, Any] = {
        "mode": "fix" if fix else "dry-run", "idle_hours": idle_hours,
        "candidates": 0, "removed": 0, "removed_bytes": 0, "removed_paths": [],
        "kept": {}, "deferred": 0, "errors": [],
        "roots": {key: str(value) for key, value in roots.items()},
        "by_pattern": {},
    }
    if opened is True:
        opened = open_paths(runner)
    if texts is True:
        texts = process_texts(runner)
    blind = opened is None or texts is None
    if blind:
        report["error"] = ("open-file or process listing unavailable (lsof or ps), so "
                           "no scratch could be proven idle; removed nothing")

    def keep(reason: str) -> None:
        report["kept"][reason] = report["kept"].get(reason, 0) + 1

    for pattern in PATTERNS:
        root = roots.get(pattern.root)
        if root is None:
            continue
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name)
        except FileNotFoundError:
            continue
        except OSError as exc:
            report["errors"].append(f"cannot list {root}: {exc}")
            continue
        tally = report["by_pattern"].setdefault(
            pattern.name, {"candidates": 0, "removed": 0, "removed_bytes": 0})
        gate_hours = max(idle_hours, pattern.min_idle_hours)
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            if not matches(pattern, entry):
                continue
            path = pathlib.Path(entry.path)
            report["candidates"] += 1
            tally["candidates"] += 1
            if time.monotonic() - started > budget_s:
                report["deferred"] += 1
                continue
            try:
                owner = entry.stat(follow_symlinks=False).st_uid
            except OSError:
                keep("unmeasurable")
                continue
            if owner != uid:
                keep("other_owner")
                continue
            if blind:
                keep("process_scan_unavailable")
                continue
            names = spellings(path)
            assert isinstance(opened, list) and isinstance(texts, list)
            if any(p == s or p.startswith(s + os.sep) for p in opened for s in names):
                keep("open_files")
                continue
            if any(s in text for text in texts for s in names):
                keep("named_by_process")
                continue
            newest = newest_mtime(path)
            if newest is None:
                keep("unmeasurable")
                continue
            if now - newest < gate_hours * 3600:
                keep("recent")
                continue
            size = size_bytes(path, runner)
            if fix:
                # The listings and the walk were taken before this; re-read the
                # age immediately before the irreversible act.
                recheck = newest_mtime(path)
                if recheck is None or time.time() - recheck < gate_hours * 3600:
                    keep("touched_during_pass")
                    continue
                error = remover(path, uid)
                if error is not None:
                    keep("remove_failed")
                    if len(report["errors"]) < LIST_LIMIT:
                        report["errors"].append(f"{path}: {error}")
                    continue
            report["removed"] += 1
            report["removed_bytes"] += size or 0
            tally["removed"] += 1
            tally["removed_bytes"] += size or 0
            if len(report["removed_paths"]) < LIST_LIMIT:
                report["removed_paths"].append(str(path))
    return report


def run(*, fix: bool, profile: pathlib.Path, pressure: bool = False,
        roots: dict[str, pathlib.Path] | None = None,
        runner: Runner = subprocess.run) -> dict[str, Any]:
    """One opted-in pass. `pressure` (the boot volume is below its floor)
    selects the shorter idle gate. Never raises."""
    settings, why = load_settings(profile)
    if settings is None:
        return {"enabled": False, "reason": why}
    idle = settings["idle_hours"]
    if pressure:
        idle = min(idle, PRESSURE_IDLE_HOURS)
    try:
        report = scan(roots if roots is not None else default_roots(runner),
                      fix=fix, idle_hours=idle, runner=runner)
    except Exception as exc:  # noqa: BLE001 - a janitor must not take the pass down
        return {"enabled": True, "error": f"scratch scan failed: {exc}"}
    report["enabled"] = True
    report["pressure"] = pressure
    return report
