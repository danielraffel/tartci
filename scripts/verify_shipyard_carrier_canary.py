#!/usr/bin/env python3
"""Check the carrier scheduler's inert-canary acceptance criteria on its host.

Each check prints PASS or FAIL with the evidence it read, and the exit status
is 0 only when every requested check passed. Checks:

  spawning    launchd is starting ticks, not merely holding the job: the
              `runs` count rises by at least three over the observation window
              and the health receipt's time moves.
  plans       over the plan ledger's window: no tick mutated, every proposal
              passes the structural negative controls (never conflicting,
              never a failed required check, never unapproved, never unarmed,
              never a head pushed after its removal), and, against a rulings
              file, no false positive and at least one true positive per
              required class.
  quarantine  a planted fixture tick, run by the installed scheduler, whose
              apply times out quarantines the controller; the next tick stays
              inert until the file is removed, and the intent names the
              unfinished action.
  lock        two ticks started together on the installed config: exactly one
              takes the scheduler lock.
  tokens      a tick started with planted GH_TOKEN and GITHUB_TOKEN reports
              that no child process saw either.
  rollback    the installed config is not live and the health receipt agrees.
  health      the newest terminal health receipt is green in the expected mode.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable

LABEL = "com.danielraffel.shipyard.steward-scheduler"
HOME = Path.home()
CONFIG = HOME / ".config/shipyard/steward-scheduler.json"
HEALTH = HOME / "Library/Logs/shipyard-steward-scheduler.health.json"
PLANS = HOME / "Library/Logs/shipyard-steward-scheduler.plans.jsonl"
LOCK = HOME / ".local/state/tartci/shipyard-steward-scheduler.lock"
ENTRYPOINT = HOME / ".local/bin/tartci"
FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"}


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def report(name: str, ok: bool, evidence: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {name}: {evidence}")
    return ok


# --- spawning ---------------------------------------------------------------


def launchd_runs(text: str) -> int | None:
    match = re.search(r"^\s*runs = (\d+)\s*$", text, re.MULTILINE)
    return int(match.group(1)) if match else None


def read_runs() -> tuple[int | None, str]:
    completed = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return launchd_runs(completed.stdout), completed.stdout


def health_time() -> str | None:
    try:
        return json.loads(HEALTH.read_text()).get("observed_at")
    except (OSError, json.JSONDecodeError):
        return None


def check_spawning(observe_seconds: int) -> bool:
    before, text = read_runs()
    if before is None:
        return report("spawning", False, "launchctl print shows no `runs` line; job not loaded")
    health_before = health_time()
    time.sleep(observe_seconds)
    after, _ = read_runs()
    health_after = health_time()
    delta = (after or 0) - before
    moved = health_after is not None and health_after != health_before
    return report(
        "spawning",
        delta >= 3 and moved,
        f"runs {before} -> {after} (delta {delta}) over {observe_seconds}s; "
        f"health observed_at {health_before} -> {health_after}",
    )


# --- plans ------------------------------------------------------------------


def ledger_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in sorted(path.parent.glob(path.name + ".*"), reverse=True) + [path]:
        if not candidate.exists() or not re.fullmatch(rf"{re.escape(path.name)}(\.\d+)?", candidate.name):
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def structural_violations(plan: dict[str, Any]) -> list[str]:
    """Why a recorded proposal breaks a negative control, from its own facts."""
    facts = plan.get("facts") or {}
    action = plan.get("action")
    problems = []
    if str(facts.get("merge_state", "")).upper() in {"DIRTY", "CONFLICTING"}:
        problems.append("proposed on a conflicting pull request")
    if not facts.get("approved_head"):
        problems.append("proposed on a head with no approval record")
    queue = facts.get("queue") or {}
    if queue.get("state") not in {"armed_not_queued", "ejected"}:
        problems.append(f"proposed while queue state is {queue.get('state')}")
    if queue.get("state") == "ejected" and queue.get("new_head_since"):
        problems.append("proposed on a head pushed after its removal")
    failed = [
        row.get("context")
        for row in facts.get("required") or []
        if str(row.get("conclusion") or "").upper() in FAILED_CONCLUSIONS
    ]
    if failed and action in {"redispatch", "rearm"}:
        problems.append(f"proposed beside failed required checks {failed}")
    if plan.get("head_sha") != facts.get("head_sha"):
        problems.append("plan head differs from the facts' head")
    if action in {"rearm", "update_branch"} and plan.get("head") != plan.get("head_sha"):
        problems.append("action head differs from the current head")
    return problems


def compare_plans(
    rows: Iterable[dict[str, Any]], rulings: list[dict[str, Any]], require: list[str]
) -> dict[str, Any]:
    ruled = {
        (r["repo"].casefold(), int(r["number"]), r["head_sha"].lower()): r["class"] for r in rulings
    }
    ticks: set[str] = set()
    mutations: list[str] = []
    violations: list[str] = []
    false_positives: list[str] = []
    unruled: set[tuple[str, int, str, str]] = set()
    true_positives: dict[str, set[tuple[str, int, str]]] = {}
    proposed: set[tuple[str, int, str, str]] = set()
    for row in rows:
        ticks.add(str(row.get("tick")))
        if "intent" in row or "outcomes" in row:
            mutations.append(f"tick {row.get('tick')} wrote an intent or outcome")
        for plan in row.get("plans") or []:
            if "mutation" in plan:
                mutations.append(f"{row.get('repo')}#{plan.get('number')} reported a mutation")
            if plan.get("decision") != "propose":
                continue
            key = (str(row.get("repo")).casefold(), int(plan["number"]), str(plan["head_sha"]).lower())
            action = str(plan.get("action"))
            proposed.add((*key, action))
            for problem in structural_violations(plan):
                violations.append(f"{row.get('repo')}#{plan['number']} {action}: {problem}")
            ruling = ruled.get(key)
            if ruling is None:
                unruled.add((*key, action))
            elif ruling == action:
                true_positives.setdefault(action, set()).add(key)
            else:
                false_positives.append(
                    f"{row.get('repo')}#{plan['number']}@{key[2][:12]} proposed {action}, ruled {ruling}"
                )
    missed = sorted(
        f"{repo}#{number}@{head[:12]} ruled {klass}"
        for (repo, number, head), klass in ruled.items()
        if klass != "none" and (repo, number, head, klass) not in proposed
    )
    return {
        "ticks": len(ticks),
        "mutations": mutations,
        "violations": sorted(set(violations)),
        "false_positives": sorted(set(false_positives)),
        "unruled": sorted(f"{r}#{n}@{h[:12]} {a}" for r, n, h, a in unruled),
        "true_positives": {k: len(v) for k, v in sorted(true_positives.items())},
        "missing_classes": [klass for klass in require if not true_positives.get(klass)],
        "missed": missed,
    }


def check_plans(
    ledger: Path, rulings_path: Path | None, hours: float, require: list[str]
) -> bool:
    rows = ledger_rows(ledger)
    if not rows:
        return report("plans", False, f"no ledger rows in {ledger}")
    times = sorted(parse_time(str(row["tick"])) for row in rows if row.get("tick"))
    span = (times[-1] - times[0]).total_seconds() / 3600 if times else 0.0
    rulings = json.loads(rulings_path.read_text()) if rulings_path else []
    result = compare_plans(rows, rulings, require if rulings_path else [])
    ok = (
        span >= hours
        and not result["mutations"]
        and not result["violations"]
        and not result["false_positives"]
        and not result["missing_classes"]
        and (rulings_path is not None or not require)
    )
    print(json.dumps({"span_hours": round(span, 2), **result}, indent=2, sort_keys=True))
    return report(
        "plans",
        ok,
        f"{result['ticks']} ticks over {span:.1f}h (need {hours}h); "
        f"{len(result['mutations'])} mutations, {len(result['violations'])} control violations, "
        f"{len(result['false_positives'])} false positives, true positives {result['true_positives']}, "
        f"classes without a true positive {result['missing_classes']}",
    )


# --- fixture and live-tick checks ---------------------------------------------


FIXTURE_SHIPYARD = """#!/bin/sh
# A planted Shipyard: one re-arm proposal, and an apply that never finishes.
if [ "$*" = "--json runner carrier --replay /dev/null" ]; then
  printf '{"schema_version":1,"command":"runner.carrier","apply":false,"plans":[]}\\n'; exit 0
