#!/usr/bin/env python3
"""Keep a fleet host on the tartci code that main has, verified.

`tartci fleet-macos self-update [--target REF] [--plan] [--apply]` codifies the
manual procedure that brought m1, m5 and m3 up to date on 2026-09-23. It adds
no new mechanics; every step is a command that already exists:

  1. Measure skew. The installed commit is the EXECUTED cohort: the sealed
     launcher bundle's source_commit on a host whose profile has
     [launch_helper], otherwise the installed generation's manifest commit.
     Main is read from a tartci-owned checkout (never a shared, possibly
     dirty working copy). The target is the newest FIRST-PARENT main commit
     older than the soak; a PR-internal commit is never a target.
  2. Prepare from that checkout, read-only for the host: support-manifest
     write, fleet-macos validate, install dry-run, and on a sealed host the
     launcher build (signing identity EXTRACTED from the live bundle's leaf
     certificate) plus its verification. Nothing on the host changes yet.
  3. One host at a time: announce, then read every other published host's
     pool state and self-update marker over SSH; any peer not `on`, or
     updating, refuses. Two hosts that announce together both see each other
     and the lower host id proceeds.
  4. Capacity floor: drain passes --allow-last-serving-host ONLY when every
     last-serving label is on the explicit idle-by-design list, and the rule
     is written to the receipt. Capacity unknown refuses.
  5. drain, wait (bounded) for `pool off --plan` to show no mid-job lane,
     pool off, pin the new launcher approval (backed up), install --apply
     (retried while agents unload), relay reconcile, `pool on` through the
     INSTALLED shim, then verify.
  6. Any failure after the drain restores the approval pin and runs `pool
     on` through the installed shim, so the host is left serving on the
     previous generation, and records the failure loudly.

Every external effect goes through `System`, so the whole procedure runs
against fakes in the tests. The plan mode performs steps 1-4 read-only.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the launchd python is 3.9
    tomllib = None  # type: ignore[assignment]

SCHEMA = "tartci.self-update/v1"
REPO_URL = "https://github.com/danielraffel/tartci.git"
SHA = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_SOAK_SECONDS = 1800
DEFAULT_RATE_HOURS = 6
DEFAULT_STALE_HOURS = 24
DEFAULT_WAIT_SECONDS = 90 * 60
DEFAULT_POLL_SECONDS = 45
INSTALL_ATTEMPTS = 4
INSTALL_RETRY_SECONDS = 30
# Readiness problems that only mean a supervisor loaded by `pool on` has not yet
# written its first heartbeat; verification re-reads them for a bounded window.
SETTLING_PROBLEM_CODES = frozenset({"heartbeat_missing", "supervisor_not_running",
                                    "supervisor_pid_missing"})
VERIFY_SETTLE_SECONDS = 180
VERIFY_SETTLE_POLL_SECONDS = 10
ACTIVE_MARKER_TTL = 3 * 3600
MAX_CONSECUTIVE_FAILURES = 3
REFUSAL_RECEIPT_TTL = 7 * 86400
CHECK_CANDIDATES = 5
SIGNING_PROBE_TIMEOUT = 60
TARTCI_REPO = "danielraffel/tartci"
# A peer with no `ssh` in its profile is reached through this alias.
SSH_ALIAS_CONVENTION = "tartci-{host_id}"
TERMINAL_STATUSES = ("succeeded", "failed", "rolled_back")
KEEP_BUILDS = 3
KEEP_SNAPSHOTS = 5
INSTALL_TIMEOUT = 1800
INSTALL_TERM_GRACE = 120  # the installer's restore trap after a TIMEOUT TERM
# launchd SIGKILLs the agent ExitTimeOut (120 s) after SIGTERM. A deferred
# SIGTERM therefore waits for the installer at most this long, then TERMs the
# installer group and leaves TERM_WAIT for its trap, which keeps recovery (re-pin,
# pool on, receipt) inside the window. Anything still running then is recorded
# in installer.json and refused by the next run until it has exited.
SIGTERM_DEFER_CAP = 80
SIGTERM_TERM_WAIT = 10
EXIT_TIMED_OUT = 124
# Lanes whose only work is occasional by design: a host that is their last
# server may still be taken down for an update, because an idle release lane
# queues nothing while it is gone. Anything else that is last-serving refuses.
DEFAULT_IDLE_BY_DESIGN = ("pulp-release-tagged", "pulp-release-pr-gate")

# Exit codes.
EXIT_OK = 0            # updated, already current, or plan says it would proceed
EXIT_NOTHING = 0
EXIT_REFUSED = 3       # a precondition refused; the host was not touched
EXIT_FAILED = 4        # a mutation ran and failed; see the receipt for what was restored
EXIT_UNKNOWN = 5       # skew or installed state could not be determined


class Refused(Exception):
    """A precondition failed before anything on the host changed."""


class Failed(Exception):
    """A step failed after the host was taken out of service."""


class NotManaged(Refused):
    """This host has no installed fleet generation, so it has no skew to measure."""


class Terminated(BaseException):
    """SIGTERM (launchd stopping the agent) while the host is out of service."""


def _raise_terminated(signum, frame):  # noqa: ARG001
    raise Terminated(f"signal {signum}")


@dataclasses.dataclass
class Result:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def text(self) -> str:
        return (self.err or self.out).strip()


class System:
    """Every side effect. Tests replace it."""

    def run(self, argv: list[str], *, cwd: str | None = None,
            env: dict[str, str] | None = None, timeout: float = 900) -> Result:
        merged = {**os.environ, **(env or {})}
        if argv and argv[0] == "python3":
            # Helpers need tomllib (3.11+). This process was started by
            # tartci_toml_python, so its interpreter qualifies; a bare python3
            # on PATH is /usr/bin/python3 (3.9) under ssh and launchd.
            argv = [sys.executable, *argv[1:]]
        try:
            proc = subprocess.run(argv, cwd=cwd, env=merged, capture_output=True,
                                  text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Result(127, "", f"{type(exc).__name__}: {exc}")
        return Result(proc.returncode, proc.stdout, proc.stderr)

    def process_start(self, pid: int) -> str | None:
        """The process's start time (identity across pid reuse), or None if gone."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        result = self.run(["ps", "-o", "lstart=", "-p", str(pid)], timeout=10)
        return result.out.strip() or None if result.rc == 0 else None

    def group_alive(self, pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def run_critical(self, argv: list[str], *, cwd: str | None = None,
                     env: dict[str, str] | None = None,
                     timeout: float = INSTALL_TIMEOUT,
                     record: Path | None = None) -> Result:
        """Run a command that must not be killed half way (the installer).

        It gets its own process group, so a signal to this process does not
        reach it. Its pgid and start time go to `record` while it runs, so a
        later run can see an installer that outlived us. A SIGTERM to this
        process is deferred for at most SIGTERM_DEFER_CAP seconds (below
        launchd's ExitTimeOut); then the group is sent TERM, given
        SIGTERM_TERM_WAIT for its restore trap, and Terminated is raised so
        recovery still runs before launchd's SIGKILL. A plain timeout sends
        TERM and waits INSTALL_TERM_GRACE before KILL.
        """
        import signal
        pending: list[float] = []
        previous = signal.signal(signal.SIGTERM, lambda signum, frame: pending.append(time.monotonic()))
        still_running = False
        try:
            with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
                try:
                    proc = subprocess.Popen(argv, cwd=cwd, env={**os.environ, **(env or {})},
                                            stdout=out, stderr=err, text=True,
                                            start_new_session=True)
                except OSError as exc:
                    return Result(127, "", f"{type(exc).__name__}: {exc}")
                if record is not None:
                    _write_json(record, {"pgid": proc.pid, "start": self.process_start(proc.pid),
                                         "argv": list(argv), "owner_pid": os.getpid()})
                deadline = time.monotonic() + timeout
                timed_out = False
                while True:
                    try:
                        rc = proc.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    now = time.monotonic()
                    if pending and now - pending[0] >= SIGTERM_DEFER_CAP:
                        _signal_group(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=SIGTERM_TERM_WAIT)
                        except subprocess.TimeoutExpired:
                            still_running = True
                        raise Terminated(f"SIGTERM; the installer did not finish within "
                                         f"{SIGTERM_DEFER_CAP}s and was sent TERM"
                                         + (f"; it is STILL RUNNING as pgid {proc.pid}"
                                            if still_running else ""))
                    if now >= deadline:
                        timed_out = True
                        _signal_group(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=INSTALL_TERM_GRACE)
                        except subprocess.TimeoutExpired:
                            _signal_group(proc.pid, signal.SIGKILL)
                            proc.wait()
                        rc = EXIT_TIMED_OUT
                        break
                out.seek(0)
                err.seek(0)
                result = Result(rc, out.read(), err.read())
                if timed_out:
                    result.err = (f"timed out after {timeout}s; the installer was sent TERM and "
                                  f"its restore trap given {INSTALL_TERM_GRACE}s\n" + result.err)
                return result
        finally:
            signal.signal(signal.SIGTERM, previous)
            if record is not None and not still_running:
                record.unlink(missing_ok=True)
            if pending and not still_running:
                # Deferred SIGTERM that arrived while the installer finished
                # within the cap: raise it now that the installer is done.
                import sys as _sys
                if _sys.exc_info()[0] is None:
                    raise Terminated("SIGTERM (deferred until the installer exited)")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.time()


@dataclasses.dataclass
class Config:
    home: Path
    soak_seconds: int = DEFAULT_SOAK_SECONDS
    rate_hours: float = DEFAULT_RATE_HOURS
    stale_hours: float = DEFAULT_STALE_HOURS
    wait_seconds: int = DEFAULT_WAIT_SECONDS
    poll_seconds: int = DEFAULT_POLL_SECONDS
    idle_by_design: tuple[str, ...] = DEFAULT_IDLE_BY_DESIGN
    peers: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def checkout(self) -> Path:
        return self.home / ".local" / "share" / "tartci" / "update-checkout"

    @property
    def state_dir(self) -> Path:
        return state_dir_for(self.home)

    @property
    def shim(self) -> Path:
        return self.home / ".local" / "bin" / "tartci"

    @property
    def installed_profile(self) -> Path:
        return self.home / ".config" / "tartci" / "macos-fleet-profile.toml"

    @property
    def install_receipt(self) -> Path:
        return self.home / ".config" / "tartci" / "macos-fleet-install.json"

    @property
    def network_profile(self) -> Path:
        return self.home / ".config" / "tartci" / "network-profile.toml"

    @property
    def settings(self) -> Path:
        return self.home / ".config" / "tartci" / "self-update.toml"


def state_dir_for(home: Path) -> Path:
    root = os.environ.get("TARTCI_HOME") or str(home / ".tartci")
    return Path(root) / "state" / "self-update"


def summary(home: Path | None = None) -> dict:
    """Cached skew + last attempt for status surfaces. Never fetches or raises."""
    state = state_dir_for(home or Path.home())
    skew = _read_json(state / "skew.json")
    last = _read_json(state / "last.json")
    problem = None
    if skew is None:
        problem = None  # never measured: reported, but not a warning by itself
    elif skew.get("state") in ("unknown", "diverged"):
        problem = f"skew {skew.get('state')}: {skew.get('reason')}"
    elif skew.get("stale"):
        problem = f"{skew.get('behind')} commits behind main since {skew.get('oldest_undeployed')}"
    if last and last.get("host_off"):
        off = "self-update LEFT THIS HOST OFF"
        problem = f"{problem}; {off}" if problem else off
    if last and last.get("status") in ("failed", "rolled_back"):
        failed = (f"last self-update {last['status'].upper().replace('_', ' ')} for "
                  f"{str(last.get('target'))[:12]}: {last.get('error')}")
        problem = f"{problem}; {failed}" if problem else failed
    halted = halt_reason(state)
    if halted:
        problem = f"{problem}; {halted}" if problem else halted
    return {"skew": skew, "last": last, "lines": status_lines(state), "problem": problem}


def load_config(home: Path, settings: Path | None = None) -> Config:
    """Defaults, overridden by ~/.config/tartci/self-update.toml when present.

    [peers] maps each other published host_id to an SSH target; one-at-a-time
    needs to read every peer, so an unmapped peer refuses rather than guessing.
    """
    cfg = Config(home=home)
    path = settings or cfg.settings
    if path.is_file() and tomllib is not None:
        data = tomllib.loads(path.read_text())
        cfg.peers = {str(k): str(v) for k, v in (data.get("peers") or {}).items()}
        for key in ("soak_seconds", "wait_seconds", "poll_seconds"):
            if isinstance(data.get(key), int):
                setattr(cfg, key, data[key])
        for key in ("rate_hours", "stale_hours"):
            if isinstance(data.get(key), (int, float)):
                setattr(cfg, key, float(data[key]))
        if isinstance(data.get("idle_by_design_labels"), list):
            cfg.idle_by_design = tuple(str(v) for v in data["idle_by_design_labels"])
    return cfg


def _toml(path: Path) -> dict:
    if tomllib is None:
        raise Refused("Python 3.11+ with tomllib is required")
    return tomllib.loads(path.read_text())


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


# ── installed state and skew ───────────────────────────────────────────────

def launch_helper(cfg: Config) -> dict | None:
    try:
        helper = _toml(cfg.installed_profile).get("launch_helper")
    except (OSError, ValueError) as exc:
        raise Refused(f"installed profile unreadable: {exc}") from exc
    return helper if isinstance(helper, dict) else None


def installed_commit(cfg: Config) -> tuple[str, str]:
    """(commit, source) of the cohort this host EXECUTES. Raises Refused.

    NotManaged when there is no installed fleet profile, or (unsealed) no
    install receipt: such a host has nothing self-update could measure, which
    is not the same as a managed host whose installed commit is unreadable.
    """
    if not cfg.installed_profile.is_file():
        raise NotManaged(f"no installed fleet profile at {cfg.installed_profile}")
    helper = launch_helper(cfg)
    if helper is None and not cfg.install_receipt.is_file():
        raise NotManaged(f"no install receipt at {cfg.install_receipt}")
    if helper is not None:
        path = Path(helper["path"]) / "Contents" / "Resources" / "bundle.json"
        value = _read_json(path)
        commit = (value or {}).get("source_commit")
        source = f"sealed launcher {path}"
    else:
        value = _read_json(cfg.install_receipt)
        support = (value or {}).get("support")
        commit = support.get("source_commit") if isinstance(support, dict) else None
        source = f"installed generation {cfg.install_receipt}"
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise Refused(f"installed commit unknown (no source_commit in {source})")
    return commit, source


def refresh_checkout(cfg: Config, sys_: System) -> None:
    """Fetch main into the tartci-owned checkout, creating it atomically.

    The clone lands in a temporary sibling and is renamed into place only
    when complete, so a timeout or kill can never leave a half checkout that
    later runs look at.
    """
    path = cfg.checkout
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".update-checkout.", dir=path.parent))
        result = sys_.run(["git", "clone", "--quiet", "--no-checkout", REPO_URL,
                           str(staging / "clone")])
        if result.rc != 0 or not (staging / "clone" / ".git").exists():
            shutil.rmtree(staging, ignore_errors=True)
            raise Refused(f"cannot create the update checkout: {result.text}")
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)  # a leftover without .git
        os.rename(staging / "clone", path)
        shutil.rmtree(staging, ignore_errors=True)
    origin = sys_.run(["git", "-C", str(path), "remote", "get-url", "origin"])
    if origin.rc != 0 or origin.out.strip().rstrip("/").removesuffix(".git").lower() \
            != REPO_URL.removesuffix(".git").lower():
        raise Refused(f"update checkout {path} is not a danielraffel/tartci clone")
    fetched = sys_.run(["git", "-C", str(path), "fetch", "--quiet", "--prune", "origin", "main"])
    if fetched.rc != 0:
        raise Refused(f"git fetch failed: {fetched.text}")


