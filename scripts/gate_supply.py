#!/usr/bin/env python3
"""Free, leasable gate slots on this host, and the fallback-lane decision.

A lane configured as a *fallback* (profile key `fallback_preferred_hosts`)
leaves young queued jobs to its preferred hosts. The time-based rule it
replaces (`min_queued_age_seconds`) waits a fixed interval whatever the
preferred hosts are doing, so the fallback slot idles while a job waits on
hosts that cannot take it. This module answers the question that rule was
standing in for: can a preferred host take this job NOW?

`report` runs on a preferred host (read-only, reached over SSH as
`tartci pool supply --json`) and says, for one repository and one event class:

  free       lanes serving that class whose supervisor is idle and fresh, capped
             by the host's free macOS VM slots (Apple's 2-guest limit, the
             GUI cap and live reservations) and by how many gate VM leases of
             that lane's size the host lease store can admit right now. A lane
             whose lease can never fit (m5's second lane beside a running
             12-core VM in a 14-core universe) therefore contributes nothing.
  in_flight  lanes already between admission and assignment. A lane whose
             class is not yet visible in its heartbeat counts as in flight for
             this class: over-counting coverage can only make the fallback
             wait, which is the existing behaviour.
  verdict    `ok`, or `unknown` with a reason. Anything that cannot be read
             (launchd, a heartbeat, the lease store, an unrecognised phase) is
             unknown, never zero. An unreadable Tart inventory follows the
             host's own slot claim instead: reservations are the occupancy.

`decide` runs on the fallback host. It fetches every preferred host's report,
reads this host's own report for sibling lanes, and prints one line:

  grant <detail>    queued demand exceeds what the preferred hosts and this
                    host's sibling lanes already cover: boot now
  hold <detail>     the preferred hosts can cover the demand
  unknown <detail>  a preferred host could not be read, or its report is stale

The caller treats `unknown` exactly like `hold`: the lane keeps its
`min_queued_age_seconds` rule, which stays the upper bound on the delay in
every case. The fallback can only make the lane act earlier, never later.

Sibling lanes are ordered by slot so two fallback supervisors on one host do
not both boot for one job: a free sibling with a lower slot number covers one
unit of demand before this lane may take it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_lane_discovery  # noqa: E402
import lane_busy  # noqa: E402

SCHEMA = "tartci.gate-supply/v1"

# runner.sh heartbeat phases, split by what they mean for serving a NEW job.
# Idle and able to boot on its next poll.
FREE_PHASES = frozenset({"waiting", "loop", "backoff", "warm-parking", "warm-parked"})
# Past admission, not yet assigned: this lane is already reaching for a job.
IN_FLIGHT_PHASES = frozenset({
    "admission-precheck", "booting", "ensuring-runner", "aqua-preflight",
    "chrome-preflight", "admission-check", "minting-jit", "idle-wait",
    "idle-retarget-check", "warm-handoff",
})
# Holding a job, or refused / blind / draining: cannot take a new job now.
BLOCKED_PHASES = frozenset({
    "job-running", "cancel-pending-terminal", "admission-deferred",
    "admission-error", "yielding", "draining", "stopped", "scan_blind",
    "scan_blind_escalated", "jit-admission-denied", "vm-lease-denied",
    "vm-lease-infeasible", "admission-precheck-deferred",
    "admission-precheck-error", "teardown-pending",
})
# A heartbeat older than this many polls (floor below) is not current.
HEARTBEAT_STALE_POLLS = 6
HEARTBEAT_STALE_FLOOR = 120
DEFAULT_POLL = 20
DEFAULT_VM_CAP = 2
HARD_MAX_VMS = 2
RESERVATION_TTL = 7200


def _utc_age(ts: Any, now: float) -> float | None:
    try:
        stamp = dt.datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return now - stamp.replace(tzinfo=dt.timezone.utc).timestamp()


def tier_classes(env: dict) -> list[str]:
    raw = env.get("TARTCI_RUNNER_WORKFLOW_TIERS") or ""
    return [line.split("|", 1)[0].strip() for line in raw.splitlines()
            if "|" in line and line.split("|", 1)[0].strip()]


def derived_vm_mem_mb(cores: int, per_job_mb: int = 1536,
                      floor: int = 8192, ceiling: int = 16384) -> int:
    """Mirror of vm-lease.lib.sh tartci_vm_lease_derived_mem_mb.

    test_gate_supply.py runs the shell function beside this one, so the two
    cannot drift apart silently.
    """
    jobs = max(1, cores - 1)
    value = jobs * per_job_mb * 4 // 3
    value = max(value, floor)
    return min(value, max(ceiling, floor))


def lane_request(env: dict, default_cores: int) -> tuple[int, int]:
    """(cores, mem_mb) one VM of this lane leases."""
    cores_text = str(env.get("TARTCI_MACOS_VM_CORES") or "")
    cores = int(cores_text) if cores_text.isdigit() and int(cores_text) > 0 else default_cores
    mem_text = str(env.get("TARTCI_MACOS_VM_MEM_MB") or "")
    mem = int(mem_text) if mem_text.isdigit() and int(mem_text) > 0 else derived_vm_mem_mb(cores)
    return cores, mem


def lease_fit_count(capacity: dict[str, Any], cores: int, mem_mb: int, limit: int) -> int:
    """How many gate-priority VM leases of this size fit on top of current use.

    A gate lease is bound only by the host-wide totals (the non-gate class
    limits do not apply to it), on cores and, when the memory axis is on, on
    memory. `capacity` is leases.usage() output.
    """
    fits = 0
    used_cores = int(capacity.get("used_cores", 0))
    total_cores = int(capacity.get("total_cores", 0))
    total_mem = int(capacity.get("total_mem_mb", 0))
    used_mem = int(capacity.get("used_mem_mb", 0))
    while fits < limit:
        k = fits + 1
        if used_cores + k * cores > total_cores:
            break
        if total_mem > 0 and used_mem + k * mem_mb > total_mem:
            break
        fits = k
    return fits


def pool_state(state_file: Path, participation_file: Path) -> str:
    """Same reading as pool.lib.sh tartci_pool_read_state + participation."""
    try:
        participation = participation_file.read_text().strip() if participation_file.exists() else "1"
    except OSError:
        return "unknown"
    if participation == "0":
        return "off"
    try:
        value = state_file.read_text().strip() if state_file.exists() else "on"
    except OSError:
        return "unknown"
    return value if value in ("on", "draining", "off") else "on"


def effective_cap(cap_file: Path, default: int = DEFAULT_VM_CAP) -> int:
    cap = default
    try:
        digits = "".join(ch for ch in cap_file.read_text() if ch.isdigit())[:2]
        if digits and int(digits) >= 1:
            cap = int(digits)
    except OSError:
        pass
    return max(1, min(cap, HARD_MAX_VMS))


def live_reservations(resv_dir: Path, now: float) -> int:
    """Read-only count of macos-vm-cap.lib.sh reservations with a live owner."""
    count = 0
    try:
        entries = sorted(resv_dir.glob("resv.*"))
    except OSError:
        return 0
    for path in entries:
        try:
            pid_text, _, ts_text = path.read_text().strip().partition(" ")
        except OSError:
            continue
        if not pid_text.isdigit():
            continue
        try:
            os.kill(int(pid_text), 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        if ts_text.isdigit() and now - int(ts_text) > RESERVATION_TTL:
            continue
        count += 1
    return count


def newest_heartbeat(state_dir: Path) -> dict | None:
    newest: dict | None = None
    for path in state_dir.glob("*.state.json"):
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and (newest is None or str(value.get("ts") or "") > str(newest.get("ts") or "")):
            newest = value
    return newest


def classify_lane(env: dict, heartbeat: dict | None, class_label: str, classes: list[str],
                  now: float) -> tuple[str, str]:
    """(free|in_flight|blocked|unknown, detail) for one lane serving the class."""
    if heartbeat is None:
        return "unknown", "no heartbeat"
    poll_text = str(env.get("TARTCI_VM_POLL") or "")
    poll = int(poll_text) if poll_text.isdigit() and int(poll_text) > 0 else DEFAULT_POLL
    stale_after = max(HEARTBEAT_STALE_FLOOR, HEARTBEAT_STALE_POLLS * poll)
    age = _utc_age(heartbeat.get("ts"), now)
    phase = str(heartbeat.get("phase") or "")
    if age is None:
        return "unknown", f"heartbeat without a readable timestamp (phase {phase})"
    if age > stale_after:
        return "unknown", f"stale heartbeat ({phase}, {int(age)}s > {stale_after}s)"
    if phase in FREE_PHASES:
        return "free", f"{phase} {int(age)}s ago"
    if phase in IN_FLIGHT_PHASES:
        labels = {item.strip() for item in str(heartbeat.get("labels") or "").split(",")}
        other = [label for label in classes if label in labels and label != class_label]
        if other and class_label not in labels:
            return "blocked", f"{phase} for {other[0]}"
        return "in_flight", f"{phase} {int(age)}s ago"
    if phase in BLOCKED_PHASES:
        return "blocked", f"{phase} {int(age)}s ago"
    return "unknown", f"unrecognised phase {phase!r}"


def build_report(
    repo: str,
    class_label: str,
    *,
    now: float | None = None,
    lanes: list | None = None,
    lane_problems: list[str] | None = None,
    env_reader: Callable[[str], dict] | None = None,
    heartbeat_reader: Callable[[Path], dict | None] = newest_heartbeat,
    capacity_reader: Callable[[], dict[str, Any]] | None = None,
    running_reader: Callable[[], int | None] | None = None,
    default_cores_reader: Callable[[], int] | None = None,
    pool_reader: Callable[[], str] | None = None,
    cap_reader: Callable[[], int] | None = None,
    reservations_reader: Callable[[], int] | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Pure assembly of the report from injectable readers."""
    now = time.time() if now is None else now
    report: dict[str, Any] = {
        "schema": SCHEMA, "host": host or os.uname().nodename.split(".")[0],
        "now": int(now), "repo": repo, "class": class_label,
        "verdict": "ok", "reason": None, "free": 0, "in_flight": 0, "lanes": [],
    }

    def unknown(reason: str) -> dict[str, Any]:
        report.update({"verdict": "unknown", "reason": reason, "free": 0})
        return report

    if lanes is None:
        return unknown("; ".join(lane_problems or ["launchd unreadable"]))
    pool = (pool_reader or (lambda: "on"))()
    report["pool"] = pool
    if pool == "unknown":
        return unknown("pool state unreadable")
    free_lanes: list[tuple[int, int]] = []
    for lane in lanes:
        env = (env_reader or (lambda _label: {}))(lane.label)
        if env.get("TARTCI_RUNNER_REPO") != repo:
            continue
        classes = tier_classes(env)
        if class_label not in classes:
            continue
        slot_text = str(env.get("TARTCI_RUNNER_SLOT") or "1")
        heartbeat = heartbeat_reader(lane.state_dir) if lane.state_dir else None
        state, detail = classify_lane(env, heartbeat, class_label, classes, now)
        row = {"lane": lane.identity, "slot": int(slot_text) if slot_text.isdigit() else 1,
               "state_dir": str(lane.state_dir or ""), "state": state, "detail": detail}
        report["lanes"].append(row)
        if state == "unknown":
            return unknown(f"lane {lane.identity}: {detail}")
        if state == "in_flight":
            report["in_flight"] += 1
        elif state == "free":
            default_cores = (default_cores_reader or (lambda: 1))()
            free_lanes.append(lane_request(env, default_cores))
    if pool != "on":
        report["reason"] = f"pool {pool}"
        return report
    if not free_lanes:
        return report
    running = (running_reader or (lambda: 0))()
    cap = (cap_reader or (lambda: DEFAULT_VM_CAP))()
    reserved = (reservations_reader or (lambda: 0))()
    # The same rule the host's own slot claim applies (macos-vm-cap.lib.sh): an
    # unreadable Tart inventory is not a full host; the reservation files,
    # which every lane holds from before boot until its VM is proved gone, are
    # the occupancy source then.
    report["inventory"] = "tart" if running is not None else "reservations"
    running = running if running is not None else 0
    vm_slots = max(0, cap - max(running, reserved))
    try:
        capacity = (capacity_reader or (lambda: {}))()
    except Exception as exc:  # noqa: BLE001 - an unreadable lease store is blindness
        return unknown(f"lease store unreadable: {exc}")
    cores, mem = free_lanes[0]
    fit = lease_fit_count(capacity, cores, mem, len(free_lanes)) if capacity else len(free_lanes)
    report.update({
        "vm_cap": cap, "running_vms": running, "reservations": reserved,
        "vm_slots_free": vm_slots, "lease_fit": fit,
        "lane_request": {"cores": cores, "mem_mb": mem},
        "free": min(len(free_lanes), vm_slots, fit),
    })
    return report


