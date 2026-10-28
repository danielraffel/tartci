#!/usr/bin/env python3
"""Keep a fleet host on the tartci code that main has, verified.

tartci fleet-macos self-update [--target REF] [--plan] [--apply] codifies the
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
  3. One host at a time, in queue order: read every other published host's
     pool state, self-update marker and waiting ticket over SSH. A peer that
     is draining, unreadable, updating, or waiting longer than this host
     DEFERS this host. A peer that is OFF and not updating does not: the
     capacity floor (step 4) counts it as serving nothing. A deferred host
     keeps its place (waiting.json), exits 0, and becomes loud (exit 3, a
     self_update_starved event) only past STARVED_AFTER_SECONDS. Then
     announce, settle, and re-read: of two hosts updating at once the earlier
     announcement proceeds, and only a near-tie falls back to the lower id.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capacity_floor  # noqa: E402 - sibling module; owns the peer liveness judgement
import gate_job_demand  # noqa: E402 - sibling module; queued self-hosted gate jobs
import tartci_incident  # noqa: E402 - sibling module; the alert issue marker line
import tart_pin  # noqa: E402 - sibling module; the pinned Tart and its drained upgrade
import secret_files  # noqa: E402 - reads keychain.env without leaking a value
from python_child import child_python  # noqa: E402 - the interpreter children start with
import fleet_source  # noqa: E402 - resolves the optional private fleet source
import fleet_source_sync  # noqa: E402 - exports the private fleet source per commit

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the launchd python is 3.9
    tomllib = None  # type: ignore[assignment]

SCHEMA = "tartci.self-update/v1"
# How often the launchd watchdog re-measures skew (tartci_launchd_watchdog.py
# refresh_skew); a cached skew older than state_age.STALE_FACTOR of these reads STALE.
SKEW_REFRESH_S = 1800
DEFAULT_REPOSITORY = "Generous-Corp/tartci"
LEGACY_REPOSITORY = "danielraffel/tartci"
ACCEPTED_REPOSITORIES = frozenset({DEFAULT_REPOSITORY, LEGACY_REPOSITORY})
REPO_URL = f"https://github.com/{DEFAULT_REPOSITORY}.git"
SHA = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_SOAK_SECONDS = 1800
DEFAULT_RATE_HOURS = 6
DEFAULT_STALE_HOURS = 24
DEFAULT_WAIT_SECONDS = 90 * 60
# While the drain waits on the same lanes, a receipt step at least this often.
WAIT_IDLE_NOTE_SECONDS = 5 * 60
DEFAULT_POLL_SECONDS = 45
INSTALL_ATTEMPTS = 4
# `shipyard writer-domain-exec` exits this, naming WRITER_DOMAIN_OVERLAP, when a
# Shipyard Sandbox E2E audit holds the writer domain past its acquire timeout.
WRITER_DOMAIN_OVERLAP_EXIT = 75
WRITER_DOMAIN_OVERLAP = "sandbox_writer_domain_overlap"
INSTALL_RETRY_SECONDS = 30
# Readiness problems that only mean a supervisor loaded by `pool on` has not yet
# written its first heartbeat; verification re-reads them for a bounded window.
SETTLING_PROBLEM_CODES = frozenset({"heartbeat_missing", "supervisor_not_running",
                                    "supervisor_pid_missing",
                                    # The previous generation's supervisor is
                                    # finishing its job before it hands the lane
                                    # over (runner.sh tartci_lane_claim).
                                    "orphaned_supervisor", "supervisor_handoff_pending"})
# Readiness problems about the host itself, not the generation: an install
# neither causes nor fixes them, so verifying a new generation must not fail
# on them. m5 (AC sleep 1) rolled back every update once its idle sleep
# became a readiness problem, twice on 2026-10-02.
HOST_CONDITION_CODES = frozenset({"host_idle_sleep_enabled", "power_unverified",
                                  "disk_pressure"})
VERIFY_SETTLE_SECONDS = 180
VERIFY_SETTLE_POLL_SECONDS = 10
ACTIVE_MARKER_TTL = 3 * 3600
# After writing its marker a host waits this long, then re-reads every peer.
# It must exceed ANNOUNCE_TIE_SECONDS plus an SSH read, so that of two hosts
# that announce close together each one sees the other's marker.
ANNOUNCE_SETTLE_SECONDS = 30
# Two markers written within this many seconds of each other are simultaneous
# and the lower host id proceeds; otherwise the EARLIER announcement proceeds.
ANNOUNCE_TIE_SECONDS = 10
# A waiting ticket not refreshed within this long belongs to a host that has
# stopped trying (halted, off the network, profile removed) and is ignored.
WAITING_TICKET_TTL = 2 * 3600
# Deferred without a turn this long is starvation: loud, and exit 3. Without
# queued gate demand for this host the bound is STARVED_FALLBACK_SECONDS; with
# it, STARVED_AFTER_SECONDS (set below), one hour past the dark-peer exclusion.
STARVED_FALLBACK_SECONDS = 6 * 3600
# The self-update agent's launchd StartInterval: one survey of every peer per
# attempt (launchd/com.danielraffel.tartci.self-update.plist.template).
SELF_UPDATE_INTERVAL_SECONDS = 1800
# A peer whose pool status has been unreadable this long stops holding the
# update turn (it still serves nothing for the capacity floor). A peer that is
# READ holding an update marker this old already counts as not updating, so a
# dark peer, even one still mid-install, gets no more trust than a seen one
# with a stale marker. The bound also outlasts the longest a peer can
# legitimately be mid-update. Unreadable means unreachable; a readable peer
# that is draining, off or updating never accrues dark time.
PEER_UNREACHABLE_EXCLUDE_SECONDS = ACTIVE_MARKER_TTL
# ...and at least this many consecutive unreadable reads inside that time, so
# two reads either side of a long sleep cannot meet it alone.
PEER_UNREACHABLE_MIN_READS = 4
# A peer corroborates "X is dark" only from its own read of X this recent,
# measured on its own clock.
PEER_UNREACHABLE_FRESH_SECONDS = 3600
PEER_UNREADABLE_NAME = "peer-unreadable.json"
# With gate demand queued, starvation is one hour past the point a dark peer
# stops holding the turn: a single dark host must not make every other host
# raise an issue two hours before the queue heals itself.
STARVED_AFTER_SECONDS = PEER_UNREACHABLE_EXCLUDE_SECONDS + 3600
# A peer DRAINING with no live update marker, whose pool state has not changed
# for this long, stops holding the update turn and is left to the capacity
# floor, like an off peer. A self-update holds a live marker for its whole
# drain, and a drain waits only for running jobs, so a markerless drain this
# old is an operator drain or an update that died after draining. The bound is
# the marker's own TTL: a dead update's marker stops counting at the same age.
PEER_DRAIN_STALE_SECONDS = ACTIVE_MARKER_TTL
# A draining peer with NO update marker at all is an operator drain once its
# pool state has been unchanged this long (see read_peer).
PEER_DRAIN_GRACE_SECONDS = 15 * 60
MAX_CONSECUTIVE_FAILURES = 3
REFUSAL_RECEIPT_TTL = 7 * 86400
CHECK_CANDIDATES = 5
SIGNING_PROBE_TIMEOUT = 60
TARTCI_REPO = DEFAULT_REPOSITORY
# A peer with no `ssh` in its profile is reached through this alias.
SSH_ALIAS_CONVENTION = "tartci-{host_id}"


def peer_ssh(target: object) -> list[str] | None:
    """The ssh argv up to and including `target`, or None when `target` is not
    a safe SSH destination. A published host row's `ssh` is not bound by
    profiles_sha256, so a value like "-oProxyCommand=..." would otherwise be
    read by ssh as an option; "--" ends option parsing as well. A refused
    destination is treated by every caller as an unreadable peer: fail closed.
    """
    destination = fleet_source.ssh_destination(target)
    if destination is None:
        return None
    return ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", destination]


def refused_destination(host_id: str, target: object) -> str:
    return f"peer {host_id} ssh destination {target!r} refused (not a safe SSH destination)"
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
# A peer was busy or had waited longer, so this host deferred its turn. That is
# the protocol working, so it exits 0 and launchd's last exit stays clean;
# waiting.json and `pool status` carry it, and it becomes EXIT_REFUSED plus a
# self_update_starved event once it has lasted STARVED_AFTER_SECONDS (4 h) with
# queued gate demand, or STARVED_FALLBACK_SECONDS (6 h) without it.
EXIT_DEFERRED = 0
# tartci pool off exits these when a precondition refuses before it stops
# anything: 11 the capacity floor (including an unreadable census), 12 a lane
# that could not be proven idle, 13 lanes launchd still held after the unload
# wait (m5studio, 2026-10-01: "launchd still holds ... forge-gate after 40s").
# In every case nothing was installed and the recovery puts the pool back on.
POOL_OFF_REFUSALS = (11, 12, 13)
EXIT_FAILED = 4        # a mutation ran and failed; see the receipt for what was restored
EXIT_UNKNOWN = 5       # skew or installed state could not be determined


class ConfigError(ValueError):
    """self-update.toml cannot be used as written."""


class TartBroken(Exception):
    """A Tart upgrade failed and its rollback did not bring the previous Tart
    back: the host must stay OFF, not serve on a Tart that may not run."""


class LauncherConsent(Exception):
    """A new sealed launcher is installed but its volume probe fails."""


class Refused(Exception):
    """A precondition failed before anything on the host changed."""


class HostFault(Refused):
    """A local host/build fault that must release a stale queue ticket."""


class Deferred(Refused):
    """Refused only because another host holds, or has waited longer for, a turn."""


class WriterDomainBusy(Refused):
    """A Shipyard Sandbox E2E audit holds the writer domain this install writes."""


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


# The pre-drain store check (store_volume_slow): one small create/write/remove
# in tart_home, run by a child (System.store_write) and abandoned after this.
STORE_WRITE_TIMEOUT_SECONDS = 10.0
STORE_WRITE_CHILD = """
import json, os, sys, tempfile, time
started = time.monotonic()
try:
    fd, name = tempfile.mkstemp(prefix=".tartci-probe-control.", dir=sys.argv[1])
    try:
        os.write(fd, b"tartci probe control\\n")
    finally:
        os.close(fd)
        os.unlink(name)
    print(json.dumps({"ms": (time.monotonic() - started) * 1000}))
except OSError as error:
    print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
"""


class System:
    """Every side effect. Tests replace it."""

    def run(self, argv: list[str], *, cwd: str | None = None,
            env: dict[str, str] | None = None, timeout: float = 900) -> Result:
        merged = {**os.environ, **(env or {})}
        if argv and argv[0] == "python3":
            # Helpers need tomllib (3.11+). This process was started by
            # tartci_toml_python, so its interpreter qualifies; a bare python3
            # on PATH is /usr/bin/python3 (3.9) under ssh and launchd. Under
            # launcher v2 that interpreter is the lane's shim (python_child).
            argv = [child_python(), *argv[1:]]
        try:
            proc = subprocess.run(argv, cwd=cwd, env=merged, capture_output=True,
                                  text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Result(127, "", f"{type(exc).__name__}: {exc}")
        return Result(proc.returncode, proc.stdout, proc.stderr)

    def store_write(self, directory: str, timeout: float = STORE_WRITE_TIMEOUT_SECONDS) -> dict:
        """Time one small create/write/remove in the Tart store, made by a
        child in its own session: {"ms"}, {"timed_out"} or {"error"}.

        Never from this process: a write into a wedged volume blocks in an
        uninterruptible wait, and a process with such a thread cannot exit, so
        self-update would never end. On a timeout the child's group is killed
        and abandoned (storage_health.run_probe_bounded does the same).
        """
        import storage_health  # noqa: PLC0415 - sibling module; owns the bounded reaping
        try:
            # -I: the child imports nothing from the environment, the user
            # site or the current directory.
            child = subprocess.Popen([child_python(), "-I", "-c", STORE_WRITE_CHILD, directory],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, start_new_session=True)
        except (OSError, RuntimeError) as exc:
            # No interpreter (child_python raises RuntimeError) or no process:
            # nothing was written, and the caller refuses on an error.
            return {"error": f"store write child could not start: {exc}"}
        try:
            out, _ = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            storage_health.reap_after_kill(child)
            if child.stdout is not None:
                child.stdout.close()
            return {"timed_out": True, "timeout": timeout}
        try:
            value = json.loads(out)
        except ValueError:
            return {"error": f"store write child exited {child.returncode} with no result"}
        return value if isinstance(value, dict) else {"error": "store write child: bad result"}

    def store_health(self, directory: str) -> tuple[str, str, str]:
        """(status, reason, checked_at) the storage-health gate last recorded
        for the Tart store: "healthy", "degraded", "quarantined" or "unknown".
        Reads only its state files under the home folder, never the volume."""
        import storage_health  # noqa: PLC0415 - sibling module; owns the state format
        target = Path(directory)
        state_path = storage_health.default_state(target)
        if not storage_health.state_path_is_safe(state_path, target, resolve_target=False):
            return "unknown", f"its state path {state_path} is unsafe", ""
        if Path(str(state_path) + ".quarantine").exists():
            return "quarantined", "a quarantine marker is present", ""
        try:
            state = storage_health.load_state(state_path, target)
        except Exception as exc:  # noqa: BLE001 - any unreadable state is "unknown"
            return "unknown", f"its state is unreadable ({exc})", ""
        if not state:
            return "unknown", f"no storage-health record at {state_path}", ""
        if state.get("state_unreadable"):
            return "unknown", "its state is unreadable", ""
        return (str(state.get("status")), str(state.get("reason") or ""),
                str(state.get("checked_at") or ""))

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
    fleet_source_repository: str | None = None
    fleet_source_canary: str | None = None
    # [tart]: upgrade to fleet/tart.toml's pin in the drained window. Off
    # unless set; the canary goes first, others after its soak.
    tart_upgrade: bool = False
    tart_canary: str | None = None
    tart_soak_seconds: int = tart_pin.DEFAULT_SOAK_SECONDS
    # [launcher] auto_apply: acknowledge a launcher change by itself when the
    # target is on main and keeps the installed Team ID. false is the owner's
    # override: every launcher change waits for launcher-change-ack again.
    launcher_auto_apply: bool = True

    @property
    def checkout(self) -> Path:
        return self.home / ".local" / "share" / "tartci" / "update-checkout"

    @property
    def fleet_source_store(self) -> Path:
        return self.home / ".local" / "share" / "tartci" / "fleet-sources"

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


def skew_stale_note(skew: dict | None, now: float | None = None) -> str | None:
    """STALE when the cached skew is older than the watchdog refreshes it."""
    if not skew:
        return None
    import state_age
    return state_age.stale_note(skew.get("measured_at"), SKEW_REFRESH_S,
                                "launchd watchdog (tartci launchd heal)", now)


def peer_reachability(home: Path | None = None) -> dict:
    """This host's record of unreachable peers, for status surfaces. Never raises.

    {"state": "ok" | "dark" | "unreadable", "peers": {peer: streak}}. A host
    that has never surveyed (or last found every peer readable) has no record.
    """
    path = state_dir_for(home or Path.home()) / PEER_UNREADABLE_NAME
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {"state": "ok", "peers": {}}
    except (OSError, json.JSONDecodeError) as exc:
        return {"state": "unreadable", "error": str(exc)[:200], "peers": {}}
    if not isinstance(value, dict):
        return {"state": "unreadable", "error": "not an object", "peers": {}}
    peers = {peer: row for peer, row in value.items() if isinstance(row, dict)}
    return {"state": "dark" if peers else "ok", "peers": peers}


def paused_line(state_dir: Path, now: float | None = None) -> str | None:
    """Why self-update has not run, when the launchd interval guard is holding
    it: during a launchd timer stall launchd starts nothing on its own and the
    guard deliberately does not kick self-update, so self-update writes no log
    line and no attempt of its own. Read-only; None when not paused or when the
    guard's receipt cannot be read."""
    try:
        import launchd_interval_guard as guard  # noqa: PLC0415 - sibling module
        directory = (Path(os.environ["TARTCI_INTERVAL_GUARD_DIR"])
                     if os.environ.get("TARTCI_INTERVAL_GUARD_DIR")
                     else state_dir.parent / "launchd-interval-guard")
        value = guard.status(directory, now=now)
    except Exception:  # noqa: BLE001 - a status surface never raises
        return None
    paused = [row for row in value.get("paused") or []
              if isinstance(row, dict) and "self-update" in str(row.get("label"))]
    if value.get("state") != "stalled" or not paused:
        return None
    started = (value.get("episode") or {}).get("started_ts")
    since = _iso(float(started)) if isinstance(started, (int, float)) else "an unknown time"
    return (f"self-update PAUSED by the launchd timer stall since {since}: launchd has not "
            "started it and the interval guard does not kick it during the stall, so it "
            "has written no attempt since; a reboot resumes it")


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
        off = f"self-update LEFT THIS HOST {str(last.get('pool_state') or 'off').upper()}"
        problem = f"{problem}; {off}" if problem else off
    if last and last.get("status") in ("failed", "rolled_back"):
        failed = (f"last self-update {last['status'].upper().replace('_', ' ')} for "
                  f"{str(last.get('target'))[:12]}: {last.get('error')}")
        problem = f"{problem}; {failed}" if problem else failed
    signing = signing_blocked(state)
    if signing:
        problem = f"{problem}; {signing}" if problem else signing
    waiting = waiting_line(state)
    if waiting and "STARVED" in waiting:
        problem = f"{problem}; {waiting}" if problem else waiting
    halted = halt_reason(state)
    if halted:
        problem = f"{problem}; {halted}" if problem else halted
    aged = skew_stale_note(skew)
    if aged:
        problem = f"{problem}; skew {aged}" if problem else f"skew {aged}"
    return {"skew": skew, "last": last, "lines": status_lines(state), "problem": problem,
            "paused": paused_line(state)}


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
        section = data.get("fleet_source")
        if section is not None:
            if not isinstance(section, dict):
                raise ConfigError(f"{path}: [fleet_source] must be a table, not {section!r}")
            for key, value in section.items():
                if key not in ("repository", "canary") or not isinstance(value, str) or not value.strip():
                    raise ConfigError(f"{path}: [fleet_source] {key} must be a non-empty string "
                                      "(keys: repository, canary)")
            cfg.fleet_source_repository = section.get("repository", "").strip() or None
            cfg.fleet_source_canary = section.get("canary", "").strip() or None
        tart = data.get("tart")
        if tart is not None:
            if not isinstance(tart, dict):
                raise ConfigError(f"{path}: [tart] must be a table, not {tart!r}")
            for key, value in tart.items():
                ok = ((key == "upgrade" and isinstance(value, bool))
                      or (key == "canary" and isinstance(value, str) and value.strip())
                      or (key == "soak_seconds" and isinstance(value, int)
                          and not isinstance(value, bool) and value >= 0))
                if not ok:
                    raise ConfigError(f"{path}: [tart] {key} = {value!r} is not valid (keys: "
                                      "upgrade = true|false, canary = \"<host id>\", "
                                      "soak_seconds = <int>)")
            cfg.tart_upgrade = tart.get("upgrade", False)
            cfg.tart_canary = str(tart.get("canary", "")).strip() or None
            cfg.tart_soak_seconds = tart.get("soak_seconds", tart_pin.DEFAULT_SOAK_SECONDS)
        launcher = data.get("launcher")
        if launcher is not None:
            if not isinstance(launcher, dict) or any(
                    key != "auto_apply" or not isinstance(value, bool)
                    for key, value in launcher.items()):
                raise ConfigError(f"{path}: [launcher] {launcher!r} is not valid (keys: "
                                  "auto_apply = true|false)")
            cfg.launcher_auto_apply = launcher.get("auto_apply", True)
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


def launcher_pointer_path(cfg: Config) -> Path:
    return cfg.home / ".config" / "tartci" / "launcher-generation.json"


def _helper_app(cfg: Config) -> Path | None:
    helper = launch_helper(cfg)
    return Path(helper["path"]) if helper is not None and helper.get("path") else None


def installed_layout(cfg: Config) -> str | None:
    """The launcher layout this host runs: "v1", "v2", or None. The receipt
    decides; without a launch helper record, v2 evidence on disk does."""
    import launcher_layout  # noqa: PLC0415 - sibling module; owns the layout rules
    return launcher_layout.installed(_read_json(cfg.install_receipt), cfg.home, _helper_app(cfg))


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
    if helper is not None and installed_layout(cfg) == "v2":
        # A v2 app holds no commit: lanes run the generation its pointer names.
        pointer = launcher_pointer_path(cfg)
        generation = (_read_json(pointer) or {}).get("generation")
        commit = None
        if isinstance(generation, str) and generation:
            manifest = Path(generation) / ".tartci-support-manifest.json"
            commit = (_read_json(manifest) or {}).get("source_commit")
        source = f"launcher v2 pointer {pointer}"
    elif helper is not None:
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
        repository = configured_repository(cfg, sys_)
        result = sys_.run(["git", "clone", "--quiet", "--no-checkout",
                           repository_url(repository), str(staging / "clone")])
        if result.rc != 0 or not (staging / "clone" / ".git").exists():
            shutil.rmtree(staging, ignore_errors=True)
            raise Refused(f"cannot create the update checkout: {result.text}")
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)  # a leftover without .git
        os.rename(staging / "clone", path)
        shutil.rmtree(staging, ignore_errors=True)
    origin = sys_.run(["git", "-C", str(path), "remote", "get-url", "origin"])
    remote = origin.out.strip().rstrip("/").removesuffix(".git").lower()
    accepted_remotes = {repository_url(value).removesuffix(".git").lower()
                        for value in ACCEPTED_REPOSITORIES}
    if origin.rc != 0 or remote not in accepted_remotes:
        raise Refused(f"update checkout {path} is not an accepted TartCI clone")
    fetched = sys_.run(["git", "-C", str(path), "fetch", "--quiet", "--prune", "origin", "main"])
    if fetched.rc != 0:
        raise Refused(f"git fetch failed: {fetched.text}")


FLEET_SOURCE_ENV = ("TARTCI_FLEET_SOURCE_ROOT", "TARTCI_FLEET_SOURCE_COMMIT",
                    "TARTCI_FLEET_SOURCE_REPOSITORY")


def installed_fleet_source_commit(cfg: Config) -> str | None:
    """The private source commit the installed generation was built from."""
    recorded = (_read_json(cfg.install_receipt) or {}).get("fleet_source")
    if isinstance(recorded, dict) and recorded.get("configured"):
        commit = recorded.get("commit")
        return commit if isinstance(commit, str) else None
    return None


def installed_fleet_source_record(cfg: Config) -> dict[str, Any] | None:
    """The complete private-source record for the installed generation."""
    recorded = (_read_json(cfg.install_receipt) or {}).get("fleet_source")
    if isinstance(recorded, dict) and recorded.get("configured") is True:
        if all(isinstance(recorded.get(key), str) and recorded[key].strip()
               for key in ("repository", "commit", "root")):
            return {key: recorded[key] for key in ("repository", "commit", "root")}
    return None


def fleet_source_receipt_is_configured(cfg: Config) -> bool:
    recorded = (_read_json(cfg.install_receipt) or {}).get("fleet_source")
    return (isinstance(recorded, dict) and "configured" in recorded
            and recorded.get("configured") is not False)


def fleet_source_env(record: dict[str, Any] | None) -> dict[str, str]:
    """Return the source environment represented by an install receipt."""
    if record is None:
        return {}
    return {
        "TARTCI_FLEET_SOURCE_ROOT": record["root"],
        "TARTCI_FLEET_SOURCE_COMMIT": record["commit"],
        "TARTCI_FLEET_SOURCE_REPOSITORY": record["repository"],
    }


def fleet_source_env_snapshot() -> dict[str, str | None]:
    return {name: os.environ.get(name) for name in FLEET_SOURCE_ENV}


def restore_fleet_source_env(snapshot: dict[str, str | None]) -> None:
    for name, value in snapshot.items():
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)


def _source_name(commit: str | None) -> str:
    return commit[:12] if commit else "the public profiles"


# A private-source commit the canary has installed and verified is adopted by
# the other hosts only after it has served this long on the canary.
FLEET_SOURCE_SOAK_SECONDS = 30 * 60
FLEET_SOURCE_VERIFIED = "fleet-source-verified.json"


def read_canary_verified(cfg: Config, sys_: System, canary: str) -> dict | None:
    """The canary's last verified source commit, aged on the canary's clock.

    {"repository", "commit", "age"} or None when it is unreadable or absent.
    """
    if canary == self_host_id(cfg):
        value = _read_json(cfg.state_dir / FLEET_SOURCE_VERIFIED)
        clock = sys_.now()
    else:
        target = published_peers(cfg, sys_).get(canary)
        if not target:
            return None
        ssh = peer_ssh(target)
        if ssh is None:
            print(f"self-update: {refused_destination(canary, target)}", file=sys.stderr)
            return None
        result = sys_.run([*ssh,
                           'date +%s; cat "${TARTCI_HOME:-$HOME/.tartci}/state/self-update/'
                           + FLEET_SOURCE_VERIFIED + '" 2>/dev/null || true'], timeout=60)
        first, _, rest = result.out.partition("\n")
        if result.rc != 0 or not first.strip().isdigit():
            return None
        clock = float(first.strip())
        try:
            value = json.loads(rest) if rest.strip() else None
        except json.JSONDecodeError:
            value = None
    if (not isinstance(value, dict) or not isinstance(value.get("verified_at"), (int, float))
            or not isinstance(value.get("commit"), str)
            or not fleet_source.COMMIT.fullmatch(value["commit"])):
        return None
    return {"repository": value.get("repository"), "commit": value["commit"],
            "age": clock - float(value["verified_at"])}


def publish_fleet_source_verified(cfg: Config, sys_: System, selected: dict | None) -> None:
    """On the canary, after an install from `selected` verified: tell the fleet."""
    if not selected or cfg.fleet_source_canary is None:
        return
    if self_host_id(cfg) != cfg.fleet_source_canary:
        return
    if installed_fleet_source_commit(cfg) != selected["commit"]:
        return
    _write_json(cfg.state_dir / FLEET_SOURCE_VERIFIED,
                {"repository": selected["repository"], "commit": selected["commit"],
                 "verified_at": sys_.now()})


