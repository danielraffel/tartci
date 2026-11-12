#!/usr/bin/env python3
"""Cross-host build disagreement alarm: probable shared-ccache poisoning.

A poisoned compiler cache does not look like a cache problem. It looks like a
source bug that only one host can see: every build on that host fails to
compile or link, with the same error, while the other gate hosts build the
same code green. On 2026-09-26 one host served a stale object for a translation
unit and every build it ran failed to link one test binary for seventeen hours,
while a sibling host built the same main green the whole time. Each individual
red read as "that PR is broken"; only the comparison across hosts says "that
HOST is broken".

This module makes that comparison. It is read-only against GitHub, does not
touch scheduling, and is disabled unless a profile or the operator enables it.

Rules (both over completed gate jobs inside a lookback window):

  same_build   The same built identity has a green Build step on host A and a
               Build-step failure on host B, B has no green for that identity,
               and B's log carries a compile/link signature. The identity is
               the head SHA for events that build their head commit (push,
               merge_group, dispatch) -- also matched by tree -- and the
               (head, base) pair for pull_request, whose built commit is the
               synthetic merge ref rather than the head.

  streak       Host B's trailing Build-step outcomes are failures on at least K
               distinct commits, the latest K carry the SAME error fingerprint,
               another host completed a green Build during the streak, and no
               other host failed with that fingerprint in the window. A broken
               PR or main fails the same way on every host that builds it; a
               poisoned object fails every build that links it, the same way,
               on one host only.

Detection floor, stated rather than hidden: an identity built on only one host
cannot be compared (same_build is blind to it), and a streak shorter than K,
or one whose logs cannot be read, stays below the alarm. A host that is the
only one serving the gate cannot be distinguished from a broken main at all.
The alarm is evidence for a human or a reset, never an automatic action.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

HERE = Path(__file__).resolve().parent

DEFAULT_REPO = "Generous-Corp/pulp"
DEFAULT_WORKFLOW = "build.yml"
DEFAULT_JOB_NAMES = ("macos",)
DEFAULT_BUILD_STEP = "Build"
DEFAULT_WINDOW_HOURS = 6.0
DEFAULT_STREAK = 3
DEFAULT_MAX_API_CALLS = 200
DEFAULT_MAX_LOG_FETCHES = 12
LISTING_MARGIN = dt.timedelta(hours=2)
# Ordered: first matching prefix wins. Shipyard's durable tag for M3 is
# `studio`, so its ephemeral runners are named studio-*.
DEFAULT_HOST_PREFIXES: tuple[tuple[str, str], ...] = (
    ("studio-", "m3"),
    ("m3-", "m3"),
    ("m5-", "m5"),
    ("m1-", "m1"),
)
# Events whose built commit IS head_sha. A pull_request run builds the merge
# ref, so its head tree is not what was compiled.
HEAD_BUILDING_EVENTS = frozenset({"push", "merge_group", "workflow_dispatch", "schedule"})

REMEDY = (
    "Probable poisoned compiler cache on {host}. On {host}, run tartci ccache "
    "reset` (quarantines zero-include manifests; add --reset to move the whole "
    "shared cache aside; refuses while a VM runs or holds a lease unless "
    "--force). On a host whose installed tartci predates that command, drain its "
    "gate lanes, move its shared ccache directory aside and resume. Then re-run "
    "the failing job and confirm it goes green on {host}."
)

# A compile or link failure. `ld: warning` is deliberately absent: the gate
# prints duplicate-library warnings on every green build.
SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("undefined_symbols", re.compile(r"Undefined symbols for architecture")),
    ("undefined_reference", re.compile(r"undefined reference to")),
    ("ld_symbols_not_found", re.compile(r"ld: symbol\(s\) not found")),
    ("ld_error", re.compile(r"\bld: (?!warning)[a-z].*")),
    ("linker_failed", re.compile(r"linker command failed")),
    ("compile_error", re.compile(r"\.(?:c|cc|cpp|cxx|m|mm|h|hpp)(?::\d+){1,2}: (?:fatal )?error: ")),
)
# The same words can come from a starved host. Those are resource failures,
# not a disagreement about what the code compiles to.
RESOURCE_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("killed", re.compile(r"unable to execute command|Killed: 9|signal: killed")),
    ("disk_full", re.compile(r"No space left on device")),
    ("oom", re.compile(r"out of memory|std::bad_alloc|Cannot allocate memory")),
)
# Lines that identify WHICH symbol or unit failed, used as the fingerprint.
FINGERPRINT_LINE = re.compile(
    r'(?:"[^"]+", referenced from:|undefined reference to .*|'
    r"[\w./+-]+\.(?:c|cc|cpp|cxx|m|mm|h|hpp)(?::\d+){1,2}: (?:fatal )?error: .*)"
)
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ── records ────────────────────────────────────────────────────────────────


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def host_for(runner: str | None, prefixes: Sequence[tuple[str, str]] = DEFAULT_HOST_PREFIXES) -> str | None:
    if not runner:
        return None
    for prefix, host in prefixes:
        if runner.startswith(prefix):
            return host
    return None


def build_identities(run: dict[str, Any]) -> list[str]:
    """Keys under which two jobs are known to have compiled the same code.

    Every attempt of one run builds the same commit (a re-run keeps the
    run's GITHUB_SHA, including a pull_request run's merge ref), so the run id
    is always a key.
    """
    head = str(run.get("head_sha") or "")
    keys = [f"run:{run['id']}"] if run.get("id") is not None else []
    if not head:
        return keys
    event = str(run.get("event") or "")
    if event in HEAD_BUILDING_EVENTS:
        keys.append(f"sha:{head}")
        tree = ((run.get("head_commit") or {}).get("tree_id")) or ""
        if tree:
            keys.append(f"tree:{tree}")
        return keys
    base = ""
    for pr in run.get("pull_requests") or []:
        base = str(((pr or {}).get("base") or {}).get("sha") or "")
        if base:
            break
    # Without a base the built merge commit is unknowable; the head alone is
    # still a valid key only against another run of the same head and base.
    return keys + ([f"pr:{head}:{base}"] if base else [])


def normalize(run: dict[str, Any], job: dict[str, Any], *,
              job_names: Iterable[str] = DEFAULT_JOB_NAMES,
              build_step: str = DEFAULT_BUILD_STEP,
              prefixes: Sequence[tuple[str, str]] = DEFAULT_HOST_PREFIXES) -> dict[str, Any] | None:
    """One gate job reduced to what the rules read, or None when out of scope."""
    if job.get("name") not in set(job_names) or job.get("status") != "completed":
        return None
    host = host_for(job.get("runner_name"), prefixes)
    if host is None:
        return None
    step = next((s for s in job.get("steps") or []
                 if isinstance(s, dict) and s.get("name") == build_step), None)
    conclusion = (step or {}).get("conclusion")
    if conclusion == "success":
        outcome = "green"
    elif conclusion == "failure":
        outcome = "build_failure"
    else:
        outcome = "other"
    return {
        "job_id": job.get("id"),
        "run_id": run.get("id"),
        "run_attempt": job.get("run_attempt") or run.get("run_attempt"),
        "event": run.get("event"),
        "head_sha": run.get("head_sha"),
        "head_branch": run.get("head_branch"),
        "identities": build_identities(run),
        "runner_name": job.get("runner_name"),
        "host": host,
        "outcome": outcome,
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "html_url": job.get("html_url"),
    }


# ── log classification ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class LogVerdict:
    """What a failing job's log says about why its Build step failed."""

    state: str  # "compile_link" | "resource" | "other" | "unknown"
    signatures: tuple[str, ...] = ()
    fingerprint: str = ""
    excerpt: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "signatures": list(self.signatures),
                "fingerprint": self.fingerprint, "excerpt": list(self.excerpt)}


