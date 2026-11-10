#!/usr/bin/env python3
"""Rank the lanes on one host that want the next free macOS VM slot.

A host runs at most two macOS guests (Apple's limit), and every lane on it
(pulp-gate, pulp-gate-slot2, vellum-gate, ...) claims a slot first-come at the
top of its loop (providers/tart-macos/macos-vm-cap.lib.sh). A lane that has just
torn its VM down loops straight back to a claim, so the lanes that are already
running keep winning, whatever their next job is worth. A merge-group job can
land up to five pull requests; a PR-head job only reports on one.

With `[leases] slot_priority` on, a lane that would claim a free slot first
records an intent in the host's lease store and asks who should have it:

  * with `slot_priority_release_first`, a lane about to serve a tagged
    release class (`pulp-release-tagged`) ranks above everything else: a
    release smoke is short and the release cannot publish until it runs;
  * merge_group jobs of any repository rank above other events (push,
    release, schedule, ...), which rank above pull_request jobs;
  * within a class the job that has waited longest ranks first, across
    repositories, so one repository cannot starve another;
  * a job of a lower class that has waited `slot_priority_promote_after_secs`
    (default 2700) ranks as merge-group, so no class starves for good either;
  * ties fall to whoever started waiting first, then the lane id.

A lane defers only while at least as many intents ranked strictly ahead of it
as there are free slots could take one now: each such intent is live (owner
process alive with its exact start time, refreshed within waiter_fresh_secs)
and its VM lease would be admitted on the store's current core and memory
budget, counting the intents ahead of it as granted. So nothing new is
admitted, the cap and the budgets are untouched, and a lane never waits behind
one that could not boot anyway. Priority is non-preemptive: this only decides
who takes a slot that is FREE. It never reads or writes a lease, a VM or a
reservation, and never stops anything that is running.

Modes: `off` (the default) touches nothing. `shadow` records intents and
reports the lane that would have been given the slot, but always answers
`claim`, so behaviour is first-come exactly as before. `on` answers `defer`.

The facts an intent carries (event class, oldest queued `created_at`) come
from the scan the lane already made (scripts/slot_demand.py); this module
makes no GitHub call.

Compatible with the hosts' Python 3.9.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - tartci hosts are POSIX
    fcntl = None  # type: ignore[assignment]

import host_profile
import leases
import slot_demand

MODES = ("off", "shadow", "on")
# A lane serving a tagged release ranks ahead of merge-group. Its rank is below
# zero so the starvation promotion (a lower class that has waited long enough
# ranks as merge-group, 0) never lifts anything past a release.
RELEASE_CLASS = "release"
CLASS_RANK = {RELEASE_CLASS: -1, slot_demand.MERGE_GROUP: 0, slot_demand.OTHER: 1,
              slot_demand.PULL_REQUEST: 2}
UNKNOWN_CLASS = "unknown"
# Runner labels that only a tagged-release job carries (the event-class-v2
# release class, scripts/macos_fleet_lanes.py V2_EXTRA_CLASS_WORKFLOWS). A lane
# whose selected registration carries one can only be handed a release job, so
# the label is the evidence; the run's own event is `push` of a tag.
RELEASE_CLASS_LABELS = ("pulp-release-tagged",)
DEFAULT_PROMOTE_AFTER_SECS = 2700
DEFAULT_DETAIL_MAX_AGE_SECS = 300
# An intent outlives one loop pass: POLL plus a scan that may wait up to 120 s
# for the observation lock. Lanes withdraw explicitly whenever they stop
# wanting a slot, and a dead owner never counts.
DEFAULT_INTENT_FRESH_SECS = 240
DEFAULT_LOCK_TIMEOUT_SECS = 10.0


def intents_file(store_dir: pathlib.Path) -> pathlib.Path:
    return store_dir / "slot-intents.json"


def load_intents(store_dir: pathlib.Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    path = intents_file(store_dir)
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "[]")
    except (OSError, ValueError):
        return [], [f"slot_intents_unreadable:{path}"]
    if not isinstance(data, list):
        return [], [f"slot_intents_unreadable:{path}"]
    return [row for row in data if isinstance(row, dict)], []


def write_intents(store_dir: pathlib.Path, intents: List[Dict[str, Any]]) -> None:
    path = intents_file(store_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(intents, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def live_intents(
    intents: List[Dict[str, Any]], fresh_secs: int
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(live, expired): the same liveness rule as VM lease waiters."""
    return leases.live_waiters(intents, fresh_secs)


