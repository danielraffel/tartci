#!/usr/bin/env python3
"""How the macOS VM slots were used: `tartci pool status --usage`.

Read-only. Everything here is derived from what the lane supervisors already
append to ~/.tartci/state/macos-fleet/<lane>/events.jsonl; nothing is sampled,
scanned or written. Per host and fleet-wide over a window (default 24h):

* slot occupancy: VM-seconds a guest existed (clone_start to teardown, or to
  the last event that still named the VM) against slots x window. A lane holds
  at most one VM, so a lane's occupancy is its busy time over the window.
* idle-with-demand: time a lane saw matching queued work, did not boot, and a
  VM slot was physically free (the guest count was below the host cap). The
  samples are the per-poll `demand_waiting` (slot_full branch),
  `yielded_to_priority` and `yielded_host_health` events. Demand while the host
  was full is reported separately as full-host wait: that is queueing, not
  waste. A lane in a lease-fit hold does not scan the queue, so its demand is
  not observed; those holds are reported as their own count and duration.
* JIT discards: VMs that were cloned and torn down without being assigned a
  job, by the event that named the cause.
* lease denials by axis (cores / memory / disk), from `lease_denied`.
* lifecycle: where a served VM's slot time went, from the one `vm_lifecycle`
  event a lane writes at teardown (providers/tart-macos/lifecycle.lib.sh):
  clone, boot to address, address to SSH, prep, runner registration, idle
  until assigned, the job, teardown. Per phase: VMs, total and median
  seconds. `job_share` is job seconds over VM seconds (clone through
  teardown, so pre_clone is excluded: no VM exists yet), and
  `overhead_per_served_job_s` is the rest of those VM seconds per served
  job. Only VMs that reached a runner write the event; VMs discarded earlier
  are the JIT discards above. Hosts on a tartci without the event report
  `vms: 0`.

Per-job VM CPU and IO are not sampled anywhere in tartci (runtime_measure
records only the configured cpu_count), so they are omitted and say so.

`--peer HOST=SSH_TARGET` runs this same file on a peer over ssh (the source is
piped to the peer's python3, so the peer needs no tartci update) and adds a
fleet-wide total. Python 3.9 compatible on purpose: that is the python3 an ssh
session gets on a stock Mac.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = "tartci.pool_usage.v1"
DEFAULT_ROOT = "~/.tartci/state/macos-fleet"
DEFAULT_CAP_FILE = "~/.config/tartci/macos-vm-cap"
HARD_MAX_SLOTS = 2
DEFAULT_POLL = 20
# Read this far before the window so a VM (job_timeout 7200 s + boot) that was
# already running when the window opened is reconstructed from its clone.
LOOKBACK_SECS = 4 * 3600
MAX_VM_SECS = 4 * 3600

DEMAND_EVENTS = ("demand_waiting", "yielded_to_priority", "yielded_host_health")
# Emitted on every pass of a waiting lane without changing what it is doing.
WAIT_NOISE = frozenset(("assignment_stale_demand", "inventory_unknown", "scan_recovered"))
# Events that name why a cloned VM was discarded without serving a job.
DISCARD_REASONS = (
    "idle_timeout", "assignment_v2_pre_mint_denied", "assignment_v2_idle_retarget",
    "yielded_to_workflow_tier", "admission_deferred", "admission_error",
    "aqua_preflight_failed", "chrome_preflight_failed", "runner_version_failed",
    "jit_repository_access_denied", "jit_repository_access_error",
    "jit_admission_denied", "boot_failed", "supervisor_signal", "teardown_restart",
)
LEASE_AXES = ("cores", "memory", "disk")
LIFECYCLE_PHASES = ("pre_clone", "clone", "boot_ip", "ip_ssh", "prep", "register", "idle",
                    "job", "teardown", "total")
CPU_IO_NOTE = ("not sampled: tartci records no per-VM CPU or IO "
               "(runtime_measure stores only the configured cpu_count)")

TS_BYTES = re.compile(rb'"ts":\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)"')
RUNNING = re.compile(r"running=(\S+?)/(\d+)")
QUEUED = re.compile(r"queued=(\d+)")
RESULT = re.compile(r"result=(\w+)")


# ── time ─────────────────────────────────────────────────────────────────────
def parse_ts(value: str) -> float:
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc).timestamp()


def fmt_ts(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_duration(value: str) -> int:
    m = re.fullmatch(r"(\d+)([smhd]?)", value.strip())
    if not m or int(m.group(1)) <= 0:
        raise argparse.ArgumentTypeError(f"bad duration {value!r}: use e.g. 90m, 24h, 7d")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def parse_until(value: str) -> float:
    try:
        return parse_ts(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad --until {value!r}: use YYYY-MM-DDTHH:MM:SSZ")


def overlap(a: float, b: float, lo: float, hi: float) -> float:
    return max(0.0, min(b, hi) - max(a, lo))


def union_seconds(spans: List[Tuple[float, float]], lo: float, hi: float) -> float:
    total, cur_a, cur_b = 0.0, None, None
    for a, b in sorted((max(a, lo), min(b, hi)) for a, b in spans):
        if b <= a:
            continue
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total


# ── reading ──────────────────────────────────────────────────────────────────
def read_lines_since(path: str, cutoff: str, chunk: int = 1 << 20) -> List[bytes]:
    """Lines whose ts is >= cutoff, reading the append-only log from its end."""
    want = cutoff.encode()
    blocks: List[bytes] = []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        carry = b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + carry
            if pos > 0:
                nl = buf.find(b"\n")
                if nl < 0:
                    carry = buf
                    continue
                carry, buf = buf[:nl + 1], buf[nl + 1:]
            else:
                carry = b""
            blocks.append(buf)
            m = TS_BYTES.search(buf[:256])
            if m and m.group(1) < want:
                break
    lines = []
    for line in b"".join(reversed(blocks)).splitlines():
        m = TS_BYTES.search(line[:256])
        if m and m.group(1) >= want:
            lines.append(line)
    return lines


def load_events(path: str, cutoff: str) -> List[Dict[str, Any]]:
    out = []
    for i, line in enumerate(read_lines_since(path, cutoff)):
        try:
            d = json.loads(line.decode("utf-8", "replace"))
            t = parse_ts(d["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        fields = d.get("fields") if isinstance(d.get("fields"), dict) else {}
        out.append({"t": t, "i": i, "event": str(d.get("event") or ""),
                    "vm": str(d.get("vm") or ""), "runner": str(d.get("runner") or ""),
                    "detail": str(d.get("detail") or ""), "fields": fields})
    out.sort(key=lambda e: (e["t"], e["i"]))
    return out


def read_state_vm(lane_dir: str, runner: str) -> Optional[str]:
    """The VM the lane's heartbeat says it holds now, or None if unreadable."""
    try:
        with open(os.path.join(lane_dir, f"{runner}.state.json")) as f:
            return str(json.load(f).get("vm") or "")
    except (OSError, ValueError, AttributeError):
        return None


