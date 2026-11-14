#!/usr/bin/env python3
"""Run one command against a source tree inside a disposable macOS VM.

    tartci run-in-vm --src DIR --owner NAME [--timeout SECS] [--patch-out FILE]
                     [--artifacts-out FILE --artifact PATH ...] -- COMMAND [ARG ...]

This is the execution half of a repair or operator lane that must not run a
pull request's own scripts on the host. The host keeps its credentials and
does the git plumbing; everything the PR can change runs here instead:

  1. take a host-local run-in-vm lock (one per host) and sweep scratch left
     by dead runs;
  2. on the host: bundle the git history and export the files git would ship
     (no .git, no ignored files, no submodules) into a scratch dir on the
     cache volume; the guest mounts that export, never the live work tree;
  3. admit immediately before the clone (pool `on`, a macOS guest slot stays
     free for the gates, a background-priority lease), record the VM as a
     scratch VM (owner, pid, TTL), clone in its own process session, and
     admit again right before `tart run`;
  4. ssh in with the host key as the client key only (no agent, no
     forwarding, no environment, no ssh config), copy the export into a
     guest-writable work tree, run the command under a wall-clock bound with
     its output capped, and stream back a binary `git diff` and/or a tar of
     named paths; yield (exit 79) when gate work starts waiting;
  5. stop and delete the VM (any clone attempt counts), prove it is gone,
     release the lease, remove the record only once absence is proved.

Everything that comes back from the guest is data: the patch is for the
caller to `git apply`, the tar for it to inspect, the logs for it to read.
See docs/runbook.md, "run-in-vm", for the isolation guarantees and the exit
codes, which are also in EXIT_CODES below.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional

from python_child import child_python

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EXIT_OK = 0
EXIT_COMMAND_FAILED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 75
EXIT_SETUP_FAILED = 76
EXIT_OUTPUT_FAILED = 77
EXIT_TEARDOWN_UNPROVED = 78
EXIT_PREEMPTED = 79
EXIT_TIMEOUT = 124
EXIT_INTERRUPTED = 130

EXIT_CODES = {
    EXIT_OK: "the command exited 0, every requested output was collected, the VM is proved deleted",
    EXIT_COMMAND_FAILED: "the command exited non-zero (its code is guest_rc in --result-json); outputs were still collected",
    EXIT_USAGE: "bad arguments; nothing was started",
    EXIT_REFUSED: "refused before any VM existed: another run-in-vm holds this host, the pool is not on, no macOS guest slot would stay free for the gates, or the lease store denied the lease",
    EXIT_SETUP_FAILED: "clone, size, boot, IP, ssh, guest preparation, or the ssh connection itself (rc 255) failed",
    EXIT_OUTPUT_FAILED: "the command ran but the patch or artifact stream failed or exceeded --max-output-bytes",
    EXIT_TEARDOWN_UNPROVED: "the VM could not be proved deleted; its scratch-vm record stays so the watchdog deletes it at the TTL (overrides every other code but usage)",
    EXIT_PREEMPTED: "a gate-priority VM lease started waiting for this host's capacity; the run yielded and the VM was torn down (retry later)",
    EXIT_TIMEOUT: "the --timeout wall clock ran out; the VM was torn down",
    EXIT_INTERRUPTED: "SIGINT or SIGTERM; the VM was torn down",
}

GOLDEN_DEFAULT = "pulp-build-runner:latest"
VM_PREFIX = "scratch-rivm-"
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
DEFAULT_TIMEOUT = 2700
MAX_TIMEOUT = 4 * 3600
DEFAULT_CORES = 4
DEFAULT_MEM_MB = 8192
DEFAULT_MAX_OUTPUT = 512 * 1024 * 1024
DEFAULT_MAX_LOG = 64 * 1024 * 1024
REASON_TAIL = 600
LEASE_PRIORITY = "background"
LEASE_KIND = "tart-macos-vm"
LEASE_LABEL = "run-in-vm"
HEARTBEAT_SECS = float(os.environ.get("TARTCI_RIVM_HEARTBEAT_SECS") or 30.0)
# A gate waiter counts while it was refreshed this recently (leases.py default).
WAITER_FRESH_SECS = 90
# The watchdog deletes a scratch VM at its TTL; give teardown room past the run.
TTL_SLACK_SECS = 1800
CLONE_SECS = 900
IP_SECS = 120
SSH_SECS = 180
SSH_ATTEMPT_SECS = 15
TEARDOWN_SECS = 120
SHARED = "/Volumes/My Shared Files"
GUEST_STATE = "$HOME/.tartci-rivm"

# The host key is the client key into the guest and nothing else crosses: no
# agent (IdentityAgent=none, ForwardAgent=no, and SSH_AUTH_SOCK is dropped from
# the environment), no forwarding of any kind, no SendEnv (-F /dev/null skips
# the system ssh_config and its `SendEnv LANG LC_*`), no control socket.
SSH_OPTIONS = [
    "-F", "/dev/null",
    "-o", "IdentitiesOnly=yes",
    "-o", "IdentityAgent=none",
    "-o", "ForwardAgent=no",
    "-o", "ForwardX11=no",
    "-o", "ClearAllForwardings=yes",
    "-o", "PermitLocalCommand=no",
    "-o", "ControlMaster=no",
    "-o", "ControlPath=none",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
]


class Refused(Exception):
    """Admission said no; nothing exists yet."""


class SetupFailed(Exception):
    pass


class OutputFailed(Exception):
    pass


class TimedOut(Exception):
    pass


class Interrupted(BaseException):
    pass


class Preempted(BaseException):
    """A gate-priority VM waiter appeared; give the slot back."""


def note(text: str) -> None:
    print(f"run-in-vm: {text}", file=sys.stderr, flush=True)


def clean(text: str, limit: int = REASON_TAIL) -> str:
    """Guest- or tool-supplied text for a reason or a JSON field: printable
    only (no terminal escapes), and only its tail."""
    text = "".join(ch if ch.isprintable() or ch == " " else " " for ch in text)
    text = " ".join(text.split())
    return text[-limit:]


def tail_file(path: Path, limit: int = REASON_TAIL * 4) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode(errors="replace")
    except OSError:
        return ""


def pid_start(pid: int) -> str:
    """The process start time, so a reused pid is not mistaken for the owner."""
    if pid <= 0:
        return ""
    try:
        proc = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                              text=True, timeout=10, check=False,
                              env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(proc.stdout.split()) if proc.returncode == 0 else ""


def owner_alive(pid: object, start: object) -> bool:
    try:
        pid = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    now = pid_start(pid)
    return bool(now) and (not start or now == start)


# ── Paths and host state ───────────────────────────────────────────────────


def tartci_home() -> Path:
    return Path(os.environ.get("TARTCI_HOME") or os.path.expanduser("~/.tartci"))


def state_dir() -> Path:
    return tartci_home() / "state" / "run-in-vm"


def cache_root() -> Path:
    value = (os.environ.get("TARTCI_CI_CACHE") or os.environ.get("PULP_CI_CACHE")
             or os.path.expanduser("~/.cache/pulp-ci"))
    return Path(value)


def runs_dir() -> Path:
    """Per-run scratch (the source export and the bundle). It lives on the cache
    volume, not the boot disk: a Pulp export plus its bundle is gigabytes."""
    return cache_root() / "run-in-vm"


def sweep_stale_runs(keep: str = "") -> int:
    """Remove run-* scratch left by a run that died (owner pid gone). Called under
    the host lock, so no live run can be mid-way through creating one."""
    removed = 0
    root = runs_dir()
    if not root.is_dir():
        return 0
    for path in root.glob("run-*"):
        if str(path) == keep or path.is_symlink() or not path.is_dir():
            continue
        try:
            owner = json.loads((path / "owner.json").read_text())
        except (OSError, ValueError):
            owner = {}
        if owner_alive(owner.get("pid"), owner.get("pid_start")):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def tart_home() -> str:
    value = os.environ.get("TART_HOME")
    if value:
        return value
    import tart_home as resolver  # noqa: PLC0415 - one resolution order for every command
    return str(resolver.resolve()["path"])


def tart_bin() -> str:
    return os.environ.get("TARTCI_TART") or shutil.which("tart") or "/opt/homebrew/bin/tart"


def ssh_bin() -> str:
    # Resolved from the caller's PATH once; ssh itself then runs with ssh_env().
    return os.environ.get("TARTCI_SSH") or shutil.which("ssh") or "/usr/bin/ssh"


def leases_script() -> str:
    return str(Path(__file__).resolve().parent / "leases.py")


def pool_open() -> tuple[bool, str]:
    """Same reading as providers/common/pool.lib.sh tartci_pool_admission_open
    plus tartci_pool_lock_absent."""
    config = Path(os.path.expanduser("~/.config/tartci"))
    participation = Path(os.environ.get("TARTCI_POOL_PARTICIPATION_FILE")
                         or config / "native-build-participation")
    state_file = Path(os.environ.get("TARTCI_POOL_STATE_FILE") or config / "pool-state")
    lock = Path(os.environ.get("TARTCI_POOL_TRANSITION_LOCK") or config / "pool-transition.lock")

    def read(path: Path) -> Optional[str]:
        try:
            return "".join(path.read_text().split())
        except OSError:
            return None

    joined = read(participation) != "0"
    state = read(state_file)
    if state not in ("on", "draining", "off"):
        state = "on" if joined else "off"
    if not joined or state != "on":
        return False, f"pool is {state if joined else 'off'}"
    if lock.exists() or lock.is_symlink():
        return False, "a pool transition is in progress"
    return True, "on"


# ── Small process helpers ──────────────────────────────────────────────────


def child_env() -> Dict[str, str]:
    """The host environment for tart and the lease store, minus the agent."""
    env = dict(os.environ)
    for key in ("SSH_AUTH_SOCK", "SSH_AGENT_PID"):
        env.pop(key, None)
    return env


def ssh_env() -> Dict[str, str]:
    """ssh gets no inherited environment at all beyond what it needs to run."""
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.path.expanduser("~"),
            "LANG": "C"}


def run(argv: List[str], timeout: float, env: Optional[Dict[str, str]] = None,
        stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=stdin, capture_output=True, text=True,
                          timeout=max(1.0, timeout), check=False,
                          env=env if env is not None else child_env())


def lease(command: List[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    return run([child_python(), leases_script(), *command], timeout)


class Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.end = clock() + seconds

    def left(self) -> float:
        return self.end - self.clock()

    def bound(self, cap: float) -> float:
        left = self.left()
        if left <= 0:
            raise TimedOut("wall clock ran out")
        return min(cap, left)


# ── Admission ──────────────────────────────────────────────────────────────


@contextlib.contextmanager
def host_lock():
    """At most one run-in-vm per host. flock is released by the kernel when
    this process dies, so a killed run never leaves the host locked."""
    path = state_dir() / "host.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("another run-in-vm is running on this host") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)


def tart_list(home: str) -> list:
    result = run([tart_bin(), "list", "--format", "json", "--source", "local"], 30,
                 env={**child_env(), "TART_HOME": home})
    if result.returncode != 0:
        raise Refused(f"tart list failed: {(result.stderr or result.stdout).strip()[:200]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise Refused("tart list returned malformed JSON") from None
    if not isinstance(payload, list):
        raise Refused("tart list did not return an array")
    return payload


def reclaim_leftover(home: str, name: str) -> bool:
    """Delete a leftover run-in-vm VM whose recorded owner process is gone.

    Only a VM with our own TTL record (owner `run-in-vm:*`) whose recorded pid
    is dead (or reused) is touched; True only when its absence is proved."""
    import scratch_vm_ttl  # noqa: PLC0415

    record = scratch_vm_ttl.read(name)
    if not record or not str(record.get("owner") or "").startswith("run-in-vm:"):
        return False
    if owner_alive(record.get("pid"), record.get("pid_start")):
        return False
    store = str(record.get("store") or home)
    env = {**child_env(), "TART_HOME": store}
    note(f"reclaiming {name}: its run-in-vm owner (pid {record.get('pid')}) is gone")
    with contextlib.suppress(subprocess.TimeoutExpired):
        run([tart_bin(), "stop", name], TEARDOWN_SECS, env=env)
    with contextlib.suppress(subprocess.TimeoutExpired):
        run([tart_bin(), "delete", name], TEARDOWN_SECS, env=env)
    import tart_inventory  # noqa: PLC0415

    if tart_inventory.prove_vm_absent(name, 30, tart_bin(), store, attempts=3,
                                      backoff_seconds=2) != "absent":
        return False
    with contextlib.suppress(Exception):
        lease(["release", "--id", f"vm-{LEASE_KIND}-{name}", "--json"], 60)
    (scratch_vm_ttl.records_dir() / f"{name}.json").unlink(missing_ok=True)
    return True


def admit_slots(home: str, cap: int, min_free: int, own: str = "") -> None:
    """Refuse while a leftover run-in-vm VM exists (after reclaiming one whose
    owner is dead), or when this run's macOS guest would leave fewer than
    `min_free` of Apple's slots for gates. `own` is this run's already-cloned VM,
    counted as the guest about to boot rather than as a leftover."""
    running = 0
    for vm in tart_list(home):
        if not isinstance(vm, dict):
            raise Refused("tart list contains a non-object entry")
        name = str(vm.get("Name") or vm.get("name") or "")
        state = str(vm.get("State") or vm.get("state") or "").lower()
        if name == own and own:
            continue
        if name.startswith(VM_PREFIX):
            if reclaim_leftover(home, name):
                continue
            raise Refused(f"a run-in-vm VM still exists on this host: {name}")
        os_name = str(vm.get("OS") or vm.get("os") or "").lower()
        if state.startswith("run") and os_name in ("", "darwin", "macos"):
            running += 1
    if running + 1 + min_free > cap:
        raise Refused(f"{running} macOS guest(s) running of {cap}; run-in-vm keeps "
                      f"{min_free} free for the gates")


def gate_waiting(lease_id: str) -> str:
    """Non-empty (a reason) when gate work wants this host's capacity: this
    lease was marked preempted, or a live VM lease waiter above background
    priority is registered. Fail-open: an unreadable store yields nothing."""
    try:
        import leases  # noqa: PLC0415

        store = leases.default_store_dir()
        records = json.loads(leases.store_file(store).read_text() or "{}")
        rows = records.get("leases", records) if isinstance(records, dict) else records
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("id") == lease_id and row.get("preempted") is True:
                return "the lease store preempted this lease"
        waiters, _ = leases.load_waiters(store)
        live, _ = leases.live_waiters(waiters, WAITER_FRESH_SECS)
        for row in live:
            if (int(row.get("priority") or 0) > leases.PRIORITY_CLASSES["background"]
                    and leases.is_vm_kind(row.get("command_kind"))
                    and str(row.get("id")) != lease_id):
                return f"VM lease waiter {row.get('id')} (priority {row.get('priority')}) is waiting"
    except Exception:  # noqa: BLE001 - observation only
        return ""
    return ""


def export_tree(src: str, dest: Path, excludes: List[str]) -> int:
    """Copy the files git would ship (tracked plus untracked-not-ignored) into
    `dest`, so the guest never sees `.git`, ignored files (build trees, local
    secrets) or submodule checkouts. Symlinks are copied as links, never
    followed. `excludes` are fnmatch patterns on the relative path or any of
    its parent directories; a leading `/` anchors at the root. Returns the
    number of entries exported."""
    import fnmatch  # noqa: PLC0415

    env = {**child_env(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_OPTIONAL_LOCKS": "0"}
    listing = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "-C", src, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, timeout=600, check=False, env=env)
    if listing.returncode != 0:
        raise SetupFailed(f"git ls-files failed: {clean(listing.stderr.decode(errors='replace'))}")

    def excluded(rel: str) -> bool:
        parts = rel.split("/")
        prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        for pattern in excludes:
            anchored = pattern.startswith("/")
            pat = pattern.lstrip("/").rstrip("/")
            for prefix in prefixes:
                if fnmatch.fnmatchcase(prefix, pat):
                    return True
                if not anchored and fnmatch.fnmatchcase(prefix.rsplit("/", 1)[-1], pat):
                    return True
        return False

    root = Path(src)
    count = 0
    seen = set()
    for raw in listing.stdout.split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        parts = rel.split("/")
        if rel in seen or rel.startswith("/") or ".." in parts or ".git" in parts:
            continue
        seen.add(rel)
        if excluded(rel):
            continue
        source = root / rel
        try:
            info = source.lstat()
        except OSError:
            continue  # deleted in the work tree; the guest sees it deleted
        target = dest / rel
        # A parent that is a symlink would write outside dest; refuse it.
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        if Path(os.path.realpath(parent)) != Path(os.path.realpath(dest)).joinpath(*parts[:-1]):
            continue
        if os.path.islink(source):
            os.symlink(os.readlink(source), target)
        elif stat_isreg(info.st_mode):
            shutil.copy2(source, target, follow_symlinks=False)
        else:
            continue  # submodule checkouts (gitlinks), sockets, devices
        count += 1
    return count


def stat_isreg(mode: int) -> bool:
    import stat  # noqa: PLC0415

    return stat.S_ISREG(mode)


# ── Guest scripts (pure, so the tests read exactly what the guest runs) ─────


def guest_prep_script(excludes: List[str], has_bundle: bool, head: str, branch: str,
                      refs: List[str], has_ccache: bool, has_fetchcontent: bool) -> str:
    # The host already exported only what git ships, minus --exclude
    # (export_tree); /.git is excluded again as a belt.
    rsync_excludes = " ".join(f"--exclude={shlex.quote(item)}" for item in ["/.git", *excludes])
    lines = [
        "set -euo pipefail",
        f'SHARED={shlex.quote(SHARED)}',
        f'STATE="{GUEST_STATE}"',
        'rm -rf "$HOME/work" "$STATE"',
        'mkdir -p "$HOME/work" "$STATE" "$HOME/.config/ccache" "$HOME/.ccache-local"',
        'touch "$HOME/.config/ccache/tartci.conf"',
        f'rsync -a {rsync_excludes} "$SHARED/src/" "$HOME/work/"',
    ]
    env_lines = [
        'eval "$(/opt/homebrew/bin/brew shellenv)"',
        # A guest-local ccache config and store; the host's shared store, when
        # mounted, is read through a read-only remote and is never written.
        'export CCACHE_DIR="$HOME/.ccache-local"',
        'export CCACHE_CONFIGPATH="$HOME/.config/ccache/tartci.conf"',
        "export CCACHE_COMPILERCHECK=content CCACHE_NODEPEND=true",
        'export PULP_CONFIGURE_CHECK_CACHE_DIR="$HOME/Library/Caches/Pulp/configure-checks"',
    ]
    if has_ccache:
        # ccache splits remote_storage on spaces and the share path has them.
        lines.append('ln -sfn "$SHARED/ccache-shared" "$STATE/ccache-shared"')
        env_lines.append('export CCACHE_REMOTE_STORAGE="file:$HOME/.tartci-rivm/ccache-shared|read-only"')
    if has_fetchcontent:
        lines += [
            'mkdir -p "$HOME/Library/Caches/Pulp/fetchcontent-src"',
            'rsync -a "$SHARED/fetchcontent/" "$HOME/Library/Caches/Pulp/fetchcontent-src/"',
        ]
        env_lines.append('export PULP_SHARED_FETCHCONTENT_SOURCE_DIR="$HOME/Library/Caches/Pulp/fetchcontent-src"')
    lines.append(f'cat >"$STATE/env.sh" <<\'TARTCI_ENV\'\n' + "\n".join(env_lines) + "\nTARTCI_ENV")
    git = ['cd "$HOME/work"', "git init -q",
           "git config user.name tartci-run-in-vm",
           "git config user.email run-in-vm@tartci.invalid"]
    if has_bundle:
        git.append(f"git symbolic-ref HEAD refs/heads/{shlex.quote(branch)}")
        git.append(f'git fetch -q "$SHARED/gitbundle/src.bundle" {shlex.quote(head)}')
        for ref in refs:
            git.append(f'git fetch -q "$SHARED/gitbundle/src.bundle" '
                       f"{shlex.quote('+' + ref + ':' + ref)}")
        # Mixed reset: HEAD and index move to the host's commit, the copied
        # files stay, so uncommitted host changes read as changes.
        git.append(f"git reset -q {shlex.quote(head)}")
    # The baseline the patch is taken against: the tree exactly as shipped in.
    git += ["git add -A", "git commit -q --allow-empty --no-verify -m tartci-run-in-vm-baseline",
            'git rev-parse HEAD >"$STATE/base"']
    lines += git
    return "\n".join(lines) + "\n"


def guest_command(command: List[str]) -> str:
    script = (f'. "{GUEST_STATE}/env.sh" && cd "$HOME/work" && exec '
              + " ".join(shlex.quote(arg) for arg in command))
    return "/bin/bash -c " + shlex.quote(script)


def guest_patch_command() -> str:
    script = ('cd "$HOME/work" && git add -A && '
              f'git diff --cached --binary "$(cat "{GUEST_STATE}/base")"')
    return "/bin/bash -c " + shlex.quote(script)


def guest_artifacts_command(paths: List[str]) -> str:
    script = 'cd "$HOME/work" && tar -cf - -- ' + " ".join(shlex.quote(p) for p in paths)
    return "/bin/bash -c " + shlex.quote(script)


def kill_session(proc: subprocess.Popen) -> None:
    """TERM then KILL a child started with start_new_session, and everything
    it forked (the guard-run's `tart clone` writer included)."""
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 10)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            proc.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue
    # The group can outlive its leader; make sure nothing in it is left.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def safe_artifact(path: str) -> bool:
    parts = Path(path).parts
    return bool(parts) and not path.startswith("/") and ".." not in parts and "\0" not in path