def sync_fleet_source(cfg: Config, sys_: System, *, write: bool = True) -> dict | None:
    """Select the configured private source's commit for this run.

    Without `[fleet_source] repository` in self-update.toml this does nothing
    and the public checkout stays the source. A move is one host first: the
    canary (`[fleet_source] canary`) adopts main's head; every other host
    adopts only the commit the canary has installed and verified, once it has
    served FLEET_SOURCE_SOAK_SECONDS there, and otherwise stays on the commit
    it was installed from (or on the public profiles). An unreadable or
    damaged source refuses; it never falls back to the public profiles. The
    selection goes into this process's environment, so every helper and the
    installer read the same commit. With `write` false (a plan) nothing is
    exported or pruned.
    """
    # self-update.toml alone selects the source. A source inherited from the
    # caller's shell or the agent's plist would otherwise survive a removed
    # [fleet_source] and reinstall private profiles while going "public".
    for name in FLEET_SOURCE_ENV:
        os.environ.pop(name, None)
    repository = cfg.fleet_source_repository
    if repository is None:
        return None
    if launch_helper(cfg) is not None and installed_layout(cfg) == "v2":
        # Launcher v2: the app holds only host policy, so a source move is a
        # refresh when the app's inputs are unchanged and an acknowledged
        # launcher change when they are not (the turn measures both). The
        # installer's layout rules still apply first.
        planned_layout(cfg)
    elif launch_helper(cfg) is not None:
        # A v1 sealed launcher seals the profile: a source move is a
        # same-generation refresh, which keeps the live launcher; the
        # installer then verifies it against the moved profile's policy
        # digest, fails after the drain, and rolls back, every turn. Refuse
        # before anything is read or drained.
        raise Refused(
            "fleet source refused on a sealed-launcher host: a [fleet_source] move changes the "
            "profile the sealed TartCI Launcher's policy digest covers, and a source move does "
            "not rebuild the launcher, so the install would fail after the drain and roll back "
            "every turn. Fix: remove [fleet_source] from self-update.toml on this host, or move "
            "it to launcher v2 (~/.config/tartci/launcher-v2.enabled, then an acknowledged "
            "install), where a source move rebuilds the launcher only when its inputs change. "
            "Verify: `tartci fleet-macos self-update --plan` no longer reports this refusal")
    if cfg.fleet_source_canary is None:
        raise Refused("fleet source has no canary: set [fleet_source] canary to the host that "
                      "installs a new source commit first")

    def api(path: str) -> tuple[int, str, str]:
        result = sys_.run([gh_cli(), "api", path], cwd=str(cfg.home),
                          env={"GH_REPO": repository}, timeout=60)
        return result.rc, result.out, result.err

    installed = installed_fleet_source_commit(cfg)
    me = self_host_id(cfg)
    if me != cfg.fleet_source_canary:
        # A mistyped canary would leave every host on its installed commit
        # forever with nothing saying why: refuse instead.
        published = set(published_peers(cfg, sys_)) | {me}
        if cfg.fleet_source_canary not in published:
            raise Refused(f"[fleet_source] canary {cfg.fleet_source_canary!r} is not a published "
                          f"host id ({', '.join(sorted(published))}); fix self-update.toml")
    try:
        if me == cfg.fleet_source_canary:
            commit, tree = fleet_source_sync.head(api, repository)
            why = "canary: main's head"
        else:
            verified = read_canary_verified(cfg, sys_, cfg.fleet_source_canary)
            if (verified and verified["repository"] == repository
                    and verified["age"] >= FLEET_SOURCE_SOAK_SECONDS):
                commit, tree = fleet_source_sync.head(api, repository, verified["commit"])
                why = (f"verified on canary {cfg.fleet_source_canary} "
                       f"{int(verified['age'] // 60)} min ago")
            elif installed:
                commit, tree = fleet_source_sync.head(api, repository, installed)
                why = (f"staying on the installed commit: canary {cfg.fleet_source_canary} "
                       + ("has verified nothing newer" if not verified else
                          f"verified {verified['commit'][:12]} only "
                          f"{int(verified['age'] // 60)} min ago"))
            else:
                print(f"self-update: fleet source {repository}: staying on the public "
                      f"profiles until canary {cfg.fleet_source_canary} verifies a commit")
                return None
        if write:
            root = fleet_source_sync.export(api, repository, commit, tree, cfg.fleet_source_store)
        else:
            root = cfg.fleet_source_store / commit
            if fleet_source.marker_problem(root, repository, commit) is not None:
                raise Refused(f"fleet source {repository}@{commit[:12]} is not exported yet; "
                              "a plan writes nothing, an apply exports it")
    except (fleet_source_sync.SyncError, OSError) as exc:
        raise Refused(f"fleet source {repository}: {exc}") from exc
    os.environ.update(dict(zip(FLEET_SOURCE_ENV, (str(root), commit, repository))))
    try:
        resolved_source = fleet_source.resolve(cfg.checkout)
    except ValueError as exc:
        raise Refused(f"fleet source {repository}@{commit[:12]}: {exc}") from exc
    if write:
        keep = {commit, installed or ""}
        fleet_source_sync.prune(cfg.fleet_source_store, keep, now=sys_.now())
    return {"repository": repository, "commit": commit, "root": str(root), "why": why,
            **resolved_source.provenance}


# A read that failed in transit (a timeout, a connection error, a 5xx) is
# retried once after this pause; a definite answer (404, 403) is not. m1 on
# 2026-10-16 refused a self-update because both reads hung for 60 s while
# every other ghapp call on the host was also timing out.
REPOSITORY_READ_RETRY_SECONDS = 20
# gh's own transport errors read "error connecting to api.github.com" and
# "dial tcp: lookup api.github.com: no such host". Only a failed read (rc != 0)
# is matched: a successful read's JSON can contain any of these words.
_TRANSIENT_READ = re.compile(r"(?i)TimeoutExpired|timed out|connection (?:reset|refused)|"
                             r"error connecting to|no such host|dial tcp|i/o timeout|"
                             r"could not resolve|network|HTTP 5\d\d|\b50[0-4]\b|EOF")


def _read_repository(cfg: Config, sys_: System, repository: str) -> tuple[str | None, str]:
    """(canonical full_name or None, why it was not read)."""
    for attempt in (1, 2):
        result = sys_.run([gh_cli(), "api", f"repos/{repository}"],
                          cwd=str(cfg.checkout), env={"GH_REPO": repository}, timeout=60)
        try:
            canonical = json.loads(result.out).get("full_name")
        except (json.JSONDecodeError, AttributeError):
            canonical = None
        if result.rc == 0 and canonical in ACCEPTED_REPOSITORIES:
            return canonical, ""
        why = (f"exit {result.rc}: {result.text.strip()[:160]}" if result.rc != 0
               else f"answered {canonical or 'no full_name'}")
        if attempt == 1 and result.rc != 0 and _TRANSIENT_READ.search(result.text or ""):
            sys_.sleep(REPOSITORY_READ_RETRY_SECONDS)
            continue
        return None, why + (" (after one retry)" if attempt == 2 else "")
    return None, "unreachable"


def configured_repository(cfg: Config, sys_: System) -> str:
    """Resolve an explicit binding or the first readable accepted slug."""
    bound = os.environ.get("GH_REPO", "").strip()
    candidates = (bound,) if bound else (LEGACY_REPOSITORY, DEFAULT_REPOSITORY)
    reasons = []
    for repository in candidates:
        if repository not in ACCEPTED_REPOSITORIES:
            raise Refused(f"unsupported TartCI repository identity: {repository}")
        canonical, why = _read_repository(cfg, sys_, repository)
        if canonical:
            return canonical
        reasons.append(f"{repository}: {why}")
        if bound:
            break
    raise Refused("no accepted TartCI repository is readable: " + "; ".join(reasons))


def repository_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"


def gh_cli() -> str:
    return os.environ.get("TARTCI_GH_CLI") or ("ghapp" if shutil.which("ghapp") else "gh")


def canonical_repository(cfg: Config, sys_: System) -> str:
    """Resolve and validate the API's canonical full_name for a commit."""
    return configured_repository(cfg, sys_)


# Workflow events that run the default branch's definition to judge a pull
# request. Their check runs land on whatever commit main's tip was at the
# time, but they say nothing about that commit: the review-verdict job, run
# for every pull-request comment, sat in_progress on main's tip and held a
# host's self-update as "not green" (m5s, 2026-10-22).
PULL_REQUEST_EVENTS = frozenset({
    "issue_comment", "pull_request_review", "pull_request_review_comment",
    "pull_request_target", "workflow_run",
})


def pull_request_check_suites(cfg: Config, sys_: System, repository: str,
                              binding: dict[str, str], sha: str) -> set[int]:
    """The check suites on ``sha`` whose workflow ran for a pull-request event.
    An unreadable answer names none, so every check run still counts."""
    result = sys_.run([gh_cli(), "api",
                       f"repos/{repository}/actions/runs?head_sha={sha}&per_page=100"],
                      cwd=str(cfg.checkout), env=binding, timeout=60)
    try:
        runs = json.loads(result.out).get("workflow_runs")
    except (json.JSONDecodeError, AttributeError):
        return set()
    if not isinstance(runs, list):
        return set()
    return {run["check_suite_id"] for run in runs
            if isinstance(run, dict) and run.get("event") in PULL_REQUEST_EVENTS
            and isinstance(run.get("check_suite_id"), int)}


def checks_green(cfg: Config, sys_: System, sha: str) -> tuple[bool, str]:
    """Every check run on main's commit completed successfully (and there is one).

    Age alone is not evidence: a commit that broke main's CI soaks like any
    other. An unreadable answer is not green. Check runs a workflow left
    there while judging a pull request (PULL_REQUEST_EVENTS) are not the
    commit's checks and are skipped.
    """
    repository = canonical_repository(cfg, sys_)
    binding = {"GH_REPO": repository, "SHIPYARD_GHAPP_REPO": repository,
               "SHIPYARD_GH_APP_REPO": repository}
    result = sys_.run([gh_cli(), "api", f"repos/{repository}/commits/{sha}/check-runs?per_page=100"],
                      cwd=str(cfg.checkout), env=binding, timeout=60)
    try:
        runs = json.loads(result.out).get("check_runs")
    except (json.JSONDecodeError, AttributeError):
        runs = None
    if not isinstance(runs, list):
        return False, f"check runs unreadable (exit {result.rc}): {result.text[:160]}"
    foreign = pull_request_check_suites(cfg, sys_, repository, binding, sha) if runs else set()
    runs = [run for run in runs
            if not (isinstance(run, dict)
                    and (run.get("check_suite") or {}).get("id") in foreign)]
    if not runs:
        return False, "no check runs recorded"
    bad = [f"{run.get('name')}={run.get('conclusion') or run.get('status')}" for run in runs
           if run.get("status") != "completed"
           or run.get("conclusion") not in ("success", "skipped", "neutral")]
    return (not bad), ("all checks green" if not bad else "not green: " + ", ".join(bad))


def measure_skew(cfg: Config, sys_: System, installed: str, now: float,
                 target_ref: str = "origin/main", verify_checks: bool = True,
                 explicit: bool | None = None) -> dict:
    """First-parent commits on main the host does not run, and the target.

    The target is the newest first-parent main commit that is past the soak
    AND whose check runs are green. An explicit --target must itself be on
    main's first-parent chain and meet both. If the recorded install is not
    in the checkout, an explicit target may still recover the host, but skew
    counts remain unknown rather than pretending the whole main history is
    undeployed.
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
    if explicit is None:
        explicit = target_ref != "origin/main"
    skew.update(explicit=explicit, requested_target=head_sha if explicit else None,
                selected_target=None)
    if explicit:
        chain = sys_.run([*git, "rev-list", "--first-parent", skew["main"]])
        if head_sha not in chain.out.split():
            skew.update(state="unknown", selected_target=None, selection_reason="not on first-parent chain",
                        reason=f"--target {target_ref} is not on origin/main's first-parent chain")
            return skew
    installed_known = sys_.run([*git, "cat-file", "-e", installed + "^{commit}"]).rc == 0
    if not installed_known and not explicit:
        skew["reason"] = f"installed commit {installed[:12]} is not in the tartci history"
        return skew
    if installed_known and sys_.run(
        [*git, "merge-base", "--is-ancestor", installed, skew["main"]]
    ).rc != 0:
        skew.update(state="diverged",
                    reason=f"installed {installed[:12]} is not an ancestor of origin/main")
        return skew
    log_ref = f"{installed}..{skew['main']}" if installed_known else skew["main"]
    log = sys_.run([*git, "log", "--first-parent", "--format=%H %ct", log_ref])
    if log.rc != 0:
        skew["reason"] = f"git log failed: {log.text}"
        return skew
    commits = []
    for line in log.out.splitlines():
        parts = line.split()
        if len(parts) == 2 and SHA.fullmatch(parts[0]) and parts[1].isdigit():
            commits.append((parts[0], int(parts[1])))
    skew["behind"] = None if not installed_known else len(commits)
    if not installed_known:
        skew["reason"] = f"installed commit {installed[:12]} is not in the tartci history"
    if not commits:
        skew["state"] = "unknown" if not installed_known else "current"
        return skew
    if installed_known:
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
        if explicit:
            soak_until = min(ts for _, ts in commits) + cfg.soak_seconds
            skew["soak_until"] = _iso(soak_until)
            skew["selection_reason"] = f"soaking until {skew['soak_until']}"
        return skew
    if not verify_checks:
        skew.update(state="behind", target=None, selected_target=None, selection_reason="same")
        return skew
    rejected = []
    for sha, _ in soaked[:CHECK_CANDIDATES]:
        green, detail = checks_green(cfg, sys_, sha)
        if green:
            skew.update(state="behind", target=sha, selected_target=sha,
                        selection_reason="same" if explicit else None)
            if rejected:
                skew["skipped"] = rejected
            return skew
        rejected.append(f"{sha[:12]}: {detail}")
    skew.update(state="unverified", reason="; ".join(rejected))
    if explicit:
        detail = rejected[0].split(": ", 1)[1]
        if detail.startswith("not green: "):
            detail = detail[len("not green: "):]
        skew["selection_reason"] = "not green: " + detail
    return skew


def render_skew(skew: dict | None) -> str:
    if not skew:
        return "tartci: skew UNKNOWN (never measured; run tartci fleet-macos self-update --plan)"
    state = skew.get("state")
    if state == "current":
        return f"tartci: current with main (measured {skew.get('measured_at')})"
    if state == "not_applicable":
        return f"tartci: skew n/a ({skew.get('reason')})"
    if state in ("behind", "soaking", "unverified") and skew.get("behind") is None:
        requested = skew.get("requested_target")
        selection = (f"; selected {skew.get('selected_target') or 'none'} (requested {requested}; "
                     f"{skew.get('selection_reason', 'same')})" if requested else "")
        return f"tartci: skew UNKNOWN ({skew.get('reason') or 'unknown installed commit'})" + selection
    if state in ("behind", "soaking", "unverified"):
        flag = " STALE" if skew.get("stale") else ""
        requested = skew.get("requested_target")
        selection = (f"; selected {skew.get('selected_target') or 'none'} (requested {requested}; "
                     f"{skew.get('selection_reason', 'same')})" if requested else "")
        return (f"tartci: {skew['behind']} commits behind main (oldest undeployed: "
                f"{skew['oldest_undeployed']}){flag}"
                + {"behind": "", "soaking": " [all still soaking]",
                   "unverified": f" [no soaked commit has green checks: {skew.get('reason')}]"}[state]
                + selection + f" (measured {skew.get('measured_at')})")
    return f"tartci: skew {str(state).upper()} ({skew.get('reason') or 'no reason'})"


# ── one host at a time ─────────────────────────────────────────────────────

def published_supply(cfg: Config, sys_: System) -> dict[str, Any]:
    """The target's published supply, from the selected fleet source."""
    source = fleet_source.resolve(cfg.checkout)
    if source.configured:
        try:
            value = json.loads(source.published_supply.read_text())
        except (OSError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict):
            raise Refused(f"published supply {source.published_supply} is unreadable")
        return value
    shown = sys_.run(["git", "-C", str(cfg.checkout), "show",
                      "origin/main:fleet/advertised-labels.json"])
    try:
        value = json.loads(shown.out) if shown.rc == 0 else None
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, dict):
        raise Refused("published supply origin/main:fleet/advertised-labels.json is unreadable")
    return value


def published_peers(cfg: Config, sys_: System) -> dict[str, str]:
    """host_id -> SSH target for every host in the CURRENT published supply.

    A profile's `host.ssh` is published there; without it the alias
    convention tartci-<host_id> applies; [peers] only overrides. Adding a
    machine therefore needs its profile and nothing on other hosts.
    Targets are returned as published; every SSH caller validates them
    through peer_ssh before use.
    """
    value = published_supply(cfg, sys_)
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
WAITING_SEPARATOR = "--- waiting ---"
UNREADABLE_SEPARATOR = "--- unreadable ---"
POOL_SINCE_SEPARATOR = "--- pool-state-since ---"
PAUSED_SEPARATOR = "--- self-update-paused ---"
# halt_reason, run by the PEER's own /usr/bin/python3 (any 3.x, no imports
# from tartci) so a peer of any version reports whether its self-update is
# halted. test_fleet_self_update checks it agrees with halt_reason.
_PEER_HALTED = (
    "import json, os, glob, datetime as d\n"
    "s = (os.environ.get(\"TARTCI_HOME\") or os.path.expanduser(\"~/.tartci\")) + \"/state/self-update\"\n"
    "def t(x):\n"
    "    try: return d.datetime.strptime(str(x), \"%Y-%m-%dT%H:%M:%SZ\").replace(tzinfo=d.timezone.utc).timestamp()\n"
    "    except ValueError: return 0.0\n"
    "def r(p):\n"
    "    try: v = json.load(open(p))\n"
    "    except Exception: return None\n"
    "    return v if isinstance(v, dict) else None\n"
    "c = t((r(s + \"/halt-cleared.json\") or {}).get(\"at\")); n = 0\n"
    "for p in sorted(glob.glob(s + \"/attempts/*.json\")):\n"
    "    v = r(p)\n"
    f"    if not v or v.get(\"mode\") != \"apply\" or v.get(\"status\") not in {json.dumps(list(TERMINAL_STATUSES))}:\n"
    "        continue\n"
    "    if t(v.get(\"started_at\")) < c: continue\n"
    "    n = 0 if v.get(\"status\") == \"succeeded\" else n + 1\n"
    f"print(\"halted\" if n >= {MAX_CONSECUTIVE_FAILURES} else \"\")\n")
_PEER_MARKER = ('date +%s; cat "${TARTCI_HOME:-$HOME/.tartci}/state/self-update/active.json" '
                '2>/dev/null || true; echo; echo "' + WAITING_SEPARATOR + '"; '
                'cat "${TARTCI_HOME:-$HOME/.tartci}/state/self-update/waiting.json" 2>/dev/null || true; '
                'echo; echo "' + UNREADABLE_SEPARATOR + '"; '
                'cat "${TARTCI_HOME:-$HOME/.tartci}/state/self-update/' + PEER_UNREADABLE_NAME
                + '" 2>/dev/null || true; echo; echo "' + POOL_SINCE_SEPARATOR + '"; '
                'stat -f %m "${TARTCI_POOL_STATE_FILE:-$HOME/.config/tartci/pool-state}" '
                '2>/dev/null || true; echo; echo "' + PAUSED_SEPARATOR + '"; '
                'test -s "${TARTCI_HOME:-$HOME/.tartci}/state/self-update/scheduled-pause.json" '
                '&& echo paused || true; /usr/bin/python3 -c \'' + _PEER_HALTED + '\' 2>/dev/null || true')


def _ssh_failure_kind(result: Result) -> str:
    """Classify a failed peer SSH probe for an actionable operator receipt."""
    text = f"{result.text}\n{result.out}".lower()
    if any(token in text for token in (
            "permission denied", "publickey", "authentication failed",
            "host key verification failed", "no supported authentication methods")):
        return "authentication"
    if any(token in text for token in (
            "timed out", "timeout", "connection refused", "connection reset",
            "network is unreachable", "no route to host", "could not resolve hostname",
            "ssh: unreachable")):
        return "transport"
    return "command"


def read_peer(cfg: Config, sys_: System, host_id: str, target: str) -> dict[str, Any]:
    """One peer's pool state, update marker and waiting ticket.

    {"busy": bool, "evidence": str, "active_age": seconds | None,
     "since": epoch | None, "off": bool, "readable": bool, "clock": epoch | None,
     "unreadable": {host: streak} | None}. Marker ages are computed on the
    PEER's clock, so host clock skew cannot make a live marker look stale.
    `since` is when the peer's live waiting ticket joined the queue. Unreachable
    or unreadable peers are busy: fail closed. `readable` is whether its pool
    status parsed; `unreadable` is the peer's own record of which hosts it
    could not read, for corroboration.

    A peer that is OFF with no live update marker is not busy: it is out of
    service on purpose (m5studio, off for a store move on 2026-10-01, blocked
    every other host's update for 11 h). It is marked `off`, and whether this
    host may still go out is the capacity floor's call, which counts an off
    host as serving nothing. So is a DRAINING peer with no live update marker
    whose self-update is paused or halted, or whose markerless drain is older
    than PEER_DRAIN_GRACE_SECONDS (an operator drain: a containment drain on
    2026-10-16, held every other host's turn for 3 h). Updating, unreadable
    and freshly draining peers still refuse.
    """
    out: dict[str, Any] = {"busy": True, "evidence": "", "active_age": None, "since": None,
                           "off": False, "readable": False, "clock": None, "unreadable": None}
    ssh = peer_ssh(target)
    if ssh is None:
        out["evidence"] = refused_destination(host_id, target)
        return out
    status = sys_.run([*ssh, "cd ~ && ~/.local/bin/tartci pool status --json"], timeout=60)
    try:
        value = json.loads(status.out)
    except json.JSONDecodeError:
        kind = _ssh_failure_kind(status)
        label = {
            "authentication": "SSH authentication failed",
            "transport": "SSH transport failed",
            "command": "pool status unreadable",
        }[kind]
        out["evidence"] = (f"peer {host_id} ({target}) {label} (exit {status.rc}): "
                           f"{status.text[:160]}")
        return out
    out["readable"] = True
    marker = sys_.run([*ssh, _PEER_MARKER], timeout=60)
    first, _, rest = marker.out.partition("\n")
    clock = int(first.strip()) if marker.rc == 0 and first.strip().isdigit() else None
    out["clock"] = clock
    rest, _, pool_since_text = rest.partition(POOL_SINCE_SEPARATOR)
    pool_since_text, _, paused_text = pool_since_text.partition(PAUSED_SEPARATOR)
    held = set(paused_text.split())   # "paused" and/or "halted"
    active_text, _, waiting_text = rest.partition(WAITING_SEPARATOR)
    waiting_text, _, unreadable_text = waiting_text.partition(UNREADABLE_SEPARATOR)
    pool_since = (int(pool_since_text.strip())
                  if clock is not None and pool_since_text.strip().isdigit() else None)
    if clock is not None and unreadable_text.strip():
        try:
            report = json.loads(unreadable_text)
        except json.JSONDecodeError:
            report = None
        out["unreadable"] = report if isinstance(report, dict) else None
    if clock is not None and waiting_text.strip():
        try:
            ticket = json.loads(waiting_text)
        except json.JSONDecodeError:
            ticket = None
        # A peer that stopped refreshing its ticket has stopped trying.
        if (isinstance(ticket, dict) and isinstance(ticket.get("since"), (int, float))
                and clock - float(ticket.get("ts", 0)) < WAITING_TICKET_TTL):
            out["since"] = float(ticket["since"])
    is_off = value.get("state") == "off"
    serving = value.get("state") == "on" and value.get("participating") is True
    not_serving = (f"peer {host_id} is {value.get('state')} "
                   f"(participating={value.get('participating')})")
    if clock is None:
        out["evidence"] = (not_serving if not (is_off or serving)
                           else f"peer {host_id} self-update marker unreadable (exit {marker.rc})")
        return out
    # The update marker is read BEFORE the pool state. A self-update announces,
    # then drains, so a peer mid-update reads as draining; checked first, the
    # pool state hid that it was the turn holder and how long it had held the
    # turn (m5 on 2026-10-09 read "peer m5studio is draining" for an update
    # that was 10 minutes in).
    active = None
    try:
        active = json.loads(active_text) if active_text.strip() else None
    except json.JSONDecodeError:
        out["evidence"] = (not_serving if not (is_off or serving)
                           else f"peer {host_id} self-update marker unreadable")
        return out
    if isinstance(active, dict) and clock - float(active.get("ts", 0)) < ACTIVE_MARKER_TTL:
        age = max(0.0, clock - float(active.get("ts", 0)))
        out.update(evidence=(f"peer {host_id} is self-updating to "
                             f"{str(active.get('target'))[:12]} ({int(age // 60)} min in)"),
                   active_age=age)
        return out
    if value.get("state") == "draining" and pool_since is not None \
            and clock - pool_since >= PEER_DRAIN_STALE_SECONDS:
        out.update(busy=False, off=True,
                   evidence=(f"peer {host_id} has been draining for "
                             f"{(clock - pool_since) / 3600:.1f} h with no update marker "
                             "(left to the capacity floor)"))
        return out
    # An operator's drain: no update marker at all, so no self-update is in
    # it. A self-update writes its marker BEFORE it drains, and a dead one's
    # marker keeps it busy above until the marker's TTL; the grace only
    # covers a marker written between this read's marker and pool reads.
    # A peer whose own self-update the operator paused cannot be in an
    # update without a live marker either. Both are left to the capacity
    # floor, like an off peer.
    operator_drain = (active is None and pool_since is not None
                      and clock - pool_since >= PEER_DRAIN_GRACE_SECONDS)
    stopped = sorted(held & {"paused", "halted"})
    if value.get("state") == "draining" and (stopped or operator_drain):
        why = (f"its self-update is {' and '.join(stopped)}, so no update is in this drain"
               if stopped else
               f"an operator drain: no update marker for more than "
               f"{PEER_DRAIN_GRACE_SECONDS // 60} min")
        age = (f" for {int((clock - pool_since) // 60)} min" if pool_since is not None else "")
        out.update(busy=False, off=True,
                   evidence=f"peer {host_id} is draining{age}, {why} (left to the capacity floor)")
        return out
    if not is_off and not serving:
        since = (f", pool state unchanged for {int((clock - pool_since) // 60)} min"
                 if pool_since is not None else "")
        out["evidence"] = not_serving + since
        return out
    if is_off:
        out.update(busy=False, off=True,
                   evidence=f"peer {host_id} is off and not updating (left to the capacity floor)")
        return out
    out.update(busy=False, evidence=f"peer {host_id} on, not updating")
    return out