@contextlib.contextmanager
def store_lock(store_dir: pathlib.Path, timeout: float) -> Iterator[bool]:
    """The lease store's own lock, but bounded: yields False when not got in time.

    `leases.locked_store` blocks without limit, which is right for a lease but
    not here: a ranker that cannot get the lock claims first-come instead of
    holding the lane loop.
    """
    if fcntl is None:  # pragma: no cover - tartci hosts are POSIX
        with leases.locked_store(store_dir):
            yield True
        return
    store_dir.mkdir(parents=True, exist_ok=True)
    with (store_dir / "leases.lock").open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def mode_setting(override: Optional[str] = None) -> str:
    if override in MODES:
        return str(override)
    raw = os.environ.get("TARTCI_SLOT_PRIORITY")
    if raw is not None and raw.strip() in MODES:
        return raw.strip()
    value = host_profile.lease_policy_settings().get("slot_priority", "off")
    return value if value in MODES else "off"


def promote_after_setting(override: Optional[int] = None) -> int:
    if override is not None and override >= 0:
        return int(override)
    value = host_profile.lease_policy_settings().get(
        "slot_priority_promote_after_secs", DEFAULT_PROMOTE_AFTER_SECS
    )
    return int(value) if isinstance(value, int) and value >= 0 else DEFAULT_PROMOTE_AFTER_SECS


def release_first_setting(override: Optional[bool] = None) -> bool:
    if override is not None:
        return bool(override)
    raw = os.environ.get("TARTCI_SLOT_PRIORITY_RELEASE_FIRST")
    if raw is not None and raw.strip() in ("0", "1"):
        return raw.strip() == "1"
    return host_profile.lease_policy_settings().get("slot_priority_release_first") is True


def carries_release_label(labels: Any) -> bool:
    """True when a registration's labels name a tagged-release class."""
    names = {part.strip() for part in str(labels or "").split(",")}
    return any(label in names for label in RELEASE_CLASS_LABELS)


def ranked_class(event_class: str, labels: Any, release_first: bool) -> str:
    """The class a lane is ranked as.

    With release-first on, a lane whose registration can only take a tagged
    release job ranks as `release`, but only on positive evidence that such a
    job is queued (a known class from its scan): `unknown` stays `unknown`.
    With it off, an explicit `release` is ranked as `other`, as before.
    """
    if not release_first:
        return slot_demand.OTHER if event_class == RELEASE_CLASS else event_class
    if event_class != UNKNOWN_CLASS and carries_release_label(labels):
        return RELEASE_CLASS
    return event_class


def _epoch(value: Any) -> Optional[float]:
    parsed = slot_demand.parse_ts(value)
    return parsed.timestamp() if parsed is not None else None


def demand_from_detail(detail: Optional[Dict[str, Any]], promote_after: int, now: float
                       ) -> Tuple[str, Optional[str]]:
    """(event class, queued_at) of the OLDEST job a detail file counted.

    GitHub, not tartci, picks which matching job a new runner gets, and a lane
    whose labels match jobs of several classes is likeliest to get the oldest.
    Ranking such a lane by the best class it saw would let a fresh merge-group
    job win the slot for an old PR job. A class's own promotion still lifts an
    old job. No usable detail is `unknown`, which ranks with pull_request: a
    lane only moves ahead on positive evidence.
    """
    if not detail:
        return UNKNOWN_CLASS, None
    oldest: Optional[Tuple[float, int, str, str]] = None
    for cls, row in (detail.get("classes") or {}).items():
        if not isinstance(row, dict) or int(row.get("count") or 0) <= 0:
            continue
        # A scan's detail never names `release` (that comes from the lane's
        # labels, ranked_class), so any other spelling is `other`.
        cls = cls if cls in CLASS_RANK and cls != RELEASE_CLASS else slot_demand.OTHER
        stamp = _epoch(row.get("oldest_created_at"))
        if stamp is None:
            continue
        # Equal ages: the better class (a tie cannot be decided by assignment).
        key = (stamp, CLASS_RANK[cls], cls, str(row.get("oldest_created_at")))
        if oldest is None or key < oldest:
            oldest = key
    if oldest is None:
        return UNKNOWN_CLASS, None
    return oldest[2], oldest[3]


