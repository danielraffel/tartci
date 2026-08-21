#!/usr/bin/env python3
"""Supervise the independent Shipyard cleanup and steward process groups."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


HERE = Path(__file__).resolve().parent
CHILDREN: list[subprocess.Popen[bytes]] = []
HANDLED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
REGISTERING_CHILD = False
DEFERRED_SIGNAL: int | None = None


class ServiceInterrupted(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def process_group_alive(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process(child: subprocess.Popen[bytes]) -> None:
    process_group = child.pid
    child.poll()
    if not process_group_alive(process_group):
        return
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2
    while process_group_alive(process_group) and time.monotonic() < deadline:
        child.poll()
        time.sleep(0.05)
    if process_group_alive(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def terminate_children() -> None:
    for child in CHILDREN:
        terminate_process(child)


def timeout_value(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    if not raw.isdigit() or not 1 <= int(raw) <= 280:
        raise ValueError(f"{name} must be 1..280")
    return int(raw)


def publish_unhealthy_health(path: Path, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "unhealthy",
                "reason": reason,
                "host": os.uname().nodename,
                "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def publish_service_failure(paths: tuple[Path, ...], reason: str) -> None:
    for path in paths:
        try:
            publish_unhealthy_health(path, reason)
        except OSError as error:
            print(
                f"queue service: could not publish {path}: {error}",
                file=sys.stderr,
            )
    print(f"queue service: {reason}", file=sys.stderr)


def stop(signum: int, _frame: object) -> None:
    global DEFERRED_SIGNAL
    if REGISTERING_CHILD:
        if DEFERRED_SIGNAL is None:
            DEFERRED_SIGNAL = signum
        return
    for handled_signal in HANDLED_SIGNALS:
        signal.signal(handled_signal, signal.SIG_IGN)
    # Raising first unwinds any active Popen.wait() lock. Cleanup in the outer
    # finally can then wait safely instead of re-entering subprocess internals
    # from inside the signal handler.
    raise ServiceInterrupted(signum)


def main() -> int:
    global DEFERRED_SIGNAL, REGISTERING_CHILD
    legacy_health = Path(
        os.environ.get(
            "SHIPYARD_QUEUE_HEALTH_FILE",
            str(Path.home() / "Library/Logs/shipyard-queue-tick.health.json"),
        )
    )
    steward_health = Path(
        os.environ.get(
            "SHIPYARD_STEWARD_HEALTH_FILE",
            str(Path.home() / "Library/Logs/shipyard-steward-tick.health.json"),
        )
    )
    try:
        legacy_timeout = timeout_value(
            "SHIPYARD_SERVICE_LEGACY_TIMEOUT_SECS", 240
        )
        steward_timeout = timeout_value(
            "SHIPYARD_SERVICE_STEWARD_TIMEOUT_SECS", 180
        )
    except ValueError as error:
        reason = f"service configuration invalid: {error}"
        publish_service_failure((legacy_health, steward_health), reason)
        return 2

    legacy_env = os.environ.copy()
    # Steward is the sole queue-advancement writer. The historical child keeps
    # only its terminal ship-state cleanup responsibility.
    legacy_env["SHIPYARD_TICK_REAP_ONLY"] = "1"
    lanes = (
        (
            "legacy ship-state",
            HERE / "shipyard_queue_tick.sh",
            legacy_env,
            legacy_timeout,
            legacy_health,
        ),
        (
            "cross-repository steward",
            HERE / "shipyard_steward_tick.sh",
            os.environ.copy(),
            steward_timeout,
            steward_health,
        ),
    )
    started_at = time.monotonic()
    lane_children: list[subprocess.Popen[bytes] | None] = []
    for label, script, env, _timeout, health in lanes:
        REGISTERING_CHILD = True
        try:
            try:
                child = subprocess.Popen(
                    ["/bin/bash", str(script)],
                    env=env,
                    start_new_session=True,
                )
            except OSError as error:
                child = None
                publish_service_failure(
                    (health,), f"{label} lane could not start: {error}"
                )
            else:
                CHILDREN.append(child)
            lane_children.append(child)
        finally:
            REGISTERING_CHILD = False
        if DEFERRED_SIGNAL is not None:
            signum = DEFERRED_SIGNAL
            DEFERRED_SIGNAL = None
            stop(signum, None)
    statuses: list[int | None] = [
        1 if child is None else None for child in lane_children
    ]
    while any(status is None for status in statuses):
        now = time.monotonic()
        for index, (label, _script, _env, timeout, health) in enumerate(lanes):
            if statuses[index] is not None:
                continue
            child = lane_children[index]
            if child is None:
                continue
            status = child.poll()
            if status is not None:
                statuses[index] = status
            elif now - started_at >= timeout:
                terminate_process(child)
                statuses[index] = 124
                publish_service_failure(
                    (health,),
                    f"{label} lane timed out after {timeout}s",
                )
        if any(status is None for status in statuses):
            time.sleep(0.05)
    for (label, _script, _env, _timeout, health), child, status in zip(
        lanes, lane_children, statuses
    ):
        if child is not None and status not in (0, 124):
            publish_service_failure(
                (health,), f"{label} lane failed (exit={status})"
            )
    return 0 if all(status == 0 for status in statuses) else 1


if __name__ == "__main__":
    for handled_signal in HANDLED_SIGNALS:
        signal.signal(handled_signal, stop)
    try:
        raise SystemExit(main())
    except ServiceInterrupted as interruption:
        raise SystemExit(128 + interruption.signum) from None
    finally:
        terminate_children()
