#!/usr/bin/env python3
"""Would a VM lease of this size be granted -- now, and ever?

Read-only companion to leases.py acquire. It answers with the same capacity
model (the same profile, the same reservations, the same core and memory axes)
without writing a record, so a VM supervisor can learn that its lease cannot be
granted BEFORE it spends a Shipyard admission call and a GitHub queue scan on
work it could not boot.

Two answers matter, and they are different:

* not now: the host is busy (another VM or agent build holds the cores). The
  supervisor waits and asks again; nothing is wrong.
* never: the lease is larger than the budget it is admitted against, so it
  would be denied on an idle host. That is a configuration fault, not load,
  and polling for work cannot fix it. It is reported, not retried.

Separately, max_concurrent says how many leases of this size the budget holds
at once. A host whose lanes outnumber it has lanes that can only boot while a
sibling is idle (m5: two gate lanes at 12 cores in a 14-core universe). That is
also a configuration finding, surfaced by tartci doctor fleet.

Exit codes: 0 fits now, 3 not now, 4 never, 1 could not tell. Callers must treat
1 as "unknown" and fall back to acquiring as before.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import plistlib
import socket
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import leases  # noqa: E402
import macos_runner_identity  # noqa: E402

FITS_NOW = 0
NOT_NOW = 3
NEVER = 4
UNKNOWN = 1


def class_budget(cfg: dict[str, int], priority: int) -> tuple[int, int]:
    """(core budget, memory budget) this priority is admitted against.

    Mirrors leases.acquire: a gate-priority lease is held to the host total, a
    lower-priority lease to the total minus the gate reserve. Memory 0 = axis off.
    """
    if priority >= cfg["gate_priority"]:
        return cfg["total"], cfg["total_mem_mb"]
    cores = max(1, cfg["total"] - cfg["reserved_gate_cores"])
    mem = 0
    if cfg["total_mem_mb"] > 0:
        mem = max(cfg["per_job_mem_mb"], cfg["total_mem_mb"] - cfg["reserved_gate_mem_mb"])
    return cores, mem


def evaluate(
    cfg: dict[str, int],
    active: list[dict[str, Any]],
    *,
    cores: int,
    mem_mb: int | None,
    priority: int,
) -> dict[str, Any]:
    if cores <= 0:
        raise ValueError("lease cores must be positive")
    req_mem = mem_mb if mem_mb is not None else cores * cfg["per_job_mem_mb"]
    core_budget, mem_budget = class_budget(cfg, priority)
    mem_axis = cfg["total_mem_mb"] > 0
    never_cores = cores > core_budget or cores > cfg["total"]
    never_mem = mem_axis and (req_mem > mem_budget or req_mem > cfg["total_mem_mb"])
    by_cores = core_budget // cores
    by_mem = (mem_budget // req_mem) if mem_axis and req_mem > 0 else by_cores
    current = leases.usage(active, cfg)
    used_class = (
        current["used_cores"]
        if priority >= cfg["gate_priority"]
        else current["non_gate_used_cores"]
    )
    now_cores = (
        current["used_cores"] + cores <= cfg["total"]
        and used_class + cores <= core_budget
    )
    now_mem = True
    if mem_axis:
        used_mem_class = (
            current.get("used_mem_mb", 0)
            if priority >= cfg["gate_priority"]
            else current.get("non_gate_used_mem_mb", 0)
        )
        now_mem = (
            current.get("used_mem_mb", 0) + req_mem <= cfg["total_mem_mb"]
            and used_mem_class + req_mem <= mem_budget
        )
    never = never_cores or never_mem
    if never:
        verdict = "never"
    elif now_cores and now_mem:
        verdict = "fits_now"
    else:
        verdict = "not_now"
    return {
        "verdict": verdict,
        "requested_cores": cores,
        "requested_mem_mb": req_mem,
        "priority": priority,
        "core_budget": core_budget,
        "mem_budget_mb": mem_budget,
        "max_concurrent": 0 if never else max(0, min(by_cores, by_mem)),
        "never_axis": {"cores": never_cores, "memory": bool(never_mem)},
        "binding_axis_now": (
            None
            if verdict != "not_now"
            else ("cores" if not now_cores else "memory")
        ),
        "used_cores": current["used_cores"],
        "available_cores": current["available_cores"],
        "total_cores": cfg["total"],
    }


RANK = {"fits_now": 0, "not_now": 1, "never": 2}


def run(argv: list[str] | None = None) -> tuple[dict[str, Any], int]:
    parser = argparse.ArgumentParser(prog="lease_fit")
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--mem-mb", type=int)
    parser.add_argument(
        "--priority", action="append", default=[],
        help="a priority this lane can lease at; repeat for each class. The "
             "most favourable answer across them is reported.")
    parser.add_argument(
        "--non-gate-cap", type=int,
        help="clamp a non-gate lease to this many cores, as acquisition does")
    parser.add_argument("--no-live", action="store_true",
                        help="ignore live leases (answers only the never/max_concurrent question)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--record", help="also write the verdict here for doctor/status")
    parser.add_argument("--lane", default="", help="lane name stored in the record")
    ours, rest = parser.parse_known_args(argv)
    # Everything else (store dir, role, capacity overrides) is leases.py's own
    # grammar, so the capacity model is the one acquire would use.
    lease_args = leases.parse_args(["status", *rest])
    cfg = leases.capacity_config(lease_args)
    active: list[dict[str, Any]] = []
    if not ours.no_live:
        store_dir = pathlib.Path(lease_args.store_dir).expanduser()
        records = leases.load_records(store_dir)
        active, _, _ = leases.reclaim(records, int(lease_args.stale_secs))
        # A parked warm VM's memory-only lease yields to real demand (the lane
        # that cannot get memory asks it to; providers/tart-macos/warm-vm.lib.sh),
        # so it must not make a lane skip the very poll that would ask. Its own
        # lane is served by upgrading that lease in place, not by a second one.
        active = [record for record in active if not record.get("memory_only")]
    best: dict[str, Any] | None = None
    for raw in ours.priority or ["vm"]:
        priority, _ = leases.parse_priority(raw)
        cores = ours.cores
        if (
            ours.non_gate_cap
            and ours.non_gate_cap > 0
            and priority < cfg["gate_priority"]
            and cores > ours.non_gate_cap
        ):
            cores = ours.non_gate_cap
        result = evaluate(cfg, active, cores=cores, mem_mb=ours.mem_mb, priority=priority)
        if best is None or RANK[result["verdict"]] < RANK[best["verdict"]] or (
            RANK[result["verdict"]] == RANK[best["verdict"]]
            and result["max_concurrent"] > best["max_concurrent"]
        ):
            best = result
    assert best is not None
    rc = {"fits_now": FITS_NOW, "not_now": NOT_NOW, "never": NEVER}[best["verdict"]]
    if ours.record:
        write_record(pathlib.Path(ours.record), best, ours.lane)
    return best, rc


def write_record(path: pathlib.Path, result: dict[str, Any], lane: str) -> None:
    """The lane's latest verdict, for tartci doctor fleet and pool status.

    Best effort: a record that cannot be written must never change the verdict.
    """
    try:
        payload = dict(result)
        payload["lane"] = lane
        payload["updated_at"] = dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def record_path(state_dir: str, runner_name: str) -> pathlib.Path:
    return pathlib.Path(state_dir).expanduser() / f"{runner_name}.lease-fit.json"


EVENT_WINDOW_SECS = 24 * 3600
EVENT_TAIL_BYTES = 8 * 1024 * 1024
FIT_EVENTS = ("lease_unfit_now", "lease_fit_restored", "lease_never_fits")


def lease_fit_events(path: pathlib.Path, now: dt.datetime | None = None,
                     window_secs: int = EVENT_WINDOW_SECS) -> dict[str, int] | None:
    """Count this lane's lease-fit transitions in the last window_secs.

    The lane logs a transition, not every poll: lease_unfit_now when its VM
    stops fitting and lease_fit_restored when it fits again. None means the
    log could not be read, which is not the same as zero transitions.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(seconds=window_secs)
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - EVENT_TAIL_BYTES))
            data = handle.read()
    except OSError:
        return None
    counts = {name: 0 for name in FIT_EVENTS}
    for raw in data.splitlines():
        if b"lease_" not in raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        name = row.get("event") if isinstance(row, dict) else None
        if name not in counts:
            continue
        try:
            ts = dt.datetime.strptime(str(row.get("ts")), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if ts >= cutoff:
            counts[name] += 1
    return counts


def lane_records(agents_dir: pathlib.Path, prefix: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Each managed macOS lane's latest lease-fit record, from its own plist.

    The record's file name is the lane's runner name, which a fleet plist
    rarely states: it is derived from the name prefix and slot exactly as the
    supervisor derives it (macos_runner_identity), so it is resolved the same
    way here. Returns (records, lanes_without_a_record).
    """
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    hostname = socket.gethostname()
    for plist in sorted(agents_dir.glob(f"{prefix}*.plist")):
        if not plist.is_file() or plist.is_symlink():
            continue
        label = plist.name.removesuffix(".plist")
        try:
            data = plistlib.loads(plist.read_bytes())
            identity = macos_runner_identity.resolve_plist_identity(data, hostname=hostname)
        except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
            missing.append(label)
            continue
        env = data.get("EnvironmentVariables") if isinstance(data, dict) else None
        env = env if isinstance(env, dict) else {}
        try:
            row = json.loads(record_path(identity.state_dir, identity.runner_name)
                             .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            missing.append(label)
            continue
        if isinstance(row, dict):
            row["label"] = label
            repo = env.get("TARTCI_RUNNER_REPO")
            if isinstance(repo, str) and repo:
                row["repo"] = repo
            runner_labels = env.get("TARTCI_RUNNER_LABELS")
            if isinstance(runner_labels, str) and runner_labels:
                row["runner_labels"] = runner_labels
            event_log = env.get("TARTCI_EVENT_LOG")
            if not isinstance(event_log, str) or not event_log:
                event_log = str(pathlib.Path(identity.state_dir) / "events.jsonl")
            row["events_24h"] = lease_fit_events(pathlib.Path(event_log).expanduser())
            records.append(row)
        else:
            missing.append(label)
    return records, missing


def configuration_findings(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Lanes that can never lease, and identical lanes that cannot all run at once.

    Identical means the same VM size against the same budget, serving the same
    repository with the same runner labels. Lanes of different sizes, or for
    different repositories or label classes (a release lane beside two gate
    lanes, whose queues fill independently), share a host on purpose and are
    not compared.
    """
    never = [row for row in records if row.get("verdict") == "never"]
    groups: dict[tuple[Any, Any, Any, Any, Any], list[dict[str, Any]]] = {}
    for row in records:
        if row.get("verdict") in ("fits_now", "not_now"):
            key = (row.get("requested_cores"), row.get("requested_mem_mb"),
                   row.get("core_budget"), row.get("repo"), row.get("runner_labels"))
            groups.setdefault(key, []).append(row)
    oversubscribed = []
    for (cores, mem, budget, _repo, _labels), rows in sorted(groups.items(), key=lambda item: str(item[0])):
        capacity = min(int(row.get("max_concurrent") or 0) for row in rows)
        if len(rows) > capacity:
            oversubscribed.append({
                "lanes": sorted(str(row.get("lane") or row.get("label")) for row in rows),
                "vm_cores": cores,
                "vm_mem_mb": mem,
                "core_budget": budget,
                "max_concurrent": capacity,
            })
    return {"never": never, "oversubscribed": oversubscribed}


def verdict_summary(records: list[dict[str, Any]]) -> str:
    """"5 lanes: 4 fit now, 1 not now; last 24h: 12 not-now waits, 11 restored"."""
    now: dict[str, int] = {}
    for row in records:
        verdict = str(row.get("verdict") or "unknown").replace("_", " ")
        now[verdict] = now.get(verdict, 0) + 1
    order = ["fits now", "not now", "never", "unknown"]
    parts = [f"{now[key]} {key}" for key in order if key in now]
    parts += [f"{count} {key}" for key, count in sorted(now.items()) if key not in order]
    text = f"{len(records)} lanes: " + ", ".join(parts)
    counted = [row["events_24h"] for row in records if isinstance(row.get("events_24h"), dict)]
    if counted:
        unfit = sum(c["lease_unfit_now"] for c in counted)
        restored = sum(c["lease_fit_restored"] for c in counted)
        text += f"; last 24h: {unfit} not-now waits, {restored} restored"
        if len(counted) < len(records):
            text += f" ({len(records) - len(counted)} lanes' logs unreadable)"
    else:
        text += "; last 24h: event logs unreadable"
    return text


def report(argv: list[str]) -> int:
    """lease_fit.py report: the configuration findings for pool status."""
    parser = argparse.ArgumentParser(prog="lease_fit report")
    parser.add_argument("--agents-dir", default=str(pathlib.Path.home() / "Library/LaunchAgents"))
    parser.add_argument("--prefix", default="com.danielraffel.tartci.tart-runner-macos-fleet.")
    parser.add_argument("--text", action="store_true")
    args = parser.parse_args(argv)
    records, missing = lane_records(pathlib.Path(args.agents_dir), args.prefix)
    findings = configuration_findings(records)
    payload = {
        "managed": bool(records or missing),
        "measured_lanes": len(records),
        "unmeasured_lanes": missing,
        "never": [
            {"lane": row.get("lane") or row.get("label"),
             "vm_cores": row.get("requested_cores"),
             "core_budget": row.get("core_budget")}
            for row in findings["never"]
        ],
        "oversubscribed": findings["oversubscribed"],
        "lanes": [
            {"lane": row.get("lane") or row.get("label"),
             "verdict": row.get("verdict"),
             "updated_at": row.get("updated_at"),
             "events_24h": row.get("events_24h")}
            for row in records
        ],
    }
    if not args.text:
        print(json.dumps(payload, sort_keys=True))
        return 0
    if not payload["managed"]:
        return 0
    if not payload["never"] and not payload["oversubscribed"]:
        if records:
            print(f"lease fit: ok ({verdict_summary(records)})")
        else:
            print("lease fit: unknown (no lane has recorded a verdict yet)")
        return 0
    print(f"lease fit: CONFIGURATION ({verdict_summary(records)})")
    for row in payload["never"]:
        print(f"  lane {row['lane']}: {row['vm_cores']}-core VM can never lease "
              f"(budget {row['core_budget']}); it does not poll for work")
    for group in payload["oversubscribed"]:
        print(f"  {len(group['lanes'])} lanes of {group['vm_cores']}-core VMs "
              f"({', '.join(group['lanes'])}) but the {group['core_budget']}-core "
              f"budget fits {group['max_concurrent']} at once")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["report"]:
        return report(argv[1:])
    try:
        result, rc = run(argv)
    except Exception as exc:  # noqa: BLE001 - "could not tell" is its own answer
        print(json.dumps({"verdict": "unknown", "error": str(exc)}, sort_keys=True))
        return UNKNOWN
    print(json.dumps(result, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
