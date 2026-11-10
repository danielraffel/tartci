#!/usr/bin/env python3
"""What kind of queued job a lane is about to ask a macOS VM slot for.

The queue scanners already read every run and job they count: each run carries
its `event` (merge_group, pull_request, push, ...) and each job its
`created_at`. Slot priority (scripts/slot_priority.py) needs exactly those two
facts, so a scanner given `--detail-file` writes them beside its count. Nothing
here makes a GitHub call; it only keeps what the scan already fetched.

A detail file records, per run event, how many matching jobs the scan saw and
the oldest one's `created_at`. A scan that stops at its first witness
(scripts/assignment_scan.py without --exhaustive-count) records only that
witness, so its age is a lower bound on the class's oldest queued job; it says
so with `witness_only`.

Compatible with the hosts' Python 3.9.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA = 1
MERGE_GROUP = "merge_group"
PULL_REQUEST = "pull_request"
OTHER = "other"
# Only these spellings are classes of their own; every other event (push,
# release, schedule, workflow_dispatch, ...) is `other`.
_PR_EVENTS = {"pull_request", "pull_request_target"}


def event_class(event: Any) -> str:
    text = str(event or "").strip().lower()
    if text == MERGE_GROUP:
        return MERGE_GROUP
    if text in _PR_EVENTS:
        return PULL_REQUEST
    return OTHER


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DemandDetail:
    """Thread-safe collector of (event class, created_at) for matched jobs."""

    def __init__(self, witness_only: bool = False) -> None:
        self.witness_only = witness_only
        self._lock = threading.Lock()
        self._classes: Dict[str, Dict[str, Any]] = {}

    def add(self, run: Dict[str, Any], job: Dict[str, Any]) -> None:
        cls = event_class(run.get("event"))
        stamp = parse_ts(job.get("created_at")) or parse_ts(run.get("created_at"))
        with self._lock:
            row = self._classes.setdefault(cls, {"count": 0, "oldest_created_at": None})
            row["count"] += 1
            if stamp is not None:
                current = parse_ts(row["oldest_created_at"])
                if current is None or stamp < current:
                    row["oldest_created_at"] = iso(stamp)

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            classes = {key: dict(value) for key, value in self._classes.items()}
        return {
            "schema": SCHEMA,
            "written_at": iso(_utcnow()),
            "witness_only": self.witness_only,
            "classes": classes,
        }

    def write(self, path: str) -> None:
        """Atomic replace; a write failure never fails the scan that called it."""
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.as_dict(), handle, sort_keys=True)
                handle.write("\n")
            os.replace(tmp, str(target))
        except OSError:
            pass


def read_detail(path: str, max_age_secs: int) -> Optional[Dict[str, Any]]:
    """A detail file no older than max_age_secs, or None (unknown)."""
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    written = parse_ts(data.get("written_at"))
    if written is None:
        return None
    age = (_utcnow() - written).total_seconds()
    if age < -60 or age > max_age_secs:
        return None
    classes = data.get("classes")
    if not isinstance(classes, dict):
        return None
    return data
