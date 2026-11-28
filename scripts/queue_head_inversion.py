#!/usr/bin/env python3
"""Queue-head inversion: an older merge-group job starves while newer jobs run.

2026-11-28: Vellum run 46104476461's GPU job sat `queued` for over an hour
while the lane counted it as demand and runners with its labels were minted,
yet GitHub handed those runners newer jobs of the same repository. Nothing
noticed. This module is the detector. It only detects: it never cancels or
re-runs anything, because that ejects the merge-queue entry and admission has
one writer (the m3 steward).

The inputs are what a scan already read (scripts/slot_demand.py's detail file):
the oldest counted queued jobs per event class, and the jobs with this scan's
labels that already have a runner. A queued merge_group job older than the
threshold is inverted when at least one job created after it started on a
runner after it was created. A runner that took a newer job was minted or
registered with matching labels, so that is the evidence of "runners were
given to newer work". An old job waiting on a full fleet, with no newer job
started, is not an inversion.

Off by default (TARTCI_QUEUE_INVERSION=1 or --inversion-detect turns it on).
Compatible with the hosts' Python 3.9.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from slot_demand import MERGE_GROUP, iso, parse_ts

EVENT = "queue_head_inversion"
IDS_EVENT = "scan_demand_ids"
DEFAULT_THRESHOLD_SECS = 1800
# Re-emit an unresolved inversion at most this often (the scan runs every poll).
REEMIT_SECS = 900
MAX_TOOK = 5
NEXT_ACTION = (
    "cancel and re-run run {run_id} (an admission action for the m3 steward; "
    "tartci does not do it). Cancelling ejects the merge-queue entry and "
    "regroups the queue, so only the steward acts on this."
)


def detect(detail: Dict[str, Any], now: dt.datetime,
           threshold_secs: int = DEFAULT_THRESHOLD_SECS,
           classes: Any = (MERGE_GROUP,)) -> List[Dict[str, Any]]:
    """Inversions in one scan's detail dict (SlotDemand.as_dict()), oldest first."""
    started = [r for r in detail.get("started") or [] if isinstance(r, dict)]
    out: List[Dict[str, Any]] = []
    all_classes = detail.get("classes") or {}
    for cls in classes:
        for job in (all_classes.get(cls) or {}).get("oldest_jobs") or []:
            created = parse_ts(job.get("created_at"))
            if created is None:
                continue
            age = (now - created).total_seconds()
            if age < threshold_secs:
                continue
            took = []
            for row in started:
                row_created = parse_ts(row.get("created_at"))
                row_started = parse_ts(row.get("started_at"))
                if row_created is None or row_started is None:
                    continue
                if row.get("job_id") == job.get("job_id"):
                    continue
                if row_created > created and row_started > created:
                    took.append(row)
            if not took:
                continue
            took.sort(key=lambda r: r["started_at"])
            out.append({
                "event_class": cls,
                "run_id": job.get("run_id"),
                "job_id": job.get("job_id"),
                "queued_since": iso(created),
                "age_seconds": int(age),
                "threshold_seconds": int(threshold_secs),
                "took_count": len(took),
                "took": [{k: r.get(k) for k in ("run_id", "job_id", "runner_name",
                                                 "created_at", "started_at")}
                         for r in took[:MAX_TOOK]],
                "next_action": NEXT_ACTION.format(run_id=job.get("run_id")),
            })
    out.sort(key=lambda r: r["queued_since"])
    return out


