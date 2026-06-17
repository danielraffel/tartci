#!/usr/bin/env python3
"""Host-local runtime measurement store for tartci VM runners."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from typing import Any

from timing_lib import collect, parse_timing


SCHEMA_VERSION = 1
FAILURE_CLASSES = {
    "source_failure",
    "runner_timeout",
    "idle_timeout",
    "boot_failed",
    "ssh_failed",
    "jit_failed",
    "runner_nonzero",
    "unknown",
}
PHASE_TO_FIELD = {
    "boot_to_ssh": "boot_ms",
    "preflight": "setup_ms",
    "runner_process": "run_ms",
    "total": "total_ms",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def default_store() -> pathlib.Path:
    return pathlib.Path(os.environ.get("TARTCI_RUNTIME_STORE", pathlib.Path.home() / ".tartci/runtime"))


def repo_key(repo: str) -> str:
    return repo.replace("/", "__").replace(":", "_")


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_atomic(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.write("\n")
        tmp = pathlib.Path(fh.name)
    os.replace(tmp, path)


def append_jsonl_locked(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def iter_records(path: pathlib.Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def records_path(store: pathlib.Path, repo: str) -> pathlib.Path:
    return store / "records" / f"{repo_key(repo)}.jsonl"


def summary_path(store: pathlib.Path, repo: str, run_id: str, job_id: str) -> pathlib.Path:
    return store / "summaries/by-run" / repo_key(repo) / str(run_id) / f"{job_id or 'unknown'}.summary.json"


def inflight_path(store: pathlib.Path, host: str, runner_name: str) -> pathlib.Path:
    return store / "inflight" / host / f"{runner_name}.json"


def stat_golden(path_or_name: str | None, hash_contents: bool) -> dict[str, Any]:
    if not path_or_name:
        return {}
    out: dict[str, Any] = {"golden": path_or_name, "golden_name": pathlib.Path(path_or_name).name}
    path = pathlib.Path(path_or_name).expanduser()
    if path.exists():
        st = path.stat()
        out.update(
            {
                "golden_size_bytes": st.st_size,
                "golden_mtime": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        if hash_contents:
            digest = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            out["golden_sha256"] = digest.hexdigest()
    return out


def phase_seconds_to_ms(phases: dict[str, float]) -> dict[str, int]:
    return {name: int(round(seconds * 1000)) for name, seconds in phases.items()}


def parse_timing_fields(path: str | None) -> tuple[dict[str, Any], dict[str, int]]:
    if not path:
        return {}, {}
    phases = parse_timing(pathlib.Path(path))
    phases_ms = phase_seconds_to_ms(phases)
    mapped: dict[str, Any] = {"phases_ms": phases_ms}
    for phase, field in PHASE_TO_FIELD.items():
        if phase in phases_ms:
            mapped[field] = phases_ms[phase]
    return mapped, phases_ms


# Match the providers: route enrichment GitHub calls through TARTCI_GH_CLI
# (default `gh`) so a host that authenticates as a GitHub App keeps this path off
# the shared personal PAT too. Lower-frequency than the per-poll loop, but it
# would otherwise be a bare-PAT hole in the off-PAT routing.
def _gh_cli() -> str:
    return os.environ.get("TARTCI_GH_CLI") or "gh"


def gh_json(repo: str, path: str, timeout: int) -> dict[str, Any]:
    out = subprocess.check_output(
        [_gh_cli(), "api", f"repos/{repo}/{path}"],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
    )
    data = json.loads(out)
    return data if isinstance(data, dict) else {}


def enrich_from_github(repo: str, runner_name: str, workflow: str, timeout: int) -> dict[str, str]:
    if not shutil.which(_gh_cli()):
        return {}
    try:
        runs = gh_json(repo, "actions/runs?per_page=50", timeout).get("workflow_runs", [])
        for run in runs:
            if workflow and run.get("name") != workflow:
                continue
            run_id = run.get("id")
            if not run_id:
                continue
            jobs = gh_json(repo, f"actions/runs/{run_id}/jobs?filter=latest&per_page=100", timeout).get("jobs", [])
            for job in jobs:
                if job.get("runner_name") == runner_name:
                    return {
                        "run_id": str(run_id),
                        "job_id": str(job.get("id") or ""),
                        "workflow": str(run.get("name") or ""),
                        "job": str(job.get("name") or ""),
                    }
    except Exception:
        return {}
    return {}


def record_from_args(args: argparse.Namespace) -> dict[str, Any]:
    timing_fields, _ = parse_timing_fields(args.timing_path)
    now = utc_now()
    status = args.status or ("pass" if args.exit_code == 0 else "fail")
    failure_class = args.failure_class or ("unknown" if status == "pass" else "source_failure")
    if failure_class not in FAILURE_CLASSES:
        raise SystemExit(f"invalid failure_class {failure_class!r}")
    run_id = args.run_id or ""
    job_id = args.job_id or ""
    workflow = args.workflow or ""
    job = args.job or ""
    if args.gh_enrich and args.runner_name and (not run_id or not job_id):
        enriched = enrich_from_github(args.repo, args.runner_name, args.workflow, args.gh_timeout)
        run_id = run_id or enriched.get("run_id", "")
        job_id = job_id or enriched.get("job_id", "")
        workflow = workflow or enriched.get("workflow", "")
        job = job or enriched.get("job", "")
    attempt = args.attempt or ""
    external_id = args.external_id or (f"github:{run_id}/{job_id}/{attempt}" if run_id or job_id else "")
    repo = args.repo
    host = args.host or socket.gethostname()
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": args.source,
        "project": args.project or repo.split("/")[-1],
        "repo": repo,
        "workflow": workflow,
        "job": job,
        "provider": args.provider,
        "backend": "vm",
        "host": host,
        "platform": args.platform or "",
        "arch": args.arch or "",
        "runner_name": args.runner_name or "",
        "vm_name": args.vm_name or args.runner_name or "",
        "labels": split_csv(args.labels),
        "external_id": external_id,
        "run_id": run_id,
        "job_id": job_id,
        "attempt": attempt,
        "queued_at": args.queued_at or "",
        "started_at": args.started_at or now,
        "completed_at": args.completed_at or now,
        "status": status,
        "exit_code": args.exit_code,
        "timed_out": bool(args.timed_out),
        "failure_class": failure_class,
        "github_conclusion": args.github_conclusion or "",
        "cache_mode": args.cache_mode,
        "cache_mode_source": args.cache_mode_source,
        "cache_hit": args.cache_hit,
        "ccache_hit_pct": args.ccache_hit_pct,
        "cache_restore_ms": args.cache_restore_ms,
        "cache_save_ms": args.cache_save_ms,
        "cpu_count": args.cpu_count,
        "ram_mb": args.ram_mb,
        "tags": split_csv(args.tags or os.environ.get("TARTCI_RUNTIME_TAGS", "")),
        "log_dir": args.log_dir or "",
        "recorded_at": now,
    }
    record.update(timing_fields)
    record.update(stat_golden(args.golden, args.golden_hash))

    started = parse_utc(record.get("started_at"))
    completed = parse_utc(record.get("completed_at"))
    queued = parse_utc(record.get("queued_at"))
    if "total_ms" not in record and started and completed:
        record["total_ms"] = int(round((completed - started).total_seconds() * 1000))
    if queued and started and "queue_ms" not in record:
        record["queue_ms"] = int(round((started - queued).total_seconds() * 1000))
    return {k: v for k, v in record.items() if v is not None}


def cmd_complete(args: argparse.Namespace) -> int:
    store = args.store
    record = record_from_args(args)
    append_jsonl_locked(records_path(store, record["repo"]), record)
    run_id = str(record.get("run_id") or "unknown")
    job_id = str(record.get("job_id") or record.get("runner_name") or "unknown")
    summary = {
        "found": True,
        "record": record,
    }
    write_atomic(summary_path(store, record["repo"], run_id, job_id), summary)
    runner = str(record.get("runner_name") or "")
    if runner:
        inflight = inflight_path(store, str(record.get("host") or socket.gethostname()), runner)
        if inflight.exists():
            inflight.unlink()
    print(json.dumps(summary if args.json else record, sort_keys=True))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    root = args.store / "summaries/by-run" / repo_key(args.repo) / str(args.run_id)
    if args.job_id:
        paths = [root / f"{args.job_id}.summary.json"]
    elif root.exists():
        paths = sorted(root.glob("*.summary.json"))
    else:
        paths = []
    records = []
    for path in paths:
        try:
            payload = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("found") and "record" in payload:
            records.append(payload["record"])
    payload = {"found": bool(records), "repo": args.repo, "run_id": str(args.run_id), "records": records}
    if args.job_id:
        payload["job_id"] = str(args.job_id)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    elif records:
        for record in records:
            print(
                "{job} {status} total={total}s provider={provider} runner={runner}".format(
                    job=record.get("job") or record.get("job_id") or "-",
                    status=record.get("status", "unknown"),
                    total=(record.get("total_ms", 0) / 1000),
                    provider=record.get("provider", ""),
                    runner=record.get("runner_name", ""),
                )
            )
    else:
        print(json.dumps(payload, sort_keys=True) if args.json else "found: false")
    return 0 if records or not args.strict else 1


def cmd_export(args: argparse.Namespace) -> int:
    paths: list[pathlib.Path]
    if args.repo:
        paths = [records_path(args.store, args.repo)]
    else:
        paths = sorted((args.store / "records").glob("*.jsonl"))
    cutoff = None
    if args.since_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.since_days)
    records = []
    for path in paths:
        for record in iter_records(path):
            if cutoff is not None:
                ts = parse_utc(str(record.get("completed_at") or record.get("recorded_at") or ""))
                if ts is not None and ts < cutoff:
                    continue
            records.append(record)
    if args.format == "json":
        print(json.dumps(records, sort_keys=True))
    else:
        for record in records:
            print(json.dumps(record, sort_keys=True, separators=(",", ":")))
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    records = list(iter_records(records_path(args.store, args.repo)))
    records.sort(key=lambda item: str(item.get("completed_at") or item.get("recorded_at") or ""), reverse=True)
    records = records[: max(args.limit, 0)]
    if args.json:
        print(json.dumps({"repo": args.repo, "records": records}, sort_keys=True))
    elif not records:
        print("No runtime records found.")
    else:
        rows = [["completed_at", "provider", "job", "status", "total_s", "runner"]]
        for record in records:
            rows.append(
                [
                    str(record.get("completed_at") or ""),
                    str(record.get("provider") or ""),
                    str(record.get("job") or record.get("job_id") or ""),
                    str(record.get("status") or ""),
                    f"{(record.get('total_ms', 0) or 0) / 1000:.1f}",
                    str(record.get("runner_name") or ""),
                ]
            )
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        for row in rows:
            print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    count = 0
    for root in args.timing:
        records, warnings = collect([root])
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        for timing in records:
            ns = argparse.Namespace(
                store=args.store,
                source="timing.tsv.backfill",
                project=args.project,
                repo=args.repo,
                workflow=args.workflow,
                job=timing.runner,
                provider=f"tart-{timing.provider}" if timing.provider in {"linux", "macos"} else "qemu-windows",
                host="",
                platform=timing.provider,
                arch=args.arch,
                runner_name=timing.runner,
                vm_name=timing.runner,
                labels="",
                run_id="",
                job_id="",
                attempt="",
                external_id="",
                queued_at="",
                started_at="",
                completed_at=dt.datetime.fromtimestamp(timing.path.stat().st_mtime, dt.timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                status="unknown",
                exit_code=0,
                timed_out=False,
                failure_class="unknown",
                github_conclusion="",
                cache_mode="unknown",
                cache_mode_source="unknown",
                cache_hit=None,
                ccache_hit_pct=None,
                cache_restore_ms=None,
                cache_save_ms=None,
                cpu_count=None,
                ram_mb=None,
                tags=args.tags,
                log_dir=str(timing.path.parent),
                golden="",
                golden_hash=False,
                timing_path=str(timing.path),
                json=True,
                gh_enrich=False,
                gh_timeout=15,
            )
            record = record_from_args(ns)
            append_jsonl_locked(records_path(args.store, args.repo), record)
            count += 1
    for metrics_file in args.metrics_jsonl:
        with metrics_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    source = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(source, dict):
                    continue
                record = {
                    "schema_version": SCHEMA_VERSION,
                    "source": "metrics.jsonl.backfill",
                    "repo": args.repo,
                    "project": args.project or args.repo.split("/")[-1],
                    "provider": source.get("provider", "unknown"),
                    "platform": source.get("os", ""),
                    "arch": source.get("arch", ""),
                    "cache_mode": source.get("mode", "unknown"),
                    "cache_mode_source": "metrics.jsonl",
                    "ccache_hit_pct": source.get("ccache_hit_pct"),
                    "status": "unknown",
                    "failure_class": "unknown",
                    "completed_at": source.get("ts", ""),
                    "recorded_at": utc_now(),
                    "tags": split_csv(args.tags),
                }
                for source_key, target_key in (("build_s", "run_ms"), ("ctest_s", "test_ms"), ("configure_s", "setup_ms")):
                    if source.get(source_key) is not None:
                        record[target_key] = int(round(float(source[source_key]) * 1000))
                append_jsonl_locked(records_path(args.store, args.repo), record)
                count += 1
    print(json.dumps({"backfilled": count}, sort_keys=True))
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    root = args.store / "records"
    if not root.exists():
        print(json.dumps({"removed": 0}, sort_keys=True))
        return 0
    cutoff = None
    if args.keep_days is not None:
        cutoff = time.time() - (args.keep_days * 24 * 60 * 60)
    removed = 0
    for path in sorted(root.glob("*.jsonl")):
        records = list(iter_records(path))
        if cutoff is not None:
            records = [
                record
                for record in records
                if (parse_utc(str(record.get("completed_at") or record.get("recorded_at") or "")) or dt.datetime.now(dt.timezone.utc)).timestamp()
                >= cutoff
            ]
        if args.keep is not None and len(records) > args.keep:
            records = records[-args.keep :]
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        removed += max(0, sum(1 for _ in iter_records(path)) - len(records))
        os.replace(tmp, path)
    if args.prune_summaries:
        summaries = args.store / "summaries/by-run"
        if summaries.exists() and args.keep_days is not None:
            for path in summaries.rglob("*.summary.json"):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            for path in sorted(summaries.rglob("*"), reverse=True):
                if path.is_dir():
                    try:
                        path.rmdir()
                    except OSError:
                        pass
    print(json.dumps({"removed": removed}, sort_keys=True))
    return 0


def add_record_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--project")
    parser.add_argument("--workflow", default="")
    parser.add_argument("--job", default="")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--host", default="")
    parser.add_argument("--platform", default="")
    parser.add_argument("--arch", default="")
    parser.add_argument("--runner-name", default="")
    parser.add_argument("--vm-name", default="")
    parser.add_argument("--labels", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--attempt", default="")
    parser.add_argument("--external-id", default="")
    parser.add_argument("--queued-at", default="")
    parser.add_argument("--started-at", default="")
    parser.add_argument("--completed-at", default="")
    parser.add_argument("--status", choices=["pass", "fail", "unknown"], default="")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--timed-out", action="store_true")
    parser.add_argument("--failure-class", choices=sorted(FAILURE_CLASSES), default="")
    parser.add_argument("--github-conclusion", default="")
    parser.add_argument("--cache-mode", choices=["cold", "warm", "unknown"], default="unknown")
    parser.add_argument("--cache-mode-source", default="unknown")
    parser.add_argument("--cache-hit")
    parser.add_argument("--ccache-hit-pct", type=float)
    parser.add_argument("--cache-restore-ms", type=int)
    parser.add_argument("--cache-save-ms", type=int)
    parser.add_argument("--cpu-count", type=int)
    parser.add_argument("--ram-mb", type=int)
    parser.add_argument("--golden", default="")
    parser.add_argument("--golden-hash", action="store_true", default=os.environ.get("TARTCI_RUNTIME_GOLDEN_HASH") == "1")
    parser.add_argument("--tags", default="")
    parser.add_argument("--log-dir", default="")
    parser.add_argument("--timing-path", default="")
    parser.add_argument("--source", default="runner")
    parser.add_argument("--gh-enrich", action="store_true", default=os.environ.get("TARTCI_RUNTIME_GH_ENRICH") == "1")
    parser.add_argument("--gh-timeout", type=int, default=int(os.environ.get("TARTCI_GH_TIMEOUT_SECS", "15")))
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record and query tartci runtime measurements")
    parser.add_argument("--store", type=pathlib.Path, default=default_store())
    sub = parser.add_subparsers(dest="cmd", required=True)

    complete = sub.add_parser("complete", help="append a completed job record")
    add_record_args(complete)
    complete.set_defaults(func=cmd_complete)

    summary = sub.add_parser("summary", help="read completion summary by repo/run/job")
    summary.add_argument("--repo", required=True)
    summary.add_argument("--run-id", required=True)
    summary.add_argument("--job-id")
    summary.add_argument("--json", action="store_true")
    summary.add_argument("--strict", action="store_true")
    summary.set_defaults(func=cmd_summary)

    export = sub.add_parser("export", help="export records as JSONL or JSON")
    export.add_argument("--repo")
    export.add_argument("--since-days", type=int)
    export.add_argument("--format", choices=["jsonl", "json"], default="jsonl")
    export.set_defaults(func=cmd_export)

    recent = sub.add_parser("recent", help="show recent records for a repo")
    recent.add_argument("--repo", required=True)
    recent.add_argument("--limit", type=int, default=20)
    recent.add_argument("--json", action="store_true")
    recent.set_defaults(func=cmd_recent)

    backfill = sub.add_parser("backfill", help="import existing timing.tsv or metrics.jsonl history")
    backfill.add_argument("--repo", required=True)
    backfill.add_argument("--project")
    backfill.add_argument("--workflow", default="")
    backfill.add_argument("--arch", default="")
    backfill.add_argument("--tags", default="")
    backfill.add_argument("--timing", action="append", type=pathlib.Path, default=[])
    backfill.add_argument("--metrics-jsonl", action="append", type=pathlib.Path, default=[])
    backfill.set_defaults(func=cmd_backfill)

    prune = sub.add_parser("prune", help="prune old records")
    prune.add_argument("--keep-days", type=int)
    prune.add_argument("--keep", type=int)
    prune.add_argument("--prune-summaries", action="store_true")
    prune.set_defaults(func=cmd_prune)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.store = args.store.expanduser()
    try:
        return args.func(args)
    except BrokenPipeError:
        return 0
    except OSError as exc:
        print(f"runtime store error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
