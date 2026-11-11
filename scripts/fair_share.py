#!/usr/bin/env python3
"""Fair share between repositories for the next macOS VM on one host.

Every VM lane leases at the priority its repository was given: Pulp's required
gate derives 100 (PR head) or 110 (merge group), Vellum leases at "vm" (60).
Admission and waiter ranking both follow that priority, so while Pulp has a
steady queue its lanes take every VM a host can run, and a lower-priority
repository waits for as long as that queue lasts. On 2026-11-10 Vellum's
"Locked macOS GPU proof" waited four hours on four hosts this way.

With `[leases] fair_share_after_secs = N` (0, the default, turns it off) a VM
lane that has had queued work it could not place for N seconds is *starved*,
and a starved lane is ranked ahead of the other repositories on its host:

  * its lease is admitted at gate priority, against the host budget the gate
    lanes use rather than the non-gate share, so it can take the cores a
    finishing gate VM frees;
  * a lane of ANOTHER repository that would take the capacity the starved lane
    needs (its cores and memory, or the last free VM slot) yields to it, at
    lease_fit (before it scans or asks Shipyard) and at acquire;
  * among starved lanes of different repositories, the one starved longest
    goes first.

Nothing changes within a repository: Pulp's merge-group-above-PR order, the
priorities lanes lease at and rank_vm_waiters all stay as they are. A lane is
promoted only while another repository holds or waits for a VM on the host,
so a lane alone on a host is admitted exactly as before. The clock starts at
the older of two moments: when the lane first saw queued work it could not
place, and when its oldest matching queued job was created (read from the
slot-demand detail file its scan already writes; no GitHub call). The lane's
own part resets when it is granted a VM or sees its queue empty, but the job
age carries across a grant: a lane granted one VM while its backlog is still
older than N stays starved for the next one, instead of waiting N again
(m5studio, 2026-11-11: one Vellum VM per 30 minutes while a merge-group job
waited over an hour). Every rule here is work-conserving: a starved lane that
could not fit anyway blocks nobody.

CLI: `fair_share.py oldest-queued <detail-file>` prints the oldest queued
job's created_at as epoch seconds, or nothing when the file is missing,
stale or names no job.
"""
from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional

DEFAULT_AFTER_SECS = 0
MIN_AFTER_SECS = 300
MAX_AFTER_SECS = 14400


def is_vm_kind(value: Any) -> bool:
    return str(value or "").endswith("-vm")


def enabled(cfg: Dict[str, Any]) -> bool:
    return int(cfg.get("fair_share_after_secs", 0) or 0) > 0


def demand_since(raw: Any) -> int:
    """A lane's demand clock as epoch seconds; 0 when absent or malformed."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def starved(cfg: Dict[str, Any], since: Any, now: Optional[float] = None) -> bool:
    after = int(cfg.get("fair_share_after_secs", 0) or 0)
    since = demand_since(since)
    if after <= 0 or since <= 0:
        return False
    now = time.time() if now is None else now
    return now - since >= after


def other_repo(a: Any, b: Any) -> bool:
    """Both repositories are known and differ. Unknown never counts as other."""
    return bool(a) and bool(b) and str(a) != str(b)


def contended(
    repo: str, active: List[Dict[str, Any]], waiters: List[Dict[str, Any]]
) -> bool:
    """Another repository holds a VM lease or waits for one on this host."""
    rows = [row for row in active if is_vm_kind(row.get("command_kind"))]
    rows += [row for row in waiters if is_vm_kind(row.get("command_kind"))]
    return any(other_repo(row.get("repo"), repo) for row in rows)


def promoted_priority(
    cfg: Dict[str, Any],
    priority: int,
    *,
    repo: str,
    since: Any,
    active: List[Dict[str, Any]],
    waiters: List[Dict[str, Any]],
    now: Optional[float] = None,
) -> int:
    """The priority a VM lease is admitted at: gate priority when starved and
    contended, else unchanged."""
    if not enabled(cfg) or not starved(cfg, since, now):
        return priority
    if not contended(repo, active, waiters):
        return priority
    return max(priority, int(cfg["gate_priority"]))


def starved_rivals(
    cfg: Dict[str, Any],
    waiters: List[Dict[str, Any]],
    *,
    repo: str,
    since: Any,
    self_waiter_id: str = "",
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Live VM waiters of other repositories this request must yield to.

    A waiter outranks the request when it is starved and the request is not,
    or when both are starved and it has been starved longer. Oldest first.
    """
    if not enabled(cfg) or not repo:
        return []
    now = time.time() if now is None else now
    mine = demand_since(since) if starved(cfg, since, now) else 0
    rivals = []
    for waiter in waiters:
        if self_waiter_id and str(waiter.get("id")) == self_waiter_id:
            continue
        if not is_vm_kind(waiter.get("command_kind")):
            continue
        if not other_repo(waiter.get("repo"), repo):
            continue
        theirs = demand_since(waiter.get("demand_since"))
        if not starved(cfg, theirs, now):
            continue
        if mine and theirs >= mine:
            continue
        rivals.append(waiter)
    return sorted(rivals, key=lambda row: (demand_since(row.get("demand_since")),
                                           str(row.get("id") or "")))


def vm_slot_taken(
    active: List[Dict[str, Any]], rivals_ahead: int, vm_slot_cap: int
) -> bool:
    """This request would take a VM slot one of `rivals_ahead` needs."""
    if vm_slot_cap <= 0 or rivals_ahead <= 0:
        return False
    running = sum(1 for row in active if is_vm_kind(row.get("command_kind")))
    return vm_slot_cap - running <= rivals_ahead


def as_waiter_lease(cfg: Dict[str, Any], waiter: Dict[str, Any]) -> Dict[str, Any]:
    """The lease a starved waiter would hold: its size at gate priority."""
    return {
        "id": str(waiter.get("id") or ""),
        "lease_size_cores": int(waiter.get("lease_size_cores") or 0),
        "lease_size_mem_mb": int(waiter.get("lease_size_mem_mb") or 0),
        "priority": max(int(waiter.get("priority") or 0), int(cfg["gate_priority"])),
        "command_kind": waiter.get("command_kind"),
        "repo": waiter.get("repo"),
    }


DETAIL_MAX_AGE_SECS = 300


def oldest_queued_epoch(detail: Optional[Dict[str, Any]]) -> int:
    """Epoch seconds of the oldest job across a detail file's classes; 0 if none."""
    import slot_demand

    if not isinstance(detail, dict):
        return 0
    oldest = 0
    for row in (detail.get("classes") or {}).values():
        if not isinstance(row, dict) or not int(row.get("count") or 0):
            continue
        stamp = slot_demand.parse_ts(row.get("oldest_created_at"))
        if stamp is None:
            continue
        epoch = int(stamp.timestamp())
        if epoch > 0 and (oldest == 0 or epoch < oldest):
            oldest = epoch
    return oldest


def main(argv: List[str]) -> int:
    if len(argv) == 2 and argv[0] == "oldest-queued":
        import slot_demand

        epoch = oldest_queued_epoch(slot_demand.read_detail(argv[1], DETAIL_MAX_AGE_SECS))
        if epoch:
            print(epoch)
        return 0
    print("usage: fair_share.py oldest-queued <detail-file>", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    raise SystemExit(main(sys.argv[1:]))