def effective_rank(cls: str, queued_epoch: Optional[float], promote_after: int,
                   now: float) -> int:
    rank = CLASS_RANK.get(cls, CLASS_RANK[slot_demand.PULL_REQUEST])
    if (
        rank > 0
        and promote_after > 0
        and queued_epoch is not None
        and now - queued_epoch >= promote_after
    ):
        return 0
    return rank


def rank_key(intent: Dict[str, Any], promote_after: int, now: float) -> Tuple[Any, ...]:
    """Smaller is better: class, then the longest wait, then when the lane began waiting.

    The wait is the older of the job's queued_at and the lane's waiting_since
    (when it began wanting a slot without a break). A scan that stopped at its
    first witness reports that witness's age, a lower bound; waiting_since keeps
    a lane that has been refused for a long time from looking young just
    because a newer job is the one its scan happened to see, so it reaches the
    promotion bound and cannot starve.
    """
    since = _epoch(intent.get("waiting_since"))
    queued = _epoch(intent.get("queued_at"))
    if queued is None or (since is not None and since < queued):
        queued = since if since is not None else (queued if queued is not None else now)
    rank = effective_rank(str(intent.get("event_class") or UNKNOWN_CLASS), queued,
                          promote_after, now)
    return (rank, queued, since if since is not None else now, str(intent.get("lane") or ""))


def intent_summary(intent: Dict[str, Any], promote_after: int, now: float) -> Dict[str, Any]:
    key = rank_key(intent, promote_after, now)
    return {
        "lane": intent.get("lane"),
        "repo": intent.get("repo"),
        "event_class": intent.get("event_class"),
        "effective_rank": key[0],
        "queued_at": intent.get("queued_at"),
        "waiting_since": intent.get("waiting_since"),
        "priority": leases.record_int(intent, "priority"),
        "cores": leases.record_int(intent, "lease_size_cores"),
    }