def _launchctl_listing() -> str | None:
    # Test seam: a saved `launchctl list`, so a test on a real fleet host never
    # reads that host's own loaded lanes.
    listing_file = os.environ.get("TARTCI_FLEET_LAUNCHCTL_LIST_FILE")
    if listing_file:
        try:
            return Path(listing_file).read_text()
        except OSError:
            return None
    return fleet_lane_discovery._launchctl_list()  # noqa: SLF001


def host_report(repo: str, class_label: str, *, lanes_only: bool = False) -> dict[str, Any]:
    """The report for this host, from the real readers.

    lanes_only classifies this host's lanes without reading capacity: the
    fallback host needs only its siblings' states, not its own free slots.
    """
    home = Path.home()
    agents_dir = Path(os.environ.get("TARTCI_LANE_AGENTS_DIR") or home / "Library/LaunchAgents")
    lanes, problems = fleet_lane_discovery.discover_lanes(agents_dir, list_reader=_launchctl_listing)
    if lanes_only:
        return build_report(
            repo, class_label, lanes=lanes, lane_problems=problems,
            env_reader=lambda label: lane_busy.lane_environment(label, agents_dir))
    import leases  # noqa: PLC0415 - only the full report reads the lease store

    config = home / ".config/tartci"

    def capacity() -> dict[str, Any]:
        store = leases.default_store_dir()
        active, _reaped, _problems = leases.reclaim(leases.load_records(store), 300)
        return leases.usage(active, leases.capacity_config(argparse.Namespace()))

    def running() -> int | None:
        # runner.sh running_macos_vms: one read, one longer retry, else unknown.
        for timeout in (os.environ.get("TARTCI_TART_INVENTORY_TIMEOUT_SECS", "5"),
                        os.environ.get("TARTCI_TART_INVENTORY_RETRY_TIMEOUT_SECS", "15")):
            try:
                out = subprocess.run(
                    [sys.executable, str(Path(__file__).with_name("tart_inventory.py")),
                     "--timeout-seconds", timeout],
                    text=True, capture_output=True, check=False, timeout=float(timeout) + 15)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                continue
            if out.returncode == 0 and out.stdout.strip().isdigit():
                return int(out.stdout.strip())
        return None

    def default_cores() -> int:
        import host_profile  # noqa: PLC0415
        return int(host_profile.build_profile()["vm_pool_cores"])

    return build_report(
        repo, class_label, lanes=lanes, lane_problems=problems,
        env_reader=lambda label: lane_busy.lane_environment(label, agents_dir),
        capacity_reader=capacity, running_reader=running,
        default_cores_reader=default_cores,
        pool_reader=lambda: pool_state(
            Path(os.environ.get("TARTCI_POOL_STATE_FILE") or config / "pool-state"),
            Path(os.environ.get("TARTCI_POOL_PARTICIPATION_FILE")
                 or config / "native-build-participation")),
        cap_reader=lambda: effective_cap(
            Path(os.environ.get("TARTCI_MACOS_CAP_FILE") or config / "macos-vm-cap")),
        reservations_reader=lambda: live_reservations(
            Path(os.environ.get("TARTCI_MACOS_RESV_DIR") or config / "macos-vm-reservations"),
            time.time()),
    )