def counted_ids(detail: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Per class, the bounded oldest counted run and job ids of one scan."""
    result: Dict[str, List[Dict[str, Any]]] = {}
    for cls, row in sorted((detail.get("classes") or {}).items()):
        result[cls] = [{"run_id": j.get("run_id"), "job_id": j.get("job_id"),
                        "created_at": j.get("created_at")}
                       for j in (row or {}).get("oldest_jobs") or []]
    return result


def _event_line(kind: str, runner: str, detail: str, fields: Dict[str, Any],
                now: dt.datetime) -> str:
    # ts first and compact, as runner.sh event() writes it: pool_usage finds a
    # line's time by a byte scan of its first 256 bytes.
    return json.dumps({"ts": iso(now), "event": kind, "runner": runner, "vm": "",
                       "detail": detail, "fields": fields}, separators=(",", ":"))


def _load(path: str) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: str, data: Dict[str, Any]) -> None:
    target = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True)
    os.replace(tmp, str(target))


def record_scan(detail: Dict[str, Any], *, event_log: str, state_file: str,
                runner: str, tier: str, now: Optional[dt.datetime] = None,
                detect_inversion: bool = False,
                threshold_secs: int = DEFAULT_THRESHOLD_SECS) -> List[str]:
    """Append this scan's diagnostics to the lane's event log; never raises.

    `scan_demand_ids` is written when the counted ids of a tier change since the
    last scan (a steady queue writes nothing). `queue_head_inversion` is written
    only with detect_inversion, once per job and again every REEMIT_SECS while
    it persists. Returns the event names written.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    written: List[str] = []
    lines: List[str] = []
    try:
        state = _load(state_file)
        ids = counted_ids(detail)
        if state.get("ids") != ids:
            lines.append(_event_line(IDS_EVENT, runner, "tier=%s" % tier,
                                     {"tier": tier, "counted": ids}, now))
            written.append(IDS_EVENT)
            state["ids"] = ids
        if detect_inversion:
            last = state.get("emitted") if isinstance(state.get("emitted"), dict) else {}
            kept: Dict[str, float] = {}
            for inv in detect(detail, now, threshold_secs):
                key = str(inv["job_id"])
                prior = last.get(key)
                kept[key] = prior if isinstance(prior, (int, float)) else 0
                if isinstance(prior, (int, float)) and now.timestamp() - prior < REEMIT_SECS:
                    continue
                kept[key] = now.timestamp()
                inv = dict(inv, tier=tier)
                lines.append(_event_line(
                    EVENT, runner,
                    "run %s job %s queued %dm while %d newer job(s) took runners" % (
                        inv["run_id"], inv["job_id"], inv["age_seconds"] // 60,
                        inv["took_count"]), inv, now))
                written.append(EVENT)
            state["emitted"] = kept
        if lines:
            with open(event_log, "a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        _save(state_file, state)
    except OSError:
        pass
    return written


def add_cli_args(parser: Any) -> None:
    parser.add_argument(
        "--event-log",
        default=None,
        help="append scan_demand_ids (and, with --inversion-detect, "
        "queue_head_inversion) events to this lane event log; no GitHub call",
    )
    parser.add_argument(
        "--inversion-detect",
        action="store_true",
        default=os.environ.get("TARTCI_QUEUE_INVERSION", "0") == "1",
        help="detect a queued merge_group job starved by newer jobs (default off; "
        "TARTCI_QUEUE_INVERSION=1). Detection only: it never cancels or re-runs",
    )
    parser.add_argument(
        "--inversion-threshold-secs",
        type=int,
        default=int(os.environ.get("TARTCI_QUEUE_INVERSION_SECS", str(DEFAULT_THRESHOLD_SECS))),
    )


def record_from_args(detail: Dict[str, Any], args: Any) -> List[str]:
    """record_scan for a scanner's parsed args; a no-op without --event-log."""
    event_log = getattr(args, "event_log", None)
    if not event_log:
        return []
    try:
        return _record_from_args(detail, args, event_log)
    except Exception:  # diagnostics must never fail the scan that called them
        return []


def _record_from_args(detail: Dict[str, Any], args: Any, event_log: str) -> List[str]:
    lane = str(getattr(args, "lane_id", "") or "lane")
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in lane)
    return record_scan(
        detail, event_log=event_log,
        state_file="%s.demand-diag.%s.json" % (event_log, safe),
        runner=lane, tier=str(getattr(args, "labels", "") or ""),
        detect_inversion=bool(getattr(args, "inversion_detect", False)),
        threshold_secs=max(1, int(getattr(args, "inversion_threshold_secs",
                                           DEFAULT_THRESHOLD_SECS))))