def peer_state(cfg: Config, sys_: System, host_id: str, target: str) -> tuple[bool, str]:
    """(busy, evidence). Unreachable or unreadable peers are busy: fail closed."""
    peer = read_peer(cfg, sys_, host_id, target)
    return peer["busy"], peer["evidence"]


def check_peers(cfg: Config, sys_: System, me: str) -> list[str]:
    return survey_peers(cfg, sys_, me).busy


@dataclasses.dataclass
class Survey:
    busy: list[str]                 # why each blocking peer blocks
    waiting: dict[str, float]       # peer -> when its ticket joined the queue
    off: list[str]                  # off and not updating: left to the capacity floor
    excluded: list[str]             # unreachable long enough to stop holding the turn
    notes: list[str]                # why each excluded peer is excluded


def peer_streaks(cfg: Config) -> dict[str, dict]:
    """This host's record of peers whose pool status it could not read."""
    value = _read_json(cfg.state_dir / PEER_UNREADABLE_NAME) or {}
    return {peer: row for peer, row in value.items() if isinstance(row, dict)
            and isinstance(row.get("since"), (int, float)) and isinstance(row.get("reads"), int)}


def survey_peers(cfg: Config, sys_: System, me: str, *, record: bool = False) -> Survey:
    """Read every other published host once.

    An off peer neither blocks nor holds a place in the queue: it cannot take
    a turn while it is off, and yielding to it would stall the fleet the same
    way refusing on it did.

    An unreachable peer blocks, except that one dark long enough stops holding
    the turn (`excluded`), when all of these hold, so two hosts never go out
    at once:
    - this host's own streak: PEER_UNREACHABLE_MIN_READS consecutive
      unreadable reads over at least PEER_UNREACHABLE_EXCLUDE_SECONDS; any
      readable read, whatever it shows, ends the streak;
    - quorum: this host reads more than half the published fleet, counting
      itself, so a host cut off from the rest excludes nobody and the two
      halves of an even split cannot both proceed;
    - unanimity: at least one peer is readable, and every readable peer's own
      read of X in the last PEER_UNREACHABLE_FRESH_SECONDS failed too. One
      peer that can still reach X means X is alive and may be updating.
    A host that flaps readable/unreadable never qualifies and keeps blocking;
    STARVED_AFTER_SECONDS reports that. `record` persists the streaks and
    emits the transition events; a plan reads them without writing.
    """
    import host_off  # noqa: PLC0415 - sibling module; owns the event log format
    now = sys_.now()
    infos = {peer: read_peer(cfg, sys_, peer, target)
             for peer, target in published_peers(cfg, sys_).items() if peer != me}
    prior = peer_streaks(cfg)
    streaks: dict[str, dict] = {}
    for peer, info in infos.items():
        was = prior.get(peer)
        if info["readable"]:
            if record and was and was.get("excluded"):
                host_off.event(cfg.state_dir, "peer_unreachable_rejoined",
                               f"peer {peer} is readable again after {int(now - was['since'])}s dark",
                               {"peer": peer, "dark_for_s": int(now - was["since"])}, now=now)
            continue
        streaks[peer] = {"since": was["since"] if was else now,
                         "reads": (was["reads"] if was else 0) + 1, "last": now,
                         "excluded": bool(was and was.get("excluded"))}
    readable = sorted(peer for peer, info in infos.items() if info["readable"])
    fleet = len(infos) + 1
    quorum = 1 + len(readable) > fleet / 2
    excluded: list[str] = []
    notes: list[str] = []
    for peer, streak in sorted(streaks.items()):
        dark = now - streak["since"]
        corroborated_by = []
        for other in readable:
            report = (infos[other]["unreadable"] or {}).get(peer)
            clock = infos[other]["clock"]
            last = report.get("last") if isinstance(report, dict) else None
            if (clock is not None and isinstance(last, (int, float))
                    and 0 <= clock - last <= PEER_UNREACHABLE_FRESH_SECONDS):
                corroborated_by.append(other)
        unanimous = bool(readable) and corroborated_by == readable
        qualifies = (streak["reads"] >= PEER_UNREACHABLE_MIN_READS
                     and dark >= PEER_UNREACHABLE_EXCLUDE_SECONDS)
        if not (qualifies and quorum and unanimous):
            streak["excluded"] = False
            continue
        excluded.append(peer)
        note = (f"{peer} unreadable since {_iso(streak['since'])} ({streak['reads']} reads; "
                f"also dark to {', '.join(corroborated_by)}; {1 + len(readable)}/{fleet} read)")
        notes.append(note)
        if record and not streak["excluded"]:
            host_off.event(cfg.state_dir, "peer_unreachable_excluded",
                           f"peer {note}: it no longer holds the update turn",
                           {"peer": peer, "since": _iso(streak["since"]), "reads": streak["reads"],
                            "corroborated_by": corroborated_by,
                            "readable": 1 + len(readable), "fleet": fleet}, now=now)
        streak["excluded"] = True
    if record:
        _write_json(cfg.state_dir / PEER_UNREADABLE_NAME, streaks)
    busy: list[str] = []
    waiting: dict[str, float] = {}
    off: list[str] = []
    for peer, info in infos.items():
        if peer in excluded:
            continue
        if info["busy"]:
            busy.append(info["evidence"])
        elif info.get("off"):
            off.append(peer)
            continue
        if info["since"] is not None:
            waiting[peer] = info["since"]
    return Survey(busy, waiting, off, excluded, notes)


def queue_ahead(me: str, my_since: float, waiting: dict[str, float]) -> list[str]:
    """Peers whose tickets joined the queue before this host's, and so go first.

    Without an order, the host whose stagger comes first in the window won
    every contended turn: m3 was refused at every attempt for hours while a
    peer updated. The order is the tickets' recorded `since`, earliest first,
    ties to the lower id: a total order every host computes identically from
    the same tickets. Comparing waits measured during each host's survey made
    the order depend on when the survey ran: on 2026-10-02 m3 held the
    earliest ticket but surveyed after a 10 min checkout, measured itself
    short, and yielded to m5 while m5 yielded back, so no host updated.
    """
    return [f"{peer} (waiting since {_iso(since)})"
            for peer, since in sorted(waiting.items(), key=lambda item: (item[1], item[0]))
            if (since, peer) < (my_since, me)]


def waiting_ticket(cfg: Config) -> dict | None:
    value = _read_json(cfg.state_dir / "waiting.json")
    return value if isinstance(value, dict) else None


def note_waiting(cfg: Config, me: str, target: str, reason: str, now: float) -> dict:
    """Keep this host's place in the queue: `since` survives new targets."""
    ticket = waiting_ticket(cfg) or {}
    since = ticket.get("since") if isinstance(ticket.get("since"), (int, float)) else now
    ticket = {"host_id": me, "target": target, "since": since, "ts": now, "reason": reason[:300],
              "starved_evented": ticket.get("starved_evented"), "issue": ticket.get("issue"),
              "issue_body": ticket.get("issue_body")}
    _write_json(cfg.state_dir / "waiting.json", ticket)
    return ticket


PENDING_FIX_NAME = "pending-fix.json"


def clear_waiting(cfg: Config, now: float | None = None) -> None:
    """This host has its turn: leave the queue, and finish its starvation issue.

    The fix time is recorded first (pending-fix.json), then the ticket goes.
    The issue's marker is PATCHed with fixed_at and only then is the issue
    closed; a failed PATCH or close is retried on the next run, so a transient
    API error never leaves a closed issue whose marker says fixed_at null.
    """
    ticket = waiting_ticket(cfg) or {}
    if ticket.get("issue"):
        fixed_at = _iso(time.time() if now is None else now)
        body = ticket.get("issue_body")
        try:
            fixed_body = tartci_incident.fixed(body, fixed_at) if isinstance(body, str) and body else None
        except ValueError:
            fixed_body = None
        pending = _pending_fixes(cfg)
        pending.append({"issue": str(ticket["issue"]), "issue_body": fixed_body,
                        "fixed_at": fixed_at, "patched": fixed_body is None})
        _write_json(cfg.state_dir / PENDING_FIX_NAME, {"fixes": pending})
    (cfg.state_dir / "waiting.json").unlink(missing_ok=True)
    finish_pending_fixes(cfg)


def _pending_fixes(cfg: Config) -> list[dict]:
    value = _read_json(cfg.state_dir / PENDING_FIX_NAME)
    fixes = value.get("fixes") if isinstance(value, dict) else None
    return [fix for fix in fixes if isinstance(fix, dict) and fix.get("issue")] \
        if isinstance(fixes, list) else []


# GitHub answers 404 for an issue that no longer exists (or that the App can
# no longer see) and 410 for a deleted one. Neither changes on a retry.
_ISSUE_GONE = re.compile(r"HTTP 40[4]|HTTP 410|\"status\"\s*:\s*\"?(?:404|410)\b|\b(?:Not Found|was deleted)\b")


def _issue_gone(text: str) -> bool:
    return bool(_ISSUE_GONE.search(text or ""))


def finish_pending_fixes(cfg: Config) -> bool:
    """PATCH each recorded fix time into its issue's marker, then close the
    issue. Whatever fails stays recorded for the next run, except an issue
    GitHub says is gone (404 or 410): that is dropped and logged once, since
    no retry can reach it. True when none is left."""
    pending = _pending_fixes(cfg)
    if not pending:
        return True
    import host_off  # noqa: PLC0415 - sibling module; owns the issue helpers
    left = []
    for fix in pending:
        step, rc, text = "", 0, ""
        if not fix.get("patched"):
            step = "patch"
            rc, text = host_off._patch_issue_body(fix["issue"], str(fix["issue_body"]))
            if rc == 0:
                fix["patched"] = True
        if rc == 0:
            step = "close"
            rc, text = host_off._close_issue(fix["issue"])
        if rc == 0:
            continue
        fix["error"] = f"{step}: {text}"[:300]
        if _issue_gone(text):
            print(f"self-update: starvation issue #{fix['issue']} is gone ({fix['error']}); "
                  f"dropping its fix time {fix.get('fixed_at')}")
            host_off.event(cfg.state_dir, "self_update_issue_gone",
                           f"starvation issue #{fix['issue']} is gone; fix time not recorded",
                           {"issue": fix["issue"], "fixed_at": fix.get("fixed_at"),
                            "error": fix["error"]})
            continue
        print(f"self-update: starvation issue #{fix['issue']}: "
              + ("fixed_at not recorded" if step == "patch" else "not closed")
              + f" ({fix['error']}); retrying next run")
        left.append(fix)
    path = cfg.state_dir / PENDING_FIX_NAME
    if left:
        _write_json(path, {"fixes": left})
    else:
        path.unlink(missing_ok=True)
    return not left


def _turn_holder(reason: str) -> tuple[str, str]:
    """(cause, the peers named) from the reason a deferral recorded."""
    text = reason or ""
    if text.startswith("yielding the update turn to a host that has waited longer: "):
        return "waited_longer", text.split(": ", 1)[1]
    if text.startswith("another fleet host is not serving normally: "):
        evidence = text.split(": ", 1)[1]
        dark = any(word in evidence.lower() for word in ("unreadable", "unreachable"))
        return ("dark" if dark else "busy"), evidence
    if text.startswith("peer announced first") or text.startswith("peer changed after announcing"):
        return "announced", text.split(": ", 1)[1] if ": " in text else text
    return "unknown", text


def starvation_body(cfg: Config, me: str, ticket: dict, waited: float, reason: str,
                    now: float) -> str:
    """What happened, the evidence, the likely cause, the exact fix, whether it
    is safe to apply unattended, and how to verify: one issue a reader can act
    on without opening a host."""
    cause, named = _turn_holder(reason)
    since = _iso(float(ticket["since"]))
    demand = ticket.get("demand") or []
    dark = _read_json(cfg.state_dir / PEER_UNREADABLE_NAME) or {}
    if demand:
        bound = (f"while gate jobs it serves were queued. The bound with queued gate demand "
                 f"is {STARVED_AFTER_SECONDS // 3600} h: one hour past the "
                 f"{PEER_UNREACHABLE_EXCLUDE_SECONDS // 3600} h after which a dark peer stops "
                 "holding the turn.")
    else:
        bound = (f"with no queued gate demand read for it"
                 f"{' (the demand read failed)' if ticket.get('demand_error') else ''}. "
                 f"The bound without demand is {STARVED_FALLBACK_SECONDS // 3600} h.")
    lines = [
        f"**What happened.** {me} has had no tartci self-update turn for "
        f"{waited / 3600:.1f} h (in the update queue since {since}), {bound}",
        "",
        "**Evidence.**",
        f"- Deferral reason: {reason[:300]}",
        f"- Waiting ticket: since {since}, target {str(ticket.get('target') or '')[:12]}",
    ]
    for job in demand[:5]:
        lines.append(f"- Queued gate job {job.get('id')} ({job.get('repo')} "
                     f"{job.get('workflow')}: {job.get('name')}) since {job.get('created_at')}")
    if len(demand) > 5:
        lines.append(f"- ...and {len(demand) - 5} more queued gate jobs")
    if ticket.get("demand_error"):
        lines.append(f"- Gate demand read failed: {str(ticket['demand_error'])[:200]}")
    for peer, streak in sorted(dark.items()) if isinstance(dark, dict) else []:
        if isinstance(streak, dict) and isinstance(streak.get("since"), (int, float)):
            lines.append(f"- {peer} unreadable since {_iso(float(streak['since']))} "
                         f"({streak.get('reads')} reads; excluded: {bool(streak.get('excluded'))})")
    lines += ["", "**Likely cause and fix.**"]
    if cause == "dark":
        lines += [
            f"A peer this host cannot read holds the turn ({named[:200]}). It stops holding "
            f"it automatically once it has been dark for {PEER_UNREACHABLE_EXCLUDE_SECONDS // 3600} h, "
            "read unreadable by every readable peer.",
            "- Check the dark host at the machine: `tartci pool status`.",
            f"- To update {me} now instead, on {me}: `tartci fleet-macos self-update --plan`, "
            "then `tartci fleet-macos self-update --apply`.",
            "- Safe to auto-apply: no. The automatic exclusion already runs; a manual "
            "update takes this host out of service.",
        ]
    elif cause == "busy":
        lines += [
            f"A peer is not serving normally ({named[:200]}), so this host will not drain too.",
            "- On that peer: `tartci pool status`, and let its update or drain finish.",
            "- Safe to auto-apply: no; wait for the peer, or fix it by hand.",
        ]
    elif cause == "waited_longer":
        lines += [
            f"{named[:200]} joined the update queue first and goes first.",
            "- On that host: `tartci fleet-macos self-update --plan` shows why it has not "
            "updated. If its ticket is stale, move it aside there: "
            "`mv ~/.tartci/state/self-update/waiting.json ~/.tartci/state/self-update/waiting.json.aside` "
            "(it rejoins at the back on its next attempt).",
            "- Safe to auto-apply: no; moving a ticket changes the queue order.",
        ]
    else:
        lines += [
            f"A peer holds the turn ({named[:200]}).",
            "- On every host: `tartci pool status`.",
            "- Safe to auto-apply: no.",
        ]
    lines += [
        "",
        f"**Verify.** On {me}, `tartci fleet-macos self-update --plan` takes the turn, or "
        "`~/.tartci/state/self-update/waiting.json` is gone. This issue records the fix "
        "time on its marker and closes itself when the host gets its turn.",
    ]
    marker = tartci_incident.marker(me, "self_update_starved", since, _iso(now))
    return tartci_incident.with_marker("\n".join(lines), marker)


def starvation_issue(cfg: Config, me: str, ticket: dict, waited: float, reason: str,
                     now: float | None = None) -> None:
    """Once per episode, a GitHub issue: the event log alone was read by nobody.

    During the 2026-10-01 stall three hosts crossed the bound and wrote
    self_update_starved, and nobody saw it for hours. Best effort, like
    host_off: a failure is recorded on the ticket and retried next run.
    """
    if ticket.get("issue") or os.environ.get("TARTCI_SELF_UPDATE_ISSUE", "1") == "0":
        return
    import host_off  # noqa: PLC0415 - sibling module; owns the issue helpers
    now = time.time() if now is None else now
    title = (f"[tartci] {me} self-update starved for {waited / 3600:.1f} h "
             f"since {_iso(float(ticket['since']))}")
    body = starvation_body(cfg, me, ticket, waited, reason, now)
    rc, text = host_off._open_issue(title, body)
    if rc == 0 and text.strip().isdigit():
        ticket["issue"] = text.strip()
        ticket["issue_body"] = body
    else:
        ticket["issue_error"] = text[:300]


def mid_job_lanes(plan_text: str) -> list[str]:
    """The `lane: state, detail` rows under `mid-job lanes:` in `pool off --plan`."""
    rows: list[str] = []
    inside = False
    for line in plan_text.splitlines():
        if line.strip() == "mid-job lanes:":
            inside = True
            continue
        if inside:
            if not line.startswith("    ") or ":" not in line:
                break
            rows.append(line.strip()[:300])
    return rows


def launcher_ack_path(cfg: Config) -> Path:
    return cfg.home / ".config" / "tartci" / "launcher-change-ack"


def launcher_change_refusal(cfg: Config, target: str, installed: str,
                            refresh: bool) -> str | None:
    """Why this update must not replace the sealed launcher yet, or None.

    The launcher bundle seals the support tree and its commit, so every update
    to a new commit replaces TartCILauncher.app, and replacing it can reset
    its Removable Volumes consent. On a host whose Tart store is on an
    external volume the helper's open() then blocks and every lane stops. The
    update proceeds only once that exact target commit is acknowledged, in
    launcher-change-ack by an operator or automatically (auto_acknowledge);
    the bundle digest cannot be the key: each build is re-signed with a new
    timestamp. Checked before any drain.
    """
    if launch_helper(cfg) is None or refresh or target == installed:
        return None
    try:
        acknowledged = launcher_ack_path(cfg).read_text().strip()
    except OSError:
        acknowledged = ""
    if acknowledged == target:
        return None
    return (f"launcher change needs consent: {installed[:12]} -> {target[:12]} replaces "
            "TartCI Launcher, which can reset its Removable Volumes consent; acknowledge "
            f"with: echo {target} > {launcher_ack_path(cfg)}")


def planned_layout(cfg: Config) -> str | None:
    """The launcher layout the installer will write ("v1"/"v2"), None on a host
    without a launch helper. Refuses with the installer's own finding (an
    untrusted opt-in file, a v2 host whose opt-in is gone) before any drain."""
    if launch_helper(cfg) is None:
        return None
    import launcher_layout  # noqa: PLC0415 - sibling module; owns the layout rules
    try:
        return launcher_layout.decide(cfg.home,
                                      launcher_layout.read_receipt(cfg.install_receipt),
                                      app=_helper_app(cfg))
    except launcher_layout.LayoutError as exc:
        raise Refused(f"launcher layout: {exc}") from exc


@dataclasses.dataclass
class LauncherChange:
    """Whether a v2 turn replaces the stable app, and why (B1).

    The app is keyed on what it is made of (the inputs digest of main.swift,
    Info.plist and policy.json), who signs it and for which team. A turn that
    leaves all three equal installs no new app, so its Removable Volumes grant
    stands; any difference, or a move from v1, is a launcher change.
    """
    live_layout: str | None
    live_inputs: str | None
    target_inputs: str
    live_identity: str
    identity: str
    live_team: str | None
    team: str

    @property
    def differs(self) -> list[str]:
        out = []
        if self.live_layout != "v2":
            out.append(f"layout {self.live_layout or 'none'} -> v2")
        if self.live_inputs != self.target_inputs:
            out.append(f"inputs {str(self.live_inputs)[:12]} -> {self.target_inputs[:12]}")
        if self.live_identity != self.identity:
            out.append(f"identity {self.live_identity[:12]} -> {self.identity[:12]}")
        if self.live_team != self.team:
            out.append(f"team {self.live_team} -> {self.team}")
        return out

    @property
    def changed(self) -> bool:
        return bool(self.differs)

    @property
    def ack(self) -> str:
        return f"v2 inputs={self.target_inputs} identity={self.identity} team={self.team}"

    def describe(self) -> str:
        if not self.changed:
            return ("launcher v2: no launcher change (inputs, signing identity and team equal "
                    "the live app's); the app stays in place")
        return "launcher v2: launcher change (" + "; ".join(self.differs) + ")"


def choose_signer(sys_: System, home: Path, live_identity: str) -> str:
    """The identity a v2 turn signs with: the live app's own certificate while
    the dedicated keychain still holds it, else the keychain's one Developer ID
    Application identity (a rotated certificate, so a launcher change)."""
    keychain = signing_keychain(home)
    result = sys_.run(["security", "find-identity", "-v", "-p", "codesigning",
                       *([keychain] if keychain else [])])
    found = re.findall(r'\b([0-9A-F]{40})\b "Developer ID Application', result.out or "")
    if live_identity in found:
        return live_identity
    if len(set(found)) == 1:
        return found[0]
    raise Refused(f"the dedicated signing keychain holds {len(set(found))} Developer ID "
                  f"Application identities and not the live launcher's {live_identity}; "
                  "keep exactly one there (`pulp ship doctor`)")


def certificate_team(sys_: System, home: Path, identity: str) -> str | None:
    """The Team ID in the dedicated keychain's Developer ID Application
    certificate `identity` ("Developer ID Application: Name (TEAMID)")."""
    keychain = signing_keychain(home)
    result = sys_.run(["security", "find-identity", "-v", "-p", "codesigning",
                       *([keychain] if keychain else [])])
    match = re.search(rf'\b{re.escape(identity)}\b "Developer ID Application: [^"]*\(([A-Z0-9]{{10}})\)"',
                      result.out or "")
    return match.group(1) if match else None


def v2_launcher_change(cfg: Config, sys_: System, helper: dict, profile: Path,
                       identity: str, live_identity: str) -> LauncherChange:
    """Measure, before any drain, whether this v2 turn needs a new app."""
    resources = Path(helper["path"]) / "Contents" / "Resources"
    digest = sys_.run(["python3", "-c",
                       "import sys, tomllib; sys.path.insert(0, 'scripts'); "
                       "import macos_launcher_identity as m; "
                       "print(m.source_inputs_digest('.', tomllib.loads(open(sys.argv[1]).read())))",
                       str(profile)], cwd=str(cfg.checkout))
    target_inputs = digest.out.strip()
    if digest.rc != 0 or not re.fullmatch(r"[0-9a-f]{64}", target_inputs):
        raise Refused(f"cannot measure the target's launcher inputs: {digest.text[:300]}")
    detail = sys_.run(["codesign", "-d", "--verbose=2", helper["path"]])
    team = re.search(r"^TeamIdentifier=(\S+)$", detail.text or "", re.MULTILINE)
    return LauncherChange(
        live_layout=installed_layout(cfg),
        live_inputs=(_read_json(resources / "inputs.json") or {}).get("sha256"),
        target_inputs=target_inputs, live_identity=live_identity, identity=identity,
        live_team=team.group(1) if team else None,
        team=signing_team(sys_, cfg.home, identity, str(helper.get("team_id"))))


def signing_team(sys_: System, home: Path, identity: str, profile_team: str) -> str:
    """The team the new app would carry: the signing certificate's own Team
    ID, which must be the profile's launch_helper.team_id (the policy pins it)."""
    team = certificate_team(sys_, home, identity)
    if team != profile_team:
        raise Refused(f"the signing certificate {identity} is for team {team or 'unknown'}, not "
                      f"the profile's launch_helper.team_id {profile_team}; a launcher signed by it "
                      "would fail its own policy. Fix the keychain (`pulp ship doctor`) or the profile")
    return team


def v2_ack_refusal(cfg: Config, change: LauncherChange) -> str | None:
    """Why a v2 launcher change may not go ahead yet, or None: the operator
    acknowledges exactly this inputs digest, signing identity and team."""
    if not change.changed:
        return None
    try:
        acknowledged = launcher_ack_path(cfg).read_text().strip()
    except OSError:
        acknowledged = ""
    if acknowledged == change.ack:
        return None
    return (f"launcher change needs consent ({'; '.join(change.differs)}): a new TartCI "
            "Launcher can reset its Removable Volumes consent; acknowledge with: "
            f"echo '{change.ack}' > {launcher_ack_path(cfg)}")


LAUNCHER_AUTO_APPLY_NAME = "launcher-auto-apply.json"
REMOVABLE_VOLUMES_GRANT = ("grant Removable Volumes to TartCI Launcher (System Settings > "
                           "Privacy & Security > Files and Folders)")


def bundle_team(sys_: System, path: str | Path) -> str | None:
    """The TeamIdentifier `codesign -dv` reports for a signed bundle."""
    detail = sys_.run(["codesign", "-d", "--verbose=2", str(path)])
    match = re.search(r"^TeamIdentifier=(\S+)$", detail.text or "", re.MULTILINE)
    return match.group(1) if match and match.group(1) != "not" else None


def launcher_auto_apply_hold(cfg: Config) -> dict | None:
    """The target whose automatic launcher change failed, with why and how to
    take it by hand; None when nothing is held."""
    value = _read_json(cfg.state_dir / LAUNCHER_AUTO_APPLY_NAME)
    return value if isinstance(value, dict) and value.get("target") else None


def hold_launcher_auto_apply(cfg: Config, sys_: System, target: str, outcome: str,
                             detail: str, ack: str) -> None:
    """Never auto-acknowledge `target` again: the next attempt at it waits for
    launcher-change-ack, and a new target is tried automatically once."""
    _write_json(cfg.state_dir / LAUNCHER_AUTO_APPLY_NAME, {
        "target": target, "outcome": outcome, "at": _iso(sys_.now()), "detail": detail[:500],
        "acknowledge": f"echo '{ack}' > {launcher_ack_path(cfg)}"})


def launcher_auto_ack_refusal(cfg: Config, sys_: System, target: str,
                              refresh: str | None) -> str | None:
    """Why this launcher change may not be acknowledged automatically, or None.

    Daniel approved acknowledging routine launcher changes (2026-10-27): a
    target on tartci main, signed for the installed app's team (checked by
    the callers), not one whose automatic apply already failed here.
    """
    if not cfg.launcher_auto_apply:
        return f"launcher auto-apply is off ([launcher] auto_apply = false in {cfg.settings})"
    if refresh:
        return "a same-generation refresh (" + refresh[:120] + ") is acknowledged by a person"
    held = launcher_auto_apply_hold(cfg)
    if held and held.get("target") == target:
        return (f"the automatic apply of {target[:12]} already failed here "
                f"({held.get('outcome')} at {held.get('at')}); held until the target changes")
    on_main = sys_.run(["git", "-C", str(cfg.checkout), "merge-base", "--is-ancestor",
                        target, "origin/main"])
    if on_main.rc != 0:
        return f"{target[:12]} is not on tartci origin/main"
    return None


