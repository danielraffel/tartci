#!/usr/bin/env python3
"""tartci update: bring this host's tartci current, and say where that stands.

    tartci update             explain, show the plan, ask y/N, then apply
    tartci update --yes       apply without asking (agents, scripts)
    tartci update --plan      the plan only
    tartci update --status    one line: version, skew, the live step, last result
    tartci update --wait      print nothing until this host's update ends, then
                              one line; the exit code is the outcome. Run it in
                              the background to be woken when an update finishes.
    tartci update --verify | --pause REASON | --resume | --clear-halt | ...
                              passed to the update engine unchanged

It updates THIS host only. Every host also updates itself every 30 minutes,
and hosts take turns: one at a time, running jobs finish first, and a failed
install rolls back. --status, --wait and --json are built to be read cheaply by
agents; --verbose prints the engine's full output.

Exit codes: 0 current, updated or would update; 3 refused or deferred (nothing
was touched); 4 failed (see the receipt); 5 unknown; 124 --wait timed out.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_self_update as engine  # noqa: E402

EXIT_TIMED_OUT = 124
OUTCOME_EXIT = {"succeeded": 0, "refused": engine.EXIT_REFUSED,
                "failed": engine.EXIT_FAILED, "rolled_back": engine.EXIT_FAILED}


def _short(commit: object) -> str:
    return str(commit or "?")[:7]


def last_attempt(state_dir: Path) -> dict | None:
    """The newest finished apply attempt."""
    for _, value in reversed(engine._attempts(state_dir)):
        if value.get("mode") == "apply" and value.get("status") != "running":
            return value
    return None


def snapshot(state_dir: Path, now: float | None = None) -> dict:
    skew = engine._read_json(state_dir / "skew.json") or {}
    last = last_attempt(state_dir)
    live = engine.progress(state_dir, now)
    return {
        "installed": skew.get("installed"),
        "main": skew.get("main"),
        "behind": skew.get("behind"),
        "state": skew.get("state") or "unknown",
        "stale": bool(skew.get("stale")),
        "measured_at": skew.get("measured_at"),
        "updating": live,
        "last": ({"status": last.get("status"), "target": last.get("target"),
                  "at": last.get("finished_at") or last.get("started_at"),
                  "error": str(last.get("error") or "")[:300]} if last else None),
        "waiting": engine.waiting_line(state_dir, now),
        "paused": engine.scheduled_pause(state_dir) or engine.paused_line(state_dir, now),
        "halted": engine.halt_reason(state_dir),
    }


def one_line(value: dict) -> str:
    parts = [f"tartci {_short(value['installed'])}"]
    state = value["state"]
    if state == "current":
        parts[0] += " current"
    elif value["behind"] is not None:
        parts[0] += (f" {value['behind']} behind main {_short(value['main'])}"
                     + {"soaking": " (soaking)", "unverified": " (main not green)"}.get(state, "")
                     + (" STALE" if value["stale"] else ""))
    else:
        parts[0] += f" skew {state}"
    live = value["updating"]
    if live and live["stale"]:
        parts.append(f"STALE update to {_short(live['target'])} (process gone)")
    elif live:
        step = live["step"] or "starting"
        detail = live["detail"].split(";")[0][:90]
        parts.append(f"UPDATING to {_short(live['target'])}: {step} {live['step_minutes']}m"
                     + (f" ({detail})" if detail else ""))
    for key in ("halted", "paused", "waiting"):
        if value[key]:
            parts.append(str(value[key]).removeprefix("self-update: ")[:120])
    last = value["last"]
    if last:
        parts.append(f"last {last['status']} {_short(last['target'])} {last['at']}"
                     + (f": {last['error'][:120]}" if last["status"] != "succeeded"
                        and last["error"] else ""))
    return " | ".join(parts)


def exit_for(value: dict) -> int:
    last = value["last"]
    return OUTCOME_EXIT.get(str(last.get("status")), 0) if last else 0


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_engine(argv: list[str], *, quiet: bool) -> tuple[int, str]:
    """Run the update engine; with quiet, keep its output and return it."""
    if not quiet:
        return engine.main(argv), ""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = engine.main(argv)
    return rc, out.getvalue()


def keep_log(text: str, state_dir: Path) -> Path:
    path = state_dir / "last-update-output.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def plan_summary(text: str) -> list[str]:
    """The lines of a plan that decide it: skew, source and the verdict."""
    keep = ("tartci:", "fleet source", "plan:", "REFUSED", "DEFERRED", "UNKNOWN",
            "nothing to do", "same-generation")
    return [line for line in text.splitlines()
            if line.startswith("self-update:") and any(word in line for word in keep)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tartci update", description=__doc__.split("\n\n")[0].split("\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--wait", action="store_true")
    mode.add_argument("--plan", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true", help="apply without asking")
    parser.add_argument("--json", action="store_true", help="with --status or --wait")
    parser.add_argument("--timeout", type=float, default=4 * 3600,
                        help="--wait gives up after this many seconds (default 4 h)")
    parser.add_argument("--poll", type=float, default=15.0, help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help="print the engine's full output")
    parser.add_argument("--home", type=Path, default=Path.home())
    args, passthrough = parser.parse_known_args(argv)
    state_dir = engine.state_dir_for(args.home)
    home = ["--home", str(args.home)]

    if passthrough:  # --verify, --pause, --resume, --clear-halt, --target, ...
        if args.status or args.wait or args.plan or args.yes:
            parser.error(f"{' '.join(passthrough)} cannot be combined with "
                         "--status, --wait, --plan or --yes")
        return engine.main([*passthrough, *home])

    if args.status or args.wait:
        if args.wait:
            deadline = time.monotonic() + args.timeout
            while engine.progress(state_dir) and not engine.progress(state_dir)["stale"]:
                if time.monotonic() >= deadline:
                    value = snapshot(state_dir)
                    print(json.dumps(value) if args.json else
                          "timed out waiting: " + one_line(value))
                    return EXIT_TIMED_OUT
                time.sleep(args.poll)
        value = snapshot(state_dir)
        print(json.dumps(value, sort_keys=True) if args.json else one_line(value))
        return exit_for(value) if args.wait else 0

    rc, text = run_engine(["--plan", *home], quiet=not args.verbose)
    if not args.verbose:
        for line in plan_summary(text):
            print(line)
    would_apply = rc == 0 and "self-update: plan: would" in text
    if args.plan or not would_apply:
        if not would_apply and not args.verbose:
            print(f"tartci update: nothing applied ({one_line(snapshot(state_dir))})")
        return rc
    if not args.yes:
        if not interactive():
            print("tartci update: not applied: no terminal to ask; pass --yes to apply")
            return 0
        print("tartci update: this updates THIS host only: it waits its turn behind other "
              "hosts, lets running jobs finish, then installs and verifies, rolling back "
              "on failure.")
        if input("Apply now? [y/N] ").strip().lower() not in ("y", "yes"):
            print("tartci update: not applied")
            return 0
    rc, text = run_engine(["--apply", *home], quiet=not args.verbose)
    value = snapshot(state_dir)
    tail = "" if args.verbose else f" (full output: {keep_log(text, state_dir)})"
    print(one_line(value) + tail)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
