#!/usr/bin/env python3
"""Do a host's gate lanes fit its gate reserve? A ratchet, not a gate.

A gate lease is admitted against the host's whole lease capacity, so a gate
slot can always start on an idle host. But agent builds may hold every core
outside the gate reserve, so on a busy host the lane's slots run together only
if they fit inside that reserve. m3, 2026-10-04: two Pulp gate slots of 12
cores against a 14-core reserve; the second slot logged `lease_denied
axis=cores reason=capacity_exceeded requested_cores=12` 8 times and macos jobs
queued (#373 resized them to 7).

The reserve differs per host: it is computed by host_profile.py from the
host's cores, memory, role and governor settings, so the fit is computed from
this host's live host-profile, per gate lane and per axis:

    cores   supervisors x the lane's VM cores against reserved_gate_cores
            (lane_vm_cores: an explicit vm_cores, else the reserve share when
            the lane sets vm_cores_from = "gate-reserve", else vm_pool_cores)
    memory  supervisors x the VM memory derived from those cores (the same
            rule as vm-lease.lib.sh) against reserved_gate_mem_mb

A gate lane is one without an explicit priority (the Pulp gate's event
classes lease at gate priority) or with `priority = "gate"`.

A lane can instead derive its VM size from the host: `vm_cores_from =
"gate-reserve"` sizes each slot to the largest core count, at most
vm_pool_cores, at which all of the lane's slots fit the gate reserve on both
axes (share_cores). The lease helper, gate supply and this fit all read the
size through share_cores, so they cannot disagree about it.

A host whose installed profile still overcommits must keep updating, so this
is a RATCHET: every overcommitted (lane, axis) is reported on
every evaluation, and a target profile is refused only when its overcommit on
some (lane, axis) is strictly greater than the installed profile's on the
same pair, both measured against the same live reserve. Equal is allowed;
smaller is the direction wanted. Resizing is a profile decision with the
host's owner, never automatic, and must not take agent cores.

Python 3.9-safe.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Tuple

PER_JOB_MEM_MB = 1536
VM_MIN_MEM_MB = 8192
VM_MAX_MEM_MB = 16384
AXES = ("cores", "memory")


def vm_mem_mb(cores: int, per_job: int = PER_JOB_MEM_MB) -> int:
    """tartci_vm_lease_derived_mem_mb: (cores - 1) x per-job x 4/3, clamped."""
    jobs = max(1, cores - 1)
    return max(VM_MIN_MEM_MB, min(VM_MAX_MEM_MB, jobs * per_job * 4 // 3))


GATE_RESERVE_SHARE = "gate-reserve"


def share_cores(host: Dict[str, Any], slots: int) -> int:
    """VM cores per slot so `slots` gate VMs fit the gate reserve together.

    The largest count from 1 to vm_pool_cores whose slots fit reserved_gate_cores
    and, when the host reports one, reserved_gate_mem_mb (memory derived from the
    cores as vm_mem_mb does). A host with no gate core reserve has nothing to
    share, so it keeps vm_pool_cores. Never below 1: a reserve smaller than the
    slot count still yields a bootable VM, and the fit reports the overcommit.
    """
    pool = max(1, int(host["vm_pool_cores"]))
    reserve_cores = int(host.get("reserved_gate_cores") or 0)
    if reserve_cores <= 0:
        return pool
    slots = max(1, int(slots))
    per_job = int(host.get("per_compile_job_mem_mb") or PER_JOB_MEM_MB)
    reserve_mem = int(host.get("reserved_gate_mem_mb") or 0)
    cores = max(1, min(pool, reserve_cores // slots))
    while cores > 1 and reserve_mem > 0 and slots * vm_mem_mb(cores, per_job) > reserve_mem:
        cores -= 1
    return cores


def lane_vm_cores(lane: Dict[str, Any], host: Dict[str, Any]) -> int:
    """The cores one VM of this lane leases on this host."""
    if lane.get("vm_cores"):
        return int(lane["vm_cores"])
    if lane.get("vm_cores_from") == GATE_RESERVE_SHARE:
        return share_cores(host, int(lane.get("supervisors", 1)))
    return int(host["vm_pool_cores"])


def gate_lanes(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [lane for lane in profile.get("lane", []) or []
            if isinstance(lane, dict) and lane.get("priority") in (None, "gate")]


def fit(profile: Dict[str, Any], host: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One row per gate lane and axis: demand, reserve and overcommit."""
    rows = []
    per_job = int(host.get("per_compile_job_mem_mb") or PER_JOB_MEM_MB)
    for lane in gate_lanes(profile):
        supervisors = int(lane.get("supervisors", 1))
        cores = lane_vm_cores(lane, host)
        demand = {"cores": supervisors * cores,
                  "memory": supervisors * vm_mem_mb(cores, per_job)}
        reserve = {"cores": int(host["reserved_gate_cores"]),
                   "memory": int(host.get("reserved_gate_mem_mb") or 0)}
        for axis in AXES:
            if reserve[axis] <= 0:
                # No gate reserve on this axis: nothing to fit inside. The
                # caller says so with not_applicable_lines(), never "fits".
                continue
            rows.append({"lane": str(lane.get("id")), "axis": axis,
                         "demand": demand[axis], "reserve": reserve[axis],
                         "over": max(0, demand[axis] - reserve[axis])})
    return rows


