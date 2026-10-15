#!/usr/bin/env python3
"""Tell someone, once per episode, that a cache or VM volume stopped writing.

On m5studio on 2026-10-12 (outage O10 in the planning ledger) every create on
/Volumes/<data-volume> stalled about 6 s, for hours, while reads stayed instant. A
tartci disk probe that ran `open()` there sat inside the kernel's `__open` for
7 h, the WindowServer watchdog eventually killed the login session, and the
kernel log that might have named the cause had rotated by the time anyone
looked. Nothing measured write latency, so the first signal was lanes dying.

`tartci disk watch` is one pass of the disk-watchdog LaunchAgent, which runs it
every 60 s. For each configured volume path it runs a probe in a child process:
create a file, write a byte, fsync, close, unlink. The pass never waits on a
probe longer than PROBE_TIMEOUT_S, and never touches the volume itself, because
a call that wedges in the kernel cannot be interrupted: the O10 probe could
not be killed. A probe still alive after the timeout is recorded as
outstanding, and while it lives no further probe is started on that path, so a
wedged volume collects one stuck process, not one a minute.

A probe that takes more than THRESHOLD_S, or times out, makes the path's
episode active. Once per episode the pass saves evidence under the tartci
state dir (`state/disk-watchdog/evidence/<path>-<start>/`):

  log-show.txt     `/usr/bin/log show --last 5m` for APFS and the kernel
  ps-blocked.txt   processes in uninterruptible wait (ps state D or U)
  sample-<pid>.txt `/usr/bin/sample` of the stuck probe and of up to
                   MAX_SAMPLES blocked processes; evidence.json lists those
                   whose stack is inside open()

then reports through host_off.episode_alert, the path the VM-boot and host-off
alerts use: a `disk_latency_slow` event and one GitHub issue per episode. The
episode ends, and the issue closes, after RECOVER_PASSES fast probes in a row.

Every evidence command runs with a bound and is abandoned rather than waited
on, for the same reason as the probe. Paths come from
TARTCI_DISK_WATCH_PATHS (os.pathsep-separated, rendered into the agent from
the profile's host.tart_home and host.cache_root), else from the profile.

Python 3.9-safe: the launchd python on fleet hosts is /usr/bin/python3.
"""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python < 3.11 (/usr/bin/python3 on macOS is 3.9)
    tomllib = None  # type: ignore[assignment]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host_off  # noqa: E402

THRESHOLD_S = 1.0
PROBE_TIMEOUT_S = 10.0
EVIDENCE_TIMEOUT_S = 60.0
SAMPLE_SECONDS = 3
MAX_SAMPLES = 5
RECOVER_PASSES = 3
PROBE_MARKER = "--tartci-disk-probe"
LOG_PREDICATE = ('sender == "apfs" OR subsystem BEGINSWITH "com.apple.apfs" '
                 'OR process == "kernel"')

# The probe a child runs. It is the only code that touches the volume.
PROBE = """
import os, sys, time
directory = sys.argv[-1]
path = os.path.join(directory, ".tartci-disk-probe-%d" % os.getpid())
start = time.monotonic()
fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
try:
    os.write(fd, b"x")
    os.fsync(fd)
finally:
    os.close(fd)
os.unlink(path)
print("%.6f" % (time.monotonic() - start))
"""

Runner = Callable[[Sequence[str], float, pathlib.Path], Tuple[str, str]]


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def state_dir() -> pathlib.Path:
    root = os.environ.get("TARTCI_HOME") or str(pathlib.Path.home() / ".tartci")
    return pathlib.Path(root) / "state" / "disk-watchdog"


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(path: str) -> str:
    """A file-name-safe key for a watched path."""
    readable = "".join(ch if ch.isalnum() else "-" for ch in path).strip("-")[:48]
    return f"{readable}-{hashlib.sha256(path.encode()).hexdigest()[:8]}"


def _profile() -> Dict[str, Any]:
    if tomllib is None:
        return {}
    path = pathlib.Path(os.environ.get(
        "TARTCI_FLEET_PROFILE",
        str(pathlib.Path.home() / ".config" / "tartci" / "macos-fleet-profile.toml")))
    try:
        return tomllib.loads(path.read_text())
    except Exception:  # noqa: BLE001 - no profile means no profile paths
        return {}


def profile_paths(profile: Dict[str, Any]) -> List[str]:
    """The VM store and the cache root a profile declares, in that order."""
    host = profile.get("host") if isinstance(profile.get("host"), dict) else {}
    paths = []
    for key in ("tart_home", "cache_root"):
        value = host.get(key)
        if isinstance(value, str) and value and value not in paths:
            paths.append(value)
    return paths


