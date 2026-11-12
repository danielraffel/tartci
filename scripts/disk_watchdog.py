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

The same pass checks Xcode's device filesystem. CoreDevice mounts a
network-paired iPhone at ~/Library/Developer/CoreDevice/DeviceFS (type
devicefs). When its DeviceFS helper wedges, every stat of the mount blocks in
the kernel for good: on m3 from 2026-10-18 it left about 21 unkillable lsof and
rg processes and hung the Shipyard sandbox guardian, and on m5studio on
2026-10-22 an rg over $HOME sat in it for 19 h while it looked like an external
volume had failed. The pass stats each devicefs mount in a child probe. A
wedge is confirmed by a probe the kernel keeps past SIGKILL, or by no answer on
two passes in a row. The pass then kills the DeviceFS helper that is stuck in
the kernel (state U, as the wedged helper was on m5studio while a healthy one
sat in S) and force-unmounts the mount, and reports a `devicefs_wedged`
episode through the same alert path. Processes blocked on the mount
(including this watchdog's probes) are released once the stuck helper is
killed: on m5studio a SIGKILL to the helper's pid freed a 19 h rg with no
reboot. Any still stuck after that need a reboot; the issue says how many.
TARTCI_DEVICEFS_REMEDIATE=0 reports without acting.

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
import re
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
from python_child import child_python  # noqa: E402

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

# A devicefs probe only stats and lists the mount root: a wedged DeviceFS
# helper blocks both, a healthy one answers from its own cache.
DEVICEFS_PROBE = """
import os, sys
os.stat(sys.argv[-1])
os.listdir(sys.argv[-1])
print("0")
"""
DEVICEFS_HELPER = "DeviceFS.appex/Contents/MacOS/DeviceFS"
# Each probe of a wedged mount can leave one process the kernel never returns;
# past this many still alive, the pass stops starting new ones.
DEVICEFS_MAX_STUCK = 3
# Consecutive passes with no answer that confirm a wedge when no probe has
# outlived SIGKILL.
DEVICEFS_CONFIRM_PASSES = 2
# A status older than this many agent intervals means the agent is not running.
STALE_S = 600.0

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

def probe_command(path: str, override_env: str = "TARTCI_DISK_PROBE_COMMAND",
                  script: str = PROBE) -> List[str]:
    """The argv of one probe: the override (a JSON list) or this interpreter
    running `script`, then the marker and the path."""
    override = os.environ.get(override_env)
    if override:
        command = json.loads(override)
        if not (isinstance(command, list) and command
                and all(isinstance(part, str) for part in command)):
            raise ValueError(f"{override_env} must be a JSON list of strings")
    else:
        command = [child_python(), "-c", script]
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


def run_probe(path: str, timeout: float, threshold: float,
              command: Optional[List[str]] = None) -> Dict[str, Any]:
    """One probe of `path`, waited on for at most `timeout`.

    Returns status ok, slow, timeout or error, the wall time the pass saw,
    and for a timeout the pid left behind (`stuck`: it outlived SIGKILL).
    """
    start = time.monotonic()
    try:
        proc = subprocess.Popen(command or probe_command(path), stdin=subprocess.DEVNULL,
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
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
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


# ── Xcode device filesystem ────────────────────────────────────────────────

def devicefs_mounts(mount_text: str) -> List[str]:
    """Mount points of type devicefs in `/sbin/mount` output."""
    found = []
    for line in mount_text.splitlines():
        match = re.match(r"^.* on (/.*) \(devicefs[,)]", line)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def devicefs_helpers(ps_text: str) -> List[Dict[str, Any]]:
    """DeviceFS helper processes in `ps -axo pid,stat,command` output. Only
    the executable is matched, so a command that merely names the helper's
    path (an lsof, a grep) is never taken for it."""
    helpers = []
    for line in ps_text.splitlines()[1:]:
        parts = line.split(None, 2)
        if (len(parts) == 3 and parts[0].isdigit()
                and parts[2].split(None, 1)[0].endswith(DEVICEFS_HELPER)):
            helpers.append({"pid": int(parts[0]), "stat": parts[1]})
    return helpers


def _list_devicefs(directory: pathlib.Path, run: Runner) -> Optional[List[str]]:
    """The devicefs mount points, or None when `mount` did not answer. `mount`
    reads the kernel's mount table without touching any filesystem, so a
    wedged mount cannot block it; an empty answer from a failed run is not
    evidence that nothing is mounted."""
    raw = os.environ.get("TARTCI_DEVICEFS_MOUNTS")
    if raw is not None:
        return [part for part in raw.split(os.pathsep) if part]
    out = directory / "devicefs-mount.txt"
    outcome, text = run(["/sbin/mount"], 10.0, out)
    out.unlink(missing_ok=True)
    return devicefs_mounts(text) if outcome == "ok" else None


def _kill(pid: int) -> str:
    """SIGKILL one pid. A process in U takes it when its call returns, so this
    never blocks."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "gone"
    except OSError as exc:
        return f"not killed: {exc}"
    return "killed"


def _umount_alive(pid: int) -> bool:
    """True while `pid` is still the umount this watchdog started, not a
    reused pid (state outlives the pass and the host's uptime)."""
    if not _alive(pid):
        return False
    try:
        out = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell: never start a second one beside it
    return out.stdout.strip().startswith("/sbin/umount")


def _devicefs_remediate(directory: pathlib.Path, mounts: List[str], run: Runner,
                        helpers: List[Dict[str, Any]], state: Dict[str, Any],
                        stuck_probe: bool) -> List[str]:
    """Kill the DeviceFS helper by pid, then force-unmount any wedged mount
    it left. Killing the helper is what releases the mount; CoreDevice starts
    a fresh one when it next needs the phone."""
    scratch = directory / "devicefs-remediate.txt"
    # Only a helper stuck in the kernel (U) is the one behind the wedge; a
    # healthy helper serving another phone is left alone.
    killed = [f"DeviceFS helper pid {h['pid']} ({h['stat']}): {_kill(int(h['pid']))}"
              for h in helpers if str(h.get("stat", "")).startswith("U")]
    actions = killed or ["no DeviceFS helper is stuck in the kernel; none killed"]
    if not killed and not stuck_probe:
        # Two slow answers with nothing stuck anywhere may be a far-away phone,
        # not a wedge: leave the mount alone and let the next passes decide.
        actions.append("umount -f skipped: no helper or probe is stuck in the kernel")
        return actions
    listed = _list_devicefs(directory, run)
    # When mount did not answer, try the unmount anyway: it only names the
    # wedged mount, and a mount that is already gone just fails.
    still = set(mounts if listed is None else listed)
    pending = state.get("umount_pid")
    if not (isinstance(pending, int) and _umount_alive(pending)):
        state.pop("umount_pid", None)
        pending = None
    for mount in mounts:
        if mount not in still:
            continue
        if pending is not None:
            # An earlier umount is itself stuck in the kernel; another would
            # only add a second unkillable process.
            actions.append(f"umount -f {mount}: skipped, pid {pending} from an earlier "
                           "pass is still stuck")
            continue
        outcome, _ = run(["/sbin/umount", "-f", mount], 15.0, scratch)
        abandoned = re.match(r"abandoned pid (\d+)", outcome)
        if abandoned:
            state["umount_pid"] = int(abandoned.group(1))
        actions.append(f"umount -f {mount}: {outcome}")
    scratch.unlink(missing_ok=True)
    return actions


def devicefs_alert_text(host: str, target: str, since: str, probe: Dict[str, Any],
                        actions: List[str], remaining: List[str], stuck: int,
                        evidence_dir: pathlib.Path) -> Tuple[str, str]:
    title = f"[tartci] {host}: Xcode device filesystem (DeviceFS) wedged since {since}"
    lines = [
        f"{host}: a stat of the iPhone mount {probe.get('path')} got no answer within "
        f"{probe.get('elapsed_s')} s. Anything that walks it (rg or find over $HOME, "
        "lsof with a path, du) blocks in the kernel and cannot be killed.",
        f"Run: ssh {target} 'tartci disk status'",
        "Done: " + ("; ".join(actions) if actions else
                    "nothing (TARTCI_DEVICEFS_REMEDIATE=0)") + ".",
        ("Still mounted: " + ", ".join(remaining) + ". " if remaining else "")
        + (f"{stuck} probe(s) are still stuck in the kernel after the helper was "
           "killed; if they do not clear on the next passes, restart the host when "
           "it is idle." if stuck else ""),
        "Prevent: turn off \"Connect via network\" for the phone in Xcode > Devices on "
        "hosts that do not need it, and keep recursive searches off $HOME "
        "(rg --one-file-system, find -x, du -x).",
        "",
        f"Probe: {json.dumps(probe, sort_keys=True)}",
        f"Evidence: {evidence_dir} (blocked processes, stack samples)",
        "",
        f"This issue closes itself after {RECOVER_PASSES} healthy passes in a row.",
    ]
    return title, "\n".join(line for line in lines if line is not None)


def watch_devicefs(directory: pathlib.Path, now: float, *, issue: Any = None,
                   close: Any = None, run: Optional[Runner] = None,
                   host: Optional[str] = None, target: Optional[str] = None) -> Dict[str, Any]:
    """Probe every devicefs mount; on a wedge, clear it and drive its episode.

    A state dir under the temp dir is a test's: it looks at no real mount
    unless the test names them (TARTCI_DEVICEFS_MOUNTS) or passes `run`.
    """
    state_path = directory / "devicefs.json"
    state = host_off._read_json(state_path) or {}
    if run is None and _scratch(directory) and "TARTCI_DEVICEFS_MOUNTS" not in os.environ:
        return {"mounts": [], "probes": [], "episode_since": state.get("since")}
    run = run or bounded
    timeout = _env_float("TARTCI_DISK_PROBE_TIMEOUT_S", PROBE_TIMEOUT_S)
    stuck = [pid for pid in state.get("stuck", []) if isinstance(pid, int) and _probe_alive(pid)]
    listed = _list_devicefs(directory, run)
    mounts = listed or []
    probes = []
    if listed is None:
        probes.append({"path": None, "status": "unprobed", "elapsed_s": 0.0,
                       "detail": "/sbin/mount did not answer; mounts unknown"})
    for mount in mounts:
        if len(stuck) >= DEVICEFS_MAX_STUCK:
            # Not a reading: the pass cannot tell a healthy remount from a
            # wedge without probing, so it neither acts nor resolves.
            probe = {"status": "unprobed", "elapsed_s": 0.0,
                     "detail": f"{len(stuck)} earlier probes are still stuck in the kernel; "
                               "none started until a reboot clears them"}
        else:
            try:
                command = probe_command(mount, "TARTCI_DEVICEFS_PROBE_COMMAND", DEVICEFS_PROBE)
            except ValueError as exc:
                command = None
                probe = {"status": "error", "elapsed_s": 0.0, "detail": str(exc)}
            if command:
                # Only a probe that never answers is a wedge: a slow answer is
                # the phone being far away, not the helper being stuck.
                probe = run_probe(mount, timeout, timeout, command=command)
                if probe.get("status") == "timeout" and probe.get("stuck"):
                    stuck.append(int(probe["pid"]))
        probes.append({"path": mount, **probe})
    unanswered = [p for p in probes if p["status"] == "timeout"]
    unprobed = any(p["status"] == "unprobed" for p in probes)
    # Consecutive means consecutive: a pass that could not probe breaks the run.
    if unanswered and not unprobed:
        state["no_answer_passes"] = int(state.get("no_answer_passes") or 0) + 1
    else:
        state["no_answer_passes"] = 0
    # A wedge is confirmed by a probe the kernel kept past SIGKILL, or by no
    # answer on consecutive passes; one slow listing is never acted on.
    wedged = [p for p in unanswered
              if p.get("stuck") or int(state["no_answer_passes"]) >= DEVICEFS_CONFIRM_PASSES]
    actions: List[str] = []
    remaining: List[str] = []
    helpers: List[Dict[str, Any]] = []
    if wedged:
        # The helpers' state before anything is killed: U is stuck in the kernel.
        out = directory / "devicefs-ps.txt"
        _, ps_text = run(["/bin/ps", "-axo", "pid,stat,command"], 10.0, out)
        out.unlink(missing_ok=True)
        helpers = devicefs_helpers(ps_text)
        state["ok_streak"] = 0
        state.setdefault("since", _iso(now))
    elif unanswered or unprobed:
        state["ok_streak"] = 0  # not healthy yet, and not a reading to act on
    else:
        state["ok_streak"] = int(state.get("ok_streak") or 0) + 1
    since = state.get("since")
    evidence_dir = directory / "evidence" / f"devicefs-{str(since or '').replace(':', '')}"
    first = wedged[0] if wedged else {}
    if wedged and not state.get("evidence"):
        # Before anything is killed, while the stuck helper's stack still exists.
        summary = capture_evidence(evidence_dir, str(first.get("path")), first, run=run)
        state["evidence"] = str(evidence_dir)
        state["blocked_pids"] = summary.get("blocked_pids")
    if wedged and os.environ.get("TARTCI_DEVICEFS_REMEDIATE", "1") != "0":
        actions = _devicefs_remediate(directory, [p["path"] for p in wedged], run, helpers,
                                      state, any(p.get("stuck") for p in wedged))
        after = _list_devicefs(directory, run)
        if after is None:
            actions.append("mount state after the repair unknown: /sbin/mount did not answer")
        else:
            remaining = [p["path"] for p in wedged if p["path"] in after]
    resolved = bool(since) and int(state.get("ok_streak") or 0) >= RECOVER_PASSES
    if host is None or target is None:
        host, target = _alert_host()

    def raise_event() -> None:
        host_off.event(directory, "devicefs_wedged",
                       f"{host}: {first.get('path')} no answer within {first.get('elapsed_s')} s",
                       {"probe": first, "helpers": helpers, "actions": actions,
                        "still_mounted": remaining,
                        "stuck_probes": stuck, "evidence": state.get("evidence"),
                        "blocked_pids": state.get("blocked_pids")}, now)

    switched_on = os.environ.get("TARTCI_DISK_WATCH_ISSUE", "1") != "0"
    out = host_off.episode_alert(
        directory / "alert-devicefs.json", active=bool(wedged) or (bool(since) and not resolved),
        resolved=resolved, since=since, raise_event=raise_event,
        render=lambda: devicefs_alert_text(host, target, str(since), first, actions,
                                           remaining, len(stuck), evidence_dir),
        issue=issue, close=close,
        issues_enabled=switched_on and (issue is not None or not _scratch(directory)))
    if out.get("closed"):
        host_off.event(directory, "devicefs_recovered", f"{host}: device filesystem healthy",
                       {"since": since}, now)
        for name in ("since", "evidence", "blocked_pids", "no_answer_passes", "umount_pid"):
            state.pop(name, None)
    state["stuck"] = stuck
    state["last"] = {"at": _iso(now), "mounts": mounts, "wedged": [p["path"] for p in wedged],
                     "actions": actions}
    host_off._write_json(state_path, state)
    return {"mounts": mounts, "probes": probes, "actions": actions, "still_mounted": remaining,
            "stuck_probes": stuck, "episode_since": state.get("since"),
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
        devicefs = watch_devicefs(directory, now, **kwargs)
    return {"at": _iso(now), "paths": results, "devicefs": devicefs}


def _age(last: Any, now: float) -> Optional[float]:
    try:
        stamp = dt.datetime.strptime(str(last["at"]), "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        return None
    return round(now - stamp.replace(tzinfo=dt.timezone.utc).timestamp(), 1)


def status(directory: Optional[pathlib.Path] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Each path's last probe, with its age. A last probe older than STALE_S
    is marked stale: the agent stopped running, and the reading is history."""
    directory = directory or state_dir()
    now = time.time() if now is None else now
    stale_after = _env_float("TARTCI_DISK_STALE_S", STALE_S)
    rows = []
    for path in watch_paths():
        state = host_off._read_json(directory / f"path-{_key(path)}.json") or {}
        age = _age(state.get("last"), now)
        rows.append({"path": path, "last": state.get("last"), "since": state.get("since"),
                     "outstanding": state.get("outstanding"), "evidence": state.get("evidence"),
                     "age_s": age, "stale": age is None or age > stale_after})
    devicefs = host_off._read_json(directory / "devicefs.json") or {}
    return {"paths": rows, "devicefs": {"last": devicefs.get("last"),
                                        "since": devicefs.get("since"),
                                        "stuck_probes": devicefs.get("stuck", []),
                                        "evidence": devicefs.get("evidence")}}


def _line(result: Dict[str, Any]) -> str:
    probe = result.get("probe") or result.get("last") or {}
    text = f"{result['path']}: {probe.get('status', 'never probed')}"
    if probe.get("elapsed_s") is not None:
        text += f" {probe['elapsed_s']} s"
    if result.get("episode_since") or result.get("since"):
        text += f"; stalling since {result.get('episode_since') or result.get('since')}"
    if result.get("issue"):
        text += f"; issue #{result['issue']}"
    if result.get("stale") and result.get("age_s") is not None:
        text += (f"; STALE: last probed {result['age_s'] / 60:.0f} min ago, so the "
                 "disk-watchdog agent is not running")
    return text


def _devicefs_line(row: Dict[str, Any]) -> str:
    last = row.get("last") or {}
    mounts = row.get("mounts", last.get("mounts")) or []
    wedged = [p["path"] for p in row.get("probes", []) if p.get("status") == "timeout"] \
        if "probes" in row else last.get("wedged") or []
    text = f"devicefs: {len(mounts)} mount(s)"
    text += f", wedged: {', '.join(wedged)}" if wedged else ", healthy"
    for action in row.get("actions") or last.get("actions") or []:
        text += f"; {action}"
    stuck = row.get("stuck_probes") or []
    if stuck:
        text += f"; {len(stuck)} probe(s) stuck until reboot"
    if row.get("episode_since") or row.get("since"):
        text += f"; episode since {row.get('episode_since') or row.get('since')}"
    if row.get("issue"):
        text += f"; issue #{row['issue']}"
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
            # Stamp each row with when it was probed, never with now: a status
            # read days after the agent stopped must not look current.
            at = (row.get("last") or {}).get("at") if args.action == "status" else result["at"]
            print(f"{at or 'never'} disk-watchdog: {_line(row)}")
        if result.get("devicefs") is not None:
            at = (result.get("at") if args.action == "watch"
                  else (result["devicefs"].get("last") or {}).get("at"))
            print(f"{at or 'never'} disk-watchdog: {_devicefs_line(result['devicefs'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
