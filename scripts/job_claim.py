#!/usr/bin/env python3
"""Per-job boot claims: at most one booting VM per queued job.

Every free lane that sees a queued job used to clone and boot for it. One of
them mints first and GitHub hands it the job; the rest reach the pre-mint
recheck, find the demand gone and discard a fully booted VM. On the Pulp gate
that was 72 discarded VMs in a day (`assignment_v2_pre_mint_denied`, ~2 min of
VM time each), plus the back-off each discard triggers.

A claim is taken before the clone. For one (repo, runner labels) key, a lane
may take a claim only while the queued job count exceeds the claims already
standing against it. Two kinds of claim stand:

* local: another lane on this host holds a live claim (its supervisor is
  alive, the claim has not expired, and it has not been released);
* fleet: a runner registered with (a superset of) these labels is online and idle.
  That is a lane on ANY host that has already minted for this class and is
  waiting for GitHub to assign it a job; it will take the next queued job, so
  booting another VM for that job is the same waste. The caller supplies the
  names from one runner listing, and names that belong to a local claim are
  not counted twice.

* booting elsewhere: a live claim another HOST published (`status
  --publish`, read over SSH by `gather-peers`) for the same key, younger than
  the age that host declares for its claims (its `max_age_s`, at most the
  TTL; a host that declares none gets DEFAULT_REMOTE_MAX_AGE_SECS). That is a
  lane on another host that has claimed the job but not minted yet; without it
  every host boots for the same job and all but the first discard at the
  pre-mint recheck. A remote claim whose VM already shows in the fleet idle
  listing is counted once (the runner name IS the VM name: the JIT config is
  minted with `name=$vm`).

Peers are read only when the lane opts in, and every peer fault (unreachable,
slow past the read budget, a non-zero exit, unparsable output) counts no
claims from that peer: the lane boots exactly as it would without peers. Only
two hosts that both read before either writes can still boot for one job;
because each host writes its claim only after reading, two lanes can never both
refuse one job.

The count a lane sees may be a lower bound (event-class V2 scans stop at the
first matching job and report 1). A lower bound that does not exceed the
standing claims answers `need_exact` rather than `contended`, so the caller
can buy the exhaustive count only when a sibling actually holds a claim.

Exit codes: 0 claimed, 3 contended, 4 need an exact count, 1 error. Callers
treat 1 as "the claim store is unavailable" and boot exactly as they did
before claims existed.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS and Linux both have it
    fcntl = None  # type: ignore[assignment]

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fleet_source  # noqa: E402
import leases  # noqa: E402

CLAIMED = 0
CONTENDED = 3
NEED_EXACT = 4
ERROR = 1

DEFAULT_TTL_SECS = 1800
# How long another host's claim counts here when that host declares no age of
# its own. A host whose lanes hold claims longer (m1 waits for a lease after
# claiming) declares its own, up to the TTL.
DEFAULT_REMOTE_MAX_AGE_SECS = 900
PEER_CONNECT_TIMEOUT_SECS = 2
DEFAULT_PEER_READ_SECS = 5.0


def default_dir() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "TARTCI_JOB_CLAIM_DIR",
            str(pathlib.Path.home() / ".tartci" / "state" / "job-claims"),
        )
    ).expanduser()


def normalized_labels(labels: str) -> list[str]:
    return sorted({item.strip().lower() for item in labels.split(",") if item.strip()})


def claim_key(repo: str, labels: str) -> str:
    body = "\n".join([repo.lower(), ",".join(normalized_labels(labels))])
    return hashlib.sha256(body.encode()).hexdigest()[:32]


@contextlib.contextmanager
def locked(directory: pathlib.Path, key: str) -> Iterator[pathlib.Path]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}.json"
    if fcntl is None:
        raise OSError("claim store requires fcntl")
    with (directory / f"{key}.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8") or "[]")
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ValueError(f"invalid claim store shape in {path}")
    return data


def save(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def owner_alive(row: dict[str, Any]) -> bool:
    """The claiming supervisor still exists (same pid AND same start time)."""
    try:
        pid = int(row.get("pid"))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    start = leases.pid_start(pid)
    if not start:
        return False
    recorded = " ".join(str(row.get("pid_start") or "").split())
    return not recorded or recorded == start


def live(rows: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if float(row.get("expires_at") or 0) > now and owner_alive(row)
    ]


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fleet_idle_names(path: str | None, labels: str) -> list[str]:
    """Names of online idle runners that can serve every job these labels can.

    Input is one JSON object per line, `{"name": ..., "labels": [...]}`, from a
    runner listing already filtered to online and not busy. A runner counts
    when its labels are a superset of ours: any job our registration could take
    (job labels within ours) it can take too. Unparsable lines are skipped; the
    listing is advisory and must not be able to block a boot by being odd.
    """
    if not path:
        return []
    want = set(normalized_labels(labels))
    names: set[str] = set()
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        have = {str(item).lower() for item in row.get("labels") or []}
        if want and want <= have:
            names.add(row["name"])
    return sorted(names)


def peer_claims(path: str | None, key: str, ttl: int) -> dict[str, Any]:
    """Live claims other hosts published for `key`, from a gather-peers file.

    Returns {"booting": [vm, ...], "read": [host, ...], "unread": [host, ...]}.
    Each line is {"host", "ok", "status"} or {"host", "ok": false, "reason"}.
    A claim counts while its age is within the age its own host declares
    (`max_age_s`, clamped to the TTL), or DEFAULT_REMOTE_MAX_AGE_SECS when the
    host declares none. Anything unparsable counts nothing: fail open.
    """
    out: dict[str, Any] = {"booting": [], "read": [], "unread": []}
    if not path:
        return out
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("host"), str):
            continue
        status = row.get("status")
        if row.get("ok") is not True or not isinstance(status, dict):
            out["unread"].append(row["host"])
            continue
        out["read"].append(row["host"])
        declared = status.get("max_age_s")
        if isinstance(declared, bool) or not isinstance(declared, (int, float)) or declared <= 0:
            limit = DEFAULT_REMOTE_MAX_AGE_SECS
        else:
            limit = min(float(declared), float(ttl))
        for claim in status.get("claims") or []:
            if not isinstance(claim, dict) or claim.get("key") != key:
                continue
            age = claim.get("age_s")
            vm = claim.get("vm")
            if isinstance(age, bool) or not isinstance(age, (int, float)) or not isinstance(vm, str):
                continue
            if 0 <= age <= limit:
                out["booting"].append(vm)
    return out


def acquire(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.queued < 0:
        raise ValueError("queued must be non-negative")
    if args.ttl <= 0:
        raise ValueError("ttl must be positive")
    key = claim_key(args.repo, args.labels)
    fleet_idle = fleet_idle_names(args.fleet_runners_file, args.labels)
    peers = peer_claims(getattr(args, "fleet_claims_file", None), key, args.ttl)
    now = time.time()
    with locked(pathlib.Path(args.dir), key) as path:
        rows = live(load(path), now)
        others = [row for row in rows if row.get("claim_id") != args.claim_id]
        local_vms = {str(row.get("vm") or "") for row in others}
        remote = [
            name for name in fleet_idle
            if name not in local_vms and name != args.vm
        ]
        booting = sorted({
            vm for vm in peers["booting"]
            if vm not in local_vms and vm not in remote and vm != args.vm
        })
        standing = len(others) + len(remote) + len(booting)
        result: dict[str, Any] = {
            "key": key,
            "queued": args.queued,
            "queued_is_lower_bound": args.lower_bound,
            "local_claims": [
                {"lane": row.get("lane"), "vm": row.get("vm")} for row in others
            ],
            "fleet_idle_runners": remote,
            "fleet_booting": booting,
            "peers_read": peers["read"],
            "peers_unread": peers["unread"],
            "standing_claims": standing,
        }
        if args.queued > standing:
            others.append(
                {
                    "claim_id": args.claim_id,
                    "lane": args.lane,
                    "vm": args.vm,
                    "pid": args.pid,
                    "pid_start": leases.pid_start(args.pid),
                    "repo": args.repo,
                    "labels": ",".join(normalized_labels(args.labels)),
                    "created_at": iso(now),
                    "expires_at": now + args.ttl,
                }
            )
            save(path, others)
            result["verdict"] = "claimed"
            return result, CLAIMED
        save(path, others)
        if args.lower_bound and args.queued > 0:
            result["verdict"] = "need_exact"
            return result, NEED_EXACT
        result["verdict"] = "contended"
        return result, CONTENDED


def release(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    key = claim_key(args.repo, args.labels)
    now = time.time()
    with locked(pathlib.Path(args.dir), key) as path:
        rows = load(path)
        kept = [row for row in live(rows, now) if row.get("claim_id") != args.claim_id]
        released = len(rows) != len(kept)
        save(path, kept)
    return {"key": key, "released": released, "remaining": len(kept)}, 0


def status(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Live claims on this host. Read-only; what peers read with --publish."""
    directory = pathlib.Path(args.dir)
    now = time.time()
    claims: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                rows = load(path)
            except (OSError, ValueError):
                continue
            for row in live(rows, now):
                created = parse_iso(row.get("created_at"))
                claims.append(
                    {
                        "key": path.stem,
                        "lane": row.get("lane"),
                        "vm": row.get("vm"),
                        "repo": row.get("repo"),
                        "labels": row.get("labels"),
                        "created_at": row.get("created_at"),
                        "age_s": None if created is None else round(now - created, 1),
                    }
                )
    result: dict[str, Any] = {"claims": claims}
    if getattr(args, "publish", False):
        result["host"] = os.environ.get("TARTCI_RECEIPT_HOST_ID") or host_id_from_profile()
        result["max_age_s"] = declared_max_age()
    return result, 0


