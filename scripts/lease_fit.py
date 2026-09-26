#!/usr/bin/env python3
"""Would a VM lease of this size be granted -- now, and ever?

Read-only companion to `leases.py acquire`. It answers with the same capacity
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

Separately, `max_concurrent` says how many leases of this size the budget holds
at once. A host whose lanes outnumber it has lanes that can only boot while a
sibling is idle (m5: two gate lanes at 12 cores in a 14-core universe). That is
also a configuration finding, surfaced by `tartci doctor fleet`.

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
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import leases  # noqa: E402

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
    """The lane's latest verdict, for `tartci doctor fleet` and `pool status`.

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


def lane_records(agents_dir: pathlib.Path, prefix: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Each managed macOS lane's latest lease-fit record, from its own plist.

    Returns (records, lanes_without_a_record).
    """
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for plist in sorted(agents_dir.glob(f"{prefix}*.plist")):
        if not plist.is_file() or plist.is_symlink():
            continue
        label = plist.name.removesuffix(".plist")
        try:
            env = plistlib.loads(plist.read_bytes()).get("EnvironmentVariables") or {}
        except (OSError, plistlib.InvalidFileException, ValueError):
            missing.append(label)
            continue
        state_dir = env.get("TARTCI_STATE_DIR")
        runner = env.get("TARTCI_RUNNER_NAME")
        if not isinstance(state_dir, str) or not isinstance(runner, str):
            missing.append(label)
            continue
        try:
            row = json.loads(record_path(state_dir, runner).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            missing.append(label)
            continue
        if isinstance(row, dict):
            row["label"] = label
            records.append(row)
        else:
            missing.append(label)
    return records, missing


def configuration_findings(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Lanes that can never lease, and identical lanes that cannot all run at once.

    Identical means the same VM size against the same budget. Lanes of
    different sizes share a host on purpose and are not compared.
    """
    never = [row for row in records if row.get("verdict") == "never"]
    groups: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = {}
    for row in records:
        if row.get("verdict") in ("fits_now", "not_now"):
            key = (row.get("requested_cores"), row.get("requested_mem_mb"), row.get("core_budget"))
            groups.setdefault(key, []).append(row)
    oversubscribed = []
    for (cores, mem, budget), rows in sorted(groups.items(), key=lambda item: str(item[0])):
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


def report(argv: list[str]) -> int:
    """`lease_fit.py report`: the configuration findings for `pool status`."""
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
    }
    if not args.text:
        print(json.dumps(payload, sort_keys=True))
        return 0
    if not payload["managed"]:
        return 0
    if not payload["never"] and not payload["oversubscribed"]:
        if records:
            print("lease fit: ok")
        else:
            print("lease fit: unknown (no lane has recorded a verdict yet)")
        return 0
    print("lease fit: CONFIGURATION")
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
