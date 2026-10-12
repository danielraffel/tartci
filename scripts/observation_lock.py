"""Fair host-global observation lock with per-repository timeout backoff."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import time
import datetime as dt
import weakref
from pathlib import Path
from typing import Iterator

from bounded_subprocess import ObservationError


class ObservationBackoff(RuntimeError):
    """This repository recently timed out and must yield the host slot."""


class ObservationTimeout(TimeoutError):
    """A scanner exhausted a bounded observation timeout."""


def _process_start(pid: int) -> str | None:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "lstart="], text=True,
            stderr=subprocess.DEVNULL,
        ).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None

_ACTIVE_LOCKS: weakref.WeakSet["FairObservationLock"] = weakref.WeakSet()

def _after_fork_child() -> None:
    # Registered once at import time; supervisors may create many lock objects.
    for lock in list(_ACTIVE_LOCKS):
        if lock._holding:
            lock._write_owner(os.getpid())

os.register_at_fork(after_in_child=_after_fork_child)


def _lsof_path() -> str | None:
    return shutil.which("lsof") or next(
        (candidate for candidate in ("/usr/sbin/lsof", "/usr/bin/lsof")
         if os.access(candidate, os.X_OK)),
        None,
    )


def _live_lock_holders(path: Path) -> list[int]:
    """Return processes that still have the flock file open, including exec children."""
    # Census and launchd environments intentionally have a minimal PATH that
    # often omits /usr/sbin, where macOS installs lsof. Resolve it explicitly
    # so the live-holder probe does not silently fall back to the old
    # owner-record-only behavior.
    lsof = _lsof_path()
    if lsof is None:
        return []
    try:
        output = subprocess.check_output(
            [lsof, "-t", "--", str(path)], text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return []
    holders: list[int] = []
    for line in output.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid not in holders:
            holders.append(pid)
    return holders


def _fifo_pids(lock_path: Path) -> set[int]:
    """Return PIDs with a ticket in the lock's FIFO wait queue."""
    queue = lock_path.with_name(lock_path.name + ".fifo")
    result: set[int] = set()
    for marker in queue.glob("[0-9]*.*"):
        try:
            result.add(int(marker.name.rsplit(".", 1)[1]))
        except (IndexError, ValueError):
            continue
    return result


def _pid_is_zombie(pid: int) -> bool:
    """Return whether ps reports a process that has already exited."""
    try:
        state = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "stat="],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return state.startswith("Z")


