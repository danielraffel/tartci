#!/usr/bin/env python3
"""Run Pulp's own build-directory reapers from the hourly reclaim pass.

Why this exists: tartci's janitor (disk_reclaim.py) only knows generic build
trees and gates them by age, so under pressure it reclaimed 0 bytes on m3 while
1,330 GB sat in the build directories of 40 MERGED Pulp worktrees. Those are
only provably dead with Pulp's own knowledge (is this exact head merged, is the
worktree's lineage `active`, is a process using it), and Pulp already ships two
reapers that encode exactly that: `tools/scripts/clean_build_cov.sh` and
`tools/scripts/clean_worktree_builds.sh`. This module runs them; it does not
reimplement or relax a single gate of theirs. Their safety is theirs.

Opt-in per host, OFF unless the installed fleet profile says:

    [reclaim]
    pulp_worktree_builds = true
    repo = "/Volumes/<data-volume>/Code/pulp"          # a Pulp checkout
    worktrees_root = "/Volumes/<data-volume>/Code/agent-worktrees"
    pressure_free_gb = 200                        # optional, default 200
    worktree_build_idle_hours = 2                 # optional, 2..24, default 2

When enabled, every pass:

  1. fetches `origin/main` into `repo` and materializes a detached, sparse
     worktree of it (tools/scripts + tools/ci) under the tartci state dir.
     A worktree rather than an export, because both reapers derive the
     repository they police from their own location (`git worktree list`,
     `--git-common-dir`); an export is not a checkout of anything. tools/ci is
     required, not incidental: the worktree reaper imports
     tools/ci/build_dir_lock.py, and a copy without it fails with
     ModuleNotFoundError on the first candidate.
  2. runs `clean_build_cov.sh` (cheap: a two-level find) every pass, and
  3. runs `clean_worktree_builds.sh` only while the volume holding
     `worktrees_root` is below `pressure_free_gb`, because that one walks every
     candidate build tree in full and fetches origin.

Both run with PULP_WORKTREES_ROOT=worktrees_root, `--yes` under `--fix` and in
their own dry-run mode otherwise. Each run is recorded with the free space of
the `worktrees_root` volume before and after, which is the reclaimed figure
this module reports; the reaper's own "~N GB" summary is kept beside it.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import boot_usage  # noqa: E402
import gate_ccache_trim  # noqa: E402
import scratch_dirs  # noqa: E402
import tmp_checkouts  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - launchd hosts run 3.11+
    tomllib = None  # type: ignore[assignment]

GIB = 1024 ** 3
DEFAULT_PRESSURE_FREE_GB = 200.0
SETTINGS_TABLE = "reclaim"
SETTINGS_KEYS = frozenset({"pulp_worktree_builds", "repo", "worktrees_root",
                           "pressure_free_gb", "worktree_build_idle_hours",
                           # read by tmp_checkouts.py; validated here so one
                           # install-time check covers the whole table
                           "tmp_checkouts", "tmp_checkout_idle_hours",
                           "worktree_root_checkouts", "extra_worktree_roots",
                           # read by scratch_dirs.py
                           "scratch_dirs", "scratch_idle_hours",
                           # read by gate_ccache_trim.py
                           "gate_ccache_trim", "gate_ccache_max_age_days",
                           "gate_ccache_trim_interval_hours",
                           # read by boot_usage.py
                           "boot_usage", "boot_usage_warn_gb",
                           "boot_usage_growth_gb_per_day"})
# How long a merged worktree's build tree must sit unwritten before the worktree
# reaper may take it (its own PULP_WORKTREE_BUILD_IDLE_HOURS gate). The ceiling
# is a disk-arithmetic fact, not a preference. Measured on m3, 2026-09-27: one
# 2-day-old build-cov was 40 GB while the volume had 21 GiB free, and a gate VM
# lease needs about 49 GiB, so a single worktree can consume the whole lease
# headroom in about 48 hours. Any idle floor measured in days (tartci's generic
# pressure gate is 7) therefore protects exactly the directory that is starving
# the host: that pass ran in fix mode under pressure and reclaimed 0 bytes. The
# merged / proven / not-in-use / lineage-not-active gates are what make a Pulp
# build dead; the idle window only has to outlast a pause in a live build, so it
# may not exceed a day. The floor of 2 is the reaper's own default: this setting
# can only keep its gate as strict or stricter, never loosen it.
DEFAULT_IDLE_HOURS = 2
MIN_IDLE_HOURS = 2
MAX_IDLE_HOURS = 24
MAIN_REF = "main"
SPARSE_PATHS = ("tools/scripts", "tools/ci")
REQUIRED_FILES = (
    "tools/scripts/clean_build_cov.sh",
    "tools/scripts/clean_worktree_builds.sh",
    "tools/ci/build_dir_lock.py",
)
BUILD_COV = "clean_build_cov"
WORKTREE_BUILDS = "clean_worktree_builds"
# A reaper that runs longer than this is killed and reported, never left to
# hold the hourly slot. The worktree reaper walks every candidate tree in full,
# which on a 1.3 TB backlog is minutes, not an hour.
REAPER_TIMEOUT_S = {BUILD_COV: 1800, WORKTREE_BUILDS: 3000}
GIT_TIMEOUT_S = 300
FS_TIMEOUT_S = 15.0
SUMMARY = re.compile(r"~([0-9]+(?:\.[0-9]+)?) GB (?:reclaimed|reclaimable)")

Runner = Callable[..., subprocess.CompletedProcess]


def bounded_fs_call(operation: Callable[[], Any], *, description: str,
                    timeout: float = FS_TIMEOUT_S,
                    deadline: float | None = None) -> Any:
    """Run one filesystem probe with a deadline and fail closed on a stall."""
    result: dict[str, Any] = {}
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            result["value"] = operation()
        except BaseException as exc:  # noqa: BLE001 - re-raised on caller thread
            errors.append(exc)

    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise OSError("ETIMEDOUT", f"pass deadline reached before {description}")
    worker = threading.Thread(target=invoke, name=f"pulp reaper fs {description}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise OSError("ETIMEDOUT", f"{description} timed out after {timeout:.0f}s")
    if errors:
        raise errors[0]
    return result.get("value")


def _remaining(deadline: float | None) -> float | None:
    """Return the remaining pass budget, or raise before starting a child."""
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("reclaim pass deadline reached")
    return remaining


def _bounded_timeout(default: float, deadline: float | None) -> float:
    remaining = _remaining(deadline)
    return default if remaining is None else min(default, remaining)


def default_profile_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get(
        "TARTCI_FLEET_PROFILE",
        str(pathlib.Path.home() / ".config" / "tartci" / "macos-fleet-profile.toml"),
    )).expanduser()


def default_state_dir() -> pathlib.Path:
    home = os.environ.get("TARTCI_HOME", str(pathlib.Path.home() / ".tartci"))
    return pathlib.Path(home).expanduser() / "state" / "reclaim"


def validate_table(table: Any) -> list[str]:
    """Problems with a `[reclaim]` table; empty when it is acceptable.

    Shared by the install-time profile validator and the runtime reader, so a
    profile that installs is a profile this module will act on.
    """
    if not isinstance(table, dict):
        return ["reclaim must be a table"]
    problems = []
    unknown = set(table) - SETTINGS_KEYS
    if unknown:
        problems.append(f"unknown reclaim keys: {sorted(unknown)}")
    enabled = table.get("pulp_worktree_builds", False)
    if type(enabled) is not bool:
        problems.append("reclaim.pulp_worktree_builds must be a boolean")
    for key in ("repo", "worktrees_root"):
        value = table.get(key)
        if value is None:
            if enabled is True:
                problems.append(f"reclaim.{key} is required when pulp_worktree_builds = true")
            continue
        if not isinstance(value, str) or not value.startswith("/") or ".." in value.split("/"):
            problems.append(f"reclaim.{key} must be an absolute path")
    pressure = table.get("pressure_free_gb", DEFAULT_PRESSURE_FREE_GB)
    if isinstance(pressure, bool) or not isinstance(pressure, (int, float)) \
            or not 1 <= pressure <= 10000:
        problems.append("reclaim.pressure_free_gb must be a number from 1 through 10000")
    idle = table.get("worktree_build_idle_hours", DEFAULT_IDLE_HOURS)
    if type(idle) is not int or not MIN_IDLE_HOURS <= idle <= MAX_IDLE_HOURS:
        problems.append("reclaim.worktree_build_idle_hours must be an integer from "
                        f"{MIN_IDLE_HOURS} through {MAX_IDLE_HOURS}")
    problems.extend(tmp_checkouts.validate(table))
    problems.extend(scratch_dirs.validate(table))
    problems.extend(gate_ccache_trim.validate(table))
    problems.extend(boot_usage.validate(table))
    return problems


def load_settings(profile: pathlib.Path) -> tuple[dict[str, Any] | None, str]:
    """The enabled `[reclaim]` settings, else (None, why it is off)."""
    if tomllib is None:
        return None, "no tomllib (needs Python 3.11+)"
    try:
        with profile.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return None, f"no installed fleet profile at {profile}"
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"fleet profile unreadable: {exc}"
    table = data.get(SETTINGS_TABLE)
    if table is None or (isinstance(table, dict)
                         and table.get("pulp_worktree_builds") is not True):
        return None, f"[{SETTINGS_TABLE}] pulp_worktree_builds = true not set in {profile}"
    problems = validate_table(table)
    if problems:
        return None, "; ".join(problems)
    settings = dict(table)
    settings.setdefault("pressure_free_gb", DEFAULT_PRESSURE_FREE_GB)
    settings.setdefault("worktree_build_idle_hours", DEFAULT_IDLE_HOURS)
    return settings, "enabled"


def skip_reason_for_scan_timeouts(
    timed_out_paths: Iterable[str], profile: pathlib.Path | None = None,
) -> str | None:
    """Return a fail-closed reason when a configured Pulp root was unreadable.

    A reclaim pass must not start Git discovery or a reaper on a volume whose
    scan already timed out. Those operations can block again for the full Git
    timeout and turn one wedged external volume into an hourly queue of stuck
    reclaim processes. This is lexical by design: checking existence or
    resolving a path here could reproduce the same kernel stall we are trying
    to avoid.
    """
    settings, _ = load_settings(profile or default_profile_path())
    if settings is None:
        return None

    def overlap(left: str, right: str) -> bool:
        left = os.path.normpath(os.path.abspath(os.path.expanduser(left)))
        right = os.path.normpath(os.path.abspath(os.path.expanduser(right)))
        try:
            return os.path.commonpath((left, right)) in (left, right)
        except ValueError:
            return False

    watched = (settings["repo"], settings["worktrees_root"])
    for timed_out in timed_out_paths:
        if any(overlap(timed_out, configured) for configured in watched):
            return ("skipped: configured Pulp root overlaps timed-out scan path "
                    f"{timed_out}")
    return None


def free_bytes(path: pathlib.Path, deadline: float | None = None) -> int | None:
    try:
        return bounded_fs_call(lambda: shutil.disk_usage(path).free,
                               description=f"disk usage {path}", deadline=deadline)
    except OSError:
        return None


def used_percent(path: pathlib.Path, deadline: float | None = None) -> float | None:
    """Percent of `path`'s volume in use, or None when it cannot be read."""
    try:
        usage = bounded_fs_call(lambda: shutil.disk_usage(path),
                                description=f"disk usage {path}", deadline=deadline)
    except OSError:
        return None
    return 100.0 * usage.used / usage.total if usage.total else None


