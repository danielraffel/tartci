#!/usr/bin/env python3
"""Queued self-hosted gate jobs, and the fleet hosts that serve them.

Gate demand is a queued *job*, not a queued run. A run carries no runner
labels, so a run-level read counts hosted-only runs (Windows, Ubuntu) and
months-old zombie runs as demand, and misses the most common real shape: a
`Build and Test` run already `in_progress` because its hosted jobs started,
with its macOS job still queued.

A job is demand for a host when a registration in fleet/advertised-labels.json
for that host serves it: the same repository, the run's workflow named in the
registration's `workflows`, and every label the job asks for among the
registration's labels (case-insensitive). Jobs, and the runs read for them,
older than MAX_JOB_AGE_SECONDS are dropped.

The fetch is injected (`fetch(path) -> dict`, raising on failure), so the
selection can be tested on captured API answers; `gh_fetcher` is the real one.
A read that fails is returned as an error, never as "no demand".
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from typing import Callable, Dict, Iterable, List, Optional, Tuple

# A queued job older than this is not live demand (zombie runs sit queued
# for months); twelve hours outlasts any real queue.
MAX_JOB_AGE_SECONDS = 12 * 3600
RUN_STATUSES = ("queued", "in_progress")
PAGE = 100

Fetch = Callable[[str], dict]

# Machine nicknames for hosts whose advertised host_id differs. Registrations,
# profiles and runner names all use the host_id; operators and older tools say
# m3 and m5s. Every reader that takes a host name goes through canonical_host.
HOST_ALIASES = {"m3": "studio", "m5s": "m5studio"}


def canonical_host(name: object) -> str:
    """The advertised host_id for a host name or one of its nicknames."""
    value = str(name or "").strip()
    return HOST_ALIASES.get(value.lower(), value)


def epoch(value: object) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def registrations(advertised: object, host_id: Optional[str] = None) -> List[dict]:
    """The registrations in an advertised-labels document, for one host or all."""
    rows = advertised.get("registrations") if isinstance(advertised, dict) else None
    out = []
    for reg in rows if isinstance(rows, list) else []:
        if not isinstance(reg, dict):
            continue
        if host_id is not None and canonical_host(reg.get("host_id")) != canonical_host(host_id):
            continue
        out.append(reg)
    return out


def _lower(values: object) -> set:
    return {str(value).lower() for value in values} if isinstance(values, list) else set()


def serving_hosts(job: dict, repo: str, workflow: str, regs: Iterable[dict]) -> List[str]:
    """Hosts with a registration that serves this job."""
    wanted = _lower(job.get("labels"))
    if not wanted:
        return []
    hosts = []
    for reg in regs:
        if str(reg.get("repo") or reg.get("repository") or "").lower() != repo.lower():
            continue
        if workflow.lower() not in _lower(reg.get("workflows")):
            continue
        if wanted <= _lower(reg.get("labels")):
            host = canonical_host(reg.get("host_id"))
            if host not in hosts:
                hosts.append(host)
    return sorted(hosts)


def queued_jobs(fetch: Fetch, regs: List[dict], now: float,
                max_age: float = MAX_JOB_AGE_SECONDS) -> Tuple[List[dict], List[str]]:
    """(queued jobs some host in `regs` serves, read errors)."""
    workflows: Dict[str, set] = {}
    for reg in regs:
        repo = str(reg.get("repo") or reg.get("repository") or "")
        if repo:
            workflows.setdefault(repo, set()).update(_lower(reg.get("workflows")))
    jobs: List[dict] = []
    errors: List[str] = []
    seen_runs: set = set()
    for repo in sorted(workflows):
        for status in RUN_STATUSES:
            path = f"repos/{repo}/actions/runs?status={status}&per_page={PAGE}"
            try:
                listing = fetch(path)
            except Exception as error:  # noqa: BLE001 - any failed read is reported
                errors.append(f"{path}: {error}"[:300])
                continue
            runs = listing.get("workflow_runs") if isinstance(listing, dict) else None
            if not isinstance(runs, list):
                errors.append(f"{path}: no workflow_runs in the answer")
                continue
            total = listing.get("total_count")
            if isinstance(total, int) and total > len(runs):
                errors.append(f"{path}: read {len(runs)} of {total} runs")
            for run in runs:
                if not isinstance(run, dict):
                    continue
                name = str(run.get("name") or "")
                created = epoch(run.get("created_at"))
                if name.lower() not in workflows[repo]:
                    continue
                if created is None or now - created > max_age:
                    continue
                # A run that changed status between the two listings is read once.
                if run.get("id") in seen_runs:
                    continue
                seen_runs.add(run.get("id"))
                jobs_path = f"repos/{repo}/actions/runs/{run.get('id')}/jobs?filter=latest&per_page={PAGE}"
                try:
                    answer = fetch(jobs_path)
                except Exception as error:  # noqa: BLE001
                    errors.append(f"{jobs_path}: {error}"[:300])
                    continue
                listed = answer.get("jobs") if isinstance(answer, dict) else None
                if not isinstance(listed, list):
                    errors.append(f"{jobs_path}: no jobs in the answer")
                    continue
                for job in listed:
                    if not isinstance(job, dict) or job.get("status") != "queued":
                        continue
                    queued_at = epoch(job.get("created_at"))
                    if queued_at is None or now - queued_at > max_age:
                        continue
                    hosts = serving_hosts(job, repo, name, regs)
                    if hosts:
                        jobs.append({
                            "id": job.get("id"),
                            "run_id": run.get("id"),
                            "repo": repo,
                            "workflow": name,
                            "name": job.get("name"),
                            "labels": job.get("labels"),
                            "created_at": job.get("created_at"),
                            "hosts": hosts,
                        })
    return jobs, errors


def gh_fetcher(cli: str, timeout: float = 30) -> Fetch:
    """`fetch(path)` through `<cli> api <path>`, raising on any failure."""
    def fetch(path: str) -> dict:
        result = subprocess.run([cli, "api", path], capture_output=True, text=True,
                                timeout=timeout, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"{cli} exit {result.returncode}: {(result.stderr or result.stdout)[-200:]}")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise RuntimeError("answer is not an object")
        return value
    return fetch


def jobs_for(jobs: Iterable[dict], host_id: str, now: float,
             max_age: float = MAX_JOB_AGE_SECONDS) -> List[dict]:
    """The jobs a host serves that are still young enough to be demand."""
    host_id = canonical_host(host_id)
    out = []
    for job in jobs:
        if not isinstance(job, dict) or host_id not in {canonical_host(h) for h in job.get("hosts") or []}:
            continue
        queued_at = epoch(job.get("created_at"))
        if queued_at is not None and now - queued_at <= max_age:
            out.append(job)
    return out