def watch_paths() -> List[str]:
    """TARTCI_DISK_WATCH_PATHS, else the profile's paths.

    An entry that is still a template placeholder (`$NAME`) is dropped: a
    profile without the setting renders the agent with the literal name.
    """
    raw = os.environ.get("TARTCI_DISK_WATCH_PATHS")
    if raw is not None:
        entries = [part for part in raw.split(os.pathsep) if part and not part.startswith("$")]
        return list(dict.fromkeys(entries))
    return profile_paths(_profile())


def _alert_host() -> Tuple[str, str]:
    host = _profile().get("host") or {}
    if not isinstance(host, dict):
        host = {}
    name = str(host.get("id") or os.uname().nodename.split(".")[0])
    return name, str(host.get("ssh") or f"tartci-{name}")


# ── probing ────────────────────────────────────────────────────────────────

def probe_command(path: str) -> List[str]:
    """The argv of one probe: TARTCI_DISK_PROBE_COMMAND (a JSON list) or this
    interpreter running PROBE, then the marker and the path."""
    override = os.environ.get("TARTCI_DISK_PROBE_COMMAND")
    if override:
        command = json.loads(override)
        if not (isinstance(command, list) and command
                and all(isinstance(part, str) for part in command)):
            raise ValueError("TARTCI_DISK_PROBE_COMMAND must be a JSON list of strings")
    else:
        command = [sys.executable, "-c", PROBE]
    return [*command, PROBE_MARKER, path]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _probe_alive(pid: int) -> bool:
    """True while `pid` is still this watchdog's probe (not a reused pid)."""
    if not _alive(pid):
        return False
    try:
        out = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell: keep treating it as ours, never start a second
    return PROBE_MARKER in out.stdout