def gh_cli() -> str:
    return os.environ.get("TARTCI_GH_CLI") or ("ghapp" if shutil.which("ghapp") else "gh")


def checks_green(cfg: Config, sys_: System, sha: str) -> tuple[bool, str]:
    """Every check run on main's commit completed successfully (and there is one).

    Age alone is not evidence: a commit that broke main's CI soaks like any
    other. An unreadable answer is not green.
    """
    binding = {"GH_REPO": TARTCI_REPO, "SHIPYARD_GHAPP_REPO": TARTCI_REPO,
               "SHIPYARD_GH_APP_REPO": TARTCI_REPO}
    result = sys_.run([gh_cli(), "api", f"repos/{TARTCI_REPO}/commits/{sha}/check-runs?per_page=100"],
                      cwd=str(cfg.checkout), env=binding, timeout=60)
    try:
        runs = json.loads(result.out).get("check_runs")
    except (json.JSONDecodeError, AttributeError):
        runs = None
    if not isinstance(runs, list):
        return False, f"check runs unreadable (exit {result.rc}): {result.text[:160]}"
    if not runs:
        return False, "no check runs recorded"
    bad = [f"{run.get('name')}={run.get('conclusion') or run.get('status')}" for run in runs
           if run.get("status") != "completed"
           or run.get("conclusion") not in ("success", "skipped", "neutral")]
    return (not bad), ("all checks green" if not bad else "not green: " + ", ".join(bad))