def auto_acknowledge(cfg: Config, sys_: System, receipt: Receipt, target: str,
                     refresh: str | None, *, key: str, team: str | None, live_bundle: str,
                     live_team: str | None = None, why_not: str | None = None) -> bool:
    """Acknowledge this launcher change in the receipt instead of
    launcher-change-ack when launcher_auto_ack_refusal allows it and the new
    app keeps the installed Team ID. `team` None means the build decides
    (verify_auto_ack_team); a v1 launcher is signed by the live identity."""
    why_not = why_not or launcher_auto_ack_refusal(cfg, sys_, target, refresh)
    if why_not is None and live_team is None:
        live_team = bundle_team(sys_, live_bundle)
    if why_not is None and live_team is None:
        why_not = "the installed launcher's Team ID is unreadable"
    if why_not is None and team is not None and team != live_team:
        why_not = f"the new launcher is for team {team}, the installed one for {live_team}"
    if why_not is not None:
        receipt.value["launcher_ack"] = {"mode": "manual", "auto_refused": why_not}
        receipt.step("launcher-ack", f"not acknowledged automatically: {why_not}", ok=False)
        return False
    receipt.value["launcher_ack"] = {
        "mode": "automatic", "target": target, "key": key, "at": _iso(sys_.now()),
        "reason": "target is on tartci main and the new launcher keeps the installed Team ID",
        "team_id": team, "installed_team_id": live_team}
    receipt.step("launcher-ack", f"acknowledged automatically: {target[:12]} is on tartci main; "
                 f"team {team or '(checked on the built bundle)'} = installed {live_team}")
    return True


def verify_auto_ack_team(cfg: Config, sys_: System, receipt: Receipt, bundle: Path,
                         live_team: str | None) -> None:
    """Before any drain: the built launcher verifies and carries the installed
    app's TeamIdentifier, or the change waits for a person and this target is
    held."""
    ack = receipt.value["launcher_ack"]
    check = sys_.run(["codesign", "--verify", "--strict", str(bundle)])
    team = bundle_team(sys_, bundle) if check.rc == 0 else None
    ack["team_id"] = team
    if check.rc == 0 and team is not None and team == live_team:
        receipt.step("launcher-ack-team", f"built launcher verifies, team {team} = installed")
        return
    why = (f"codesign --verify failed: {check.text[:200]}" if check.rc != 0 else
           f"the built launcher is for team {team or 'unknown'}, the installed one for {live_team}")
    ack.update(mode="manual", auto_refused=why)
    receipt.step("launcher-ack-team", why, ok=False)
    hold_launcher_auto_apply(cfg, sys_, str(ack["target"]), "team_mismatch", why, str(ack["key"]))
    raise Refused(f"launcher change not acknowledged automatically: {why}; refused before drain, "
                  f"the host keeps serving; acknowledge with: echo '{ack['key']}' > "
                  f"{launcher_ack_path(cfg)}")


def launcher_auto_apply_line(state_dir: Path) -> str | None:
    """For status: an automatic launcher change rolled back for lost consent
    and the last attempt is still that one."""
    held = _read_json(state_dir / LAUNCHER_AUTO_APPLY_NAME) or {}
    last = _read_json(state_dir / "last.json") or {}
    if (not held or held.get("outcome") != "probe_failed"
            or last.get("target") != held.get("target") or last.get("status") == "succeeded"):
        return None
    return (f"self-update: LAUNCHER CONSENT LOST for {str(held['target'])[:12]} at "
            f"{held.get('at')}: {REMOVABLE_VOLUMES_GRANT} once it installs; install it with: "
            f"{held.get('acknowledge')} (auto-apply rolled back and holds this target)")


def launcher_consent_alert(cfg: Config, me: str, target: str, installed: str,
                           refusal: str | None, now: float) -> None:
    """One issue while a launcher change waits for consent; it closes once the
    change is acknowledged or no longer needed. Best effort: never raises."""
    if os.environ.get("TARTCI_SELF_UPDATE_ISSUE", "1") == "0":
        return
    try:
        import host_off  # noqa: PLC0415 - sibling module; owns the issue helpers
        path = cfg.state_dir / "launcher-consent-alert.json"
        since = (host_off._read_json(path) or {}).get("since") or _iso(now)
        ack = launcher_ack_path(cfg)

        def render() -> tuple[str, str]:
            body = "\n".join([
                "## What happened",
                f"`{me}`'s self-update to `{target[:12]}` would replace TartCI Launcher "
                f"(installed from `{installed[:12]}`). It refused before draining: the host "
                "is still serving on the installed generation.", "",
                "## Evidence",
                f"- `{ack}` does not name `{target}`",
                "- the self-update receipt's `refused` step quotes this refusal", "",
                "## Likely cause",
                "Every TartCI commit seals a new launcher bundle. Replacing the bundle can "
                "reset its per-app Removable Volumes consent; this host's Tart store is on "
                "an external volume, so without that consent the helper blocks in open() "
                "and no lane starts.", "",
                "## Fix", "```sh",
                "# 1. allow this one update to replace the launcher",
                f"echo {target} > {ack}",
                "# 2. right after it installs: grant Removable Volumes to TartCI Launcher",
                "#    System Settings > Privacy & Security > Files and Folders >",
                "#    TartCI Launcher > Removable Volumes (on)",
                "```", "",
                "## Safe to auto-apply",
                "No. The consent is a person's decision in System Settings.", "",
                "## How to verify",
                "The next self-update passes this gate and installs; `tartci pool on` then "
                "passes the launch-helper volume probe. This issue closes itself.",
            ])
            title = (f"[tartci] {me}: grant Removable Volumes to TartCI Launcher before "
                     f"self-update installs {target[:12]}")
            marker = tartci_incident.marker(me, "launcher_consent_needed", since, since)
            return title, tartci_incident.with_marker(body, marker)

        host_off.episode_alert(
            path, active=refusal is not None, resolved=refusal is None,
            since=since if refusal is not None else None,
            raise_event=lambda: host_off.event(cfg.state_dir, "launcher_consent_needed",
                                               (refusal or "")[:300],
                                               {"target": target, "installed": installed}, now),
            render=render)
    except Exception as exc:  # noqa: BLE001 - an alert never fails the update
        print(f"self-update: launcher-consent alert failed: {type(exc).__name__}: {exc}", flush=True)


# pool on's readiness failure from macos_launcher_probe (any outcome: a denial
# exits at once, a blocked open() times out).
LAUNCH_PROBE_FAILED = re.compile(r"launch helper volume probe")
# The probe's own control write was slow too (macos_launcher_probe): the volume,
# not the launcher's consent, is the cause.
LAUNCH_PROBE_SLOW_VOLUME = re.compile(r"volume I/O slow")


def probe_slow_volume(probe: str) -> bool:
    return bool(LAUNCH_PROBE_SLOW_VOLUME.search(probe or ""))


# The probe calls a control write this slow a slow volume (macos_launcher_probe).
STORE_SLOW_MS = 1000.0
# How old a "healthy" storage-health record may be. Each idle lane rewrites
# it on every loop (runner.sh, TARTCI_VM_POLL, 20 s), but a lane mid-job does
# not, so twice that would refuse whenever every lane is busy. No lane
# having looked at the store for this long means the record says nothing
# about the store now.
STORAGE_HEALTH_MAX_AGE_SECONDS = 10 * 60


def previous_store_timeout(cfg: Config, current: Path | None = None) -> str | None:
    """When the most recent apply that actually ran a store write saw it
    time out: that receipt's name; None otherwise.

    Receipts that ran no write (a deferral, a pause, a rate limit, or the
    stale-record refusal this guards) are skipped, so the guard stays on
    until a write is seen to finish.
    """
    for path, value in reversed(_attempts(cfg.state_dir)):
        if path == current or value.get("mode") != "apply" or "store_write" not in value:
            continue
        return path.name if value["store_write"] == "timed_out" else None
    return None


def store_volume_slow(cfg: Config, sys_: System, *, write: bool = True,
                      receipt: Receipt | None = None) -> str | None:
    """Why the Tart store is not fit to change the launcher now, or None.

    m5studio, 2026-10-18: the store volume was slow, the NEW launcher's
    volume probe timed out after the install, and since a rollback replaces
    the launcher again the host was left OFF until a person turned it on.
    Before the drain, while nothing has changed and the host is still
    serving: the storage-health gate's record must say healthy (read from
    the home folder, so a quarantined or unknown store is never written
    to), then, unless `write` is False (a plan writes nothing), the same
    control write the probe makes, from a bounded child.

    A healthy record older than STORAGE_HEALTH_MAX_AGE_SECONDS is normal on
    a busy host (only a lane at its loop top rewrites it), so it does not
    refuse by itself; the write decides. It refuses only when the previous
    attempt's write timed out, so a wedged store is not written to again on
    the strength of an old record.
    """
    # tart_home from the INSTALLED profile: that is the store the running
    # launcher, and so the post-install volume probe, uses. A target profile
    # that moves tart_home is checked against the old store; the move itself
    # is checked when the new profile's lanes first run.
    try:
        store = (_toml(cfg.installed_profile).get("host") or {}).get("tart_home")
    except (OSError, ValueError):
        store = None
    if not isinstance(store, str) or not store:
        return None
    status, reason, checked_at = sys_.store_health(store)
    if status != "healthy":
        return f"storage health for {store} is {status}" + (f" ({reason})" if reason else "")
    try:
        checked = dt.datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return f"storage health for {store} is healthy but its record has no check time"
    age = sys_.now() - checked
    if age > STORAGE_HEALTH_MAX_AGE_SECONDS:
        timed_out = previous_store_timeout(cfg, receipt.path if receipt else None)
        if timed_out:
            return (f"storage health for {store} is stale: its healthy record is {age / 60:.0f} "
                    f"min old (checked {checked_at}; at most "
                    f"{STORAGE_HEALTH_MAX_AGE_SECONDS // 60} min), and the last store write "
                    f"timed out ({timed_out})")
    if not write:
        return None
    result = sys_.store_write(store)
    if receipt is not None:
        # What the write did, for the next attempt's previous_store_timeout.
        receipt.value["store_write"] = ("ms" if "ms" in result else
                                        "timed_out" if result.get("timed_out") else "error")
    if "ms" in result:
        if result["ms"] < STORE_SLOW_MS:
            return None
        return f"a small write to {store} took {result['ms']:.0f} ms"
    if result.get("timed_out"):
        return f"a small write to {store} did not finish within {result.get('timeout', 0):g}s"
    return f"a small write to {store} failed: {result.get('error', 'unknown')}"


def launcher_probe_alert(cfg: Config, me: str, target: str, probe: str, now: float,
                         rolled_back: str | None = None) -> None:
    """One issue when a new launcher's volume probe fails: the host is left
    OFF without a rollback, or, after an automatic acknowledgement, rolled
    back to `rolled_back`. Best effort: never raises."""
    if os.environ.get("TARTCI_SELF_UPDATE_ISSUE", "1") == "0":
        return
    try:
        import host_off  # noqa: PLC0415 - sibling module; owns the issue helpers
        path = cfg.state_dir / "launcher-probe-alert.json"
        since = _iso(now)

        def render() -> tuple[str, str]:
            if probe_slow_volume(probe):
                return render_slow()
            if rolled_back:
                return render_rolled_back()
            body = "\n".join([
                "## What happened",
                f"`{me}`'s self-update installed `{target[:12]}`, which replaced TartCI "
                "Launcher, and then `pool on` failed the launch-helper volume probe. "
                "Self-update STOPPED with the host OFF and did not roll back.", "",
                "## Evidence",
                f"- pool on: `{probe.strip()[:600]}`",
                "- the self-update receipt's `pool-on` and `failed` steps", "",
                "## Likely cause",
                "Replacing the launcher app reset its per-app Removable Volumes consent, so "
                "the helper blocks in open() on the external-volume Tart store. A rollback "
                "would replace the app again and reset the consent again, so none was done.",
                "",
                "## Fix", "```sh",
                "# 1. System Settings > Privacy & Security > Files and Folders >",
                "#    TartCI Launcher > Removable Volumes (on)",
                "# 2. then, on the host:",
                "tartci pool on",
                "```", "",
                "## Safe to auto-apply",
                "No. The consent is a person's decision in System Settings, and a rollback "
                "would undo it again.", "",
                "## How to verify",
                "`tartci pool on` passes the launch-helper volume probe and the lanes start. "
                "This issue closes after the next self-update that completes.",
            ])
            title = f"[tartci] {me} is OFF: grant Removable Volumes to TartCI Launcher, then pool on"
            marker = tartci_incident.marker(me, "launcher_probe_failed", since, since)
            return title, tartci_incident.with_marker(body, marker)

        def render_rolled_back() -> tuple[str, str]:
            held = launcher_auto_apply_hold(cfg) or {}
            body = "\n".join([
                "## What happened",
                f"`{me}`'s self-update acknowledged a new TartCI Launcher for `{target[:12]}` "
                "automatically (on main, same Team ID), installed it, and `pool on` failed the "
                f"launch-helper volume probe. It rolled back to `{rolled_back[:12]}` and holds "
                "this target: it will not retry it by itself.", "",
                "## Evidence",
                f"- pool on: `{probe.strip()[:600]}`",
                "- the self-update receipt's `launcher-ack`, `pool-on` and `rollback` steps",
                f"- `{cfg.state_dir / LAUNCHER_AUTO_APPLY_NAME}`", "",
                "## Likely cause",
                "The new launcher app did not inherit the per-app Removable Volumes consent, "
                "so the helper blocks in open() on the external-volume Tart store.", "",
                "## Fix", "```sh",
                "# 1. let the next self-update install this launcher",
                str(held.get("acknowledge") or f"echo {target} > {launcher_ack_path(cfg)}"),
                "# 2. once it installs: System Settings > Privacy & Security >",
                "#    Files and Folders > TartCI Launcher > Removable Volumes (on)",
                "tartci pool on",
                "```", "",
                "## Safe to auto-apply",
                "No. The consent is a person's decision in System Settings.", "",
                "## How to verify",
                "`tartci pool on` passes the launch-helper volume probe and the lanes start. "
                "This issue closes after the next self-update that completes.",
            ])
            title = (f"[tartci] {me}: launcher auto-apply rolled back; grant Removable Volumes "
                     f"to TartCI Launcher for {target[:12]}")
            marker = tartci_incident.marker(me, "launcher_probe_failed", since, since)
            return title, tartci_incident.with_marker(body, marker)

        def render_slow() -> tuple[str, str]:
            body = "\n".join([
                "## What happened",
                f"`{me}`'s self-update installed `{target[:12]}`, which replaced TartCI "
                "Launcher, and then `pool on` failed the launch-helper volume probe because "
                "the Tart store's volume was slow. Self-update STOPPED with the host OFF and "
                "did not roll back.", "",
                "## Evidence",
                f"- pool on: `{probe.strip()[:600]}`",
                "- the probe's own control write into the same directory was slow too, so "
                "this is the volume, not the launcher's Removable Volumes consent", "",
                "## Likely cause",
                "Heavy I/O on the external volume that holds the Tart store (a large VM "
                "copy, a reclaim scan, Spotlight or backup), or a failing disk.", "",
                "## Fix", "```sh",
                "# 1. find what is loading the volume",
                "sudo fs_usage -w -f filesys | grep -i Volumes   # or Activity Monitor > Disk",
                "# 2. once a write to the store is fast again, on the host:",
                "tartci pool on",
                "```", "",
                "## Safe to auto-apply",
                "No. Whether the volume is healthy is a person's call; a rollback would "
                "replace the launcher again.", "",
                "## How to verify",
                "`tartci pool on` passes the launch-helper volume probe and the lanes start. "
                "This issue closes after the next self-update that completes.",
            ])
            title = f"[tartci] {me} is OFF: its Tart store volume was too slow for the launcher probe"
            marker = tartci_incident.marker(me, "launcher_probe_slow_volume", since, since)
            return title, tartci_incident.with_marker(body, marker)

        host_off.episode_alert(
            path, active=True, resolved=False, since=since,
            raise_event=lambda: host_off.event(cfg.state_dir, "launcher_probe_failed",
                                               probe[:300], {"target": target}, now),
            render=render)
    except Exception as exc:  # noqa: BLE001 - an alert never changes the outcome
        print(f"self-update: launcher-probe alert failed: {type(exc).__name__}: {exc}", flush=True)


def close_launcher_probe_alert(cfg: Config) -> None:
    """Close the issue once a self-update completes. Best effort."""
    path = cfg.state_dir / "launcher-probe-alert.json"
    if not path.is_file():
        return
    try:
        import host_off  # noqa: PLC0415
        host_off.episode_alert(path, active=False, resolved=True, since=None,
                               raise_event=lambda: None, render=lambda: ("", ""))
    except Exception as exc:  # noqa: BLE001
        print(f"self-update: launcher-probe alert close failed: {exc}", flush=True)


def drain_timeout_alert(cfg: Config, me: str, busy: list[str] | None, waited: float,
                        now: float) -> None:
    """One issue while the self-update drain timed out waiting on a lane; it
    closes on the next drain that completes. `busy` None means the wait ended.

    The bounded wait only failed the attempt, and a human heard of it only
    when consecutive failures halted self-update. Best effort: never raises.
    """
    if os.environ.get("TARTCI_SELF_UPDATE_ISSUE", "1") == "0":
        return
    try:
        import host_off  # noqa: PLC0415 - sibling module; owns the issue helpers
        path = cfg.state_dir / "drain-timeout-alert.json"
        # One episode across attempts: a later timeout keeps the first's time.
        since = (host_off._read_json(path) or {}).get("since") or _iso(now)
        lanes = "; ".join(busy or []) or "(not named)"

        def render() -> tuple[str, str]:
            body = "\n".join([
                "## What happened",
                f"`{me}`'s self-update drained the pool and then waited {waited / 60:.0f} min "
                f"(the bound) for its own lanes to finish, and they did not: {lanes}. "
                "The attempt failed and the host was put back ON; the update did not land.", "",
                "## Evidence",
                f"- the self-update receipt's `wait-idle` steps name what it waited on: {lanes}",
                "- `tartci pool off --plan` on the host lists `mid-job lanes:`", "",
                "## Likely cause",
                "A lane job that runs longer than the bound (a long build, or a hung job "
                "that is not timing out), or a lane supervisor whose VM lease or heartbeat "
                "outlived its work. Only tartci's own lanes count: a manual VM never "
                "blocks the drain.", "",
                "## Fix", "```sh",
                "tartci pool off --plan        # which lane, which pid",
                "# a real job: let it finish; self-update retries on its next tick",
                "# a hung job: cancel it in GitHub Actions, then let the lane tear down",
                "```", "",
                "## Safe to auto-apply",
                "No. Killing a lane mid-job kills someone's CI run.", "",
                "## How to verify",
                "The next self-update logs `wait-idle: no owned lane mid-job`; this issue "
                "then closes itself.",
            ])
            title = (f"[tartci] {me} self-update drain timed out after {waited / 60:.0f} min "
                     f"waiting on a lane")
            marker = tartci_incident.marker(me, "self_update_drain_timeout", since, since)
            return title, tartci_incident.with_marker(body, marker)

        host_off.episode_alert(
            path, active=busy is not None,
            resolved=busy is None, since=since if busy is not None else None,
            raise_event=lambda: host_off.event(cfg.state_dir, "self_update_drain_timeout",
                                               f"waited {waited:.0f}s on {lanes}"[:300],
                                               {"lanes": busy or []}, now),
            render=render)
    except Exception as exc:  # noqa: BLE001 - an alert never fails the update
        print(f"self-update: drain-timeout alert failed: {type(exc).__name__}: {exc}", flush=True)


# A gate-demand snapshot older than this is not used: the bound falls back.
GATE_DEMAND_FRESH_SECONDS = 10 * 60


def _demand_fetch() -> gate_job_demand.Fetch:
    return gate_job_demand.gh_fetcher(os.environ.get("TARTCI_GH_CLI") or "ghapp")


def _gate_queue_path(cfg: Config) -> Path:
    return _gate_queue_path_in(cfg.state_dir)


def refresh_gate_demand(cfg: Config, me: str, now: float) -> dict:
    """Read the queued self-hosted gate jobs this host serves and keep them in
    gate-queue.json. A host with no published registration reads nothing."""
    source = fleet_source.resolve(cfg.checkout)
    advertised = _read_json(source.published_supply)
    regs = gate_job_demand.registrations(advertised, me)
    if not regs:
        return {}
    try:
        jobs, errors = gate_job_demand.queued_jobs(_demand_fetch(), regs, now)
    except Exception as error:  # noqa: BLE001 - a failed read is recorded, not raised
        jobs, errors = [], [str(error)[:300]]
    snapshot = {"observed_at": _iso(now), "host_id": me, "jobs": jobs, "errors": errors}
    _write_json(_gate_queue_path(cfg), snapshot)
    return snapshot


def gate_demand(cfg: Config, me: str, now: float | None = None) -> tuple[list[dict], list[str]]:
    """(queued gate jobs this host serves, read errors) from a fresh snapshot."""
    return gate_demand_at(_gate_queue_path(cfg), me, now)


def gate_demand_at(path: Path, me: str, now: float | None = None) -> tuple[list[dict], list[str]]:
    """gate_demand for the snapshot at `path`; a stale or missing snapshot is
    no demand, reported as an error."""
    now = time.time() if now is None else now
    value = _read_json(path)
    if not isinstance(value, dict) or value.get("host_id") != me:
        return [], ["no gate-demand snapshot for this host"]
    observed = gate_job_demand.epoch(value.get("observed_at"))
    if observed is None or not 0 <= now - observed <= GATE_DEMAND_FRESH_SECONDS:
        return [], ["the gate-demand snapshot is stale"]
    jobs = value.get("jobs") if isinstance(value.get("jobs"), list) else []
    errors = value.get("errors") if isinstance(value.get("errors"), list) else []
    return gate_job_demand.jobs_for(jobs, me, now), [str(error) for error in errors]


def gate_demand_waiting(cfg: Config, me: str = "", now: float | None = None) -> bool:
    """Whether queued gate jobs this host serves are waiting."""
    return bool(gate_demand(cfg, me, now)[0])


def _gate_queue_path_in(state_dir: Path) -> Path:
    return Path(os.environ.get("TARTCI_GATE_QUEUE_JSON") or str(state_dir / "gate-queue.json"))


def defer(cfg: Config, me: str, target: str, reason: str, now: float) -> int:
    """Record the deferral; become loud past the starvation bound: one hour
    after the dark-peer exclusion when gate jobs this host serves are queued,
    STARVED_FALLBACK_SECONDS otherwise or when the demand read failed."""
    ticket = note_waiting(cfg, me, target, reason, now)
    waited = max(0.0, now - float(ticket["since"]))
    refresh_gate_demand(cfg, me, now)
    demand, demand_errors = gate_demand(cfg, me, now)
    ticket["demand"] = demand[:20]
    # A failed demand read falls back to the long bound, and says so: a blind
    # read is not "no demand".
    ticket["demand_error"] = "; ".join(demand_errors)[:300] or None
    _write_json(cfg.state_dir / "waiting.json", ticket)
    if demand_errors:
        print(f"self-update: gate demand unread ({ticket['demand_error']}); "
              f"starvation bound {STARVED_FALLBACK_SECONDS // 3600} h")
    bound = STARVED_AFTER_SECONDS if demand else STARVED_FALLBACK_SECONDS
    if waited < bound:
        print(f"self-update: DEFERRED ({waited / 3600:.1f} h in the update queue): {reason}")
        return EXIT_DEFERRED
    print(f"self-update: STARVED: no update turn for {waited / 3600:.1f} h "
          f"(since {_iso(float(ticket['since']))}): {reason}")
    if not ticket.get("starved_evented"):
        import host_off  # noqa: PLC0415 - sibling module; owns the event log format
        host_off.event(cfg.state_dir, "self_update_starved",
                       f"no update turn for {waited / 3600:.1f} h: {reason[:200]}",
                       {"since": _iso(float(ticket["since"])), "target": target[:12],
                        "gate_jobs": [job.get("id") for job in demand[:20]],
                        "demand_error": ticket.get("demand_error")}, now)
        ticket["starved_evented"] = now
    starvation_issue(cfg, me, ticket, waited, reason, now)
    _write_json(cfg.state_dir / "waiting.json", ticket)
    return EXIT_REFUSED


def stagger_seconds(host_id: str, window: int = 600) -> int:
    """A stable per-host offset so scheduled runs on different hosts spread out."""
    return int(hashlib.sha256(host_id.encode()).hexdigest(), 16) % window


# ── capacity floor ─────────────────────────────────────────────────────────

_PYTHON_SHIM_DIR: str | None = None


def python_shim_dir() -> str:
    """A private directory whose `python3` is this process's child interpreter.

    Prepended to PATH for every tartci call, so the installed generation AND an
    older one being rolled back to (whose shell helpers may call a bare
    `python3`) resolve the tomllib-capable interpreter this run already uses,
    not /usr/bin/python3 3.9 from a minimal ssh or launchd PATH.
    """
    global _PYTHON_SHIM_DIR
    if _PYTHON_SHIM_DIR is None or not os.path.isdir(_PYTHON_SHIM_DIR):
        directory = tempfile.mkdtemp(prefix="tartci-self-update-python-")
        os.symlink(child_python(), os.path.join(directory, "python3"))
        _PYTHON_SHIM_DIR = directory
    return _PYTHON_SHIM_DIR


def installed_lane_python(home: Path) -> str | None:
    """The absolute TARTCI_PYTHON the installed fleet lanes run, or None."""
    import plistlib  # noqa: PLC0415
    for plist in sorted((home / "Library" / "LaunchAgents").glob(
            "com.danielraffel.tartci.tart-runner-macos-fleet.*.plist")):
        try:
            value = plistlib.loads(plist.read_bytes()).get("EnvironmentVariables", {}).get(
                "TARTCI_PYTHON")
        except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
            continue
        if isinstance(value, str) and value.startswith("/"):
            return value
    return None


def python_pin_path(home: Path) -> Path:
    return Path(os.environ.get("TARTCI_PYTHON_PIN") or home / ".config" / "tartci" / "python.pin")


