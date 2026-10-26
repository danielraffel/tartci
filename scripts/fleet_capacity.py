#!/usr/bin/env python3
"""Emit a strict, observation-only eligible fleet-capacity sample.

The sample is a receipt for accounting. It never changes pool state, leases,
routing, or admission. Unknown host evidence remains ``verification=unknown``
and never becomes a zero-capacity observation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any, Mapping

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import gate_supply  # noqa: E402

SCHEMA = "tartci.fleet-capacity/v1"
GATE_SCHEMA = "tartci.gate-supply/v1"
SOURCE = "tartci"
AUTH_METHODS = frozenset({"managed_local", "managed_ssh"})
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
REQUIRED = frozenset({
    "schema", "sample_id", "observed_at", "valid_until", "host_id", "host_boot_id",
    "generation", "repo", "target", "profile_sha256", "configured_slots",
    "eligible_slots", "free_slots", "occupied_slots", "in_flight_slots", "unknown_slots",
    "pool_state", "source", "auth_method", "verification", "evidence_sha256", "census_sha256",
})
OPTIONAL = frozenset({"reason"})


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Mapping[str, Any], *, omit_sample_id: bool = False) -> bytes:
    payload = {key: value[key] for key in sorted(value) if not (omit_sample_id and key == "sample_id")}
    return (json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n").encode()


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def profile_digest(path: pathlib.Path) -> str:
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"profile is unavailable: {path}")
    return digest_bytes(path.read_bytes())


def host_boot_id() -> str:
    explicit = os.environ.get("TARTCI_HOST_BOOT_ID", "").strip()
    if explicit:
        return explicit
    try:
        result = subprocess.run(["sysctl", "-n", "kern.bootsessionuuid"],
                                text=True, capture_output=True, check=False,
                                timeout=3, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def generation_id() -> str:
    for key in ("TARTCI_GENERATION", "TARTCI_SUPPORT_GENERATION"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    pointer = pathlib.Path.home() / ".config/tartci/launcher-generation.json"
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    generation = value.get("generation") if isinstance(value, dict) else None
    return str(generation).strip() if generation else ""


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def build_sample(*, report: Mapping[str, Any], repo: str, target: str,
                 host_id: str, host_boot: str, generation: str,
                 profile_sha256: str, observed_at: dt.datetime,
                 valid_for_seconds: int = 120,
                 auth_method: str = "managed_local") -> dict[str, Any]:
    """Build and validate one sample from an already-read gate-supply report."""
    if not REPO.fullmatch(repo):
        raise ValueError("repo must be OWNER/REPO")
    if not TARGET.fullmatch(target):
        raise ValueError("target is invalid")
    if not host_id or not host_boot or not generation:
        raise ValueError("host_id, host_boot_id, and generation are required")
    if not HEX64.fullmatch(profile_sha256):
        raise ValueError("profile_sha256 must be a SHA-256 digest")
    if auth_method not in AUTH_METHODS:
        raise ValueError("unsupported auth_method")
    if valid_for_seconds <= 0:
        raise ValueError("valid_for_seconds must be positive")
    if report.get("schema") != GATE_SCHEMA:
        raise ValueError("capacity report has an unsupported schema")
    if report.get("repo") != repo or report.get("class") != target:
        raise ValueError("capacity report identity does not match repo/target")
    if str(report.get("host") or "").strip() != host_id:
        raise ValueError("capacity report host does not match host_id")

    lanes = report.get("lanes") if isinstance(report.get("lanes"), list) else []
    configured = len(lanes)
    verdict = report.get("verdict")
    pool = report.get("pool")
    if verdict == "ok" and pool == "on":
        free = _int_or_none(report.get("free"))
        inflight = _int_or_none(report.get("in_flight"))
        if free is None or inflight is None or free + inflight > configured:
            verification = "unknown"
            reason = "capacity report is incomplete or inconsistent"
            free = inflight = eligible = occupied = unknown = None
        else:
            verification = "verified"
            reason = None
            eligible = free + inflight
            occupied = inflight
            unknown = configured - eligible
    else:
        verification = "unknown"
        reason = str(report.get("reason") or f"capacity report verdict={verdict!r} pool={pool!r}")
        free = inflight = eligible = occupied = None
        unknown = configured if configured else 1

    evidence = dict(report)
    evidence_sha = digest_bytes(canonical_bytes(evidence))
    census = {key: report.get(key) for key in
              ("schema", "host", "repo", "class", "verdict", "pool", "lanes")}
    census_sha = digest_bytes(canonical_bytes(census))
    sample: dict[str, Any] = {
        "schema": SCHEMA,
        "sample_id": "",
        "observed_at": iso(observed_at),
        "valid_until": iso(observed_at + dt.timedelta(seconds=valid_for_seconds)),
        "host_id": host_id,
        "host_boot_id": host_boot,
        "generation": generation,
        "repo": repo,
        "target": target,
        "profile_sha256": profile_sha256,
        "configured_slots": configured,
        "eligible_slots": eligible,
        "free_slots": free,
        "occupied_slots": occupied,
        "in_flight_slots": inflight,
        "unknown_slots": unknown,
        "pool_state": str(pool or "unknown"),
        "source": SOURCE,
        "auth_method": auth_method,
        "verification": verification,
        "evidence_sha256": evidence_sha,
        "census_sha256": census_sha,
    }
    if reason:
        sample["reason"] = reason
    sample["sample_id"] = digest_bytes(canonical_bytes(sample, omit_sample_id=True))
    validate_sample(sample)
    return sample


def validate_sample(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("sample must be an object")
    keys = set(value)
    if not REQUIRED.issubset(keys):
        raise ValueError(f"sample is missing fields: {sorted(REQUIRED - keys)}")
    if keys - REQUIRED - OPTIONAL:
        raise ValueError(f"sample has unknown fields: {sorted(keys - REQUIRED - OPTIONAL)}")
    if value.get("schema") != SCHEMA or value.get("source") != SOURCE:
        raise ValueError("unsupported sample schema or source")
    if not isinstance(value.get("sample_id"), str) or not HEX64.fullmatch(value["sample_id"]):
        raise ValueError("sample_id must be a SHA-256 digest")
    expected = digest_bytes(canonical_bytes(value, omit_sample_id=True))
    if value["sample_id"] != expected:
        raise ValueError("sample_id does not match canonical sample")
    if not isinstance(value.get("repo"), str) or not REPO.fullmatch(value["repo"]):
        raise ValueError("repo must be OWNER/REPO")
    if not isinstance(value.get("target"), str) or not TARGET.fullmatch(value["target"]):
        raise ValueError("target is invalid")
    for key in ("host_id", "host_boot_id", "generation"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"{key} is required")
    for key in ("profile_sha256", "evidence_sha256", "census_sha256"):
        if not isinstance(value.get(key), str) or not HEX64.fullmatch(value[key]):
            raise ValueError(f"{key} must be a SHA-256 digest")
    for key in ("configured_slots", "eligible_slots", "free_slots", "occupied_slots", "in_flight_slots", "unknown_slots"):
        item = value.get(key)
        if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0):
            raise ValueError(f"{key} must be a nonnegative integer or null")
    configured = value["configured_slots"]
    eligible, free, occupied = value["eligible_slots"], value["free_slots"], value["occupied_slots"]
    if value["verification"] == "verified":
        if any(item is None for item in (eligible, free, occupied, value["in_flight_slots"])):
            raise ValueError("verified sample cannot have unknown capacity")
        if not (free + occupied <= eligible <= configured):
            raise ValueError("capacity counts violate free+occupied <= eligible <= configured")
    elif value["verification"] != "unknown":
        raise ValueError("verification must be verified or unknown")
    if value["auth_method"] not in AUTH_METHODS:
        raise ValueError("unsupported auth_method")
    if not isinstance(value["pool_state"], str) or value["pool_state"] not in {"on", "draining", "off", "unknown"}:
        raise ValueError("invalid pool_state")
    for field in ("observed_at", "valid_until"):
        try:
            parsed = dt.datetime.fromisoformat(str(value[field]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be RFC3339") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{field} must include timezone")
    observed = dt.datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
    valid_until = dt.datetime.fromisoformat(value["valid_until"].replace("Z", "+00:00"))
    if valid_until <= observed:
        raise ValueError("valid_until must be after observed_at")
    return dict(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--profile", type=pathlib.Path,
                        default=pathlib.Path.home() / ".config/tartci/macos-fleet-profile.toml")
    parser.add_argument("--host-id", default=os.uname().nodename.split(".", 1)[0])
    parser.add_argument("--host-boot-id", default=None)
    parser.add_argument("--generation", default=None)
    parser.add_argument("--valid-for-seconds", type=int, default=120)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = gate_supply.host_report(args.repo, args.target)
        sample = build_sample(report=report, repo=args.repo, target=args.target,
                              host_id=args.host_id,
                              host_boot=args.host_boot_id or host_boot_id(),
                              generation=args.generation or generation_id(),
                              profile_sha256=profile_digest(args.profile),
                              observed_at=utc_now(), valid_for_seconds=args.valid_for_seconds)
    except Exception as exc:  # noqa: BLE001 - observation must fail closed
        payload = {"schema": SCHEMA, "verification": "unknown", "reason": str(exc)}
        print(json.dumps(payload, sort_keys=True))
        return 2
    print(json.dumps(sample, sort_keys=True) if args.json else
          f"{sample['verification']} eligible={sample['eligible_slots']} free={sample['free_slots']} "
          f"occupied={sample['occupied_slots']} sample_id={sample['sample_id']}")
    return 0 if sample["verification"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