def measure_skew(cfg: Config, sys_: System, installed: str, now: float,
                 target_ref: str = "origin/main", verify_checks: bool = True) -> dict:
    """First-parent commits on main the host does not run, and the target.

    The target is the newest first-parent main commit that is past the soak
    AND whose check runs are green. An explicit --target must itself be on
    main's first-parent chain and meet both.
    """
    git = ["git", "-C", str(cfg.checkout)]
    skew: dict[str, Any] = {"installed": installed, "measured_at": _iso(now),
                            "state": "unknown", "behind": None, "oldest_undeployed": None,
                            "target": None, "soak_seconds": cfg.soak_seconds}
    main = sys_.run([*git, "rev-parse", "--verify", "origin/main^{commit}"])
    head = sys_.run([*git, "rev-parse", "--verify", f"{target_ref}^{{commit}}"])
    if head.rc != 0 or not SHA.fullmatch(head.out.strip()) or main.rc != 0:
        skew["reason"] = f"cannot resolve {target_ref}: {head.text}"
        return skew
    skew["main"] = main.out.strip()
    head_sha = head.out.strip()
    explicit = head_sha != skew["main"]
    if explicit:
        chain = sys_.run([*git, "rev-list", "--first-parent", skew["main"]])
        if head_sha not in chain.out.split():
            skew.update(state="unknown",
                        reason=f"--target {target_ref} is not on origin/main's first-parent chain")
            return skew
    if sys_.run([*git, "cat-file", "-e", f"{installed}^{{commit}}"]).rc != 0:
        skew["reason"] = f"installed commit {installed[:12]} is not in the tartci history"
        return skew
    if sys_.run([*git, "merge-base", "--is-ancestor", installed, skew["main"]]).rc != 0:
        skew.update(state="diverged",
                    reason=f"installed {installed[:12]} is not an ancestor of origin/main")
        return skew
    log = sys_.run([*git, "log", "--first-parent", "--format=%H %ct",
                    f"{installed}..{skew['main']}"])
    if log.rc != 0:
        skew["reason"] = f"git log failed: {log.text}"
        return skew
    commits = []
    for line in log.out.splitlines():
        parts = line.split()
        if len(parts) == 2 and SHA.fullmatch(parts[0]) and parts[1].isdigit():
            commits.append((parts[0], int(parts[1])))
    skew["behind"] = len(commits)
    if not commits:
        skew["state"] = "current"
        return skew
    skew["oldest_undeployed"] = _iso(min(ts for _, ts in commits))
    skew["stale"] = min(ts for _, ts in commits) <= now - cfg.stale_hours * 3600
    if explicit:
        commits = [(sha, ts) for sha, ts in commits if sha == head_sha]
        if not commits:
            skew.update(state="unknown",
                        reason=f"--target {head_sha[:12]} is not newer than the installed commit")
            return skew
    soaked = [(sha, ts) for sha, ts in commits if ts <= now - cfg.soak_seconds]
    if not soaked:
        skew["state"] = "soaking"
        return skew
    if not verify_checks:
        skew.update(state="behind", target=None)
        return skew
    rejected = []
    for sha, _ in soaked[:CHECK_CANDIDATES]:
        green, detail = checks_green(cfg, sys_, sha)
        if green:
            skew.update(state="behind", target=sha)
            if rejected:
                skew["skipped"] = rejected
            return skew
        rejected.append(f"{sha[:12]}: {detail}")
    skew.update(state="unverified", reason="; ".join(rejected))
    return skew


def render_skew(skew: dict | None) -> str:
    if not skew:
        return "tartci: skew UNKNOWN (never measured; run tartci fleet-macos self-update --plan)"
    state = skew.get("state")
    if state == "current":
        return f"tartci: current with main (measured {skew.get('measured_at')})"
    if state == "not_applicable":
        return f"tartci: skew n/a ({skew.get('reason')})"
    if state in ("behind", "soaking", "unverified"):
        flag = " STALE" if skew.get("stale") else ""
        return (f"tartci: {skew['behind']} commits behind main (oldest undeployed: "
                f"{skew['oldest_undeployed']}){flag}"
                + {"behind": "", "soaking": " [all still soaking]",
                   "unverified": f" [no soaked commit has green checks: {skew.get('reason')}]"}[state]
                + f" (measured {skew.get('measured_at')})")
    return f"tartci: skew {str(state).upper()} ({skew.get('reason') or 'no reason'})"


# ── one host at a time ─────────────────────────────────────────────────────

def published_peers(cfg: Config, sys_: System) -> dict[str, str]:
    """host_id -> SSH target for every host in the CURRENT published supply.

    Read from main's fleet/advertised-labels.json (the target can predate a
    host being added). A profile's `host.ssh` is published there; without it
    the alias convention `tartci-<host_id>` applies; [peers] only overrides.
    Adding a machine therefore needs its profile and nothing on other hosts.
    """
    shown = sys_.run(["git", "-C", str(cfg.checkout), "show",
                      "origin/main:fleet/advertised-labels.json"])
    try:
        value = json.loads(shown.out) if shown.rc == 0 else None
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, dict):
        raise Refused("published supply origin/main:fleet/advertised-labels.json is unreadable")
    ssh = {row["host_id"]: row.get("ssh") for row in value.get("hosts", [])
           if isinstance(row, dict) and isinstance(row.get("host_id"), str)}
    ids = {row["host_id"] for row in value.get("registrations", [])
           if isinstance(row, dict) and isinstance(row.get("host_id"), str)} | set(ssh)
    return {host_id: cfg.peers.get(host_id) or ssh.get(host_id)
            or SSH_ALIAS_CONVENTION.format(host_id=host_id) for host_id in sorted(ids)}


def published_hosts(cfg: Config, sys_: System) -> list[str]:
    return list(published_peers(cfg, sys_))


def self_host_id(cfg: Config) -> str:
    host = _toml(cfg.installed_profile).get("host") or {}
    if not isinstance(host.get("id"), str):
        raise Refused("installed profile has no host.id")
    return host["id"]


# Printed by the peer: its own clock, then its marker. Ages are computed on
# the peer's clock so host clock skew cannot make a live marker look stale.
_PEER_MARKER = ('date +%s; cat "${TARTCI_HOME:-$HOME/.tartci}/state/self-update/active.json" '
                '2>/dev/null || true')


def peer_state(cfg: Config, sys_: System, host_id: str, target: str) -> tuple[bool, str]:
    """(busy, evidence). Unreachable or unreadable peers are busy: fail closed."""
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target]
    status = sys_.run([*ssh, "cd ~ && ~/.local/bin/tartci pool status --json"], timeout=60)
    try:
        value = json.loads(status.out)
    except json.JSONDecodeError:
        return True, (f"peer {host_id} ({target}) pool status unreadable (exit {status.rc}): "
                      f"{status.text[:160]}")
    if value.get("state") != "on" or value.get("participating") is not True:
        return True, f"peer {host_id} is {value.get('state')} (participating={value.get('participating')})"
    marker = sys_.run([*ssh, _PEER_MARKER], timeout=60)
    first, _, rest = marker.out.partition("\n")
    if marker.rc != 0 or not first.strip().isdigit():
        return True, f"peer {host_id} self-update marker unreadable (exit {marker.rc})"
    active = None
    try:
        active = json.loads(rest) if rest.strip() else None
    except json.JSONDecodeError:
        return True, f"peer {host_id} self-update marker unreadable"
    if isinstance(active, dict) and int(first) - float(active.get("ts", 0)) < ACTIVE_MARKER_TTL:
        return True, f"peer {host_id} is self-updating to {str(active.get('target'))[:12]}"
    return False, f"peer {host_id} on, not updating"


def check_peers(cfg: Config, sys_: System, me: str) -> list[str]:
    busy = []
    for peer, target in published_peers(cfg, sys_).items():
        if peer == me:
            continue
        is_busy, evidence = peer_state(cfg, sys_, peer, target)
        if is_busy:
            busy.append(evidence)
    return busy


def stagger_seconds(host_id: str, window: int = 600) -> int:
    """A stable per-host offset so scheduled runs on different hosts spread out."""
    return int(hashlib.sha256(host_id.encode()).hexdigest(), 16) % window


# ── capacity floor ─────────────────────────────────────────────────────────

def census_env() -> dict[str, str]:
    # The census binds its identity per call (#227); this only pins the CLI.
    return {"TARTCI_GH_CLI": os.environ.get("TARTCI_GH_CLI") or "ghapp"}


def floor_decision(cfg: Config, sys_: System) -> tuple[bool, str]:
    """(allow_last_serving_host, rule). Refuses unless the floor allows it."""
    result = sys_.run(["python3", "scripts/capacity_floor.py", "check", "--action", "drain",
                       "--json"], cwd=str(cfg.checkout), env=census_env(), timeout=180)
    try:
        decision = json.loads(result.out)
    except json.JSONDecodeError as exc:
        raise Refused(f"capacity floor gave no decision (exit {result.rc}): {result.text}") from exc
    if decision.get("allowed"):
        return False, "capacity floor: every required label is served elsewhere"
    if decision.get("reason") != "last_serving_host":
        raise Refused(f"capacity floor refused ({decision.get('reason')}): "
                      f"{decision.get('message')}")
    last = [f for f in decision.get("findings", []) if f.get("verdict") == "last_serving_host"]
    blocked = [f["label"] for f in last if f.get("label") not in cfg.idle_by_design]
    if blocked:
        raise Refused(f"this host is the last server of {', '.join(blocked)}, which is not "
                      "idle by design; refusing to take it down for an update")
    labels = ", ".join(sorted({f["label"] for f in last}))
    return True, (f"--allow-last-serving-host: last server only of idle-by-design "
                  f"label(s) {labels} (rule: idle_by_design_labels)")


# ── sealed launcher ────────────────────────────────────────────────────────