def census_env(repository: str | None = None) -> dict[str, str]:
    # The census binds its identity per call (#227); this also pins the CLI and
    # the interpreter every tartci helper runs under, including on rollback.
    env = {
        "TARTCI_GH_CLI": os.environ.get("TARTCI_GH_CLI") or "ghapp",
        "TARTCI_PYTHON": child_python(),
        "HOME": os.environ.get("HOME") or os.path.expanduser("~"),
        "PATH": f"{python_shim_dir()}:{os.environ.get('PATH') or '/usr/bin:/bin'}",
    }
    if repository is not None:
        env["GH_REPO"] = repository
    for name in (
        "TARTCI_FLEET_SOURCE_ROOT", "TARTCI_FLEET_SOURCE_COMMIT",
        "TARTCI_FLEET_SOURCE_REPOSITORY", "TARTCI_ADVERTISED_LABELS",
        "TARTCI_JOB_CLAIM_SUPPLY",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            env[name] = value
    return env


# The capacity floor owns the judgement of whether a peer can mint on demand;
# this module reuses it so the two never disagree about the same peer.
peer_mints_on_demand = capacity_floor.peer_mints_on_demand


def on_demand_supply(cfg: Config, sys_: System, me: str,
                     wanted: list[tuple[str, str | None]]) -> tuple[dict[str, list[str]], list[str]]:
    """Which wanted labels another host can serve by minting a runner on demand.

    On an ephemeral JIT fleet a runner exists only while it holds a job, so
    "no other host has a runner registered right now" is the normal idle state,
    not a missing server. A label still has a server when another host
    PUBLISHES a registration for it (main's advertised-labels.json) and that
    host's supervisors are all verified running, fleet-ready and not blocked.
    Returns ({label: [evidence]}, [why each uncovered label is uncovered]).
    """
    registrations = [r for r in published_supply(cfg, sys_).get("registrations", [])
                     if isinstance(r, dict) and isinstance(r.get("host_id"), str)
                     and r["host_id"] != me]
    peers = published_peers(cfg, sys_)
    health: dict[str, str | None] = {}

    def healthy(host_id: str) -> str | None:
        if host_id not in health:
            target = peers.get(host_id) or SSH_ALIAS_CONVENTION.format(host_id=host_id)
            ssh = peer_ssh(target)
            if ssh is None:
                health[host_id] = refused_destination(host_id, target)
                return health[host_id]
            status = sys_.run([*ssh, "cd ~ && ~/.local/bin/tartci pool status --json"], timeout=60)
            try:
                value = json.loads(status.out)
            except json.JSONDecodeError:
                value = None
            health[host_id] = (peer_mints_on_demand(value) if value is not None
                               else f"pool status unreadable (exit {status.rc})")
        return health[host_id]

    covered: dict[str, list[str]] = {}
    missing: list[str] = []
    for label, repo in wanted:
        hosts = sorted({r["host_id"] for r in registrations
                        if (label in (r.get("labels") or []) or r.get("class_label") == label)
                        and (repo is None or r.get("repo") == repo)})
        if not hosts:
            missing.append(f"{label}: no other host publishes a registration for it")
            continue
        reasons = []
        for host_id in hosts:
            why = healthy(host_id)
            if why is None:
                covered.setdefault(label, []).append(host_id)
            else:
                reasons.append(f"{host_id} {why}")
        if label not in covered:
            missing.append(f"{label}: " + "; ".join(reasons))
    return covered, missing


def floor_decision(cfg: Config, sys_: System, me: str = "") -> tuple[bool, str]:
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
    blocked = [f for f in last if f.get("label") not in cfg.idle_by_design]
    if blocked:
        wanted = list(dict.fromkeys((f["label"], f.get("repo") or None) for f in blocked))
        covered, missing = on_demand_supply(cfg, sys_, me, wanted) if me else ({}, [])
        if missing or not covered:
            detail = ("; ".join(missing) if missing
                      else "this host's identity is unknown, so peers cannot be told apart")
            raise Refused(f"this host is the last server of "
                          f"{', '.join(label for label, _ in wanted)}, which is not idle by "
                          f"design, and no other host can mint it on demand ({detail}); "
                          "refusing to take it down for an update")
        served = "; ".join(f"{label} by {', '.join(hosts)}" for label, hosts in covered.items())
        return True, (f"--allow-last-serving-host: no runner of this host's label(s) is "
                      f"registered elsewhere right now, but published supply is minted on "
                      f"demand by fully verified peers ({served}) (rule: on-demand supply)")
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


def signing_secrets(home: Path) -> dict[str, str]:
    """pulp's dedicated signing keychain settings (~/.config/pulp/secrets/keychain.env).

    Only well-formed lines are returned; see secret_files.parse. Never put a
    value from here into a message: name the key instead.
    """
    env = secret_files.load(home, "keychain.env")
    values = dict(env.values) if env is not None else {}
    for key in ("PULP_SIGN_KEYCHAIN", "PULP_SIGN_KEYCHAIN_PW"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def signing_secrets_problem(home: Path) -> str | None:
    """What is wrong with keychain.env, by key name only, or None."""
    env = secret_files.load(home, "keychain.env")
    if env is None:
        return None
    bad = list(env.malformed)
    keychain = signing_secrets(home).get("PULP_SIGN_KEYCHAIN")
    if keychain and not secret_files.keychain_path_ok(_expand_home(keychain, home)):
        bad.append("PULP_SIGN_KEYCHAIN (not a keychain path)")
    if not bad:
        return None
    return ("keychain.env is malformed: " + ", ".join(bad)
            + "; values withheld. Rewrite it with `pulp ship doctor`")


def _expand_home(value: str, home: Path) -> str:
    return str(Path(value.replace("$HOME", str(home))).expanduser())


def signing_keychain(home: Path) -> str | None:
    """The dedicated keychain every unattended signature must name.

    keychain.env's PULP_SIGN_KEYCHAIN, or its `-unattended` sibling when that
    exists: pulp's ensure_signing_ready.sh builds the sibling when the
    configured keychain cannot be unlocked, and signs from it from then on.
    A value that is not a keychain path is ignored (None): it is what a
    malformed file parses as, and it may hold the password.
    """
    keychain = signing_secrets(home).get("PULP_SIGN_KEYCHAIN")
    if not keychain:
        return None
    path = _expand_home(keychain, home)
    if not secret_files.keychain_path_ok(path):
        return None
    stem = path[: -len(".keychain-db")] if path.endswith(".keychain-db") else path
    sibling = Path(f"{stem}-unattended.keychain-db")
    return str(sibling if sibling.exists() else path)


def unlock_signing_keychain(sys_: System, home: Path) -> str:
    """Unlock the dedicated signing keychain in THIS process's session.

    A keychain unlock belongs to the security session that made it: running
    `pulp ship doctor` over SSH unlocks it for that SSH session only, and the
    launchd agent kept failing the probe with errSecInternalComponent (17
    refusals, about 9 h on m5studio, 2026-10-02). This is the same
    non-interactive step pulp's ensure_signing_ready.sh takes: only the
    dedicated keychain, its password from keychain.env, never the login
    keychain or a prompt. Returns what it did, for the receipt. A failed
    unlock does not refuse by itself; the probe that follows decides.
    """
    keychain = signing_keychain(home)
    password = signing_secrets(home).get("PULP_SIGN_KEYCHAIN_PW")
    if not keychain or not password:
        problem = signing_secrets_problem(home)
        return (f"{problem}; probing as-is" if problem else
                "no dedicated signing keychain configured in keychain.env; probing as-is")
    result = sys_.run(["security", "unlock-keychain", "-p", password, keychain], timeout=30)
    if result.rc != 0:
        return secret_files.redact(
            f"{keychain} could not be unlocked from keychain.env (exit {result.rc}); "
            "the signing probe decides", home)
    return secret_files.redact(f"unlocked {keychain} for this session", home)


def keychain_args(home: Path) -> list[str]:
    """`--keychain <dedicated>` for every codesign this module runs.

    Without it codesign walks the user search list, and a LOCKED keychain on
    that list makes securityd raise a password dialog in the GUI session. That
    is how the agent's probe put a dialog on m5studio's screen at each attempt
    on 2026-10-02 (SecurityAgent spawns at 06:06Z, 06:37Z, 13:59Z, 14:28Z,
    14:58Z, each probe ending in a 60 s TimeoutExpired).
    """
    keychain = signing_keychain(home)
    return ["--keychain", keychain] if keychain else []


def signing_probe(sys_: System, identity: str, workdir: Path,
                  keychain: list[str] | None = None) -> None:
    """Prove the identity signs unattended, with a timestamp, in bounded time.

    The equivalent of pulp's ensure_signing_ready.sh probe: an unattended run
    must never block on a keychain prompt, so a slow or failing probe refuses
    before the host is touched.
    """
    probe = workdir / "signing-probe"
    probe.write_bytes(b"tartci signing probe\n")
    result = sys_.run(["codesign", "--force", "--timestamp", *(keychain or []), "--sign", identity,
                       str(probe)], timeout=SIGNING_PROBE_TIMEOUT)
    probe.unlink(missing_ok=True)
    if result.rc != 0:
        raise Refused(f"SIGNING KEYCHAIN LOCKED or unusable: signing identity {identity} "
                      f"cannot sign unattended (exit {result.rc}: "
                      f"{secret_files.redact(result.text)[:200]}); run `pulp ship doctor` to prepare "
                      "the dedicated signing keychain, and never answer a keychain password prompt")


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


def launch_python(profile: Path) -> str:
    """The interpreter a launcher is built and verified with: the profile's
    host.python when it declares one (the installer will pin exactly that),
    else the render handoff, TARTCI_PYTHON, or this process's own."""
    host = _toml(profile).get("host") or {}
    declared = host.get("python") if isinstance(host, dict) else None
    if isinstance(declared, str) and declared.startswith("/"):
        return os.path.realpath(declared)
    return os.path.realpath(os.environ.get("TARTCI_RENDER_PYTHON") or child_python())


def build_launcher(cfg: Config, sys_: System, helper: dict, profile: Path, target: str,
                   out_dir: Path, *, layout: str = "v1", identity: str | None = None,
                   expected_inputs: str | None = None) -> tuple[Path, Path]:
    live = Path(helper["path"])
    identity = identity or extract_signing_identity(sys_, live, out_dir)
    pinned = keychain_args(cfg.home)
    signing_probe(sys_, identity, out_dir, pinned)
    bundle = out_dir / "TartCILauncher.app"
    approval = out_dir / "approved.sha256"
    manifest = cfg.checkout / ".tartci-support-manifest.json"
    set_immutable(cfg.checkout, manifest, sys_)
    try:
        launcher_python = launch_python(profile)
        result = sys_.run(["bash", "scripts/build_macos_launcher.sh", "--output", str(bundle),
                           "--approval-output", str(approval), "--identity", identity,
                           "--support-root", ".", "--profile", str(profile),
                           "--python", launcher_python, *pinned,
                           *(["--layout", "v2"] if layout == "v2" else [])],
                          cwd=str(cfg.checkout),
                          env={"TARTCI_RENDER_PYTHON": launcher_python,
                               "TARTCI_PYTHON": launcher_python},
                          timeout=1800)
    finally:
        restore_writable(cfg.checkout)
    # The builder's cleanup trap cannot remove its read-only staging copy and
    # prints `rm:` noise after a successful build; the bundle is the verdict.
    if not bundle.is_dir() or not approval.is_file():
        raise Refused(f"launcher build produced no bundle (exit {result.rc}): "
                      + "\n".join(l for l in result.text.splitlines() if not l.startswith("rm:")))
    if layout == "v2":
        verify_bundle_v2(sys_, bundle, expected_inputs)
    else:
        verify_bundle(cfg, sys_, bundle, profile, target)
    return bundle, approval


def verify_bundle_v2(sys_: System, bundle: Path, expected_inputs: str | None) -> None:
    """A new v2 app: built from exactly the inputs this turn measured, and signed."""
    inputs = (_read_json(bundle / "Contents" / "Resources" / "inputs.json") or {}).get("sha256")
    if not expected_inputs or inputs != expected_inputs:
        raise Refused(f"new launcher v2 inputs {inputs} != the target's {expected_inputs}")
    if (bundle / "Contents" / "Resources" / "bundle.json").exists():
        raise Refused("new launcher v2 carries a v1 bundle.json")
    check = sys_.run(["codesign", "--verify", "--deep", "--strict", str(bundle)])
    if check.rc != 0:
        raise Refused(f"new launcher fails codesign --verify --deep --strict: {check.text}")


def verify_bundle(cfg: Config, sys_: System, bundle: Path, profile: Path, target: str) -> None:
    resources = bundle / "Contents" / "Resources"
    meta = _read_json(resources / "bundle.json") or {}
    if meta.get("source_commit") != target:
        raise Refused(f"new launcher bundle source_commit {meta.get('source_commit')} != "
                      f"target {target}")
    lanes = (_read_json(resources / "lanes.json") or {}).get("lanes") or {}
    rendered = cfg.state_dir / "verify-render"
    shutil.rmtree(rendered, ignore_errors=True)
    launcher_python = launch_python(profile)
    result = sys_.run(["python3", "scripts/macos_fleet_lanes.py", "render", str(profile),
                       "--output", str(rendered)], cwd=str(cfg.checkout),
                      env={"TARTCI_RENDER_PYTHON": launcher_python,
                           "TARTCI_PYTHON": launcher_python})
    if result.rc != 0:
        raise Refused(f"cannot render the profile to verify the bundle: {result.text}")
    import plistlib
    expected = {}
    for plist in sorted(rendered.glob("*.plist")):
        env = plistlib.loads(plist.read_bytes()).get("EnvironmentVariables") or {}
        expected[env.get("TARTCI_QUEUE_LANE_ID")] = dict(sorted(env.items()))
    if not expected or {k: (v or {}).get("environment") for k, v in lanes.items()} != expected:
        raise Refused("new launcher lanes.json does not carry the rendered lane environment")
    for lane, environment in expected.items():
        if environment.get("TARTCI_PYTHON") != launcher_python:
            raise Refused(f"new launcher lane {lane} is missing pinned TARTCI_PYTHON {launcher_python}")
        if environment.get("SHIPYARD_GHAPP_PYTHON_BINARY") != launcher_python:
            raise Refused(f"new launcher lane {lane} is missing pinned ghapp interpreter {launcher_python}")
    check = sys_.run(["codesign", "--verify", "--deep", "--strict", str(bundle)])
    if check.rc != 0:
        raise Refused(f"new launcher fails codesign --verify --deep --strict: {check.text}")
    check_support_agent_templates(cfg, sys_, profile, target)


def check_support_agent_templates(cfg: Config, sys_: System, profile: Path, target: str) -> None:
    """Every support agent the profile declares must render from the target
    checkout, checked before any drain so a missing or broken template refuses
    the update rather than surfacing after the lanes are back."""
    agents = sys_.run(["python3", "scripts/support_agents.py", "check-templates",
                       "--profile-file", str(profile)], cwd=str(cfg.checkout))
    if agents.rc != 0:
        raise Refused(f"declared support agents do not render from {target[:12]}: "
                      f"{agents.text.strip()[:300]}")


# ── receipts ───────────────────────────────────────────────────────────────

_GENERATION_DIR = re.compile(r"tartci-generations/([0-9a-f]{7,40})(?:-[0-9a-f]+)?(?:/|$)")


def orchestrator_generation(script: Path | None = None) -> str | None:
    """The tartci commit whose code is running this self-update.

    The orchestration (gates, drain, install sequencing, verify, rollback) is
    the INSTALLED generation's code: the agent runs ~/.local/bin/tartci. Only
    the steps run through tartci() (support-manifest, validate, install) and
    the template check run the TARGET's code. So a change to orchestration takes
    effect from the update after the one that installs it. Recording which code
    orchestrated each attempt makes that visible. None when the code is not
    running from an installed generation (a checkout).
    """
    match = _GENERATION_DIR.search(str((script or Path(__file__)).resolve()))
    return match.group(1) if match else None


class Receipt:
    def __init__(self, cfg: Config, sys_: System, target: str | None, mode: str) -> None:
        self.cfg, self.sys = cfg, sys_
        now = sys_.now()
        self.value: dict[str, Any] = {"schema": SCHEMA, "mode": mode, "target": target,
                                      "orchestrator_generation": orchestrator_generation(),
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
            "host_off": bool(self.value.get("host_off")),
            "pool_state": self.value.get("pool_state")})


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


def _same_fleet_source(first: Any, second: Any) -> bool:
    """Whether two attempts selected the same profile source.

    The target commit alone is not enough for a same-generation reinstall:
    moving from the public profiles to a private source deliberately keeps the
    TartCI generation unchanged.  Older receipts have no source field, which
    is the public-source value and remains comparable to ``None``.
    """
    if not first and not second:
        return True
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    # A new commit on the same private authority is still the same source
    # class.  The source move itself gets a fresh window when crossing the
    # public/private boundary, but a busy private authority must not make the
    # canary drain once per commit.
    return bool(first) == bool(second)


def recent_attempt(cfg: Config, target: str, now: float,
                   fleet_source: dict[str, Any] | None = None) -> str | None:
    for path, value in _attempts(cfg.state_dir):
        # A refusal changed nothing, so it does not spend the attempt.
        if (value.get("mode") != "apply" or value.get("target") != target
                or value.get("status") not in TERMINAL_STATUSES
                or not _same_fleet_source(value.get("fleet_source"), fleet_source)):
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
                "receipts, fix the cause, then tartci fleet-macos self-update --clear-halt")
    return None


def scheduled_pause(state_dir: Path) -> str | None:
    value = _read_json(state_dir / "scheduled-pause.json")
    if not isinstance(value, dict) or not value.get("reason"):
        return None
    return (f"self-update PAUSED by operator at {value.get('at')}: {value['reason']} "
            "(resume with tartci fleet-macos self-update --resume)")


def record_pause_control(state_dir: Path, action: str, reason: str | None = None) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "scheduled-pause-receipts.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"action": action, "at": _iso(time.time()),
                                 **({"reason": reason} if reason else {})},
                                sort_keys=True) + "\n")


def prune_refusals(state_dir: Path, now: float) -> None:
    for path, value in _attempts(state_dir):
        if value.get("status") in ("refused", "planned") \
                and now - _ts(value.get("started_at")) > REFUSAL_RECEIPT_TTL:
            path.unlink(missing_ok=True)


# ── the procedure ──────────────────────────────────────────────────────────

def checked_in_profile(cfg: Config) -> Path:
    name = _toml(cfg.installed_profile).get("name")
    source = fleet_source.resolve(cfg.checkout)
    for path in sorted(source.profiles_dir.glob("*-macos-fleet.toml")):
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


def tartci(cfg: Config, sys_: System, *args: str, timeout: float = 900,
           repository: str | None = None) -> Result:
    """The TARGET commit's tartci, run from the managed checkout."""
    return sys_.run(["./tartci", *args], cwd=str(cfg.checkout), env=census_env(repository),
                    timeout=timeout)


def handoff_target_orchestrator(cfg: Config, target: str, *,
                                apply: bool, scheduled: bool, refresh: bool) -> bool:
    """Run the target checkout's orchestration code before sealed builds.

    A sealed launcher starts the self-update script copied into its installed
    generation.  That copy may predate a launcher verification fix and can
    therefore reject the target's corrected bundle forever.  Once the
    authenticated checkout is detached at the selected target, hand off to
    its own dispatcher.  The environment marker prevents the target copy from
    handing off to itself again.
    """
    if (not apply or refresh
            or os.environ.get("TARTCI_SELF_UPDATE_TARGET_ORCHESTRATOR") == "1"):
        return False
    if orchestrator_generation() is None:
        return False
    if launch_helper(cfg) is None:
        return False
    dispatcher = cfg.checkout / "tartci"
    args = [str(dispatcher), "fleet-macos", "self-update", "--apply",
            "--target", target, "--home", str(cfg.home)]
    if scheduled:
        args.append("--scheduled")
    # The checkout was authenticated and detached immediately before this
    # call. Replace the installed-generation process so launchd's signal
    # reaches the real orchestrator and its output remains attached.
    environment = {**os.environ, "TARTCI_SELF_UPDATE_TARGET_ORCHESTRATOR": "1"}
    os.execve(str(dispatcher), args, environment)
    return True  # only reachable under the unit-test execve stub


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
                "tartci fleet-macos self-update --verify"), []
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
        pending = _read_json(cfg.state_dir / tart_pin.PENDING)
        if pending is not None:
            # Killed mid Tart upgrade: which Tart is linked is unknown. Hold
            # the host OFF rather than serve on it.
            notes.append(f"HOST HELD OFF: killed mid Tart upgrade ({pending.get('installed')} -> "
                         f"{pending.get('pin')}); {TART_RECOVERY}")
        else:
            on = installed_tartci(cfg, sys_, "pool", "on")
            notes.append("host is on" if on.rc == 0
                         else f"HOST LEFT OFF: pool on failed: {on.text[:200]}")
        message = (f"interrupted: the self-update process (pid {value.get('pid')}) was killed "
                   f"during {value['steps'][-1]['step'] if value.get('steps') else 'start'}; "
                   f"host runs {str(running)[:12]}; " + "; ".join(notes)
                   + "; run tartci fleet-macos self-update --verify")
        value.setdefault("steps", []).append({"at": _iso(sys_.now()), "step": "interrupted",
                                              "ok": False, "detail": message})
        value.update(status="failed", error=message, finished_at=_iso(sys_.now()))
        _write_json(path, value)
        _write_json(cfg.state_dir / "last.json", {"status": "failed", "target": target,
                                                  "error": message, "at": value["finished_at"],
                                                  "receipt": str(path),
                                                  **({"host_off": True, "pool_state": "off"}
                                                     if pending is not None else {})})
        if pending is not None:
            tart_pin.hold(cfg.state_dir, sys_.now(), message)
        print(f"self-update: FAILED (recovered interrupted run): {message}", file=sys.stderr)
    marker = _read_json(cfg.state_dir / "active.json")
    if marker is not None and not _alive(sys_, marker.get("pid"), marker.get("pid_start")):
        (cfg.state_dir / "active.json").unlink(missing_ok=True)


def recover_left_off(cfg: Config, sys_: System) -> dict:
    """Before any update logic: put back in service a host a failed update left OFF.

    `pool on` for the generation installed now, backed off to one attempt per
    host_off.RECOVERY_BACKOFF_SECONDS. It installs nothing, so the same-target
    guard does not apply; it is what stood between m3 and six hours OFF.
    """
    import host_off  # noqa: PLC0415 - sibling module

    def pool_on() -> tuple[int, str]:
        result = installed_tartci(cfg, sys_, "pool", "on")
        return result.rc, result.text

    if not (_read_json(cfg.state_dir / "last.json") or {}).get("host_off"):
        return {"attempted": False, "ok": None, "reason": "not left off"}
    status = installed_tartci(cfg, sys_, "pool", "status", "--json")
    try:
        pool_state = str(json.loads(status.out).get("state") or "unknown")
    except (json.JSONDecodeError, AttributeError):
        pool_state = "unknown"
    outcome = host_off.recover(cfg.state_dir, host_off.pool_state_file(cfg.home), pool_on,
                               now=sys_.now(), who="self-update", pool_state=pool_state)
    if outcome["attempted"]:
        word = "ON again" if outcome["ok"] else "STILL OFF"
        print(f"self-update: host was left OFF by a failed update; pool on: {word} "
              f"({outcome['reason']})")
    return outcome


