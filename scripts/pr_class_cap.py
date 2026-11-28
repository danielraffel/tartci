#!/usr/bin/env python3
"""Cap a repository's PR-class VMs on one host while its merge group is queued.

A merge-group job lands up to five pull requests, and a running VM cannot be
preempted. So the only lever for getting merge-group work onto a host sooner is
to stop starting new PR-class VMs there. With a lane's
`pr_class_cap_while_merge_group_queued = N` (rendered as
TARTCI_PR_CLASS_CAP_WHILE_MG), a lane about to claim a slot for a PR-class tier
asks this module first. It answers `cap` only when ALL of these hold:

  * the merge-group tier's own scan detail file (scripts/slot_demand.py, the
    same scan that tier's selection used) is readable, no older than
    --detail-max-age-secs, and counted merge_group jobs;
  * the oldest of those jobs was queued no longer than --max-wait-secs ago, so
    a run that can never run (docs/stale-merge-group-demand.md) cannot cap
    forever;
  * the host already holds N or more live VM leases whose labels include a
    PR-class tier label of this lane's repository.

Anything else answers `claim`, including every failure: an unreadable or stale
detail file, an unreadable lease store, a malformed argument. The cap is only
ever a reason not to START a VM; it never touches a lease, a VM or a
reservation, and it makes no GitHub call.

Output is one line: `cap|claim key=value ...` (the fields the runner logs).
Compatible with the hosts' Python 3.9.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import leases  # noqa: E402
import slot_demand  # noqa: E402

DEFAULT_DETAIL_MAX_AGE_SECS = 300
# A merge-group job queued this long is more likely a wedged run than demand a
# slot should be held for; the cap lets go and the lane claims first-come.
DEFAULT_MAX_WAIT_SECS = 3600


def merge_group_demand(detail: Optional[Dict[str, Any]], now: float,
                       max_wait_secs: int) -> Tuple[int, Optional[str], str]:
    """(count, oldest queued_at, reason) of merge-group demand worth capping for.

    A count of 0 carries the reason it is 0: no readable detail, no merge-group
    row, or an oldest job past the bound.
    """
    if not detail:
        return 0, None, "demand_unreadable"
    row = (detail.get("classes") or {}).get(slot_demand.MERGE_GROUP)
    if not isinstance(row, dict):
        return 0, None, "no_merge_group_queued"
    try:
        count = int(row.get("count") or 0)
    except (TypeError, ValueError):
        return 0, None, "demand_unreadable"
    if count <= 0:
        return 0, None, "no_merge_group_queued"
    oldest = row.get("oldest_created_at")
    stamp = slot_demand.parse_ts(oldest)
    if stamp is None:
        # Without an age the bound cannot be applied; do not cap on it.
        return 0, None, "demand_age_unknown"
    if max_wait_secs > 0 and now - stamp.timestamp() > max_wait_secs:
        return 0, str(oldest), "demand_stale"
    return count, str(oldest), "merge_group_queued"


def running_pr_class(records: List[Dict[str, Any]], pr_labels: List[str]) -> int:
    """Live VM leases whose labels include one of this repository's PR-class tier labels."""
    wanted = {label for label in pr_labels if label}
    total = 0
    for record in records:
        if not leases.is_vm_kind(record.get("command_kind")) or record.get("memory_only"):
            continue
        have = {part.strip() for part in str(record.get("label") or "").split(",")}
        if have & wanted:
            total += 1
    return total


def decide(args: argparse.Namespace, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    out: Dict[str, Any] = {"decision": "claim", "repo": args.repo, "host": args.host,
                           "cap": args.cap}
    if args.cap < 1:
        out["reason"] = "cap_off"
        return out
    pr_labels = [part.strip() for part in str(args.pr_labels).split(",") if part.strip()]
    if not pr_labels:
        out["reason"] = "no_pr_class_labels"
        return out
    detail = slot_demand.read_detail(args.mg_detail_file, args.detail_max_age_secs)
    count, oldest, reason = merge_group_demand(detail, now, args.max_wait_secs)
    out["mg_queued"] = count
    out["mg_oldest_queued_at"] = oldest
    if count <= 0:
        out["reason"] = reason
        return out
    try:
        store_dir = pathlib.Path(args.store_dir).expanduser()
        stale_secs = int(leases.parse_args(["status"]).stale_secs)
        active, _, _ = leases.reclaim(leases.load_records(store_dir), stale_secs)
    except (OSError, ValueError, SystemExit):
        out["reason"] = "lease_store_unreadable"
        return out
    running = running_pr_class(active, pr_labels)
    out["running"] = running
    if running < args.cap:
        out["reason"] = "under_cap"
        return out
    out["decision"] = "cap"
    out["reason"] = "merge_group_queued"
    return out


def render(result: Dict[str, Any]) -> str:
    fields = []
    for key in ("reason", "repo", "host", "cap", "running", "mg_queued", "mg_oldest_queued_at"):
        value = result.get(key)
        if value is None or value == "":
            continue
        fields.append(f"{key}={str(value).replace(' ', '_')}")
    return " ".join([str(result["decision"])] + fields)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default="")
    parser.add_argument("--host", default="")
    parser.add_argument("--cap", type=int, default=0,
                        help="PR-class VMs per host while merge-group work is queued (0 = off)")
    parser.add_argument("--pr-labels", default="",
                        help="comma-separated PR-class tier labels of this lane's repository")
    parser.add_argument("--mg-detail-file", default="",
                        help="the merge-group tier's slot-demand detail file")
    parser.add_argument("--store-dir", default=str(leases.default_store_dir()))
    parser.add_argument("--detail-max-age-secs", type=int, default=DEFAULT_DETAIL_MAX_AGE_SECS)
    parser.add_argument("--max-wait-secs", type=int, default=DEFAULT_MAX_WAIT_SECS)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args = parse_args(argv)
        result = decide(args)
    except (Exception, SystemExit) as exc:  # fail open: a broken cap must never hold a lane
        result = {"decision": "claim", "reason": f"helper_failed:{type(exc).__name__}"}
        args = None
    print(json.dumps(result, sort_keys=True) if args is not None and args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
