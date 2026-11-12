#!/usr/bin/env python3
"""Dispatch a repository's frequent safety-net and daily workflows at their real cadence.

Why this exists
---------------
GitHub delays ``schedule`` events under load and drops the ones that pile up.
On Generous-Corp/pulp every hourly-or-faster cron fires about once every five
hours (2026-10-01..02: 7-8 runs per ``*/15``/``*/30`` workflow in 41 h, 82-164
expected), while daily crons fire five to seven hours late and sometimes not at
all (read-audit-nightly.yml, 2026-10-05). The merge-stall, runner-health,
release-reconcile, and similar watchdogs therefore run a fraction as often as
their crons promise, and a daily check counting consecutive days can lose one.
A daily workflow is listed with ``cadence_minutes`` 1440.

This agent closes the gap from outside GitHub's scheduler. Each tick it reads
the repository's ``.github/schedule-backstop.json`` manifest (which the
repository's own check keeps consistent with each workflow's cron, dispatch
inputs, concurrency group, and hosted runner) and calls ``workflow_dispatch`` on
a listed workflow only when

* the newest run of that workflow on the manifest ref, from ANY event, was
  created at least ``cadence_minutes`` ago, and
* that newest run is not still queued or running, and
* this agent has not itself dispatched it within ``cadence_minutes``.

The first rule makes it back off whenever a cron, push, or event trigger already
keeps the workflow fresh, and makes a second host's dispatch visible to this
one. The second keeps it from stacking runs behind hosted-runner saturation.
The third bounds it to one dispatch per cadence even if a read comes back stale.

It only dispatches: the workflows still run on GitHub-hosted runners, and the
cron stays in place, so with this agent off, failing, or its host down the
repository behaves exactly as before (fail open). An unreadable manifest stops
the whole tick; a failed read for one workflow skips only that workflow.

Safety: DRY-RUN by default (``TARTCI_BACKSTOP_APPLY=0``) and install on exactly
ONE always-on host. ``--apply`` also requires ``TARTCI_BACKSTOP_AUTHORITY=1``.

Modes
-----
  (default)      read state and report what would be dispatched; never dispatch
  --apply        dispatch due workflows (requires TARTCI_BACKSTOP_AUTHORITY=1)
  --json         machine-readable report on stdout

Tests: scripts/test_schedule_backstop.py (hermetic; no network).
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

SCHEMA_VERSION = 1
# A daily workflow (one cron firing a day) is listed with this cadence: the
# agent then dispatches it only when no run on the ref is a day old, so a daily
# check that counts consecutive days cannot lose one to a dropped cron.
DAILY_MINUTES = 1440
MANIFEST_PATH = ".github/schedule-backstop.json"
MANIFEST_REFRESH_SECS = 3600
ACTIVE_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}
DEFAULT_MAX_DISPATCHES = 20
COMMAND_TIMEOUT_SECS = 30
RUNS_PAGE = 20

GhCall = Callable[[List[str]], str]


class BackstopError(RuntimeError):
    """A GitHub read or the manifest is unusable; the tick must not dispatch."""


def parse_time(stamp: str) -> float:
    """Epoch seconds for GitHub's ``YYYY-MM-DDTHH:MM:SSZ`` timestamps."""
    try:
        parsed = dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError) as error:
        raise BackstopError(f"unparseable GitHub timestamp {stamp!r}") from error
    return parsed.replace(tzinfo=dt.timezone.utc).timestamp()


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_manifest(value: object) -> Dict[str, Any]:
    """Accept only the schema the repository's own check enforces."""
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise BackstopError(f"manifest schema_version must be {SCHEMA_VERSION}")
    if set(value) != {"schema_version", "ref", "workflows", "excluded"}:
        raise BackstopError("manifest must hold exactly schema_version, ref, workflows, excluded")
    ref = value["ref"]
    if not isinstance(ref, str) or not ref or ref.startswith("-") or " " in ref:
        raise BackstopError("manifest ref must be a plain branch name")
    rows = value["workflows"]
    if not isinstance(rows, list) or len(rows) > 64:
        raise BackstopError("manifest workflows must be a list of at most 64 rows")
    seen = set()
    workflows = []
    for row in rows:
        if not isinstance(row, dict) or not set(row).issubset(
            {"file", "cadence_minutes", "dispatch_inputs"}
        ) or not {"file", "cadence_minutes"}.issubset(row):
            raise BackstopError(
                "manifest workflow rows need file and cadence_minutes, with optional dispatch_inputs"
            )
        name, cadence = row["file"], row["cadence_minutes"]
        if (
            not isinstance(name, str)
            or not name.endswith((".yml", ".yaml"))
            or "/" in name
            or name.startswith(("-", "."))
            or name in seen
        ):
            raise BackstopError(f"manifest workflow {name!r} is not a unique bare workflow file")
        if type(cadence) is not int or not (1 <= cadence <= 60 or cadence == DAILY_MINUTES):
            raise BackstopError(f"{name}: cadence_minutes must be an integer in 1..60, or {DAILY_MINUTES}")
        dispatch_inputs = row.get("dispatch_inputs", {})
        if not isinstance(dispatch_inputs, dict) or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in dispatch_inputs.items()
        ):
            raise BackstopError(f"{name}: dispatch_inputs must map non-empty strings")
        seen.add(name)
        normalized = {"file": name, "cadence_minutes": cadence}
        if dispatch_inputs:
            normalized["dispatch_inputs"] = dict(dispatch_inputs)
        workflows.append(normalized)
    return {"ref": ref, "workflows": workflows}