def plan_or_apply(cfg: Config, sys_: System, *, apply: bool, target_ref: str,
                  explicit: bool = False, scheduled: bool = False,
                  reinstall: bool = False) -> int:
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
    if apply:
        recover_left_off(cfg, sys_)
        finish_pending_fixes(cfg)
    # Resolve launcher layout before reading the installed generation or any
    # selected source. A missing receipt must still refuse when the helper app
    # on disk proves that this host runs v2.
    try:
        planned_layout(cfg)
    except Refused as exc:
        reason = f"launcher layout: {exc}" if not str(exc).startswith("launcher layout:") else str(exc)
        if apply:
            receipt = Receipt(cfg, sys_, None, "apply")
            receipt.step("refused", reason, ok=False)
            receipt.finish("refused", reason)
        else:
            print(f"self-update: REFUSED {reason}")
        return EXIT_REFUSED
    try:
        installed, source = installed_commit(cfg)
        previous_fleet_source = installed_fleet_source_record(cfg)
        if fleet_source_receipt_is_configured(cfg) and previous_fleet_source is None:
            raise Refused("installed private-source receipt lacks the source root and repository; "
                          "refusing a source move until the host is reinstalled from a complete "
                          "source receipt")
        refresh_checkout(cfg, sys_)
        selected_source = sync_fleet_source(cfg, sys_, write=apply)
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
    try:
        skew = measure_skew(cfg, sys_, installed, now, target_ref, explicit=explicit)
    except Refused as exc:
        # Repository authentication is a normal preflight refusal.  Keep the
        # scheduled agent alive and leave an attempt receipt behind instead of
        # allowing a refusal from configured_repository() to escape as a
        # traceback before Receipt has been created.
        reason = str(exc)
        skew = {"state": "unknown", "reason": reason, "measured_at": _iso(now),
                "installed": installed}
        _write_json(cfg.state_dir / "skew.json", skew)
        receipt = Receipt(cfg, sys_, installed, "apply" if apply else "plan")
        receipt.value["previous"] = installed
        receipt.step("refused", reason, ok=False)
        receipt.finish("refused", reason)
        print(f"self-update: REFUSED {reason}")
        return EXIT_REFUSED
    _write_json(cfg.state_dir / "skew.json", skew)
    print(f"self-update: installed {installed[:12]} ({source})")
    if selected_source:
        print(f"self-update: fleet source {selected_source['repository']}"
              f"@{selected_source['commit'][:12]} (installed from "
              f"{_source_name(installed_fleet_source_commit(cfg))}; {selected_source['why']})")
    print(f"self-update: {render_skew(skew)}")
    if skew.get("explicit"):
        print(f"self-update: selected {skew.get('selected_target') or 'none'} "
              f"(requested {skew.get('requested_target')}; {skew.get('selection_reason', 'same')})")
        if skew["state"] in ("soaking", "unverified"):
            print(f"self-update: REFUSED explicit target {skew.get('requested_target')}: "
                  f"{skew.get('selection_reason')}")
            return EXIT_REFUSED
    if skew["state"] in ("unknown", "diverged"):
        return EXIT_UNKNOWN
    target = skew.get("target")
    refresh = None
    if reinstall:
        # The operator's own request: reinstall the generation this host runs,
        # with the current settings (a removed [fleet_source], say), through the
        # same drain, install, pool-on and verify path as any update. It never
        # takes a NEW private source: that waits for the canary and its soak.
        recorded = installed_fleet_source_record(cfg) or {}
        if selected_source and ((selected_source.get("repository"), selected_source.get("commit"))
                                != (recorded.get("repository"), recorded.get("commit"))):
            reason = (f"--reinstall-current refused: it would install private source "
                      f"{selected_source.get('repository')}@{str(selected_source.get('commit'))[:12]} "
                      f"(installed from "
                      + (f"{recorded['repository']}@{recorded['commit'][:12]}" if recorded
                         else "the public profiles")
                      + "), which only a canary-gated update may do. "
                      "It reinstalls on the installed source, or moves to the public profiles "
                      "when [fleet_source] is removed. Fix: wait for the normal update, or "
                      "remove [fleet_source] to go public")
            print(f"self-update: REFUSED {reason}")
            return EXIT_REFUSED
        target = installed
        refresh = ("operator requested --reinstall-current: reinstall the installed "
                   "generation with the current settings")
        print(f"self-update: same-generation reinstall requested: {installed[:12]}")
    elif not target:
        refresh = refresh_reason(cfg, sys_)
        # A private-source change is a same-generation install. It must not
        # wait for an unrelated TartCI commit to finish its soak: the running
        # code is unchanged, while the selected profile bytes still need to
        # reach the installed generation. The ordinary TartCI target remains
        # gated by its own skew state below.
        source_moved = bool(refresh and refresh.startswith("fleet source moved to "))
        if refresh and (source_moved and skew["state"] in ("current", "soaking", "behind", "unverified")
                        or not source_moved and skew["state"] == "current"):
            target = installed
            print(f"self-update: same-generation reinstall needed: {refresh}")
    if not target:
        clear_waiting(cfg)
        print("self-update: nothing to do" + {
            "soaking": " (undeployed commits are still soaking)",
            "unverified": " (no soaked commit has green checks)"}.get(skew["state"], ""))
        return EXIT_NOTHING
    receipt = Receipt(cfg, sys_, target, "apply" if apply else "plan")
    receipt.value.update(requested_target=skew.get("requested_target"),
                         selected_target=skew.get("selected_target"),
                         explicit=bool(skew.get("explicit")),
                         soak_until=skew.get("soak_until"))
    receipt.value["previous"] = installed
    receipt.value["refresh"] = refresh
    receipt.value["fleet_source"] = selected_source
    try:
        if apply and launch_helper(cfg) is not None:
            _checkout(cfg, sys_, target)
        if handoff_target_orchestrator(cfg, target, apply=apply, scheduled=scheduled,
                                       refresh=bool(refresh)):
            return EXIT_OK
    except (FileNotFoundError, PermissionError) as exc:
        reason = f"target dispatcher unavailable: {type(exc).__name__}: {exc}"
        receipt.step("refused", reason, ok=False)
        receipt.finish("refused", reason)
        print(f"self-update: REFUSED {reason}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        me = self_host_id(cfg)
        # Observe peers before any later gate can defer this run.  In
        # particular, capacity-floor and installer checks may refuse before
        # the queue decision below; skipping the survey there meant a dead
        # peer never accumulated its unreadable-read streak and every host
        # could wait forever for a fresh report.  Plans remain read-only.
        survey = survey_peers(cfg, sys_, me, record=apply)
        if apply:
            paused = scheduled_pause(cfg.state_dir)
            if paused:
                raise Refused(paused)
            halted = halt_reason(cfg.state_dir)
            if halted:
                raise Refused(halted)
            tart_held = tart_hold_refusal(cfg, sys_)
            if tart_held:
                raise Refused(tart_held)
            # An operator's --reinstall-current is not a retry of an automatic
            # attempt, so the attempt rate does not hold it back.
            # A private-source move is a same-generation refresh. Its target
            # commit is unchanged, so the normal commit rate limiter would
            # mistake the earlier generation update for a repeated refresh
            # and strand the new profile source for the full backoff window.
            source_refresh = bool(refresh and refresh.startswith("fleet source moved to "))
            prior = (None if reinstall
                     else recent_attempt(cfg, target, now, selected_source))
            # A successful generation update is the harmless prior attempt
            # that must not block a private-source refresh. Failed or rolled
            # back refreshes retain the normal backoff so a broken source
            # cannot churn the fleet.
            if source_refresh and prior and prior.endswith("(succeeded)"):
                prior = None
            if prior:
                raise Refused(f"already attempted {target[:12]} within {cfg.rate_hours}h: {prior}")
        layout = planned_layout(cfg)
        consent = (launcher_change_refusal(cfg, target, installed, bool(refresh))
                   if layout != "v2" else None)
        auto_ack = False
        live_team = None
        if consent is not None:
            live_bundle = launch_helper(cfg)["path"]
            auto_ack = auto_acknowledge(cfg, sys_, receipt, target, refresh, key=target,
                                        team=None, live_bundle=live_bundle)
            if auto_ack:
                consent = None
                live_team = receipt.value["launcher_ack"]["installed_team_id"]
        if apply:
            launcher_consent_alert(cfg, me, target, installed, consent, now)
        if consent is not None:
            raise Refused(consent)
        if apply and scheduled:
            sys_.sleep(stagger_seconds(me))
        _checkout(cfg, sys_, target)
        receipt.step("checkout", f"{cfg.checkout} detached at {target}")
        profile = checked_in_profile(cfg)
        for name, args in (
                ("support-manifest", ["support-manifest", "write", "--root", ".",
                                      "--output", ".tartci-support-manifest.json"]),
                ("validate", ["fleet-macos", "validate", str(profile), "--check-reserve"])):
            result = tartci(cfg, sys_, *args)
            if result.rc != 0:
                raise Refused(f"{name} failed: {result.text}")
            receipt.step(name)
        helper = launch_helper(cfg)
        bundle = approval = None
        identity = None
        change = None
        if helper is not None and (not refresh or layout == "v2"):
            with tempfile.TemporaryDirectory() as scratch:
                live_identity = extract_signing_identity(sys_, Path(helper["path"]), Path(scratch))
                receipt.step("signing-keychain", unlock_signing_keychain(sys_, cfg.home))
                identity = (choose_signer(sys_, cfg.home, live_identity) if layout == "v2"
                            else live_identity)
                signing_probe(sys_, identity, Path(scratch), keychain_args(cfg.home))
            if layout == "v2":
                change = v2_launcher_change(cfg, sys_, helper, profile, identity, live_identity)
                receipt.step("launcher", change.describe())
                ack = v2_ack_refusal(cfg, change)
                if ack is not None:
                    auto_ack = auto_acknowledge(
                        cfg, sys_, receipt, target, refresh, key=change.ack, team=change.team,
                        live_bundle=helper["path"], live_team=change.live_team,
                        why_not=("a move from launcher v1 to v2 is acknowledged by a person"
                                 if change.live_layout != "v2" else None))
                    if auto_ack:
                        ack = None
                        live_team = change.live_team
                if apply:
                    launcher_consent_alert(cfg, me, target, installed, ack, now)
                if ack is not None:
                    raise Refused(ack)
            receipt.step("signing", f"{'would sign' if not apply else 'will sign'} "
                         + ("a sealed launcher" if layout != "v2" else
                            "the launch set" + (" and a new launcher" if change.changed else ""))
                         + f" with {identity} (a timestamped signing probe with it succeeded)")
        install_args = ["fleet-macos", "install", str(profile), "--support-source", ".",
                        "--support-manifest", ".tartci-support-manifest.json"]
        if helper is not None and layout == "v2":
            keychain = signing_keychain(cfg.home)
            if not keychain:
                raise Refused("launcher v2 signs every launch set through the dedicated signing "
                              "keychain, and keychain.env names none; run `pulp ship doctor`")
            install_args += ["--launch-set-identity", str(identity),
                             "--launch-set-keychain", keychain]
            check_support_agent_templates(cfg, sys_, profile, target)
        repository = configured_repository(cfg, sys_)
        print(f"self-update: repository={repository}")
        if helper is None:
            dry = tartci(cfg, sys_, *install_args, repository=repository)
            if dry.rc != 0:
                raise Refused(f"install dry-run failed: {dry.text}")
            receipt.step("install-dry-run", "ok")
        else:
            # Authenticate the repository and exact support commit while the
            # host is still serving. Only approval-pin dependent validation is
            # allowed to wait until after drain and the pin move.
            preflight = tartci(cfg, sys_, *install_args, "--preflight", repository=repository)
            if preflight.rc != 0:
                raise Refused(f"sealed-launcher installer preflight refused before drain: {preflight.text}")
            receipt.step("installer-preflight", "repository and exact source commit authenticated before drain")
            install_args += ["--skip-preflight"]
        # A plan evaluates every gate and reports all refusals; an apply stops
        # at the first.
        refusals = []
        busy, waiting, off = survey.busy, survey.waiting, survey.off
        ticket = waiting_ticket(cfg)
        my_since = (float(ticket["since"]) if ticket and isinstance(ticket.get("since"), (int, float))
                    else sys_.now())
        ahead = queue_ahead(me, my_since, waiting)
        if busy:
            refusal = "another fleet host is not serving normally: " + "; ".join(busy)
            if apply:
                raise Deferred(refusal)
            refusals.append(refusal)
            receipt.step("peers", refusal, ok=False)
        elif ahead:
            refusal = ("yielding the update turn to a host that has waited longer: "
                       + ", ".join(ahead))
            if apply:
                raise Deferred(refusal)
            refusals.append(refusal)
            receipt.step("peers", refusal, ok=False)
        else:
            receipt.step("peers", "every other published host is on and not updating"
                         + (f"; off and not updating, so counted as serving nothing by the "
                            f"capacity floor: {', '.join(off)}" if off else "")
                         + (f"; unreachable, so no longer holding the turn and counted as "
                            f"serving nothing by the capacity floor: {'; '.join(survey.notes)}"
                            if survey.excluded else ""))
        allow = False
        try:
            allow, rule = floor_decision(cfg, sys_, me)
            receipt.step("capacity-floor", rule)
        except Refused as exc:
            if apply:
                raise
            refusals.append(str(exc))
            receipt.step("capacity-floor", str(exc), ok=False)
        if (helper is not None and (not refresh or layout == "v2")
                and (layout != "v2" or change.changed)):
            # A new launcher must pass its volume probe after the install, and
            # a failed probe leaves the host OFF; check the volume first.
            slow = store_volume_slow(cfg, sys_, write=apply, receipt=receipt if apply else None)
            if slow:
                refusal = (f"Tart store volume slow before a launcher change: {slow}; "
                           "refused before drain, the host keeps serving; retried on the "
                           "next attempt")
                if apply:
                    raise HostFault(refusal)
                refusals.append(refusal)
                receipt.step("store-volume", refusal, ok=False)
            elif apply:
                receipt.step("store-volume", "fast enough for the new launcher's volume probe")
            else:
                receipt.step("store-volume", "storage health is healthy; an apply also times a "
                             "small write before drain (a plan writes nothing)")
        if refusals:
            raise Refused(" | ".join(refusals))
        # A v2 refresh (a [fleet_source] move) builds too when the app's
        # inputs changed: the installer verifies the live app against the
        # moved profile.
        if (helper is not None and apply and (not refresh or layout == "v2")
                and (layout != "v2" or change.changed)):
            # Built only once every cheap gate passed: a refused run must not
            # have spent a signing build, and a rebuild must always be possible.
            builds = cfg.state_dir / "builds"
            out_dir = builds / target
            clear_dir(out_dir)
            out_dir.mkdir(parents=True)
            try:
                bundle, approval = build_launcher(cfg, sys_, helper, profile, target, out_dir,
                                                  layout=layout, identity=identity,
                                                  expected_inputs=change.target_inputs
                                                  if change else None)
            except Refused as exc:
                raise HostFault(str(exc)) from exc
            prune_dirs(builds, KEEP_BUILDS)
            if auto_ack:
                verify_auto_ack_team(cfg, sys_, receipt, bundle, live_team)
            install_args += ["--launch-helper-source", str(bundle)]
            receipt.step("launcher-build", f"{bundle} verified for {target[:12]}")
        if not apply:
            tart_plan = tart_decision(cfg, sys_)
            receipt.step("plan", "would snapshot the running generation, drain, wait for no "
                         "mid-job lane, pool off, " + ("pin approval, " if helper else "")
                         + (tart_plan.describe() + ", " if tart_plan else "")
                         + "install --apply, " + ("relay reconcile, " if relay_enabled(cfg) else "")
                         + "pool on, verify; on any failure after install, roll back to "
                         f"{installed[:12]} and verify it")
            receipt.finish("planned")
            return EXIT_OK
        run = Run(cfg, sys_, receipt, me=me, target=target, previous=installed,
                  profile=profile, install_args=install_args, allow=allow,
                  repository=repository,
                  helper=helper, approval=approval, excluded=survey.excluded,
                  previous_fleet_source=previous_fleet_source, auto_ack=auto_ack)
        rc = run.execute()
        if rc == EXIT_OK and receipt.value.get("status") == "succeeded":
            publish_fleet_source_verified(cfg, sys_, selected_source)
        return rc
    except Deferred as exc:
        receipt.step("refused", str(exc), ok=False)
        receipt.value["deferred"] = True
        receipt.finish("refused", str(exc))
        return defer(cfg, me if "me" in locals() else "", target, str(exc), now)
    except HostFault as exc:
        # A pre-drain refusal must not retain an old queue ticket forever.
        # Leave the turn available to peers; the next scheduled attempt gets
        # a fresh ticket and naturally backs off behind any active holder.
        if apply:
            clear_waiting(cfg)
        receipt.step("refused", str(exc), ok=False)
        receipt.finish("refused", str(exc))
        return EXIT_REFUSED
    except Refused as exc:
        receipt.step("refused", str(exc), ok=False)
        receipt.finish("refused", str(exc))
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - nothing was mutated yet; never leave "running"
        message = f"unexpected {type(exc).__name__} before any change: {exc}"
        receipt.step("refused", message, ok=False)
        receipt.finish("refused", message)
        return EXIT_REFUSED


def _state_words(state: str) -> str:
    return {"on": "on", "undrained": "serving again (undrained)"}.get(state, state.upper())


def _same_source_bytes(cfg: Config) -> bool:
    """Whether the selected source's tree digest is the installed receipt's."""
    recorded = (_read_json(cfg.install_receipt) or {}).get("fleet_source")
    digest = recorded.get("tree_sha256") if isinstance(recorded, dict) else None
    if not isinstance(digest, str) or not digest:
        return False
    try:
        return fleet_source.resolve(cfg.checkout).tree_sha256 == digest
    except (ValueError, OSError):
        return False


# ── Tart ──────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class TartDecision:
    """This host's Tart against the pin: move it now, or why not yet."""
    installed: str
    pin: tart_pin.Pin
    refusal: str | None = None       # set: Tart stays as it is this turn
    prove_only: bool = False         # the canary already runs the pin; boot it once

    def describe(self) -> str:
        if self.refusal:
            return f"Tart {self.installed} stays ({self.refusal})"
        if self.prove_only:
            return f"boot-check Tart {self.installed} as the canary"
        return f"upgrade Tart {self.installed} -> {self.pin.version} (boot-checked)"


def _pin_run(sys_: System) -> tart_pin.Run:
    def run(argv: list[str], timeout: float = 60, env: dict[str, str] | None = None
            ) -> tuple[int, str]:
        result = sys_.run(argv, env=env, timeout=timeout)
        return result.rc, result.text
    return run


TART_VERIFIED_SCRIPT = ('date +%s; cat "${TARTCI_HOME:-$HOME/.tartci}/state/self-update/'
                        + tart_pin.VERIFIED + '" 2>/dev/null || true')


def read_tart_verified(cfg: Config, sys_: System, canary: str) -> dict | None:
    """The canary's verified Tart, aged on the canary's clock; {"unreachable":
    reason} when the canary could not be read; None when it published none."""
    if canary == self_host_id(cfg):
        return tart_pin.read_verified(_read_json(cfg.state_dir / tart_pin.VERIFIED), sys_.now())
    target = published_peers(cfg, sys_).get(canary)
    if not target:
        return {"unreachable": f"{canary} is not a published host"}
    ssh = peer_ssh(target)
    if ssh is None:
        return {"unreachable": refused_destination(canary, target)}
    result = sys_.run([*ssh, TART_VERIFIED_SCRIPT], timeout=60)
    first, _, rest = result.out.partition("\n")
    if result.rc != 0 or not first.strip().isdigit():
        return {"unreachable": f"ssh {target} exit {result.rc}"}
    try:
        value = json.loads(rest) if rest.strip() else None
    except json.JSONDecodeError:
        return {"unreachable": f"{canary}'s {tart_pin.VERIFIED} is not JSON"}
    return tart_pin.read_verified(value, float(first.strip()))


# Refusals that only mean "not this host's turn yet": Tart behind its pin is
# information, not a problem, while one of these holds.
_NOT_DUE = ("waiting for canary", "soaking for", "no canary")


TART_RECOVERY = "to bring back the previous Tart: `ls /opt/homebrew/Cellar/tart` (and .../softnet); while it lists the failed version and /opt/homebrew/opt/tart points at it, `brew uninstall --ignore-dependencies openai/tools/tart`, then `brew link --overwrite openai/tools/tart` and `tart --version` (the same for openai/tools/softnet); then remove ~/.tartci/state/self-update/tart-broken.json and run `tartci pool on`"


def tart_decision(cfg: Config, sys_: System, root: Path | None = None,
                  fresh: bool = False) -> TartDecision | None:
    """What this turn does to Tart, against the pin under `root`: by default
    cfg.checkout, the commit this turn installs; refresh_reason passes the
    installed generation (tart_pin.REPO_ROOT), which is checked out before
    the drain on a same-generation turn. `fresh` reads the tap itself instead
    of the hour-long cache: for a decision to drain and right before brew.

    None when this host does not upgrade Tart, Tart is absent or already the
    pin (and, on the canary, already proven), so an unknown never starts a
    drain. Records whether the host is due (tart-gate.json). Read-only apart
    from that record and the tap cache.
    """
    decision = _tart_decision(cfg, sys_, cfg.checkout if root is None else root, fresh)
    due = bool(cfg.tart_upgrade and decision is not None
               and not (decision.refusal and decision.refusal.startswith(_NOT_DUE)))
    try:
        tart_pin.write_gate(cfg.state_dir, {
            "upgrade": cfg.tart_upgrade, "due": due, "at": sys_.now(),
            "pin": decision.pin.version if decision else None,
            "refusal": decision.refusal if decision else None})
    except OSError:
        pass
    return decision


def _tart_decision(cfg: Config, sys_: System, root: Path, fresh: bool = False
                   ) -> TartDecision | None:
    if not cfg.tart_upgrade:
        return None
    run = _pin_run(sys_)
    installed = tart_pin.installed_tart(run)
    if installed is None:
        return None
    try:
        pin = tart_pin.load_pin(root)
    except tart_pin.PinError as exc:
        return TartDecision(installed, tart_pin.Pin("?", "", "?", ""), f"pin unreadable: {exc}")
    if tart_pin.vkey(installed) > tart_pin.vkey(pin.version):
        return TartDecision(installed, pin, f"Tart {installed} is newer than the pin "
                            f"{pin.version}; never downgraded here, move the pin or the host")
    failed = tart_pin.failure_for(cfg.state_dir, pin)
    if failed:
        return TartDecision(installed, pin, f"Tart {pin.version} failed here at "
                            f"{_iso(float(failed.get('at') or 0))}: "
                            f"{str(failed.get('reason'))[:200]}; not retried until the pin moves "
                            f"or {cfg.state_dir / tart_pin.FAILED} is removed")
    deferred = tart_pin.deferral_for(cfg.state_dir, pin, sys_.now())
    if deferred:
        return TartDecision(installed, pin, f"Tart {pin.version} deferred until "
                            f"{_iso(float(deferred['until']))}: {str(deferred.get('reason'))[:200]}")
    if installed != pin.version and tart_boot_target(cfg) is None:
        # Checked before the drain: a turn that could only refuse after it
        # would drain the host for nothing every attempt.
        return TartDecision(installed, pin, "the installed profile names no golden and "
                            "tart_home to boot-check Tart with")
    me = self_host_id(cfg)
    if cfg.tart_canary is None:
        refusal = "no canary: set [tart] canary in self-update.toml"
        return None if installed == pin.version else TartDecision(installed, pin, refusal)
    if installed == pin.version:
        if me != cfg.tart_canary:
            return None
        proven = tart_pin.read_verified(_read_json(cfg.state_dir / tart_pin.VERIFIED), sys_.now())
        if proven and (proven["version"], proven["sha256"]) == (pin.version, pin.sha256):
            return None
        return TartDecision(installed, pin, prove_only=True)
    if me != cfg.tart_canary:
        verified = read_tart_verified(cfg, sys_, cfg.tart_canary)
        if verified and "unreachable" in verified:
            return TartDecision(installed, pin, f"waiting for canary {cfg.tart_canary}: "
                                f"unreachable or unknown ({verified['unreachable']})")
        if not verified or (verified["version"], verified["sha256"]) != (pin.version, pin.sha256):
            return TartDecision(installed, pin, f"waiting for canary {cfg.tart_canary} to boot "
                                f"and serve Tart {pin.version}")
        if verified["age"] < cfg.tart_soak_seconds:
            return TartDecision(installed, pin, f"soaking for {cfg.tart_soak_seconds // 60} min: "
                                f"canary {cfg.tart_canary} verified Tart {pin.version} "
                                f"{int(verified['age'] // 60)} min ago")
    try:
        refusal = tart_pin.tap_refusal(pin, tart_pin.tap_offer(run, pin, cfg.state_dir,
                                                               sys_.now(), fresh=fresh))
    except tart_pin.PinError as exc:
        refusal = f"tap unreadable: {exc}"
    return TartDecision(installed, pin, refusal)


def tart_hold_refusal(cfg: Config, sys_: System) -> str | None:
    """Why no update may run while this host is held OFF for Tart, or None.

    An update would drain, install, `pool on` and verify on a Tart that may
    not run, and its success would clear host_off. A hold from an earlier
    episode is removed. A hold whose host is on again with a Tart that answers
    and is not the failed pin was fixed by hand: it is removed too.
    """
    import host_off  # noqa: PLC0415 - sibling module; owns the hold's validity
    path = cfg.state_dir / tart_pin.HOLD
    if not path.exists():
        return None
    if host_off.held(cfg.state_dir) is None:
        path.unlink(missing_ok=True)
        print("self-update: removed a Tart hold left from an earlier episode")
        return None
    status = installed_tartci(cfg, sys_, "pool", "status", "--json")
    try:
        pool_state = str(json.loads(status.out).get("state") or "unknown")
    except (json.JSONDecodeError, AttributeError):
        pool_state = "unknown"
    version = tart_pin.installed_tart(_pin_run(sys_))
    failed = (_read_json(cfg.state_dir / tart_pin.FAILED) or {}).get("version")
    if pool_state == "on" and version is not None and version != failed:
        path.unlink(missing_ok=True)
        print(f"self-update: Tart hold cleared: the pool is on and Tart {version} answers")
        return None
    return (f"host HELD OFF for Tart ({path}); no update runs until Tart is fixed: "
            f"{TART_RECOVERY}")


def tart_boot_target(cfg: Config) -> tuple[str, str] | None:
    """(golden, tart_home) to boot-check Tart with: the first lane's golden."""
    try:
        profile = _toml(cfg.installed_profile)
    except (OSError, ValueError):
        return None
    home = (profile.get("host") or {}).get("tart_home")
    goldens = [lane.get("golden") for lane in profile.get("lane") or [] if isinstance(lane, dict)]
    golden = next((g for g in goldens if isinstance(g, str) and g), None)
    return (golden, home) if golden and isinstance(home, str) and home else None


def refresh_reason(cfg: Config, sys_: System) -> str | None:
    """Why a current host must reinstall its own generation, or None.

    Today: its receipt no longer matches the OS-managed interpreter because
    macOS was updated (interpreter_changed_by_os_update), or the configured
    private fleet source moved to a commit it was not installed from.
    Read-only.
    """
    if cfg.fleet_source_repository is not None:
        selected = os.environ.get("TARTCI_FLEET_SOURCE_COMMIT", "").strip()
        installed = installed_fleet_source_commit(cfg)
        if selected and selected != installed:
            if installed and _same_source_bytes(cfg):
                # A commit that changed only other files in the private repo
                # (a backup, a README): the installed bytes are the selected
                # ones, so a reinstall would drain the host for nothing.
                print(f"self-update: fleet source moved to {selected[:12]} with the "
                      f"installed profiles and supply unchanged; no reinstall")
            else:
                return (f"fleet source moved to {selected[:12]} "
                        f"(installed from {_source_name(installed)})")
    status = installed_tartci(cfg, sys_, "pool", "status", "--json")
    try:
        problems = (json.loads(status.out).get("fleet") or {}).get("problems") or []
    except (json.JSONDecodeError, AttributeError):
        return None
    for problem in problems:
        if isinstance(problem, dict) and problem.get("code") == "interpreter_changed_by_os_update":
            return str(problem.get("detail") or problem["code"])
    # Tart is replaced only while the pool is off, so a host whose TartCI is
    # current takes a drained turn of its own for the pinned Tart, once its
    # gates (canary, soak, tap) let it. The installed generation's pin counts.
    decision = tart_decision(cfg, sys_, tart_pin.REPO_ROOT, fresh=True)
    if decision and decision.refusal:
        print(f"self-update: {decision.describe()}")
    elif decision:
        return f"{decision.describe()} while the pool is off"
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
                 helper: dict | None, approval: Path | None,
                 repository: str | None = None,
                 excluded: list[str] | None = None,
                 previous_fleet_source: dict[str, Any] | None = None,
                 auto_ack: bool = False) -> None:
        self.cfg, self.sys, self.receipt = cfg, sys_, receipt
        # The launcher change was acknowledged automatically, not by a person
        # waiting to grant Removable Volumes: a lost grant rolls back.
        self.auto_ack = auto_ack
        self.excluded = list(excluded or [])
        self.me, self.target, self.previous = me, target, previous
        self.profile, self.install_args, self.helper, self.approval = (
            profile, install_args, helper, approval)
        self.repository = repository
        self.previous_fleet_source = previous_fleet_source
        self.flag = ["--allow-last-serving-host"] if allow else []
        self.pin_path = Path(helper["approval_sha256_path"]) if helper else None
        self.pin_moved = False
        self.tart_before: tuple[TartDecision, dict[str, str | None]] | None = None
        self.tart_proven: tart_pin.Pin | None = None
        self.snapshot: Path | None = None
        self.phase = "announce"
        receipt.value.update(approval=str(approval) if approval else None,
                             pin_path=str(self.pin_path) if self.pin_path else None)

    def launch_set_args(self) -> list[str]:
        """The forward install's launch-set signing options, for a v2 rollback."""
        args = self.install_args
        try:
            return ["--launch-set-identity", args[args.index("--launch-set-identity") + 1],
                    "--launch-set-keychain", args[args.index("--launch-set-keychain") + 1]]
        except (ValueError, IndexError) as exc:
            raise Failed("a launcher v2 rollback has no launch-set signing options") from exc

    # ── entry ───────────────────────────────────────────────────────────
    def _record_skew_after_apply(self) -> None:
        """Rewrite skew.json for the generation this run just verified.

        skew.json is otherwise written only before the run, so a successful
        update left status reading the PRE-update skew until the watchdog's
        next refresh, which is never when the watchdog is not running. Checks
        are not re-queried: verification already proved this generation runs.
        Best effort: the update has succeeded whatever this does.
        """
        try:
            skew = measure_skew(self.cfg, self.sys, self.target, self.sys.now(),
                                verify_checks=False)
            skew["recorded_by"] = "verified_apply"
            _write_json(self.cfg.state_dir / "skew.json", skew)
            self.receipt.step("skew", render_skew(skew))
        except Exception as exc:  # noqa: BLE001 - never turn a success into a failure
            self.receipt.step("skew", f"not recorded: {type(exc).__name__}: {exc}", ok=False)

    def execute(self) -> int:
        import signal
        previous_handler = signal.signal(signal.SIGTERM, _raise_terminated)
        try:
            _announce(self.cfg, self.sys, self.me, self.target, self.excluded)
            self.receipt.step("announce", str(self.cfg.state_dir / "active.json"))
            # This host has its turn: whatever the update's outcome, it no
            # longer holds a place in the queue.
            clear_waiting(self.cfg, self.sys.now())
            self._snapshot()
            try:
                self._update()
            except LauncherConsent as exc:
                if self.auto_ack and not probe_slow_volume(str(exc)):
                    return self._roll_back_lost_consent(str(exc))
                return self._stop_for_consent(str(exc))
            except TartBroken as exc:
                return self._stop_tart_broken(str(exc))
            except Refused as exc:
                return self._safe_recover(f"refused after drain: {exc}", refused=True)
            except Failed as exc:
                return self._safe_recover(str(exc))
            except Terminated as exc:
                return self._safe_recover(f"terminated ({exc}) during {self.phase}",
                                          terminated=True)
            except Exception as exc:  # noqa: BLE001 - never leave the host out
                return self._safe_recover(f"{type(exc).__name__} during {self.phase}: {exc}")
            self._record_skew_after_apply()
            close_launcher_probe_alert(self.cfg)
            (self.cfg.state_dir / LAUNCHER_AUTO_APPLY_NAME).unlink(missing_ok=True)
            self.receipt.finish("succeeded")
            prune_dirs(self.cfg.state_dir / "rollback", KEEP_SNAPSHOTS)
            return EXIT_OK
        except Refused as exc:  # announce lost the race: nothing was touched
            self.receipt.step("refused", str(exc), ok=False)
            if isinstance(exc, Deferred):
                self.receipt.value["deferred"] = True
                self.receipt.finish("refused", str(exc))
                return defer(self.cfg, self.me, self.target, str(exc), self.sys.now())
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
        # The interpreter the running generation's lanes use, and its pin:
        # a rollback reinstalls with these, never with the new generation's.
        # m5s, 2026-10-15: the rollback rendered the old launcher against the
        # new pin and refused ("launch helper lanes are missing pinned
        # interpreter"), leaving the host off.
        pin = python_pin_path(self.cfg.home)
        previous_pin = pin.read_text().strip() if pin.is_file() else None
        _write_json(snap / "snapshot.json", {
            "previous": self.previous, "at": _iso(self.sys.now()),
            "python": installed_lane_python(self.cfg.home), "python_pin": previous_pin,
            "launcher_layout": installed_layout(self.cfg) if self.helper is not None else None})
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
        if installed_layout(self.cfg) == "v2":
            check = self.sys.run(["python3", "scripts/macos_launcher_identity.py", "verify-v2",
                                  str(snap / "TartCILauncher.app"),
                                  "--identifier", str(self.helper.get("identifier", "")),
                                  "--team-id", str(self.helper.get("team_id", "")),
                                  "--sha256", pin, "--profile", str(snap / "profile.toml")],
                                 cwd=str(self.cfg.checkout))
        else:
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
        if "--launch-helper-source" in self.install_args:
            # The drain can take an hour; check the store again right before
            # anything stops. A Refused here undoes the drain with nothing
            # installed.
            slow = store_volume_slow(cfg, sys_, receipt=self.receipt)
            if slow:
                raise Refused(f"Tart store volume slow after the drain, before pool off: {slow}")
            self.receipt.step("store-volume", "still fast enough after the drain")
        self.phase = "off"
        off = tartci(cfg, sys_, "pool", "off", *self.flag)
        if off.rc in POOL_OFF_REFUSALS:
            # pool off checked a precondition and stopped nothing (on m3 the
            # capacity census timed out). The drain is undone by the recovery,
            # so this is a refusal: it must not spend the 6 h attempt or count
            # toward the halt the way a failed install does.
            raise Refused(f"pool off refused (exit {off.rc}), nothing stopped: {off.text}")
        if off.rc != 0:
            raise Failed(f"pool off failed: {off.text}")
        self.receipt.step("off")
        self.phase = "tart-upgrade"
        self._tart_upgrade()
        if self.pin_path is not None and self.approval is not None:
            self.phase = "pin"
            self._write_pin(self.approval.read_text())
            self.pin_moved = True
            self.receipt.step("pin", "new launcher approval pinned (previous in the snapshot)")
            self.receipt.step("install-dry-run", "checking installer staging after the pin move; dry-run does not perform pin validation")
            dry = tartci(cfg, sys_, *self.install_args, repository=self.repository)
            if dry.rc != 0:
                self.receipt.step("install-dry-run", f"refused: {dry.text[:300]}", ok=False)
                raise Refused(f"sealed-launcher post-pin installer staging refused: {dry.text}")
            self.receipt.step("install-dry-run", "staging checks ok; pin validation runs in install --apply")
        self.phase = "install"
        self._install(self.install_args)
        self.receipt.step("install", f"applied {self.target[:12]}")
        self.phase = "relay"
        self._relay()
        self.phase = "pool-on"
        if not self._pool_on():
            if self.approval is not None and LAUNCH_PROBE_FAILED.search(
                    getattr(self, "pool_on_text", "") or ""):
                raise LauncherConsent(self.pool_on_text)
            raise Failed("pool on failed after install")
        self.phase = "verify"
        verify(cfg, sys_, self.target, self.receipt)
        self._publish_tart()
        self.phase = "support-agents"
        self._support_agents()
        self._vm_dhcp_verify()

    def _support_agents(self) -> None:
        """Converge the declared support agents with the installed generation.

        Non-fatal by design: the lanes are verified and serving by now, and a
        janitor's plist must not be able to take the serving generation back.
        A failure is a receipt step with ok=False, an event in the support
        agents' own log, and a tartci doctor fleet finding.
        """
        try:
            result = installed_tartci(self.cfg, self.sys, "fleet-macos", "support-agents",
                                      "auto", timeout=300)
        except Exception as exc:  # noqa: BLE001 - never fails the lane update
            self.receipt.step("support-agents", f"{type(exc).__name__}: {exc}", ok=False)
            return
        self.receipt.step("support-agents", result.text.strip()[-1500:], ok=result.rc == 0)

    def _vm_dhcp_verify(self) -> None:
        """Prove the VM network with one probe before the lanes clone freely.

        Non-fatal: the lanes are already verified and serving, and the
        breaker fails open if this cannot be written.
        """
        try:
            result = installed_tartci(self.cfg, self.sys, "vm-dhcp", "verify",
                                      "--reason", "self_update", timeout=60)
        except Exception as exc:  # noqa: BLE001 - never fails the lane update
            self.receipt.step("vm-dhcp-verify", f"{type(exc).__name__}: {exc}", ok=False)
            return
        self.receipt.step("vm-dhcp-verify", result.text.strip()[-500:], ok=result.rc == 0)

    def _tart_upgrade(self) -> None:
        """Move Tart to the pin while the pool is off, proven by a VM boot.

        Tart must not be replaced while a VM runs; after `pool off` none can,
        and a leftover Tart VM process refuses. The previous kegs stay
        installed: if the upgrade, its version, its Softnet or the boot check
        fails, the new kegs are removed so the previous ones are linked again,
        the failure is recorded so this pin is not retried here, and the
        TartCI update goes on. When the previous Tart does not come back the
        host stays OFF (TartBroken).
        """
        # The tap read uncached: the plan's answer may be an hour old.
        decision = tart_decision(self.cfg, self.sys, fresh=True)
        if decision is None:
            return
        if decision.refusal:
            self.receipt.step("tart-upgrade", decision.describe(), ok=False)
            return
        run, pin = _pin_run(self.sys), decision.pin
        running = self.sys.run(["pgrep", "-f", tart_pin.TART_RUN_PATTERN], timeout=10)
        if running.rc == 0:
            reason = (f"a Tart VM process was still running after pool off: "
                      f"{running.out.strip()[:200]}")
            # Found only after the drain: without a record the next turn
            # would drain again for the same refusal.
            tart_pin.record_deferral(self.cfg.state_dir, pin, self.sys.now(), reason)
            self.receipt.step("tart-upgrade", f"Tart {decision.installed} stays: {reason}; "
                              f"deferred for {tart_pin.DEFER_SECONDS // 3600} h", ok=False)
            return
        boot = tart_boot_target(self.cfg)
        if boot is None:
            self.receipt.step("tart-upgrade", f"Tart {decision.installed} stays: the installed "
                              "profile names no golden and tart_home to boot-check it with",
                              ok=False)
            return
        records = self.cfg.state_dir.parent / "scratch-vms"
        if decision.prove_only:
            failure, cleanup = tart_pin.boot_check(run, boot[0], boot[1], os.getpid(),
                                                   self.sys.now(), records, self.sys.sleep)
            self._tart_cleanup_step(cleanup)
            self.receipt.step("tart-upgrade", f"canary boot check of Tart {decision.installed}: "
                              + (failure or "booted"), ok=failure is None)
            if failure:
                tart_pin.record_failure(self.cfg.state_dir, pin, self.sys.now(), failure)
            else:
                self.tart_proven = pin
            return
        names = (pin.formula.rsplit("/", 1)[-1], pin.softnet_formula.rsplit("/", 1)[-1])
        self.tart_before = (decision, tart_pin.opt_links(names))
        # On disk first: a SIGKILL from here on leaves a record that
        # recover_interrupted holds the host OFF for, instead of pool on.
        _write_json(self.cfg.state_dir / tart_pin.PENDING,
                    {"installed": decision.installed, "pin": pin.version,
                     "before": self.tart_before[1], "at": self.sys.now()})
        upgrade = self.sys.run([tart_pin.BREW, "upgrade", pin.formula], env=tart_pin.BREW_ENV,
                               timeout=tart_pin.UPGRADE_TIMEOUT_S)
        after = tart_pin.installed_tart(run)
        softnet = tart_pin.opt_links(names[1:])[names[1]] or ""
        if upgrade.rc != 0:
            failure = f"brew upgrade exit {upgrade.rc}: {upgrade.text.strip()[-200:]}"
        elif after != pin.version:
            failure = f"brew installed Tart {after}, not {pin.version}"
        elif not softnet.endswith("/" + pin.softnet_version):
            failure = f"Softnet links {softnet or 'nothing'}, not {pin.softnet_version}"
        else:
            failure = self._tart_archive_failure(pin, softnet_moved=(
                self.tart_before[1][names[1]] != softnet))
        if failure is None:
            failure, cleanup = tart_pin.boot_check(run, boot[0], boot[1], os.getpid(),
                                                   self.sys.now(), records, self.sys.sleep)
            self._tart_cleanup_step(cleanup)
        if failure is None:
            self._tart_settled()
            self.receipt.step("tart-upgrade", f"Tart {decision.installed} -> {pin.version}, "
                              f"Softnet {pin.softnet_version}; a clone of {boot[0]} booted")
            self.tart_proven = pin
            return
        self.receipt.step("tart-upgrade", f"Tart {decision.installed} -> {pin.version} failed: "
                          f"{failure}", ok=False)
        tart_pin.record_failure(self.cfg.state_dir, pin, self.sys.now(), failure)
        self._tart_roll_back()

    def _tart_archive_failure(self, pin: tart_pin.Pin, softnet_moved: bool) -> str | None:
        """The archives brew downloaded must be the pinned bytes: a re-cut
        release under the same version has another sha256."""
        run = _pin_run(self.sys)
        checks = [(pin.formula, pin.sha256)]
        if softnet_moved:
            checks.append((pin.softnet_formula, pin.softnet_sha256))
        for formula, want in checks:
            digest, where = tart_pin.archive_sha256(run, formula)
            if digest is None:
                return f"could not hash the downloaded {formula} archive: {where}"
            if digest != want:
                return (f"the downloaded {formula} archive {where} has sha256 {digest[:12]}, "
                        f"the pin is {want[:12]}")
        return None

    def _tart_settled(self) -> None:
        self.tart_before = None
        (self.cfg.state_dir / tart_pin.PENDING).unlink(missing_ok=True)

    def _tart_cleanup_step(self, cleanup: str | None) -> None:
        if cleanup:
            self.receipt.step("tart-check-cleanup", cleanup, ok=False)

    def _tart_roll_back(self) -> None:
        """Bring back the Tart and Softnet linked before the upgrade, or raise
        TartBroken. Tart first, then Softnet."""
        decision, before = self.tart_before
        pin = decision.pin
        run = _pin_run(self.sys)
        problems, restored = [], []
        now = tart_pin.opt_links(tuple(before))
        for formula in (pin.formula, pin.softnet_formula):
            name = formula.rsplit("/", 1)[-1]
            if before[name] and now[name] != before[name]:
                problem = tart_pin.roll_back(run, formula)
                if problem:
                    problems.append(problem)
                elif tart_pin.opt_links((name,))[name] != before[name]:
                    problems.append(f"{name} links {tart_pin.opt_links((name,))[name]} after the "
                                    f"rollback, not {before[name]}")
                else:
                    restored.append(f"{name} {before[name]}")
        back = tart_pin.installed_tart(run)
        self._tart_settled()
        if problems or back != decision.installed:
            detail = (f"Tart is {back or 'NOT runnable'} now, expected {decision.installed}"
                      + (f": {'; '.join(problems)}" if problems else ""))
            self.receipt.step("tart-rollback", detail, ok=False)
            raise TartBroken(f"Tart rollback failed: {detail}")
        self.receipt.step("tart-rollback",
                          (f"restored {', '.join(restored)}; " if restored else
                           "nothing was relinked; ") + f"Tart {back} answers")

    def _publish_tart(self) -> None:
        """On the canary, once the pool is verified on the proven Tart: tell
        the fleet, which adopts it after the soak."""
        pin = self.tart_proven
        if pin and self.cfg.tart_canary == self.me:
            tart_pin.published_verified(self.cfg.state_dir, pin, self.sys.now())
            self.receipt.step("tart-verified", f"Tart {pin.version} published for the fleet")

    def _wait_idle(self, *, allow_now: bool = False) -> None:
        started = self.sys.now()
        deadline = started + self.cfg.wait_seconds
        waiting_on: list[str] | None = None
        noted = started
        while True:
            plan = tartci(self.cfg, self.sys, "pool", "off", "--plan", *self.flag)
            if plan.rc == 0:
                break
            if plan.rc in POOL_OFF_REFUSALS and plan.rc != 12:
                # A capacity refusal stops nothing: the drain is undone by the
                # recovery, so it must not spend the attempt or count toward
                # the halt. m1 on 2026-10-03 drained, waited 28 min, got exit
                # 11 here, and recorded a failed attempt.
                raise Refused(f"pool off --plan refused (exit {plan.rc}), nothing stopped: "
                              f"{plan.text[:300]}")
            if plan.rc != 12:
                raise Failed(f"pool off --plan refused (exit {plan.rc}): {plan.text[:300]}")
            # Say what the drain waits on, once per change: a receipt that
            # reads only "drain" for half an hour looks stuck (m1, 2026-10-16,
            # waiting on one Forge gate job).
            busy = mid_job_lanes(f"{plan.out}\n{plan.err}")
            lanes = "; ".join(busy) or "an owned lane (not named)"
            if busy != waiting_on:
                waiting_on = busy
                noted = self.sys.now()
                self.receipt.step("wait-idle", "waiting on " + lanes)
            elif self.sys.now() - noted >= WAIT_IDLE_NOTE_SECONDS:
                # Unchanged, but say so: m1 on 2026-10-22 waited on one job
                # with steps at 08:00 and 08:55 and nothing between, which
                # reads as a hung update.
                noted = self.sys.now()
                self.receipt.step("wait-idle", f"still waiting on {lanes}, "
                                  f"{int((noted - started) // 60)} min")
            if self.sys.now() >= deadline:
                drain_timeout_alert(self.cfg, self.me, busy, self.sys.now() - started,
                                    self.sys.now())
                raise Failed(f"a lane was still mid-job after {self.cfg.wait_seconds}s: "
                             + ("; ".join(busy) or "(not named)"))
            self.sys.sleep(self.cfg.poll_seconds)
        drain_timeout_alert(self.cfg, self.me, None, self.sys.now() - started, self.sys.now())
        self.receipt.step("wait-idle", "no owned lane mid-job")

    def _writer_fence(self) -> list[str]:
        """The `shipyard writer-domain-exec` prefix for a write to ~/.local/bin.

        The install writes ~/.local/bin/tartci, inside Shipyard's protected
        writer domain. On 2026-10-02 m3's install landed in the middle of a
        Shipyard Sandbox E2E audit on the same host, which attributed the
        write to the sandbox and failed. Shipyard's own command takes the
        writer domain SHARED around the child (the audit holds it EXCLUSIVE),
        so the protocol is not copied here to drift. A host without Shipyard,
        or with one too old to have the command, has no audit to fence.
        """
        shipyard = self.cfg.home / ".local" / "bin" / "shipyard"
        if not shipyard.is_file():
            return []
        probe = self.sys.run([str(shipyard), "writer-domain-exec", "--help"], timeout=30)
        if probe.rc != 0:
            return []
        return [str(shipyard), "writer-domain-exec", "--path", str(self.cfg.shim), "--"]

    def _install(self, args: list[str], python: str | None = None) -> None:
        fence = self._writer_fence()
        env = census_env(self.repository)
        if python:
            # Only a rollback names one. TARTCI_ROLLBACK_PYTHON outranks the
            # snapshot profile's host.python, which may name the switch the
            # preflight refused; an older installer reads TARTCI_PYTHON only.
            env["TARTCI_PYTHON"] = python
            if python.startswith("/"):
                env["TARTCI_ROLLBACK_PYTHON"] = python
        for attempt in range(1, INSTALL_ATTEMPTS + 1):
            result = self.sys.run_critical([*fence, "./tartci", *args, "--apply"],
                                           cwd=str(self.cfg.checkout), env=env,
                                           timeout=INSTALL_TIMEOUT,
                                           record=self.cfg.state_dir / "installer.json")
            if result.rc == 0:
                return
            if fence and result.rc == WRITER_DOMAIN_OVERLAP_EXIT and WRITER_DOMAIN_OVERLAP in result.text:
                # Nothing was written: the child never started. Defer to a
                # later cycle rather than spend the attempt.
                self.receipt.step("writer-lease", result.text[:300], ok=False)
                raise WriterDomainBusy(f"{WRITER_DOMAIN_OVERLAP}: a Shipyard sandbox audit owns "
                                       f"the writer domain; install deferred: {result.text[:200]}")
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

    def _pool_on(self, *, restore_receipt: bool = False) -> bool:
        for attempt in range(1, self.POOL_ON_ATTEMPTS + 1):
            args = ["pool", "on"]
            if restore_receipt:
                args.append("--restore-receipt")
            on = installed_tartci(self.cfg, self.sys, *args)
            self.pool_on_text = on.text
            if on.rc == 0:
                self.receipt.step("pool-on", "installed shim" +
                                  ("; restored prior receipted serving set without fresh relay proof"
                                   if restore_receipt else ""))
                return True
            self.receipt.step("pool-on", f"attempt {attempt} failed (exit {on.rc}): {on.text[:300]}",
                              ok=False)
            if attempt < self.POOL_ON_ATTEMPTS:
                self.sys.sleep(self.POOL_ON_BACKOFF[attempt - 1])
        return False

    def _reinstall_previous(self, *, failed_source_env: dict[str, str | None] | None = None) -> None:
        """Reinstall the snapshot generation from its own commit (pin + bundle)."""
        cfg, sys_ = self.cfg, self.sys
        current_env = fleet_source_env_snapshot()
        try:
            restore_fleet_source_env(current_env |
                                     {name: fleet_source_env(self.previous_fleet_source).get(name)
                                      for name in FLEET_SOURCE_ENV})
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
                # The layout the snapshot ran, explicitly: a v1 -> v2 move rolls
                # back to v1 with the opt-in still present. A v2 rollback names
                # the signing keychain, which the installer uses only when the
                # previous generation's signed set cannot simply be pointed at
                # again.
                snap = _read_json(self.snapshot / "snapshot.json") or {}
                layout = (snap.get("launcher_layout")
                          if snap.get("launcher_layout") in ("v1", "v2") else "v1")
                args += ["--launcher-layout", layout]
                if layout == "v2":
                    args += self.launch_set_args()
            self._install(args, python=self._restore_previous_python())
        except BaseException:
            # The failed target may still be live. Keep its source selected so
            # recovery helpers cannot reinstall the old code with the new
            # generation's profile source.
            restore_fleet_source_env(failed_source_env or current_env)
            raise

    def _restore_previous_python(self) -> str | None:
        """Pin the interpreter the previous generation's lanes ran (else its
        old pin) and return it, for the rollback install."""
        snap = (_read_json(self.snapshot / "snapshot.json") if self.snapshot else None) or {}
        previous = snap.get("python") if isinstance(snap.get("python"), str) else None
        pin_value = snap.get("python_pin") if isinstance(snap.get("python_pin"), str) else None
        pin = python_pin_path(self.cfg.home)
        # The pin names what the rollback installs: the installer honours
        # TARTCI_ROLLBACK_PYTHON only when it equals the pin or a lane's.
        restore = previous if previous and previous.startswith("/") else pin_value
        if restore:
            pin.parent.mkdir(parents=True, exist_ok=True)
            tmp = pin.with_name(f".{pin.name}.rollback")
            tmp.write_text(restore + "\n")
            os.replace(tmp, pin)
        if restore or previous:
            self.receipt.step("python-restore",
                              f"rollback runs the previous interpreter {restore or previous}")
        return restore or previous

    def _pool_state(self) -> str:
        status = installed_tartci(self.cfg, self.sys, "pool", "status", "--json")
        try:
            return str(json.loads(status.out).get("state") or "unknown")
        except (json.JSONDecodeError, AttributeError):
            return "unknown"

    def _ensure_on(self, *, allow_reinstall: bool = True,
                   restore_receipt: bool = False,
                   failed_previous_source_env: dict[str, str | None] | None = None) -> str:
        """Put the host back in service, whatever else failed. Returns the
        resulting pool state: "on", "undrained", or what the host is left in.

        `pool on` first. If it refuses (e.g. a receipt it rejects after a
        macOS update), the running generation is reinstalled from its own
        commit, which rewrites that receipt, and `pool on` is tried again. A
        reinstall needs the pool off, so a draining host is first waited idle
        and taken off; if it never goes idle, it is UNDRAINED instead (admission
        reopened, agents re-enabled), which restores exactly the service it had
        and never strands it drained. `allow_reinstall=False` (a terminated
        run, with launchd's SIGKILL pending) tries `pool on` only.
        """
        if self._pool_on(restore_receipt=restore_receipt):
            return "on"
        state = self._pool_state()
        if not allow_reinstall:
            return self._undrain_or(state)
        running = self._running()
        if running not in (self.previous, self.target):
            return self._undrain_or(state)
        if state == "draining":
            try:
                self._wait_idle()
                off = tartci(self.cfg, self.sys, "pool", "off", *self.flag)
                if off.rc != 0:
                    raise Failed(f"pool off before reinstall failed: {off.text[:200]}")
            except Failed as exc:
                self.receipt.step("reinstall-for-pool-on", f"not reinstalled: {exc}", ok=False)
                return self._undrain_or(self._pool_state())
        try:
            if running == self.previous and self.snapshot is not None:
                self.receipt.step("reinstall-for-pool-on",
                                  f"pool on refused; reinstalling {self.previous[:12]}")
                self._reinstall_previous(failed_source_env=failed_previous_source_env)
            else:
                self.receipt.step(
                    "reinstall-for-pool-on",
                    f"pool on refused; reinstalling {self.target[:12]}, the generation that "
                    "FAILED verification, only because it is what this host runs now and "
                    "serving on it beats leaving the host off")
                _checkout(self.cfg, self.sys, self.target)
                manifest = tartci(self.cfg, self.sys, "support-manifest", "write", "--root", ".",
                                  "--output", ".tartci-support-manifest.json")
                if manifest.rc != 0:
                    raise Failed(f"support-manifest for {self.target[:12]}: {manifest.text[:200]}")
                self._install(self.install_args)
        except Exception as exc:  # noqa: BLE001 - recorded; the host stays as it is
            self.receipt.step("reinstall-for-pool-on", f"{type(exc).__name__}: {exc}", ok=False)
            return self._pool_state()
        return ("on" if self._pool_on(restore_receipt=restore_receipt)
                else self._pool_state())

    def _undrain_or(self, state: str) -> str:
        if state != "draining":
            return state
        result = tartci(self.cfg, self.sys, "pool", "undrain")
        if result.rc == 0:
            self.receipt.step("undrain", "pool on refused; undrained back to serving")
            return "undrained"
        self.receipt.step("undrain", f"failed (exit {result.rc}): {result.text[:200]}", ok=False)
        return self._pool_state()

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
                if self._running() == self.previous:
                    self._restore_previous_source_env()
                on = self._pool_on()
            except BaseException:  # noqa: BLE001
                pass
            return self._terminal("failed", pool_state="on" if on else self._pool_state_quiet(), message=f"{reason}; RECOVERY INTERRUPTED "
                                  f"({type(exc).__name__}: {exc}); host is "
                                  f"{'on' if on else 'possibly OFF'} running "
                                  f"{str(self._running())[:12]}; run tartci fleet-macos "
                                  "self-update --verify`")

    def _pool_state_quiet(self) -> str:
        try:
            return self._pool_state()
        except BaseException:  # noqa: BLE001
            return "unknown"

    def _repin_to_live(self) -> None:
        """Make the approval pin match whichever launcher bundle is actually live."""
        if self.pin_path is None or self.snapshot is None:
            return
        running = self._running()
        if running == self.target and self.approval is not None:
            self._write_pin(self.approval.read_text())
        elif running == self.previous:
            self._write_pin((self.snapshot / "approved.sha256").read_text())

    def _restore_previous_source_env(self) -> None:
        """Make recovery helpers use the source recorded for the live rollback."""
        restore_fleet_source_env({
            name: fleet_source_env(self.previous_fleet_source).get(name)
            for name in FLEET_SOURCE_ENV
        })
        self.receipt.step("source-restore", "previous installed source selected for recovery")

    def _running(self) -> str | None:
        try:
            return installed_commit(self.cfg)[0]
        except Refused:
            return None

    def _stop_for_consent(self, probe: str) -> int:
        """Stop with the new launcher installed and the host OFF; never roll back
        (a person acknowledged this change, or the store volume was slow).

        Rolling back replaces TartCILauncher.app a second time, which can reset
        its Removable Volumes consent again; the new bundle stays in place and
        a person grants the consent, then turns the pool on.
        """
        self.receipt.step("failed", f"pool on failed after installing a new launcher: {probe[:500]}",
                          ok=False)
        self._repin_to_live()
        launcher_probe_alert(self.cfg, self.me, self.target, probe, self.sys.now())
        if probe_slow_volume(probe):
            next_step = ("The Tart store volume was slow (the probe's control write was slow "
                         "too); once writes to it are fast again, run `tartci pool on`.")
        else:
            next_step = ("Grant Removable Volumes to TartCI Launcher (System Settings > "
                         "Privacy & Security > Files and Folders), then run `tartci pool on`.")
        return self._terminal(
            "failed",
            f"launch helper volume probe failed after installing a new launcher for "
            f"{self.target[:12]}; STOPPED and NOT rolled back (a rollback replaces the launcher "
            f"again and can reset its consent). {next_step} Probe: {probe[:300]}",
            pool_state=self._pool_state_quiet())

    def _roll_back_lost_consent(self, probe: str) -> int:
        """An automatically acknowledged launcher failed its volume probe: no
        person is standing by to grant Removable Volumes, so put the previous
        generation (its byte-identical launcher, which keeps its grant) back,
        hold this target, and say what the owner does to take it by hand.
        """
        ack = self.receipt.value.get("launcher_ack") or {}
        ack["outcome"] = "probe_failed_rolled_back"
        self.receipt.value["launcher_consent_lost"] = True
        hold_launcher_auto_apply(self.cfg, self.sys, self.target, "probe_failed", probe,
                                 str(ack.get("key") or self.target))
        launcher_probe_alert(self.cfg, self.me, self.target, probe, self.sys.now(),
                             rolled_back=self.previous)
        return self._safe_recover(
            f"launcher consent lost: the automatically applied TartCI Launcher for "
            f"{self.target[:12]} failed its Removable Volumes probe; {REMOVABLE_VOLUMES_GRANT} "
            f"once it installs (tartci pool status prints the command that installs it); "
            f"auto-apply holds this target. Probe: {probe[:200]}")

    def _stop_tart_broken(self, reason: str) -> int:
        """Stop with the pool OFF: the Tart upgrade failed and its rollback did
        not bring the previous Tart back. No pool on, no TartCI rollback (it
        never touches Tart); counted toward the halt, with an incident."""
        self._repin_to_live()
        message = f"{reason}; host HELD OFF: {TART_RECOVERY}"
        rc = self._terminal("failed", message, pool_state=self._pool_state_quiet())
        # After the terminal record, whose time names this episode. Without
        # the hold, host_off's recovery (watchdog, next self-update) and the
        # next update would turn the pool on within minutes on a Tart that
        # may not run.
        tart_pin.hold(self.cfg.state_dir, self.sys.now(), message)
        return rc

    def _recover(self, reason: str, *, refused: bool = False, terminated: bool = False) -> int:
        self.receipt.step("refused" if refused else "failed", reason, ok=False)
        if self.tart_before and terminated:
            # launchd may SIGKILL soon: a brew rollback can outlive that.
            # Hold the host OFF quickly instead; the operator rolls back.
            return self._stop_tart_broken(f"{reason}; interrupted mid Tart upgrade, not rolled "
                                          "back (terminated)")
        if self.tart_before:
            # Interrupted mid Tart upgrade by an error: put the previous Tart
            # back before anything turns the pool on, or stay OFF.
            try:
                self._tart_roll_back()
            except TartBroken as exc:
                return self._stop_tart_broken(f"{reason}; {exc}")
        running = self._running()
        if terminated:
            # launchd will SIGKILL soon: no reinstall, no rollback. Get the
            # host serving and say exactly what it runs.
            self._repin_to_live()
            state = self._ensure_on(allow_reinstall=False)
            return self._terminal("failed", f"{reason}; NOT rolled back (terminated); host is "
                                  f"{_state_words(state)} running {str(running)[:12]}; run "
                                  "tartci fleet-macos self-update --verify to check it",
                                  pool_state=state)
        # A source move reinstalls the same generation, so the generation alone
        # cannot say whether the moved profiles are live.
        source_moved = installed_fleet_source_record(self.cfg) != self.previous_fleet_source
        if running == self.previous and not source_moved:
            # Nothing new is installed (the installer rolls its own failure
            # back), so putting the host back is: pin, then pool on.
            failed_previous_source_env = fleet_source_env_snapshot()
            if self.pin_moved:
                self._write_pin((self.snapshot / "approved.sha256").read_text())
                self.receipt.step("pin-restore", "previous approval restored from the snapshot")
            self._restore_previous_source_env()
            state = self._ensure_on(restore_receipt=True,
                                    failed_previous_source_env=failed_previous_source_env)
            if state not in ("on", "undrained"):
                return self._terminal("failed", f"{reason}; HOST LEFT {state.upper()}: pool on "
                                      f"failed (generation {self.previous[:12]} is installed)",
                                      pool_state=state)
            if refused:
                self.receipt.value["pool_state"] = state
                self.receipt.finish("refused", reason)
                return EXIT_REFUSED
            return self._terminal("failed", f"{reason}; host {_state_words(state)} on the "
                                  f"previous generation {self.previous[:12]}", pool_state=state)
        return self._rollback(reason)

    def _rollback(self, reason: str) -> int:
        cfg, sys_ = self.cfg, self.sys
        self.receipt.step("rollback", f"reinstalling {self.previous[:12]} from {self.snapshot}")
        rollback_install_ok = False
        rollback_pin_ok = self.helper is None
        rollback_pool_on_ok = False
        try:
            self._wait_idle()
            off = tartci(cfg, sys_, "pool", "off", *self.flag)
            if off.rc != 0:
                raise Failed(f"pool off for rollback failed: {off.text}")
            self._reinstall_previous()
            rollback_install_ok = True
            rollback_pin_ok = True
            self.receipt.step("rollback-install", f"reinstalled {self.previous[:12]}")
            if not self._pool_on(restore_receipt=True):
                raise Failed("pool on after rollback failed")
            rollback_pool_on_ok = True
            verify(cfg, sys_, self.previous, self.receipt)
        except BaseException as exc:  # noqa: BLE001 - incl. a second SIGTERM
            # Whatever the rollback's own failure, capacity comes back: a
            # failed verification is never a reason to leave the host out.
            self._repin_to_live()
            state = self._ensure_on(allow_reinstall=not isinstance(exc, Terminated),
                                    restore_receipt=True)
            rollback_verify_timeout = (
                isinstance(exc, Failed)
                and "verification of" in str(exc)
                and "heartbeat_missing" in str(exc)
            )
            if rollback_install_ok and rollback_pin_ok and rollback_pool_on_ok and rollback_verify_timeout:
                message = (f"{reason}; rollback installed and pool on; rollback verification "
                           f"timed out: {type(exc).__name__}: {exc}; host is "
                           f"{_state_words(state)} running {str(self._running())[:12]}")
            else:
                message = (f"{reason}; ROLLBACK FAILED: {type(exc).__name__}: {exc}; host is "
                           f"{_state_words(state)} running {str(self._running())[:12]}")
            return self._terminal("failed", message, pool_state=state)
        return self._terminal("rolled_back", _rollback_terminal_message(reason, self.previous))

    def _terminal(self, status: str, message: str, *, pool_state: str = "on") -> int:
        self.receipt.value["pool_state"] = pool_state
        self.receipt.value["host_off"] = pool_state not in ("on", "undrained")
        self.receipt.step(status, message, ok=False)
        self.receipt.finish(status, message)
        if self.receipt.value["host_off"]:
            # Do not wait for the watchdog's 15-minute threshold: a roll that
            # leaves a gate host OFF needs an issue immediately, with the
            # failure reason and the recovery command in its body.
            try:
                import host_off  # noqa: PLC0415 - owns the alert/issue format
                try:
                    host = self_host_id(self.cfg)
                except (Refused, OSError, ValueError):
                    host = os.uname().nodename.split(".")[0]
                host_off.alert(host_off.state_dir(self.cfg.home),
                               host_off.pool_state_file(self.cfg.home),
                               host=host, now=self.sys.now(),
                               immediate=True)
            except Exception as exc:  # noqa: BLE001 - preserve the terminal receipt
                print(f"self-update: host-off alert failed: {exc}", file=sys.stderr)
        print(f"self-update: {status.upper().replace('_', ' ')}: {message}", file=sys.stderr)
        return EXIT_FAILED


