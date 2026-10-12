#!/usr/bin/env python3
"""Periodic, report-only runner for the cross-host build disagreement alarm.

The launchd watchdog (tartci launchd heal, every 5 minutes) calls cycle
once per pass. This module decides whether the detector is due, runs it
bounded, and turns its findings into log lines for the watchdog log. It never
resets a cache, never touches scheduling and never fails the watchdog: every
detector outcome, including a crash or a timeout, is reported and absorbed.

Rules:

  enabled   only when the host's installed fleet profile carries
            `[build_disagreement] enabled = true` (the detector's own key).
            Absent, false, or unreadable is "disabled" and nothing runs.
  cadence   at most once per `interval_minutes` (default and floor: 15),
            whatever the watchdog's own interval.
  budgets   the profile's `max_api_calls` / `max_log_fetches` are passed to
            the detector, capped at the detector's own defaults, and the whole
            run is killed after `timeout_seconds` (default 240).
  outcome   exit 0 and 1 are read from the detector's JSON; exit 3 (GitHub
            unreadable), any other exit, a timeout or unparseable output is
            `unknown`. None of them is a watchdog failure.
  dedup     one ALARM per (flagged host, error fingerprint). A pair that keeps
            being reported is not re-sent; it alarms again only after it has
            not been seen for `realert_hours` (default 24), i.e. a new
            incident after a reset.
  log CLI   job logs need a plain `gh` (see TARTCI_GH_LOG_CLI). An explicit
            TARTCI_GH_LOG_CLI is used when it answers `--version` as gh;
            otherwise the Homebrew/usr-local gh and then every `gh` on PATH
            are probed. Wrappers that do not answer as gh are skipped. None
            found is `unknown` (log_cli_unavailable) and the detector is not
            run, because without logs it can only ever say unknown.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - launchd hosts run this under 3.11+
    tomllib = None  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
DETECTOR = HERE / "build_disagreement.py"

MIN_INTERVAL_MINUTES = 15
DEFAULT_TIMEOUT_S = 240
DEFAULT_REALERT_HOURS = 24
PRUNE_AFTER_S = 7 * 86400
# The detector's own budgets are the ceiling for an unattended run.
DETECTOR_MAX_API_CALLS = 200
DETECTOR_MAX_LOG_FETCHES = 12
PLAIN_GH_CANDIDATES = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")
HUMAN_ONLY = ("report only: the watchdog never resets a cache or changes scheduling; "
              "a human runs the remedy on the flagged host")

Runner = Callable[..., subprocess.CompletedProcess]


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_state_path() -> Path:
    home = os.environ.get("HOME", os.path.expanduser("~"))
    root = os.environ.get("TARTCI_HOME", os.path.join(home, ".tartci"))
    return Path(root) / "state" / "build-disagreement-watch.json"


# ── settings ───────────────────────────────────────────────────────────────


def load_settings(profile_file: Path) -> tuple[dict[str, Any] | None, str]:
    """The `[build_disagreement]` table when enabled, else (None, why)."""
    if tomllib is None:
        return None, "no tomllib (needs Python 3.11+)"
    try:
        with profile_file.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return None, "no installed fleet profile"
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"fleet profile unreadable: {exc}"
    table = data.get("build_disagreement")
    if not isinstance(table, dict) or table.get("enabled") is not True:
        return None, "[build_disagreement] enabled = true not set in the fleet profile"
    return table, "enabled"


def _number(table: dict[str, Any], key: str, default: float) -> float:
    value = table.get(key, default)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def detector_args(table: dict[str, Any]) -> list[str]:
    """Profile tuning as detector flags, budgets capped at the detector defaults."""
    out: list[str] = []
    for key, flag in (("repo", "--repo"), ("workflow", "--workflow")):
        if isinstance(table.get(key), str) and table[key]:
            out += [flag, table[key]]
    for key, flag in (("hours", "--hours"), ("streak", "--streak")):
        value = table.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            out += [flag, str(value)]
    for key, flag, cap in (("max_api_calls", "--max-api-calls", DETECTOR_MAX_API_CALLS),
                           ("max_log_fetches", "--max-log-fetches", DETECTOR_MAX_LOG_FETCHES)):
        out += [flag, str(max(1, min(int(_number(table, key, cap)), cap)))]
    return out


# ── log CLI ────────────────────────────────────────────────────────────────


def _answers_as_gh(path: str, runner: Runner) -> bool:
    try:
        proc = runner([path, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and (proc.stdout or "").startswith("gh version")


def resolve_log_cli(env: dict[str, str], runner: Runner) -> tuple[str | None, str]:
    explicit = env.get("TARTCI_GH_LOG_CLI")
    if explicit:
        if _answers_as_gh(explicit, runner):
            return explicit, "TARTCI_GH_LOG_CLI"
        return None, f"TARTCI_GH_LOG_CLI={explicit} does not answer as a plain gh"
    candidates = list(PLAIN_GH_CANDIDATES)
    for directory in (env.get("PATH") or "").split(os.pathsep):
        if directory:
            candidates.append(os.path.join(directory, "gh"))
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        seen.add(candidate)
        if _answers_as_gh(candidate, runner):
            return candidate, "probed"
    return None, "no plain gh found (set TARTCI_GH_LOG_CLI)"


# ── state ──────────────────────────────────────────────────────────────────


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError:
        pass  # an unwritable state file costs dedup, never the watchdog


def alarm_key(host: str, fingerprint: str) -> str:
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
    return f"{host}:{digest}"


# ── one cycle ──────────────────────────────────────────────────────────────


def run_detector(argv: list[str], timeout: float, runner: Runner, env: dict[str, str]
                 ) -> tuple[dict[str, Any], int | None]:
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"state": "unknown", "code": "detector_timeout",
                "detail": f"detector exceeded {timeout:g}s"}, None
    except OSError as exc:
        return {"state": "unknown", "code": "detector_unrunnable", "detail": str(exc)}, None
    try:
        result = json.loads(proc.stdout)
        if not isinstance(result, dict):
            raise ValueError("not an object")
    except ValueError:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
        return {"state": "unknown", "code": "detector_output_unreadable",
                "detail": f"exit {proc.returncode}: {tail[0][:200]}"}, proc.returncode
    if proc.returncode not in (0, 1):
        # 3 = GitHub unreadable; anything else is a detector fault. Neither
        # is evidence about a host, and neither fails the watchdog.
        # Findings from a run that did not finish cleanly are not evidence.
        result.setdefault("code", f"detector_exit_{proc.returncode}")
        result["state"] = "unknown"
        result["findings"] = []
    return result, proc.returncode


def cycle(profile_file: Path, state_path: Path, *, now: float | None = None,
          runner: Runner = subprocess.run, env: dict[str, str] | None = None,
          python: str | None = None, replay: Sequence[str] = (),
          force: bool = False) -> dict[str, Any]:
    now = time.time() if now is None else now
    env = dict(os.environ if env is None else env)
    table, why = load_settings(profile_file)
    if table is None:
        return {"state": "disabled", "reason": why, "ran": False}

    state = load_state(state_path)
    interval_s = max(MIN_INTERVAL_MINUTES, _number(table, "interval_minutes", MIN_INTERVAL_MINUTES)) * 60
    last = state.get("last_run_at")
    if not force and isinstance(last, (int, float)) and now - last < interval_s:
        return {"state": "skipped", "ran": False, "next_due": _iso(last + interval_s),
                "last_state": state.get("last_state")}

    report: dict[str, Any] = {"ran": False, "alarms": [], "deduplicated": []}
    argv = [python or sys.executable, str(DETECTOR), "--enable", "--json", *detector_args(table)]
    if replay:
        argv += list(replay)
    else:
        log_cli, source = resolve_log_cli(env, runner)
        if log_cli is None:
            state.update(last_run_at=now, last_state="unknown", last_code="log_cli_unavailable")
            save_state(state_path, state)
            report.update(state="unknown", code="log_cli_unavailable", detail=source)
            return report
        argv += ["--log-cli", log_cli]
        report["log_cli"] = log_cli

    timeout = _number(table, "timeout_seconds", DEFAULT_TIMEOUT_S)
    result, rc = run_detector(argv, timeout, runner, env)
    report.update(ran=True, exit_code=rc, state=result.get("state", "unknown"),
                  code=result.get("code"), detail=result.get("detail"),
                  jobs_considered=result.get("jobs_considered"),
                  hosts_seen=result.get("hosts_seen"), api_calls=result.get("api_calls"),
                  log_fetches=result.get("log_fetches"))

    realert_s = _number(table, "realert_hours", DEFAULT_REALERT_HOURS) * 3600
    alarms: dict[str, Any] = state.get("alarms") if isinstance(state.get("alarms"), dict) else {}
    for finding in result.get("findings") or []:
        if not isinstance(finding, dict) or finding.get("state") != "problem":
            continue
        host = str(finding.get("host"))
        fingerprint = str((finding.get("log") or {}).get("fingerprint") or finding.get("code"))
        key = alarm_key(host, fingerprint)
        prior = alarms.get(key) if isinstance(alarms.get(key), dict) else None
        entry = {"host": host, "rule": finding.get("rule"), "commit": finding.get("commit"),
                 "fingerprint": fingerprint,
                 "failing_url": (finding.get("failing_job") or {}).get("html_url"),
                 "green_url": (finding.get("green_counterpart") or {}).get("html_url"),
                 "remedy": finding.get("remedy")}
        if prior and now - float(prior.get("last_seen_at", 0)) <= realert_s:
            prior["last_seen_at"] = now
            report["deduplicated"].append({"key": key, **entry,
                                           "first_alarmed_at": _iso(prior["alarmed_at"])})
            continue
        alarms[key] = {**entry, "alarmed_at": now, "last_seen_at": now}
        report["alarms"].append({"key": key, **entry})
    state["alarms"] = {k: v for k, v in alarms.items()
                       if isinstance(v, dict) and now - float(v.get("last_seen_at", 0)) < PRUNE_AFTER_S}
    state.update(last_run_at=now, last_state=report["state"], last_code=report.get("code"),
                 runs=int(state.get("runs") or 0) + 1)
    save_state(state_path, state)
    return report


def render(report: dict[str, Any], now: float) -> list[str]:
    """Watchdog log lines. Disabled and not-yet-due cycles print nothing."""
    ts = _iso(now)
    if report["state"] in ("disabled", "skipped"):
        return []
    if not report.get("ran"):
        return [f"{ts} build-disagreement: unknown ({report.get('code')}: {report.get('detail')}); "
                "detector not run"]
    reason = f" code={report['code']}" if report.get("code") else ""
    lines = [f"{ts} build-disagreement: ran state={report['state']}{reason} "
             f"exit={report.get('exit_code')} jobs={report.get('jobs_considered')} "
             f"hosts={','.join(report.get('hosts_seen') or []) or '-'} "
             f"api_calls={report.get('api_calls')} log_fetches={report.get('log_fetches')} "
             f"alarms={len(report['alarms'])} deduplicated={len(report['deduplicated'])}"]
    if report["state"] == "unknown" and report.get("detail"):
        lines[0] += f" detail={str(report['detail'])[:200]}"
    for alarm in report["alarms"]:
        lines.append(f"{ts} build-disagreement: ALARM host={alarm['host']} rule={alarm['rule']} "
                     f"commit={alarm['commit']} failing={alarm['failing_url']} "
                     f"green={alarm['green_url']} fingerprint={alarm['fingerprint'][:240]}")
        lines.append(f"{ts} build-disagreement:   remedy: {alarm['remedy']} ({HUMAN_ONLY})")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build_disagreement_watch.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("cycle", "status"))
    ap.add_argument("--profile-file", type=Path,
                    default=Path.home() / ".config" / "tartci" / "macos-fleet-profile.toml")
    ap.add_argument("--state", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the interval (not the enable key)")
    ap.add_argument("--replay-jobs", type=Path, help="detector --from-jobs (offline replay)")
    ap.add_argument("--replay-logs", type=Path, help="detector --logs-dir")
    ap.add_argument("--replay-now", help="detector --now")
    ap.add_argument("--clock", type=float, help="cycle time as epoch seconds (tests)")
    args = ap.parse_args(argv)
    state_path = args.state or default_state_path()
    now = time.time() if args.clock is None else args.clock
    if args.command == "status":
        state = load_state(state_path)
        print(json.dumps(state, indent=1, sort_keys=True) if args.json else
              f"build-disagreement: last={state.get('last_state', 'never')} "
              f"at={_iso(state['last_run_at']) if state.get('last_run_at') else '-'} "
              f"runs={state.get('runs', 0)} open_alarms={len(state.get('alarms') or {})}")
        return 0
    replay: list[str] = []
    if args.replay_jobs:
        replay += ["--from-jobs", str(args.replay_jobs)]
        if args.replay_logs:
            replay += ["--logs-dir", str(args.replay_logs)]
        if args.replay_now:
            replay += ["--now", args.replay_now]
    try:
        report = cycle(args.profile_file, state_path, now=now, replay=replay, force=args.force)
    except Exception as exc:  # noqa: BLE001 - never fail the watchdog
        report = {"state": "unknown", "ran": False, "code": "watch_error", "detail": repr(exc)}
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        for line in render(report, now):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