def extract_signing_identity(sys_: System, bundle: Path, workdir: Path) -> str:
    """SHA-1 of the leaf certificate that signs the LIVE bundle."""
    prefix = workdir / "livecert"
    result = sys_.run(["codesign", "-d", f"--extract-certificates={prefix}", str(bundle)])
    if result.rc != 0:
        raise Refused(f"cannot extract the live launcher certificate: {result.text}")
    fp = sys_.run(["openssl", "x509", "-inform", "DER", "-in", f"{prefix}0", "-noout",
                   "-fingerprint", "-sha1"])
    match = re.search(r"Fingerprint=([0-9A-Fa-f:]{59})", fp.out)
    if fp.rc != 0 or match is None:
        raise Refused(f"cannot fingerprint the live launcher certificate: {fp.text}")
    return match.group(1).replace(":", "").upper()


def signing_probe(sys_: System, identity: str, workdir: Path) -> None:
    """Prove the identity signs unattended, with a timestamp, in bounded time.

    The equivalent of pulp's ensure_signing_ready.sh probe: an unattended run
    must never block on a keychain prompt, so a slow or failing probe refuses
    before the host is touched.
    """
    probe = workdir / "signing-probe"
    probe.write_bytes(b"tartci signing probe\n")
    result = sys_.run(["codesign", "--force", "--timestamp", "--sign", identity, str(probe)],
                      timeout=SIGNING_PROBE_TIMEOUT)
    probe.unlink(missing_ok=True)
    if result.rc != 0:
        raise Refused(f"signing identity {identity} cannot sign unattended (exit {result.rc}: "
                      f"{result.text[:200]}); run `pulp ship doctor` to prepare the dedicated "
                      "signing keychain, and never answer a keychain password prompt")


def set_immutable(root: Path, manifest: Path, sys_: System) -> None:
    """The build's `verify --immutable` preconditions (2026-09-21 reseal runbook)."""
    members = [m["path"] for m in json.loads(manifest.read_text())["members"]]
    directories = set()
    for member in members:
        path = root / member
        if path.exists():
            os.chmod(path, (path.stat().st_mode & 0o7777) & ~0o222)
        parent = path.parent
        while parent != root and root in parent.parents:
            directories.add(parent)
            parent = parent.parent
    os.chmod(manifest, 0o444)
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        os.chmod(directory, 0o555)
    os.chmod(root, 0o555)


def _signal_group(pgid: int, sig: int) -> None:
    """Signal a process group that may already have emptied."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def clear_dir(path: Path) -> None:
    """Remove a tree the launcher build left read-only (a-w files, 0555 dirs)."""
    if not path.exists():
        return
    for current, dirs, files in os.walk(path):
        os.chmod(current, 0o755)
        for name in dirs:
            target = Path(current) / name
            if not target.is_symlink():
                os.chmod(target, 0o755)
        for name in files:
            target = Path(current) / name
            if not target.is_symlink():
                os.chmod(target, (target.stat().st_mode & 0o7777) | 0o200)
    shutil.rmtree(path)


def prune_dirs(parent: Path, keep: int) -> None:
    """Keep the newest `keep` entries (names sort by time), removing the rest."""
    if not parent.is_dir():
        return
    entries = sorted((p for p in parent.iterdir() if p.is_dir()),
                     key=lambda p: p.stat().st_mtime)
    for old in entries[:-keep] if keep else entries:
        clear_dir(old)


def restore_writable(root: Path) -> None:
    os.chmod(root, 0o755)
    for current, dirs, files in os.walk(root):
        if ".git" in Path(current).parts:
            continue
        for name in dirs:
            path = Path(current) / name
            if not path.is_symlink():
                os.chmod(path, 0o755)
        for name in files:
            path = Path(current) / name
            if not path.is_symlink():
                os.chmod(path, (path.stat().st_mode & 0o7777) | 0o200)


def build_launcher(cfg: Config, sys_: System, helper: dict, profile: Path, target: str,
                   out_dir: Path) -> tuple[Path, Path]:
    live = Path(helper["path"])
    identity = extract_signing_identity(sys_, live, out_dir)
    signing_probe(sys_, identity, out_dir)
    bundle = out_dir / "TartCILauncher.app"
    approval = out_dir / "approved.sha256"
    manifest = cfg.checkout / ".tartci-support-manifest.json"
    set_immutable(cfg.checkout, manifest, sys_)
    try:
        result = sys_.run(["bash", "scripts/build_macos_launcher.sh", "--output", str(bundle),
                           "--approval-output", str(approval), "--identity", identity,
                           "--support-root", ".", "--profile", str(profile)],
                          cwd=str(cfg.checkout), timeout=1800)
    finally:
        restore_writable(cfg.checkout)
    # The builder's cleanup trap cannot remove its read-only staging copy and
    # prints `rm:` noise after a successful build; the bundle is the verdict.
    if not bundle.is_dir() or not approval.is_file():
        raise Refused(f"launcher build produced no bundle (exit {result.rc}): "
                      + "\n".join(l for l in result.text.splitlines() if not l.startswith("rm:")))
    verify_bundle(cfg, sys_, bundle, profile, target)
    return bundle, approval


def verify_bundle(cfg: Config, sys_: System, bundle: Path, profile: Path, target: str) -> None:
    resources = bundle / "Contents" / "Resources"
    meta = _read_json(resources / "bundle.json") or {}
    if meta.get("source_commit") != target:
        raise Refused(f"new launcher bundle source_commit {meta.get('source_commit')} != "
                      f"target {target}")
    lanes = (_read_json(resources / "lanes.json") or {}).get("lanes") or {}
    rendered = cfg.state_dir / "verify-render"
    shutil.rmtree(rendered, ignore_errors=True)
    result = sys_.run(["python3", "scripts/macos_fleet_lanes.py", "render", str(profile),
                       "--output", str(rendered)], cwd=str(cfg.checkout))
    if result.rc != 0:
        raise Refused(f"cannot render the profile to verify the bundle: {result.text}")
    import plistlib
    expected = {}
    for plist in sorted(rendered.glob("*.plist")):
        env = plistlib.loads(plist.read_bytes()).get("EnvironmentVariables") or {}
        expected[env.get("TARTCI_QUEUE_LANE_ID")] = dict(sorted(env.items()))
    if not expected or {k: (v or {}).get("environment") for k, v in lanes.items()} != expected:
        raise Refused("new launcher lanes.json does not carry the rendered lane environment")
    check = sys_.run(["codesign", "--verify", "--deep", "--strict", str(bundle)])
    if check.rc != 0:
        raise Refused(f"new launcher fails codesign --verify --deep --strict: {check.text}")


# ── receipts ───────────────────────────────────────────────────────────────

class Receipt:
    def __init__(self, cfg: Config, sys_: System, target: str | None, mode: str) -> None:
        self.cfg, self.sys = cfg, sys_
        now = sys_.now()
        self.value: dict[str, Any] = {"schema": SCHEMA, "mode": mode, "target": target,
                                      "started_at": _iso(now), "steps": [], "status": "running",
                                      "pid": os.getpid(),
                                      "pid_start": sys_.process_start(os.getpid())}
        stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = cfg.state_dir / "attempts" / f"{stamp}-{(target or 'none')[:12]}.json"

    def step(self, name: str, detail: str = "", ok: bool = True) -> None:
        self.value["steps"].append({"at": _iso(self.sys.now()), "step": name,
                                    "ok": ok, "detail": detail[:2000]})
        mark = "" if ok else ("REFUSED " if name == "refused" else "FAILED ")
        print(f"self-update: {mark}{name}{': ' + detail if detail else ''}", flush=True)
        if self.value["mode"] == "apply" and self.value["status"] == "running":
            # On disk from the first step, so a SIGKILL leaves a receipt the
            # next run can find and recover instead of nothing at all.
            _write_json(self.path, self.value)

    def finish(self, status: str, error: str = "") -> None:
        self.value.update(status=status, error=error or None, finished_at=_iso(self.sys.now()))
        _write_json(self.path, self.value)
        if self.value["mode"] != "apply" or status not in TERMINAL_STATUSES:
            # A plan or a refusal changed nothing; it must never overwrite (and
            # so hide) the record of the last real attempt.
            return
        _write_json(self.cfg.state_dir / "last.json", {
            "status": status, "target": self.value["target"], "error": error or None,
            "at": self.value["finished_at"], "receipt": str(self.path),
            "host_off": bool(self.value.get("host_off"))})


def _attempts(state_dir: Path) -> list[tuple[Path, dict]]:
    rows = []
    for path in sorted((state_dir / "attempts").glob("*.json")):
        value = _read_json(path)
        if value is not None:
            rows.append((path, value))
    return rows


def _ts(text: str | None) -> float:
    try:
        return dt.datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return 0.0


def recent_attempt(cfg: Config, target: str, now: float) -> str | None:
    for path, value in _attempts(cfg.state_dir):
        # A refusal changed nothing, so it does not spend the attempt.
        if (value.get("mode") != "apply" or value.get("target") != target
                or value.get("status") not in TERMINAL_STATUSES):
            continue
        if now - _ts(value.get("started_at")) < cfg.rate_hours * 3600:
            return f"{path.name} ({value.get('status')})"
    return None


def halt_reason(state_dir: Path) -> str | None:
    """Stop automatic attempts after consecutive failures until a human clears it."""
    cleared = _ts((_read_json(state_dir / "halt-cleared.json") or {}).get("at"))
    streak = 0
    for _, value in _attempts(state_dir):
        if value.get("mode") != "apply" or value.get("status") not in TERMINAL_STATUSES:
            continue
        if _ts(value.get("started_at")) < cleared:
            continue
        streak = 0 if value.get("status") == "succeeded" else streak + 1
    if streak >= MAX_CONSECUTIVE_FAILURES:
        return (f"self-update HALTED after {streak} consecutive failed attempts; read the "
                "receipts, fix the cause, then `tartci fleet-macos self-update --clear-halt`")
    return None


def prune_refusals(state_dir: Path, now: float) -> None:
    for path, value in _attempts(state_dir):
        if value.get("status") in ("refused", "planned") \
                and now - _ts(value.get("started_at")) > REFUSAL_RECEIPT_TTL:
            path.unlink(missing_ok=True)


# ── the procedure ──────────────────────────────────────────────────────────

def checked_in_profile(cfg: Config) -> Path:
    name = _toml(cfg.installed_profile).get("name")
    for path in sorted((cfg.checkout / "profiles").glob("*-macos-fleet.toml")):
        if _toml(path).get("name") == name:
            return path
    raise Refused(f"no checked-in profile named {name!r} at the target commit")


def relay_enabled(cfg: Config) -> bool:
    try:
        return bool((_toml(cfg.network_profile).get("http_connect_relay") or {}).get("enabled"))
    except (OSError, Refused):
        return False
    except ValueError:
        return True  # unreadable profile: run reconcile, which will say why


def tartci(cfg: Config, sys_: System, *args: str, timeout: float = 900) -> Result:
    """The TARGET commit's tartci, run from the managed checkout."""
    return sys_.run(["./tartci", *args], cwd=str(cfg.checkout), env=census_env(),
                    timeout=timeout)


