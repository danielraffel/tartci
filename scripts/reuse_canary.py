#!/usr/bin/env python3
"""Run Pulp's mac lane once per new main SHA a day, so reuse records keep existing.

Why this exists: Shipyard can bind a pull request's mac validation to an earlier
run of the same tree ("reuse") only when a bindable record for that tree exists
on a host in shadow_compare mode. Pull requests run only the fast tier, so in
practice no such record is ever written. This agent produces one: on each
shadow_compare host it runs shipyard run --targets mac at origin/main's head,
in a dedicated canary worktree, at background build priority, and records what
shipyard reuse records reports for that SHA.

Opt-in per host, OFF unless the installed fleet profile says:

    [reuse_canary]
    enabled = true
    repo = "/Volumes/<data-volume>/Code/pulp"                       # a Pulp checkout
    worktrees_root = "/Volumes/<data-volume>/Code/agent-worktrees"  # PULP_WORKTREES_ROOT

The LaunchAgent (com.danielraffel.tartci.reuse-canary, every 6 h, from
launchd/com.danielraffel.tartci.reuse-canary.plist.template) runs tartci
reuse-canary run, the installed generation's copy of this file, so self-update
refreshes it. The interval guard kicks it like any fleet timer when launchd
stops starting timers, so a stalled host still runs it within twice its
interval.

Each pass, in order:

  1. takes a nonblocking host lock; a pass already running makes this one a
     no-op (busy);
  2. refuses, touching nothing, when pool participation is off or draining
     (self-update drains the pool first, so an update in progress refuses too);
  3. reads origin/main's head with git ls-remote;
  4. skips when today's ledger holds a completed (ran) pass for that SHA, or
     MAX_STARTS_PER_DAY starts of it. A failed, timed-out or unrecorded pass
     may retry within that cap, and so may a half-run (a start with no finish,
     the pass was killed);
  5. refuses unless shipyard reuse records reports
     changed_surface_execution_mode = shadow_compare, read from the trusted
     machine config exactly as the ship path reads it; any failure refuses;
  6. checks the canary worktree out at that SHA under worktrees_root, never
     falling back to another disk when the root's volume is not mounted;
  7. runs shipyard run --targets mac there with PULP_BUILD_CLASS=background
     (the lease comes from governed-build.sh, as for any local mac run), in its
     own process group, bounded by RUN_TIMEOUT_S, writing a progress line every
     HEARTBEAT_S so the launchd watchdog sees a live log;
  8. records shipyard reuse records for the SHA verbatim. Only that output
     says whether the run is bindable; this file never infers it. The store
     files nothing for a run that died before writing job.json, so an empty
     list after a pass reads as not filed, and not bindable;
  9. writes an atomic receipt and one terminal event.

status --json summarises the newest receipts for tartci doctor fleet.

Exit codes: 0 ran or nothing due, 3 refused by a gate (host untouched), 4 the
run failed or timed out, 5 records unreadable or the profile incomplete,
6 the SHA or the worktree could not be prepared.
"""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - tartci hosts are POSIX.
    fcntl = None  # type: ignore[assignment]

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # Python < 3.11 (/usr/bin/python3 on macOS is 3.9)
    tomllib = None  # type: ignore[assignment]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tartci_launchd_watchdog as watchdog  # noqa: E402

LABEL = "com.danielraffel.tartci.reuse-canary"
TABLE = "reuse_canary"
SETTINGS_KEYS = {"enabled", "repo", "worktrees_root"}
REPO_SLUG = "Generous-Corp/pulp"
TARGET = "mac"
WORKTREE_NAME = "pulp-reuse-canary"
REQUIRED_MODE = "shadow_compare"
INTERVAL_S = 6 * 60 * 60
MAX_STARTS_PER_DAY = 2
RUN_TIMEOUT_S = 3 * 60 * 60
HEARTBEAT_S = 120
LEDGER_KEEP_DAYS = 7
# Doctor thresholds: a pass is due every INTERVAL_S and the interval guard
# kicks an overdue one at twice that, so no receipt for longer than 2x + 1 h
# means the agent is not running at all.
STALE_AFTER_S = 2 * INTERVAL_S + 60 * 60
NO_BINDABLE_AFTER_S = 36 * 60 * 60
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