# ── reconstruction ───────────────────────────────────────────────────────────
class Vm:
    def __init__(self, start: float):
        self.start = start
        self.last_t = start
        self.end: Optional[float] = None
        self.name: Optional[str] = None
        self.minted = False
        self.assigned_at: Optional[float] = None
        self.reason: Optional[str] = None
        self.running = False

    @property
    def served(self) -> bool:
        return self.assigned_at is not None

    def discard_reason(self) -> str:
        if self.reason:
            return self.reason
        return "unrecorded" if self.minted else "unrecorded_pre_mint"


def reconstruct_vms(events: List[Dict[str, Any]], now: float,
                    live_vm: Optional[str]) -> List[Vm]:
    """One interval per VM this lane cloned.

    A VM is live from clone_start while the supervisor's events carry its name.
    It ends at its `teardown`, else at the last event that named it (every
    discard path clears CURRENT_VM, so the next event has an empty vm).
    """
    vms: List[Vm] = []
    cur: Optional[Vm] = None

    def close(at: float) -> None:
        nonlocal cur
        if cur is not None:
            cur.end = max(cur.start, at)
            vms.append(cur)
            cur = None

    for ev in events:
        e, vm, t = ev["event"], ev["vm"], ev["t"]
        if e == "clone_start":
            if cur is not None:
                close(cur.last_t)
            cur = Vm(t)
            continue
        if cur is None:
            continue
        if not vm:
            # CURRENT_VM is empty again: the VM is gone. An unnamed one (it
            # failed before any event could name it) ends at its own cause.
            if e in DISCARD_REASONS and cur.reason is None:
                cur.reason = e
            close(t if cur.name is None and e in DISCARD_REASONS else cur.last_t)
            continue
        if cur.name is None:
            cur.name = vm
        elif vm != cur.name:
            close(cur.last_t)
            continue
        cur.last_t = t
        if e == "mint_jit":
            cur.minted = True
        elif e == "job_assigned" and cur.assigned_at is None:
            cur.assigned_at = t
        if e in DISCARD_REASONS and cur.reason is None and cur.assigned_at is None:
            cur.reason = e
        if e == "teardown":
            close(t)
    if cur is not None:
        # Still open at the end of the log: running now if the heartbeat holds
        # it; otherwise it ended unrecorded at its last event.
        if live_vm is not None and cur.name and live_vm == cur.name:
            cur.running = True
            cur.end = now
        elif live_vm is None:
            cur.running = True
            cur.end = min(now, cur.start + MAX_VM_SECS)
        else:
            cur.end = cur.last_t
        vms.append(cur)
    return vms


