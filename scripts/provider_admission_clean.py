#!/usr/bin/env python3
"""Consume Shipyard's typed runner-admission verdict for TartCI providers.

Shipyard owns stale-run observation and cancellation policy.  This adapter only
invokes that authority and validates the small admit/defer/error contract before
a provider is allowed to register a JIT runner.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import datetime
import hashlib
import pathlib
import subprocess
import sys
import time
from typing import Any, Callable, Sequence


COMMAND = "runner:admission-clean"
VERDICT_EXIT = {"admit": 0, "defer": 3, "error": 1}
REPO_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+"
)
REASON_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
RFC3339_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
REASONS = {
    "clean",
    "cleaned",
    "stale_compatible_runs",
    "mutation_authority_required",
    "cancellation_pending",
    "observation_in_progress",
    "stewardship_in_progress",
    "invalid_labels",
    "observation_failed",
    "authority_failed",
    "revalidation_failed",
    "mutation_failed",
}
VERDICT_REASONS = {
    "admit": {"clean", "cleaned"},
    "defer": {
        "stale_compatible_runs",
        "mutation_authority_required",
        "cancellation_pending",
        "observation_in_progress",
        "stewardship_in_progress",
    },
    "error": {
        "invalid_labels",
        "observation_failed",
        "authority_failed",
        "revalidation_failed",
        "mutation_failed",
    },
}
U64_MAX = (1 << 64) - 1
# An `error` verdict is not one thing.  Some error reasons mean Shipyard *looked*
# and found dirt it could not clear; those stay fail-closed forever.  The rest
# mean Shipyard could not look at all, which is a statement about Shipyard's own
# reachability and says nothing about the queue.  Only the second class may ever
# degrade, and only after it has repeated.
INCONCLUSIVE_ERROR_REASONS = {
    "observation_failed",
    "authority_failed",
    "revalidation_failed",
}
# `mutation_failed` is a positive observation of a superseded run that Shipyard
# failed to cancel; `invalid_labels` is local misconfiguration.  Never degrade.
CONCLUSIVE_ERROR_REASONS = {"invalid_labels", "mutation_failed"}
# A `defer` for one of these reasons says nothing about the queue: another
# caller holds Shipyard's exact-key observation (or stewardship) lock and is
# computing the same (repo, base, labels) verdict right now.  Shipyard answers
# with a try-lock, so the refusal is instant.  Re-asking after a short pause
# gets the real verdict; giving up discards a VM that is already booted.  Every
# other `defer` is a statement about the queue and is returned at once.
CONTENTION_DEFER_REASONS = {"observation_in_progress", "stewardship_in_progress"}


class ConfigurationError(ValueError):
    """Local or Shipyard command configuration is invalid."""


def parse_labels(value: str) -> list[str]:
    labels = [label.strip() for label in value.split(",")]
    if not labels or any(not label for label in labels):
        raise ConfigurationError(
            "labels must be a nonempty comma-separated list"
        )
    if len(set(labels)) != len(labels):
        raise ConfigurationError("labels must not contain duplicates")
    return labels


def normalized_labels(labels: Sequence[str]) -> list[str]:
    return sorted({label.lower() for label in labels})


def validate_verdict(
    value: Any,
    *,
    repo: str,
    base: str,
    labels: Sequence[str],
    process_exit: int,
    unknown_reason: list[str] | None = None,
) -> dict[str, Any]:
    if unknown_reason is None:
        unknown_reason = []
    if not isinstance(value, dict):
        raise ValueError("admission verdict must be a JSON object")
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("command") != COMMAND
    ):
        raise ValueError("unexpected admission verdict envelope")
    verdict = value.get("verdict")
    reason = value.get("reason")
    if verdict not in VERDICT_EXIT:
        raise ValueError("unexpected admission verdict")
    if not isinstance(reason, str) or not REASON_PATTERN.fullmatch(reason):
        raise ValueError("admission reason does not match verdict")
    if reason not in REASONS or reason not in VERDICT_REASONS[verdict]:
        # Shipyard is versioned separately, so an unrecognized pairing under an
        # `error` verdict is skew, not proof of a dirty queue: the breaker
        # classifies it as inconclusive.  `admit` and `defer` stay strict --
        # widening `admit` would let a future reason silently become an
        # admission, and `defer` already backs off without stopping the fleet.
        if verdict != "error":
            raise ValueError("admission reason does not match verdict")
        unknown_reason.append(reason)
    if process_exit != VERDICT_EXIT[verdict]:
        raise ValueError("admission verdict does not match process exit")
    if value.get("repo") != repo or value.get("base") != base:
        raise ValueError("admission verdict target does not match request")
    returned_labels = value.get("labels")
    if (
        not isinstance(returned_labels, list)
        or any(not isinstance(label, str) for label in returned_labels)
        or returned_labels != normalized_labels(labels)
    ):
        raise ValueError(
            "admission verdict labels are not the normalized request labels"
        )
    observed_at = value.get("observed_at")
    if (
        not isinstance(observed_at, str)
        or not RFC3339_PATTERN.fullmatch(observed_at)
    ):
        raise ValueError("admission verdict requires RFC3339 observed_at")
    blockers = value.get("blocker_run_ids")
    if (
        not isinstance(blockers, list)
        or any(
            type(run_id) is not int
            or run_id <= 0
            or run_id > U64_MAX
            for run_id in blockers
        )
    ):
        raise ValueError("blocker_run_ids must be positive u64 integers")
    if verdict == "admit" and blockers:
        raise ValueError("admit verdict must not contain blockers")
    return value


def _state_dir() -> pathlib.Path:
    raw = os.environ.get("TARTCI_ADMISSION_CLEAN_STATE_DIR")
    base = pathlib.Path(raw) if raw else pathlib.Path.home() / ".tartci/state"
    return base / "admission-clean"


def _bounded_env_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, str(default))
    if not re.fullmatch(r"[0-9]+", raw) or not low <= int(raw) <= high:
        raise ConfigurationError(f"{name} must be {low}..{high}")
    return int(raw)


def _counter_path(repo: str, base: str, labels: Sequence[str]) -> pathlib.Path:
    # Key per lane target.  Two lanes sharing a state dir must not pool each
    # other's failures into a degrade that neither one earned.
    key = hashlib.sha256(
        "\n".join([repo, base, ",".join(labels)]).encode()
    ).hexdigest()[:32]
    return _state_dir() / f"inconclusive.{key}.json"


def _read_counter(path: pathlib.Path) -> int:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    count = value.get("consecutive") if isinstance(value, dict) else None
    return count if type(count) is int and count >= 0 else 0


def _write_counter(path: pathlib.Path, count: int, reason: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "consecutive": count,
                "reason": reason,
                "updated_at": _now_rfc3339(),
            }
        )
        # Atomic replace so a concurrent lane never reads a torn counter.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(payload)
        os.replace(tmp, path)
    except OSError:
        # The breaker is a safety valve; never fail admission over its bookkeeping.
        pass


def _clear_counter(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _now_rfc3339() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def bounded_timeout() -> int:
    raw = os.environ.get("TARTCI_ADMISSION_CLEAN_TIMEOUT_SECS", "300")
    if not re.fullmatch(r"[0-9]+", raw) or not 1 <= int(raw) <= 1800:
        raise ConfigurationError(
            "TARTCI_ADMISSION_CLEAN_TIMEOUT_SECS must be 1..1800"
        )
    return int(raw)


def contention_wait() -> tuple[int, int]:
    """Bound on re-asking a contention defer: (total seconds, poll seconds).

    0 total disables the wait and restores the single-shot behaviour.
    """
    total = _bounded_env_int(
        "TARTCI_ADMISSION_CLEAN_CONTENTION_WAIT_SECS", 90, 0, 600
    )
    poll = _bounded_env_int(
        "TARTCI_ADMISSION_CLEAN_CONTENTION_POLL_SECS", 5, 1, 60
    )
    return total, poll


def validate_configuration(args: argparse.Namespace) -> list[str]:
    if not REPO_PATTERN.fullmatch(args.repo):
        raise ConfigurationError("repo must be a canonical owner/name slug")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", args.base):
        raise ConfigurationError("base must be a branch name")
    labels = parse_labels(args.labels)
    bounded_timeout()
    contention_wait()
    return labels


def run(
    args: argparse.Namespace,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    labels = validate_configuration(args)
    if args.validate_only:
        return 0
    wait_total, wait_poll = contention_wait()
    started = clock()
    waits = 0
    while True:
        value, unknown_reason = _ask_shipyard(args, labels)
        if (
            value["verdict"] != "defer"
            or value["reason"] not in CONTENTION_DEFER_REASONS
        ):
            break
        # Only the wait is bounded here; the verdict that ends it is always a
        # fresh Shipyard answer.  Running out of budget returns the contention
        # defer itself, exactly as a single-shot call would have.
        remaining = wait_total - (clock() - started)
        if remaining <= 0:
            break
        sleep(min(wait_poll, remaining))
        waits += 1
    if waits:
        value = dict(value)
        value["tartci_contention_waits"] = waits
        value["tartci_contention_wait_secs"] = round(clock() - started, 1)
    return _apply_breaker(args, labels, value, unknown_reason)


def _ask_shipyard(
    args: argparse.Namespace, labels: Sequence[str]
) -> tuple[dict[str, Any], bool]:
    completed = subprocess.run(
        [
            args.shipyard,
            "runner",
            "admission-clean",
            "--repo",
            args.repo,
            "--base",
            args.base,
            "--labels",
            ",".join(labels),
            "--apply",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=bounded_timeout(),
    )
    if completed.returncode == 2:
        raise ConfigurationError(
            "Shipyard rejected admission-clean configuration"
        )
    if completed.returncode not in set(VERDICT_EXIT.values()):
        raise RuntimeError(
            f"Shipyard admission command failed with exit "
            f"{completed.returncode}"
        )
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("Shipyard admission output was not JSON") from error
    unknown_reason: list[str] = []
    try:
        value = validate_verdict(
            raw,
            repo=args.repo,
            base=args.base,
            labels=labels,
            process_exit=completed.returncode,
            unknown_reason=unknown_reason,
        )
    except ValueError:
        # Persist what Shipyard actually said before failing.  The previous
        # behaviour discarded the envelope, so a version skew could only be
        # diagnosed by reading Shipyard's source.
        _persist_rejected(args, completed.stdout)
        raise
    return value, bool(unknown_reason)


def _persist_rejected(args: argparse.Namespace, stdout: str) -> None:
    try:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "rejected-envelope.json").write_text(stdout[:65536])
    except OSError:
        pass


def _apply_breaker(
    args: argparse.Namespace,
    labels: Sequence[str],
    value: dict[str, Any],
    unknown_reason: bool,
) -> int:
    verdict = value["verdict"]
    reason = value["reason"]
    path = _counter_path(args.repo, args.base, labels)
    if verdict != "error":
        # A real admit or defer proves Shipyard can observe again.
        _clear_counter(path)
        print(json.dumps(value, separators=(",", ":"), sort_keys=True))
        return VERDICT_EXIT[verdict]
    inconclusive = unknown_reason or reason in INCONCLUSIVE_ERROR_REASONS
    if not inconclusive:
        # Shipyard looked and found dirt it could not clear.  Stay closed.
        print(json.dumps(value, separators=(",", ":"), sort_keys=True))
        return VERDICT_EXIT[verdict]
    degrade_after = _bounded_env_int(
        "TARTCI_ADMISSION_CLEAN_DEGRADE_AFTER", 3, 1, 100
    )
    degrade_max = _bounded_env_int(
        "TARTCI_ADMISSION_CLEAN_DEGRADE_MAX", 20, 1, 10000
    )
    count = _read_counter(path) + 1
    _write_counter(path, count, reason)
    if count < degrade_after or count > degrade_max:
        # Below the threshold the gate keeps its full strength; above the cap it
        # closes again so a permanent outage cannot leave the gate open forever.
        print(json.dumps(value, separators=(",", ":"), sort_keys=True))
        return VERDICT_EXIT[verdict]
    degraded = dict(value)
    degraded["tartci_degraded"] = True
    degraded["tartci_consecutive_inconclusive"] = count
    print(json.dumps(degraded, separators=(",", ":"), sort_keys=True))
    print(
        f"admission-clean DEGRADED: {count} consecutive inconclusive verdicts "
        f"({reason}); admitting without a clean proof. Gate closes again after "
        f"{degrade_max}.",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shipyard", default="shipyard")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--labels", required=True)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except ConfigurationError as error:
        print(f"admission-clean configuration error: {error}", file=sys.stderr)
        return 2
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print(f"admission-clean error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