def fetch_manifest(gh: GhCall, repo: str) -> Dict[str, Any]:
    raw = gh(["api", f"repos/{repo}/contents/{MANIFEST_PATH}"])
    try:
        envelope = json.loads(raw)
        content = base64.b64decode(envelope["content"]).decode("utf-8")
        return validate_manifest(json.loads(content))
    except (KeyError, TypeError, ValueError) as error:
        raise BackstopError(f"manifest {MANIFEST_PATH} is unreadable: {error}") from error


def newest_run(gh: GhCall, repo: str, workflow: str, ref: str) -> Optional[Dict[str, Any]]:
    """Newest run of ``workflow`` on ``ref``, from any event.

    The listing is read WITHOUT the server-side ``branch=`` filter and the ref
    is selected here. A cold ``branch=``-filtered read intermittently returns a
    page that is weeks old (measured on Generous-Corp/pulp: 2 of 6 cold reads),
    while the unfiltered listing of the same workflow was current every time.
    A stale page would read as "overdue" and dispatch a workflow that had just
    run.
    """
    raw = gh(["api", f"repos/{repo}/actions/workflows/{workflow}/runs?per_page={RUNS_PAGE}"])
    try:
        runs = json.loads(raw)["workflow_runs"]
    except (KeyError, TypeError, ValueError) as error:
        raise BackstopError(f"{workflow}: runs listing is malformed: {error}") from error
    if not isinstance(runs, list):
        raise BackstopError(f"{workflow}: runs listing is malformed")
    on_ref = [r for r in runs if isinstance(r, dict) and r.get("head_branch") == ref]
    if not on_ref:
        return None
    run = max(on_ref, key=lambda r: str(r.get("created_at")))
    if not isinstance(run.get("created_at"), str):
        raise BackstopError(f"{workflow}: newest run has no created_at")
    return run


def decide(run: Optional[Dict[str, Any]], cadence_secs: int, last_dispatch: Optional[float],
           now: float) -> Dict[str, Any]:
    """Pure decision for one workflow: dispatch, or why not, and when to look again."""
    if last_dispatch is not None and now - last_dispatch < cadence_secs:
        return {"action": "skip", "reason": "dispatched_recently",
                "next_check": last_dispatch + cadence_secs}
    if run is not None:
        created = parse_time(run["created_at"])
        if run.get("status") in ACTIVE_STATUSES:
            return {"action": "skip", "reason": f"newest_run_{run.get('status')}",
                    "next_check": now}
        if now - created < cadence_secs:
            return {"action": "skip", "reason": "fresh", "next_check": created + cadence_secs}
    return {"action": "dispatch", "reason": "stale" if run else "no_runs",
            "next_check": now + cadence_secs}


def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as source:
            value = json.load(source)
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION, "workflows": {}}
    except (OSError, ValueError):
        # A corrupt state file only costs extra reads; never let it stop a tick.
        return {"schema_version": SCHEMA_VERSION, "workflows": {}}
    if not isinstance(value, dict) or not isinstance(value.get("workflows"), dict):
        return {"schema_version": SCHEMA_VERSION, "workflows": {}}
    return value


