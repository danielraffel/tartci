#!/usr/bin/env python3
"""Render an admission verdict envelope as a bounded single-line event detail.

The provider event carried only `rc=<n> unregistered=true`, which says a
verdict was refused but never why.  The typed reason and the underlying error
were written to the envelope on disk and nowhere else, so every diagnosis
started by hunting for a per-VM JSON file that a later teardown may already
have rotated away.  This renders the same two fields into the event itself.

Classification is by the envelope's typed `reason`, never by matching the
error text.  One inconclusive outcome reaches a provider as an HTTP 504, as a
truncated body (`unexpected end of JSON input`), or as a timeout, depending on
where the request died; a matcher keyed on any one of those spellings would
silently misclassify the others.  `reason` is the field Shipyard types for
exactly this purpose.

Reads the envelope on stdin, writes one line on stdout, and never fails on
malformed input: a diagnostic must not be able to break the failure path it
describes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

# Matches provider_admission_clean.REASON_PATTERN: an unrecognised or
# non-conforming value is reported as such rather than echoed into the log.
REASON_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
DEFAULT_MAX_ERROR_CHARS = 120
TRUNCATION_MARKER = "..."
MISSING = "reason=missing"
UNREADABLE = "reason=unreadable"


def collapse(text: str) -> str:
    """One line, no control characters, no runs of whitespace."""
    return " ".join(text.split())


def bounded_error(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    collapsed = collapse(value)
    if not collapsed:
        return ""
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + TRUNCATION_MARKER


def detail(envelope: Any, limit: int) -> str:
    if not isinstance(envelope, dict):
        return UNREADABLE
    reason = envelope.get("reason")
    if not isinstance(reason, str) or not REASON_PATTERN.fullmatch(reason):
        reason = "unknown"
    rendered = f"reason={reason}"
    rechecks = envelope.get("tartci_in_progress_rechecks")
    if type(rechecks) is int and rechecks > 0:
        rendered = f"{rendered} in_progress_rechecks={rechecks}"
    error = bounded_error(envelope.get("error"), limit)
    if error:
        rendered = f"{rendered} error={error}"
    return rendered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-error-chars", type=int, default=DEFAULT_MAX_ERROR_CHARS
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    limit = args.max_error_chars
    if limit < 1:
        limit = DEFAULT_MAX_ERROR_CHARS
    raw = sys.stdin.read()
    if not raw.strip():
        print(MISSING)
        return 0
    try:
        envelope = json.loads(raw)
    except (ValueError, RecursionError):
        print(UNREADABLE)
        return 0
    print(detail(envelope, limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
