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
# This command emits an integrity-bound observation, not an authenticated
# attestation.  A caller cannot select a stronger authority by changing a
# field in the JSON receipt.
AUTH_METHODS = frozenset({"untrusted_local"})
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
DEFAULT_VALID_FOR_SECONDS = 120
MAX_VALID_FOR_SECONDS = 3600
MAX_OBSERVATION_FUTURE_SECONDS = 60
REQUIRED = frozenset({
    "schema", "sample_id", "observed_at", "valid_until", "host_id", "host_boot_id",
    "generation", "repo", "target", "profile_sha256", "configured_slots",
    "eligible_slots", "free_slots", "occupied_slots", "in_flight_slots", "unknown_slots",
    "pool_state", "source", "auth_method", "verification", "evidence", "evidence_sha256", "census_sha256",
})
OPTIONAL = frozenset({"reason"})


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Mapping[str, Any], *, omit_sample_id: bool = False) -> bytes:
    payload = {key: value[key] for key in sorted(value) if not (omit_sample_id and key == "sample_id")}
    # Keep this byte-for-byte compatible with serde_json::to_vec: UTF-8 is
    # emitted directly, keys are sorted recursively, and exponent padding is
    # omitted (serde_json writes ``1e-6`` rather than Python's ``1e-06``).
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    text = re.sub(r"(?P<number>-?(?:\d+(?:\.\d*)?|\.\d+))e(?P<sign>[+-])0+(?P<exp>\d+)(?P<end>(?=[,}\]]|$))",
                  r"\g<number>e\g<sign>\g<exp>", text)
    return (text + "\n").encode("utf-8")


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def profile_digest(path: pathlib.Path) -> str:
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"profile is unavailable: {path}")
    return digest_bytes(path.read_bytes())