fi
case "$*" in
  *--apply*) sleep 30; exit 0 ;;
  "--json runner carrier --repo owner/fixture")
    printf '{"schema_version":1,"command":"runner.carrier","apply":false,"classes":[],"repos":[{"repo":"owner/fixture","base":"main","prs":[{"number":1,"head_sha":"%s","decision":"propose","action":"rearm","head":"%s","facts":{}}],"errors":[]}]}\\n' "$HEAD" "$HEAD"
    exit 0 ;;
esac
exit 97
"""


def fixture_tick(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    config = root / "config.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "mode": mode,
                "authority": mode == "live",
                "classes": ["rearm"] if mode == "live" else [],
                "shipyard": str((root / "shipyard").resolve()),
                "repositories": [{"repo": "owner/fixture", "checkout": str((root / "fixture").resolve())}],
                "carrier_timeout_seconds": 3,
                "max_log_bytes": 1024 * 1024,
                "log_generations": 2,
            }
        )
    )
    config.chmod(0o600)
    names = {name: str(root / f"fixture.{name}") for name in
             ("report", "health", "startup", "log", "plans", "intent", "lock", "quarantine")}
    return subprocess.run(
        ["/bin/bash", str(ENTRYPOINT), "steward-scheduler", "--config", str(config),
         *[item for name, value in names.items() for item in (f"--{name}", value)]],
        env=dict(os.environ, HEAD="f" * 40),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def check_quarantine(scratch: Path) -> bool:
    """Plant an apply that times out on the INSTALLED scheduler, in a fixture."""
    root = scratch / "quarantine"
    root.mkdir()
    shipyard = root / "shipyard"
    shipyard.write_text(FIXTURE_SHIPYARD)
    shipyard.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(root / "fixture")], check=True)
    subprocess.run(["git", "-C", str(root / "fixture"), "remote", "add", "origin",
                    "git@github.com:owner/fixture.git"], check=True)
    timed_out = fixture_tick(root, "live")
    fence = root / "fixture.quarantine"
    intent_path = root / "fixture.intent"
    intent = json.loads(intent_path.read_text()) if intent_path.exists() else {}
    named = [a.get("number") for a in intent.get("actions", [])] == [1] and "completed_at" not in intent
    later = fixture_tick(root, "plan")
    inert = later.returncode == 2 and fence.exists()
    fence.unlink(missing_ok=True)
    cleared = fixture_tick(root, "plan")
    health = json.loads((root / "fixture.health").read_text())
    ok = (
        timed_out.returncode == 1
        and named
        and inert
        and cleared.returncode == 0
        and health.get("status") == "healthy"
    )
    return report(
        "quarantine",
        ok,
        f"timed-out apply rc={timed_out.returncode}; intent names unfinished action={named}; "
        f"next tick rc={later.returncode} fenced={inert}; after removal rc={cleared.returncode} "
        f"health={health.get('status')}",
    )


def manual_tick(scratch: Path, name: str, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            "/bin/bash", str(ENTRYPOINT), "steward-scheduler",
            "--config", str(CONFIG),
            "--report", str(scratch / f"{name}.report.json"),
            "--health", str(scratch / f"{name}.health.json"),
            "--startup", str(scratch / f"{name}.startup.json"),
            "--log", str(scratch / f"{name}.log"),
            "--plans", str(scratch / f"{name}.plans.jsonl"),
            "--intent", str(scratch / f"{name}.intent.json"),
            "--lock", str(LOCK),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_for_free_lock(scratch: Path) -> bool:
    """Run probe ticks until one is not refused by the launchd tick's lock."""
    for _ in range(40):
        probe = manual_tick(scratch, "lockprobe", dict(os.environ))
        _, stderr = probe.communicate(timeout=900)
        if "another tick holds the scheduler lock" not in stderr:
            return True
        time.sleep(15)
    return False