def parse_iso(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def profile_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get(
        "TARTCI_FLEET_PROFILE",
        str(pathlib.Path.home() / ".config" / "tartci" / "macos-fleet-profile.toml"),
    )).expanduser()


def profile_host() -> dict[str, Any]:
    try:
        import tomllib  # type: ignore[import-not-found]
        with profile_path().open("rb") as handle:
            host = tomllib.load(handle).get("host")
    except Exception:  # noqa: BLE001 - an unreadable profile declares nothing
        return {}
    return host if isinstance(host, dict) else {}


def host_id_from_profile() -> str | None:
    value = profile_host().get("id")
    return value if isinstance(value, str) else None


def declared_max_age() -> int | None:
    """How long this host's claims may count elsewhere (`host.job_claim_max_age_seconds`)."""
    value = profile_host().get("job_claim_max_age_seconds")
    return value if type(value) is int and value > 0 else None


def peer_supply(named: str | None) -> pathlib.Path:
    """The published supply to read peers from.

    A configured fleet source is validated and must be the supply named, if
    one is named; otherwise the named file, else this checkout's copy. Raises
    ValueError rather than reading another supply, and the caller's claims
    then fail open.
    """
    selected = fleet_source.selected_published_supply()
    if named and selected is not None and os.environ.get("TARTCI_FLEET_SOURCE_ROOT", "").strip():
        if pathlib.Path(named).expanduser().resolve() != selected.resolve():
            raise ValueError(f"--supply {named} is not the configured fleet source's {selected}")
    if named:
        return pathlib.Path(named)
    if selected is not None:
        return selected
    return pathlib.Path(__file__).resolve().parents[1] / fleet_source.PUBLISHED_SUPPLY


