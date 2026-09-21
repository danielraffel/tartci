#!/usr/bin/env python3
"""Read queued-job demand from the local Shipyard daemon's push feed.

The daemon receives GitHub `workflow_job` webhooks over a Tailscale tunnel and
fans them out on a local Unix socket. A `workflow_job` payload carries the job's
requested runner `labels` directly, which is the datum the REST assignment scan
spends one `/actions/runs/<id>/jobs` call per queued run to recover.

WHAT THIS MODULE REFUSES TO DO
------------------------------
An event stream cannot prove the absence of work. Silence from a healthy feed
and silence from a severed one are the same bytes. So this module never returns
a verdict meaning "the queue is empty": the best negative it can express is
`STALE`, whose only correct handling is to fall back to the REST scan.

`Verdict.FRESH` is therefore the *only* verdict carrying demand, and
`demand_is_known_absent()` is hard-wired to `False` for every verdict. A caller
cannot accidentally read a severed feed as an idle queue.

Liveness is established from the daemon's own status frame, never inferred from
event silence:
  * the repo must appear in `registered_repos` -- a repo that is configured but
    unregistered means webhook registration failed, so the feed is blind to it;
  * `last_event_at` must fall inside the freshness window.

Label matching mirrors GitHub's assignment rule and the REST scanner's contract:
a job matches when the required label is present AND its label set is a subset
of the runner's labels. Malformed labels are a refusal, never a silent skip.
"""
from __future__ import annotations

import enum
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SOCKET = (
    Path.home()
    / "Library"
    / "Application Support"
    / "shipyard"
    / "daemon"
    / "daemon.sock"
)

# Daemon IPC contract (Shipyard src/daemon_ipc.rs).
IPC_PROTOCOL_VERSION = 3
IPC_ERROR_SUBSCRIBER_CAPACITY = "subscriber_capacity_exceeded"

# A repo with no webhook traffic this long is not proven live.
DEFAULT_FRESHNESS_WINDOW_S = 300.0
DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_COLLECT_TIMEOUT_S = 2.0


class Verdict(enum.Enum):
    """Typed feed outcome. Only FRESH may be acted on as demand."""

    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass
class FeedResult:
    """Outcome of one feed read."""

    verdict: Verdict
    detail: str
    events: list = field(default_factory=list)

    @property
    def matched(self) -> int:
        """Number of matching queued jobs observed. Meaningful only when FRESH."""
        return len(self.events)

    def demand_is_known_absent(self) -> bool:
        """Never true.

        There is no feed verdict that proves an empty queue. This exists so a
        caller reaching for "is there nothing to do?" gets a hard `False`
        instead of reading a severed feed as an idle one.
        """
        return False

    def should_fall_back_to_scan(self) -> bool:
        """True whenever the feed did not positively observe demand."""
        return self.verdict is not Verdict.FRESH


class FeedError(RuntimeError):
    """The feed could not be read. Always degrades to a scan, never to empty."""


def _read_frame(reader, want_type: str):
    """Return the first frame of `want_type`, skipping the `hello` handshake."""
    for _ in range(8):
        line = reader.readline()
        if not line:
            return None
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue
        if frame.get("error") == IPC_ERROR_SUBSCRIBER_CAPACITY:
            raise FeedError("daemon subscriber capacity reached")
        if frame.get("error"):
            raise FeedError(f"daemon refused: {frame.get('error')}")
        if frame.get("type") == want_type:
            return frame
    return None


def _connect(socket_path: Path, timeout: float):
    if not socket_path.exists():
        raise FeedError(f"daemon socket absent at {socket_path}")
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(socket_path))
    except OSError as exc:
        conn.close()
        raise FeedError(f"daemon socket not accepting connections: {exc}") from exc
    return conn


def read_status(socket_path: Path, timeout: float = DEFAULT_CONNECT_TIMEOUT_S) -> dict:
    """Fetch one daemon status frame."""
    conn = _connect(socket_path, timeout)
    try:
        conn.sendall(b'{"type":"status"}\n')
        frame = _read_frame(conn.makefile("rb"), "status")
    except (OSError, socket.timeout) as exc:
        raise FeedError(f"daemon status read failed: {exc}") from exc
    finally:
        conn.close()
    if frame is None:
        raise FeedError("daemon returned no status frame")
    return frame


def assess_liveness(status: dict, repo: str, freshness_window_s: float, now: float):
    """Return `None` when the feed is proven live for `repo`, else a FeedResult.

    Liveness is positive proof, never the absence of events.
    """
    registered = {str(r).lower() for r in status.get("registered_repos") or []}
    configured = {str(r).lower() for r in status.get("configured_repos") or []}
    target = repo.lower()

    if target not in registered:
        if target in configured:
            return FeedResult(
                Verdict.UNAVAILABLE,
                f"{repo} is configured but its webhook is not registered; "
                f"the feed is blind to it (daemon last_error: {status.get('last_error')!r})",
            )
        return FeedResult(
            Verdict.UNAVAILABLE, f"{repo} is not watched by this daemon"
        )

    last_event_at = status.get("last_event_at")
    if not isinstance(last_event_at, (int, float)):
        return FeedResult(
            Verdict.STALE, "daemon reports no event timestamp; feed not proven live"
        )
    age = now - float(last_event_at)
    if age > freshness_window_s:
        return FeedResult(
            Verdict.STALE,
            f"last daemon event was {age:.0f}s ago, beyond the "
            f"{freshness_window_s:.0f}s freshness window",
        )
    return None


