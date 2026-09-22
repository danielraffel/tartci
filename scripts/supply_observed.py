#!/usr/bin/env python3
"""Fact-check declared fleet supply against what GitHub actually ran.

The published supply (fleet/advertised-labels.json) says which label sets each
host's lanes register. This reads completed Actions jobs for one repository
and attributes every self-hosted job to a declared registration by its runner
name and labels. Runs anywhere with GitHub read access; needs no fleet host.

Runner naming (providers/tart-macos/runner.sh + macos_runner_identity.py, the
same prefixes capacity_floor.py owns):
    <host_id>-<lane>[-slotN]-<NN>-<supervisor pid>-<boot index>
where NN is the two-digit supervisor slot. Persistent Actions services
register under the last component of their launchd label
(actions.runner.<owner>-<repo>.<name>), listed in `persistent_runners`.

Verdicts, per declared registration of --repo:
  OBSERVED     a job ran on this host+lane with labels within the registration
  NOT_OBSERVED no such job, but jobs this registration could serve existed in
               the window (demand existed; someone else or no one served it)
  IDLE         no such job and no demand for this label set in the window
and per observed runner that no declared registration explains:
  UNDECLARED_OBSERVED  the machine ran something git does not declare
Persistent runners that ran jobs are reported as PERSISTENT_OBSERVED.

Bounded: at most --max-run-pages pages of 100 completed runs (all workflows)
created inside --lookback-hours, of which at most --max-runs matching runs
(newest first), and at most --max-job-pages pages of 100 jobs per run. The
report states whether either bound truncated the scan.

Exit 0 clean, 1 NOT_OBSERVED or UNDECLARED_OBSERVED present, 2 unreadable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable

OBSERVED = "OBSERVED"
NOT_OBSERVED = "NOT_OBSERVED"
IDLE = "IDLE"
UNDECLARED_OBSERVED = "UNDECLARED_OBSERVED"
PERSISTENT_OBSERVED = "PERSISTENT_OBSERVED"

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLISHED = ROOT / "fleet" / "advertised-labels.json"
_EPHEMERAL_TAIL = re.compile(r"^(?:-slot(?P<slot>\d+))?-(?P<nn>\d{2})-\d+-\d+$")

Fetch = Callable[[str], dict]


def _lower(labels: Iterable[str]) -> set[str]:
    return {label.lower() for label in labels}


def attribute_runner(runner_name: str, registrations: list[dict],
                     persistent: list[dict]) -> tuple[str, str | None, str | None]:
    """(kind, host_id, lane) for a runner name: kind is lane|persistent|unknown."""
    for row in persistent:
        if row.get("runner_name") and runner_name == row["runner_name"]:
            return "persistent", row["host_id"], None
    best: tuple[int, str, str] | None = None
    for row in registrations:
        prefix = f"{row['host_id']}-{row['lane']}"
        if runner_name.startswith(prefix) and _EPHEMERAL_TAIL.fullmatch(
                runner_name[len(prefix):]):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), row["host_id"], row["lane"])
    if best is None:
        return "unknown", None, None
    return "lane", best[1], best[2]


def _serves(registration: dict, job: dict, repo: str) -> bool:
    return (registration["repo"].lower() == repo.lower()
            and job.get("workflow_name") in registration["workflows"]
            and _lower(job.get("labels") or []) <= _lower(registration["labels"]))


def _undeclared_why(kind: str, host_id, lane, job: dict, registrations: list[dict]) -> str:
    if kind == "unknown":
        return "runner name matches no declared host/lane or persistent runner"
    labels = _lower(job.get("labels") or [])
    lane_regs = [r for r in registrations if (r["host_id"], r["lane"]) == (host_id, lane)]
    if any(labels <= _lower(r["labels"]) for r in lane_regs):
        return ("labels fit this lane's registration but the workflow is not one it mints "
                "for; GitHub assigns by labels alone, so the lane served another "
                "workflow's job")
    return "declared lane ran a job with labels outside its declared registrations"


def classify(published: dict, repo: str, jobs: list[dict]) -> dict:
    """Pure: attribute completed jobs to declared registrations."""
    registrations = published["registrations"]
    persistent = published.get("persistent_runners") or []
    in_repo = [row for row in registrations if row["repo"].lower() == repo.lower()]
    self_hosted = [
        job for job in jobs
        if job.get("status") == "completed"
        and "self-hosted" in _lower(job.get("labels") or [])
    ]
    observed: dict[int, list[dict]] = {i: [] for i in range(len(in_repo))}
    undeclared: dict[tuple, dict] = {}
    persistent_rows: dict[str, dict] = {}
    for job in self_hosted:
        runner = job.get("runner_name")
        if not runner:
            continue  # never assigned (skipped/cancelled in queue): demand only
        kind, host_id, lane = attribute_runner(runner, in_repo, persistent)
        if kind == "persistent":
            row = persistent_rows.setdefault(runner, {
                "verdict": PERSISTENT_OBSERVED, "host_id": host_id,
                "runner_name": runner, "count": 0, "last_seen": None,
                "labels": sorted(set()), "workflows": []})
            row["count"] += 1
            row["last_seen"] = max(filter(None, [row["last_seen"], job.get("completed_at")]),
                                   default=None)
            row["labels"] = sorted(set(row["labels"]) | set(job.get("labels") or []))
            if job.get("workflow_name") not in row["workflows"]:
                row["workflows"].append(job.get("workflow_name"))
            continue
        matched = False
        if kind == "lane":
            for index, reg in enumerate(in_repo):
                if (reg["host_id"], reg["lane"]) == (host_id, lane) and _serves(reg, job, repo):
                    observed[index].append(job)
                    matched = True
                    break
        if not matched:
            key = (runner if kind == "unknown" else f"{host_id}-{lane}",
                   tuple(sorted(job.get("labels") or [])), job.get("workflow_name"))
            row = undeclared.setdefault(key, {
                "verdict": UNDECLARED_OBSERVED,
                "runner": key[0],
                "attributed_to": None if kind == "unknown" else {"host_id": host_id, "lane": lane},
                "labels": list(key[1]), "workflow": key[2],
                "count": 0, "last_seen": None, "example": None,
                "why": _undeclared_why(kind, host_id, lane, job, in_repo),
            })
            row["count"] += 1
            row["last_seen"] = max(filter(None, [row["last_seen"], job.get("completed_at")]),
                                   default=None)
            row["example"] = row["example"] or job.get("html_url")
    rows = []
    for index, reg in enumerate(in_repo):
        hits = observed[index]
        base = {"host_id": reg["host_id"], "lane": reg["lane"],
                "class_label": reg.get("class_label"), "labels": reg["labels"],
                "workflows": reg["workflows"]}
        if hits:
            rows.append({**base, "verdict": OBSERVED, "count": len(hits),
                         "last_seen": max((j.get("completed_at") or "") for j in hits) or None})
            continue
        demand = [job for job in self_hosted
                  if job.get("conclusion") != "skipped" and _serves(reg, job, repo)]
        if demand:
            served_by: dict[str, int] = {}
            for job in demand:
                runner = job.get("runner_name") or "<never assigned>"
                kind, host_id, lane = attribute_runner(runner, in_repo, persistent)
                who = f"{host_id}/{lane}" if kind == "lane" else runner
                served_by[who] = served_by.get(who, 0) + 1
            rows.append({**base, "verdict": NOT_OBSERVED, "count": 0,
                         "demand_jobs": len(demand), "served_by": served_by})
        else:
            rows.append({**base, "verdict": IDLE, "count": 0})
    return {
        "schema": "tartci.supply-observed/v1",
        "repo": repo,
        "jobs_considered": len(self_hosted),
        "registrations": rows,
        "undeclared": sorted(undeclared.values(), key=lambda r: (r["runner"], r["workflow"] or "")),
        "persistent": sorted(persistent_rows.values(), key=lambda r: r["runner_name"]),
    }


# ── GitHub reading (bounded) ────────────────────────────────────────────────

def gh_fetcher(cli: str) -> Fetch:
    def fetch(path: str) -> dict:
        proc = subprocess.run([cli, "api", path], capture_output=True, text=True,
                              timeout=60, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"{cli} api {path}: exit {proc.returncode}: "
                               f"{(proc.stderr or proc.stdout).strip()[:300]}")
        return json.loads(proc.stdout)
    return fetch


def default_cli() -> str:
    return os.environ.get("TARTCI_GH_CLI") or ("ghapp" if shutil.which("ghapp") else "gh")


def collect_jobs(fetch: Fetch, repo: str, workflows: set[str], lookback_hours: int,
                 max_runs: int, max_job_pages: int,
                 now: dt.datetime | None = None, max_run_pages: int = 10) -> tuple[list[dict], dict]:
    now = now or dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs: list[dict] = []
    page, run_pages_truncated = 1, False
    while True:
        if len(runs) >= max_runs or page > max_run_pages:
            run_pages_truncated = True
            break
        value = fetch(f"repos/{repo}/actions/runs?status=completed&per_page=100"
                      f"&page={page}&created=%3E%3D{since}")
        batch = value.get("workflow_runs") or []
        runs.extend(run for run in batch if run.get("name") in workflows)
        if len(batch) < 100:
            break
        page += 1
    if len(runs) > max_runs:
        runs, run_pages_truncated = runs[:max_runs], True
    jobs: list[dict] = []
    job_pages_truncated = False
    for run in runs:
        for job_page in range(1, max_job_pages + 1):
            value = fetch(f"repos/{repo}/actions/runs/{run['id']}/jobs"
                          f"?filter=latest&per_page=100&page={job_page}")
            batch = value.get("jobs") or []
            jobs.extend(batch)
            if len(batch) < 100:
                break
        else:
            job_pages_truncated = True
    return jobs, {"since": since, "runs_scanned": len(runs),
                  "runs_truncated": run_pages_truncated,
                  "job_pages_truncated": job_pages_truncated,
                  "max_runs": max_runs, "max_job_pages": max_job_pages,
                  "max_run_pages": max_run_pages}


def render(result: dict) -> str:
    scan = result.get("scan") or {}
    lines = [f"supply observed: repo={result['repo']} since={scan.get('since', '-')} "
             f"runs={scan.get('runs_scanned', '-')} self-hosted jobs={result['jobs_considered']}"
             + (" TRUNCATED" if scan.get("runs_truncated") or scan.get("job_pages_truncated")
                else "")]
    for row in result["registrations"]:
        name = f"{row['host_id']}/{row['lane']}" + (
            f" [{row['class_label']}]" if row.get("class_label") else "")
        detail = ""
        if row["verdict"] == OBSERVED:
            detail = f"{row['count']} jobs, last {row['last_seen']}"
        elif row["verdict"] == NOT_OBSERVED:
            detail = (f"0 jobs; {row['demand_jobs']} matching jobs in window served by "
                      + ", ".join(f"{k}×{v}" for k, v in sorted(row["served_by"].items())))
        else:
            detail = "no demand for this label set in window"
        lines.append(f"  {row['verdict']:<20} {name}: {detail}")
    for row in result["persistent"]:
        lines.append(f"  {row['verdict']:<20} {row['host_id']}/{row['runner_name']}: "
                     f"{row['count']} jobs, last {row['last_seen']} labels={','.join(row['labels'])}")
    for row in result["undeclared"]:
        lines.append(f"  {row['verdict']:<20} {row['runner']}: {row['count']} jobs "
                     f"[{row['workflow']}] labels={','.join(row['labels'])} — {row['why']}")
    return "\n".join(lines)


def exit_code(result: dict) -> int:
    bad = any(row["verdict"] == NOT_OBSERVED for row in result["registrations"])
    return 1 if bad or result["undeclared"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--published", default=str(DEFAULT_PUBLISHED),
                        help="file or URL of the tartci.advertised-labels/v1 supply")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--max-runs", type=int, default=150)
    parser.add_argument("--max-job-pages", type=int, default=3)
    parser.add_argument("--max-run-pages", type=int, default=10)
    parser.add_argument("--jobs-file", type=Path,
                        help="classify a saved actions/runs/<id>/jobs response instead of fetching")
    parser.add_argument("--gh-cli", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import macos_fleet_lanes as fleet
        published = fleet.read_published(args.published)
        workflows = {w for row in published["registrations"]
                     if row["repo"].lower() == args.repo.lower() for w in row["workflows"]}
        if args.jobs_file:
            jobs = json.loads(args.jobs_file.read_text()).get("jobs") or []
            scan = {"source": str(args.jobs_file)}
        else:
            jobs, scan = collect_jobs(gh_fetcher(args.gh_cli or default_cli()), args.repo,
                                      workflows, args.lookback_hours, args.max_runs,
                                      args.max_job_pages, max_run_pages=args.max_run_pages)
    except Exception as exc:  # noqa: BLE001 - any unread input is not a verdict
        print(f"supply-observed: UNKNOWN: {exc}", file=sys.stderr)
        return 2
    result = classify(published, args.repo, jobs)
    result["scan"] = scan
    print(json.dumps(result, indent=2) if args.json else render(result))
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