def _git(repo: pathlib.Path, *args: str, runner: Runner = subprocess.run,
         deadline: float | None = None) -> subprocess.CompletedProcess:
    timeout = _bounded_timeout(GIT_TIMEOUT_S, deadline)
    return runner(["git", "-C", str(repo), *args], capture_output=True, text=True,
                  timeout=timeout, check=False)


def _common_dir(path: pathlib.Path, runner: Runner,
                deadline: float | None = None) -> str | None:
    proc = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir",
                runner=runner, deadline=deadline)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return os.path.realpath(proc.stdout.strip())


def materialize(repo: pathlib.Path, checkout: pathlib.Path,
                runner: Runner = subprocess.run,
                deadline: float | None = None) -> tuple[pathlib.Path | None, str, str | None]:
    """A sparse detached worktree of `repo` at a fresh origin/main.

    Returns (checkout, detail, sha). `checkout` is None when the scripts could
    not be put in place, and then nothing is run: a stale copy would police
    today's worktrees with yesterday's gates.
    """
    try:
        common = _common_dir(repo, runner, deadline=deadline)
    except TimeoutError as exc:
        return None, str(exc), None
    if common is None:
        return None, f"{repo} is not a git checkout", None
    try:
        fetch = _git(repo, "fetch", "--no-tags", "--quiet", "origin", MAIN_REF,
                     runner=runner, deadline=deadline)
    except TimeoutError as exc:
        return None, str(exc), None
    if fetch.returncode != 0:
        return None, f"git fetch origin {MAIN_REF} failed: {fetch.stderr.strip()[:300]}", None
    try:
        tip = _git(repo, "rev-parse", "--verify", "--quiet",
                   f"refs/remotes/origin/{MAIN_REF}^{{commit}}", runner=runner,
                   deadline=deadline)
    except TimeoutError as exc:
        return None, str(exc), None
    sha = tip.stdout.strip()
    if tip.returncode != 0 or not sha:
        return None, f"origin/{MAIN_REF} has no tip after fetch", None

    # Reuse only a checkout that is provably a worktree of THIS repository.
    # Anything else at our own state path is ours to replace.
    try:
        checkout_common = (
            _common_dir(checkout, runner, deadline=deadline)
            if checkout.exists() else None
        )
    except TimeoutError as exc:
        return None, str(exc), sha
    if checkout.exists() and checkout_common != common:
        shutil.rmtree(checkout, ignore_errors=True)
    if not checkout.exists():
        try:
            _git(repo, "worktree", "prune", runner=runner, deadline=deadline)
        except TimeoutError as exc:
            return None, str(exc), sha
        checkout.parent.mkdir(parents=True, exist_ok=True)
        try:
            add = _git(repo, "worktree", "add", "--detach", "--no-checkout",
                       str(checkout), sha, runner=runner, deadline=deadline)
        except TimeoutError as exc:
            return None, str(exc), sha
        if add.returncode != 0:
            return None, f"git worktree add failed: {add.stderr.strip()[:300]}", sha
    try:
        sparse = _git(checkout, "sparse-checkout", "set", *SPARSE_PATHS,
                      runner=runner, deadline=deadline)
    except TimeoutError as exc:
        return None, str(exc), sha
    if sparse.returncode != 0:
        return None, f"git sparse-checkout failed: {sparse.stderr.strip()[:300]}", sha
    try:
        move = _git(checkout, "checkout", "--detach", "--force", sha,
                    runner=runner, deadline=deadline)
    except TimeoutError as exc:
        return None, str(exc), sha
    if move.returncode != 0:
        return None, f"git checkout {sha[:12]} failed: {move.stderr.strip()[:300]}", sha
    try:
        head = _git(checkout, "rev-parse", "HEAD", runner=runner,
                    deadline=deadline).stdout.strip()
    except TimeoutError as exc:
        return None, str(exc), sha
    if head != sha:
        return None, f"checkout HEAD {head[:12]} is not origin/{MAIN_REF} {sha[:12]}", sha
    missing = [name for name in REQUIRED_FILES if not (checkout / name).is_file()]
    if missing:
        return None, f"origin/{MAIN_REF} checkout lacks {', '.join(missing)}", sha
    return checkout, f"origin/{MAIN_REF} {sha[:12]}", sha