def classify_log(text: str | None) -> LogVerdict:
    if text is None:
        return LogVerdict("unknown")
    lines = [TIMESTAMP.sub("", ANSI.sub("", raw)).rstrip() for raw in text.splitlines()]
    resource = sorted({name for name, pat in RESOURCE_SIGNATURES
                       for line in lines if pat.search(line)})
    found = sorted({name for name, pat in SIGNATURES for line in lines if pat.search(line)})
    if resource:
        return LogVerdict("resource", tuple(resource))
    if not found:
        return LogVerdict("other")
    marks = []
    for line in lines:
        match = FINGERPRINT_LINE.search(line)
        if match:
            marks.append(normalize_mark(match.group(0)))
    fingerprint = "|".join(sorted(set(marks))[:6]) or ",".join(found)
    excerpt = [line.strip() for line in lines
               if any(pat.search(line) for _, pat in SIGNATURES)][:8]
    return LogVerdict("compile_link", tuple(found), fingerprint, tuple(excerpt))


def normalize_mark(mark: str) -> str:
    """Strip what differs between two builds of the same fault: paths, lines."""
    mark = re.sub(r"(?:/[^\s:\"]+)+/", "", mark)       # directory prefixes
    mark = re.sub(r"(\.(?:c|cc|cpp|cxx|m|mm|h|hpp)):\d+(?::\d+)?", r"\1", mark)
    return mark.strip()


