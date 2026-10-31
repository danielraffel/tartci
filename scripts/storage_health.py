#!/usr/bin/env python3
"""Bounded, fail-closed storage admission probe for TartCI macOS hosts."""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from python_child import child_python

HEALTHY, DEGRADED, QUARANTINED = 0, 10, 20
TERMINATE_GRACE_SECONDS = 0.5


def default_state(path: Path) -> Path:
    host = os.uname().nodename.split(".", 1)[0] or "host"
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:12]
    return Path.home() / ".tartci" / "state" / f"storage-health-{host}-{digest}.json"


def state_path_is_safe(state_path: Path, target: Path, *, resolve_target: bool = True) -> bool:
    """Reject state paths that could resolve into the probed volume.

    Do this lexically and inspect only the state path's existing components.
    Resolving the complete path can itself touch a wedged external volume;
    the admission guard must not do that before its bounded worker starts.
    """
    state = os.path.abspath(os.fspath(state_path))
    probe = os.path.abspath(os.fspath(target))
    try:
        if os.path.commonpath((state, probe)) == probe:
            return False
    except ValueError:
        return False

    if resolve_target:
        # Only the bounded worker follows aliases. The outer timeout path uses
        # lexical-only validation so reporting a stuck probe cannot itself
        # touch the wedged volume.
        try:
            state_real = os.path.realpath(state)
            probe_real = os.path.realpath(probe)
            if os.path.commonpath((state_real, probe_real)) == probe_real:
                return False
        except (OSError, ValueError):
            return False

    try:
        shared = os.path.commonpath((state, probe))
    except ValueError:
        return False
    state_suffix = os.path.relpath(state, shared)
    current = shared
    for component in Path(state_suffix).parts:
        if component == os.curdir:
            continue
        current = os.path.join(current, component)
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            break
        except OSError:
            return False
        if stat.S_ISLNK(mode):
            return False
    return True