# ── The VM ─────────────────────────────────────────────────────────────────


class Run:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.home = args.tart_home or tart_home()
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        self.name = f"{VM_PREFIX}{args.owner}-{stamp}"
        self.lease_id = f"vm-{LEASE_KIND}-{self.name}"
        self.ip = ""
        self.guardian: Optional[subprocess.Popen] = None
        self.lease_held = False
        self.cloned = False
        self.record_written = False
        self.heartbeat_stop = threading.Event()
        self.heartbeat_thread: Optional[threading.Thread] = None
        self.tmp: Optional[str] = None
        self.export: Optional[Path] = None
        self.admit_again: Optional[Callable[[], None]] = None
        self.preempted = ""
        self.result: Dict[str, object] = {"vm": self.name, "guest_rc": None}
        self.key = os.environ.get("TARTCI_VM_SSH_KEY") or os.path.expanduser("~/.ssh/id_ed25519")
        self.user = os.environ.get("TARTCI_VM_USER") or "admin"

    # TTL record: the launchd watchdog stops and deletes a scratch VM at its
    # expiry, which is the backstop for a run killed with SIGKILL.
    def write_record(self) -> None:
        import scratch_vm_ttl  # noqa: PLC0415

        now = time.time()
        scratch_vm_ttl.write({
            "name": self.name, "owner": f"run-in-vm:{self.args.owner}", "store": self.home,
            "from": self.args.golden, "created_at": now,
            "expires_at": now + self.args.timeout + TTL_SLACK_SECS, "warned_at": None,
            # Lets the next run reclaim this VM when this process is gone.
            "pid": os.getpid(), "pid_start": pid_start(os.getpid())})
        self.record_written = True

    def remove_record(self) -> None:
        import scratch_vm_ttl  # noqa: PLC0415

        (scratch_vm_ttl.records_dir() / f"{self.name}.json").unlink(missing_ok=True)

    def acquire(self) -> None:
        result = lease([
            "acquire", "--id", self.lease_id, "--cores", str(self.args.cores),
            "--mem-mb", str(self.args.mem_mb), "--priority", LEASE_PRIORITY,
            "--pid", str(os.getpid()), "--kind", LEASE_KIND,
            "--owner", f"run-in-vm:{self.args.owner}", "--label", LEASE_LABEL,
            "--vm-name", self.name, "--disk-path", self.home,
            "--disk-growth-mb", str(24 * 1024), "--disk-floor-mb", str(25 * 1024), "--json"])
        if result.returncode != 0:
            raise Refused(f"lease denied (rc {result.returncode}): "
                          f"{(result.stdout or result.stderr).strip()[:400]}")
        self.lease_held = True
        self.heartbeat_thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.heartbeat_thread.start()

    def heartbeat(self) -> None:
        while not self.heartbeat_stop.wait(HEARTBEAT_SECS):
            with contextlib.suppress(Exception):
                lease(["heartbeat", "--id", self.lease_id, "--json"], 30)
            why = gate_waiting(self.lease_id)
            if why and not self.heartbeat_stop.is_set():
                # Yield the slot: the main thread's SIGUSR1 handler raises
                # Preempted, which interrupts whatever it is waiting on.
                self.preempted = why
                os.kill(os.getpid(), signal.SIGUSR1)
                return

    def tart(self, *argv: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
        return run([tart_bin(), *argv], timeout, env={**child_env(), "TART_HOME": self.home})

    def mounts(self) -> List[str]:
        assert self.export is not None
        dirs = [f"--dir=src:{self.export}:ro"]
        if self.tmp and (Path(self.tmp) / "src.bundle").exists():
            dirs.append(f"--dir=gitbundle:{self.tmp}:ro")
        root = cache_root()
        shared = root / "ccache-layers-v1" / "shared"
        if shared.is_dir() and not shared.is_symlink():
            dirs.append(f"--dir=ccache-shared:{shared}:ro")
        fetch = Path(os.environ.get("PULP_SHARED_FETCHCONTENT_SOURCE_DIR")
                     or os.path.expanduser("~/Library/Caches/Pulp/fetchcontent-src"))
        if fetch.is_dir():
            dirs.append(f"--dir=fetchcontent:{fetch}:ro")
        for name in ("pip-wheelhouse", "artifact-cache"):
            if (root / name).is_dir():
                dirs.append(f"--dir={name}:{root / name}:ro")
        return dirs

    def prepare(self, deadline: Deadline) -> tuple[bool, str, str, List[str]]:
        """Host-side, before admission: the run's scratch dir, the git bundle
        and the source export the guest will mount instead of --src."""
        root = runs_dir()
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.mkdtemp(prefix="run-", dir=str(root))
        (Path(self.tmp) / "owner.json").write_text(json.dumps(
            {"pid": os.getpid(), "pid_start": pid_start(os.getpid()), "vm": self.name}))
        bundled = self.bundle(deadline)
        self.export = Path(self.tmp) / "src"
        self.export.mkdir()
        self.result["exported_entries"] = export_tree(self.args.src, self.export,
                                                      self.args.exclude or [])
        deadline.bound(1)
        return bundled

    def bundle(self, deadline: Deadline) -> tuple[bool, str, str, List[str]]:
        """A bundle of HEAD (and origin/main when present), never the .git dir:
        the guest gets history, not the host's git config, hooks or other refs."""
        src = self.args.src
        env = {**child_env(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        head = run(["git", "-C", src, "rev-parse", "--verify", "-q", "HEAD"], 30, env=env)
        if head.returncode != 0:
            return False, "", "", []
        sha = head.stdout.strip()
        branch = run(["git", "-C", src, "symbolic-ref", "--short", "-q", "HEAD"], 30, env=env)
        name = branch.stdout.strip() if branch.returncode == 0 else ""
        if not REF_RE.match(name or "") or name.endswith(".lock") or ".." in name:
            name = "work"
        refs = []
        for ref in self.args.git_ref or ["refs/remotes/origin/main"]:
            probe = run(["git", "-C", src, "rev-parse", "--verify", "-q", ref], 30, env=env)
            if probe.returncode == 0:
                refs.append(ref)
        assert self.tmp
        made = run(["git", "-C", src, "bundle", "create", str(Path(self.tmp) / "src.bundle"),
                    "HEAD", *refs], deadline.bound(900), env=env)
        if made.returncode != 0:
            raise SetupFailed(f"git bundle failed: {made.stderr.strip()[:300]}")
        return True, sha, name, refs

    def ssh(self, remote: str, timeout: float, stdin: Optional[str] = None,
            stdout=None, stderr=None, forward: bool = False) -> int:
        """Run `remote` in the guest. With forward=True the guest's stdout and
        stderr are copied to ours with a byte cap (--max-log-bytes, shared), so
        a hostile command cannot flood the caller's logs."""
        argv = [ssh_bin(), *SSH_OPTIONS, "-i", self.key, f"{self.user}@{self.ip}", remote]
        if forward:
            stdout = stderr = subprocess.PIPE
        proc = subprocess.Popen(argv, env=ssh_env(),
                                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr)
        pumps: List[threading.Thread] = []
        if forward:
            budget = {"left": self.args.max_log_bytes, "noted": False}
            lock = threading.Lock()

            def pump(src, dst) -> None:
                for chunk in iter(lambda: src.read1(65536) if hasattr(src, "read1") else src.read(65536), b""):
                    with lock:
                        take = min(len(chunk), max(0, budget["left"]))
                        budget["left"] -= len(chunk)
                        if take:
                            dst.write(chunk[:take])
                            dst.flush()
                        if take < len(chunk) and not budget["noted"]:
                            budget["noted"] = True
                            self.result["log_truncated"] = True
                            note(f"guest output passed --max-log-bytes "
                                 f"({self.args.max_log_bytes}); the rest is dropped")

            for src, dst in ((proc.stdout, sys.stdout.buffer), (proc.stderr, sys.stderr.buffer)):
                thread = threading.Thread(target=pump, args=(src, dst), daemon=True)
                thread.start()
                pumps.append(thread)
        try:
            if stdin is not None:
                assert proc.stdin
                proc.stdin.write(stdin.encode())
                proc.stdin.close()
            rc = proc.wait(timeout=max(1.0, timeout))
            for thread in pumps:
                thread.join(timeout=10)
            return rc
        except BaseException:
            proc.kill()
            proc.wait()
            raise

    def boot(self, deadline: Deadline) -> None:
        # Any clone attempt may leave a VM (a partial clone, or the writer
        # finishing after we gave up), so teardown must delete and prove from
        # here on. The guard and its forked `tart clone` share a new session so
        # a timeout or a signal kills both, not just the guard.
        self.cloned = True
        clone_log = Path(self.tmp or ".") / "tart-clone.log"
        with open(clone_log, "wb") as log:
            clone = subprocess.Popen(
                [child_python(), leases_script(), "guard-run", "--id", self.lease_id, "--",
                 tart_bin(), "clone", self.args.golden, self.name],
                env={**child_env(), "TART_HOME": self.home}, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            rc = clone.wait(timeout=max(1.0, deadline.bound(CLONE_SECS)))
        except BaseException as exc:
            kill_session(clone)
            if isinstance(exc, subprocess.TimeoutExpired):
                deadline.bound(1)
                raise SetupFailed("tart clone exceeded its bound") from None
            raise
        if rc != 0:
            raise SetupFailed(f"tart clone failed (rc {rc}): {clean(tail_file(clone_log))}")
        sized = self.tart("set", self.name, "--cpu", str(self.args.cores),
                          "--memory", str(self.args.mem_mb), timeout=deadline.bound(60))
        if sized.returncode != 0:
            raise SetupFailed(f"tart set failed: {clean(sized.stderr)}")
        # A gate may have booted while this run bundled, exported and cloned.
        if self.admit_again is not None:
            self.admit_again()
        log = open(Path(self.tmp or ".") / "tart-run.log", "wb")  # noqa: SIM115
        self.guardian = subprocess.Popen(
            [child_python(), leases_script(), "guard-exec", "--id", self.lease_id, "--",
             tart_bin(), "run", "--no-graphics", *self.mounts(), self.name],
            env={**child_env(), "TART_HOME": self.home}, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        run_log = Path(self.tmp or ".") / "tart-run.log"
        ip_deadline = time.monotonic() + deadline.bound(IP_SECS)
        while time.monotonic() < ip_deadline:
            if self.guardian.poll() is not None:
                tail = clean(tail_file(run_log))
                self.result["tart_run_log_tail"] = tail
                raise SetupFailed(f"tart run exited {self.guardian.returncode} before an IP: {tail}")
            got = self.tart("ip", self.name, timeout=10)
            if got.returncode == 0 and got.stdout.strip():
                self.ip = got.stdout.strip().splitlines()[0]
                break
            time.sleep(2)
        if not self.ip:
            deadline.bound(1)
            tail = clean(tail_file(run_log))
            self.result["tart_run_log_tail"] = tail
            raise SetupFailed(f"no IP within the boot bound: {tail}")
        ssh_deadline = time.monotonic() + deadline.bound(SSH_SECS)
        while True:
            left = ssh_deadline - time.monotonic()
            if left <= 0:
                deadline.bound(1)
                raise SetupFailed("ssh did not answer within the boot bound")
            with contextlib.suppress(subprocess.TimeoutExpired):
                if self.ssh("true", min(SSH_ATTEMPT_SECS, left),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
                    return
            time.sleep(2)

    def collect(self, remote: str, out_path: str, deadline: Deadline, what: str) -> int:
        tmp_out = Path(out_path).with_name(f".{Path(out_path).name}.partial")
        err_path = Path(self.tmp or ".") / f"collect-{what}.err"
        # stderr goes to a file, never a pipe read after stdout EOF: a guest
        # that writes more stderr than the pipe holds would otherwise stall the
        # stream until the deadline and read as a timeout.
        with open(tmp_out, "wb") as handle, open(err_path, "wb") as err_handle:
            argv = [ssh_bin(), *SSH_OPTIONS, "-i", self.key, f"{self.user}@{self.ip}", remote]
            proc = subprocess.Popen(argv, env=ssh_env(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=err_handle)
            assert proc.stdout
            total = 0
            timer = threading.Timer(max(1.0, deadline.bound(3600)), proc.kill)
            timer.start()
            try:
                while True:
                    chunk = proc.stdout.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.args.max_output_bytes:
                        proc.kill()
                        proc.wait()
                        tmp_out.unlink(missing_ok=True)
                        raise OutputFailed(f"{what} exceeded {self.args.max_output_bytes} bytes")
                    handle.write(chunk)
                rc = proc.wait()
            finally:
                timer.cancel()
        if deadline.left() <= 0:
            tmp_out.unlink(missing_ok=True)
            raise TimedOut(f"wall clock ran out while streaming the {what}")
        if rc != 0:
            tmp_out.unlink(missing_ok=True)
            raise OutputFailed(f"{what} stream failed (rc {rc}): {clean(tail_file(err_path))}")
        os.replace(tmp_out, out_path)
        return total

    def execute(self, admit: Callable[[str], None]) -> int:
        """Prepare on the host, then admit (pool, slots, lease) immediately
        before the clone, and again immediately before `tart run`."""
        deadline = Deadline(self.args.timeout)
        has_bundle, head, branch, refs = self.prepare(deadline)
        admit("")
        self.write_record()
        self.acquire()
        self.admit_again = lambda: admit(self.name)
        note(f"booting {self.name} from {self.args.golden} ({self.args.cores} cores, "
             f"{self.args.mem_mb} MB, background priority)")
        self.boot(deadline)
        mounts = self.mounts()
        prep = guest_prep_script(
            [], has_bundle, head, branch, refs,
            any(m.startswith("--dir=ccache-shared:") for m in mounts),
            any(m.startswith("--dir=fetchcontent:") for m in mounts))
        try:
            rc = self.ssh("/bin/bash -s", deadline.bound(900), stdin=prep, forward=True)
        except subprocess.TimeoutExpired:
            deadline.bound(1)
            raise SetupFailed("guest preparation exceeded its bound") from None
        if rc != 0:
            raise SetupFailed(f"guest preparation failed (rc {rc})")
        note(f"running in {self.name}: {' '.join(shlex.quote(a) for a in self.args.command)}")
        try:
            guest_rc = self.ssh(guest_command(self.args.command), deadline.bound(MAX_TIMEOUT),
                                forward=True)
        except subprocess.TimeoutExpired:
            raise TimedOut("the command ran past --timeout") from None
        if guest_rc == 255:
            raise SetupFailed("ssh connection to the guest failed (rc 255)")
        self.result["guest_rc"] = guest_rc
        note(f"guest exit {guest_rc}")
        if self.args.patch_out:
            self.result["patch_bytes"] = self.collect(
                guest_patch_command(), self.args.patch_out, deadline, "patch")
        if self.args.artifacts_out:
            self.result["artifacts_bytes"] = self.collect(
                guest_artifacts_command(self.args.artifact), self.args.artifacts_out,
                deadline, "artifacts")
        return EXIT_OK if guest_rc == 0 else EXIT_COMMAND_FAILED

    def teardown(self) -> bool:
        """Stop and delete the VM, prove it is gone, release the lease. The TTL
        record is removed only once absence is proved; the scratch dir always."""
        proved = True
        try:
            if self.cloned:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.tart("stop", self.name, timeout=TEARDOWN_SECS)
                if self.guardian is not None and self.guardian.poll() is None:
                    kill_session(self.guardian)
                if self.guardian is not None and self.guardian.poll() is None:
                    proved = False
                else:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        self.tart("delete", self.name, timeout=TEARDOWN_SECS)
                    import tart_inventory  # noqa: PLC0415

                    verdict = tart_inventory.prove_vm_absent(
                        self.name, 30, tart_bin(), self.home, attempts=3, backoff_seconds=2)
                    proved = verdict == "absent"
        except Exception:  # noqa: BLE001 - anything unproved keeps the record
            traceback.print_exc()
            proved = False
        finally:
            self.heartbeat_stop.set()
            if self.lease_held and proved:
                with contextlib.suppress(Exception):
                    lease(["release", "--id", self.lease_id, "--json"], 60)
                self.lease_held = False
            if self.record_written and proved:
                self.remove_record()
                self.record_written = False
            if self.tmp:
                shutil.rmtree(self.tmp, ignore_errors=True)
                self.tmp = None
        self.result["teardown"] = "proved" if proved else "unproved"
        return proved


# ── CLI ────────────────────────────────────────────────────────────────────


def parse_args(argv: List[str]) -> argparse.Namespace:
    codes = "\n".join(f"  {code:>3}  {text}" for code, text in EXIT_CODES.items())
    parser = argparse.ArgumentParser(
        prog="tartci run-in-vm",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run COMMAND against --src inside a disposable, credential-free "
                    "macOS VM, then discard the VM.",
        epilog=f"exit codes:\n{codes}\n\nSee docs/runbook.md, \"run-in-vm\".")
    parser.add_argument("--src", required=True, help="source tree; mounted read-only")
    parser.add_argument("--owner", required=True, help="who asked (recorded with the VM)")
    parser.add_argument("--golden", default=os.environ.get("TARTCI_MACOS_GOLDEN", GOLDEN_DEFAULT))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"wall-clock bound in seconds for the whole run (max {MAX_TIMEOUT})")
    parser.add_argument("--cores", type=int, default=DEFAULT_CORES)
    parser.add_argument("--mem-mb", type=int, default=DEFAULT_MEM_MB)
    parser.add_argument("--patch-out", help="write `git diff --binary` of the guest work tree here")
    parser.add_argument("--artifacts-out", help="write a tar of the --artifact paths here")
    parser.add_argument("--artifact", action="append", default=[],
                        help="a path relative to the work tree (repeatable)")
    parser.add_argument("--exclude", action="append", default=[],
                        help="fnmatch pattern (relative path or a parent dir; leading / "
                             "anchors) left out of the host-side export (repeatable)")
    parser.add_argument("--git-ref", action="append", default=[],
                        help="extra ref to bundle (default refs/remotes/origin/main)")
    parser.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT)
    parser.add_argument("--max-log-bytes", type=int, default=DEFAULT_MAX_LOG,
                        help="cap on guest stdout+stderr copied to ours")
    parser.add_argument("--min-free-slots", type=int, default=1,
                        help="macOS guest slots that must stay free for the gates")
    parser.add_argument("--macos-vm-cap", type=int,
                        default=int(os.environ.get("TARTCI_MACOS_VM_CAP", "2")))
    parser.add_argument("--tart-home", help="Tart store (default: as other tartci commands)")
    parser.add_argument("--result-json", help="write a JSON summary here")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARG ...]")
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    problems = []
    if not args.command:
        problems.append("a command is required after --")
    if not OWNER_RE.match(args.owner):
        problems.append(f"--owner must match {OWNER_RE.pattern}")
    if not 1 <= args.timeout <= MAX_TIMEOUT:
        problems.append(f"--timeout must be 1..{MAX_TIMEOUT}")
    if not 1 <= args.cores <= 16 or not 2048 <= args.mem_mb <= 65536:
        problems.append("--cores must be 1..16 and --mem-mb 2048..65536")
    if not 0 <= args.min_free_slots < max(1, args.macos_vm_cap):
        problems.append("--min-free-slots must be below --macos-vm-cap")
    if args.artifact and not args.artifacts_out:
        problems.append("--artifact needs --artifacts-out")
    if args.artifacts_out and not args.artifact:
        problems.append("--artifacts-out needs at least one --artifact")
    bad = [p for p in args.artifact if not safe_artifact(p)]
    if bad:
        problems.append(f"--artifact must be relative without '..': {bad}")
    bad_refs = [r for r in args.git_ref if not REF_RE.match(r) or ".." in r]
    if bad_refs:
        problems.append(f"invalid --git-ref: {bad_refs}")
    src = Path(args.src)
    if not src.is_dir():
        problems.append(f"--src is not a directory: {args.src}")
    else:
        args.src = str(src.resolve())
        if ":" in args.src:
            problems.append("--src must not contain ':' (Tart's --dir separator)")
    for out in (args.patch_out, args.artifacts_out, args.result_json):
        if out and not Path(out).expanduser().resolve().parent.is_dir():
            problems.append(f"output directory does not exist: {out}")
    if problems:
        parser.error("; ".join(problems))
    return args


def main(argv: Optional[List[str]] = None) -> int:
    for key in ("SSH_AUTH_SOCK", "SSH_AGENT_PID"):
        os.environ.pop(key, None)
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code not in (0, None) else 0

    def interrupt(signum, _frame):
        raise Interrupted(signal.Signals(signum).name)

    def preempt(_signum, _frame):
        raise Preempted("gate demand")

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    previous[signal.SIGUSR1] = signal.signal(signal.SIGUSR1, preempt)
    state_dir().mkdir(parents=True, exist_ok=True)
    job: Optional[Run] = None
    code = EXIT_OK
    reason = ""
    try:
        with host_lock():
            swept = sweep_stale_runs()
            if swept:
                note(f"removed {swept} stale run-in-vm scratch dir(s)")
            home = args.tart_home or tart_home()

            def admit(own: str) -> None:
                ok, why = pool_open()
                if not ok:
                    raise Refused(why)
                admit_slots(home, args.macos_vm_cap, args.min_free_slots, own=own)

            job = Run(args)
            try:
                code = job.execute(admit)
            except Refused as exc:
                code, reason = EXIT_REFUSED, str(exc)
            except Preempted:
                code, reason = EXIT_PREEMPTED, f"yielded to gate work: {job.preempted}"
            except TimedOut as exc:
                code, reason = EXIT_TIMEOUT, str(exc)
            except SetupFailed as exc:
                code, reason = EXIT_SETUP_FAILED, str(exc)
            except OutputFailed as exc:
                code, reason = EXIT_OUTPUT_FAILED, str(exc)
            except Interrupted as exc:
                code, reason = EXIT_INTERRUPTED, f"interrupted by {exc}"
            except subprocess.TimeoutExpired as exc:
                code, reason = EXIT_SETUP_FAILED, f"bounded step timed out: {exc}"
            except Exception as exc:  # noqa: BLE001 - still a documented code, never a traceback
                traceback.print_exc()
                code, reason = EXIT_SETUP_FAILED, f"unexpected error: {exc!r}"
            finally:
                for sig in previous:
                    signal.signal(sig, signal.SIG_IGN)
                job.heartbeat_stop.set()
                if job.preempted and code not in (EXIT_PREEMPTED, EXIT_USAGE):
                    code, reason = EXIT_PREEMPTED, f"yielded to gate work: {job.preempted}"
                try:
                    proved = job.teardown()
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
                    proved = False
                if not proved:
                    code = EXIT_TEARDOWN_UNPROVED
                    reason = (f"{reason}; " if reason else "") + (
                        f"VM {job.name} not proved deleted; the watchdog deletes it at its TTL")
    except Refused as exc:
        code, reason = EXIT_REFUSED, str(exc)
    except Interrupted as exc:
        code, reason = EXIT_INTERRUPTED, f"interrupted by {exc}"
    except Preempted:
        code, reason = EXIT_PREEMPTED, "yielded to gate work"
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if reason:
        note(reason)
    note(f"exit {code}: {EXIT_CODES.get(code, '')}")
    if args.result_json:
        summary = dict(job.result if job else {})
        summary.update({"exit_code": code, "reason": clean(reason, 2000)})
        Path(args.result_json).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