def _timed_out(error: BaseException) -> bool:
    """Recognize typed observation timeouts through scanner wrapping layers."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ObservationError) and current.kind == "timeout":
            return True
        if isinstance(current, TimeoutError):
            return True
        current = current.__cause__ or current.__context__
    return False


class FairObservationLock:
    def __init__(self, lock_file: str, timeout: float, repo: str,
                 backoff_file: str | None = None, backoff_seconds: float = 30.0,
                 lane: str | None = None, generation: str | None = None,
                 max_age: float | None = None) -> None:
        self.lock_path = Path(lock_file)
        self.timeout = timeout
        self.repo = repo
        self.backoff_path = Path(backoff_file or (str(self.lock_path) + ".backoff.json"))
        self.backoff_seconds = backoff_seconds
        self.lane = lane or os.environ.get("TARTCI_QUEUE_LANE_ID") or f"pid-{os.getppid()}"
        self.generation = generation or os.environ.get("TARTCI_GENERATION") or "unknown"
        self.max_age = max_age if max_age is not None else float(
            os.environ.get("TARTCI_QUEUE_OBSERVATION_LOCK_MAX_AGE_SECS", "300")
        )
        self.owner_path = self.lock_path.with_name(self.lock_path.name + ".owner.json")
        self.escalation_path = self.lock_path.with_name(
            self.lock_path.name + ".escalation.json"
        )
        self.backoff_lock_path = self.backoff_path.with_name(self.backoff_path.name + ".lock")
        self.ticket_path: Path | None = None
        self.handle = None
        self._holding = False
        self._owner_pid: int | None = None
        self._owner_process_start: str | None = None

    def _backoff(self) -> dict[str, float]:
        try:
            value = json.loads(self.backoff_path.read_text())
            return {str(k): float(v) for k, v in value.items()}
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}

    def _write_backoff(self, values: dict[str, float]) -> None:
        self.backoff_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.backoff_path.with_name(
            self.backoff_path.name + f".{os.getpid()}.{time.time_ns()}.tmp"
        )
        temp.write_text(json.dumps(values, sort_keys=True, separators=(",", ":")))
        os.replace(temp, self.backoff_path)

    def _check_backoff(self) -> None:
        until = self._backoff().get(self.repo, 0.0)
        if until > time.time():
            raise ObservationBackoff(
                f"host queue observation lock backoff active for {until - time.time():.1f}s"
            )

    def _mark_timeout(self) -> None:
        with self.backoff_lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            values = self._backoff()
            values[self.repo] = time.time() + self.backoff_seconds
            self._write_backoff(values)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _clear_backoff(self) -> None:
        with self.backoff_lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            values = self._backoff()
            if self.repo in values:
                values.pop(self.repo, None)
                self._write_backoff(values)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _owner(self) -> dict[str, object] | None:
        try:
            value = json.loads(self.owner_path.read_text())
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _write_owner(self, pid: int, *, started_epoch: float | None = None) -> None:
        """Publish the process that currently holds the inherited flock."""
        now = time.time() if started_epoch is None else started_epoch
        owner = {
            "pid": pid,
            "process_start": _process_start(pid),
            "lane": self.lane,
            "generation": self.generation,
            "started_epoch": now,
            "started_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
        }
        temp = self.owner_path.with_name(
            self.owner_path.name + f".{os.getpid()}.{time.time_ns()}.tmp"
        )
        temp.write_text(json.dumps(owner, sort_keys=True, separators=(",", ":")))
        os.replace(temp, self.owner_path)
        self._owner_pid = pid
        self._owner_process_start = owner["process_start"]

    def _clear_owner(self) -> None:
        owner = self._owner()
        if owner and owner.get("pid") == self._owner_pid and owner.get("process_start") == self._owner_process_start:
            try:
                self.owner_path.unlink()
            except FileNotFoundError:
                pass

    def _owner_detail(self) -> str:
        owner = self._owner()
        probe_available = _lsof_path() is not None
        holders = _live_lock_holders(self.lock_path)
        owner_pid = None if not owner else owner.get("pid")
        recorded_start = None if not owner else owner.get("process_start")
        try:
            recorded_pid = int(owner_pid) if owner_pid is not None else None
        except (TypeError, ValueError):
            recorded_pid = None
        # lsof sees this contender and all queued contenders because they hold
        # the lock file open while waiting. Keep only a real holder: the
        # recorded acquirer may still be valid, while every other FIFO ticket
        # and this process itself are contenders.
        waiter_pids = _fifo_pids(self.lock_path)
        candidates = [pid for pid in holders
                      if pid != os.getpid()
                      and (pid == recorded_pid or pid not in waiter_pids)]
        if recorded_pid in candidates and _pid_is_zombie(recorded_pid):
            candidates.remove(recorded_pid)
        if not owner and not candidates:
            return "owner=unknown age=unknown" + (" holders=unavailable" if not probe_available else "")
        live_owner = False
        pid_reused = False
        try:
            if owner_pid is not None:
                pid = int(owner_pid)
                live_owner = pid in candidates if probe_available else os.kill(pid, 0) is None
                if live_owner and recorded_start and _process_start(pid) != recorded_start:
                    live_owner = False
                    pid_reused = True
        except (TypeError, ValueError, OSError):
            live_owner = False
        holder_pid = int(owner_pid) if live_owner and owner_pid is not None else (candidates[-1] if candidates else None)
        if holder_pid is None:
            return "owner=unknown age=unknown" + (" holders=unavailable" if not probe_available else "")
        if owner is None or not live_owner:
            # An exec'd child inherited the descriptor; retain lane/generation
            # metadata while naming the process that actually holds the flock.
            owner = {
                "pid": holder_pid,
                "lane": owner.get("lane", "unknown") if owner and not pid_reused else "unknown",
                "generation": owner.get("generation", "unknown") if owner and not pid_reused else "unknown",
                "started_at": owner.get("started_at", "unknown") if owner and not pid_reused else "unknown",
                "started_epoch": owner.get("started_epoch") if owner and not pid_reused else None,
            }
        started = owner.get("started_at")
        try:
            age_seconds = max(0.0, time.time() - float(owner["started_epoch"]))
            age_text = f"{age_seconds:.1f}s"
        except (KeyError, TypeError, ValueError):
            age_seconds = None
            age_text = "unknown"
        detail = (
            f"owner_pid={owner.get('pid', 'unknown')} "
            f"lane={owner.get('lane', 'unknown')} "
            f"generation={owner.get('generation', 'unknown')} "
            f"started_at={started or 'unknown'} age={age_text}"
        )
        if age_seconds is not None and age_seconds >= self.max_age:
            escalation = {
                **owner,
                "age_seconds": age_seconds,
                "escalated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "reason": "observation lock owner exceeded bounded age",
            }
            temp = self.escalation_path.with_name(
                self.escalation_path.name + f".{os.getpid()}.{time.time_ns()}.tmp"
            )
            try:
                temp.write_text(json.dumps(escalation, sort_keys=True, separators=(",", ":")))
                os.replace(temp, self.escalation_path)
            except OSError:
                temp.unlink(missing_ok=True)
            detail += f" escalation=owner_age_exceeded:{self.max_age:.1f}s"
        return detail

    def _next_ticket(self, queue: Path) -> Path:
        queue.mkdir(parents=True, exist_ok=True)
        counter = queue / "counter"
        with counter.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            raw = handle.read().strip()
            ticket = int(raw or "0") + 1
            handle.seek(0)
            handle.truncate()
            handle.write(str(ticket))
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        marker = queue / f"{ticket:020d}.{os.getpid()}"
        marker.write_text(json.dumps({"pid": os.getpid(), "start": _process_start(os.getpid())}))
        return marker

    @staticmethod
    def _remove_stale(queue: Path) -> None:
        for marker in queue.glob("[0-9]*.*"):
            try:
                owner = json.loads(marker.read_text())
                pid = int(owner["pid"])
                if not owner.get("start") or owner["start"] != _process_start(pid):
                    raise ProcessLookupError(pid)
                os.kill(pid, 0)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError,
                    FileNotFoundError, ProcessLookupError, PermissionError):
                try:
                    marker.unlink()
                except FileNotFoundError:
                    pass

    @contextlib.contextmanager
    def hold(self) -> Iterator[object]:
        self._check_backoff()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        queue = self.lock_path.with_name(self.lock_path.name + ".fifo")
        marker = self._next_ticket(queue)
        self.ticket_path = marker
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                self._remove_stale(queue)
                lower = sorted(p for p in queue.glob("[0-9]*.*") if p.name < marker.name)
                if not lower:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "host queue observation lock timed out; "
                        + self._owner_detail()
                    )
                time.sleep(0.05)
            with self.lock_path.open("a+", encoding="utf-8") as handle:
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "host queue observation lock timed out; "
                                + self._owner_detail()
                            )
                        time.sleep(0.05)
                self.handle = handle
                self._holding = True
                _ACTIVE_LOCKS.add(self)
                self._write_owner(os.getpid())
                try:
                    yield handle
                except BaseException as error:
                    if _timed_out(error):
                        self._mark_timeout()
                    raise
                else:
                    self._clear_backoff()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    self._holding = False
                    _ACTIVE_LOCKS.discard(self)
                    self._clear_owner()
        finally:
            if marker.exists():
                marker.unlink()
            self.ticket_path = None