def not_applicable_lines(profile: Dict[str, Any], host: Dict[str, Any]) -> List[str]:
    """Axes with gate lanes declared but no gate reserve to measure them against.

    A reserve of 0 is not a fit: the measurement cannot be made. A host whose
    role reserves no gate cores reads n/a for the whole check; a cores reserve
    with an unread memory reserve reads n/a for the memory axis alone.
    """
    if not gate_lanes(profile):
        return []
    if int(host.get("reserved_gate_cores") or 0) <= 0:
        return ["gate reserve: n/a (this host reserves no gate cores)"]
    if int(host.get("reserved_gate_mem_mb") or 0) <= 0:
        return ["gate reserve: memory axis n/a (this host reports no gate memory reserve)"]
    return []


def finding_lines(rows: List[Dict[str, Any]]) -> List[str]:
    return [f"gate_reserve_overcommitted lane={r['lane']} axis={r['axis']} "
            f"demand={r['demand']} reserve={r['reserve']}" for r in rows if r["over"] > 0]


def ratchet(installed: Optional[Dict[str, Any]], target: Dict[str, Any],
            host: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(target rows, refusals). A refusal is a (lane, axis) the target makes worse.

    With no installed profile (a first install), nothing is refused: there is
    nothing to ratchet against, and the findings still report.
    """
    rows = fit(target, host)
    if installed is None:
        return rows, []
    before = {(r["lane"], r["axis"]): r["over"] for r in fit(installed, host)}
    refusals = [f"gate_reserve_worse lane={r['lane']} axis={r['axis']} "
                f"installed_over={before.get((r['lane'], r['axis']), 0)} "
                f"target_over={r['over']} reserve={r['reserve']}"
                for r in rows if r["over"] > before.get((r["lane"], r["axis"]), 0)]
    return rows, refusals


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    share = sub.add_parser("share-cores", help="print the per-slot VM cores of a "
                           "gate-reserve-sized lane on this host")
    share.add_argument("--slots", type=int, required=True)
    share.add_argument("--host-json", help="a host-profile JSON file instead of this "
                       "host's live profile")
    args = parser.parse_args(argv)
    if args.slots < 1:
        parser.error("--slots must be at least 1")
    if args.host_json:
        with open(args.host_json, encoding="utf-8") as fh:
            host = json.load(fh)
    else:
        import host_profile  # noqa: PLC0415 - the live profile only when asked for
        host = host_profile.build_profile()
    print(share_cores(host, args.slots))
    return 0


if __name__ == "__main__":
    sys.exit(main())