def _announce(cfg: Config, sys_: System, me: str, target: str,
              excluded: list[str] | None = None) -> None:
    marker = cfg.state_dir / "active.json"
    announced = sys_.now()
    _write_json(marker, {"host_id": me, "target": target, "ts": announced,
                         "pid": os.getpid(), "pid_start": sys_.process_start(os.getpid())})
    # Re-read peers AFTER announcing and after a settle long enough that a peer
    # announcing close behind is visible. Between two self-updating hosts the
    # EARLIER announcement proceeds; only markers within ANNOUNCE_TIE_SECONDS
    # of each other fall back to the lower host id. The id alone was not
    # enough: a lower-id host announcing minutes after a peer had re-checked
    # and moved on also proceeded, and both drained.
    sys_.sleep(ANNOUNCE_SETTLE_SECONDS)
    for peer, ssh_target in published_peers(cfg, sys_).items():
        if peer == me:
            continue
        info = read_peer(cfg, sys_, peer, ssh_target)
        if not info["busy"]:
            continue
        # Only a peer this run's survey excluded, and only while it is still
        # unreadable: one that answers now goes through the protocol below.
        if peer in (excluded or ()) and not info["readable"]:
            continue
        theirs = info["active_age"]
        if theirs is None:
            marker.unlink(missing_ok=True)
            raise Deferred(f"peer changed after announcing: {info['evidence']}")
        mine = max(0.0, sys_.now() - announced)
        if theirs > mine + ANNOUNCE_TIE_SECONDS or (
                abs(theirs - mine) <= ANNOUNCE_TIE_SECONDS and peer < me):
            marker.unlink(missing_ok=True)
            raise Deferred(f"peer announced first (or won the tie): {info['evidence']}")