EXIT_OK, EXIT_REFUSED, EXIT_RUN_FAILED, EXIT_RECORDS, EXIT_SETUP = 0, 3, 4, 5, 6



def records_command(sha: str) -> List[str]:
    return ["shipyard", "--json", "reuse", "records", "--target", TARGET,
            "--repo", REPO_SLUG, "--sha", sha]


# ── settings ───────────────────────────────────────────────────────────────

def validate_table(table: Any) -> List[str]:
    """Problems with a [reuse_canary] table; empty when it is acceptable.

    Shared by the install-time profile validator and the runtime reader, so a
    profile that installs is a profile this module acts on.
    """
    if not isinstance(table, dict):
        return ["reuse_canary must be a table"]
    problems = []
    unknown = set(table) - SETTINGS_KEYS
    if unknown:
        problems.append(f"unknown reuse_canary keys: {sorted(unknown)}")
    enabled = table.get("enabled", False)
    if type(enabled) is not bool:
        problems.append("reuse_canary.enabled must be a boolean")
    for key in ("repo", "worktrees_root"):
        value = table.get(key)
        if value is None:
            if enabled is True:
                problems.append(f"reuse_canary.{key} is required when enabled = true")
            continue
        if not isinstance(value, str) or not value.startswith("/") or ".." in value.split("/"):
            problems.append(f"reuse_canary.{key} must be an absolute path")
    return problems


def default_profile_path() -> Path:
    return Path(os.environ.get(
        "TARTCI_FLEET_PROFILE",
        str(Path.home() / ".config" / "tartci" / "macos-fleet-profile.toml"),
    )).expanduser()


def load_settings(profile: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """(settings, why). Settings None means unknown; enabled False means off."""
    if not profile.exists():
        return {"enabled": False}, f"no fleet profile at {profile}"
    if tomllib is None:
        return None, "no tomllib (needs Python 3.11+); cannot read the fleet profile"
    try:
        with profile.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError) as exc:
        return None, f"cannot read {profile}: {exc}"
    table = data.get(TABLE)
    if table is None:
        return {"enabled": False}, f"no [{TABLE}] table in the fleet profile"
    problems = validate_table(table)
    if problems:
        return None, "; ".join(problems)
    return dict(table, enabled=table.get("enabled", False)), f"[{TABLE}] in {profile}"


# ── effects ────────────────────────────────────────────────────────────────

