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

A lane that declares short jobs (`[[lane]] short_jobs`, rendered as
TARTCI_SHORT_JOBS) also gets, per class, a `short` row: how many matched jobs
are on that list and the oldest one's created_at, job id, run id and name.
Slot priority's short-job-first rule reads it. Old readers ignore the row and a
file without it means no short job is queued, so the schema is unchanged.

Compatible with the hosts' Python 3.9.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

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


def parse_short_jobs(raw: Optional[str]) -> List[Tuple[str, str]]:
    """`workflow|job` lines (TARTCI_SHORT_JOBS) as (workflow, job) pairs.

    An empty workflow matches the job name in any workflow. Lines without a job
    name are dropped: a short job is only ever named, never guessed.
    """
    pairs: List[Tuple[str, str]] = []
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        workflow, sep, job = line.rpartition("|")
        if not sep:
            workflow, job = "", line
        workflow, job = workflow.strip(), job.strip()
        if job:
            pairs.append((workflow, job))
    return pairs


def is_short_job(run: Dict[str, Any], job: Dict[str, Any],
                 short_jobs: Iterable[Tuple[str, str]]) -> bool:
    """The job's name, and its workflow when one is given, match a declared pair."""
    name = str(job.get("name") or "")
    workflow = str(job.get("workflow_name") or run.get("name") or "")
    for want_workflow, want_job in short_jobs:
        if name == want_job and (not want_workflow or workflow == want_workflow):
            return True
    return False


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

    def __init__(self, witness_only: bool = False,
                 short_jobs: Optional[Iterable[Tuple[str, str]]] = None) -> None:
        self.witness_only = witness_only
        self.short_jobs = list(short_jobs or [])
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
            if self.short_jobs and is_short_job(run, job, self.short_jobs):
                short = row.setdefault("short", {"count": 0, "oldest_created_at": None})
                short["count"] += 1
                current = parse_ts(short["oldest_created_at"])
                if stamp is not None and (current is None or stamp < current):
                    short.update(oldest_created_at=iso(stamp), job_id=job.get("id"),
                                 run_id=run.get("id"), name=str(job.get("name") or ""))

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            classes = {}
            for key, value in self._classes.items():
                classes[key] = dict(value)
                if isinstance(value.get("short"), dict):
                    classes[key]["short"] = dict(value["short"])
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
