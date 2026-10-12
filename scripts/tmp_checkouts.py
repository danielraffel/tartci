#!/usr/bin/env python3
"""Remove finished git checkouts from /tmp and the worktree root; report the rest.

Why this exists: agents create Pulp worktrees and clones under /private/tmp
although the host rule forbids it (pulp_reapers.TMP_WORKTREE_RULE). /tmp is on
the boot volume, outside every reaper root, so they pile up where nothing
looks: m5 held 432 git checkouts there on 2026-09-27. The reclaim lane already
counted them (`worktrees_in_tmp`) and never removed one.

A checkout directly under the scan root is removed only when every gate holds:

  * no process has its cwd inside it and no live build command line names it;
  * it has been idle for `tmp_checkout_idle_hours` (default 48): the newest
    mtime of the directory, its top-level entries, and its git index/HEAD;
  * `git status --porcelain` is empty (untracked files count as dirty);
  * HEAD is reachable from a remote-tracking ref, and for a plain clone so is
    every local branch, and the clone has no stash;
  * a worktree is removed with `git worktree remove` WITHOUT --force, so git's
    own refusals (submodules, locks, dirt) stand and are reported;
  * a plain clone (a `.git` directory) is removed with rmtree.

A worktree whose parent repository is gone (its `.git` file points at a
gitdir that no longer exists) is ORPHANED, and a `.git` directory without a
HEAD is BROKEN (m5 held 72 such husks). Both are reported, never removed,
because git can no longer tell us whether they hold unpushed work.

Anything that cannot be measured (lsof, git, a stat) keeps the checkout. The
pass also has a time budget; checkouts it did not reach are counted as
`deferred`, never as kept-for-cause.

The same rules apply to the host's worktree root (`worktrees_root`, the
directory PULP_WORKTREES_ROOT names), where agents are supposed to put their
checkouts. On m3 on 2026-09-29 it held 1,026 entries and 1.8 TiB, 622 GiB of it
created in the two days after 09-27, while the Workshop volume went from 70% to
84% and its I/O stalled. There, three more gates apply to every root:

  * a checkout whose `.pulp-build-active` marker names a live pid is in use;
  * a clone other worktrees point into is kept (`has_worktrees`): removing it
    would orphan them;
  * a branch Pulp's worktree lineage marks `active` is kept (`lineage_active`):
    its owner decides.

A host whose agents also leave worktrees in a directory that holds real clones
(m5s's internal ~/Code: 229 worktrees of an old primary checkout, 923 GiB,
after its worktree root moved to /Volumes/Atelier) names it in
`extra_worktree_roots`. There only linked worktrees are eligible, under every
gate above; a plain clone is always kept (`clone_in_shared_root`), because in
a developer's Code directory a clone is someone's checkout, not scratch.

Keep-verdicts that only a change to the checkout can reverse are cached
(~/.tartci/state/reclaim/checkout-verdicts.json) while the checkout's newest
mtime is unchanged, for at most a day, so hundreds of idle checkouts are not
re-`git status`ed every hour. A stale cached verdict can only keep a checkout.

Opt-in per host through the installed fleet profile:

    [reclaim]
    tmp_checkouts = true              # /private/tmp
    worktree_root_checkouts = true    # reclaim.worktrees_root
    tmp_checkout_idle_hours = 48      # optional, 24..720
    extra_worktree_roots = ["/Users/me/Code"]   # optional; worktrees only
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - launchd hosts run 3.11+
    tomllib = None  # type: ignore[assignment]

DEFAULT_ROOT = "/private/tmp"
DEFAULT_IDLE_HOURS = 48
MIN_IDLE_HOURS = 24
MAX_IDLE_HOURS = 720
GIT_TIMEOUT_S = 120
DU_TIMEOUT_S = 60
PASS_BUDGET_S = 1200
LIST_LIMIT = 20
# Keep-verdicts that only a change to the checkout can reverse. A verdict is
# reused while the checkout's newest mtime is unchanged, for at most a day, so
# a worktree root of hundreds of checkouts is not re-`git status`ed hourly.
# A stale cached verdict can only keep a checkout longer, never remove one.
CACHEABLE = frozenset({"dirty", "unpushed", "stash", "has_worktrees", "lineage_active"})
CACHE_TTL_S = 24 * 3600

Runner = Callable[..., subprocess.CompletedProcess]


def validate(table: dict[str, Any]) -> list[str]:
    """Problems with the tmp_checkouts keys of a `[reclaim]` table."""
    problems = []
    enabled = table.get("tmp_checkouts", False)
    if type(enabled) is not bool:
        problems.append("reclaim.tmp_checkouts must be a boolean")
    idle = table.get("tmp_checkout_idle_hours", DEFAULT_IDLE_HOURS)
    if type(idle) is not int or not MIN_IDLE_HOURS <= idle <= MAX_IDLE_HOURS:
        problems.append("reclaim.tmp_checkout_idle_hours must be an integer from "
                        f"{MIN_IDLE_HOURS} through {MAX_IDLE_HOURS}")
    root = table.get("worktree_root_checkouts", False)
    if type(root) is not bool:
        problems.append("reclaim.worktree_root_checkouts must be a boolean")
    elif root and not isinstance(table.get("worktrees_root"), str):
        problems.append("reclaim.worktree_root_checkouts needs reclaim.worktrees_root")
    extra = table.get("extra_worktree_roots", [])
    if not isinstance(extra, list) or not all(
            isinstance(r, str) and r.startswith("/") and r.rstrip("/")
            and ".." not in r.split("/") for r in extra):
        problems.append("reclaim.extra_worktree_roots must be a list of absolute paths "
                        "other than /")
    return problems


def load_settings(profile: pathlib.Path) -> tuple[dict[str, Any] | None, str]:
    """The enabled settings, else (None, why it is off)."""
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
    if not isinstance(table, dict) or (table.get("tmp_checkouts") is not True
                                       and table.get("worktree_root_checkouts") is not True
                                       and not table.get("extra_worktree_roots")):
        return None, (f"neither [reclaim] tmp_checkouts nor worktree_root_checkouts "
                      f"is true, and no extra_worktree_roots, in {profile}")
    problems = validate(table)
    if problems:
        return None, "; ".join(problems)
    roots = []
    if table.get("tmp_checkouts") is True:
        roots.append(os.environ.get("TARTCI_TMP_CHECKOUT_ROOT", DEFAULT_ROOT))
    if table.get("worktree_root_checkouts") is True:
        roots.append(table["worktrees_root"])
    extra = [r for r in table.get("extra_worktree_roots", []) if r not in roots]
    return {"idle_hours": table.get("tmp_checkout_idle_hours", DEFAULT_IDLE_HOURS),
            "roots": roots + extra, "worktrees_only": extra}, "enabled"


def process_cwds(runner: Runner = subprocess.run) -> list[str] | None:
    """Every process cwd on the host, or None when lsof could not answer."""
    try:
        proc = runner(["lsof", "-w", "-d", "cwd", "-Fn"], capture_output=True, text=True,
                      timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    cwds = [line[1:] for line in proc.stdout.splitlines() if line.startswith("n/")]
    # lsof exits 1 when some processes could not be inspected; an empty
    # listing is never a real answer on a live host (lsof itself has a cwd).
    if not cwds:
        return None
    return cwds


def _inside(path: str, spellings: set[str]) -> bool:
    return any(path == s or path.startswith(s + os.sep) for s in spellings)


def _spellings(path: pathlib.Path) -> set[str]:
    out = {str(path)}
    try:
        out.add(str(path.resolve()))
    except OSError:
        pass
    # /tmp is a symlink to /private/tmp on macOS; either may appear.
    for spelling in list(out):
        if spelling.startswith("/private/tmp/"):
            out.add(spelling[len("/private"):])
        elif spelling.startswith("/tmp/"):
            out.add("/private" + spelling)
    return out


def _git(path: pathlib.Path, *args: str, runner: Runner) -> subprocess.CompletedProcess:
    # --no-optional-locks: `git status` must not refresh the index, or asking
    # would itself make the checkout look recently used (the idle gate reads
    # the index mtime) and hide it from the next pass for another 48 h.
    return runner(["git", "--no-optional-locks", "-C", str(path), *args],
                  capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False)


def gitdir_of(checkout: pathlib.Path) -> tuple[str, pathlib.Path | None]:
    """("clone"|"worktree"|"orphan"|"broken"|"unreadable", the usable gitdir).

    "broken" is a `.git` directory git no longer recognises (no HEAD): a husk
    left by an interrupted clone or removal, which git cannot vouch for.
    """
    dot_git = checkout / ".git"
    if dot_git.is_dir() and not dot_git.is_symlink():
        if not (dot_git / "HEAD").is_file():
            return "broken", None
        return "clone", dot_git
    try:
        text = dot_git.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unreadable", None
    line = next((ln for ln in text.splitlines() if ln.startswith("gitdir:")), None)
    if line is None:
        return "unreadable", None
    target = pathlib.Path(line[len("gitdir:"):].strip())
    if not target.is_absolute():
        target = checkout / target
    if not target.is_dir():
        return "orphan", None
    return "worktree", target


def newest_mtime(checkout: pathlib.Path, gitdir: pathlib.Path) -> float | None:
    """Newest mtime of the checkout, its top-level entries and git HEAD/index."""
    newest = 0.0
    try:
        newest = checkout.stat().st_mtime
        for entry in os.scandir(checkout):
            newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
    except OSError:
        return None
    for name in ("index", "HEAD", "logs/HEAD"):
        try:
            newest = max(newest, (gitdir / name).stat().st_mtime)
        except FileNotFoundError:
            continue
        except OSError:
            return None
    return newest


def unpushed_reason(checkout: pathlib.Path, kind: str, runner: Runner) -> str | None:
    """Why this checkout still holds work the remotes do not, or None.

    The gitdir exists (the caller checked), so a git failure here is a real
    refusal to answer and keeps the checkout.
    """
    status = _git(checkout, "status", "--porcelain", runner=runner)
    if status.returncode != 0:
        return "git_error"
    if status.stdout.strip():
        return "dirty"
    head = _git(checkout, "rev-list", "-1", "HEAD", "--not", "--remotes", runner=runner)
    if head.returncode != 0:
        return "git_error"
    if head.stdout.strip():
        return "unpushed"
    if kind == "clone":
        # A worktree's other branches and stash live in its parent repository,
        # which is not being removed; a clone's live nowhere else.
        branches = _git(checkout, "rev-list", "-1", "--branches", "--not", "--remotes",
                        runner=runner)
        if branches.returncode != 0:
            return "git_error"
        if branches.stdout.strip():
            return "unpushed"
        stash = _git(checkout, "stash", "list", runner=runner)
        if stash.returncode != 0:
            return "git_error"
        if stash.stdout.strip():
            return "stash"
    return None


def size_bytes(path: pathlib.Path, runner: Runner) -> int | None:
    try:
        proc = runner(["du", "-sk", str(path)], capture_output=True, text=True,
                      timeout=DU_TIMEOUT_S, check=False)
        return int(proc.stdout.split()[0]) * 1024
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def remove(checkout: pathlib.Path, kind: str, gitdir: pathlib.Path,
           runner: Runner) -> str | None:
    """Remove one checkout; returns the refusal, or None when it is gone."""
    if kind == "worktree":
        common = _git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir",
                      runner=runner)
        if common.returncode != 0 or not common.stdout.strip():
            return "git_error"
        proc = runner(["git", "--git-dir", common.stdout.strip(), "worktree", "remove",
                       str(checkout)], capture_output=True, text=True,
                      timeout=GIT_TIMEOUT_S, check=False)
        if proc.returncode != 0 or checkout.exists():
            return "worktree_remove_refused"
        return None
    try:
        shutil.rmtree(checkout)
    except OSError:
        return "remove_failed"
    return None


def build_marker_live(checkout: pathlib.Path) -> bool:
    """Pulp's governed build writes `.pulp-build-active` (pid=...) at the tree
    root while it runs; a live pid there is a build in progress."""
    try:
        text = (checkout / ".pulp-build-active").read_text(errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        if line.startswith("pid="):
            try:
                os.kill(int(line[4:].strip()), 0)
            except (ValueError, ProcessLookupError):
                return False
            except PermissionError:
                return True
            return True
    return False


def holds_worktrees(gitdir: pathlib.Path, kind: str) -> str | None:
    """A clone that other worktrees point into is their repository: removing
    it would orphan every one of them."""
    if kind != "clone":
        return None
    registered = gitdir / "worktrees"
    try:
        return "has_worktrees" if registered.is_dir() and any(registered.iterdir()) else None
    except OSError:
        return "has_worktrees"


def lineage_active(checkout: pathlib.Path, runner: Runner) -> str | None:
    """Pulp's worktree lineage (tools/scripts/worktree_lineage.sh) marks a
    branch `active` while an agent owns it; that owner decides, not us."""
    branch = _git(checkout, "symbolic-ref", "--short", "-q", "HEAD", runner=runner).stdout.strip()
    if not branch:
        return None
    status = _git(checkout, "config", "--get", f"branch.{branch}.pulpWorktreeStatus",
                  runner=runner).stdout.strip()
    return "lineage_active" if status == "active" else None


def scan(root: pathlib.Path, *, fix: bool, idle_hours: int,
         in_use: Callable[[pathlib.Path], bool] | None,
         runner: Runner = subprocess.run, now: float | None = None,
         budget_s: float = PASS_BUDGET_S, cwds: list[str] | None | bool = True,
         cache: dict | None = None, next_cache: dict | None = None,
         worktrees_only: bool = False,
         ) -> dict[str, Any]:
    """One pass over the git checkouts directly under `root`.

    `worktrees_only` keeps every plain clone (an extra_worktree_roots root).

    `in_use` is the caller's live-build test (None when the process table
    could not be read, which removes nothing). `cwds` defaults to lsof.
    `cache` holds earlier keep-verdicts, `next_cache` receives this pass's.
    """
    now = time.time() if now is None else now
    started = time.monotonic()
    report: dict[str, Any] = {
        "root": str(root), "mode": "fix" if fix else "dry-run", "idle_hours": idle_hours,
        "checkouts": 0, "removed": 0, "removed_bytes": 0, "removed_paths": [],
        "kept": {}, "deferred": 0, "orphaned_worktrees": 0, "orphaned_paths": [],
        "broken_checkouts": 0, "broken_paths": [],
    }
    if cwds is True:
        cwds = process_cwds(runner)
    blind = in_use is None or cwds is None
    if blind:
        report["error"] = ("process table unreadable (lsof or pgrep), so no checkout "
                           "could be proven idle; removed nothing")

    def keep(reason: str) -> None:
        report["kept"][reason] = report["kept"].get(reason, 0) + 1

    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError as exc:
        report["error"] = f"cannot list {root}: {exc}"
        return report
    for entry in entries:
        checkout = pathlib.Path(entry.path)
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        if not os.path.lexists(checkout / ".git"):
            continue
        report["checkouts"] += 1
        if time.monotonic() - started > budget_s:
            report["deferred"] += 1
            continue
        kind, gitdir = gitdir_of(checkout)
        if kind == "orphan":
            report["orphaned_worktrees"] += 1
            if len(report["orphaned_paths"]) < LIST_LIMIT:
                report["orphaned_paths"].append(str(checkout))
            continue
        if kind == "broken":
            report["broken_checkouts"] += 1
            if len(report["broken_paths"]) < LIST_LIMIT:
                report["broken_paths"].append(str(checkout))
            continue
        if kind == "unreadable" or gitdir is None:
            keep("not_git")
            continue
        if worktrees_only and kind == "clone":
            keep("clone_in_shared_root")
            continue
        if blind:
            keep("process_scan_unavailable")
            continue
        spellings = _spellings(checkout)
        assert isinstance(cwds, list)
        if any(_inside(cwd, spellings) for cwd in cwds) or in_use(checkout) \
                or build_marker_live(checkout):
            keep("in_use")
            continue
        newest = newest_mtime(checkout, gitdir)
        if newest is None:
            keep("unmeasurable")
            continue
        if now - newest < idle_hours * 3600:
            keep("recent")
            continue
        remembered = (cache or {}).get(str(checkout))
        if (isinstance(remembered, dict) and remembered.get("newest") == newest
                and remembered.get("reason") in CACHEABLE
                and now - float(remembered.get("at", 0)) < CACHE_TTL_S):
            # Nothing at the top of the checkout or in its index/HEAD moved
            # since git last said why it must stay. Re-asked daily regardless.
            keep(remembered["reason"])
            if next_cache is not None:
                next_cache[str(checkout)] = remembered
            continue
        try:
            reason = holds_worktrees(gitdir, kind) or lineage_active(checkout, runner) \
                or unpushed_reason(checkout, kind, runner)
        except subprocess.TimeoutExpired:
            reason = "git_timeout"
        if reason:
            keep(reason)
            if next_cache is not None and reason in CACHEABLE:
                next_cache[str(checkout)] = {"newest": newest, "reason": reason, "at": now}
            continue
        size = size_bytes(checkout, runner)
        if fix:
            try:
                refusal = remove(checkout, kind, gitdir, runner)
            except subprocess.TimeoutExpired:
                refusal = "git_timeout"
            if refusal:
                keep(refusal)
                continue
        report["removed"] += 1
        report["removed_bytes"] += size or 0
        if len(report["removed_paths"]) < LIST_LIMIT:
            report["removed_paths"].append(str(checkout))
    return report


SUMMED = ("checkouts", "removed", "removed_bytes", "deferred", "orphaned_worktrees",
          "broken_checkouts")
LISTED = ("removed_paths", "orphaned_paths", "broken_paths")


def state_dir() -> pathlib.Path:
    home = os.environ.get("TARTCI_HOME", str(pathlib.Path.home() / ".tartci"))
    return pathlib.Path(home).expanduser() / "state" / "reclaim"


def combine(reports: list[dict[str, Any]], fix: bool, idle_hours: int) -> dict[str, Any]:
    """One report with the pass-level shape, plus each root's own under `by_root`."""
    out: dict[str, Any] = {"mode": "fix" if fix else "dry-run", "idle_hours": idle_hours,
                           "kept": {}, "by_root": {}}
    for key in SUMMED:
        out[key] = sum(int(r.get(key) or 0) for r in reports)
    for key in LISTED:
        out[key] = [p for r in reports for p in (r.get(key) or [])][:LIST_LIMIT]
    errors = []
    for report in reports:
        for reason, count in (report.get("kept") or {}).items():
            out["kept"][reason] = out["kept"].get(reason, 0) + count
        if report.get("error"):
            errors.append(f"{report.get('root')}: {report['error']}")
        out["by_root"][str(report.get("root"))] = {
            key: report.get(key) for key in (*SUMMED, "kept", "error")}
    if errors:
        out["error"] = "; ".join(errors)
    out["root"] = ", ".join(str(r.get("root")) for r in reports)
    return out