def demand_spans(events: List[Dict[str, Any]], default_poll: int) -> List[Dict[str, Any]]:
    """Each demand sample covers [t, end): up to the next sample when the lane
    kept waiting (next sample within two polls), else until the lane did
    something else, bounded by one poll (the sleep that followed the sample)."""
    flow = [ev for ev in events if ev["event"] not in WAIT_NOISE]
    spans = []
    for k, ev in enumerate(flow):
        if ev["event"] not in DEMAND_EVENTS:
            continue
        f = ev["fields"]
        poll = f.get("poll") if isinstance(f.get("poll"), int) and f.get("poll") > 0 else default_poll
        nxt = flow[k + 1] if k + 1 < len(flow) else None
        if nxt is not None and nxt["event"] in DEMAND_EVENTS and nxt["t"] - ev["t"] <= 2 * poll:
            end = nxt["t"]
        else:
            end = ev["t"] + poll
            if nxt is not None:
                end = min(end, nxt["t"])
        running, cap = f.get("running"), f.get("cap")
        if running is None or cap is None:
            m = RUNNING.search(ev["detail"])
            if m:
                running, cap = m.group(1), m.group(2)
        try:
            state = "idle" if int(running) < int(cap) else "full"
        except (TypeError, ValueError):
            state = "unknown"
        if ev["event"] == "demand_waiting":
            reason = "demand_waiting:" + str(f.get("reason") or "unspecified")
        else:
            reason = ev["event"]
        spans.append({"a": ev["t"], "b": max(ev["t"], end), "state": state, "reason": reason})
    return spans


def lease_fit_holds(events: List[Dict[str, Any]], now: float) -> List[Tuple[float, float]]:
    """[lease_unfit_now, lane proceeds) intervals: the lane could not lease its
    VM and so did not scan for work."""
    spans, start = [], None
    for ev in events:
        e = ev["event"]
        if e in ("lease_unfit_now", "lease_never_fits"):
            if start is None:
                start = ev["t"]
        elif start is not None and e in ("lease_fit_restored", "job_claim", "admission_precheck",
                                         "clone_start", "supervisor_signal", "demand_waiting"):
            spans.append((start, ev["t"]))
            start = None
    if start is not None:
        spans.append((start, now))
    return spans


