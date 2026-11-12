#!/usr/bin/env python3
"""Turn point-in-time ``pool supply --json`` reports into interval records.

This is an offline export. It never reads host state or calls ``tartci``; the
input is a JSON array or JSONL fixture containing reports already collected by
an observer. Each output row keeps the complete source report so heartbeat,
inventory, and lease-fit evidence remains inspectable. A final sample is
capped at the observation limit and followed by an explicit ``unknown``
tail. Gaps longer than the observation cap become explicit ``unknown``
intervals; no capacity is inferred through a gap.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
import pathlib
from collections import defaultdict
from typing import Any, Iterable

SOURCE_SCHEMA = "tartci.gate-supply/v1"
SCHEMA = "tartci.capacity-interval/v1"
VERDICTS = frozenset(("ok", "unknown"))
DEFAULT_MAX_GAP_SECONDS = 120.0


def _timestamp(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("timestamp must be an epoch number or timezone-aware RFC3339 timestamp")
    if isinstance(value, (int, float)):
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ValueError("timestamp epoch must be finite and non-negative")
        return result
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timestamp must include a timezone")
            result = parsed.timestamp()
            if not math.isfinite(result) or result < 0:
                raise ValueError("timestamp must be finite and non-negative")
            return result
        except ValueError:
            raise ValueError("timestamp must be an epoch number or timezone-aware RFC3339 timestamp")
    raise ValueError("timestamp must be an epoch number or timezone-aware RFC3339 timestamp")


def _format_timestamp(value: float) -> str:
    parsed = dt.datetime.fromtimestamp(value, dt.timezone.utc)
    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _report_key(report: dict[str, Any]) -> tuple[str, str, str]:
    return (report["host"], report["repo"], report["class"])


def _sample_timestamp(report: dict[str, Any]) -> tuple[float, str]:
    # fetched_at is the collector's clock and is preferred whenever present;
    # peer and local reports then share one reference clock.
    for field in ("fetched_at", "observed_at", "now"):
        if field in report:
            return _timestamp(report[field]), field
    raise ValueError("supply sample is missing 'now', 'observed_at', or 'fetched_at'")


def _canonical_json(value: Any) -> str:
    """Canonical comparison retaining JSON's bool/int/float distinctions."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validate_report(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValueError("each supply sample must be a JSON object")
    if report.get("schema") != SOURCE_SCHEMA:
        raise ValueError(f"expected {SOURCE_SCHEMA} report")
    for field in ("host", "repo", "class", "verdict"):
        if field not in report:
            raise ValueError(f"supply sample is missing {field!r}")
        if not isinstance(report[field], str) or not report[field].strip():
            raise ValueError(f"supply sample field {field!r} must be a non-empty string")
    if report["verdict"] not in VERDICTS:
        raise ValueError(f"unsupported supply verdict {report['verdict']!r}")
    _sample_timestamp(report)
    return copy.deepcopy(report)


def _error_interval(report: Any, error: str, observed_at: Any | None) -> dict[str, Any]:
    """Keep a producer failure even when its error shape lacks normal fields."""
    source = copy.deepcopy(report)
    source_dict = report if isinstance(report, dict) else {}
    timestamp = None
    timestamp_source = "unavailable"
    has_clock_field = any(field in source_dict for field in ("fetched_at", "observed_at", "now"))
    if "fetched_at" in source_dict:
        try:
            timestamp = _timestamp(source_dict["fetched_at"])
            timestamp_source = "fetched_at"
        except ValueError:
            pass
    elif "observed_at" in source_dict:
        try:
            timestamp = _timestamp(source_dict["observed_at"])
            timestamp_source = "observed_at"
        except ValueError:
            pass
    elif "now" in source_dict:
        try:
            timestamp = _timestamp(source_dict["now"])
            timestamp_source = "now"
        except ValueError:
            pass
    if timestamp is None and observed_at is not None and not has_clock_field:
        timestamp = _timestamp(observed_at)
        timestamp_source = "export_observed_at"
    def identity(field: str) -> str:
        value = source_dict.get(field)
        return value.strip() if isinstance(value, str) and value.strip() else "unknown"
    return {
        "schema": SCHEMA,
        "host": identity("host"),
        "repo": identity("repo"),
        "class": identity("class"),
        "interval_start": _format_timestamp(timestamp) if timestamp is not None else None,
        "interval_end": None,
        "duration_s": None,
        "timestamp_source": timestamp_source,
        "verdict": "unknown",
        "reason": "invalid_supply_sample",
        "error": error,
        "supply": source,
    }


def _is_producer_error_shape(report: Any) -> bool:
    """Recognize only the two read-only gate-supply failure envelopes."""
    if not isinstance(report, dict) or report.get("verdict") != "unknown":
        return False
    reason = str(report.get("reason") or "")
    if reason.startswith("report failed:") and report.get("schema") == SOURCE_SCHEMA:
        return True
    return reason.startswith("ssh ") or reason == "report schema mismatch"