# ── decision (fallback host) ────────────────────────────────────────────────

def parse_peers(text: str) -> list[tuple[str, str]]:
    peers = []
    for item in text.replace(",", "\n").splitlines():
        item = item.strip()
        if not item:
            continue
        host_id, _, target = item.partition("=")
        peers.append((host_id.strip(), (target or f"tartci-{host_id}").strip()))
    return peers


def fetch_peer(host_id: str, target: str, repo: str, class_label: str, *,
               ssh: str, timeout: float) -> dict[str, Any]:
    command = [ssh, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(timeout // 2))}",
               target, f"cd ~ && ~/.local/bin/tartci pool supply --repo {repo} --class {class_label} --json"]
    started = time.time()
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"verdict": "unknown", "reason": f"ssh {target}: {type(exc).__name__}", "host": host_id}
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        return {"verdict": "unknown", "host": host_id,
                "reason": f"ssh {target} exit {result.returncode}: {tail[0][:160]}"}
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return {"verdict": "unknown", "host": host_id, "reason": "report schema mismatch"}
    value["fetched_at"] = int(started)
    return value


def decide(demand: int, peers: list[dict[str, Any]], local: dict[str, Any] | None, *,
           own_slot: int, own_state_dir: str, repo: str, class_label: str,
           max_age: float, now: float) -> tuple[str, str]:
    if demand <= 0:
        return "hold", "no queued demand"
    cover = 0
    parts = []
    for report in peers:
        host = report.get("host")
        if report.get("verdict") != "ok":
            return "unknown", f"peer {host}: {report.get('reason') or 'unreadable'}"
        if report.get("repo") != repo or report.get("class") != class_label:
            return "unknown", f"peer {host}: report is for a different repo/class"
        fetched = float(report.get("fetched_at", now))
        if now - fetched > max_age:
            return "unknown", f"peer {host}: report {int(now - fetched)}s old > {int(max_age)}s"
        free, flight = int(report.get("free", 0)), int(report.get("in_flight", 0))
        cover += free + flight
        parts.append(f"{host}:free={free},in_flight={flight}")
    local_cover = 0
    if local is not None and local.get("verdict") == "ok":
        for row in local.get("lanes", []):
            if row.get("state_dir") == own_state_dir:
                continue
            if row.get("state") == "in_flight" or (
                    row.get("state") == "free" and int(row.get("slot", 1)) < own_slot):
                local_cover += 1
    elif local is not None:
        # Siblings unreadable: assume every other local lane may be reaching
        # for this job. Holding keeps the time-based rule, which is safe.
        return "unknown", f"local siblings: {local.get('reason') or 'unreadable'}"
    excess = demand - cover - local_cover
    detail = (f"demand={demand} peer_cover={cover} local_cover={local_cover} "
              f"excess={excess} {' '.join(parts)}").strip()
    return ("grant" if excess > 0 else "hold"), detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    report_p = sub.add_parser("report", help="this host's free gate slots for one class (read-only)")
    report_p.add_argument("--repo", required=True)
    report_p.add_argument("--class", dest="class_label", required=True)
    report_p.add_argument("--json", action="store_true")
    decide_p = sub.add_parser("decide", help="grant, hold or unknown for a fallback lane")
    decide_p.add_argument("--repo", required=True)
    decide_p.add_argument("--class", dest="class_label", required=True)
    decide_p.add_argument("--demand", type=int, required=True)
    decide_p.add_argument("--peers", required=True, help="host_id=ssh-target, comma or newline separated")
    decide_p.add_argument("--slot", type=int, default=1)
    decide_p.add_argument("--state-dir", default="")
    decide_p.add_argument("--max-age-seconds", type=float, default=60.0)
    decide_p.add_argument("--timeout-seconds", type=float, default=20.0)
    decide_p.add_argument("--ssh", default=os.environ.get("TARTCI_FALLBACK_SSH", "ssh"))
    decide_p.add_argument("--no-local", action="store_true", help="skip the sibling-lane read")
    args = parser.parse_args(argv)

    if args.command == "report":
        try:
            report = host_report(args.repo, args.class_label)
        except Exception as exc:  # noqa: BLE001 - report blindness, never crash the caller
            report = {"schema": SCHEMA, "verdict": "unknown", "reason": f"report failed: {exc}",
                      "repo": args.repo, "class": args.class_label, "free": 0, "in_flight": 0}
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            print(f"{report.get('verdict')} free={report.get('free')} "
                  f"in_flight={report.get('in_flight')} {report.get('reason') or ''}".rstrip())
        return 0 if report.get("verdict") == "ok" else 2

    peers = parse_peers(args.peers)
    if not peers:
        print("unknown no preferred hosts configured")
        return 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(peers)) as pool:
        reports = list(pool.map(
            lambda peer: fetch_peer(peer[0], peer[1], args.repo, args.class_label,
                                    ssh=args.ssh, timeout=args.timeout_seconds),
            peers))
    for (host_id, _target), report in zip(peers, reports):
        report.setdefault("host", host_id)
    local = None
    if not args.no_local:
        try:
            local = host_report(args.repo, args.class_label, lanes_only=True)
        except Exception as exc:  # noqa: BLE001
            local = {"verdict": "unknown", "reason": f"local report failed: {exc}"}
    verdict, detail = decide(
        args.demand, reports, local, own_slot=args.slot, own_state_dir=args.state_dir,
        repo=args.repo, class_label=args.class_label,
        max_age=args.max_age_seconds, now=time.time())
    print(f"{verdict} {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