def installed_tartci(cfg: Config, sys_: System, *args: str, timeout: float = 900) -> Result:
    """The INSTALLED shim, run from $HOME (never a checkout)."""
    return sys_.run([str(cfg.shim), *args], cwd=str(cfg.home), env=census_env(),
                    timeout=timeout)


def _alive(sys_: System, pid: Any, start: Any) -> bool:
    return isinstance(pid, int) and start is not None and sys_.process_start(pid) == start


def previous_run_state(cfg: Config, sys_: System) -> tuple[str | None, list[Path]]:
    """(refusal, interrupted receipts).

    An installer that outlived its self-update (recorded in installer.json) or
    a self-update that is still running refuses this run. A receipt still
    "running" whose process is gone was killed (launchd SIGKILL after
    ExitTimeOut) and must be recovered.
    """
    installer = _read_json(cfg.state_dir / "installer.json")
    if installer and isinstance(installer.get("pgid"), int) and sys_.group_alive(installer["pgid"]) \
            and _alive(sys_, installer["pgid"], installer.get("start")):
        return (f"a previous self-update's installer is still running (pgid {installer['pgid']}, "
                f"started {installer.get('start')}); wait for it to exit, then run "
                "`tartci fleet-macos self-update --verify`"), []
    interrupted = []
    for path, value in _attempts(cfg.state_dir):
        if value.get("mode") != "apply" or value.get("status") != "running":
            continue
        if _alive(sys_, value.get("pid"), value.get("pid_start")):
            return (f"another self-update is running (pid {value.get('pid')}, receipt "
                    f"{path.name})"), []
        interrupted.append(path)
    return None, interrupted


def recover_interrupted(cfg: Config, sys_: System, paths: list[Path]) -> None:
    """Finish receipts of runs that were killed: re-pin, pool on, record, count."""
    for path in paths:
        value = _read_json(path) or {}
        target, previous = value.get("target"), value.get("previous")
        if not any(step.get("step") == "announce" for step in value.get("steps", [])):
            # Killed during the read-only gates: nothing on the host changed.
            message = (f"interrupted before any change (pid {value.get('pid')} was killed); "
                       "nothing to recover")
            value.update(status="refused", error=message, finished_at=_iso(sys_.now()))
            _write_json(path, value)
            print(f"self-update: {message} ({path.name})")
            continue
        notes = []
        try:
            running = installed_commit(cfg)[0]
        except Refused:
            running = None
        pin = Path(value["pin_path"]) if value.get("pin_path") else None
        source = None
        if pin is not None:
            if running == target and value.get("approval"):
                source = Path(value["approval"])
            elif running == previous and value.get("snapshot"):
                source = Path(value["snapshot"]) / "approved.sha256"
            if source is not None and source.is_file():
                tmp = pin.with_name(f".{pin.name}.new")
                tmp.write_text(source.read_text())
                os.chmod(tmp, 0o600)
                os.replace(tmp, pin)
                notes.append(f"pin re-matched to the live launcher ({str(running)[:12]})")
        on = installed_tartci(cfg, sys_, "pool", "on")
        notes.append("host is on" if on.rc == 0 else f"HOST LEFT OFF: pool on failed: {on.text[:200]}")
        message = (f"interrupted: the self-update process (pid {value.get('pid')}) was killed "
                   f"during {value['steps'][-1]['step'] if value.get('steps') else 'start'}; "
                   f"host runs {str(running)[:12]}; " + "; ".join(notes)
                   + "; run `tartci fleet-macos self-update --verify`")
        value.setdefault("steps", []).append({"at": _iso(sys_.now()), "step": "interrupted",
                                              "ok": False, "detail": message})
        value.update(status="failed", error=message, finished_at=_iso(sys_.now()))
        _write_json(path, value)
        _write_json(cfg.state_dir / "last.json", {"status": "failed", "target": target,
                                                  "error": message, "at": value["finished_at"],
                                                  "receipt": str(path)})
        print(f"self-update: FAILED (recovered interrupted run): {message}", file=sys.stderr)
    marker = _read_json(cfg.state_dir / "active.json")
    if marker is not None and not _alive(sys_, marker.get("pid"), marker.get("pid_start")):
        (cfg.state_dir / "active.json").unlink(missing_ok=True)