def check_lock(scratch: Path) -> bool:
    if not wait_for_free_lock(scratch):
        return report("lock", False, "the scheduler lock never came free")
    first = manual_tick(scratch, "first", dict(os.environ))
    second = manual_tick(scratch, "second", dict(os.environ))
    outputs = [process.communicate(timeout=900) for process in (first, second)]
    refused = ["another tick holds the scheduler lock" in stderr for _, stderr in outputs]
    wrote = [(scratch / f"{name}.report.json").exists() for name in ("first", "second")]
    ok = sorted(refused) == [False, True] and sorted(wrote) == [False, True]
    return report("lock", ok, f"refused={refused} wrote_report={wrote}")


def check_tokens(scratch: Path) -> bool:
    if not wait_for_free_lock(scratch):
        return report("tokens", False, "the scheduler lock never came free")
    env = dict(os.environ, GH_TOKEN="planted-canary-token", GITHUB_TOKEN="planted-canary-token")
    process = manual_tick(scratch, "tokens", env)
    process.communicate(timeout=900)
    path = scratch / "tokens.report.json"
    if not path.exists():
        return report("tokens", False, "the planted-token tick wrote no report")
    value = json.loads(path.read_text())
    seen = value.get("child_environment_tokens")
    ok = seen == {"GH_TOKEN": False, "GITHUB_TOKEN": False} and value.get("status") == "healthy"
    return report("tokens", ok, f"child saw {seen}; tick status {value.get('status')}")