# ── report ───────────────────────────────────────────────────────────────────
def lane_report(lane: str, lane_dir: str, events: List[Dict[str, Any]], since: float,
                until: float, now: float, default_poll: int) -> Dict[str, Any]:
    runner = next((ev["runner"] for ev in reversed(events) if ev["runner"]), lane)
    live_vm = read_state_vm(lane_dir, runner)
    vms = reconstruct_vms(events, now, live_vm)
    window = until - since
    busy = sum(overlap(v.start, v.end, since, until) for v in vms)
    job = sum(overlap(v.assigned_at, v.end, since, until) for v in vms if v.served)
    done = [v for v in vms if not v.running and since <= v.end < until]
    served = [v for v in done if v.served]
    discards: Dict[str, int] = {}
    for v in done:
        if not v.served:
            discards[v.discard_reason()] = discards.get(v.discard_reason(), 0) + 1
    in_win = [ev for ev in events if since <= ev["t"] < until]
    denials = {axis: 0 for axis in LEASE_AXES}
    for ev in in_win:
        if ev["event"] == "lease_denied":
            axis = str(ev["fields"].get("axis") or "")
            if not axis:
                m = re.search(r"axis=(\S+)", ev["detail"])
                axis = m.group(1) if m else "none"
            denials[axis] = denials.get(axis, 0) + 1
    results: Dict[str, int] = {}
    for ev in in_win:
        if ev["event"] == "job_terminal_receipt":
            m = RESULT.search(ev["detail"])
            key = m.group(1) if m else "unknown"
            results[key] = results.get(key, 0) + 1
    spans = demand_spans(events, default_poll)
    by_reason: Dict[str, float] = {}
    for s in spans:
        if s["state"] == "idle":
            by_reason[s["reason"]] = by_reason.get(s["reason"], 0.0) + overlap(s["a"], s["b"], since, until)
    holds = lease_fit_holds(events, now)
    lifecycle = [ev["fields"] for ev in in_win if ev["event"] == "vm_lifecycle"]
    return {
        "lane": lane,
        "runner": runner,
        "window_seconds": window,
        "busy_vm_seconds": round(busy, 1),
        "job_vm_seconds": round(job, 1),
        "occupancy": round(busy / window, 4) if window else None,
        "vms_cloned": sum(1 for v in vms if since <= v.start < until),
        "vms_running": sum(1 for v in vms if v.running),
        "jobs_served": len(served),
        "vms_discarded": sum(discards.values()),
        "discards_by_reason": dict(sorted(discards.items())),
        "idle_with_demand_seconds": round(sum(by_reason.values()), 1),
        "idle_with_demand_by_reason": {k: round(v, 1) for k, v in sorted(by_reason.items())},
        "full_host_wait_seconds": round(sum(overlap(s["a"], s["b"], since, until)
                                            for s in spans if s["state"] == "full"), 1),
        "demand_samples": sum(1 for s in spans if since <= s["a"] < until),
        "demand_waiting_events": sum(1 for ev in in_win if ev["event"] == "demand_waiting"),
        "lease_denials_by_axis": denials,
        "lease_fit_holds": sum(1 for a, _ in holds if since <= a < until),
        "lease_fit_hold_seconds": round(sum(overlap(a, b, since, until) for a, b in holds), 1),
        "job_results": dict(sorted(results.items())),
        "warm_parked_events": sum(1 for ev in in_win if ev["event"] == "warm_parked"),
        "_spans": spans,
        "_vms": [(v.start, v.end) for v in vms],
        "_lifecycle": lifecycle,
    }


def host_slots(cap_file: str, override: Optional[int]) -> Tuple[int, str]:
    if override:
        return max(1, min(override, HARD_MAX_SLOTS)), "--slots"
    try:
        with open(os.path.expanduser(cap_file)) as f:
            digits = re.sub(r"\D", "", f.read())[:2]
        if digits and int(digits) >= 1:
            return min(int(digits), HARD_MAX_SLOTS), cap_file
    except OSError:
        pass
    return HARD_MAX_SLOTS, "default (Apple 2-guest limit)"


def max_concurrent(spans: List[Tuple[float, float]], lo: float, hi: float) -> int:
    edges = []
    for a, b in spans:
        a, b = max(a, lo), min(b, hi)
        if b > a:
            edges += [(a, 1), (b, -1)]
    cur = best = 0
    for _, d in sorted(edges, key=lambda x: (x[0], x[1])):
        cur += d
        best = max(best, cur)
    return best