def plan_or_apply(cfg: Config, sys_: System, *, apply: bool, target_ref: str,
                  scheduled: bool = False) -> int:
    now = sys_.now()
    prune_refusals(cfg.state_dir, now)
    blocked, interrupted = previous_run_state(cfg, sys_)
    if blocked:
        print(f"self-update: REFUSED: {blocked}")
        return EXIT_REFUSED
    if interrupted:
        if not apply:
            print(f"self-update: {len(interrupted)} interrupted run(s) would be recovered first: "
                  + ", ".join(p.name for p in interrupted))
        else:
            recover_interrupted(cfg, sys_, interrupted)
            return EXIT_FAILED
    try:
        installed, source = installed_commit(cfg)
        refresh_checkout(cfg, sys_)
    except NotManaged as exc:
        print(f"self-update: not applicable: {exc}")
        _write_json(cfg.state_dir / "skew.json", {"state": "not_applicable", "reason": str(exc),
                                                  "measured_at": _iso(now)})
        return EXIT_UNKNOWN
    except Refused as exc:
        print(f"self-update: UNKNOWN: {exc}")
        _write_json(cfg.state_dir / "skew.json", {"state": "unknown", "reason": str(exc),
                                                  "measured_at": _iso(now)})
        return EXIT_UNKNOWN
    skew = measure_skew(cfg, sys_, installed, now, target_ref)
    _write_json(cfg.state_dir / "skew.json", skew)
    print(f"self-update: installed {installed[:12]} ({source})")
    print(f"self-update: {render_skew(skew)}")
    if skew["state"] in ("unknown", "diverged"):
        return EXIT_UNKNOWN
    target = skew.get("target")
    refresh = None
    if not target and skew["state"] == "current":
        refresh = refresh_reason(cfg, sys_)
        if refresh:
            # Same generation, reinstalled only to rewrite its receipt.
            target = installed
            print(f"self-update: same-generation reinstall needed: {refresh}")
    if not target:
        print("self-update: nothing to do" + {
            "soaking": " (undeployed commits are still soaking)",
            "unverified": " (no soaked commit has green checks)"}.get(skew["state"], ""))
        return EXIT_NOTHING
    receipt = Receipt(cfg, sys_, target, "apply" if apply else "plan")
    receipt.value["previous"] = installed
    receipt.value["refresh"] = refresh
    try:
        me = self_host_id(cfg)
        if apply:
            halted = halt_reason(cfg.state_dir)
            if halted:
                raise Refused(halted)
            prior = recent_attempt(cfg, target, now)
            if prior:
                raise Refused(f"already attempted {target[:12]} within {cfg.rate_hours}h: {prior}")
        if apply and scheduled:
            sys_.sleep(stagger_seconds(me))
        _checkout(cfg, sys_, target)
        receipt.step("checkout", f"{cfg.checkout} detached at {target}")
        profile = checked_in_profile(cfg)
        for name, args in (
                ("support-manifest", ["support-manifest", "write", "--root", ".",
                                      "--output", ".tartci-support-manifest.json"]),
                ("validate", ["fleet-macos", "validate", str(profile)])):
            result = tartci(cfg, sys_, *args)
            if result.rc != 0:
                raise Refused(f"{name} failed: {result.text}")
            receipt.step(name)
        helper = launch_helper(cfg)
        bundle = approval = None
        if helper is not None and not refresh:
            with tempfile.TemporaryDirectory() as scratch:
                identity = extract_signing_identity(sys_, Path(helper["path"]), Path(scratch))
                signing_probe(sys_, identity, Path(scratch))
            receipt.step("signing", f"{'would build' if not apply else 'will build'} a sealed "
                         f"launcher signed by {identity} (extracted from the live bundle's leaf "
                         "certificate; a timestamped signing probe with it succeeded)")
        install_args = ["fleet-macos", "install", str(profile), "--support-source", ".",
                        "--support-manifest", ".tartci-support-manifest.json"]
        if helper is None:
            dry = tartci(cfg, sys_, *install_args)
            if dry.rc != 0:
                raise Refused(f"install dry-run failed: {dry.text}")
            receipt.step("install-dry-run", "ok")
        else:
            # The installer verifies the new bundle against the approval pin,
            # and the pin may only move once the host is out of service, so a
            # sealed host's dry run happens right after the pin, before --apply.
            receipt.step("install-dry-run", "deferred until the approval pin moves (after pool off)")
        # A plan evaluates every gate and reports all refusals; an apply stops
        # at the first.
        refusals = []
        busy = check_peers(cfg, sys_, me)
        if busy:
            refusal = "another fleet host is not serving normally: " + "; ".join(busy)
            if apply:
                raise Refused(refusal)
            refusals.append(refusal)
            receipt.step("peers", refusal, ok=False)
        else:
            receipt.step("peers", "every other published host is on and not updating")
        allow = False
        try:
            allow, rule = floor_decision(cfg, sys_)
            receipt.step("capacity-floor", rule)
        except Refused as exc:
            if apply:
                raise
            refusals.append(str(exc))
            receipt.step("capacity-floor", str(exc), ok=False)
        if refusals:
            raise Refused(" | ".join(refusals))
        if helper is not None and apply and not refresh:
            # Built only once every cheap gate passed: a refused run must not
            # have spent a signing build, and a rebuild must always be possible.
            builds = cfg.state_dir / "builds"
            out_dir = builds / target
            clear_dir(out_dir)
            out_dir.mkdir(parents=True)
            bundle, approval = build_launcher(cfg, sys_, helper, profile, target, out_dir)
            prune_dirs(builds, KEEP_BUILDS)
            install_args += ["--launch-helper-source", str(bundle)]
            receipt.step("launcher-build", f"{bundle} verified for {target[:12]}")
        if not apply:
            receipt.step("plan", "would snapshot the running generation, drain, wait for no "
                         "mid-job lane, pool off, " + ("pin approval, " if helper else "")
                         + "install --apply, " + ("relay reconcile, " if relay_enabled(cfg) else "")
                         + "pool on, verify; on any failure after install, roll back to "
                         f"{installed[:12]} and verify it")
            receipt.finish("planned")
            return EXIT_OK
        run = Run(cfg, sys_, receipt, me=me, target=target, previous=installed,
                  profile=profile, install_args=install_args, allow=allow,
                  helper=helper, approval=approval)
        return run.execute()
    except Refused as exc:
        receipt.step("refused", str(exc), ok=False)
        receipt.finish("refused", str(exc))
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - nothing was mutated yet; never leave "running"
        message = f"unexpected {type(exc).__name__} before any change: {exc}"
        receipt.step("refused", message, ok=False)
        receipt.finish("refused", message)
        return EXIT_REFUSED


def refresh_reason(cfg: Config, sys_: System) -> str | None:
    """Why a current host must reinstall its own generation, or None.

    Today: its receipt no longer matches the OS-managed interpreter because
    macOS was updated (interpreter_changed_by_os_update). Read-only.
    """
    status = installed_tartci(cfg, sys_, "pool", "status", "--json")
    try:
        problems = (json.loads(status.out).get("fleet") or {}).get("problems") or []
    except (json.JSONDecodeError, AttributeError):
        return None
    for problem in problems:
        if isinstance(problem, dict) and problem.get("code") == "interpreter_changed_by_os_update":
            return str(problem.get("detail") or problem["code"])
    return None


def _checkout(cfg: Config, sys_: System, commit: str) -> None:
    result = sys_.run(["git", "-C", str(cfg.checkout), "checkout", "--quiet",
                       "--detach", "--force", commit])
    if result.rc != 0:
        raise Refused(f"cannot check out {commit[:12]}: {result.text}")