def _verify_once(cfg: Config, sys_: System, target: str) -> tuple[list[str], bool, list[str]]:
    """(problems, settling). settling: every problem is a lane that has not yet
    written its first heartbeat, which a freshly loaded supervisor needs a few
    seconds to do."""
    problems = []
    settling = False
    heartbeat_labels: list[str] = []
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
            found = [p for p in fleet.get("problems") or []
                     if not (isinstance(p, dict) and p.get("code") in HOST_CONDITION_CODES)]
            if found or not fleet.get("problems"):
                problems.append(f"fleet not ready: {found}")
            heartbeat_labels = [str(p.get("label")) for p in found
                                if isinstance(p, dict)
                                and p.get("code") == "heartbeat_missing"
                                and p.get("label")]
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
        problems.append(f"installed tartci launchd guard missing or wrong "
                        f"(block={guard.rc}, allow={allow.rc})")
    return problems, settling and len(problems) == 1, heartbeat_labels


def _heartbeat_failure_diagnostics(sys_: System, labels: list[str]) -> list[str]:
    """Include a failed lane's final log line when launchd reports a crash."""
    details: list[str] = []
    domain = f"gui/{os.getuid()}"
    for label in labels:
        try:
            result = sys_.run(["launchctl", "print", f"{domain}/{label}"], timeout=10)
        except Exception:
            continue
        if result.rc != 0:
            continue
        exit_match = re.search(r"^\s*last exit (?:code|status)\s*=\s*(-?\d+)\s*$",
                               result.out, re.MULTILINE)
        if not exit_match or int(exit_match.group(1)) == 0:
            continue
        path_match = re.search(r"^\s*(?:stderr|stdout) path\s*=\s*(\S+)\s*$",
                               result.out, re.MULTILINE)
        if not path_match:
            continue
        try:
            lines = Path(path_match.group(1)).read_text(errors="replace").splitlines()
        except OSError:
            continue
        if lines:
            details.append(f"lane {label} last exit {exit_match.group(1)}; "
                           f"last log line: {lines[-1]}")
    return details


def _rollback_terminal_message(reason: str, previous: str) -> str:
    if "verification of" in reason and "heartbeat_missing" in reason:
        return (f"rollback installed; verification timed out; rolled back to "
                f"{previous[:12]} and verified")
    return f"{reason}; rolled back to {previous[:12]} and verified"


def verify(cfg: Config, sys_: System, target: str, receipt: Receipt) -> None:
    """Pool on, fleet ready, executing `target`, guard present.

    Verification runs seconds after `pool on`, before a just-loaded supervisor
    has written its first heartbeat. A result whose only problem is that is
    re-read until VERIFY_SETTLE_SECONDS elapse; anything else fails at once.
    """
    deadline = sys_.now() + VERIFY_SETTLE_SECONDS
    while True:
        problems, settling, heartbeat_labels = _verify_once(cfg, sys_, target)
        if not problems:
            break
        if not settling or sys_.now() >= deadline:
            problems.extend(_heartbeat_failure_diagnostics(sys_, heartbeat_labels))
            raise Failed(f"verification of {target[:12]}: " + "; ".join(problems))
        sys_.sleep(VERIFY_SETTLE_POLL_SECONDS)
    receipt.step("verify", f"pool on and ready, executing {target[:12]}, guard present")


def tart_status_line(state_dir: Path) -> str | None:
    """Tart differing from its pin, from tool-freshness's cached measurement."""
    tools = (_read_json(state_dir.parent / "tool-freshness" / "state.json") or {}).get("tools") or {}
    row = tools.get("tart") if isinstance(tools, dict) else None
    if not isinstance(row, dict) or row.get("state") != "behind":
        return None
    return (f"self-update: Tart {row.get('installed')} is behind its pin {row.get('latest_tag')} "
            "(fleet/tart.toml); with [tart] upgrade = true a drained self-update moves it"
            + (" (STALE)" if row.get("stale") else ""))


def running_line(state_dir: Path, sys_: System | None = None) -> str | None:
    """The self-update running on this host now, with its latest step.

    m1, 2026-10-22: an apply sat 20 min waiting for its VMs to drain, and
    neither --status nor pool status said one was running; it was found
    only by searching the process list.
    """
    sys_ = sys_ or System()
    for path, value in reversed(_attempts(state_dir)):
        if value.get("mode") != "apply" or value.get("status") != "running":
            continue
        steps = value.get("steps") or []
        last = steps[-1] if steps and isinstance(steps[-1], dict) else {}
        where = (f"; last step {last.get('step')} at {last.get('at')}"
                 + (f": {str(last.get('detail'))[:160]}" if last.get("detail") else "")
                 if last else "")
        if _alive(sys_, value.get("pid"), value.get("pid_start")):
            return (f"self-update: IN PROGRESS to {str(value.get('target'))[:12]} since "
                    f"{value.get('started_at')} (pid {value.get('pid')}){where}")
        return (f"self-update: INTERRUPTED to {str(value.get('target'))[:12]} (started "
                f"{value.get('started_at')}, pid {value.get('pid')} gone){where}; the next "
                f"attempt recovers it (receipt {path.name})")
    return None


def status_lines(state_dir: Path, sys_: System | None = None) -> list[str]:
    """For pool status / doctor / watchdog: skew, a running update, and the
    last attempt."""
    skew = _read_json(state_dir / "skew.json")
    aged = skew_stale_note(skew)
    lines = [render_skew(skew) + (f" {aged}" if aged else "")]
    running = running_line(state_dir, sys_)
    if running:
        lines.append(running)
    tart = tart_status_line(state_dir)
    if tart:
        lines.append(tart)
    paused = paused_line(state_dir)
    if paused:
        lines.append(paused)
    scheduled = scheduled_pause(state_dir)
    if scheduled:
        lines.append(scheduled)
    consent_lost = launcher_auto_apply_line(state_dir)
    if consent_lost:
        lines.append(consent_lost)
    last = _read_json(state_dir / "last.json")
    if last and last.get("status") in ("failed", "rolled_back"):
        word = "FAILED" if last["status"] == "failed" else "ROLLED BACK"
        lines.append(f"self-update: LAST ATTEMPT {word} at {last.get('at')} for "
                     f"{str(last.get('target'))[:12]}: {last.get('error')}")
    if last and last.get("host_off"):
        lines.append(f"self-update: THIS HOST WAS LEFT {str(last.get('pool_state') or 'off').upper()} "
                     "(pool on failed after a failed update); check tartci pool status, "
                     "then tartci pool on")
    progress = progress_line(state_dir)
    if progress:
        lines.append(progress)
    waiting = waiting_line(state_dir)
    if waiting:
        lines.append(waiting)
    signing = signing_blocked(state_dir)
    if signing:
        lines.append(signing)
    halted = halt_reason(state_dir)
    if halted:
        lines.append(halted)
    return lines


def signing_blocked(state_dir: Path) -> str | None:
    """The run of most recent attempts refused at signing, as one line.

    m5studio refused 17 times in a row at the signing probe, about 9 h, and
    no status surface said so. Any other outcome ends the run.
    """
    run = []
    for _, value in reversed(_attempts(state_dir)):
        if value.get("mode") != "apply":
            continue
        error = str(value.get("error") or "")
        at_signing = "SIGNING KEYCHAIN LOCKED" in error or "cannot sign unattended" in error
        if value.get("status") == "refused" and at_signing:
            run.append(value)
            continue
        break
    if not run:
        return None
    return (f"self-update: SIGNING KEYCHAIN LOCKED: {len(run)} refusal(s) in a row since "
            f"{run[-1].get('started_at')}; run `pulp ship doctor` on this host "
            f"(last: {str(run[0].get('error'))[:160]})")


# The steps of an update after its preflight, in order, as Receipt.step names
# them. A sealed-launcher host adds pin and install-dry-run before install.
UPDATE_PHASES = ("snapshot", "drain", "wait-idle", "off", "install", "relay", "pool-on",
                 "verify", "support-agents")


def progress(state_dir: Path, now: float | None = None) -> dict | None:
    """The update running on this host now, or None.

    {"target", "stale", "started", "minutes", "step", "step_minutes",
    "detail", "next"}. stale means active.json names a process that is gone.
    """
    active = _read_json(state_dir / "active.json")
    if not isinstance(active, dict):
        return None
    now = time.time() if now is None else now
    value: dict[str, Any] = {"target": str(active.get("target") or ""), "stale": False,
                             "started": None, "minutes": 0, "step": None,
                             "step_minutes": 0, "detail": "", "next": []}
    pid = active.get("pid")
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            value["stale"] = True
            value["pid"] = pid
            return value
        except PermissionError:
            pass
    running = next((attempt for _, attempt in reversed(_attempts(state_dir))
                    if attempt.get("mode") == "apply" and attempt.get("status") == "running"),
                   None)
    steps = (running or {}).get("steps") or []
    if not steps:
        return value
    name = str(steps[-1].get("step"))
    since = steps[-1].get("at")
    for step in reversed(steps):  # a repeated step (wait-idle) counts from its first
        if step.get("step") != name:
            break
        since = step.get("at")
    value.update(
        started=(running or {}).get("started_at"),
        minutes=int(max(0.0, now - _ts((running or {}).get("started_at"))) // 60),
        step=name, step_minutes=int(max(0.0, now - _ts(since)) // 60),
        detail=str(steps[-1].get("detail") or ""),
        next=[phase for phase in UPDATE_PHASES[UPDATE_PHASES.index(name) + 1:]
              if phase in ("off", "install", "pool-on", "verify")]
        if name in UPDATE_PHASES else [])
    return value


def progress_line(state_dir: Path, now: float | None = None) -> str | None:
    """progress() as one status line. A drained host with 0 supervisors reads
    as broken; this says which step it is in and what that step waits on.
    """
    value = progress(state_dir, now)
    if value is None:
        return None
    target = value["target"][:12]
    if value["stale"]:
        return (f"self-update: STALE active.json: the update to {target} (pid {value['pid']}) "
                "is not running; the next run recovers it")
    if value["step"] is None:
        return f"self-update: IN PROGRESS to {target}: starting"
    return (f"self-update: IN PROGRESS to {target} (started {value['started']}, "
            f"{value['minutes']} min): {value['step']} for {value['step_minutes']} min"
            + (f": {value['detail'][:200]}" if value["detail"] else "")
            + (f"; then {', '.join(value['next'])}" if value["next"] else ""))


def waiting_line(state_dir: Path, now: float | None = None) -> str | None:
    """This host's place in the update queue, when it has one."""
    ticket = _read_json(state_dir / "waiting.json")
    if not isinstance(ticket, dict) or not isinstance(ticket.get("since"), (int, float)):
        return None
    now = time.time() if now is None else now
    waited = max(0.0, now - float(ticket["since"]))
    # The run that crossed the bound records it, so a reader whose clock is
    # behind the run's still says STARVED.
    has_demand = bool(gate_demand_at(_gate_queue_path_in(state_dir),
                                      str(ticket.get("host_id") or ""), now)[0])
    bound = STARVED_AFTER_SECONDS if has_demand else STARVED_FALLBACK_SECONDS
    starved = waited >= bound or bool(ticket.get("starved_evented"))
    word = "STARVED" if starved else "deferred"
    return (f"self-update: {word}, {waited / 3600:.1f} h in the update queue (since "
            f"{_iso(float(ticket['since']))}): {ticket.get('reason')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci fleet-macos self-update")
    parser.add_argument("--target", default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="read-only (default)")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--status", action="store_true", help="print cached skew and last attempt")
    mode.add_argument("--verify", action="store_true",
                      help="verify the running generation (pool on and ready, executing what "
                      "the installer says, guard present) without changing anything")
    mode.add_argument("--clear-halt", action="store_true",
                      help="resume automatic attempts after consecutive failures")
    mode.add_argument("--pause", metavar="REASON",
                      help="pause scheduled self-updates with an operator reason")
    mode.add_argument("--resume", action="store_true",
                      help="resume scheduled self-updates")
    mode.add_argument("--peers", action="store_true",
                      help="print each published peer and the SSH target it resolves to")
    mode.add_argument("--refresh-skew", action="store_true",
                      help="fetch main and record skew only (no other checks)")
    parser.add_argument("--reinstall-current", action="store_true",
                        help="with --plan or --apply: reinstall the installed generation with the "
                        "current settings (to apply a settings change, such as a removed "
                        "[fleet_source], without waiting for a new commit)")
    parser.add_argument("--scheduled", action="store_true",
                        help="apply after this host's stagger offset (the periodic agent)")
    parser.add_argument("--if-older", type=int, default=0,
                        help="with --refresh-skew: skip unless the cache is older (seconds)")
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--settings", type=Path, default=None,
                        help="settings TOML (default ~/.config/tartci/self-update.toml)")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(Path(args.home), args.settings)
    except ConfigError as exc:
        print(f"self-update: REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
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
    if args.pause is not None:
        reason = args.pause.strip()
        if not reason:
            parser.error("--pause requires a non-empty reason")
        _write_json(cfg.state_dir / "scheduled-pause.json",
                    {"at": _iso(sys_.now()), "reason": reason})
        record_pause_control(cfg.state_dir, "pause", reason)
        print(f"self-update: scheduled attempts paused: {reason}")
        return 0
    if args.resume:
        (cfg.state_dir / "scheduled-pause.json").unlink(missing_ok=True)
        record_pause_control(cfg.state_dir, "resume")
        print("self-update: scheduled attempts resumed")
        return 0
    if args.peers:
        try:
            refresh_checkout(cfg, sys_)
            me = self_host_id(cfg)
            for peer, target in published_peers(cfg, sys_).items():
                note = ("  (this host)" if peer == me else
                        "" if peer_ssh(target) else "  (refused: not a safe SSH destination)")
                print(f"{peer}\t{target}{note}")
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
            target_ref = args.target or "origin/main"
            skew = measure_skew(cfg, sys_, installed, sys_.now(), target_ref,
                               explicit=args.target is not None)
        except NotManaged as exc:
            skew = {"state": "not_applicable", "reason": str(exc), "measured_at": _iso(sys_.now())}
        except Refused as exc:
            skew = {"state": "unknown", "reason": str(exc), "measured_at": _iso(sys_.now())}
        _write_json(cfg.state_dir / "skew.json", skew)
        print(render_skew(skew))
        return 0
    if args.reinstall_current and (args.target or args.scheduled):
        parser.error("--reinstall-current reinstalls the installed generation; it takes no "
                     "--target and is never --scheduled")
    target_ref = args.target or "origin/main"
    return plan_or_apply(cfg, sys_, apply=args.apply, target_ref=target_ref,
                         explicit=args.target is not None, scheduled=args.scheduled,
                         reinstall=args.reinstall_current)


if __name__ == "__main__":
    sys.exit(main())