def job_matches(payload: dict, repo: str, require_label: str, runner_labels) -> bool:
    """Mirror GitHub's assignment rule, refusing malformed labels."""
    if str(payload.get("repo", "")).lower() != repo.lower():
        return False
    if str(payload.get("status", "")).lower() != "queued":
        return False
    raw = payload.get("labels")
    if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
        raise FeedError(
            f"queued job {payload.get('job_id')} has invalid labels: {raw!r}"
        )
    labels = {x.strip().lower() for x in raw}
    if require_label.strip().lower() not in labels:
        return False
    return labels.issubset({str(x).strip().lower() for x in runner_labels})


def read_feed(
    repo: str,
    require_label: str,
    runner_labels,
    socket_path: Path = DEFAULT_SOCKET,
    freshness_window_s: float = DEFAULT_FRESHNESS_WINDOW_S,
    collect_timeout_s: float = DEFAULT_COLLECT_TIMEOUT_S,
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
    now: float | None = None,
) -> FeedResult:
    """Read queued demand for `repo` from the local daemon.

    Returns FRESH only when the feed is proven live AND matching queued jobs
    were observed. Every other outcome is STALE or UNAVAILABLE, both of which
    mean "fall back to the scan" -- never "no work".
    """
    if require_label.strip().lower() not in {
        str(x).strip().lower() for x in runner_labels
    }:
        raise FeedError(
            f"required label {require_label!r} absent from runner labels; "
            "this lane could never match a job"
        )
    clock = time.time if now is None else (lambda: now)

    try:
        status = read_status(socket_path, connect_timeout_s)
    except FeedError as exc:
        return FeedResult(Verdict.UNAVAILABLE, str(exc))

    refusal = assess_liveness(status, repo, freshness_window_s, clock())
    if refusal is not None:
        return refusal

    try:
        conn = _connect(socket_path, connect_timeout_s)
    except FeedError as exc:
        return FeedResult(Verdict.UNAVAILABLE, str(exc))

    matched = []
    try:
        conn.sendall(b'{"type":"subscribe"}\n')
        conn.settimeout(collect_timeout_s)
        reader = conn.makefile("rb")
        deadline = time.monotonic() + collect_timeout_s
        while time.monotonic() < deadline:
            try:
                line = reader.readline()
            except (socket.timeout, TimeoutError):
                break
            if not line:
                break
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            if frame.get("error"):
                return FeedResult(
                    Verdict.UNAVAILABLE, f"daemon refused: {frame['error']}"
                )
            if frame.get("kind") != "workflow_job":
                continue
            payload = frame.get("payload")
            if not isinstance(payload, dict):
                continue
            if job_matches(payload, repo, require_label, runner_labels):
                matched.append(payload)
    except FeedError as exc:
        return FeedResult(Verdict.STALE, f"refusing a malformed feed: {exc}")
    except OSError as exc:
        return FeedResult(Verdict.UNAVAILABLE, f"feed read failed: {exc}")
    finally:
        conn.close()

    if matched:
        return FeedResult(
            Verdict.FRESH, f"{len(matched)} matching queued job(s) on the feed", matched
        )
    # Deliberately NOT a "queue empty" verdict. Per-event age is unknowable from
    # a replayed ring, and a live feed with no matching events is not proof of
    # absence. Until the daemon publishes a per-repo delivery-liveness signal,
    # the honest answer is "I could not see demand", which means: go scan.
    return FeedResult(
        Verdict.STALE,
        "feed is live but showed no matching queued job; "
        "absence of events is not evidence of an empty queue",
    )


# --- observed-age ledger -------------------------------------------------
#
# A lane may require a queued job to have waited `min_queued_age_seconds`
# before it is worth booting a VM for. The scan reads that age from the job's
# own `created_at`. The feed cannot: Shipyard's normalized `workflow_job`
# payload carries no timestamp, and the daemon replays its ring without
# stamping one, so a replayed frame is undatable by construction.
#
# What a subscriber CAN date is its own first sighting. A webhook cannot arrive
# before the job was queued, so `first_seen >= queued_at`, and therefore
# `now - first_seen <= now - queued_at`. Measuring age from `first_seen` is an
# UNDER-estimate of the true wait, so it can only ever withhold a job that is
# in fact old enough -- never release one that is too young. Erring late means
# the scan answers instead, which is the behaviour this whole path falls back
# to anyway.