class Run:
    """One --apply after every precondition passed: take the host out, update,
    verify, and on any failure put the previous generation back and verify it.
    """

    POOL_ON_ATTEMPTS = 3

    def __init__(self, cfg: Config, sys_: System, receipt: Receipt, *, me: str, target: str,
                 previous: str, profile: Path, install_args: list[str], allow: bool,
                 helper: dict | None, approval: Path | None) -> None:
        self.cfg, self.sys, self.receipt = cfg, sys_, receipt
        self.me, self.target, self.previous = me, target, previous
        self.profile, self.install_args, self.helper, self.approval = (
            profile, install_args, helper, approval)
        self.flag = ["--allow-last-serving-host"] if allow else []
        self.pin_path = Path(helper["approval_sha256_path"]) if helper else None
        self.pin_moved = False
        self.snapshot: Path | None = None
        self.phase = "announce"
        receipt.value.update(approval=str(approval) if approval else None,
                             pin_path=str(self.pin_path) if self.pin_path else None)

    # ── entry ───────────────────────────────────────────────────────────
    def execute(self) -> int:
        import signal
        previous_handler = signal.signal(signal.SIGTERM, _raise_terminated)
        try:
            _announce(self.cfg, self.sys, self.me, self.target)
            self.receipt.step("announce", str(self.cfg.state_dir / "active.json"))
            self._snapshot()
            try:
                self._update()
            except Refused as exc:
                return self._safe_recover(f"refused after drain: {exc}", refused=True)
            except Failed as exc:
                return self._safe_recover(str(exc))
            except Terminated as exc:
                return self._safe_recover(f"terminated ({exc}) during {self.phase}",
                                          terminated=True)
            except Exception as exc:  # noqa: BLE001 - never leave the host out
                return self._safe_recover(f"{type(exc).__name__} during {self.phase}: {exc}")
            self.receipt.finish("succeeded")
            prune_dirs(self.cfg.state_dir / "rollback", KEEP_SNAPSHOTS)
            return EXIT_OK
        except Refused as exc:  # announce lost the race: nothing was touched
            self.receipt.step("refused", str(exc), ok=False)
            self.receipt.finish("refused", str(exc))
            return EXIT_REFUSED
        finally:
            (self.cfg.state_dir / "active.json").unlink(missing_ok=True)
            signal.signal(signal.SIGTERM, previous_handler)

    # ── the update ──────────────────────────────────────────────────────
    def _snapshot(self) -> None:
        """What rollback reinstalls: the running profile, pin and sealed bundle."""
        stamp = dt.datetime.fromtimestamp(self.sys.now(), dt.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
        snap = self.cfg.state_dir / "rollback" / f"{stamp}-{self.previous[:12]}"
        snap.mkdir(parents=True)
        shutil.copy2(self.cfg.installed_profile, snap / "profile.toml")
        if self.helper is not None:
            shutil.copy2(self.pin_path, snap / "approved.sha256")
            result = self.sys.run(["/usr/bin/ditto", "--noqtn", self.helper["path"],
                                   str(snap / "TartCILauncher.app")])
            if result.rc != 0:
                raise Refused(f"cannot snapshot the live launcher: {result.text}")
            self._verify_snapshot_bundle(snap)
        _write_json(snap / "snapshot.json", {"previous": self.previous, "at": _iso(self.sys.now())})
        self.snapshot = snap
        self.receipt.value["snapshot"] = str(snap)
        self.receipt.step("snapshot", f"{snap} (previous generation {self.previous[:12]})")

    def _verify_snapshot_bundle(self, snap: Path) -> None:
        """The copy rollback would reinstall must verify against the current pin NOW."""
        policy = self.sys.run(["python3", "-c",
                               "import sys; sys.path.insert(0, 'scripts'); "
                               "import macos_launcher_identity as m; "
                               "print(m.profile_policy_digest(sys.argv[1]))",
                               str(snap / "profile.toml")], cwd=str(self.cfg.checkout))
        pin = (snap / "approved.sha256").read_text().strip()
        check = self.sys.run(["python3", "scripts/macos_launcher_identity.py", "verify",
                              str(snap / "TartCILauncher.app"),
                              "--identifier", str(self.helper.get("identifier", "")),
                              "--team-id", str(self.helper.get("team_id", "")),
                              "--sha256", pin, "--profile-policy-sha256", policy.out.strip()],
                             cwd=str(self.cfg.checkout))
        if policy.rc != 0 or check.rc != 0:
            raise Refused("the snapshot of the live launcher does not verify against the current "
                          f"approval pin, so it could not be rolled back to: "
                          f"{(check.text or policy.text)[:300]}")
        self.receipt.step("snapshot-verify", "snapshot launcher verifies against the current pin")

    def _update(self) -> None:
        cfg, sys_ = self.cfg, self.sys
        self.phase = "drain"
        drain = tartci(cfg, sys_, "pool", "drain", *self.flag)
        if drain.rc not in (0, 3):  # 3 = drain pending on a persistent runner
            raise Refused(f"pool drain refused: {drain.text}")
        self.receipt.step("drain", drain.text[:300])
        self.phase = "wait-idle"
        self._wait_idle()
        self.phase = "off"
        off = tartci(cfg, sys_, "pool", "off", *self.flag)
        if off.rc != 0:
            raise Failed(f"pool off failed: {off.text}")
        self.receipt.step("off")
        if self.pin_path is not None and self.approval is not None:
            self.phase = "pin"
            self._write_pin(self.approval.read_text())
            self.pin_moved = True
            self.receipt.step("pin", "new launcher approval pinned (previous in the snapshot)")
            dry = tartci(cfg, sys_, *self.install_args)
            if dry.rc != 0:
                raise Failed(f"install dry-run failed against the new pin: {dry.text}")
            self.receipt.step("install-dry-run", "ok against the new pin")
        self.phase = "install"
        self._install(self.install_args)
        self.receipt.step("install", f"applied {self.target[:12]}")
        self.phase = "relay"
        self._relay()
        self.phase = "pool-on"
        if not self._pool_on():
            raise Failed("pool on failed after install")
        self.phase = "verify"
        verify(cfg, sys_, self.target, self.receipt)

    def _wait_idle(self, *, allow_now: bool = False) -> None:
        deadline = self.sys.now() + self.cfg.wait_seconds
        while True:
            plan = tartci(self.cfg, self.sys, "pool", "off", "--plan", *self.flag)
            if plan.rc == 0:
                break
            if plan.rc != 12:
                raise Failed(f"pool off --plan refused (exit {plan.rc}): {plan.text[:300]}")
            if self.sys.now() >= deadline:
                raise Failed(f"a lane was still mid-job after {self.cfg.wait_seconds}s")
            self.sys.sleep(self.cfg.poll_seconds)
        self.receipt.step("wait-idle", "no owned lane mid-job")

    def _install(self, args: list[str]) -> None:
        for attempt in range(1, INSTALL_ATTEMPTS + 1):
            result = self.sys.run_critical(["./tartci", *args, "--apply"],
                                           cwd=str(self.cfg.checkout), env=census_env(),
                                           timeout=INSTALL_TIMEOUT,
                                           record=self.cfg.state_dir / "installer.json")
            if result.rc == 0:
                return
            self.receipt.step("install", f"attempt {attempt} failed: {result.text[:300]}", ok=False)
            if result.rc == EXIT_TIMED_OUT:
                # Never install on top of an interrupted install: recovery reads
                # what the installer's restore trap left and decides.
                raise Failed(f"install --apply timed out: {result.text[:300]}")
            if attempt == INSTALL_ATTEMPTS:
                raise Failed(f"install --apply failed {INSTALL_ATTEMPTS} times: {result.text}")
            self.sys.sleep(INSTALL_RETRY_SECONDS)

    def _relay(self) -> None:
        if not relay_enabled(self.cfg):
            return
        relay = self.sys.run(["python3", "scripts/network_profile.py", "reconcile", "--json"],
                             cwd=str(self.cfg.checkout))
        value = {}
        try:
            value = json.loads(relay.out)
        except json.JSONDecodeError:
            pass
        if relay.rc != 0 or value.get("ok") is not True or not value.get("probe"):
            raise Failed(f"relay reconcile/probe failed: {relay.text[:300]}")
        self.receipt.step("relay", f"reconciled; probe: {value.get('probe')}")

    def _write_pin(self, text: str) -> None:
        tmp = self.pin_path.with_name(f".{self.pin_path.name}.new")
        tmp.write_text(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.pin_path)

    POOL_ON_BACKOFF = (15, 45, 90)

    def _pool_on(self) -> bool:
        for attempt in range(1, self.POOL_ON_ATTEMPTS + 1):
            on = installed_tartci(self.cfg, self.sys, "pool", "on")
            if on.rc == 0:
                self.receipt.step("pool-on", "installed shim")
                return True
            self.receipt.step("pool-on", f"attempt {attempt} failed (exit {on.rc}): {on.text[:300]}",
                              ok=False)
            if attempt < self.POOL_ON_ATTEMPTS:
                self.sys.sleep(self.POOL_ON_BACKOFF[attempt - 1])
        return False

    def _reinstall_previous(self) -> None:
        """Reinstall the snapshot generation from its own commit (pin + bundle)."""
        cfg, sys_ = self.cfg, self.sys
        _checkout(cfg, sys_, self.previous)
        result = tartci(cfg, sys_, "support-manifest", "write", "--root", ".",
                        "--output", ".tartci-support-manifest.json")
        if result.rc != 0:
            raise Failed(f"support-manifest for {self.previous[:12]}: {result.text}")
        args = ["fleet-macos", "install", str(self.snapshot / "profile.toml"),
                "--support-source", ".", "--support-manifest", ".tartci-support-manifest.json"]
        if self.helper is not None:
            self._write_pin((self.snapshot / "approved.sha256").read_text())
            self.receipt.step("pin-restore", "previous approval restored from the snapshot")
            args += ["--launch-helper-source", str(self.snapshot / "TartCILauncher.app")]
        self._install(args)

    def _ensure_on(self) -> bool:
        """Put the host back in service, whatever else failed.

        `pool on` first. If it refuses (its own rollback then closes
        admission), reinstall the generation the host is running from its
        own commit, which rewrites a receipt `pool on` rejects, and try again.
        A host is only left OFF when that also fails; the caller records it.
        """
        if self._pool_on():
            return True
        running = self._running()
        try:
            if running == self.previous and self.snapshot is not None:
                self.receipt.step("reinstall-for-pool-on",
                                  f"pool on refused; reinstalling {self.previous[:12]}")
                self._reinstall_previous()
            elif running == self.target:
                self.receipt.step("reinstall-for-pool-on",
                                  f"pool on refused; reinstalling {self.target[:12]}")
                _checkout(self.cfg, self.sys, self.target)
                tartci(self.cfg, self.sys, "support-manifest", "write", "--root", ".",
                       "--output", ".tartci-support-manifest.json")
                self._install(self.install_args)
            else:
                return False
        except Exception as exc:  # noqa: BLE001 - recorded; the host stays as it is
            self.receipt.step("reinstall-for-pool-on", f"{type(exc).__name__}: {exc}", ok=False)
            return False
        return self._pool_on()

    # ── recovery ────────────────────────────────────────────────────────
    def _safe_recover(self, reason: str, **kwargs) -> int:
        """Recovery that always ends with a finished receipt and a counted attempt.

        A second SIGTERM (or anything else) during recovery must not leave the
        receipt `running` with no last.json: that hides the host's state and
        does not count toward the halt.
        """
        try:
            return self._recover(reason, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            on = False
            try:
                self._repin_to_live()
                on = self._pool_on()
            except BaseException:  # noqa: BLE001
                pass
            return self._terminal("failed", host_off=not on, message=f"{reason}; RECOVERY INTERRUPTED "
                                  f"({type(exc).__name__}: {exc}); host is "
                                  f"{'on' if on else 'possibly OFF'} running "
                                  f"{str(self._running())[:12]}; run `tartci fleet-macos "
                                  "self-update --verify`")

    def _repin_to_live(self) -> None:
        """Make the approval pin match whichever launcher bundle is actually live."""
        if self.pin_path is None or self.snapshot is None:
            return
        running = self._running()
        if running == self.target and self.approval is not None:
            self._write_pin(self.approval.read_text())
        elif running == self.previous:
            self._write_pin((self.snapshot / "approved.sha256").read_text())

    def _running(self) -> str | None:
        try:
            return installed_commit(self.cfg)[0]
        except Refused:
            return None

    def _recover(self, reason: str, *, refused: bool = False, terminated: bool = False) -> int:
        self.receipt.step("failed", reason, ok=False)
        running = self._running()
        if running == self.previous:
            # Nothing new is installed (the installer rolls its own failure
            # back), so putting the host back is: pin, then pool on.
            if self.pin_moved:
                self._write_pin((self.snapshot / "approved.sha256").read_text())
                self.receipt.step("pin-restore", "previous approval restored from the snapshot")
            if not self._ensure_on():
                return self._terminal("failed", f"{reason}; HOST LEFT OFF: pool on failed "
                                      f"(generation {self.previous[:12]} is installed)",
                                      host_off=True)
            if refused:
                self.receipt.finish("refused", reason)
                return EXIT_REFUSED
            return self._terminal("failed", f"{reason}; host left on the previous generation "
                                  f"{self.previous[:12]}")
        if terminated:
            # launchd will SIGKILL soon: no time for a full rollback. Get the
            # host serving and say exactly what it runs.
            self._repin_to_live()
            on = self._pool_on()
            return self._terminal("failed", f"{reason}; NOT rolled back (terminated); host is "
                                  f"{'on' if on else 'OFF'} running {str(running)[:12]}; run "
                                  "`tartci fleet-macos self-update --verify` to check it",
                                  host_off=not on)
        return self._rollback(reason)

    def _rollback(self, reason: str) -> int:
        cfg, sys_ = self.cfg, self.sys
        self.receipt.step("rollback", f"reinstalling {self.previous[:12]} from {self.snapshot}")
        try:
            self._wait_idle()
            off = tartci(cfg, sys_, "pool", "off", *self.flag)
            if off.rc != 0:
                raise Failed(f"pool off for rollback failed: {off.text}")
            self._reinstall_previous()
            self.receipt.step("rollback-install", f"reinstalled {self.previous[:12]}")
            if not self._pool_on():
                raise Failed("pool on after rollback failed")
            verify(cfg, sys_, self.previous, self.receipt)
        except BaseException as exc:  # noqa: BLE001 - incl. a second SIGTERM
            # Whatever the rollback's own failure, capacity comes back: a
            # failed verification is never a reason to leave the host out.
            self._repin_to_live()
            on = self._ensure_on() if not isinstance(exc, Terminated) else self._pool_on()
            return self._terminal("failed", f"{reason}; ROLLBACK FAILED: "
                                  f"{type(exc).__name__}: {exc}; host is "
                                  f"{'on' if on else 'OFF'} running "
                                  f"{str(self._running())[:12]}", host_off=not on)
        return self._terminal("rolled_back", f"{reason}; rolled back to {self.previous[:12]} "
                              "and verified")

    def _terminal(self, status: str, message: str, *, host_off: bool = False) -> int:
        self.receipt.value["host_off"] = host_off
        self.receipt.step(status, message, ok=False)
        self.receipt.finish(status, message)
        print(f"self-update: {status.upper().replace('_', ' ')}: {message}", file=sys.stderr)
        return EXIT_FAILED


def _announce(cfg: Config, sys_: System, me: str, target: str) -> None:
    marker = cfg.state_dir / "active.json"
    _write_json(marker, {"host_id": me, "target": target, "ts": sys_.now(),
                         "pid": os.getpid(), "pid_start": sys_.process_start(os.getpid())})
    # Re-read peers AFTER announcing: two hosts that announce together both
    # see each other here, and only the lower host id proceeds.
    for peer, ssh_target in published_peers(cfg, sys_).items():
        if peer == me:
            continue
        busy, evidence = peer_state(cfg, sys_, peer, ssh_target)
        if busy and ("self-updating" not in evidence or peer < me):
            marker.unlink(missing_ok=True)
            raise Refused(f"peer changed after announcing: {evidence}")


def _verify_once(cfg: Config, sys_: System, target: str) -> tuple[list[str], bool]:
    """(problems, settling). settling: every problem is a lane that has not yet
    written its first heartbeat, which a freshly loaded supervisor needs a few
    seconds to do."""
    problems = []
    settling = False
    status = installed_tartci(cfg, sys_, "pool", "status", "--json")
    try:
        value = json.loads(status.out)
    except json.JSONDecodeError:
        value = {}
        problems.append(f"pool status unreadable: {status.text[:200]}")
    if value:
        fleet = value.get("fleet") or {}
        if value.get("state") != "on" or value.get("participating") is not True:
            problems.append(f"pool is {value.get('state')} after pool on")
        if fleet.get("managed") and fleet.get("fleet_ready") is not True:
            found = fleet.get("problems") or []
            problems.append(f"fleet not ready: {found}")
            settling = bool(found) and all(
                isinstance(p, dict) and p.get("code") in SETTLING_PROBLEM_CODES
                for p in found)
        if (fleet.get("serving") or {}).get("blocked") is True:
            problems.append("serving BLOCKED")
    try:
        running, source = installed_commit(cfg)
        if running != target:
            problems.append(f"{source} runs {running[:12]}, not {target[:12]}")
    except Refused as exc:
        problems.append(str(exc))
    guard = installed_tartci(cfg, sys_, "launchd", "guard", "--command",
                             "launchctl kickstart -k gui/1/com.danielraffel.tartci."
                             "tart-runner-macos-fleet.verify.lane")
    allow = installed_tartci(cfg, sys_, "launchd", "guard", "--command", "launchctl list")
    if guard.rc != 2 or allow.rc != 0:
        problems.append(f"installed `tartci launchd guard` missing or wrong "
                        f"(block={guard.rc}, allow={allow.rc})")
    return problems, settling and len(problems) == 1


def verify(cfg: Config, sys_: System, target: str, receipt: Receipt) -> None:
    """Pool on, fleet ready, executing `target`, guard present.

    Verification runs seconds after `pool on`, before a just-loaded supervisor
    has written its first heartbeat. A result whose only problem is that is
    re-read until VERIFY_SETTLE_SECONDS elapse; anything else fails at once.
    """
    deadline = sys_.now() + VERIFY_SETTLE_SECONDS
    while True:
        problems, settling = _verify_once(cfg, sys_, target)
        if not problems:
            break
        if not settling or sys_.now() >= deadline:
            raise Failed(f"verification of {target[:12]}: " + "; ".join(problems))
        sys_.sleep(VERIFY_SETTLE_POLL_SECONDS)
    receipt.step("verify", f"pool on and ready, executing {target[:12]}, guard present")


def status_lines(state_dir: Path) -> list[str]:
    """For pool status / doctor / watchdog: skew and the last attempt."""
    lines = [render_skew(_read_json(state_dir / "skew.json"))]
    last = _read_json(state_dir / "last.json")
    if last and last.get("status") in ("failed", "rolled_back"):
        word = "FAILED" if last["status"] == "failed" else "ROLLED BACK"
        lines.append(f"self-update: LAST ATTEMPT {word} at {last.get('at')} for "
                     f"{str(last.get('target'))[:12]}: {last.get('error')}")
    if last and last.get("host_off"):
        lines.append("self-update: THIS HOST WAS LEFT OFF (pool on failed after a failed "
                     "update); check `tartci pool status`, then `tartci pool on`")
    halted = halt_reason(state_dir)
    if halted:
        lines.append(halted)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci fleet-macos self-update")
    parser.add_argument("--target", default="origin/main")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="read-only (default)")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--status", action="store_true", help="print cached skew and last attempt")
    mode.add_argument("--verify", action="store_true",
                      help="verify the running generation (pool on and ready, executing what "
                      "the installer says, guard present) without changing anything")
    mode.add_argument("--clear-halt", action="store_true",
                      help="resume automatic attempts after consecutive failures")
    mode.add_argument("--peers", action="store_true",
                      help="print each published peer and the SSH target it resolves to")
    mode.add_argument("--refresh-skew", action="store_true",
                      help="fetch main and record skew only (no other checks)")
    parser.add_argument("--scheduled", action="store_true",
                        help="apply after this host's stagger offset (the periodic agent)")
    parser.add_argument("--if-older", type=int, default=0,
                        help="with --refresh-skew: skip unless the cache is older (seconds)")
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--settings", type=Path, default=None,
                        help="settings TOML (default ~/.config/tartci/self-update.toml)")
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.home), args.settings)
    sys_ = System()
    if args.status:
        print("\n".join(status_lines(cfg.state_dir)))
        return 0
    if args.verify:
        try:
            running, source = installed_commit(cfg)
            verify(cfg, sys_, running, Receipt(cfg, sys_, running, "verify"))
        except (Refused, Failed) as exc:
            print(f"self-update: VERIFY FAILED: {exc}", file=sys.stderr)
            return EXIT_FAILED
        return 0
    if args.clear_halt:
        _write_json(cfg.state_dir / "halt-cleared.json", {"at": _iso(sys_.now())})
        print("self-update: halt cleared; automatic attempts resume")
        return 0
    if args.peers:
        try:
            refresh_checkout(cfg, sys_)
            me = self_host_id(cfg)
            for peer, target in published_peers(cfg, sys_).items():
                print(f"{peer}\t{target}{'  (this host)' if peer == me else ''}")
        except Refused as exc:
            print(f"self-update: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        return 0
    if args.refresh_skew:
        cached = _read_json(cfg.state_dir / "skew.json")
        if cached and args.if_older:
            try:
                age = sys_.now() - dt.datetime.strptime(
                    cached["measured_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=dt.timezone.utc).timestamp()
                if age < args.if_older:
                    print(render_skew(cached))
                    return 0
            except (KeyError, ValueError):
                pass
        try:
            installed, _ = installed_commit(cfg)
            refresh_checkout(cfg, sys_)
            skew = measure_skew(cfg, sys_, installed, sys_.now(), args.target)
        except NotManaged as exc:
            skew = {"state": "not_applicable", "reason": str(exc), "measured_at": _iso(sys_.now())}
        except Refused as exc:
            skew = {"state": "unknown", "reason": str(exc), "measured_at": _iso(sys_.now())}
        _write_json(cfg.state_dir / "skew.json", skew)
        print(render_skew(skew))
        return 0
    return plan_or_apply(cfg, sys_, apply=args.apply, target_ref=args.target,
                         scheduled=args.scheduled)


if __name__ == "__main__":
    sys.exit(main())