def gather_peers(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Read every other published host's claims, in parallel, within a hard budget.

    Writes one JSON line per peer to --out. A peer that fails to answer within
    the budget is killed and recorded unread; nothing here can block a boot.
    """
    me = args.self_host
    targets = peer_targets(peer_supply(args.supply), me)
    procs: dict[str, subprocess.Popen] = {}
    ssh = args.ssh
    for host, target in targets.items():
        # -n and a null stdin: the caller runs inside the supervisor's loops,
        # and an ssh that inherits their stdin drains the rest of the list.
        argv = [ssh, "-n", "-o", "BatchMode=yes", "-o",
                f"ConnectTimeout={PEER_CONNECT_TIMEOUT_SECS}", target,
                "cd ~ && ~/.local/bin/tartci job-claim status --publish"]
        try:
            # Own process group: a straggler is killed with everything it
            # started, so nothing can hold its pipe open past the budget.
            procs[host] = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True,
                                           start_new_session=True)
        except OSError:
            procs[host] = None  # type: ignore[assignment]
    deadline = time.monotonic() + args.read_secs
    rows: list[dict[str, Any]] = []
    for host, proc in procs.items():
        if proc is None:
            rows.append({"host": host, "ok": False, "reason": "spawn_failed"})
            continue
        try:
            out, _ = proc.communicate(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
            if proc.stdout is not None:
                proc.stdout.close()
            rows.append({"host": host, "ok": False, "reason": "timeout"})
            continue
        if proc.returncode != 0:
            rows.append({"host": host, "ok": False, "reason": f"exit_{proc.returncode}"})
            continue
        try:
            value = json.loads(out)
        except ValueError:
            rows.append({"host": host, "ok": False, "reason": "unparsable"})
            continue
        if not isinstance(value, dict) or not isinstance(value.get("claims"), list):
            rows.append({"host": host, "ok": False, "reason": "unparsable"})
            continue
        rows.append({"host": host, "ok": True, "status": value})
    pathlib.Path(args.out).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                                      encoding="utf-8")
    return {"peers": sorted(targets), "unread": [r["host"] for r in rows if not r["ok"]]}, 0


def peer_targets(supply: pathlib.Path, me: str) -> dict[str, str]:
    """host_id -> SSH target for every published host except this one."""
    try:
        value = json.loads(supply.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    targets: dict[str, str] = {}
    for row in (value.get("hosts") or []) if isinstance(value, dict) else []:
        if not isinstance(row, dict) or not isinstance(row.get("host_id"), str):
            continue
        host = row["host_id"]
        if host == me:
            continue
        ssh = row.get("ssh")
        targets[host] = ssh if isinstance(ssh, str) and ssh else f"tartci-{host}"
    return targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job_claim")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dir", default=str(default_dir()))

    acq = sub.add_parser("acquire")
    common(acq)
    acq.add_argument("--repo", required=True)
    acq.add_argument("--labels", required=True)
    acq.add_argument("--claim-id", required=True)
    acq.add_argument("--lane", required=True)
    acq.add_argument("--vm", required=True)
    acq.add_argument("--pid", type=int, required=True)
    acq.add_argument("--queued", type=int, required=True)
    acq.add_argument("--lower-bound", action="store_true",
                     help="--queued is 'at least', not an exact count")
    acq.add_argument("--fleet-runners-file",
                     help="JSON lines {name, labels} of online idle runners")
    acq.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECS)

    rel = sub.add_parser("release")
    common(rel)
    rel.add_argument("--repo", required=True)
    rel.add_argument("--labels", required=True)
    rel.add_argument("--claim-id", required=True)

    acq.add_argument("--fleet-claims-file",
                     help="gather-peers output: other hosts' published claims")

    st = sub.add_parser("status")
    common(st)
    st.add_argument("--publish", action="store_true",
                    help="add this host's id and declared claim max age (for peers)")

    gp = sub.add_parser("gather-peers")
    gp.add_argument("--out", required=True)
    gp.add_argument("--self-host", required=True)
    gp.add_argument("--supply", default=None,
                    help="published supply (default: the selected fleet source's)")
    gp.add_argument("--read-secs", type=float, default=DEFAULT_PEER_READ_SECS)
    gp.add_argument("--ssh", default="ssh")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        handler = {"acquire": acquire, "release": release, "status": status,
                   "gather-peers": gather_peers}[args.command]
        result, rc = handler(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - the caller fails open on ERROR
        print(json.dumps({"verdict": "error", "error": str(exc)}, sort_keys=True))
        return ERROR
    print(json.dumps(result, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