DEFAULT_LEDGER_RETENTION_S = 21_600.0


def _load_ledger(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _store_ledger(path: Path, ledger: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(ledger), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # A ledger that cannot be persisted re-dates every job on the next
        # poll, which only delays eligibility. Never fatal.
        pass


def eligible_witnesses(
    result: FeedResult,
    ledger_path: Path,
    min_observed_age_s: float,
    now: float | None = None,
    retention_s: float = DEFAULT_LEDGER_RETENTION_S,
) -> tuple[int, str]:
    """Count FRESH witnesses this subscriber has observed queued long enough.

    Returns `(count, reason)`. A zero count is never evidence of an empty
    queue -- it means this feed did not license a decision, so the caller must
    keep whatever the scan told it.
    """
    if result.verdict is not Verdict.FRESH:
        return 0, result.detail
    stamp = time.time() if now is None else now
    ledger = _load_ledger(ledger_path)
    eligible = 0
    oldest = None
    for payload in result.events:
        job_id = payload.get("job_id")
        if not isinstance(job_id, int) or isinstance(job_id, bool):
            # Distinct jobs sharing one ledger key would let the first one to
            # age in vouch for all the rest, releasing work that never waited.
            # Refuse the read rather than key on a value that cannot identify.
            return 0, (
                f"refusing a malformed feed: queued job has no integer id "
                f"({job_id!r}); not an empty queue"
            )
        key = str(job_id)
        entry = ledger.get(key)
        first_seen = entry.get("first_seen") if isinstance(entry, dict) else None
        if not isinstance(first_seen, (int, float)):
            first_seen = stamp
        ledger[key] = {"first_seen": float(first_seen), "last_seen": stamp}
        observed = stamp - float(first_seen)
        if observed >= min_observed_age_s:
            eligible += 1
        elif oldest is None or observed > oldest:
            oldest = observed
    # Pruning only reaches jobs that have LEFT the feed: `last_seen` is
    # refreshed above for every job still being witnessed, so an entry cannot
    # be aged out from under a job that is still queued.
    for key in [
        k
        for k, v in ledger.items()
        if not isinstance(v, dict)
        or not isinstance(v.get("last_seen"), (int, float))
        or stamp - float(v["last_seen"]) > retention_s
    ]:
        del ledger[key]
    _store_ledger(ledger_path, ledger)
    if eligible:
        return eligible, (
            f"{eligible} witness(es) observed queued for at least "
            f"{min_observed_age_s:.0f}s"
        )
    return 0, (
        f"{result.matched} witness(es) on the feed but none observed for "
        f"{min_observed_age_s:.0f}s (oldest observed "
        f"{0.0 if oldest is None else oldest:.0f}s); not an empty queue"
    )


# Exit codes. `NO_LICENCE` deliberately has no "the queue is empty" reading:
# it says this feed did not license a decision, so the caller keeps its own.
EXIT_DEMAND = 0
EXIT_USAGE = 2
EXIT_NO_LICENCE = 3


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Report queued demand observed on the local Shipyard daemon feed. "
            "Exit 0 means demand was positively observed. Any other exit means "
            "this feed licensed no decision -- never that the queue is empty."
        )
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--require-label", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--min-observed-age-seconds", type=float, default=0.0)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--socket", default=str(DEFAULT_SOCKET))
    parser.add_argument(
        "--freshness-window-seconds", type=float, default=DEFAULT_FRESHNESS_WINDOW_S
    )
    parser.add_argument(
        "--collect-timeout-seconds", type=float, default=DEFAULT_COLLECT_TIMEOUT_S
    )
    parser.add_argument(
        "--connect-timeout-seconds", type=float, default=DEFAULT_CONNECT_TIMEOUT_S
    )
    args = parser.parse_args(argv)
    if args.min_observed_age_seconds < 0:
        parser.error("--min-observed-age-seconds must be non-negative")
    labels = [item for item in args.labels.split(",") if item.strip()]

    try:
        result = read_feed(
            args.repo,
            args.require_label,
            labels,
            socket_path=Path(args.socket),
            freshness_window_s=args.freshness_window_seconds,
            collect_timeout_s=args.collect_timeout_seconds,
            connect_timeout_s=args.connect_timeout_seconds,
        )
    except FeedError as error:
        print(f"feed licensed no decision: {error}", file=sys.stderr)
        return EXIT_USAGE

    count, reason = eligible_witnesses(
        result, Path(args.ledger), args.min_observed_age_seconds
    )
    if count:
        # One witness settles the only question the caller asks, and the feed
        # cannot claim a magnitude: a replay ring is not a queue census.
        print(1)
        print(f"feed observed demand: {reason}", file=sys.stderr)
        return EXIT_DEMAND
    print(f"feed licensed no decision: {reason}", file=sys.stderr)
    return EXIT_NO_LICENCE


if __name__ == "__main__":
    raise SystemExit(main())
