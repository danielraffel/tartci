#!/usr/bin/env python3
"""Keep git's automatic maintenance from stampeding a shared object store.

On m3 at about 07:15Z on 2026-10-16 the load reached 207. Every agent worktree
of the shared pulp checkout shares one object store, and each `git` command
any of them ran could start `git maintenance run --auto` / `git repack -d -l
--cruft` in the background: 53+ of them ran at once (159 git processes, some
for 2.7 h). Three parts, all opt-in per host:

check     (watchdog, every pass) WARN when more than MAX_CONCURRENT `git
          maintenance` / `git repack` / `git gc` processes run on this host,
          naming the repositories and the oldest. Report only.
enforce   for every repository listed in ~/.config/tartci/git-maintenance.repos
          (one absolute path per line), set gc.auto=0, maintenance.auto=false
          and gc.autoDetach=false so no git command starts maintenance on its
          own. The fleet installer runs it; so does every nightly run.
nightly   (watchdog, once a day, only where ~/.config/tartci/git-maintenance.enabled
          exists) run exactly one `nice -n 19 taskpolicy -b git maintenance run
          --task=gc --task=loose-objects` per listed repository, one at a time
          behind a lock, and only while no gate lane is mid-job, no other git
          maintenance/repack/gc runs and the host's load is under 2x its cores.
          Pruning is pinned (gc.pruneExpire=2.weeks.ago,
          gc.worktreePruneExpire=3.months.ago) whatever the repo config says.

    git_maintenance_guard.py check | enforce | nightly [--dry-run]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MAX_CONCURRENT = 3
NIGHTLY_INTERVAL_SECONDS = 20 * 3600
RUN_TIMEOUT_SECONDS = 2 * 3600
TASKPOLICY = "/usr/sbin/taskpolicy"
SETTINGS = (("gc.auto", "0"), ("maintenance.auto", "false"), ("gc.autoDetach", "false"))
MAINTENANCE_RE = re.compile(r"(?:^|/)git(?:\s+-C\s+(\S+))?(?:\s+-c\s+\S+)*\s+"
                            r"(maintenance\s+run|repack|gc)\b")
LANE_GLOB = "com.danielraffel.tartci.tart-runner-macos-fleet.*.plist"
# Pinned so a repo's own gc.pruneExpire=now (or worktreePruneExpire=now) can
# never prune objects concurrent agents just wrote: m3's store has 712 worktrees.
PRUNE_PINS = ("-c", "gc.pruneExpire=2.weeks.ago", "-c", "gc.worktreePruneExpire=3.months.ago")
# The nightly run waits while the host is this loaded (1-minute load per core).
LOAD_PER_CORE = 2.0

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _config(user_home: Path, name: str) -> Path:
    return user_home / ".config" / "tartci" / name


def repos(user_home: Path) -> list[Path]:
    path = Path(os.environ.get("TARTCI_GIT_MAINTENANCE_REPOS")
                or _config(user_home, "git-maintenance.repos"))
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    return [Path(line.strip()) for line in lines
            if line.strip().startswith("/") and not line.strip().startswith("#")]


def enabled(user_home: Path) -> bool:
    return Path(os.environ.get("TARTCI_GIT_MAINTENANCE_ENABLE_FILE")
                or _config(user_home, "git-maintenance.enabled")).is_file()


def _run(argv: list, **kwargs) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)


# ── check ────────────────────────────────────────────────────────────────────

def maintenance_processes(ps_out: str) -> list[dict]:
    """{pid, etime, command, repo} for each git maintenance/repack/gc process."""
    rows = []
    for line in ps_out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        match = MAINTENANCE_RE.search(parts[2])
        if match:
            rows.append({"pid": int(parts[0]), "etime": parts[1], "command": parts[2][:300],
                         "repo": match.group(1) or ""})
    return rows


def cwd_of(pid: int, run: Runner = _run) -> str:
    try:
        out = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        if line.startswith("n/"):
            return line[1:]
    return ""


def check(ps_out: str, cwd: Callable[[int], str]) -> Optional[str]:
    rows = maintenance_processes(ps_out)
    if len(rows) <= MAX_CONCURRENT:
        return None
    by_repo: dict[str, int] = {}
    for row in rows:
        repo = row["repo"] or cwd(row["pid"]) or "?"
        by_repo[repo] = by_repo.get(repo, 0) + 1
    oldest = max(rows, key=lambda r: _etime(r["etime"]))
    return (f"{len(rows)} git maintenance/repack/gc processes (more than {MAX_CONCURRENT}): "
            + "; ".join(f"{repo} x{n}" for repo, n in sorted(by_repo.items(), key=lambda i: -i[1]))
            + f"; oldest pid {oldest['pid']} running {oldest['etime']}. Stop automatic "
            "maintenance on a shared store: git -C <repo> config gc.auto 0; "
            "git -C <repo> config maintenance.auto false; list the repo in "
            "~/.config/tartci/git-maintenance.repos so installs enforce it")


def _etime(value: str) -> int:
    days, _, clock = value.rpartition("-")
    try:
        nums = [int(p) for p in clock.split(":")]
    except ValueError:
        return 0
    while len(nums) < 3:
        nums.insert(0, 0)
    return int(days or 0) * 86400 + nums[0] * 3600 + nums[1] * 60 + nums[2]


# ── enforce ──────────────────────────────────────────────────────────────────

def enforce(paths: list[Path], run: Runner = _run) -> list[str]:
    """Set SETTINGS on each repo; one line per change or failure, none if already set."""
    lines = []
    for repo in paths:
        for key, value in SETTINGS:
            current = run(["git", "-C", str(repo), "config", "--get", key], timeout=30)
            if current.returncode == 0 and current.stdout.strip() == value:
                continue
            done = run(["git", "-C", str(repo), "config", key, value], timeout=30)
            lines.append(f"{repo}: {key}={value}" if done.returncode == 0 else
                         f"{repo}: could not set {key}: {(done.stderr or '').strip()[:200]}")
    return lines


# ── nightly ──────────────────────────────────────────────────────────────────

def lanes_busy(user_home: Path) -> bool:
    """True when any gate lane is mid-job or its state is unknown."""
    import lane_busy  # noqa: PLC0415 - sibling module; the pool's own mid-job probe
    agents = user_home / "Library" / "LaunchAgents"
    labels = sorted(p.stem for p in agents.glob(LANE_GLOB))
    if not labels:
        return False
    rows = lane_busy.probe(labels, agents_dir=agents)
    return any(row.state in (lane_busy.BUSY, lane_busy.UNKNOWN) for row in rows)


def host_loaded() -> Optional[str]:
    """Why the host is too loaded to start maintenance, or None."""
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        return None
    cores = os.cpu_count() or 1
    if load1 > LOAD_PER_CORE * cores:
        return f"load {load1:.0f} is above {LOAD_PER_CORE:g} x {cores} cores"
    return None


def nightly(now: float, user_home: Path, *, run: Runner = _run,
            busy: Callable[[Path], bool] = lanes_busy, dry_run: bool = False,
            ps_out: Optional[str] = None,
            loaded: Callable[[], Optional[str]] = host_loaded) -> list[str]:
    paths = repos(user_home)
    if not paths or not enabled(user_home):
        return []
    state_dir = Path(os.environ.get("TARTCI_HOME") or user_home / ".tartci") / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = state_dir / "git-maintenance.json"
    try:
        last = float(json.loads(state.read_text()).get("ran_at", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        last = 0.0
    if 0 <= now - last < NIGHTLY_INTERVAL_SECONDS:
        return []
    lock = (state_dir / "git-maintenance.lock").open("w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return [f"{_iso(now)} launchd-watchdog: git-maintenance: skipped, another run "
                    "holds the lock"]
        if busy(user_home):
            return [f"{_iso(now)} launchd-watchdog: git-maintenance: deferred, a gate lane "
                    "is mid-job"]
        # m3's storm came from agents' host-side git, not from gate lanes: never
        # add a run to maintenance already in progress, or to a loaded host.
        if ps_out is None:
            ps_out = run(["/bin/ps", "-axo", "pid=,etime=,command="], timeout=15).stdout
        running = maintenance_processes(ps_out)
        if running:
            return [f"{_iso(now)} launchd-watchdog: git-maintenance: deferred, {len(running)} "
                    f"git maintenance/repack/gc process(es) already running"]
        reason = loaded()
        if reason:
            return [f"{_iso(now)} launchd-watchdog: git-maintenance: deferred, {reason}"]
        lines = [f"{_iso(now)} launchd-watchdog: git-maintenance: {text}"
                 for text in enforce(paths, run)]
        for repo in paths:
            argv = ["nice", "-n", "19"]
            if os.path.exists(TASKPOLICY):
                argv += [TASKPOLICY, "-b"]
            argv += ["git", "-C", str(repo), *PRUNE_PINS, "maintenance", "run",
                     "--task=gc", "--task=loose-objects"]
            if dry_run:
                lines.append(f"{_iso(now)} launchd-watchdog: git-maintenance: would run "
                             + " ".join(argv))
                continue
            started = time.monotonic()
            try:
                done = run(argv, timeout=RUN_TIMEOUT_SECONDS)
                rc = done.returncode
            except subprocess.TimeoutExpired:
                rc = "timeout"
            lines.append(f"{_iso(now)} launchd-watchdog: {'' if rc == 0 else 'WARN '}"
                         f"git-maintenance: {repo} gc+loose-objects rc={rc} "
                         f"in {time.monotonic() - started:.0f}s")
        if not dry_run:
            state.write_text(json.dumps({"ran_at": now, "repos": [str(p) for p in paths]}) + "\n")
        return lines
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def watchdog_pass(now: float, *, user_home: Optional[Path] = None, run: Runner = _run,
                  ps: Optional[Callable[[], str]] = None,
                  cwd: Optional[Callable[[int], str]] = None,
                  busy: Callable[[Path], bool] = lanes_busy,
                  loaded: Callable[[], Optional[str]] = host_loaded) -> list[str]:
    user_home = user_home or Path(os.path.expanduser("~"))
    lines = []
    ps_out = ps() if ps else run(["/bin/ps", "-axo", "pid=,etime=,command="], timeout=15).stdout
    warning = check(ps_out, cwd or (lambda pid: cwd_of(pid, run)))
    if warning:
        lines.append(f"{_iso(now)} launchd-watchdog: WARN git-maintenance: {warning}")
    lines += nightly(now, user_home, run=run, busy=busy, ps_out=ps_out, loaded=loaded)
    return lines


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "enforce", "nightly"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    home = Path(os.path.expanduser("~"))
    if args.command == "check":
        line = check(_run(["/bin/ps", "-axo", "pid=,etime=,command="], timeout=15).stdout,
                     cwd_of)
        print(line or f"at most {MAX_CONCURRENT} git maintenance processes")
    elif args.command == "enforce":
        for line in enforce(repos(home)):
            print(line)
    else:
        for line in nightly(time.time(), home, dry_run=args.dry_run):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