def run_probe(path: str, timeout: float, threshold: float) -> Dict[str, Any]:
    """One probe of `path`, waited on for at most `timeout`.

    Returns status ok, slow, timeout or error, the wall time the pass saw,
    and for a timeout the pid left behind (`stuck`: it outlived SIGKILL).
    """
    start = time.monotonic()
    try:
        proc = subprocess.Popen(probe_command(path), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except (OSError, ValueError) as exc:
        return {"status": "error", "elapsed_s": 0.0, "detail": f"probe did not start: {exc}"}
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
            stuck = False
        except subprocess.TimeoutExpired:
            stuck = True  # in the kernel: it ignores SIGKILL until the call returns
        return {"status": "timeout", "elapsed_s": round(elapsed, 3), "pid": proc.pid,
                "stuck": stuck,
                "detail": f"no result within {timeout:g} s"
                          + ("; the probe outlived SIGKILL" if stuck else "")}
    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        status = "slow" if elapsed > threshold else "error"
        return {"status": status, "elapsed_s": round(elapsed, 3),
                "detail": f"probe exited {proc.returncode}: {stderr.strip()[-300:]}"}
    return {"status": "slow" if elapsed > threshold else "ok",
            "elapsed_s": round(elapsed, 3), "write_s": _float(stdout)}


def _float(text: str) -> Optional[float]:
    try:
        return round(float(text.strip().splitlines()[-1]), 3)
    except (ValueError, IndexError):
        return None


# ── evidence ───────────────────────────────────────────────────────────────

def bounded(command: Sequence[str], timeout: float, out: pathlib.Path) -> Tuple[str, str]:
    """Run `command` with its output in `out`, for at most `timeout` seconds.

    Returns (outcome, text). A command still running at the bound is killed
    and, if it does not exit within a second (stuck in the kernel), abandoned
    rather than waited on. Output goes to a file so no pipe can block us.
    """
    try:
        with out.open("w") as handle:
            proc = subprocess.Popen(list(command), stdin=subprocess.DEVNULL, stdout=handle,
                                    stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        out.write_text(f"unavailable: {exc}\n")
        return "unavailable", str(exc)
    try:
        proc.wait(timeout=timeout)
        outcome = "ok" if proc.returncode == 0 else f"exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
            outcome = f"timed out after {timeout:g} s"
        except subprocess.TimeoutExpired:
            outcome = f"abandoned pid {proc.pid} after {timeout:g} s"
    try:
        text = out.read_text(errors="replace")
    except OSError:
        text = ""
    return outcome, text


def blocked_pids(ps_text: str) -> List[int]:
    """Pids in uninterruptible wait in `ps -axo pid,ppid,stat,...` output:
    state D on Linux, U on macOS."""
    pids = []
    for line in ps_text.splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) >= 3 and parts[0].isdigit() and parts[2][:1] in ("D", "U"):
            pids.append(int(parts[0]))
    return pids


def in_open(sample_text: str) -> bool:
    """A `sample` stack that is inside an open() system call."""
    return any(name in sample_text for name in ("__open", "open$NOCANCEL", "__open_nocancel"))


def capture_evidence(directory: pathlib.Path, path: str, probe: Dict[str, Any],
                     run: Optional[Runner] = None,
                     timeout: Optional[float] = None) -> Dict[str, Any]:
    """Save the log, the blocked processes and stack samples for one episode."""
    run = run or bounded
    timeout = timeout or _env_float("TARTCI_DISK_EVIDENCE_TIMEOUT_S", EVIDENCE_TIMEOUT_S)
    directory.mkdir(parents=True, exist_ok=True)
    files: Dict[str, str] = {}
    outcome, _ = run(["/usr/bin/log", "show", "--last", "5m", "--style", "compact",
                      "--predicate", LOG_PREDICATE], timeout, directory / "log-show.txt")
    files["log-show.txt"] = outcome
    raw = directory / "ps-all.txt"
    outcome, ps_text = run(["/bin/ps", "-axo", "pid,ppid,stat,etime,wchan,command"],
                           min(timeout, 20.0), raw)
    lines = ps_text.splitlines()
    blocked = blocked_pids(ps_text)
    keep = {str(pid) for pid in blocked}
    (directory / "ps-blocked.txt").write_text("\n".join(
        lines[:1] + [line for line in lines[1:] if line.split(None, 1)[:1] and
                     line.split(None, 1)[0] in keep]) + "\n")
    raw.unlink(missing_ok=True)
    files["ps-blocked.txt"] = outcome
    candidates: List[int] = []
    if isinstance(probe.get("pid"), int):
        candidates.append(int(probe["pid"]))
    candidates += [pid for pid in blocked if pid not in candidates]
    stuck_in_open = []
    for pid in candidates[:MAX_SAMPLES]:
        name = f"sample-{pid}.txt"
        outcome, text = run(["/usr/bin/sample", str(pid), str(SAMPLE_SECONDS)],
                            min(timeout, SAMPLE_SECONDS + 20.0), directory / name)
        files[name] = outcome
        if in_open(text):
            stuck_in_open.append(pid)
    summary = {"path": path, "probe": probe, "blocked_pids": blocked,
               "sampled": candidates[:MAX_SAMPLES], "stuck_in_open": stuck_in_open,
               "files": files}
    (directory / "evidence.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary


# ── one pass ───────────────────────────────────────────────────────────────

def alert_text(host: str, target: str, path: str, since: str, probe: Dict[str, Any],
               evidence_dir: pathlib.Path) -> Tuple[str, str]:
    """A title and body readable from a phone notification."""
    seen = (f"{probe.get('elapsed_s')} s" if probe.get("status") == "slow"
            else f"no answer within {probe.get('elapsed_s')} s")
    title = f"[tartci] {host}: writes on {path} stalling since {since}"
    body = "\n".join([
        f"{host}: a create+fsync+unlink on {path} took {seen} (threshold {THRESHOLD_S:g} s).",
        f"Run: ssh {target} 'tartci disk status'",
        "Fix: if a probe is stuck in the kernel (evidence.json `stuck_in_open`), the volume "
        "is wedged and only a reboot clears it; take the host out of the pool first. "
        "Otherwise check the enclosure link and free space.",
        "",
        f"Probe: {json.dumps(probe, sort_keys=True)}",
        f"Evidence: {evidence_dir} (kernel/APFS log, blocked processes, stack samples)",
        "",
        f"This issue closes itself after {RECOVER_PASSES} fast probes in a row.",
    ])
    return title, body


def _scratch(directory: pathlib.Path) -> bool:
    """A state dir under the temp dir is a test's; it never reaches GitHub
    unless the test passes its own issue opener."""
    scratch = os.path.realpath(tempfile.gettempdir())
    return os.path.realpath(str(directory)).startswith(scratch + os.sep)


def watch_path(path: str, directory: pathlib.Path, now: float, *,
               issue: Any = None, close: Any = None, run: Optional[Runner] = None,
               host: Optional[str] = None, target: Optional[str] = None) -> Dict[str, Any]:
    """Probe one path and drive its episode."""
    state_path = directory / f"path-{_key(path)}.json"
    state = host_off._read_json(state_path) or {}
    threshold = _env_float("TARTCI_DISK_THRESHOLD_S", THRESHOLD_S)
    timeout = _env_float("TARTCI_DISK_PROBE_TIMEOUT_S", PROBE_TIMEOUT_S)
    outstanding = state.get("outstanding") if isinstance(state.get("outstanding"), dict) else None
    if outstanding and _probe_alive(int(outstanding.get("pid", 0))):
        probe = {"status": "timeout", "pid": int(outstanding["pid"]), "stuck": True,
                 "elapsed_s": round(now - float(outstanding.get("started", now)), 3),
                 "detail": "the previous probe is still running; no new probe started"}
    else:
        probe = run_probe(path, timeout, threshold)
        if probe.get("status") == "timeout" and probe.get("stuck"):
            state["outstanding"] = {"pid": probe["pid"], "started": now - probe["elapsed_s"]}
        else:
            state.pop("outstanding", None)
    alarm = probe["status"] in ("slow", "timeout")
    if alarm:
        state["ok_streak"] = 0
        state.setdefault("since", _iso(now))
        state["worst"] = max(probe["elapsed_s"], float(state.get("worst") or 0))
    elif probe["status"] == "ok":
        state["ok_streak"] = int(state.get("ok_streak") or 0) + 1
    since = state.get("since")
    resolved = bool(since) and int(state.get("ok_streak") or 0) >= RECOVER_PASSES
    if host is None or target is None:
        host, target = _alert_host()
    evidence_dir = directory / "evidence" / f"{_key(path)}-{str(since or '').replace(':', '')}"

    def raise_event() -> None:
        summary = capture_evidence(evidence_dir, path, probe, run=run)
        state["evidence"] = str(evidence_dir)
        host_off.event(directory, "disk_latency_slow",
                       f"{host}: {path} {probe['status']} {probe['elapsed_s']} s",
                       {"path": path, "since": since, "probe": probe,
                        "evidence": str(evidence_dir),
                        "stuck_in_open": summary.get("stuck_in_open")}, now)

    switched_on = os.environ.get("TARTCI_DISK_WATCH_ISSUE", "1") != "0"
    out = host_off.episode_alert(
        directory / f"alert-{_key(path)}.json", active=alarm or (bool(since) and not resolved),
        resolved=resolved, since=since, raise_event=raise_event,
        render=lambda: alert_text(host, target, path, str(since), probe, evidence_dir),
        issue=issue, close=close,
        issues_enabled=switched_on and (issue is not None or not _scratch(directory)))
    if out.get("closed"):
        host_off.event(directory, "disk_latency_recovered", f"{host}: {path} writes fast again",
                       {"path": path, "since": since, "worst_s": state.get("worst")}, now)
        for name in ("since", "worst", "evidence"):
            state.pop(name, None)
    state["last"] = {"at": _iso(now), **probe}
    host_off._write_json(state_path, state)
    return {"path": path, "probe": probe, "episode_since": state.get("since"),
            "evidence": state.get("evidence"), **out}


def watch(now: Optional[float] = None, directory: Optional[pathlib.Path] = None,
          **kwargs: Any) -> Dict[str, Any]:
    """One pass over every watched path, under a lock so two never overlap."""
    now = time.time() if now is None else now
    directory = directory or state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "watch.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                return {"skipped": "another pass holds the lock", "paths": []}
            raise
        results = [watch_path(path, directory, now, **kwargs) for path in watch_paths()]
    return {"at": _iso(now), "paths": results}


def status(directory: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    directory = directory or state_dir()
    rows = []
    for path in watch_paths():
        state = host_off._read_json(directory / f"path-{_key(path)}.json") or {}
        rows.append({"path": path, "last": state.get("last"), "since": state.get("since"),
                     "outstanding": state.get("outstanding"), "evidence": state.get("evidence")})
    return {"paths": rows}


def _line(result: Dict[str, Any]) -> str:
    probe = result.get("probe") or result.get("last") or {}
    text = f"{result['path']}: {probe.get('status', 'never probed')}"
    if probe.get("elapsed_s") is not None:
        text += f" {probe['elapsed_s']} s"
    if result.get("episode_since") or result.get("since"):
        text += f"; stalling since {result.get('episode_since') or result.get('since')}"
    if result.get("issue"):
        text += f"; issue #{result['issue']}"
    return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci disk")
    parser.add_argument("action", choices=("watch", "status"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = watch() if args.action == "watch" else status()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if result.get("skipped"):
            print(f"disk-watchdog: skipped: {result['skipped']}")
        if not result["paths"]:
            print("disk-watchdog: no volume paths configured (TARTCI_DISK_WATCH_PATHS, "
                  "or host.tart_home / host.cache_root in the profile)")
        for row in result["paths"]:
            print(f"{_iso(time.time())} disk-watchdog: {_line(row)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