def check_rollback() -> bool:
    try:
        config = json.loads(CONFIG.read_text())
        health = json.loads(HEALTH.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return report("rollback", False, f"unreadable receipt: {error}")
    installed = dt.datetime.fromtimestamp(CONFIG.stat().st_mtime, dt.timezone.utc)
    observed = health.get("observed_at")
    fresh = bool(observed) and parse_time(observed) >= installed
    ok = (
        config.get("mode") in {"plan", "disabled"}
        and config.get("authority") is False
        and config.get("classes") == []
        and health.get("mode") == config.get("mode")
        and fresh
    )
    return report(
        "rollback",
        ok,
        f"config mode={config.get('mode')} authority={config.get('authority')} "
        f"classes={config.get('classes')}; health mode={health.get('mode')} at {observed} "
        f"(config written {installed.isoformat(timespec='seconds')})",
    )


def check_health(expected_mode: str) -> bool:
    try:
        health = json.loads(HEALTH.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return report("health", False, f"unreadable health receipt: {error}")
    ok = health.get("status") == "healthy" and health.get("mode") == expected_mode
    return report(
        "health",
        ok,
        f"status={health.get('status')} mode={health.get('mode')} "
        f"proposals={health.get('proposals')} mutations={health.get('mutations')} "
        f"at {health.get('observed_at')}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check",
        action="append",
        choices=["spawning", "plans", "quarantine", "lock", "tokens", "rollback", "health"],
        required=True,
    )
    parser.add_argument("--observe-seconds", type=int, default=3 * 300 + 60)
    parser.add_argument("--ledger", type=Path, default=PLANS)
    parser.add_argument("--rulings", type=Path)
    parser.add_argument("--hours", type=float, default=48.0)
    parser.add_argument("--require-class", action="append", default=[])
    parser.add_argument("--expect-mode", default="plan")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(dir=HOME, prefix=".carrier-canary-") as raw:
        scratch = Path(raw)
        for name in args.check:
            if name == "spawning":
                results.append(check_spawning(args.observe_seconds))
            elif name == "plans":
                results.append(check_plans(args.ledger, args.rulings, args.hours, args.require_class))
            elif name == "quarantine":
                results.append(check_quarantine(scratch))
            elif name == "lock":
                results.append(check_lock(scratch))
            elif name == "tokens":
                results.append(check_tokens(scratch))
            elif name == "rollback":
                results.append(check_rollback())
            elif name == "health":
                results.append(check_health(args.expect_mode))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
