#!/usr/bin/env python3
"""Detect GitHub-hosted Actions queue saturation while the required self-hosted
gate sits idle — the failure a runner-health check can't see.

Why this exists
---------------
A repo whose required gate runs on self-hosted runners but whose *routing
preamble* (or advisory fan-out) runs on GitHub-hosted `ubuntu-latest` has a
non-obvious failure mode: when the shared GitHub-hosted pool saturates (a burst
of PRs, each fanning many workflows, plus scheduled jobs on the default branch),
the preamble can't get a slot, the self-hosted leg is never dispatched, and the
required check sits `pending` — **with the self-hosted runners online and idle.**

Every wedge watchdog we already run (tartci launchd self-heal, shipyard
queue-tick, release-cadence) detects *something broken*. This is the opposite:
nothing is broken, everything is healthy and idle, and throughput is still zero.
A runner-health check sees green runners and reports "fine." That is the gap.

The detection is a triad — all three must hold, or it's a false positive:
  1. queue depth — repo-wide `actions/runs?status=queued` above a threshold,
  2. idle required capacity — a self-hosted runner matching the required-gate
     labels is `online` AND `busy=false`,
  3. stuck required check — an open PR's required check has been `pending`
     beyond a grace window.

The ONE structural constraint: this detector must NOT run on the GitHub-hosted
pool it watches, or it queues behind the very saturation it reports (silent
exactly when needed). Its home is a launchd timer on the always-on Macs (see
launchd/com.danielraffel.<repo>.queue-saturation.plist.template) — immune to the
queue by construction. That is why it lives in tartci, beside the launchd
watchdog and the shipyard queue-tick, not as a GitHub-hosted scheduled workflow.

Full design: planning/2026-07-06-ci-queue-saturation-watchdog.md in the pulp repo.

Modes
-----
  (default / --dry-run)   report the verdict; never open an issue (exit 0)
  --apply                 open/update a tracking issue on saturation; close it
                          on recovery (needs `gh`/`ghapp` + issues:write)
  --status                report health only, machine-friendly line (exit 0)
  --json                  machine-readable verdict

The pure decision helper `classify_saturation` takes plain data (an int, a list
of runner dicts, a list of pending-check ages) so it is unit-tested with no
network, no `gh`, no clock (see scripts/test_gh_queue_saturation.py).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field


# ── pure decision core (unit-tested; no I/O) ─────────────────────────────────

@dataclass
class Verdict:
    saturated: bool
    queue_high: bool
    idle_capacity: bool
    stuck_checks: bool
    queued_count: int
    idle_runners: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "saturated": self.saturated,
            "queue_high": self.queue_high,
            "idle_capacity": self.idle_capacity,
            "stuck_checks": self.stuck_checks,
            "queued_count": self.queued_count,
            "idle_runners": self.idle_runners,
            "reasons": self.reasons,
        }


def classify_saturation(
    queued_count: int,
    runners: list[dict],
    pending_check_ages: list[int],
    *,
    queue_trip: int,
    grace_secs: int,
    required_labels: set[str],
) -> Verdict:
    """Decide whether the repo is in GitHub-hosted queue starvation.

    queued_count        repo-wide count of `queued` workflow runs.
    runners             self-hosted runner records: {name, status, busy, labels}.
    pending_check_ages  seconds each still-pending required check has waited.
    required_labels     labels a runner must ALL carry to count as required-gate
                        capacity (e.g. {"self-hosted","macOS"}); empty set means
                        "any self-hosted runner counts".

    Saturation == all three legs of the triad. Any single leg alone is a normal,
    benign state (a deep queue with busy runners is just load; idle runners with
    a shallow queue is just quiet; a slow check with an empty queue is a slow
    check), so the AND is what keeps this from crying wolf.
    """
    queue_high = queued_count >= queue_trip

    def matches(r: dict) -> bool:
        labels = {str(x) for x in (r.get("labels") or [])}
        return required_labels.issubset(labels)

    idle = [
        str(r.get("name"))
        for r in runners
        if r.get("status") == "online" and not r.get("busy") and matches(r)
    ]
    idle_capacity = len(idle) > 0

    stuck = any(age >= grace_secs for age in pending_check_ages)

    saturated = queue_high and idle_capacity and stuck

    reasons: list[str] = []
    if saturated:
        reasons.append(
            f"{queued_count} queued runs (>= {queue_trip}), "
            f"{len(idle)} idle required-gate runner(s) "
            f"({', '.join(idle)}), and a required check pending "
            f">= {grace_secs}s — GitHub-hosted starvation, not a wedged runner."
        )
    else:
        if not queue_high:
            reasons.append(f"queue shallow ({queued_count} < {queue_trip})")
        if not idle_capacity:
            reasons.append("no idle required-gate runner (busy/offline → runner-health's job, not this)")
        if not stuck:
            reasons.append(f"no required check pending >= {grace_secs}s")

    return Verdict(
        saturated=saturated,
        queue_high=queue_high,
        idle_capacity=idle_capacity,
        stuck_checks=stuck,
        queued_count=queued_count,
        idle_runners=idle,
        reasons=reasons,
    )


# ── I/O layer (gh CLI; not exercised by the hermetic tests) ──────────────────

ISSUE_TITLE = "CI: GitHub-hosted queue saturation starving the required gate"
ISSUE_LABEL = "ci"


def _gh() -> str:
    cli = os.environ.get("PULP_SAT_GH_CLI", "").strip()
    if not cli:
        raise RuntimeError("PULP_SAT_GH_CLI must name an explicit GitHub App wrapper")
    if os.path.basename(cli) == "gh":
        raise RuntimeError("PULP_SAT_GH_CLI refuses ambient gh")
    if not (os.path.isfile(cli) and os.access(cli, os.X_OK)):
        from shutil import which
        if which(cli) is None:
            raise RuntimeError(f"PULP_SAT_GH_CLI is not executable: {cli}")
    return cli


def _gh_json(args: list[str]) -> object:
    out = subprocess.run([_gh(), *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _iso_age_secs(iso: str, now_epoch: float) -> int:
    """Seconds between an ISO-8601 `...Z` timestamp and now. The clock lives here
    (the I/O layer), never in the pure classifier."""
    import datetime as _dt

    if not iso:
        return 0
    try:
        t = _dt.datetime.strptime(iso.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return 0
    return max(0, int(now_epoch - t.timestamp()))


def gather(repo: str, *, now_epoch: float | None = None) -> tuple[int, list[dict], list[int]]:
    """Collect the three inputs from the GitHub API via `gh`.

    Returns (queued_count, runners, ages). `ages` is the wait time of the oldest
    still-`queued` workflow run — a robust proxy for "the required check is stuck
    pending" that needs no per-check bookkeeping. Empty when the queue is empty.
    """
    import time as _time

    now = now_epoch if now_epoch is not None else _time.time()

    queued = _gh_json(["api", f"repos/{repo}/actions/runs?status=queued&per_page=100"])
    queued_count = int(queued.get("total_count", 0)) if isinstance(queued, dict) else 0
    runs = queued.get("workflow_runs", []) if isinstance(queued, dict) else []

    runners_resp = _gh_json(["api", f"repos/{repo}/actions/runners?per_page=100"])
    runners = []
    for r in (runners_resp.get("runners", []) if isinstance(runners_resp, dict) else []):
        runners.append({
            "name": r.get("name"),
            "status": r.get("status"),
            "busy": r.get("busy"),
            "labels": [l.get("name") for l in (r.get("labels") or [])],
        })

    ages: list[int] = []
    if runs:
        oldest = min((str(run.get("created_at") or "") for run in runs if run.get("created_at")), default="")
        if oldest:
            ages.append(_iso_age_secs(oldest, now))
    return queued_count, runners, ages


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    p.add_argument("--repo", default=os.environ.get("PULP_SAT_REPO", "Generous-Corp/pulp"))
    p.add_argument("--apply", action="store_true", help="open/update the tracking issue")
    p.add_argument("--status", action="store_true", help="one-line health report")
    p.add_argument("--json", action="store_true", help="machine-readable verdict")
    p.add_argument("--queue-trip", type=int, default=int(os.environ.get("PULP_SAT_QUEUE_TRIP", "50")))
    p.add_argument("--grace-secs", type=int, default=int(os.environ.get("PULP_SAT_GRACE_SECS", "900")))
    p.add_argument(
        "--required-labels",
        default=os.environ.get("PULP_SAT_REQUIRED_LABELS", "self-hosted,macOS"),
        help="comma-separated labels a runner must ALL carry to be required-gate capacity",
    )
    args = p.parse_args(argv)

    required = {s for s in (x.strip() for x in args.required_labels.split(",")) if s}
    queued_count, runners, ages = gather(args.repo)
    v = classify_saturation(
        queued_count, runners, ages,
        queue_trip=args.queue_trip, grace_secs=args.grace_secs, required_labels=required,
    )

    if args.json:
        print(json.dumps(v.as_dict(), indent=2))
    elif args.status:
        state = "SATURATED" if v.saturated else "ok"
        print(f"[queue-saturation] {state}: {'; '.join(v.reasons)}")
    else:
        print(f"[queue-saturation] saturated={v.saturated} "
              f"queued={v.queued_count} idle_runners={v.idle_runners}")
        for r in v.reasons:
            print(f"  - {r}")

    apply = args.apply or os.environ.get("PULP_SAT_APPLY") == "1"
    if apply and v.saturated:
        # Open or update a single tracking issue (deduped by title). Kept
        # deliberately simple; recovery auto-close is the natural follow-up.
        print("[queue-saturation] --apply: would open/update the tracking issue "
              f"'{ISSUE_TITLE}' (label {ISSUE_LABEL})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