def canonical_profile_path() -> pathlib.Path:
    """The same installed profile path that gate_supply measures."""
    import host_profile  # noqa: PLC0415
    return host_profile.fleet_profile_path()


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
                 valid_for_seconds: int = DEFAULT_VALID_FOR_SECONDS,
                 auth_method: str = "untrusted_local") -> dict[str, Any]:
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
    if not 0 < valid_for_seconds <= MAX_VALID_FOR_SECONDS:
        raise ValueError(
            f"valid_for_seconds must be between 1 and {MAX_VALID_FOR_SECONDS}"
        )
    if report.get("schema") != GATE_SCHEMA:
        raise ValueError("capacity report has an unsupported schema")
    if report.get("repo") != repo or report.get("class") != target:
        raise ValueError("capacity report identity does not match repo/target")
    if str(report.get("host") or "").strip() != host_id:
        raise ValueError("capacity report host does not match host_id")
    measured_profile = report.get("profile_sha256")
    if not isinstance(measured_profile, str) or not HEX64.fullmatch(measured_profile):
        raise ValueError("capacity report profile digest is unavailable")
    if measured_profile != profile_sha256:
        raise ValueError("profile digest does not match capacity report")

    lanes = report.get("lanes") if isinstance(report.get("lanes"), list) else []
    configured = len(lanes)
    verdict = report.get("verdict")
    pool = report.get("pool")
    inventory_present = "inventory" in report
    inventory = report.get("inventory")
    inventory_unavailable = inventory == "unavailable"
    inventory_recognized = not inventory_present or inventory in {"tart", "unavailable"}
    if verdict == "ok" and pool == "on" and inventory_recognized and not inventory_unavailable:
        free = _int_or_none(report.get("free"))
        inflight = _int_or_none(report.get("in_flight"))
        occupied = _int_or_none(report.get("occupied"))
        blocked = [row for row in lanes if isinstance(row, Mapping) and row.get("state") == "blocked"
                   and not str(row.get("detail") or "").startswith("job-running")]
        if (free is None or inflight is None or occupied is None
                or free + inflight + occupied > configured or blocked):
            verification = "unknown"
            reason = ("capacity report is incomplete or inconsistent" if not blocked
                      else "capacity report contains blocked lanes")
            free = inflight = eligible = occupied = None
            unknown = configured if configured else 1
        else:
            verification = "verified"
            reason = None
            eligible = free + inflight + occupied
            unknown = configured - eligible
    else:
        verification = "unknown"
        reason = str(report.get("reason") or (
            "capacity inventory is unavailable" if inventory_unavailable
            else "capacity inventory is unrecognized" if inventory_present
            else f"capacity report verdict={verdict!r} pool={pool!r}"
        ))
        free = inflight = eligible = occupied = None
        unknown = configured

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
        "evidence": evidence,
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
    evidence = value.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("evidence must be an object")
    if evidence.get("schema") != GATE_SCHEMA:
        raise ValueError("evidence has an unsupported schema")
    if evidence.get("host") != value["host_id"] or evidence.get("repo") != value["repo"] \
            or evidence.get("class") != value["target"] \
            or evidence.get("profile_sha256") != value["profile_sha256"]:
        raise ValueError("evidence identity or profile does not match sample")
    lanes = evidence.get("lanes")
    if not isinstance(lanes, list):
        raise ValueError("evidence lanes must be an array")
    if value["configured_slots"] != len(lanes):
        raise ValueError("configured_slots must match evidence lane count")
    if not isinstance(evidence.get("verdict"), str) or not isinstance(evidence.get("pool"), str):
        raise ValueError("evidence verdict and pool are required")
    if value["pool_state"] != evidence["pool"]:
        raise ValueError("pool_state must match evidence pool")
    if value["verification"] == "verified":
        if evidence["verdict"] != "ok" or evidence["pool"] != "on":
            raise ValueError("verified sample requires ok/on evidence")
        if evidence.get("inventory") == "unavailable":
            raise ValueError("verified sample requires available capacity inventory")
        if "inventory" in evidence and evidence["inventory"] != "tart":
            raise ValueError("verified sample requires a recognized capacity inventory")
        for key, expected in (("free", value["free_slots"]),
                              ("occupied", value["occupied_slots"]),
                              ("in_flight", value["in_flight_slots"])):
            if evidence.get(key) != expected:
                raise ValueError(f"evidence {key} does not match sample")
    if value["evidence_sha256"] != digest_bytes(canonical_bytes(evidence)):
        raise ValueError("evidence_sha256 does not match evidence")
    census = {key: evidence.get(key) for key in
              ("schema", "host", "repo", "class", "verdict", "pool", "lanes")}
    if value["census_sha256"] != digest_bytes(canonical_bytes(census)):
        raise ValueError("census_sha256 does not match evidence")
    configured = value["configured_slots"]
    eligible = value["eligible_slots"]
    free = value["free_slots"]
    occupied = value["occupied_slots"]
    inflight = value["in_flight_slots"]
    unknown = value["unknown_slots"]
    if value["verification"] == "verified":
        if any(item is None for item in (eligible, free, occupied, inflight, unknown)):
            raise ValueError("verified sample cannot have unknown capacity")
        if free + occupied + inflight != eligible or eligible + unknown != configured:
            raise ValueError("capacity counts do not partition configured slots")
    elif value["verification"] != "unknown":
        raise ValueError("verification must be verified or unknown")
    else:
        if any(item is not None for item in (eligible, free, occupied, inflight)):
            raise ValueError("unknown sample must not advertise capacity counts")
        if unknown is None or unknown != configured:
            raise ValueError("unknown sample must mark all configured slots unknown")
    if value["auth_method"] not in AUTH_METHODS:
        raise ValueError("unsupported auth_method")
    if not isinstance(value["pool_state"], str) or value["pool_state"] not in {"on", "draining", "off", "unknown"}:
        raise ValueError("invalid pool_state")
    parsed_times: dict[str, dt.datetime] = {}
    for field in ("observed_at", "valid_until"):
        try:
            parsed = dt.datetime.fromisoformat(str(value[field]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be RFC3339") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field} must include timezone")
        parsed_times[field] = parsed
    observed = parsed_times["observed_at"]
    valid_until = parsed_times["valid_until"]
    if observed > utc_now() + dt.timedelta(seconds=MAX_OBSERVATION_FUTURE_SECONDS):
        raise ValueError("observed_at is materially in the future")
    if valid_until <= observed:
        raise ValueError("valid_until must be after observed_at")
    if valid_until - observed > dt.timedelta(seconds=MAX_VALID_FOR_SECONDS):
        raise ValueError(f"validity interval exceeds {MAX_VALID_FOR_SECONDS} seconds")
    return dict(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--profile", type=pathlib.Path, default=None,
                        help="profile to hash (defaults to the installed fleet profile)")
    parser.add_argument("--host-id", default=os.uname().nodename.split(".", 1)[0])
    parser.add_argument("--host-boot-id", default=None)
    parser.add_argument("--generation", default=None)
    parser.add_argument("--valid-for-seconds", type=int, default=DEFAULT_VALID_FOR_SECONDS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        profile = args.profile or canonical_profile_path()
        profile_sha = profile_digest(profile)
        host_boot = args.host_boot_id or host_boot_id() or "unknown"
        generation = args.generation or generation_id() or "unknown"
        report = gate_supply.host_report(args.repo, args.target)
    except Exception as exc:  # noqa: BLE001 - emit an importable unknown receipt
        # Keep an observation failure useful to the metrics importer.  The
        # sentinel identities are explicit unknowns, never evidence of a
        # healthy host or zero capacity.
        try:
            profile = args.profile or canonical_profile_path()
            profile_sha = profile_digest(profile)
        except Exception:
            profile_sha = digest_bytes(b"tartci unknown profile")
        host_boot = args.host_boot_id or host_boot_id() or "unknown"
        generation = args.generation or generation_id() or "unknown"
        report = {
            "schema": GATE_SCHEMA, "host": args.host_id, "repo": args.repo,
            "class": args.target, "verdict": "unknown", "pool": "unknown",
            "reason": str(exc), "lanes": [], "profile_sha256": profile_sha,
        }
    try:
        if report.get("profile_sha256") != profile_sha:
            # A report that observed the profile changing is deliberately
            # unknown, even if the rest of the report says ok. Replace the
            # unusable measured digest with an explicit sentinel so the
            # unknown observation remains importable without pretending either
            # profile version was authoritative.
            profile_sha = digest_bytes(b"tartci unknown profile")
            report = dict(report)
            report["verdict"] = "unknown"
            report["reason"] = "profile changed during capacity observation"
            report["profile_sha256"] = profile_sha
        sample = build_sample(report=report, repo=args.repo, target=args.target,
                              host_id=args.host_id, host_boot=host_boot,
                              generation=generation, profile_sha256=profile_sha,
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