class System:
    """Every external effect, so the whole pass runs against fakes in tests."""

    def __init__(self) -> None:
        self.out = sys.stdout

    def now(self) -> float:
        return time.time()

    def today(self) -> str:
        return dt.date.today().isoformat()

    def run(self, argv: Sequence[str], cwd: Optional[str] = None,
            timeout: float = 120) -> Tuple[int, str]:
        try:
            proc = subprocess.run(list(argv), cwd=cwd, text=True, capture_output=True,
                                  timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, f"{type(exc).__name__}: {exc}"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def run_bounded(self, argv: Sequence[str], cwd: str, env: Dict[str, str],
                    timeout: float, heartbeat: Callable[[float], None]) -> Tuple[Optional[int], bool]:
        """(rc, timed_out). Own process group; the whole group dies on timeout."""
        proc = subprocess.Popen(list(argv), cwd=cwd, env=env, start_new_session=True,
                                stdout=self.out, stderr=subprocess.STDOUT)
        started = time.monotonic()
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(HEARTBEAT_S):
                heartbeat(time.monotonic() - started)

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            rc = proc.wait(timeout=timeout)
            return rc, False
        except subprocess.TimeoutExpired:
            kill_group(proc.pid)
            proc.wait()
            return None, True
        finally:
            stop.set()

    def is_mount(self, path: Path) -> bool:
        return os.path.ismount(path)

    def emit(self, line: str) -> None:
        print(line, file=self.out, flush=True)


def kill_group(pid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        time.sleep(5)


def atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# ── the pass ───────────────────────────────────────────────────────────────

class Canary:
    def __init__(self, settings: Dict[str, Any], state_dir: Path, sys_: System,
                 participation_file: Path, host: str = "") -> None:
        self.settings = settings
        self.state_dir = state_dir
        self.sys = sys_
        self.participation_file = participation_file
        self.host = host or os.uname().nodename.split(".")[0]
        self.receipt: Dict[str, Any] = {}

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "ledger.json"

    def event(self, kind: str, detail: str, **fields: Any) -> None:
        record = {"ts": self.sys.now(), "event": kind, "detail": detail, "host": self.host, **fields}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "events.jsonl").open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        self.sys.emit(f"{kind} {detail}")

    def finish(self, outcome: str, code: int, detail: str, **fields: Any) -> int:
        """Write the receipt and exactly one terminal event."""
        now = self.sys.now()
        self.receipt.update(outcome=outcome, exit_code=code, detail=detail,
                            finished_at=now, host=self.host, **fields)
        self.receipt.setdefault("started_at", now)
        sha = self.receipt.get("sha") or "none"
        stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        name = f"{stamp}-{sha[:12]}-{outcome}.json"
        path = self.state_dir / "attempts" / name
        atomic_write(path, self.receipt)
        atomic_write(self.state_dir / "status.json", dict(self.receipt, receipt=str(path)))
        self.event(f"reuse_canary_{outcome}", detail,
                   sha=self.receipt.get("sha"), bindable=self.receipt.get("bindable"))
        return code

    # gates

    def gate(self) -> Optional[str]:
        if not watchdog.pool_participating(str(self.participation_file)):
            return "pool participation is off or draining"
        return None

    def mode_gate(self, sha: str) -> Optional[str]:
        """Refuse unless Shipyard reads its own mode as shadow_compare.

        The mode comes from shipyard reuse records, which reads the trusted
        machine config exactly as the ship path does; any failure refuses.
        """
        value, why = self.records(Path(self.settings["repo"]), sha)
        if value is None:
            return f"cannot read Shipyard's changed_surface_execution_mode: {why}"
        mode = value.get("changed_surface_execution_mode")
        if mode != REQUIRED_MODE:
            return f"Shipyard's changed_surface_execution_mode is {mode!r}, not {REQUIRED_MODE!r}"
        return None

    def main_sha(self) -> Optional[str]:
        rc, out = self.sys.run(["git", "-C", self.settings["repo"], "ls-remote", "origin",
                                "refs/heads/main"])
        if rc != 0:
            return None
        fields = out.split()
        sha = fields[0] if fields else ""
        return sha if SHA_RE.match(sha) else None

    # ledger

    def load_ledger(self) -> Dict[str, Any]:
        value = read_json(self.ledger_path)
        return value if isinstance(value, dict) else {}

    def due(self, ledger: Dict[str, Any], day: str, sha: str) -> Tuple[bool, str]:
        """A ran finish is final for the day; anything else may retry within the cap."""
        entry = (ledger.get(day) or {}).get(sha) or {}
        if "ran" in (entry.get("finishes") or []):
            return False, f"already ran {sha[:12]} today"
        starts = entry.get("starts") or []
        if len(starts) >= MAX_STARTS_PER_DAY:
            return False, f"{len(starts)} starts of {sha[:12]} today without a completed run"
        return True, ""

    def mark(self, ledger: Dict[str, Any], day: str, sha: str, outcome: str = "") -> None:
        """Record a start (no outcome) or a finish with its outcome."""
        entry = ledger.setdefault(day, {}).setdefault(sha, {"starts": []})
        if not outcome:
            entry["starts"].append(self.sys.now())
        else:
            entry.setdefault("finishes", []).append(outcome)
        for old in sorted(ledger)[:-LEDGER_KEEP_DAYS]:
            ledger.pop(old, None)
        atomic_write(self.ledger_path, ledger)

    # worktree

    def worktree(self, sha: str) -> Tuple[Optional[Path], str]:
        root = Path(self.settings["worktrees_root"])
        parts = root.parts
        if len(parts) > 2 and parts[1] == "Volumes" and not self.sys.is_mount(Path(*parts[:3])):
            return None, f"{Path(*parts[:3])} is not mounted; refusing any other disk"
        if not root.is_dir():
            return None, f"worktrees_root {root} does not exist"
        repo = self.settings["repo"]
        path = root / WORKTREE_NAME
        rc, out = self.sys.run(["git", "-C", repo, "fetch", "--quiet", "origin", "main"],
                               timeout=600)
        if rc != 0:
            return None, f"git fetch failed: {out.strip()[:300]}"
        if not path.exists():
            rc, out = self.sys.run(["git", "-C", repo, "worktree", "add", "--detach",
                                    str(path), sha], timeout=600)
            if rc != 0:
                return None, f"git worktree add failed: {out.strip()[:300]}"
        else:
            rc, mine = self.sys.run(["git", "-C", str(path), "rev-parse", "--git-common-dir"])
            rc2, theirs = self.sys.run(["git", "-C", repo, "rev-parse", "--git-common-dir"])
            if rc or rc2 or common_dir(path, mine) != common_dir(Path(repo), theirs):
                return None, f"{path} is not a worktree of {repo}"
            rc, out = self.sys.run(["git", "-C", str(path), "checkout", "--quiet", "--detach",
                                    sha], timeout=600)
            if rc != 0:
                return None, f"git checkout {sha[:12]} failed: {out.strip()[:300]}"
        lineage = path / "tools" / "scripts" / "worktree_lineage.sh"
        if lineage.exists():
            rc, out = self.sys.run(["bash", str(lineage), "mark", "--status", "active",
                                    "--owner", "tartci-reuse-canary",
                                    "--note", "reuse canary; detached at origin/main"],
                                   cwd=str(path))
            if rc != 0:
                self.receipt["lineage_warning"] = out.strip()[:300]
        return path, ""

    # run

    def run_shipyard(self, path: Path, sha: str) -> Tuple[Optional[int], bool]:
        env = dict(os.environ, PULP_BUILD_CLASS="background",
                   PULP_WORKTREES_ROOT=self.settings["worktrees_root"])

        def heartbeat(elapsed: float) -> None:
            self.sys.emit(f"reuse_canary progress sha={sha[:12]} elapsed={int(elapsed)}s")

        return self.sys.run_bounded(["shipyard", "run", "--targets", TARGET], str(path), env,
                                    RUN_TIMEOUT_S, heartbeat)

    def records(self, path: Path, sha: str) -> Tuple[Optional[Dict[str, Any]], str]:
        rc, out = self.sys.run(records_command(sha), cwd=str(path))
        if rc != 0:
            return None, f"shipyard reuse records exited {rc}: {out.strip()[:300]}"
        try:
            value = json.loads(out)
        except ValueError:
            return None, "shipyard reuse records printed no JSON"
        if not isinstance(value, dict) or not isinstance(value.get("records"), list) \
                or not isinstance(value.get("changed_surface_execution_mode"), str):
            return None, "shipyard reuse records JSON lacks records or the mode"
        return value, ""

    def run(self) -> int:
        self.receipt = {"started_at": self.sys.now()}
        refusal = self.gate()
        if refusal:
            return self.finish("refused", EXIT_REFUSED, refusal)
        sha = self.main_sha()
        if sha is None:
            return self.finish("failed", EXIT_SETUP, "cannot read origin/main's head")
        self.receipt["sha"] = sha
        day = self.sys.today()
        ledger = self.load_ledger()
        due, why = self.due(ledger, day, sha)
        if not due:
            return self.finish("not_due", EXIT_OK, why)
        refusal = self.mode_gate(sha)
        if refusal:
            return self.finish("refused", EXIT_REFUSED, refusal)
        path, why = self.worktree(sha)
        if path is None:
            return self.finish("failed", EXIT_SETUP, why)
        self.mark(ledger, day, sha)
        self.event("reuse_canary_started", f"sha={sha[:12]}", sha=sha)
        rc, timed_out = self.run_shipyard(path, sha)
        self.receipt["shipyard_rc"] = rc
        # Recorded either way: the records say what Shipyard kept, and a failed
        # or cut run must read as not bindable rather than as unknown.
        records, why = self.records(path, sha)
        self.receipt["reuse_records"] = records
        if records is not None:
            mine = [r for r in records["records"] if isinstance(r, dict)
                    and r.get("sha") == sha and r.get("target") == TARGET]
            # The store files nothing for a run that died before job.json, so
            # an empty list after a pass means "not filed", never "unknown".
            self.receipt["filed"] = bool(mine)
            self.receipt["bindable"] = any(r.get("bindable") is True for r in mine)
        if timed_out:
            outcome, code, detail = "timeout", EXIT_RUN_FAILED, f"shipyard run exceeded {RUN_TIMEOUT_S}s"
        elif records is None:
            outcome, code, detail = "records_error", EXIT_RECORDS, why
        elif rc != 0:
            outcome, code, detail = "failed", EXIT_RUN_FAILED, f"shipyard run exited {rc}"
        else:
            outcome, code = "ran", EXIT_OK
            detail = (f"sha={sha[:12]} bindable={self.receipt['bindable']} "
                      f"filed={self.receipt['filed']}")
        self.mark(ledger, day, sha, outcome)
        return self.finish(outcome, code, detail)




def common_dir(base: Path, text: str) -> str:
    value = Path(text.strip())
    return str((value if value.is_absolute() else base / value).resolve())


def acquire_lock(path: Path) -> Optional[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    if fcntl is None:
        return handle
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return None
        raise
    return handle


# ── status for the doctor ──────────────────────────────────────────────────

def status(state_dir: Path, settings: Optional[Dict[str, Any]], *, plist: Path,
           now: Optional[float] = None) -> Dict[str, Any]:
    """The facts tartci doctor fleet judges, from the receipts alone."""
    now = time.time() if now is None else now
    if settings is None:
        return {"state": "unreadable", "error": "fleet profile unreadable"}
    if not settings.get("enabled"):
        return {"state": "off", "installed": plist.exists()}
    if not plist.exists():
        return {"state": "not_installed"}
    attempts = sorted((state_dir / "attempts").glob("*.json")) \
        if (state_dir / "attempts").is_dir() else []
    receipts = [r for r in (read_json(p) for p in attempts) if isinstance(r, dict)]
    if attempts and not receipts:
        return {"state": "unreadable", "error": "no receipt parses"}
    if not receipts:
        return {"state": "never", "installed_at": plist.stat().st_mtime, "now": now}
    newest = max(receipts, key=lambda r: r.get("finished_at") or 0)
    first = min(r.get("finished_at") or now for r in receipts)
    bindable = [r.get("finished_at") or 0 for r in receipts if r.get("bindable") is True]
    facts = {
        "newest_receipt_at": newest.get("finished_at"),
        "newest_outcome": newest.get("outcome"),
        "newest_sha": newest.get("sha"),
        "first_receipt_at": first,
        "newest_bindable_at": max(bindable) if bindable else None,
        "now": now,
    }
    if now - float(newest.get("finished_at") or 0) > STALE_AFTER_S:
        return dict(facts, state="stale")
    anchor = facts["newest_bindable_at"] or first
    if now - float(anchor) > NO_BINDABLE_AFTER_S:
        return dict(facts, state="no_bindable")
    return dict(facts, state="ok")


# ── entry point ────────────────────────────────────────────────────────────

def default_state_dir() -> Path:
    return Path(os.environ.get("TARTCI_REUSE_CANARY_DIR",
                               str(Path.home() / ".tartci" / "state" / "reuse-canary")))


def default_plist() -> Path:
    agents = os.environ.get("TARTCI_AGENTS_DIR", str(Path.home() / "Library" / "LaunchAgents"))
    return Path(agents) / f"{LABEL}.plist"


def main(argv: Optional[Sequence[str]] = None, sys_: Optional[System] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["run", "status"])
    parser.add_argument("--profile-file", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--participation-file", type=Path,
                        default=Path.home() / ".config" / "tartci" / "native-build-participation")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    state_dir = args.state_dir or default_state_dir()
    settings, why = load_settings(args.profile_file or default_profile_path())
    if args.command == "status":
        value = status(state_dir, settings, plist=default_plist())
        print(json.dumps(value, sort_keys=True) if args.json else value.get("state"))
        return 0
    sys_ = sys_ or System()
    if settings is None:
        sys_.emit(f"reuse_canary profile unreadable: {why}")
        return EXIT_RECORDS
    if not settings.get("enabled"):
        sys_.emit(f"reuse_canary off: {why}")
        return EXIT_OK
    lock = acquire_lock(state_dir / "lock")
    if lock is None:
        sys_.emit("reuse_canary busy: another pass holds the lock")
        return EXIT_OK
    try:
        return Canary(settings, state_dir, sys_, args.participation_file).run()
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