def run_reaper(script: pathlib.Path, *, fix: bool, worktrees_root: str,
               measure_path: pathlib.Path, timeout_s: float,
               idle_hours: int = DEFAULT_IDLE_HOURS,
               stream: Any = None, deadline: float | None = None) -> dict[str, Any]:
    """Run one reaper as-is and record what it did.

    Its output is relayed line by line to `stream` (stderr, which the launchd
    plist points at the reclaim log) as it arrives, so a long reaper keeps the
    log's mtime moving and the log keeps its own receipt of every removal.
    """
    stream = sys.stderr if stream is None else stream
    name = script.stem
    argv = ["/bin/bash", str(script)] + (["--yes"] if fix else [])
    env = {**os.environ, "PULP_WORKTREES_ROOT": worktrees_root,
           "PULP_WORKTREE_BUILD_IDLE_HOURS": str(idle_hours)}
    before = free_bytes(measure_path, deadline)
    started = time.time()
    record: dict[str, Any] = {"reaper": name, "mode": "fix" if fix else "dry-run",
                              "worktrees_root": worktrees_root,
                              "free_bytes_before": before}
    try:
        effective_timeout = _bounded_timeout(timeout_s, deadline)
    except TimeoutError as exc:
        record.update(exit_code=None, error=str(exc), duration_s=0.0,
                      free_bytes_after=before, reclaimed_bytes=0,
                      reason="pass_deadline")
        return record
    lines: list[str] = []
    try:
        proc = subprocess.Popen(argv, cwd=str(script.parents[2]), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
    except OSError as exc:
        record.update(exit_code=None, error=str(exc), duration_s=0.0,
                      free_bytes_after=before, reclaimed_bytes=0)
        return record

    def relay() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            print(f"{name}: {line}", file=stream, flush=True)

    reader = threading.Thread(target=relay, daemon=True)
    reader.start()
    try:
        code: int | None = proc.wait(timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        # A process may ignore SIGKILL while its kernel task is blocked in
        # uninterruptible I/O.  Keep the reclaim pass bounded even then.
        try:
            proc.wait(timeout=min(10.0, effective_timeout))
        except subprocess.TimeoutExpired:
            record["error"] = (
                f"timed out after {effective_timeout:g}s; kill acknowledged "
                "but child did not exit within 10s")
        code = None
        if "error" not in record:
            record["error"] = f"timed out after {effective_timeout:g}s and was killed"
        if deadline is not None and effective_timeout < timeout_s:
            record["reason"] = "pass_deadline"
    reader.join(timeout=10)
    if proc.stdout is not None:
        proc.stdout.close()
    after = free_bytes(measure_path, deadline)
    record["exit_code"] = code
    record["duration_s"] = round(time.time() - started, 1)
    record["free_bytes_after"] = after
    record["reclaimed_bytes"] = (max(0, after - before)
                                 if fix and before is not None and after is not None else 0)
    summary = next((line for line in reversed(lines) if line.startswith(f"{name}:")
                    and SUMMARY.search(line)), None)
    if summary:
        record["summary"] = summary
        record["reported_gb"] = float(SUMMARY.search(summary).group(1))
    return record


# Pulp's rule (CLAUDE.md, "Parallel Work via Worktrees"). Quoted by every report
# so the finding says what is wrong, not only where.
TMP_WORKTREE_RULE = ("Pulp worktrees must live under PULP_WORKTREES_ROOT or a sibling "
                     "of the primary checkout, never /tmp")
TMP_PREFIXES = ("/tmp/", "/private/tmp/")
TMP_DU_TIMEOUT_S = 60
TMP_LIST_LIMIT = 20


def _is_under_tmp(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in TMP_PREFIXES)


def tmp_worktrees(repo: pathlib.Path, runner: Runner = subprocess.run,
                  du_timeout_s: float = TMP_DU_TIMEOUT_S,
                  deadline: float | None = None) -> dict[str, Any]:
    """REPORT ONLY: git worktrees of `repo` that live under /tmp or /private/tmp.

    They are outside every reaper root, and /tmp is shared scratch on the same
    volume, so they fill the disk invisibly (m5 had 162 on 2026-09-27). Nothing
    here deletes or moves them: a worktree may be live, and only its owner can
    say it is finished. Size is one bounded `du`; a timeout reads "unknown",
    never 0.
    """
    out: dict[str, Any] = {"rule": TMP_WORKTREE_RULE, "count": None,
                           "total_bytes": None, "size": "unknown",
                           "oldest_mtime": None, "paths": []}
    try:
        proc = runner(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                      capture_output=True, text=True,
                      timeout=_bounded_timeout(GIT_TIMEOUT_S, deadline), check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        out["error"] = f"git worktree list failed: {exc}"
        return out
    if proc.returncode != 0:
        out["error"] = f"git worktree list failed: {proc.stderr.strip()[:200]}"
        return out
    listed = [line[len("worktree "):] for line in proc.stdout.splitlines()
              if line.startswith("worktree ")]
    found = sorted(path for path in listed if _is_under_tmp(path)
                   or _is_under_tmp(os.path.realpath(path)))
    out["count"] = len(found)
    out["paths"] = found[:TMP_LIST_LIMIT]
    existing = [path for path in found if os.path.isdir(path)]
    mtimes = []
    for path in existing:
        try:
            mtimes.append(os.stat(path).st_mtime)
        except OSError:
            continue
    out["oldest_mtime"] = min(mtimes) if mtimes else None
    if not existing:
        out.update(total_bytes=0, size="0")
        return out
    try:
        du = runner(["du", "-sk", *existing], capture_output=True, text=True,
                    timeout=du_timeout_s, check=False)
        total = sum(int(line.split()[0]) for line in du.stdout.splitlines()
                    if line.split() and line.split()[0].isdigit()) * 1024
        out.update(total_bytes=total, size=f"{total / GIB:.1f} GiB")
    except subprocess.TimeoutExpired:
        out["size"] = f"unknown (du did not finish in {du_timeout_s:g}s)"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        out["size"] = f"unknown ({exc})"
    return out


def normalize_origin(url: str) -> str:
    """`host/path` for any of git@host:path, ssh://git@host/path and https://host/path.

    Lowercased, with a trailing `.git` and `/` removed, so two clones of one
    repository compare equal however each was cloned. A local path is kept as
    a path, normalized the same way.
    """
    text = url.strip()
    for prefix in ("https://", "http://", "ssh://", "git://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            host, _, rest = text.partition("/")
            host = host.split("@", 1)[-1]
            if prefix == "ssh://":
                host = host.split(":", 1)[0]        # ssh://git@host:22/o/r
            text = f"{host}/{rest}"
            break
    else:
        head = text.split("/", 1)[0]
        if ":" in head and "@" in head:          # scp-style git@host:path
            user_host, path = text.split(":", 1)
            text = f"{user_host.split('@', 1)[1]}/{path}"
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-len(".git")]
    return text.rstrip("/").lower()


def origin_of(path: pathlib.Path, runner: Runner,
              deadline: float | None = None) -> str | None:
    """The normalized origin URL of the repository `path` belongs to, or None."""
    proc = _git(path, "config", "--get", "remote.origin.url", runner=runner,
                deadline=deadline)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return normalize_origin(proc.stdout)


def same_origin_worktrees(root: pathlib.Path, origin: str, runner: Runner = subprocess.run,
                          deadline: float | None = None
                          ) -> tuple[list[str], int, list[str], set[str]]:
    """(matching, children_seen, unreadable, clone_roots) over the children of `root`.

    A child counts when its `.git` is a gitdir file (a worktree) and its
    repository's origin URL, read through to the common config, equals
    `origin`. Any clone of the configured repository counts, not only the
    configured clone: on m5s the worktrees in ~/Code belong to a second clone
    of Pulp. A child whose URL cannot be read is never counted.
    """
    matching: list[str] = []
    unreadable: list[str] = []
    clones: set[str] = set()
    seen = 0
    try:
        children = bounded_fs_call(lambda: sorted(root.iterdir()),
                                   description=f"list worktree root {root}", deadline=deadline)
    except OSError:
        return matching, seen, unreadable, clones
    for child in children:
        try:
            is_worktree = bounded_fs_call((child / ".git").is_file,
                                          description=f"check worktree {child}", deadline=deadline)
        except OSError:
            unreadable.append(str(child))
            continue
        if not is_worktree:
            continue
        seen += 1
        try:
            _remaining(deadline)
            url = origin_of(child, runner, deadline=deadline)
        except TimeoutError:
            raise
        if url is None:
            unreadable.append(str(child))
        elif url == origin:
            matching.append(str(child))
            common = _common_dir(child, runner, deadline=deadline)
            if common and os.path.basename(common) == ".git":
                clones.add(os.path.dirname(common))
    return matching, seen, unreadable, clones


def run(*, fix: bool, profile: pathlib.Path | None = None,
        state_dir: pathlib.Path | None = None,
        runner: Runner = subprocess.run, stream: Any = None,
        reaper: Callable[..., dict[str, Any]] = run_reaper,
        discovered_roots: list[pathlib.Path] | None = None,
        deadline: float | None = None,
        skip_reason: str | None = None,
        pressure_used_percent: float = 0.0) -> dict[str, Any]:
    """One pass of the opt-in Pulp reapers. Never raises, never deletes itself.

    `pressure_used_percent` is disk_reclaim's --pressure-used-percent: when
    positive, a volume at least that full is pressure even with
    `pressure_free_gb` to spare, as for disk_reclaim's own build-dir tier.
    """
    profile = profile or default_profile_path()
    settings, why = load_settings(profile)
    if settings is None:
        return {"enabled": False, "reason": why}
    root = pathlib.Path(settings["worktrees_root"])
    report: dict[str, Any] = {
        "enabled": True, "repo": settings["repo"], "worktrees_root": str(root),
        "pressure_free_gb": settings["pressure_free_gb"],
        "worktree_build_idle_hours": settings["worktree_build_idle_hours"], "runs": [],
        "reclaimed_bytes": 0,
        # Present from the start, so "discovery did not run" reads differently
        # from "discovery ran and matched nothing".
        "discovery": {"reason": "not_reached", "roots": [], "children_seen": 0,
                      "configured_origin": None, "unreadable": [],
                      "unreadable_children": 0},
    }
    if skip_reason:
        report.update(reason=skip_reason, skipped={"all": skip_reason})
        return report
    try:
        _remaining(deadline)
        report["worktrees_in_tmp"] = tmp_worktrees(
            pathlib.Path(settings["repo"]), runner=runner, deadline=deadline)
    except TimeoutError as exc:
        report.update(reason=str(exc), skipped={"all": str(exc)})
        return report
    checkout_path = (state_dir or default_state_dir()) / "pulp-reapers"
    try:
        checkout, detail, sha = materialize(pathlib.Path(settings["repo"]),
                                            checkout_path, runner=runner,
                                            deadline=deadline)
    except (OSError, subprocess.SubprocessError) as exc:
        checkout, detail, sha = None, f"could not materialize reapers: {exc}", None
    report["scripts"] = detail
    report["scripts_sha"] = sha
    if checkout is not None:
        # The same fresh origin/main checkout carries Pulp's host-vitals
        # sensor. It is kept current even where the reapers cannot run (no
        # worktree root yet, as on a new host): scripts/host_vitals_sensor.py.
        try:
            import host_vitals_sensor  # noqa: PLC0415 - sibling module
            report["host_vitals_sensor"] = host_vitals_sensor.refresh(
                checkout / "tools" / "scripts", fix)
        except Exception as exc:  # noqa: BLE001 - the reapers still run
            report["host_vitals_sensor"] = {"state": "refresh_failed", "detail": str(exc)}
    try:
        root_is_dir = bounded_fs_call(root.is_dir, description=f"check worktree root {root}",
                                      deadline=deadline)
    except OSError as exc:
        report["error"] = f"worktrees_root probe failed: {exc}"
        return report
    if not root_is_dir:
        report["error"] = f"worktrees_root {root} is not a directory"
        return report
    free = free_bytes(root, deadline)
    report["free_bytes_before"] = free
    # Unknown free space is not pressure: guessing "full" would run the heavier
    # reaper on exactly the host whose volume we cannot read.
    pressure = free is not None and free < settings["pressure_free_gb"] * GIB
    # A large volume can sit past readiness's disk_pressure limit (92% used)
    # with hundreds of GiB free (m3, m5, 2026-10-24/25), so a used-percent
    # threshold is pressure too. An unreadable percent is not.
    used = None
    if not pressure and pressure_used_percent > 0 and free is not None:
        used = used_percent(root, deadline)
        pressure = used is not None and used >= pressure_used_percent
    report["pressure"] = pressure
    report["used_percent"] = used
    if checkout is None:
        report["error"] = detail
        report["free_bytes_after"] = free
        return report
    wanted = [BUILD_COV] + ([WORKTREE_BUILDS] if pressure else [])
    if not pressure:
        report["skipped"] = {WORKTREE_BUILDS: "no pressure: free space is at or above "
                             f"{settings['pressure_free_gb']:g} GiB"
                             + (f" and the volume is under {pressure_used_percent:g}% used"
                                if pressure_used_percent > 0 else "")
                             if free is not None else "free space unreadable"}
    def invoke_reaper(script: pathlib.Path, *, worktrees_root: str,
                      measure_path: pathlib.Path, timeout_s: float) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "fix": fix, "worktrees_root": worktrees_root,
            "measure_path": measure_path, "timeout_s": timeout_s,
            "idle_hours": settings["worktree_build_idle_hours"], "stream": stream,
        }
        # Keep the existing callback contract unchanged for callers that
        # provide a test/dry-run reaper; only the built-in reaper needs the
        # optional pass deadline.
        if deadline is not None:
            kwargs["deadline"] = deadline
        return reaper(script, **kwargs)

    for name in wanted:
        try:
            _remaining(deadline)
        except TimeoutError as exc:
            report.setdefault("skipped", {})[name] = str(exc)
            continue
        record = invoke_reaper(checkout / "tools" / "scripts" / f"{name}.sh",
                               worktrees_root=str(root), measure_path=root,
                               timeout_s=REAPER_TIMEOUT_S[name])
        report["runs"].append(record)
        report["reclaimed_bytes"] += int(record.get("reclaimed_bytes") or 0)
    report["free_bytes_after"] = free_bytes(root, deadline)
    # Agents do not always create worktrees where the profile says: on m5s on
    # 2026-10-04, 32 coverage build dirs (~725 GB) in ~/Code worktrees filled
    # the boot volume while this ran only over the profile's root on another
    # volume. The cheap coverage reaper therefore also runs, every pass, over
    # every discovered Code root that holds worktrees of any clone of this
    # repository. The heavier reaper stays on the configured root.
    report["outside_profile_roots"] = []
    configured = os.path.realpath(root)
    discovery = report["discovery"]
    try:
        _remaining(deadline)
        origin = origin_of(pathlib.Path(settings["repo"]), runner, deadline=deadline)
    except TimeoutError as exc:
        discovery["reason"] = str(exc)
        report["skipped"] = {"discovery": str(exc)}
        return report
    discovery.update(reason=None, configured_origin=origin)
    clones: set[str] = set()
    if origin is None:
        # Fail closed and say so: no identity, no pass outside the profile root.
        discovery["reason"] = "configured_origin_unreadable"
        discovered_roots = []
    for extra in discovered_roots or []:
        resolved = os.path.realpath(extra)
        if resolved == configured:
            continue
        try:
            _remaining(deadline)
            worktrees, seen, unreadable, found = same_origin_worktrees(
                pathlib.Path(resolved), origin, runner, deadline=deadline)
        except TimeoutError as exc:
            discovery["reason"] = str(exc)
            discovery.setdefault("skipped", []).append(resolved)
            continue
        discovery["roots"].append(resolved)
        discovery["children_seen"] += seen
        discovery["unreadable"] += unreadable
        discovery["unreadable_children"] += len(unreadable)
        clones |= found
        if not worktrees:
            continue
        warning = f"worktrees_outside_profile_root root={resolved} count={len(worktrees)}"
        report["outside_profile_roots"].append({"root": resolved, "count": len(worktrees)})
        print(f"pulp_reapers: WARNING {warning} (profile worktrees_root={configured})",
              file=stream or sys.stderr, flush=True)
        try:
            _remaining(deadline)
        except TimeoutError as exc:
            discovery["reason"] = str(exc)
            discovery.setdefault("skipped", []).append(resolved)
            continue
        record = invoke_reaper(checkout / "tools" / "scripts" / f"{BUILD_COV}.sh",
                               worktrees_root=resolved,
                               measure_path=pathlib.Path(resolved),
                               timeout_s=REAPER_TIMEOUT_S[BUILD_COV])
        record["reason"] = "discovered_pulp_root"
        report["runs"].append(record)
        report["reclaimed_bytes"] += int(record.get("reclaimed_bytes") or 0)
    # A clone's in-repo agent worktrees (.claude/worktrees) are under none of
    # the roots above: the reaper's own repo root is the materialized checkout,
    # not any clone. One more run of the same path-based script covers each,
    # the configured clone's included, where the directory exists.
    agent_dirs = [(str(pathlib.Path(settings["repo"]) / ".claude" / "worktrees"),
                   "configured_clone_agent_worktrees")]
    configured_repo = os.path.realpath(settings["repo"])
    agent_dirs += [(os.path.join(clone, ".claude", "worktrees"), "discovered_clone_agent_worktrees")
                   for clone in sorted(clones) if os.path.realpath(clone) != configured_repo]
    for agent_dir, reason in agent_dirs:
        if not os.path.isdir(agent_dir):
            continue
        try:
            _remaining(deadline)
        except TimeoutError as exc:
            report.setdefault("skipped", {})[reason] = str(exc)
            continue
        record = invoke_reaper(checkout / "tools" / "scripts" / f"{BUILD_COV}.sh",
                               worktrees_root=agent_dir,
                               measure_path=pathlib.Path(agent_dir),
                               timeout_s=REAPER_TIMEOUT_S[BUILD_COV])
        record["reason"] = reason
        report["runs"].append(record)
        report["reclaimed_bytes"] += int(record.get("reclaimed_bytes") or 0)
    failed = [r["reaper"] for r in report["runs"] if r.get("exit_code") != 0]
    if failed:
        report["error"] = f"reaper(s) did not exit 0: {', '.join(failed)}"
    return report