def _duration(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if value.is_integer() else value


def export_intervals(
    reports: Iterable[Any],
    until: Any | None = None,
    max_gap_s: float = DEFAULT_MAX_GAP_SECONDS,
    observed_at: Any | None = None,
) -> list[dict[str, Any]]:
    """Export adjacent observations as interval envelopes.

    Samples are grouped by host/repository/class and sorted by their collector
    ``fetched_at`` (then ``observed_at`` or producer ``now``) timestamp. The
    source report is retained verbatim under ``supply``. ``until`` closes the
    capped unknown tail. Gaps longer than ``max_gap_s`` are split into a capped
    observed interval and an explicit unknown gap interval.
    """
    max_gap = float(max_gap_s)
    if not math.isfinite(max_gap) or max_gap <= 0:
        raise ValueError("max_gap_s must be finite and positive")
    if observed_at is not None:
        _timestamp(observed_at)
    samples: list[dict[str, Any]] = []
    diagnostics: list[tuple[int, dict[str, Any]]] = []
    for index, report in enumerate(reports):
        try:
            samples.append(_validate_report(report))
        except ValueError as exc:
            if not _is_producer_error_shape(report):
                raise
            diagnostics.append((index, _error_interval(report, str(exc), observed_at)))
    end = _timestamp(until) if until is not None else None
    grouped: dict[tuple[str, str, str], list[tuple[float, int, str, dict[str, Any]]]] = defaultdict(list)
    for index, report in enumerate(samples):
        timestamp, field = _sample_timestamp(report)
        grouped[_report_key(report)].append((timestamp, index, field, report))

    output: list[tuple[float, int, dict[str, Any]]] = []
    all_sources = {field for rows in grouped.values() for _time, _index, field, _report in rows}
    all_sources.update(row.get("timestamp_source") for _index, row in diagnostics)
    all_sources.discard("unavailable")
    if len(all_sources) > 1:
        raise ValueError(f"mixed timestamp sources in export: {sorted(all_sources)}")
    for key, rows in grouped.items():
        rows.sort(key=lambda item: (item[0], item[1]))
        timestamp_source = rows[0][2]
        deduped: list[tuple[float, int, str, dict[str, Any]]] = []
        for row in rows:
            if deduped and row[0] == deduped[-1][0]:
                if _canonical_json(row[3]) == _canonical_json(deduped[-1][3]):
                    continue
                raise ValueError(f"conflicting supply evidence at {row[0]} for {key}")
            deduped.append(row)
        rows = deduped
        for position, (start, index, _field, report) in enumerate(rows):
            has_next_sample = position + 1 < len(rows)
            next_start = rows[position + 1][0] if has_next_sample else end
            if next_start is not None and next_start <= start:
                raise ValueError(f"interval end is not after start for {key}")
            observed_end = next_start
            final_open = observed_end is None
            if observed_end is None and end is None:
                observed_end = start + max_gap
            elif observed_end is not None and observed_end - start > max_gap:
                observed_end = start + max_gap
            output.append((start, index, {
                "schema": SCHEMA,
                "host": report["host"],
                "repo": report["repo"],
                "class": report["class"],
                "interval_start": _format_timestamp(start),
                "interval_end": _format_timestamp(observed_end) if observed_end is not None else None,
                "duration_s": _duration(None if observed_end is None else observed_end - start),
                "timestamp_source": timestamp_source,
                "verdict": report["verdict"],
                "supply": report,
            }))
            if final_open or (next_start is not None and observed_end < next_start):
                output.append((observed_end, index, {
                    "schema": SCHEMA,
                    "host": report["host"],
                    "repo": report["repo"],
                    "class": report["class"],
                    "interval_start": _format_timestamp(observed_end),
                    "interval_end": _format_timestamp(next_start) if next_start is not None else None,
                    "duration_s": _duration(next_start - observed_end) if next_start is not None else None,
                    "timestamp_source": timestamp_source,
                    "verdict": "unknown",
                    "reason": "observation_gap",
                    "supply": None,
                    "gap": {
                        "max_gap_s": max_gap_s,
                        "previous_observation_at": _format_timestamp(start),
                        "next_observation_at": _format_timestamp(next_start) if has_next_sample else None,
                    },
                }))
    output.sort(key=lambda item: (item[0], item[1]))
    # Producer diagnostics are retained after interval rows. An observer can
    # supply observed_at when a legacy error envelope has no clock field.
    return [interval for _start, _index, interval in output] + [row for _index, row in diagnostics]


def _read_samples(path: pathlib.Path) -> list[Any]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    raise ValueError("input JSON must be an object, array, or JSONL")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--input", type=pathlib.Path, required=True, help="supply JSON or JSONL fixture")
    parser.add_argument("--until", help="close final intervals at RFC3339 time")
    parser.add_argument("--max-gap-seconds", type=float, default=DEFAULT_MAX_GAP_SECONDS)
    parser.add_argument("--observed-at", help="timestamp to attach to malformed producer reports")
    parser.add_argument("--json", action="store_true", help="emit one JSON array instead of JSONL")
    args = parser.parse_args(argv)
    rows = export_intervals(_read_samples(args.input), args.until, args.max_gap_seconds, args.observed_at)
    if args.json:
        print(json.dumps(rows, sort_keys=True))
    else:
        for row in rows:
            print(json.dumps(row, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
