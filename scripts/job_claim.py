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

A booting lane on another host that has not minted yet is invisible here: no
state tartci already publishes carries it, so cross-host races before the mint
remain possible and fall through to the existing pre-mint recheck.

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
import sys
import time
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS and Linux both have it
    fcntl = None  # type: ignore[assignment]

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import leases  # noqa: E402

CLAIMED = 0
CONTENDED = 3
NEED_EXACT = 4
ERROR = 1

DEFAULT_TTL_SECS = 1800


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


def acquire(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.queued < 0:
        raise ValueError("queued must be non-negative")
    if args.ttl <= 0:
        raise ValueError("ttl must be positive")
    key = claim_key(args.repo, args.labels)
    fleet_idle = fleet_idle_names(args.fleet_runners_file, args.labels)
    now = time.time()
    with locked(pathlib.Path(args.dir), key) as path:
        rows = live(load(path), now)
        others = [row for row in rows if row.get("claim_id") != args.claim_id]
        local_vms = {str(row.get("vm") or "") for row in others}
        remote = [
            name for name in fleet_idle
            if name not in local_vms and name != args.vm
        ]
        standing = len(others) + len(remote)
        result: dict[str, Any] = {
            "key": key,
            "queued": args.queued,
            "queued_is_lower_bound": args.lower_bound,
            "local_claims": [
                {"lane": row.get("lane"), "vm": row.get("vm")} for row in others
            ],
            "fleet_idle_runners": remote,
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
                claims.append(
                    {
                        "lane": row.get("lane"),
                        "vm": row.get("vm"),
                        "repo": row.get("repo"),
                        "labels": row.get("labels"),
                        "created_at": row.get("created_at"),
                    }
                )
    return {"claims": claims}, 0


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

    st = sub.add_parser("status")
    common(st)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        handler = {"acquire": acquire, "release": release, "status": status}[args.command]
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