def _seconds(row: Dict[str, Any], phase: str) -> Optional[float]:
    value = row.get(f"{phase}_s")
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def lifecycle_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-phase VMs, total and median seconds over `vm_lifecycle` fields, and
    how much of the VMs' slot time was the job."""
    phases: Dict[str, Dict[str, Any]] = {}
    for phase in LIFECYCLE_PHASES:
        values = [v for v in (_seconds(r, phase) for r in rows) if v is not None]
        phases[phase] = {"vms": len(values), "seconds": round(sum(values), 1),
                         "median_s": _median(values)}
    served = sum(1 for r in rows if r.get("served") == 1)
    vm_seconds = 0.0
    for r in rows:
        total = _seconds(r, "total")
        if total is not None:
            vm_seconds += total - (_seconds(r, "pre_clone") or 0.0)
    job = phases["job"]["seconds"]
    return {
        "vms": len(rows),
        "served": served,
        "warm": sum(1 for r in rows if r.get("warm") == 1),
        "vm_seconds": round(vm_seconds, 1),
        "job_seconds": job,
        "job_share": round(job / vm_seconds, 4) if vm_seconds else None,
        "overhead_per_served_job_s": round((vm_seconds - job) / served, 1) if served else None,
        "phases": phases,
    }


def host_report(root: str, host: str, since: float, until: float, now: float,
                slots: int, slots_source: str, default_poll: int) -> Dict[str, Any]:
    root = os.path.expanduser(root)
    cutoff = fmt_ts(since - LOOKBACK_SECS)
    lanes, errors = [], []
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        names = []
        errors.append(f"cannot list {root}: {exc.strerror}")
    first_demand_waiting = None
    for name in names:
        lane_dir = os.path.join(root, name)
        path = os.path.join(lane_dir, "events.jsonl")
        if not os.path.isfile(path):
            continue
        try:
            events = load_events(path, cutoff)
        except OSError as exc:
            errors.append(f"{path}: {exc.strerror}")
            continue
        for ev in events:
            if ev["event"] == "demand_waiting":
                if first_demand_waiting is None or ev["t"] < first_demand_waiting:
                    first_demand_waiting = ev["t"]
                break
        rep = lane_report(name, lane_dir, events, since, until, now, default_poll)
        if rep["busy_vm_seconds"] or any(since <= ev["t"] < until for ev in events):
            lanes.append(rep)
    window = until - since
    capacity = slots * window
    busy = sum(l["busy_vm_seconds"] for l in lanes)
    spans = [s for l in lanes for s in l["_spans"]]
    vm_spans = [x for l in lanes for x in l["_vms"]]
    by_reason: Dict[str, float] = {}
    discards: Dict[str, int] = {}
    denials = {axis: 0 for axis in LEASE_AXES}
    results: Dict[str, int] = {}
    for l in lanes:
        for k, v in l["idle_with_demand_by_reason"].items():
            by_reason[k] = round(by_reason.get(k, 0.0) + v, 1)
        for k, v in l["discards_by_reason"].items():
            discards[k] = discards.get(k, 0) + v
        for k, v in l["lease_denials_by_axis"].items():
            denials[k] = denials.get(k, 0) + v
        for k, v in l["job_results"].items():
            results[k] = results.get(k, 0) + v
    lifecycle = lifecycle_summary([row for l in lanes for row in l["_lifecycle"]])
    for l in lanes:
        del l["_spans"], l["_vms"], l["_lifecycle"]
    notes = []
    if first_demand_waiting is None or first_demand_waiting > since:
        notes.append("demand_waiting not recorded for the whole window (first seen: %s): until "
                     "then only the yield branches sampled demand, so idle-with-demand and "
                     "full-host wait are lower bounds" % (fmt_ts(first_demand_waiting)
                                                          if first_demand_waiting else "never"))
    if any(l["warm_parked_events"] for l in lanes):
        notes.append("warm-parked VMs are not modelled: their slot time is not counted as busy")
    return {
        "host": host,
        "slots": slots,
        "slots_source": slots_source,
        "window_seconds": window,
        "capacity_vm_seconds": capacity,
        "busy_vm_seconds": round(busy, 1),
        "job_vm_seconds": round(sum(l["job_vm_seconds"] for l in lanes), 1),
        "occupancy": round(busy / capacity, 4) if capacity else None,
        "max_concurrent_vms": max_concurrent(vm_spans, since, until),
        "idle_with_demand_seconds": round(union_seconds(
            [(s["a"], s["b"]) for s in spans if s["state"] == "idle"], since, until), 1),
        "idle_with_demand_by_reason": dict(sorted(by_reason.items())),
        "full_host_wait_seconds": round(union_seconds(
            [(s["a"], s["b"]) for s in spans if s["state"] == "full"], since, until), 1),
        "demand_waiting_first_seen": fmt_ts(first_demand_waiting) if first_demand_waiting else None,
        "jobs_served": sum(l["jobs_served"] for l in lanes),
        "vms_discarded": sum(discards.values()),
        "discards_by_reason": dict(sorted(discards.items())),
        "lease_denials_by_axis": denials,
        "lease_fit_holds": sum(l["lease_fit_holds"] for l in lanes),
        "lease_fit_hold_seconds": round(sum(l["lease_fit_hold_seconds"] for l in lanes), 1),
        "job_results": dict(sorted(results.items())),
        "lifecycle": lifecycle,
        "vm_cpu_io": None,
        "notes": notes,
        "errors": errors,
        "lanes": lanes,
    }