# ── rules ──────────────────────────────────────────────────────────────────


@dataclass
class Evaluation:
    """One evaluation pass. Pure: no I/O happens in here except via `log_for`."""

    records: list[dict[str, Any]]
    now: dt.datetime
    window: dt.timedelta
    streak: int = DEFAULT_STREAK
    log_for: Callable[[dict[str, Any]], str | None] = lambda _r: None
    max_log_fetches: int = DEFAULT_MAX_LOG_FETCHES
    log_fetches: int = 0
    verdicts: dict[Any, LogVerdict] = field(default_factory=dict)

    def in_window(self) -> list[dict[str, Any]]:
        lo = self.now - self.window
        out = []
        for record in self.records:
            done = parse_time(record.get("completed_at"))
            if done is not None and lo <= done <= self.now:
                out.append(record)
        return sorted(out, key=lambda r: (r.get("completed_at") or "", r.get("job_id") or 0))

    def verdict(self, record: dict[str, Any]) -> LogVerdict:
        key = record.get("job_id")
        if key in self.verdicts:
            return self.verdicts[key]
        if self.log_fetches >= self.max_log_fetches:
            return LogVerdict("unknown", fingerprint="log_budget_exhausted")
        self.log_fetches += 1
        verdict = classify_log(self.log_for(record))
        self.verdicts[key] = verdict
        return verdict


