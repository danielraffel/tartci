#!/usr/bin/env python3
"""Shadow-first, rules-only serving truth monitor for ONE host.

Each host judges only itself: the rules read this host's own lane event logs
and the queued self-hosted gate jobs this host's published registrations
serve (gate_job_demand.py). No host ever reports a verdict about a peer, so
four hosts each running this pass never flag one another from a local view.

Rules, each scoped to this host (`me`, its advertised host_id):

- host_queued_no_job_start: a job this host serves has waited the whole
  window and no lane here logged `job_assigned` in it, while some lane that
  serves the job was free to take it.
- scan_blind_while_queued: a lane here is scan-blind for SCAN_BLIND_CYCLES
  consecutive cycles while a job this host serves waits.
- admission_error_streak: ADMISSION_STREAK admission errors in a row; any
  precheck, admission success or assignment breaks the streak.
- host_error_burst: one first-error across 3 heads within the burst window.
- lane_death_by_signal: a lane with an assigned job died by a quarantining
  signal before its terminal receipt and before its VM was torn down.

Each (rule, host) is one episode with its own lifecycle and, outside shadow
mode, its own GitHub issue whose body ends with the tartci-incident marker
(tartci_incident.py). An episode is fixed only by evidence on this host
AFTER it was detected: a `job_assigned` for the serving rules, a successful
terminal receipt for the failure rules. The fix is written into the marker
(issue body PATCH) before the issue is closed. This module records evidence
and may open issues; it never changes pool state.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import gate_job_demand as demand
import host_off
import tartci_incident as incident

WINDOW_SECONDS = 20 * 60
ADMISSION_STREAK = 3
BURST_WINDOW_SECONDS = 30 * 60
SCAN_BLIND_CYCLES = 3
# How far back the event logs are read. An assigned job that has not logged
# its terminal receipt within this long is no longer counted as running.
LOOKBACK_SECONDS = 6 * 3600
TITLE = "[tartci] serving-truth: {rule} on {host}"
STATE_NAME = "serving-truth-episodes.json"

SERVING_RULES = ("host_queued_no_job_start", "scan_blind_while_queued", "admission_error_streak")
FAILURE_RULES = ("host_error_burst", "lane_death_by_signal")
_ADMISSION_OK = {"admission_precheck", "admission_check", "admission_success",
                 "admission_parallel_start", "job_claim", "lease_acquired", "job_assigned"}
_ADMISSION_ERR = {"admission_error", "admission_precheck_error"}
_SUCCESS = re.compile(r"result=succeeded|conclusion\W*success\b", re.I)


def _epoch(value: Any) -> Optional[float]:
    return demand.epoch(value)


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time(row: dict) -> Optional[float]:
    return _epoch(row.get("ts") or row.get("timestamp"))


def _fields(row: dict) -> dict:
    value = row.get("fields")
    return value if isinstance(value, dict) else {}


def _detail(row: dict) -> str:
    return str(row.get("detail") or _fields(row).get("detail") or "")


def _runner(row: dict) -> str:
    return str(row.get("runner") or _fields(row).get("runner") or "")


def host_of(row: dict) -> str:
    """The advertised host_id a lane event belongs to.

    Fleet runners are named `<host_id>-<identity>-<pid>-<boot>`, so the host is
    the runner name up to its first dash: `studio-pulp-gate-01` is `studio`,
    `m5studio-pulp-gate-01` is `m5studio`. An explicit `host` field wins.
    Nicknames (m3, m5s) resolve through gate_job_demand.canonical_host.
    """
    explicit = row.get("host") or _fields(row).get("host")
    if explicit:
        return demand.canonical_host(explicit)
    runner = _runner(row)
    return demand.canonical_host(runner.split("-", 1)[0]) if runner else ""


def _value(row: dict, *keys: str) -> Any:
    fields = _fields(row)
    for key in keys:
        if row.get(key) is not None:
            return row[key]
        if fields.get(key) is not None:
            return fields[key]
    return None


def _head(row: dict) -> str:
    value = _value(row, "pr_head", "head_sha", "head", "sha")
    if value:
        return str(value)
    found = re.search(r"(?:head|sha|commit)[=: ]+([0-9a-f]{7,40})", _detail(row), re.I)
    return found.group(1) if found else ""


def _first_error(row: dict) -> str:
    value = _value(row, "first_error", "error", "reason")
    return str(value or (_detail(row).splitlines() or [""])[0])


def _parse(raw: bytes) -> Optional[dict]:
    try:
        row = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return row if isinstance(row, dict) and row.get("event") else None


def read_events_since(path: Path, since: float, chunk: int = 1 << 16) -> List[dict]:
    """The rows of one append-only events.jsonl logged at or after `since`.

    The file is read backwards in chunks and reading stops at the first
    complete row older than `since`, so a multi-megabyte lane log costs only
    its recent tail. Rows without a timestamp are kept.
    """
    out: List[dict] = []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            pos = handle.tell()
            tail = b""
            while pos > 0:
                step = min(chunk, pos)
                pos -= step
                handle.seek(pos)
                lines = (handle.read(step) + tail).split(b"\n")
                tail = lines.pop(0) if pos > 0 else b""
                for raw in reversed(lines):
                    row = _parse(raw)
                    if row is None:
                        continue
                    stamp = _time(row)
                    if stamp is not None and stamp < since:
                        return list(reversed(out))
                    out.append(row)
    except OSError:
        return []
    return list(reversed(out))


def read_event_paths(paths: Iterable[Path], since: float) -> List[dict]:
    rows: List[dict] = []
    for path in paths:
        rows.extend(read_events_since(path, since))
    return sorted(rows, key=lambda row: _time(row) or 0)


def _serving_lanes(job: dict, regs: List[dict], me: str) -> List[str]:
    """This host's lanes whose registration serves `job`."""
    lanes = []
    for reg in regs:
        if str(reg.get("host_id")) != me or not reg.get("lane"):
            continue
        if demand.serving_hosts(job, str(job.get("repo") or ""), str(job.get("workflow") or ""), [reg]):
            if reg["lane"] not in lanes:
                lanes.append(str(reg["lane"]))
    return lanes


