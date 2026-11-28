#!/usr/bin/env python3
"""Yield a PR-class VM claim to a queued merge-group job, with a bounded hold.

A merge-group job can land up to five pull requests, and a running VM cannot be
preempted. The lane's scan is a snapshot that can lag a freshly queued
merge-group run (a PR-class claim is selected exactly when the last snapshot
counted none), so a snapshot-based cap never fires. Instead, with a lane's
`pr_class_yield_to_merge_group = true`, a lane about to start a PR-class VM
makes ONE fresh read of the repository's queued merge_group runs:

  * none queued, or every queued one already held for once on this host:
    claim as before;
  * otherwise it yields, and holds the free slot for `merge_group_grace_secs`
    (default 120, bounded 30 to 600) by writing a placeholder reservation that
    every lane's slot claim counts (providers/tart-macos/macos-vm-cap.lib.sh).
    The placeholder carries a start time backdated by the reservation TTL, so it
    stops counting at exactly the end of the grace period without anyone
    having to remove it;
  * the hold ends early when the merge-group run is no longer queued (a lane on
    any host claimed it: seen by the next PR-class poll's fresh read) or when
    this repository's merge-group tier is being claimed here (`release`);
  * a run id holds at most once per host: after its hold ends, that run never
    triggers a hold again, so a job that is never claimed cannot starve PR work.

Fail open: a read error, a timeout, a rate limit, a bad payload or any local
failure claims as before and drops a placeholder it holds. Merge-group claims,
other tiers and other repositories are never held (state is per repository).

One output line: `claim|yield|hold key=value ...`; `ended=served|expired` names
a hold this call ended. The read is the lane's own `gh api` (TARTCI_GH_CLI) with
no cache. Compatible with the hosts' Python 3.9.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bounded_subprocess import run_bounded  # noqa: E402

DEFAULT_GRACE_SECS = 120
MIN_GRACE_SECS = 30
MAX_GRACE_SECS = 600
DEFAULT_RESV_TTL = 7200
HELD_KEEP_SECS = 7 * 86400


def slug(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", repo)


def clamp_grace(value: Any) -> int:
    try:
        grace = int(value)
    except (TypeError, ValueError):
        return DEFAULT_GRACE_SECS
    return max(MIN_GRACE_SECS, min(MAX_GRACE_SECS, grace))


def placeholder_path(resv_dir: pathlib.Path, repo: str) -> pathlib.Path:
    return resv_dir / f"resv.yield.{slug(repo)}"


def pid_token(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return "".join(out.split())


def write_placeholder(path: pathlib.Path, owner_pid: int, until: float, ttl: int) -> None:
    """A reservation that stops counting at `until`: its stamp is backdated by the TTL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = max(1, int(until) - ttl)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{owner_pid} {pid_token(owner_pid)} {stamp}", encoding="utf-8")
    os.replace(str(tmp), str(path))


def remove_placeholder(path: pathlib.Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


@contextlib.contextmanager
def locked(state_dir: pathlib.Path, repo: str) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / f"{slug(repo)}.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_state(path: pathlib.Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": None, "held": {}}
    if not isinstance(data, dict):
        return {"active": None, "held": {}}
    active = data.get("active") if isinstance(data.get("active"), dict) else None
    held = data.get("held") if isinstance(data.get("held"), dict) else {}
    return {"active": active, "held": held}


def save_state(path: pathlib.Path, state: Dict[str, Any], now: float) -> None:
    state["held"] = {k: v for k, v in state["held"].items()
                     if isinstance(v, (int, float)) and now - v < HELD_KEEP_SECS}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def gh_json(args: argparse.Namespace, path: str) -> Dict[str, Any]:
    result = run_bounded([args.gh_cli, "api", path], timeout=float(args.gh_timeout),
                         operation="pr_class_yield_github_api")
    if getattr(result, "timed_out", False):
        raise RuntimeError("timeout")
    if result.returncode:
        raise RuntimeError(f"gh_exit_{result.returncode}")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("non-object payload")
    return payload


def queued_merge_group_runs(args: argparse.Namespace) -> List[Dict[str, Any]]:
    """Queued merge_group runs of the repository (one fresh call), oldest first."""
    payload = gh_json(args, f"repos/{args.repo}/actions/runs"
                            "?event=merge_group&status=queued&per_page=100")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise ValueError("no workflow_runs")
    wanted = set(args.workflow or [])
    out = []
    for run in runs:
        if not isinstance(run, dict) or run.get("status") != "queued":
            continue
        if run.get("event") not in (None, "merge_group"):
            continue
        if wanted and str(run.get("name") or "") not in wanted:
            continue
        run_id = run.get("id")
        if isinstance(run_id, int):
            out.append({"id": run_id, "created_at": str(run.get("created_at") or "")})
    out.sort(key=lambda row: (row["created_at"], row["id"]))
    return out


JOB_IDS_TIMEOUT_SECS = 5


def queued_job_ids(args: argparse.Namespace, run_id: int) -> str:
    """The run's queued job ids (one more call, only when a hold starts), or `unknown`.

    Read after the lock is released and with a short timeout: the ids only label
    an event, so a slow API must never stall a hold or the sibling supervisor.
    """
    try:
        short = argparse.Namespace(**{**vars(args), "gh_timeout": min(
            int(args.gh_timeout), JOB_IDS_TIMEOUT_SECS)})
        payload = gh_json(short, f"repos/{args.repo}/actions/runs/{run_id}/jobs"
                                "?filter=latest&per_page=100")
        ids = [str(job["id"]) for job in payload.get("jobs", [])
               if isinstance(job, dict) and job.get("status") == "queued"
               and isinstance(job.get("id"), int)]
        return ",".join(ids) or "unknown"
    except Exception:  # noqa: BLE001 - ids are labels on an event, never a gate
        return "unknown"