def run(*, fix: bool, profile: pathlib.Path,
        in_use: Callable[[pathlib.Path], bool] | None,
        root: pathlib.Path | None = None, runner: Runner = subprocess.run,
        verdicts: pathlib.Path | None = None) -> dict[str, Any]:
    """One opted-in pass over every configured root. Never raises."""
    settings, why = load_settings(profile)
    if settings is None:
        return {"enabled": False, "reason": why}
    roots = [root] if root is not None else [pathlib.Path(r) for r in settings["roots"]]
    verdicts = verdicts or state_dir() / "checkout-verdicts.json"
    try:
        cache = json.loads(verdicts.read_text())
        cache = cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        cache = {}
    next_cache: dict = {}
    reports = []
    try:
        cwds = process_cwds(runner)
        for one in roots:
            reports.append(scan(one, fix=fix, idle_hours=settings["idle_hours"],
                                in_use=in_use, runner=runner, cwds=cwds,
                                cache=cache, next_cache=next_cache,
                                worktrees_only=str(one) in settings.get("worktrees_only", [])))
    except Exception as exc:  # noqa: BLE001 - a janitor must not take the pass down
        return {"enabled": True, "error": f"checkout scan failed: {exc}"}
    try:
        verdicts.parent.mkdir(parents=True, exist_ok=True)
        tmp = verdicts.with_name(f".{verdicts.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(next_cache, sort_keys=True))
        os.replace(tmp, verdicts)
    except OSError:
        pass
    report = combine(reports, fix, settings["idle_hours"])
    report["enabled"] = True
    return report