def ranked_ahead(
    cfg: Dict[str, int],
    records: List[Dict[str, Any]],
    live: List[Dict[str, Any]],
    me: Dict[str, Any],
    promote_after: int,
    now: float,
    waiters: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Intents ranked strictly ahead of `me` that could take a slot now.

    Each counts only if its VM lease is admitted on the current store with the
    intents ahead of it already granted, so the list never names more lanes
    than the budget could boot at once. With ranked VM lease waiters on, the
    store would also defer an acquire that displaces a higher-priority live
    waiter (leases.blocking_waiter); such an intent cannot boot either, so it
    holds nothing back. This lane's own waiter is left out: a lane that defers
    here withdraws it (slot-priority.lib.sh).
    """
    my_key = rank_key(me, promote_after, now)
    simulated = list(records)
    ahead: List[Dict[str, Any]] = []
    others = [row for row in live if str(row.get("lane")) != str(me.get("lane"))]
    for row in sorted(others, key=lambda r: rank_key(r, promote_after, now)):
        if rank_key(row, promote_after, now) >= my_key:
            break
        priority = leases.record_int(row, "priority")
        cores = leases.record_int(row, "lease_size_cores")
        mem = leases.record_int(row, "lease_size_mem_mb")
        if not leases.admits(cfg, simulated, priority, cores, mem):
            continue
        after = simulated + [{"lease_size_cores": cores, "lease_size_mem_mb": mem,
                              "priority": priority}]
        others_waiting = [
            w for w in (waiters or [])
            if str(w.get("id")) not in (f"waiter-{row.get('lane')}", f"waiter-{me.get('lane')}")
        ]
        if others_waiting and leases.blocking_waiter(
            cfg, others_waiting, simulated, after, priority, f"waiter-{row.get('lane')}"
        ) is not None:
            continue
        simulated.append({"lease_size_cores": cores, "lease_size_mem_mb": mem,
                          "priority": priority})
        ahead.append(row)
    return ahead


def decide(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    mode = mode_setting(args.mode)
    if mode == "off":
        return {"ok": True, "mode": "off", "decision": "claim", "reason": "slot_priority_off"}, 0
    store_dir = pathlib.Path(args.store_dir).expanduser()
    cfg = leases.capacity_config(args)
    promote_after = promote_after_setting(args.promote_after_secs)
    fresh = int(args.fresh_secs) if args.fresh_secs else max(
        DEFAULT_INTENT_FRESH_SECS, int(cfg["waiter_fresh_secs"]))
    nowdt = slot_demand._utcnow()
    now = nowdt.timestamp()
    witness_only = False
    if args.event_class:
        event_class = args.event_class
        queued_at = args.queued_at or None
    else:
        detail = slot_demand.read_detail(args.demand_file or "", args.detail_max_age_secs)
        event_class, queued_at = demand_from_detail(detail, promote_after, now)
        witness_only = bool(detail and detail.get("witness_only"))
    release_first = release_first_setting(
        None if args.release_first is None else args.release_first == "1")
    event_class = ranked_class(event_class, args.labels, release_first)
    priority, priority_class = leases.parse_priority(args.priority)
    cores = max(0, int(args.cores))
    mem = int(args.mem_mb) if args.mem_mb is not None else cores * cfg["per_job_mem_mb"]
    identity = leases.process_identity(int(args.pid) if args.pid else os.getpid())
    if not identity["process_start_time"]:
        return {"ok": True, "mode": mode, "decision": "claim",
                "reason": "intent_owner_not_alive"}, 0
    stamp = slot_demand.iso(nowdt)
    with store_lock(store_dir, args.lock_timeout_secs) as got:
        if not got:
            return {"ok": True, "mode": mode, "decision": "claim",
                    "reason": "store_lock_timeout"}, 0
        intents, problems = load_intents(store_dir)
        live, _ = live_intents(intents, fresh)
        previous = next((row for row in live if str(row.get("lane")) == args.lane), None)
        same_owner = previous is not None and (
            previous.get("pid") == identity["pid"]
            and previous.get("process_start_time") == identity["process_start_time"]
        )
        me = {
            "lane": args.lane,
            "repo": args.repo,
            "labels": args.labels,
            "event_class": event_class,
            "queued_at": queued_at,
            "queued_at_lower_bound": witness_only,
            "lease_size_cores": cores,
            "lease_size_mem_mb": mem,
            "priority": priority,
            "priority_class": priority_class,
            "pid": identity["pid"],
            "process_start_time": identity["process_start_time"],
            "host_boot_time": identity["host_boot_time"],
            "waiting_since": previous.get("waiting_since") if same_owner and previous else stamp,
            "seen_at": stamp,
        }
        kept = [row for row in live if str(row.get("lane")) != args.lane]
        kept.append(me)
        write_intents(store_dir, kept)
        result: Dict[str, Any] = {
            "ok": True,
            "mode": mode,
            "self": intent_summary(me, promote_after, now),
            "free_slots": int(args.free_slots),
            "problems": sorted(set(problems)),
        }
        if int(args.free_slots) <= 0:
            result.update(decision="full", reason="no_free_slot")
            return result, 0
        records = leases.load_records(store_dir)
        waiters: List[Dict[str, Any]] = []
        if cfg.get("rank_vm_waiters"):
            loaded, _ = leases.load_waiters(store_dir)
            waiters, _ = leases.live_waiters(loaded, int(cfg["waiter_fresh_secs"]))
        ahead = ranked_ahead(cfg, records, kept, me, promote_after, now, waiters)
    result["ahead"] = [intent_summary(row, promote_after, now) for row in ahead]
    if len(ahead) < int(args.free_slots):
        result.update(decision="claim", reason="ranked_first")
        return result, 0
    winner = intent_summary(ahead[0], promote_after, now)
    result["winner"] = winner
    if mode == "shadow":
        result.update(decision="claim", reason="shadow_would_defer", shadow_winner=winner)
        return result, 0
    result.update(decision="defer", reason="outranked")
    return result, 0


def withdraw(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    store_dir = pathlib.Path(args.store_dir).expanduser()
    if not intents_file(store_dir).exists():
        return {"ok": True, "withdrawn": None}, 0
    with store_lock(store_dir, args.lock_timeout_secs) as got:
        if not got:
            return {"ok": False, "withdrawn": None, "reason": "store_lock_timeout"}, 0
        intents, problems = load_intents(store_dir)
        kept = [row for row in intents if str(row.get("lane")) != args.lane]
        removed = len(kept) != len(intents)
        if removed:
            write_intents(store_dir, kept)
    return {"ok": True, "withdrawn": args.lane if removed else None,
            "problems": sorted(set(problems))}, 0


def status(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    store_dir = pathlib.Path(args.store_dir).expanduser()
    cfg = leases.capacity_config(args)
    promote_after = promote_after_setting(args.promote_after_secs)
    fresh = int(args.fresh_secs) if args.fresh_secs else max(
        DEFAULT_INTENT_FRESH_SECS, int(cfg["waiter_fresh_secs"]))
    now = slot_demand._utcnow().timestamp()
    intents, problems = load_intents(store_dir)
    live, _ = live_intents(intents, fresh)
    ordered = sorted(live, key=lambda r: rank_key(r, promote_after, now))
    return {"ok": True, "mode": mode_setting(args.mode),
            "intents": [intent_summary(row, promote_after, now) for row in ordered],
            "problems": sorted(set(problems))}, 0


def line(result: Dict[str, Any]) -> str:
    """One shell-friendly line: `<decision> key=value ...` (no spaces in values)."""
    def v(value: Any) -> str:
        text = "unknown" if value in (None, "") else str(value)
        return text.replace(" ", "_")

    parts = [v(result.get("decision", "claim")), f"mode={v(result.get('mode'))}",
             f"reason={v(result.get('reason'))}"]
    me = result.get("self") or {}
    parts += [f"class={v(me.get('event_class'))}", f"rank={v(me.get('effective_rank'))}",
              f"queued_at={v(me.get('queued_at'))}", f"free={v(result.get('free_slots'))}",
              f"ahead={len(result.get('ahead') or [])}"]
    winner = result.get("winner") or {}
    if winner:
        parts += [f"winner_lane={v(winner.get('lane'))}", f"winner_repo={v(winner.get('repo'))}",
                  f"winner_class={v(winner.get('event_class'))}",
                  f"winner_rank={v(winner.get('effective_rank'))}",
                  f"winner_queued_at={v(winner.get('queued_at'))}"]
    return " ".join(parts)


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="slot_priority")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--store-dir", default=str(leases.default_store_dir()))
        p.add_argument("--mode", choices=MODES)
        p.add_argument("--promote-after-secs", type=int)
        p.add_argument("--fresh-secs", type=int)
        # Budget overrides, as on `leases.py` (tests and dry runs).
        p.add_argument("--capacity", type=int)
        p.add_argument("--capacity-mem-mb", type=int)
        p.add_argument("--reserved-gate-cores", type=int)
        p.add_argument("--lock-timeout-secs", type=float, default=DEFAULT_LOCK_TIMEOUT_SECS)
        p.add_argument("--json", action="store_true")

    d = sub.add_parser("decide", help="record this lane's intent and say whether it may claim")
    common(d)
    d.add_argument("--lane", required=True)
    d.add_argument("--repo", default="")
    d.add_argument("--labels", default="")
    d.add_argument("--free-slots", type=int, required=True)
    d.add_argument("--cores", type=int, required=True)
    d.add_argument("--mem-mb", type=int)
    d.add_argument("--priority", default="vm")
    d.add_argument("--pid", type=int)
    d.add_argument("--demand-file", default="")
    d.add_argument("--detail-max-age-secs", type=int, default=DEFAULT_DETAIL_MAX_AGE_SECS)
    d.add_argument("--event-class", choices=sorted(CLASS_RANK) + [UNKNOWN_CLASS])
    d.add_argument("--queued-at", default="")
    # Overrides [leases] slot_priority_release_first (tests and dry runs).
    d.add_argument("--release-first", choices=("0", "1"))

    w = sub.add_parser("withdraw", help="remove this lane's intent")
    common(w)
    w.add_argument("--lane", required=True)

    s = sub.add_parser("status", help="list live intents in rank order")
    common(s)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "decide":
            result, rc = decide(args)
        elif args.command == "withdraw":
            result, rc = withdraw(args)
        else:
            result, rc = status(args)
    except Exception as exc:  # noqa: BLE001 - fail open: a broken ranker claims first-come
        result, rc = {"ok": False, "decision": "claim", "reason": "error",
                      "error": str(exc)}, 0
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif args.command == "decide" or not result.get("ok"):
        print(line(result))
    else:
        print(json.dumps(result, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
