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
import subprocess
import sys
from typing import Any, Sequence


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
) -> dict[str, Any]:
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
    if (
        not isinstance(reason, str)
        or not REASON_PATTERN.fullmatch(reason)
        or reason not in REASONS
        or reason not in VERDICT_REASONS[verdict]
    ):
        raise ValueError("admission reason does not match verdict")
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


def bounded_timeout() -> int:
    raw = os.environ.get("TARTCI_ADMISSION_CLEAN_TIMEOUT_SECS", "300")
    if not re.fullmatch(r"[0-9]+", raw) or not 1 <= int(raw) <= 1800:
        raise ConfigurationError(
            "TARTCI_ADMISSION_CLEAN_TIMEOUT_SECS must be 1..1800"
        )
    return int(raw)


def validate_configuration(args: argparse.Namespace) -> list[str]:
    if not REPO_PATTERN.fullmatch(args.repo):
        raise ConfigurationError("repo must be a canonical owner/name slug")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", args.base):
        raise ConfigurationError("base must be a branch name")
    labels = parse_labels(args.labels)
    bounded_timeout()
    return labels


def run(args: argparse.Namespace) -> int:
    labels = validate_configuration(args)
    if args.validate_only:
        return 0
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
    value = validate_verdict(
        raw,
        repo=args.repo,
        base=args.base,
        labels=labels,
        process_exit=completed.returncode,
    )
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))
    return VERDICT_EXIT[value["verdict"]]


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