def load_state(path: Path, target: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise ValueError("schema")
        if value.get("path") != str(target):
            raise ValueError("path")
        if value.get("status") not in {"healthy", "degraded", "quarantined"}:
            raise ValueError("status")
        for key in ("failure_streak", "healthy_streak"):
            if not isinstance(value.get(key), int) or value[key] < 0:
                raise ValueError(key)
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        return {"state_unreadable": True, "status": "quarantined"}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(directory)
            except OSError as exc:
                if exc.errno != errno.EINVAL:
                    raise
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def quarantine_result(path: Path, state_path: Path, args: argparse.Namespace,
                      reason: str) -> dict:
    """Build the fail-closed result used when the worker cannot persist state."""
    return {
        "schema": 1,
        "status": "quarantined",
        "reason": reason,
        "path": str(path),
        "state": str(state_path),
        "failure_streak": args.failure_threshold,
        "healthy_streak": 0,
    }


def persist_quarantine_marker(state_path: Path) -> None:
    """Leave a durable local marker when the JSON state cannot be written."""
    Path(str(state_path) + ".quarantine").touch(exist_ok=True)


def durable_quarantine(path: Path, state_path: Path, args: argparse.Namespace,
                       reason: str) -> dict:
    """Persist quarantine before returning an exceptional worker result.

    The marker is checked before a later probe, even when an older healthy JSON
    state remains. This closes the window where a timeout or state write error
    could be followed by a fresh invocation that reopened admission.
    """
    result = quarantine_result(path, state_path, args, reason)
    try:
        persist_quarantine_marker(state_path)
    except OSError:
        # State may itself be unavailable. The caller still fails closed and
        # reports the exact persistence failure; there is no safe write target.
        pass
    try:
        save_state(state_path, result)
    except OSError:
        pass
    return result


def probe(path: Path) -> tuple[bool, str, float]:
    """Inspect the store directory without changing its contents.

    The storage admission check is deliberately metadata-only.  Creating a
    temporary file, fsyncing it, or unlinking it would turn a diagnostic gate
    into a write test against the potentially wedged external volume and
    would violate the read-only recovery contract.  ``open``/``fstat`` still
    exercise the APFS lookup path that can block, and the caller runs this
    function in a bounded child process.
    """
    started = time.monotonic()
    fd = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(str(path), flags)
        try:
            stat = os.fstat(fd)
        finally:
            os.close(fd)
            fd = -1
        if not stat:
            raise OSError("directory metadata unavailable")
        return True, "directory_metadata_ok", time.monotonic() - started
    except FileNotFoundError:
        return False, "path_missing", time.monotonic() - started
    except PermissionError:
        return False, "permission_denied", time.monotonic() - started
    except InterruptedError:
        return False, "interrupted_system_call", time.monotonic() - started
    except OSError as exc:
        return False, f"os_error_{exc.errno or 'unknown'}", time.monotonic() - started
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _group_exists(pgid: int) -> bool:
    """Return whether a private process group still has a member."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # We own the group leader; permission failure still means the group is
        # present and must not be treated as safely reaped.
        return True
    return True


def reap_after_kill(child: subprocess.Popen[object]) -> bool:
    """Kill and boundedly reap a child process group.

    The probe worker can itself have a probe child blocked in a filesystem
    syscall. Killing only the worker leaves that child holding descriptors and
    can turn a bounded supervisor into an orphan leak. Every caller starts a
    private session, so kill the complete process group and verify that it has
    disappeared before returning.
    """
    pgid = child.pid
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # The leader may have exited between the timeout and this call. The
        # direct fallback is harmless on platforms without usable groups.
        try:
            child.kill()
        except OSError:
            pass
    try:
        child.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except OSError:
            pass
        try:
            child.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    # A successfully reaped leader can still have a descendant in the group.
    # Give normal signal delivery a bounded window, then make one final group
    # kill. A D-state process can remain unreapable; report that to the caller
    # while preserving the supervisor's bounded return.
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while _group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    return not _group_exists(pgid)


def run_probe_bounded(path: Path, timeout: float) -> tuple[bool, str, float]:
    child = subprocess.Popen(
        [child_python(), str(Path(__file__).resolve()), "--probe-only", "--path", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True,
    )
    try:
        stdout, _ = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Do not wait for a filesystem syscall that is stuck in the kernel. The
        # supervisor remains bounded; any orphan is harmless and holds no Tart VM.
        child.kill()
        reaped = reap_after_kill(child)
        if child.stdout is not None:
            child.stdout.close()
        return False, "probe_timeout" if reaped else "probe_timeout_unreaped", timeout
    try:
        payload = json.loads(stdout)
        return bool(payload["ok"]), str(payload["reason"]), float(payload["elapsed_seconds"])
    except (ValueError, TypeError, KeyError):
        return False, "probe_failed" if child.returncode else "probe_malformed", 0.0


# Reasons that are evidence about the store itself rather than about the
# process that observed it. A probe child that SIGKILL could not reap is stuck
# in an uninterruptible kernel wait on the volume, so every lane must stop
# admitting until it has seen the store answer again for itself.
STORE_WIDE_FAILURES = frozenset({"probe_timeout_unreaped"})
# Observer records older than this are dropped so the shared state stays small.
OBSERVER_RETENTION_SECONDS = 7 * 24 * 3600


def _marker_epoch(marker: Path) -> int | None:
    try:
        return marker.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _observer_record(document: dict, observer: str) -> dict:
    """This observer's hysteresis record from a shared state document.

    Every admitting lane on a host probes the same store and shares one state
    file. Hysteresis is kept per observer: one lane whose own probe keeps
    timing out must not reset the healthy streak of a lane whose probe the
    store answers in milliseconds. The legacy (unnamed) observer keeps
    reading the top-level fields, so an older state file keeps its streaks.
    """
    observers = document.get("observers")
    if isinstance(observers, dict) and isinstance(observers.get(observer), dict):
        record = dict(observers[observer])
    elif observer == "" and document:
        record = {key: document[key] for key in
                  ("status", "failure_streak", "healthy_streak", "marker_ack")
                  if key in document}
    else:
        record = {}
    for key in ("failure_streak", "healthy_streak"):
        if not isinstance(record.get(key), int) or record[key] < 0:
            record[key] = 0
    if record.get("status") not in {"healthy", "degraded", "quarantined"}:
        record["status"] = "healthy" if not record.get("failure_streak") else "degraded"
    return record


def _prune_observers(observers: dict, now: float) -> dict:
    kept = {}
    for name, record in observers.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            continue
        try:
            checked = time.mktime(time.strptime(str(record.get("checked_at", "")),
                                                "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        except ValueError:
            continue
        if now - checked <= OBSERVER_RETENTION_SECONDS:
            kept[name] = record
    return kept


def next_observer_state(record: dict, ok: bool, reason: str, *, marker_epoch: int | None,
                        failure_threshold: int, recovery_threshold: int) -> tuple[dict, str]:
    """Advance one observer's hysteresis; return (record, reason).

    A host quarantine marker the observer has not yet acknowledged forces it
    into quarantine and restarts its recovery window, so a store-wide failure
    seen by any lane is honoured by every lane until each sees the store
    answer `recovery_threshold` consecutive times itself.
    """
    failures = int(record.get("failure_streak", 0))
    healthy = int(record.get("healthy_streak", 0))
    prior_status = str(record.get("status", "healthy"))
    ack = record.get("marker_ack")
    if marker_epoch is not None and ack != marker_epoch:
        prior_status, healthy, ack = "quarantined", 0, marker_epoch
    if ok:
        failures, healthy = 0, healthy + 1
        if prior_status == "quarantined" and healthy < recovery_threshold:
            status, reason = "quarantined", "recovery_check_pending"
        else:
            status = "healthy"
    else:
        failures, healthy = failures + 1, 0
        status = ("quarantined" if failures >= failure_threshold
                  or prior_status == "quarantined" else "degraded")
    return ({"status": status, "failure_streak": failures, "healthy_streak": healthy,
             "marker_ack": ack}, reason)


def run_worker(args: argparse.Namespace) -> int:
    path = Path(args.path)
    state_path = Path(args.state) if args.state else default_state(path)
    observer = str(getattr(args, "observer", "") or "")
    if not state_path_is_safe(state_path, path):
        print(json.dumps(quarantine_result(path, state_path, args,
                                            "health_state_path_unsafe"),
                         sort_keys=True))
        return QUARANTINED
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    quarantine_marker = Path(str(state_path) + ".quarantine")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            # Read under the lock, then release it for the probe: a lane whose
            # probe runs to its timeout must not hold every other lane's
            # worker past the supervisor deadline.
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            unreadable = load_state(state_path, path).get("state_unreadable")
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            if unreadable:
                result = {"schema": 1, "status": "quarantined", "reason": "health_state_unreadable", "path": str(path), "state": str(state_path), "failure_streak": args.failure_threshold, "healthy_streak": 0}
                print(json.dumps(result, sort_keys=True))
                return QUARANTINED
            ok, reason, elapsed = run_probe_bounded(path, args.timeout)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            document = load_state(state_path, path)
            if document.get("state_unreadable"):
                result = {"schema": 1, "status": "quarantined", "reason": "health_state_unreadable", "path": str(path), "state": str(state_path), "failure_streak": args.failure_threshold, "healthy_streak": 0}
                print(json.dumps(result, sort_keys=True))
                return QUARANTINED
            record = _observer_record(document, observer)
            # The legacy single observer raises the host marker whenever it
            # quarantines, as before (below). A named observer raises it only
            # on evidence about the store itself, at once.
            raise_marker = (not ok) and observer != "" and reason in STORE_WIDE_FAILURES
            if raise_marker:
                quarantine_marker.touch(exist_ok=True)
            record, reason = next_observer_state(
                record, ok, reason, marker_epoch=_marker_epoch(quarantine_marker),
                failure_threshold=args.failure_threshold,
                recovery_threshold=args.recovery_threshold)
            if raise_marker:
                record["status"] = "quarantined"
            status = record["status"]
            checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            result = {"schema": 1, "status": status, "reason": reason, "path": str(path), "state": str(state_path), "failure_streak": record["failure_streak"], "healthy_streak": record["healthy_streak"], "elapsed_seconds": round(elapsed, 3), "checked_at": checked_at}
            if observer:
                result["observer"] = observer
            observers = document.get("observers") if isinstance(document.get("observers"), dict) else {}
            observers = _prune_observers(observers, time.time())
            observers[observer] = dict(record, reason=reason, checked_at=checked_at)
            saved = dict(result, observers=observers, marker_ack=record["marker_ack"])
            try:
                save_state(state_path, saved)
                if status == "quarantined" and observer == "":
                    quarantine_marker.touch(exist_ok=True)
                    observers[observer]["marker_ack"] = _marker_epoch(quarantine_marker)
                    save_state(state_path, dict(saved, observers=observers,
                                                marker_ack=observers[observer]["marker_ack"]))
                elif (status == "healthy" and record["marker_ack"] is not None
                      and _marker_epoch(quarantine_marker) == record["marker_ack"]):
                    # This observer completed a full recovery window after the
                    # marker it acknowledged; nobody has raised a newer one.
                    quarantine_marker.unlink()
            except OSError:
                result = durable_quarantine(path, state_path, args, "health_state_write_failed")
                print(json.dumps(result, sort_keys=True))
                return QUARANTINED
            print(json.dumps(result, sort_keys=True))
            return HEALTHY if status == "healthy" else DEGRADED if status == "degraded" else QUARANTINED
    except OSError:
        result = durable_quarantine(path, state_path, args, "health_state_lock_failed")
        print(json.dumps(result, sort_keys=True))
        return QUARANTINED


def run(args: argparse.Namespace) -> int:
    command = [child_python(), str(Path(__file__).resolve()), "--storage-health-worker"] + sys.argv[1:]
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=args.timeout + 2.0)
    except subprocess.TimeoutExpired:
        reap_after_kill(child)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()
        path = Path(args.path)
        state_path = Path(args.state) if args.state else default_state(path)
        if args.state:
            # Do not write an explicit custom path after a timeout; it may be
            # an alias into the volume whose probe just wedged.
            result = quarantine_result(path, state_path, args,
                                       "health_supervisor_timeout")
            print(json.dumps(result, sort_keys=True))
            return QUARANTINED
        if not state_path_is_safe(state_path, path, resolve_target=False):
            result = quarantine_result(path, state_path, args,
                                       "health_state_path_unsafe")
            print(json.dumps(result, sort_keys=True))
            return QUARANTINED
        result = durable_quarantine(path, state_path, args, "health_supervisor_timeout")
        print(json.dumps(result, sort_keys=True))
        return QUARANTINED
    if child.returncode in (HEALTHY, DEGRADED, QUARANTINED) and stdout.strip():
        print(stdout, end="")
        return child.returncode
    if stderr.strip():
        print(stderr, file=sys.stderr, end="")
    path = Path(args.path)
    state_path = Path(args.state) if args.state else default_state(path)
    if args.state:
        result = quarantine_result(path, state_path, args, "health_worker_failed")
        print(json.dumps(result, sort_keys=True))
        return QUARANTINED
    if not state_path_is_safe(state_path, path, resolve_target=False):
        result = quarantine_result(path, state_path, args, "health_state_path_unsafe")
        print(json.dumps(result, sort_keys=True))
        return QUARANTINED
    result = durable_quarantine(path, state_path, args, "health_worker_failed")
    print(json.dumps(result, sort_keys=True))
    return QUARANTINED


def run_observe(args: argparse.Namespace) -> int:
    """Probe liveness without advancing recovery hysteresis or writing state."""
    ok, reason, elapsed = run_probe_bounded(Path(args.path), args.timeout)
    result = {
        "schema": 1,
        "status": "healthy" if ok else "quarantined",
        "reason": reason,
        "path": str(args.path),
        "state": args.state,
        "elapsed_seconds": round(elapsed, 3),
    }
    print(json.dumps(result, sort_keys=True))
    return HEALTHY if ok else QUARANTINED


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    parser.add_argument("--state", default="")
    parser.add_argument("--observer", default="",
                        help="stable id of the admitting lane; hysteresis is kept per observer")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--failure-threshold", type=int, default=2)
    parser.add_argument("--recovery-threshold", type=int, default=3)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--observe-only", action="store_true")
    parser.add_argument("--storage-health-worker", action="store_true")
    args = parser.parse_args()
    if args.probe_only:
        ok, reason, elapsed = probe(Path(args.path))
        print(json.dumps({"ok": ok, "reason": reason, "elapsed_seconds": elapsed}))
        return 0 if ok else 1
    if not (0 < args.timeout <= 60) or not (1 <= args.failure_threshold <= 10) or not (1 <= args.recovery_threshold <= 20):
        print(json.dumps({"schema": 1, "status": "quarantined", "reason": "health_configuration_invalid", "path": args.path, "state": args.state or str(default_state(Path(args.path))), "failure_streak": 0, "healthy_streak": 0}, sort_keys=True))
        return QUARANTINED
    if args.observe_only:
        return run_observe(args)
    return run_worker(args) if args.storage_health_worker else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