def _running(rows: List[dict], end: float) -> Dict[str, dict]:
    """runner -> its `job_assigned` row, for runners with no terminal receipt since."""
    running: Dict[str, dict] = {}
    for row in rows:
        stamp = _time(row)
        if stamp is None or stamp > end:
            continue
        if row.get("event") == "job_assigned":
            running[_runner(row)] = row
        elif row.get("event") == "job_terminal_receipt":
            running.pop(_runner(row), None)
    return running


def _in_lanes(runner: str, me: str, lanes: Optional[List[str]]) -> bool:
    """A runner `<me>-<lane>-...` belongs to lane `<lane>` (slots such as
    `pulp-gate-slot2` included). `lanes` None means every lane."""
    return lanes is None or any(runner.startswith(f"{me}-{lane}-") for lane in lanes)


def _all_serving_runners_busy(me: str, lanes: List[str], seen: Iterable[str],
                              running: Dict[str, dict]) -> bool:
    """True only when every runner seen for the serving lanes holds a job.

    Unknown lanes, or no runner seen at all, are never busy, so a silent host
    still trips.
    """
    if not lanes:
        return False
    mine = [runner for runner in seen if _in_lanes(runner, me, lanes)]
    return bool(mine) and all(runner in running for runner in mine)


def _job_row(job: dict, now: float) -> dict:
    queued = _epoch(job.get("created_at"))
    return {"id": job.get("id"), "run_id": job.get("run_id"), "repo": job.get("repo"),
            "workflow": job.get("workflow"), "name": job.get("name"),
            "labels": job.get("labels"), "created_at": job.get("created_at"),
            "waited_minutes": None if queued is None else int((now - queued) // 60)}


def classify(events: Iterable[dict], jobs: Iterable[dict] = (), *, me: str,
             regs: Optional[List[dict]] = None, now: Optional[float] = None,
             window_seconds: int = WINDOW_SECONDS, admission_streak: int = ADMISSION_STREAK,
             burst_window_seconds: int = BURST_WINDOW_SECONDS,
             scan_blind_cycles: int = SCAN_BLIND_CYCLES,
             lookback_seconds: int = LOOKBACK_SECONDS) -> List[dict]:
    """The rule trips for host `me` alone.

    `events` may hold any host's rows; only `me`'s are read. `jobs` are
    gate_job_demand.queued_jobs answers; only the ones `me` serves count.
    `regs` (advertised registrations) maps a job to the lanes here that serve
    it, so a host whose serving lanes are all running jobs is not flagged.
    """
    me = demand.canonical_host(me)
    end = time.time() if now is None else float(now)
    start = end - window_seconds
    rows = sorted((row for row in events if isinstance(row, dict) and host_of(row) == me
                   and (_time(row) is None or end - lookback_seconds <= _time(row) <= end)),
                  key=lambda row: _time(row) or 0)
    in_window = [row for row in rows if (_time(row) or -1) >= start]
    waiting = demand.jobs_for(jobs, me, end)
    trips: List[dict] = []

    def trip(rule: str, since: float, latest: float, evidence: dict) -> None:
        trips.append({"rule": rule, "host": me, "since": _iso(since), "latest": _iso(latest),
                      "episode_key": f"{rule}:{me}", "evidence": evidence})

    starts = [row for row in in_window if row.get("event") == "job_assigned"]
    assigned = [row for row in rows if row.get("event") == "job_assigned"]
    last_start = assigned[-1] if assigned else None
    running = _running(rows, end)
    seen = sorted({_runner(row) for row in rows if _runner(row)})
    # A blind or holding lane matters only when it serves the waiting work;
    # without published registrations every lane here is read.
    serving = (sorted({lane for job in waiting for lane in _serving_lanes(job, regs, me)})
               if regs is not None else None)
    blind_rows = [row for row in in_window if row.get("event") == "scan_blind"
                  and _in_lanes(_runner(row), me, serving)]
    holds = [row for row in in_window if row.get("event") == "fallback_hold"
             and _in_lanes(_runner(row), me, serving)]

    # 1. Queued work this host serves waited the whole window, and nothing here started.
    old = [job for job in waiting if (_epoch(job.get("created_at")) or end) <= start]
    if old and not starts:
        lanes = sorted({lane for job in old for lane in _serving_lanes(job, regs or [], me)})
        if not _all_serving_runners_busy(me, lanes, seen, running):
            # The symptom began when work was waiting AND this host was free:
            # the later of the oldest wait, its last start, and the last
            # terminal receipt on a serving lane.
            oldest = min(_epoch(job.get("created_at")) or end for job in old)
            freed = [_time(row) or 0 for row in rows
                     if row.get("event") == "job_terminal_receipt" and _in_lanes(_runner(row), me, lanes)]
            since = max([oldest, (_time(last_start) or 0) if last_start else 0] + freed)
            trip("host_queued_no_job_start", since, end, {
                "queued_jobs": [_job_row(job, end) for job in old],
                "serving_lanes": lanes,
                "window_start": _iso(start), "window_end": _iso(end),
                "last_job_assigned": last_start,
                "running": sorted(running),
                "scan_blind_in_window": len(blind_rows),
                "last_scan_blind": blind_rows[-1] if blind_rows else None,
                "fallback_holds_in_window": len(holds),
                "last_fallback_hold": holds[-1] if holds else None,
            })

    # 2. A lane that cannot see its queue while work it serves waits.
    if waiting and blind_rows:
        worst: Dict[str, Tuple[int, dict]] = {}
        for row in blind_rows:
            found = re.search(r"consecutive\s*[=:]\s*(\d+)", _detail(row), re.I)
            count = int(found.group(1)) if found else 1
            runner = _runner(row)
            if runner not in worst or count >= worst[runner][0]:
                worst[runner] = (count, row)
        blind = {runner: hit for runner, hit in worst.items() if hit[0] >= scan_blind_cycles}
        if blind:
            hits = [hit for _, hit in blind.values()]
            trip("scan_blind_while_queued", min(_time(hit) or end for hit in hits),
                 max(_time(hit) or end for hit in hits), {
                     "blind_runners": {runner: {"consecutive": count, "row": hit}
                                       for runner, (count, hit) in sorted(blind.items())},
                     "queued_jobs": [_job_row(job, end) for job in waiting],
                     "last_job_assigned": last_start,
                 })

    # 3. Sustained admission errors; any precheck, admission or assignment breaks it.
    streak: List[dict] = []
    for row in in_window:
        if row.get("event") in _ADMISSION_OK:
            streak = []
        elif row.get("event") in _ADMISSION_ERR:
            streak.append(row)
    if len(streak) >= admission_streak:
        trip("admission_error_streak", _time(streak[0]) or end, _time(streak[-1]) or end,
             {"errors": streak, "threshold": admission_streak, "last_job_assigned": last_start})

    # 4. One first-error on three different heads inside the burst window.
    failures: Dict[str, List[dict]] = {}
    for row in rows:
        stamp = _time(row)
        if (stamp is None or stamp < end - burst_window_seconds
                or row.get("event") not in {"job_failed", "job_failure", "job_terminal_receipt"}):
            continue
        head, error = _head(row), _first_error(row)
        if head and error and any(word in error.lower()
                                  for word in ("error", "fail", "license", "timeout", "refused")):
            failures.setdefault(error, []).append(row)
    for error, hit in sorted(failures.items()):
        if len({_head(row) for row in hit}) >= 3:
            times = [_time(row) or end for row in hit]
            trip("host_error_burst", min(times), max(times),
                 {"first_error": error, "heads": sorted({_head(row) for row in hit}),
                  "failures": hit, "threshold": 3})

    # 5. A lane holding a job died by a quarantining signal.
    held: Dict[str, dict] = {}
    deaths: List[dict] = []
    for row in rows:
        event, runner = row.get("event"), _runner(row)
        if event == "job_assigned":
            held[runner] = row
        elif event in ("job_terminal_receipt", "teardown"):
            # The lane's VM is gone once it is torn down, so the job it held
            # can no longer die. A supervisor stopped later (a self-update or
            # a reload of an idle lane) still logs the last job's quarantine
            # (m1, m3 and m5, 2026-10-15 and 10-16).
            held.pop(runner, None)
        elif event == "supervisor_signal" and (_time(row) or -1) >= start:
            found = re.search(r"quarantine=([^\s]+)", _detail(row))
            if found and found.group(1).lower() != "none" and runner in held:
                deaths.append({"signal": row, "assigned": held.pop(runner)})
    if deaths:
        times = [_time(death["signal"]) or end for death in deaths]
        trip("lane_death_by_signal", min(times), max(times), {"deaths": deaths})
    return trips


# ── the issue body ───────────────────────────────────────────────────────────

_LOCK_FIX = ["cat ~/.tartci/state/queue-observation.lock.owner.json "
             "~/.tartci/state/queue-observation.lock.escalation.json",
             "tartci pool status",
             "tartci launchd reload <label of the lane named in owner.json> --dry-run",
             "tartci launchd reload <label of the lane named in owner.json>"]
_LOCK_SAFE = ("No. Reload only the lane that holds the lock; `launchd reload` refuses a "
              "lane that is mid-job (exit 3, nothing changed).")

def _jobs_text(jobs: List[dict]) -> List[str]:
    return [f"- job {job.get('id')} `{job.get('name')}` ({job.get('repo')} {job.get('workflow')}, "
            f"run {job.get('run_id')}), queued {job.get('created_at')}, "
            f"waited {job.get('waited_minutes')} min" for job in jobs]


def _row_text(row: Optional[dict]) -> str:
    if not row:
        return "none in the last 6 h"
    return f"{row.get('ts')} on `{_runner(row)}`: {row.get('event')} {_detail(row)[:200]}".rstrip()


def render(trip: dict, detected_at: str) -> Tuple[str, str]:
    """(title, body) of one episode's issue: what, evidence, cause, fix,
    whether the fix is safe to auto-apply, how to verify, then the marker."""
    rule, host, ev = trip["rule"], trip["host"], trip["evidence"]
    lines: List[str] = []
    if rule == "host_queued_no_job_start":
        jobs = ev["queued_jobs"]
        lanes = ", ".join(ev["serving_lanes"]) or "none published"
        what = (f"`{host}` started 0 gate jobs between {ev['window_start']} and {ev['window_end']} "
                f"while {len(jobs)} queued job(s) its lanes serve ({lanes}) waited.")
        evidence = _jobs_text(jobs) + [
            f"- last `job_assigned` here: {_row_text(ev['last_job_assigned'])}",
            f"- runners holding a job: {', '.join(ev['running']) or 'none'}",
            f"- `scan_blind` rows in the window: {ev['scan_blind_in_window']}"
            + (f"; last: {_row_text(ev['last_scan_blind'])}" if ev["last_scan_blind"] else ""),
            f"- `fallback_hold` rows in the window: {ev['fallback_holds_in_window']}"
            + (f"; last: {_row_text(ev['last_fallback_hold'])}" if ev["last_fallback_hold"] else ""),
        ]
        fix = ["tartci pool status", "tartci doctor fleet",
               "tartci pool on   # only if pool status shows the host off"]
        safe = "No. Confirm the cause in pool status first; `pool on` is safe only when the host is off."
        if ev["scan_blind_in_window"]:
            cause = ("The lanes cannot read their queue: the host queue observation lock or the "
                     "assignment scan is timing out (see scan_blind rows).")
            fix, safe = _LOCK_FIX, _LOCK_SAFE
        elif ev["fallback_holds_in_window"]:
            cause = ("The lanes are holding for peer cover (`fallback_hold`), but no peer has taken "
                     "the job either: the peer the hold counts on is not serving.")
            fix = ["tartci pool status   # on this host and on the peer named in the hold",
                   "tartci doctor fleet"]
            safe = "No. The peer the hold counts on has to be fixed or taken out of the pool."
        else:
            cause = "The serving lanes are not running, are quarantined, or the pool is off."
        verify = [f"A `job_assigned` on `{host}` (watchdog closes this issue on it), "
                  "or none of the jobs above still queued."]
    elif rule == "scan_blind_while_queued":
        runners = ev["blind_runners"]
        what = (f"{len(runners)} lane(s) on `{host}` have been scan-blind for "
                f"{max(item['consecutive'] for item in runners.values())} consecutive cycles while "
                f"{len(ev['queued_jobs'])} job(s) they serve waited.")
        evidence = [f"- `{runner}`: consecutive={item['consecutive']}; {_row_text(item['row'])}"
                    for runner, item in runners.items()]
        evidence += _jobs_text(ev["queued_jobs"])
        evidence.append(f"- last `job_assigned` here: {_row_text(ev['last_job_assigned'])}")
        cause = ("The host queue observation lock is held too long, or the assignment scan "
                 "exceeds its deadline (lock_contention or timeout in the rows above).")
        fix, safe = _LOCK_FIX, _LOCK_SAFE
        verify = [f"The next `scan_blind` row is gone and a `job_assigned` appears on `{host}`."]
    elif rule == "admission_error_streak":
        errors = ev["errors"]
        text = _detail(errors[-1])[:300]
        what = (f"`{host}` logged {len(errors)} admission errors in a row with no precheck, "
                "admission or assignment between them.")
        evidence = [f"- {_row_text(row)}" for row in errors[-5:]]
        evidence.append(f"- last `job_assigned` here: {_row_text(ev['last_job_assigned'])}")
        cause = f"Admission keeps failing with: {text}"
        fix = ["tartci pool status", "tartci doctor fleet"]
        safe = "No. The fix depends on the admission error text above."
        verify = [f"An admission success or `job_assigned` on `{host}`."]
    elif rule == "host_error_burst":
        what = (f"`{host}` failed {len(ev['heads'])} different heads with the same first error "
                "inside 30 minutes.")
        evidence = [f"- first error: {ev['first_error'][:300]}"]
        evidence += [f"- head {head}" for head in ev["heads"]]
        cause = "A host-level fault (tool, license, disk or network) fails every job the same way."
        fix = ["tartci pool status", "tartci doctor fleet",
               "tartci pool drain   # stop taking jobs until the fault is fixed"]
        safe = "Drain is safe (finishes running jobs); the repair itself needs an operator."
        verify = [f"A successful terminal receipt on `{host}` (watchdog closes this issue on it)."]
    else:
        deaths = ev["deaths"]
        what = f"{len(deaths)} lane(s) on `{host}` died by a quarantining signal while holding a job."
        evidence = [f"- `{_runner(d['signal'])}`: {_row_text(d['signal'])}; job assigned "
                    f"{d['assigned'].get('ts')}" for d in deaths]
        cause = "The supervisor was signalled mid-job (reload, kill, or launchd teardown)."
        fix = ["tartci pool status", "tartci launchd reload <lane label> --dry-run"]
        safe = "No. Find who sent the signal before reloading."
        verify = [f"A successful terminal receipt on `{host}`."]
    lines += ["## What happened", what, "", "## Evidence", *evidence, "",
              "## Likely cause", cause, "", f"## Fix (run on `{host}`)", "```sh", *fix, "```", "",
              "## Safe to auto-apply", safe, "", "## How to verify", *verify]
    body = "\n".join(lines)
    line = incident.marker(host, rule, trip["since"], detected_at)
    return TITLE.format(rule=rule, host=host), incident.with_marker(body, line)


# ── the per-episode lifecycle ────────────────────────────────────────────────

def recovery(rule: str, rows: Iterable[dict], host: str, after: float) -> Optional[dict]:
    """The first row on `host` after `after` that proves the episode fixed."""
    for row in rows:
        stamp = _time(row)
        if stamp is None or stamp <= after or host_of(row) != host:
            continue
        if rule in SERVING_RULES and row.get("event") == "job_assigned":
            return row
        if (rule in FAILURE_RULES and row.get("event") == "job_terminal_receipt"
                and _SUCCESS.search(_detail(row))):
            return row
    return None


def _read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"episodes": {}, "last_fixed": {}}
    if not isinstance(value, dict):
        return {"episodes": {}, "last_fixed": {}}
    value.setdefault("episodes", {})
    value.setdefault("last_fixed", {})
    return value


def trip_receipt(state: Path, trip: dict, now: float) -> Path:
    state.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = state / f"serving-truth-{trip['rule']}-{trip['host']}-{stamp}.json"
    n = 1
    while path.exists():
        path = state / f"serving-truth-{trip['rule']}-{trip['host']}-{stamp}-{n}.json"
        n += 1
    path.write_text(json.dumps({"kind": "tartci.serving-truth", "observed_at": stamp, **trip},
                               indent=2, sort_keys=True) + "\n")
    return path


def shadow_mode() -> bool:
    """Shadow unless TARTCI_SERVING_TRUTH_ALARM=on."""
    return os.environ.get("TARTCI_SERVING_TRUTH_ALARM", "shadow").lower() != "on"


IssueOpen = Callable[[str, str], Tuple[int, str]]
IssuePatch = Callable[[str, str], Tuple[int, str]]
IssueClose = Callable[[str], Tuple[int, str]]


def alert_pass(state: Path, rows: List[dict], jobs: List[dict], *, me: str,
               regs: Optional[List[dict]] = None, now: Optional[float] = None,
               shadow: bool = True, demand_errors: Iterable[str] = (),
               issue: Optional[IssueOpen] = None, patch: Optional[IssuePatch] = None,
               close: Optional[IssueClose] = None) -> dict:
    """One tick: classify, then advance each (rule, host) episode.

    A trip whose key has no open episode starts one (receipt, event, and an
    issue outside shadow mode) unless its newest evidence is no newer than
    that key's last fix. An open episode is fixed by the first recovery row
    after its detection, once the rule no longer trips on newer evidence:
    `fixed_at` is written into the issue's marker (PATCH), then the issue is
    closed. A failed open, PATCH or close is retried on the next tick.
    """
    end = time.time() if now is None else float(now)
    me = demand.canonical_host(me)
    trips = classify(rows, jobs, me=me, regs=regs, now=end)
    path = state / STATE_NAME
    data = _read_state(path)
    episodes, last_fixed = data["episodes"], data["last_fixed"]
    opener = issue or host_off._open_issue
    patcher = patch or host_off._patch_issue_body
    closer = close or host_off._close_issue
    by_key = {trip["episode_key"]: trip for trip in trips}
    out: dict = {"trips": trips, "receipts": [], "shadow": shadow, "opened": [], "fixed": [],
                 "opened_keys": [], "demand_errors": list(demand_errors)}

    for key, trip in by_key.items():
        if key in episodes:
            episodes[key]["latest"] = trip["latest"]
            continue
        fixed_at = _epoch(last_fixed.get(key))
        if fixed_at is not None and (_epoch(trip["latest"]) or end) <= fixed_at:
            continue
        detected = _iso(end)
        title, body = render(trip, detected)
        receipt = trip_receipt(state, trip, end)
        episodes[key] = {"rule": trip["rule"], "host": trip["host"], "symptom_at": trip["since"],
                         "detected_at": detected, "fixed_at": None, "latest": trip["latest"],
                         "title": title, "issue_body": body, "issue": None,
                         "receipt": str(receipt)}
        out["receipts"].append(str(receipt))
        out["opened_keys"].append(key)
        host_off.event(state, "serving_truth_alarm", f"{trip['rule']} on {trip['host']}",
                       {"episode_key": key, "receipt": str(receipt)}, end)

    for key, ep in list(episodes.items()):
        if not ep.get("fixed_at"):
            hit = recovery(ep["rule"], rows, ep["host"], _epoch(ep["detected_at"]) or end)
            current = by_key.get(key)
            if hit is None or (current and (_epoch(current["latest"]) or end) > (_time(hit) or end)):
                if not shadow and not ep.get("issue"):
                    rc, text = opener(ep["title"], ep["issue_body"])
                    if rc == 0 and text.strip().isdigit():
                        ep["issue"] = text.strip()
                        ep.pop("issue_error", None)
                        out["opened"].append(ep["issue"])
                    else:
                        ep["issue_error"] = text[:300]
                continue
            ep["fixed_at"] = hit.get("ts")
            ep["fixed_by"] = hit
        if ep.get("issue") and not ep.get("patched"):
            body = incident.fixed(ep["issue_body"], ep["fixed_at"])
            rc, text = patcher(ep["issue"], body)
            if rc != 0:
                ep["issue_error"] = text[:300]
                continue
            ep["issue_body"], ep["patched"] = body, True
        if ep.get("issue"):
            rc, text = closer(ep["issue"])
            if rc != 0:
                ep["issue_error"] = text[:300]
                continue
        last_fixed[key] = ep["fixed_at"]
        out["fixed"].append({"episode_key": key, "symptom_at": ep["symptom_at"],
                             "detected_at": ep["detected_at"], "fixed_at": ep["fixed_at"],
                             "issue": ep.get("issue")})
        del episodes[key]

    state.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    out["episodes"] = episodes
    return out


def fetch_demand(cli: str, advertised: dict, me: str, now: float) -> Tuple[List[dict], List[str]]:
    """Queued gate jobs this host's registrations serve, and any read errors."""
    return demand.queued_jobs(demand.gh_fetcher(cli), demand.registrations(advertised, me), now)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host-id", required=True, help="this host's advertised host_id")
    parser.add_argument("--events", type=Path, action="append", required=True)
    parser.add_argument("--advertised", type=Path, required=True,
                        help="fleet/advertised-labels.json")
    parser.add_argument("--jobs-json", type=Path,
                        help="recorded {jobs: [...]} instead of reading GitHub")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--now", default=None, help="ISO time to judge at (replay)")
    parser.add_argument("--gh-cli", default=os.environ.get("TARTCI_GH_CLI", "ghapp"))
    args = parser.parse_args(argv)
    now = _epoch(args.now) if args.now else time.time()
    advertised = json.loads(args.advertised.read_text())
    if args.jobs_json:
        jobs, errors = json.loads(args.jobs_json.read_text()).get("jobs", []), []
    else:
        jobs, errors = fetch_demand(args.gh_cli, advertised, demand.canonical_host(args.host_id), now)
    rows = read_event_paths(args.events, now - LOOKBACK_SECONDS)
    out = alert_pass(args.state, rows, jobs, me=args.host_id,
                     regs=demand.registrations(advertised), now=now,
                     shadow=shadow_mode(), demand_errors=errors)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