def render(verdict: str, fields: Dict[str, Any]) -> str:
    parts = [verdict]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={str(value).replace(' ', '_')}")
    return " ".join(parts)


def check(args: argparse.Namespace) -> str:
    now = float(args.now) if args.now is not None else time.time()
    grace = clamp_grace(args.grace_secs)
    state_dir = pathlib.Path(args.state_dir).expanduser()
    resv_dir = pathlib.Path(args.resv_dir).expanduser()
    holder = placeholder_path(resv_dir, args.repo)
    state_file = state_dir / f"{slug(args.repo)}.json"
    base = {"repo": args.repo, "host": args.host}
    try:
        queued = queued_merge_group_runs(args)
    except Exception as exc:  # noqa: BLE001 - fail open on anything
        with contextlib.suppress(Exception), locked(state_dir, args.repo):
            state = load_state(state_file)
            ended = state["active"]
            if ended:
                state["active"] = None
                remove_placeholder(holder)
                save_state(state_file, state, now)
        return render("claim", {**base, "reason": f"read_error:{type(exc).__name__}"})
    ids = {row["id"] for row in queued}
    try:
        with locked(state_dir, args.repo):
            state = load_state(state_file)
            out: Dict[str, Any] = dict(base)
            active = state["active"]
            if active:
                run_id = active.get("run_id")
                if now < float(active.get("until") or 0) and run_id in ids and holder.exists():
                    out.update(run_id=run_id, hold_left_secs=int(float(active["until"]) - now))
                    return render("hold", out)
                served = run_id not in ids
                out.update(ended="served" if served else "expired", ended_run_id=run_id)
                state["active"] = None
                remove_placeholder(holder)
            fresh = [row for row in queued if str(row["id"]) not in state["held"]]
            if not fresh:
                save_state(state_file, state, now)
                out["reason"] = "no_unheld_merge_group_queued"
                return render("claim", out)
            target = fresh[0]
            until = now + grace
            # One hold covers every run queued right now: a burst of regrouped
            # runs must not chain into one hold each.
            for row in fresh:
                state["held"][str(row["id"])] = now
            state["active"] = {"run_id": target["id"], "since": now, "until": until}
            write_placeholder(holder, int(args.owner_pid), until,
                              int(args.resv_ttl))
            save_state(state_file, state, now)
            out.update(run_id=target["id"], grace=grace,
                       oldest_queued_at=target["created_at"],
                       covered_runs=",".join(str(row["id"]) for row in fresh))
            started = (target["id"], out)
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            remove_placeholder(holder)
        return render("claim", {**base, "reason": f"state_error:{type(exc).__name__}"})
    out["job_ids"] = queued_job_ids(args, started[0])
    return render("yield", out)


def release(args: argparse.Namespace) -> str:
    """This repository's merge-group tier is being claimed here: end the hold."""
    now = float(args.now) if args.now is not None else time.time()
    state_dir = pathlib.Path(args.state_dir).expanduser()
    holder = placeholder_path(pathlib.Path(args.resv_dir).expanduser(), args.repo)
    state_file = state_dir / f"{slug(args.repo)}.json"
    base = {"repo": args.repo, "host": args.host}
    try:
        with locked(state_dir, args.repo):
            state = load_state(state_file)
            active = state["active"]
            remove_placeholder(holder)
            if not active:
                return render("claim", {**base, "reason": "no_hold"})
            state["active"] = None
            save_state(state_file, state, now)
            return render("claim", {**base, "ended": "served",
                                    "ended_run_id": active.get("run_id"),
                                    "via": "merge_group_claim"})
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            remove_placeholder(holder)
        return render("claim", {**base, "reason": f"state_error:{type(exc).__name__}"})


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "release"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--host", default="")
    parser.add_argument("--grace-secs", type=int, default=DEFAULT_GRACE_SECS)
    parser.add_argument("--workflow", action="append", default=[],
                        help="restrict to runs of this workflow name (repeatable)")
    parser.add_argument("--owner-pid", type=int, default=os.getppid())
    home = pathlib.Path.home()
    parser.add_argument("--state-dir", default=os.environ.get(
        "TARTCI_PR_CLASS_YIELD_DIR", str(home / ".tartci/state/pr-class-yield")))
    parser.add_argument("--resv-dir", default=os.environ.get(
        "TARTCI_MACOS_RESV_DIR", str(home / ".config/tartci/macos-vm-reservations")))
    parser.add_argument("--resv-ttl", type=int, default=int(os.environ.get(
        "TARTCI_MACOS_RESV_TTL", DEFAULT_RESV_TTL)))
    parser.add_argument("--gh-cli", default=os.environ.get("TARTCI_GH_CLI") or "gh")
    parser.add_argument("--gh-timeout", type=int,
                        default=int(os.environ.get("TARTCI_GH_TIMEOUT_SECS", "10")))
    parser.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args = parse_args(argv)
        line = check(args) if args.command == "check" else release(args)
    except (Exception, SystemExit) as exc:  # fail open: a broken yield must never hold a lane
        line = f"claim reason=helper_failed:{type(exc).__name__}"
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