def same_build_findings(ev: Evaluation, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    green: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        if record["outcome"] == "green":
            for key in record["identities"]:
                green[key][record["host"]].append(record)
    per_host: dict[str, list[dict]] = defaultdict(list)
    seen: set[tuple[str, Any]] = set()
    for record in records:
        if record["outcome"] != "build_failure":
            continue
        host = record["host"]
        for key in record["identities"]:
            hosts = green.get(key, {})
            if host in hosts:  # this host can build it: not a host fault
                continue
            others = [r for h, rows in hosts.items() if h != host for r in rows]
            if not others or (host, record["job_id"]) in seen:
                continue
            seen.add((host, record["job_id"]))
            per_host[host].append({"failure": record, "green": others[0], "identity": key})
            break
    findings = []
    for host, pairs in sorted(per_host.items()):
        confirmed, unknown = [], []
        for pair in pairs:
            verdict = ev.verdict(pair["failure"])
            pair["log"] = verdict.as_dict()
            if verdict.state == "compile_link":
                confirmed.append(pair)
            elif verdict.state == "unknown":
                unknown.append(pair)
        if confirmed or unknown:
            findings.append(finding("same_build", host, confirmed, unknown))
    return findings


def streak_findings(ev: Evaluation, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_host: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if record["outcome"] in ("green", "build_failure"):
            by_host[record["host"]].append(record)
    findings = []
    for host, rows in sorted(by_host.items()):
        tail: list[dict] = []
        for record in reversed(rows):
            if record["outcome"] != "build_failure":
                break
            tail.append(record)
        if len(tail) < ev.streak:
            continue
        tail.reverse()
        start = parse_time(tail[0].get("started_at")) or parse_time(tail[0].get("completed_at"))
        others_green = [r for h, rs in by_host.items() if h != host for r in rs
                        if r["outcome"] == "green"
                        and (parse_time(r.get("completed_at")) or ev.now) >= start]
        if not others_green:
            continue
        # One commit retried is one piece of evidence, not K: keep the latest
        # failure per head SHA and require K distinct commits.
        latest: dict[str, dict] = {}
        for record in tail:
            latest[str(record.get("head_sha"))] = record
        distinct = sorted(latest.values(), key=lambda r: r.get("completed_at") or "")
        if len(distinct) < ev.streak:
            continue
        recent = distinct[-ev.streak:]
        verdicts = [(r, ev.verdict(r)) for r in recent]
        if any(v.state == "unknown" for _, v in verdicts):
            unknown = [{"failure": r, "log": v.as_dict()} for r, v in verdicts]
            findings.append(finding("streak", host, [], unknown, streak=len(tail),
                                    green=others_green[-1]))
            continue
        prints = {v.fingerprint for _, v in verdicts}
        if not (all(v.state == "compile_link" for _, v in verdicts) and len(prints) == 1):
            continue
        # The same error on another host is a source bug everyone hits (a
        # broken PR or main), not this host's cache.
        shared = [r for h, rs in by_host.items() if h != host for r in rs
                  if r["outcome"] == "build_failure" and ev.verdict(r).fingerprint in prints]
        if shared:
            continue
        confirmed = [{"failure": r, "log": v.as_dict()} for r, v in verdicts]
        findings.append(finding("streak", host, confirmed, [], streak=len(tail),
                                green=others_green[-1]))
    return findings


def finding(rule: str, host: str, confirmed: list[dict], unknown: list[dict],
            **extra: Any) -> dict[str, Any]:
    state = "problem" if confirmed else "unknown"
    rows = confirmed or unknown
    # A streak is reported by its newest failure: that is the job to re-run.
    first = rows[-1] if rule == "streak" else rows[0]
    failure = first["failure"]
    green = first.get("green") or extra.get("green") or {}
    out = {
        "rule": rule,
        "state": state,
        "code": "probable_cache_poisoning" if confirmed else "disagreement_log_unreadable",
        "host": host,
        "disagreeing_pairs": len(confirmed) if rule == "same_build" else 0,
        "commit": failure.get("head_sha"),
        "failing_job": {k: failure.get(k) for k in
                        ("job_id", "run_id", "runner_name", "head_branch", "completed_at", "html_url")},
        "failing_step": DEFAULT_BUILD_STEP,
        "green_counterpart": {k: green.get(k) for k in
                              ("job_id", "run_id", "host", "runner_name", "head_sha",
                               "completed_at", "html_url")} if green else None,
        "log": first.get("log"),
        "evidence": [{"identity": r.get("identity"),
                      "failing_job_id": r["failure"].get("job_id"),
                      "head_sha": r["failure"].get("head_sha"),
                      "green_job_id": (r.get("green") or {}).get("job_id"),
                      "fingerprint": (r.get("log") or {}).get("fingerprint")} for r in rows],
        "remedy": REMEDY.format(host=host),
    }
    if "streak" in extra:
        out["streak"] = extra["streak"]
    return out


def evaluate(ev: Evaluation) -> dict[str, Any]:
    records = ev.in_window()
    findings = same_build_findings(ev, records) + streak_findings(ev, records)
    hosts = sorted({r["host"] for r in records})
    state = "problem" if any(f["state"] == "problem" for f in findings) else (
        "unknown" if findings else "ok")
    return {
        "schema": 1,
        "check": "build_disagreement",
        "state": state,
        "now": iso(ev.now),
        "window_hours": ev.window.total_seconds() / 3600,
        "jobs_considered": len(records),
        "hosts_seen": hosts,
        "log_fetches": ev.log_fetches,
        "floor": ("blind to an identity built on only one host and to streaks "
                  f"shorter than {ev.streak}; needs at least two hosts serving the gate"),
        "findings": findings,
    }


# ── GitHub I/O (bounded, read-only) ────────────────────────────────────────


class GitHub:
    """Paginated, budgeted GitHub reads through a configurable CLI."""

    def __init__(self, cli: str, log_cli: str, timeout: float, max_calls: int) -> None:
        self.cli = cli
        self.log_cli = log_cli
        self.timeout = timeout
        self.max_calls = max_calls
        self.calls = 0

    def _run(self, argv: list[str], operation: str) -> str:
        from bounded_subprocess import run_bounded

        if self.calls >= self.max_calls:
            raise RuntimeError(f"GitHub API call budget exhausted ({self.max_calls})")
        self.calls += 1
        proc = run_bounded(argv, timeout=self.timeout, operation=operation)
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip()[:300] or f"{argv[0]} exited {proc.returncode}")
        return proc.stdout

    def api(self, path: str) -> dict[str, Any]:
        payload = json.loads(self._run([self.cli, "api", path], "build_disagreement_api"))
        if not isinstance(payload, dict):
            raise ValueError(f"GitHub API returned non-object for {path}")
        return payload

    def job_log(self, repo: str, job_id: Any) -> str | None:
        # Job logs carry terminal colour codes; current gh refuses to print
        # them without an explicit opt-in, older gh does not know the flag.
        path = f"repos/{repo}/actions/jobs/{job_id}/logs"
        for argv in ([self.log_cli, "api", "--allow-escape-sequences", path],
                     [self.log_cli, "api", path]):
            try:
                return self._run(argv, "build_disagreement_log")
            except (RuntimeError, OSError):
                continue
        return None


def fetch_records(gh: GitHub, repo: str, workflow: str, since: dt.datetime,
                  until: dt.datetime, **norm: Any) -> list[dict[str, Any]]:
    """Every in-scope gate job of runs created in [since, until].

    Runs are listed through the workflow's own endpoint: the `workflow_id`
    query parameter on `actions/runs` is silently ignored by GitHub.
    """
    runs: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = gh.api(f"repos/{repo}/actions/workflows/{workflow}/runs?per_page=100"
                         f"&page={page}&created={iso(since)}..{iso(until)}")
        batch = payload.get("workflow_runs") or []
        runs.extend(batch)
        if len(batch) < 100 or len(runs) >= int(payload.get("total_count") or 0):
            break
        page += 1
    records = []
    for run in runs:
        if run.get("status") != "completed":
            continue
        jobs_page = 1
        while True:
            payload = gh.api(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=all"
                             f"&per_page=100&page={jobs_page}")
            jobs = payload.get("jobs") or []
            for job in jobs:
                record = normalize(run, job, **norm)
                if record:
                    records.append(record)
            if len(jobs) < 100:
                break
            jobs_page += 1
    return records


# ── enablement + CLI ───────────────────────────────────────────────────────


def profile_settings(name: str | None) -> dict[str, Any]:
    if not name:
        return {}
    from profile import load_profile

    _, data = load_profile(name)
    table = data.get("build_disagreement")
    return table if isinstance(table, dict) else {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tartci doctor build-disagreement",
        description="Flag a gate host that fails to compile/link what other hosts build green.")
    p.add_argument("--profile", help="fleet profile whose [build_disagreement] table enables and tunes the check")
    p.add_argument("--enable", action="store_true", help="run even when no profile enables the check")
    p.add_argument("--repo")
    p.add_argument("--workflow")
    p.add_argument("--hours", type=float)
    p.add_argument("--streak", type=int)
    p.add_argument("--now", help="evaluate as of this UTC time (default: now)")
    p.add_argument("--gh-cli", default=os.environ.get("TARTCI_GH_CLI") or "gh")
    p.add_argument("--log-cli", default=os.environ.get("TARTCI_GH_LOG_CLI") or "gh",
                   help="CLI for job-log downloads (must allow --allow-escape-sequences)")
    p.add_argument("--gh-timeout", type=float, default=30.0)
    p.add_argument("--max-api-calls", type=int)
    p.add_argument("--max-log-fetches", type=int)
    p.add_argument("--from-jobs", type=Path, help="replay a recorded job list instead of calling GitHub")
    p.add_argument("--logs-dir", type=Path, help="with --from-jobs: directory of <job_id>.log or .txt files")
    p.add_argument("--dump-jobs", type=Path, help="write the fetched job list here")
    p.add_argument("--json", action="store_true")
    return p


def render_text(result: dict[str, Any]) -> str:
    lines = [f"build-disagreement: {result['state']} ({result['jobs_considered']} gate jobs, "
             f"hosts {', '.join(result['hosts_seen']) or 'none'}, window {result['window_hours']:g}h)"]
    for f in result["findings"]:
        lines.append(f"  [{f['state']}] {f['rule']} host={f['host']} commit={f['commit']} "
                     f"code={f['code']}")
        job = f["failing_job"]
        lines.append(f"    failing: job {job['job_id']} on {job['runner_name']} ({job['html_url']})")
        if f.get("green_counterpart"):
            g = f["green_counterpart"]
            lines.append(f"    green:   job {g['job_id']} on {g['runner_name']} ({g['html_url']})")
        if f.get("log") and f["log"].get("fingerprint"):
            lines.append(f"    error:   {f['log']['fingerprint']}")
        lines.append(f"    remedy:  {f['remedy']}")
    lines.append(f"  floor: {result['floor']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = profile_settings(args.profile)
    enabled = bool(settings.get("enabled", False)) or args.enable
    if not enabled:
        result = {"schema": 1, "check": "build_disagreement", "state": "disabled",
                  "detail": "enable with [build_disagreement] enabled = true in the fleet "
                            "profile, or pass --enable"}
        print(json.dumps(result) if args.json else "build-disagreement: disabled (default off)")
        return 0

    def pick(name: str, default: Any) -> Any:
        value = getattr(args, name)
        return value if value is not None else settings.get(name, default)

    repo = pick("repo", DEFAULT_REPO)
    now = parse_time(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    if now is None:
        print(f"invalid --now: {args.now}", file=sys.stderr)
        return 2
    window = dt.timedelta(hours=float(pick("hours", DEFAULT_WINDOW_HOURS)))
    norm = {"job_names": tuple(settings.get("job_names", DEFAULT_JOB_NAMES)),
            "build_step": settings.get("build_step", DEFAULT_BUILD_STEP)}

    gh = GitHub(args.gh_cli, args.log_cli, args.gh_timeout,
                int(pick("max_api_calls", DEFAULT_MAX_API_CALLS)))
    if args.from_jobs:
        records = json.loads(args.from_jobs.read_text())

        def log_for(record: dict[str, Any]) -> str | None:
            if not args.logs_dir:
                return None
            for suffix in (".log", ".txt"):
                path = args.logs_dir / f"{record['job_id']}{suffix}"
                if path.exists():
                    return path.read_text(errors="replace")
            return None
    else:
        try:
            # Runs are listed by creation, jobs judged by completion: widen the
            # listing so a job that started before the window still counts.
            records = fetch_records(gh, repo, pick("workflow", DEFAULT_WORKFLOW),
                                    now - window - LISTING_MARGIN, now, **norm)
        except (RuntimeError, ValueError, OSError) as error:
            result = {"schema": 1, "check": "build_disagreement", "state": "unknown",
                      "code": "github_unreadable", "detail": str(error)}
            print(json.dumps(result) if args.json else f"build-disagreement: unknown ({error})")
            return 3

        def log_for(record: dict[str, Any]) -> str | None:
            try:
                return gh.job_log(repo, record["job_id"])
            except RuntimeError:
                return None
    if args.dump_jobs:
        args.dump_jobs.write_text(json.dumps(records, indent=1, sort_keys=True) + "\n")

    ev = Evaluation(records=records, now=now, window=window,
                    streak=int(pick("streak", DEFAULT_STREAK)), log_for=log_for,
                    max_log_fetches=int(pick("max_log_fetches", DEFAULT_MAX_LOG_FETCHES)))
    result = evaluate(ev)
    result["api_calls"] = gh.calls
    print(json.dumps(result, indent=1) if args.json else render_text(result))
    return 1 if result["state"] == "problem" else 0


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    sys.exit(main())
