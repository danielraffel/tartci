"""Fair host-global observation lock with per-repository timeout backoff."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Iterator


class ObservationBackoff(RuntimeError):
    """This repository recently timed out and must yield the host slot."""


def _process_start(pid: int) -> str | None:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "lstart="], text=True,
            stderr=subprocess.DEVNULL,
        ).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _timed_out(error: BaseException) -> bool:
    text = str(error).lower()
    return any(token in text for token in (
        "scan exceeded its overall deadline",
        "host queue observation lock timed out",
    ))


class FairObservationLock:
    def __init__(self, lock_file: str, timeout: float, repo: str,
                 backoff_file: str | None = None, backoff_seconds: float = 30.0) -> None:
        self.lock_path = Path(lock_file)
        self.timeout = timeout
        self.repo = repo
        self.backoff_path = Path(backoff_file or (str(self.lock_path) + ".backoff.json"))
        self.backoff_seconds = backoff_seconds
        self.backoff_lock_path = self.backoff_path.with_name(self.backoff_path.name + ".lock")
        self.ticket_path: Path | None = None
        self.handle = None

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
                    raise TimeoutError("host queue observation lock timed out")
                time.sleep(0.05)
            with self.lock_path.open("a+", encoding="utf-8") as handle:
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("host queue observation lock timed out")
                        time.sleep(0.05)
                self.handle = handle
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
        finally:
            if marker.exists():
                marker.unlink()
            self.ticket_path = None