SUM_KEYS = ("capacity_vm_seconds", "busy_vm_seconds", "job_vm_seconds",
            "idle_with_demand_seconds", "full_host_wait_seconds", "jobs_served",
            "vms_discarded", "lease_fit_holds", "lease_fit_hold_seconds")


def fleet_total(hosts: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok = [h for h in hosts if "busy_vm_seconds" in h]
    out: Dict[str, Any] = {"hosts": len(ok), "unreachable": [h["host"] for h in hosts if h not in ok]}
    for k in SUM_KEYS:
        out[k] = round(sum(h[k] for h in ok), 1)
    out["occupancy"] = round(out["busy_vm_seconds"] / out["capacity_vm_seconds"], 4) \
        if out["capacity_vm_seconds"] else None
    lc = [h.get("lifecycle") or {} for h in ok]
    vm_s = round(sum(x.get("vm_seconds") or 0 for x in lc), 1)
    job_s = round(sum(x.get("job_seconds") or 0 for x in lc), 1)
    served = sum(x.get("served") or 0 for x in lc)
    out["lifecycle"] = {
        "vms": sum(x.get("vms") or 0 for x in lc), "served": served,
        "vm_seconds": vm_s, "job_seconds": job_s,
        "job_share": round(job_s / vm_s, 4) if vm_s else None,
        "overhead_per_served_job_s": round((vm_s - job_s) / served, 1) if served else None,
        "phase_seconds": {ph: round(sum(((x.get("phases") or {}).get(ph) or {}).get("seconds") or 0
                                        for x in lc), 1) for ph in LIFECYCLE_PHASES},
    }
    for key in ("discards_by_reason", "lease_denials_by_axis", "idle_with_demand_by_reason"):
        merged: Dict[str, float] = {a: 0 for a in LEASE_AXES} if key == "lease_denials_by_axis" else {}
        for h in ok:
            for k, v in h[key].items():
                merged[k] = round(merged.get(k, 0) + v, 1)
        out[key] = merged if key == "lease_denials_by_axis" else dict(sorted(merged.items()))
    return out


def run_peer(host: str, target: str, args: argparse.Namespace, until: float) -> Dict[str, Any]:
    with open(os.path.abspath(__file__), "rb") as f:
        source = f.read()
    remote = ["python3", "-", "--json", "--host-label", host, "--range", f"{args.range}s",
              "--until", fmt_ts(until), "--default-poll", str(args.default_poll)]
    # ssh-stdin: this script's own source is piped in (input=) for `python3 -`
    cmd = [args.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target,
           " ".join(remote)]
    try:
        proc = subprocess.run(cmd, input=source, capture_output=True, timeout=args.peer_timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"host": host, "error": f"ssh {target}: {type(exc).__name__}"}
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        return {"host": host, "error": f"ssh {target} exit {proc.returncode}: {tail[0][:200]}"}
    try:
        return json.loads(proc.stdout)["hosts"][0]
    except (ValueError, KeyError, IndexError):
        return {"host": host, "error": f"ssh {target}: unreadable report"}


# ── output ───────────────────────────────────────────────────────────────────
def mins(seconds: float) -> str:
    return f"{seconds / 60:.1f}"


def pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def counts(d: Dict[str, Any]) -> str:
    items = [f"{k} {v}" for k, v in d.items() if v]
    return ", ".join(items) if items else "none"


def axes(d: Dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items())


def render_host(h: Dict[str, Any]) -> List[str]:
    if "error" in h:
        return [f"host {h['host']}: UNREACHABLE ({h['error']})"]
    out = [f"host {h['host']}: {h['slots']} VM slot(s) ({h['slots_source']})"]
    out.append(f"  {'lane':<22}{'busy min':>10}{'occ':>8}{'job min':>9}{'served':>8}"
               f"{'discard':>9}{'idle+dem min':>13}{'full-wait min':>14}{'lease-hold min':>15}")
    for l in h["lanes"]:
        out.append(f"  {l['lane']:<22}{mins(l['busy_vm_seconds']):>10}{pct(l['occupancy']):>8}"
                   f"{mins(l['job_vm_seconds']):>9}{l['jobs_served']:>8}{l['vms_discarded']:>9}"
                   f"{mins(l['idle_with_demand_seconds']):>13}{mins(l['full_host_wait_seconds']):>14}"
                   f"{mins(l['lease_fit_hold_seconds']):>15}")
    out.append(f"  occupancy: {mins(h['busy_vm_seconds'])} of {mins(h['capacity_vm_seconds'])} "
               f"slot-min ({pct(h['occupancy'])}); running a job {mins(h['job_vm_seconds'])} min; "
               f"max concurrent VMs {h['max_concurrent_vms']}")
    out.append(f"  idle-with-demand: {mins(h['idle_with_demand_seconds'])} min "
               f"({counts(h['idle_with_demand_by_reason'])}); full-host wait "
               f"{mins(h['full_host_wait_seconds'])} min")
    out.append(f"  jobs served: {h['jobs_served']} ({counts(h['job_results'])}); "
               f"VMs discarded without a job: {h['vms_discarded']}")
    out.append(f"  discards by reason: {counts(h['discards_by_reason'])}")
    out.append(f"  lease denials by axis: {axes(h['lease_denials_by_axis'])}; lease-fit holds "
               f"{h['lease_fit_holds']} ({mins(h['lease_fit_hold_seconds'])} lane-min)")
    out += render_lifecycle(h.get("lifecycle") or {})
    out.append(f"  per-job VM CPU/IO: {CPU_IO_NOTE}")
    for n in h["notes"]:
        out.append(f"  note: {n}")
    for e in h["errors"]:
        out.append(f"  error: {e}")
    return out


def render_lifecycle(lc: Dict[str, Any]) -> List[str]:
    if not lc.get("vms"):
        return ["  lifecycle: no vm_lifecycle events in the window (a tartci without them, "
                "or no VM reached a runner)"]
    out = [f"  lifecycle ({lc['vms']} VM(s) that reached a runner, {lc['served']} served, "
           f"{lc['warm']} warm): job share {pct(lc['job_share'])} of "
           f"{mins(lc['vm_seconds'])} VM-min; overhead per served job "
           + ("n/a" if lc["overhead_per_served_job_s"] is None
              else f"{mins(lc['overhead_per_served_job_s'])} min")]
    cells = []
    for phase in LIFECYCLE_PHASES:
        row = lc["phases"][phase]
        if row["vms"]:
            cells.append(f"{phase} {mins(row['seconds'])} min (median "
                         f"{row['median_s']:.0f}s, {row['vms']})")
    out.append("  phases: " + "; ".join(cells))
    return out


def render(rep: Dict[str, Any]) -> str:
    w = rep["window"]
    out = [f"pool usage: {w['since']} -> {w['until']} ({mins(w['seconds'])} min)"]
    for h in rep["hosts"]:
        out += render_host(h)
    if rep.get("fleet"):
        f = rep["fleet"]
        out.append(f"fleet ({f['hosts']} host(s)"
                   + (f"; UNREACHABLE: {', '.join(f['unreachable'])}" if f["unreachable"] else "")
                   + f"): occupancy {pct(f['occupancy'])} ({mins(f['busy_vm_seconds'])} of "
                   f"{mins(f['capacity_vm_seconds'])} slot-min); idle-with-demand "
                   f"{mins(f['idle_with_demand_seconds'])} min; full-host wait "
                   f"{mins(f['full_host_wait_seconds'])} min; served {f['jobs_served']}; "
                   f"discarded {f['vms_discarded']} ({counts(f['discards_by_reason'])}); "
                   f"lease denials {axes(f['lease_denials_by_axis'])}")
        lc = f.get("lifecycle") or {}
        if lc.get("vms"):
            out.append(f"fleet lifecycle: job share {pct(lc['job_share'])} of "
                       f"{mins(lc['vm_seconds'])} VM-min over {lc['vms']} VM(s); overhead per "
                       "served job "
                       + ("n/a" if lc["overhead_per_served_job_s"] is None
                          else f"{mins(lc['overhead_per_served_job_s'])} min"))
    return "\n".join(out)


def span(seconds: int) -> str:
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def summary_line(h: Dict[str, Any], seconds: int) -> str:
    lower = " (lower bound)" if h["notes"] and "demand_waiting" in h["notes"][0] else ""
    return (f"usage ({span(seconds)}): occupancy {pct(h['occupancy'])} of {h['slots']} slot(s), "
            f"{h['jobs_served']} job(s) served, {h['vms_discarded']} VM(s) discarded, "
            f"idle-with-demand {mins(h['idle_with_demand_seconds'])} min{lower}, "
            f"lease denials {sum(h['lease_denials_by_axis'].values())} "
            f"(details: tartci pool status --usage)")


def parse_peers(values: List[str]) -> List[Tuple[str, str]]:
    peers = []
    for value in values:
        for item in re.split(r"[,\s]+", value.strip()):
            if not item:
                continue
            host, _, target = item.partition("=")
            peers.append((host, target or host))
    return peers


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--range", type=parse_duration, default=86400, help="window length (default 24h)")
    p.add_argument("--until", type=parse_until, default=None, help="window end, UTC (default now)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--summary", action="store_true", help="one line for `tartci pool status`")
    p.add_argument("--events-root", default=os.environ.get("TARTCI_POOL_USAGE_ROOT", DEFAULT_ROOT))
    p.add_argument("--cap-file", default=os.environ.get("TARTCI_MACOS_CAP_FILE", DEFAULT_CAP_FILE))
    p.add_argument("--slots", type=int, default=None, help="host VM slots (default: cap file, else 2)")
    p.add_argument("--host-label", default=None)
    p.add_argument("--default-poll", type=int, default=DEFAULT_POLL,
                   help="poll seconds for demand samples that do not carry one")
    p.add_argument("--peer", action="append", default=[], metavar="HOST=SSH_TARGET",
                   help="also report a peer over ssh (repeatable or comma-separated)")
    p.add_argument("--no-local", action="store_true", help="with --peer: peers only")
    p.add_argument("--ssh", default=os.environ.get("TARTCI_POOL_USAGE_SSH", "ssh"))
    p.add_argument("--peer-timeout", type=float, default=180.0)
    p.add_argument("--now", type=parse_until, default=None, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    now = args.now if args.now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    until = args.until if args.until is not None else now
    since = until - args.range
    host = args.host_label or socket.gethostname().split(".")[0]
    hosts = []
    if not (args.no_local and args.peer):
        slots, source = host_slots(args.cap_file, args.slots)
        hosts.append(host_report(args.events_root, host, since, until, now, slots, source,
                                 args.default_poll))
    for peer, target in parse_peers(args.peer):
        hosts.append(run_peer(peer, target, args, until))
    rep: Dict[str, Any] = {"schema": SCHEMA,
                           "window": {"since": fmt_ts(since), "until": fmt_ts(until),
                                      "seconds": args.range},
                           "vm_cpu_io": {"sampled": False, "note": CPU_IO_NOTE},
                           "hosts": hosts}
    if len(hosts) > 1:
        rep["fleet"] = fleet_total(hosts)
    if args.summary:
        print(summary_line(hosts[0], args.range) if hosts and "error" not in hosts[0]
              else "usage: unavailable")
    elif args.json:
        print(json.dumps(rep, indent=2, sort_keys=False))
    else:
        print(render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
