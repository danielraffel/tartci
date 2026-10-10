#!/usr/bin/env python3
"""Read-only validation for Pulp golden receipts and freshness policy."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
REQUIRED = {
    "schema",
    "manifest_sha256",
    "source_commit",
    "provider_digests",
    "image_digest",
    "baked_at",
    "observed_versions",
}


def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != REQUIRED or value.get("schema") != 1:
        raise ValueError("receipt schema must be 1 with the complete receipt fields")
    for key in ("manifest_sha256", "image_digest"):
        if not HEX64.fullmatch(str(value[key])):
            raise ValueError(f"receipt {key} must be lowercase SHA-256")
    if not HEX40.fullmatch(str(value["source_commit"])):
        raise ValueError("receipt source_commit must be lowercase Git SHA")
    if not isinstance(value["provider_digests"], dict) or not value["provider_digests"]:
        raise ValueError("receipt provider_digests must be non-empty")
    if not isinstance(value["observed_versions"], dict) or not value["observed_versions"]:
        raise ValueError("receipt observed_versions must be non-empty")
    try:
        dt.datetime.fromisoformat(str(value["baked_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("receipt baked_at must be ISO-8601 UTC") from exc
    return value


def age_days(receipt: dict, now: dt.datetime) -> float:
    baked = dt.datetime.fromisoformat(receipt["baked_at"].replace("Z", "+00:00"))
    if baked.tzinfo is None:
        raise ValueError("receipt baked_at must include timezone")
    return (
        now.astimezone(dt.timezone.utc) - baked.astimezone(dt.timezone.utc)
    ).total_seconds() / 86400


def inspect(
    path: Path,
    *,
    now: dt.datetime,
    max_age_days: float,
    expected_manifest: str | None = None,
    promotion: bool = False,
) -> tuple[dict, int]:
    value = load(path)
    age = age_days(value, now)
    problems = []
    if age < 0:
        problems.append("baked_at is in the future")
    if age > max_age_days:
        problems.append(f"receipt is {age:.2f} days old (limit {max_age_days:g})")
    if expected_manifest and value["manifest_sha256"] != expected_manifest:
        problems.append("manifest digest does not match authority")
    result = {
        "status": "stale" if problems else "fresh",
        "age_days": round(age, 3),
        "max_age_days": max_age_days,
        "problems": problems,
        "receipt": value,
    }
    # A normal doctor is advisory. Promotion/release preflight refuses problems.
    return result, 2 if problems and promotion else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--max-age-days", type=float, default=14)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--promotion", action="store_true")
    parser.add_argument("--now", help="UTC ISO time for deterministic tests")
    args = parser.parse_args(argv)
    try:
        now = (
            dt.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
            if args.now
            else dt.datetime.now(dt.timezone.utc)
        )
        result, code = inspect(
            args.receipt,
            now=now,
            max_age_days=args.max_age_days,
            expected_manifest=args.manifest_sha256,
            promotion=args.promotion,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"pulp-golden-receipt: ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