def save_state(path: str, state: Dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".schedule-backstop.", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            json.dump(state, out, indent=2, sort_keys=True)
            out.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def tick(gh: GhCall, repo: str, state: Dict[str, Any], now: float, apply: bool,
         max_dispatches: int = DEFAULT_MAX_DISPATCHES) -> Dict[str, Any]:
    """One pass. Mutates ``state``; returns the report. Raises BackstopError on reads."""
    manifest = state.get("manifest")
    fetched = state.get("manifest_fetched_at", 0)
    if not isinstance(manifest, dict) or now - fetched >= MANIFEST_REFRESH_SECS:
        manifest = fetch_manifest(gh, repo)
        state["manifest"] = manifest
        state["manifest_fetched_at"] = now
    manifest = validate_manifest({**manifest, "schema_version": SCHEMA_VERSION, "excluded": []})
    ref = manifest["ref"]
    rows = state.setdefault("workflows", {})
    report: Dict[str, Any] = {"repo": repo, "ref": ref, "apply": apply, "observed_at": iso(now),
                              "workflows": []}
    dispatched = 0
    errors = 0
    for row in manifest["workflows"]:
        name, cadence_secs = row["file"], row["cadence_minutes"] * 60
        record = rows.setdefault(name, {})
        entry: Dict[str, Any] = {"file": name, "cadence_minutes": row["cadence_minutes"]}
        next_check = record.get("next_check")
        if isinstance(next_check, (int, float)) and now < next_check:
            entry.update(action="skip", reason="not_due")
            report["workflows"].append(entry)
            continue
        last = record.get("last_dispatch")
        last = last if isinstance(last, (int, float)) else None
        try:
            run = None
            if last is None or now - last >= cadence_secs:
                run = newest_run(gh, repo, name, ref)
            verdict = decide(run, cadence_secs, last, now)
            entry.update(action=verdict["action"], reason=verdict["reason"])
            if run is not None:
                entry["newest_run"] = {"created_at": run.get("created_at"),
                                       "event": run.get("event"), "status": run.get("status")}
            if verdict["action"] == "dispatch":
                if not apply:
                    entry["action"] = "would_dispatch"
                elif dispatched >= max_dispatches:
                    entry.update(action="skip", reason="tick_dispatch_cap")
                    report["workflows"].append(entry)
                    continue
                else:
                    dispatch = [
                        "api",
                        "-X",
                        "POST",
                        f"repos/{repo}/actions/workflows/{name}/dispatches",
                        "-f",
                        f"ref={ref}",
                    ]
                    for key, value in row.get("dispatch_inputs", {}).items():
                        dispatch.extend(["-f", f"inputs[{key}]={value}"])
                    gh(dispatch)
                    record["last_dispatch"] = now
                    dispatched += 1
            # Dry-run paces its reads exactly as apply would, so its log shows
            # one would_dispatch per cadence rather than one per tick.
            record["next_check"] = verdict["next_check"]
        except BackstopError as error:
            entry.update(action="error", reason=str(error))
            errors += 1
        report["workflows"].append(entry)
    report["dispatched"] = dispatched
    report["errors"] = errors
    return report


def resolve_gh(cli: str) -> str:
    if not cli:
        raise BackstopError("TARTCI_BACKSTOP_GH_CLI must name an explicit GitHub App wrapper")
    if os.path.basename(cli) == "gh":
        raise BackstopError("TARTCI_BACKSTOP_GH_CLI refuses ambient gh")
    resolved = cli if os.path.isabs(cli) else shutil.which(cli)
    if not resolved or not os.access(resolved, os.X_OK):
        raise BackstopError(f"TARTCI_BACKSTOP_GH_CLI is not executable: {cli}")
    return resolved


def make_gh(cli: str, cwd: str) -> GhCall:
    def call(args: List[str]) -> str:
        try:
            done = subprocess.run([cli, *args], capture_output=True, text=True, cwd=cwd,
                                  timeout=COMMAND_TIMEOUT_SECS)
        except subprocess.TimeoutExpired as error:
            raise BackstopError(f"{' '.join(args[:3])} timed out") from error
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip().splitlines()[-1:] or [""]
            raise BackstopError(f"{' '.join(args[:3])} failed: {detail[0][:300]}")
        return done.stdout
    return call


def main(argv: Optional[List[str]] = None) -> int:
    home = os.path.expanduser("~")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("TARTCI_BACKSTOP_REPO", "Generous-Corp/pulp"))
    parser.add_argument("--apply", action="store_true",
                        default=os.environ.get("TARTCI_BACKSTOP_APPLY") == "1")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--state", default=os.environ.get(
        "TARTCI_BACKSTOP_STATE", os.path.join(home, ".local/state/tartci/schedule-backstop.json")))
    parser.add_argument("--max-dispatches", type=int, default=DEFAULT_MAX_DISPATCHES)
    args = parser.parse_args(argv)

    def emit(report: Dict[str, Any], code: int) -> int:
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            print(f"{report.get('observed_at', iso(time.time()))} [schedule-backstop] "
                  f"status={report['status']} apply={args.apply} "
                  f"dispatched={report.get('dispatched', 0)}"
                  + (f" error={report['error']}" if report.get("error") else ""))
            for entry in report.get("workflows", []):
                if entry["action"] != "skip" or entry["reason"] not in ("not_due", "fresh"):
                    print(f"  {entry['file']}: {entry['action']} ({entry['reason']})")
        return code

    try:
        if args.apply and os.environ.get("TARTCI_BACKSTOP_AUTHORITY") != "1":
            raise BackstopError("--apply requires TARTCI_BACKSTOP_AUTHORITY=1 on exactly one host")
        if not 0 <= args.max_dispatches <= 64:
            raise BackstopError("--max-dispatches must be in 0..64")
        gh = make_gh(resolve_gh(os.environ.get("TARTCI_BACKSTOP_GH_CLI", "").strip()), home)
        state = load_state(args.state)
        try:
            report = tick(gh, args.repo, state, time.time(), args.apply, args.max_dispatches)
        finally:
            # Persist what this tick learned and dispatched even if a later
            # read failed, so a partial tick cannot re-dispatch next time.
            save_state(args.state, state)
        report["status"] = "degraded" if report["errors"] else "ok"
        return emit(report, 1 if report["errors"] else 0)
    except (BackstopError, OSError) as error:
        return emit({"status": "error", "error": str(error), "apply": args.apply}, 2)


if __name__ == "__main__":
    sys.exit(main())
