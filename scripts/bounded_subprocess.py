#!/usr/bin/env python3
"""Bounded subprocess execution for read-only TartCI observations."""

from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Sequence


TIMEOUT_EXIT_CODE = 124
DESCENDANT_LEAK_EXIT_CODE = 125
TERMINATE_GRACE_SECONDS = 0.25


@dataclasses.dataclass(frozen=True)
class ObservationError(RuntimeError):
    """A typed failure from one read-only observation command."""

    operation: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.operation}:{self.kind}:{self.detail}"

    @property
    def problem_code(self) -> str:
        return f"{self.operation}_{self.kind}"


def _read_capture(stream: object) -> str:
    stream.seek(0)  # type: ignore[attr-defined]
    return stream.read().decode("utf-8", errors="replace")  # type: ignore[attr-defined]


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def _terminate_group(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the private process group without waiting on inherited pipes."""

    pgid = proc.pid
    _signal_group(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=TERMINATE_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            # The capture files still guarantee bounded return even if the OS
            # delays reaping the already-signalled group leader.
            pass

    # Reaping the leader before checking the group avoids mistaking its zombie
    # for a live descendant. Normal Tart/gh child trees exit on SIGTERM; retain
    # a bounded escalation for a descendant that does not.
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline and _group_exists(pgid):
        time.sleep(0.01)
    if _group_exists(pgid):
        _signal_group(pgid, signal.SIGKILL)


def _terminate_remaining_group(pgid: int) -> None:
    """Terminate descendants left behind after their group leader exited."""

    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline and _group_exists(pgid):
        time.sleep(0.01)
    if _group_exists(pgid):
        _signal_group(pgid, signal.SIGKILL)
        deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline and _group_exists(pgid):
            time.sleep(0.01)


def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    """Run a trusted read-only controller command with bounded capture.

    Temporary files deliberately replace stdout/stderr pipes. A grandchild that
    inherits those descriptors therefore cannot keep ``communicate()`` waiting
    for EOF after the direct child times out. The private process group contains
    normal Tart/gh helper descendants; this is not a sandbox for hostile code
    that deliberately creates a new session to escape its controller.
    """

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    command = [str(arg) for arg in argv]
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
        except OSError as exc:
            raise ObservationError(operation, "spawn_failed", str(exc)) from exc

        timed_out = False
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_group(proc)
            returncode = TIMEOUT_EXIT_CODE

        leaked_descendant = False
        if not timed_out and _group_exists(proc.pid):
            # A successful observation command must not daemonize. This also
            # closes the classic capture wedge where the leader exits but a
            # grandchild retains its inherited stdout/stderr descriptors.
            leaked_descendant = True
            _terminate_remaining_group(proc.pid)
            returncode = DESCENDANT_LEAK_EXIT_CODE

        stdout = _read_capture(stdout_file)
        stderr = _read_capture(stderr_file)
        if timed_out and not stderr.strip():
            stderr = f"timed out after {timeout:g}s"
        if leaked_descendant:
            detail = "unexpected descendant survived observation leader"
            stderr = f"{stderr.rstrip()}\n{detail}".lstrip()
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def require_success(
    result: subprocess.CompletedProcess[str],
    *,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    """Turn a bounded command result into a stable typed observation error."""

    if result.returncode == 0:
        return result
    detail = result.stderr.strip() or f"exit {result.returncode}"
    if result.returncode == TIMEOUT_EXIT_CODE:
        kind = "timeout"
    elif result.returncode == DESCENDANT_LEAK_EXIT_CODE:
        kind = "descendant_leak"
    else:
        kind = "failed"
    raise ObservationError(operation, kind, detail)
