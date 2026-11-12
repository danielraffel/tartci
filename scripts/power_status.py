#!/usr/bin/env python3
"""Whether this host is configured to stay awake on AC power.

A Mac that sleeps when its display does takes its runners, SSH and lease store
with it: m5studio ran for days with System Settings > Energy > "Prevent
automatic sleeping when the display is off" left OFF and spent 4-6 hours a day
asleep, waking only for maintenance, while every launchd and pool view
reported it healthy.

Read from pmset -g custom (the configured AC Power profile), never from
pmset -g: the live view prints sleep 1 (sleep prevented by ...) while some
app holds an assertion, which reads as awake and is not. The host sleeps as
soon as that app lets go.

States:
  ok           AC sleep is 0: the host never sleeps on its own
  sleeps       AC sleep is N > 0 minutes
  unknown      no AC Power profile could be read (not macOS, or pmset failed)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Bound at import: callers that mock subprocess.run for their own commands
# (fleet readiness tests script every call) must not have pmset consume them.
_RUN = subprocess.run

# How the system says it went to sleep: idle sleep, and the maintenance sleeps
# between the brief dark wakes that follow. Counted from powerd's own log.
SLEEP_PREDICATE = 'process == "powerd" AND eventMessage CONTAINS "Entering Sleep state"'
SLEEP_MARKER = "Entering Sleep state"
# The launchd watchdog refreshes the count every pass (5 min); an older reading
# is reported as stale rather than as a count.
SLEEP_CACHE_FRESH_SECS = 30 * 60


def sleep_cache_path() -> Path:
    home = os.environ.get("TARTCI_HOME") or str(Path.home() / ".tartci")
    return Path(home) / "state" / "power" / "sleep-events.json"


def count_sleep_events(log_text: str) -> int:
    return sum(1 for line in log_text.splitlines() if SLEEP_MARKER in line)


def refresh_sleep_events(path: Path | None = None, now: float | None = None,
                         run=None) -> dict[str, Any]:
    """Count sleeps in the last hour and cache the result. Never raises.

    log show takes seconds, too slow for every pool status, so the
    watchdog's heal pass runs this and status reads the cache.
    """
    path = path or sleep_cache_path()
    now = time.time() if now is None else now
    run = run or _RUN
    try:
        proc = run(["/usr/bin/log", "show", "--last", "1h", "--style", "compact",
                    "--predicate", SLEEP_PREDICATE],
                   capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise OSError(f"log show exit {proc.returncode}: {(proc.stderr or '')[-200:]}")
        value: dict[str, Any] = {"count": count_sleep_events(proc.stdout), "window_hours": 1,
                                 "measured_at": now}
    except (OSError, subprocess.SubprocessError) as exc:
        value = {"count": None, "error": str(exc)[:300], "measured_at": now}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        tmp.write_text(json.dumps(value))
        os.replace(tmp, path)
    except OSError:
        pass
    return value


def cached_sleep_events(path: Path | None = None, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    try:
        value = json.loads((path or sleep_cache_path()).read_text())
    except (OSError, ValueError):
        return {"state": "unmeasured"}
    age = now - float(value.get("measured_at") or 0)
    if age > SLEEP_CACHE_FRESH_SECS:
        return {"state": "stale", "age_secs": int(age)}
    if value.get("count") is None:
        return {"state": "unknown", "error": value.get("error")}
    return {"state": "measured", "count": int(value["count"]), "age_secs": int(age)}


def parse_custom(text: str) -> dict[str, int]:
    """Return the integer settings of the AC Power: section of pmset -g custom."""
    section: str | None = None
    values: dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if not line[0].isspace():
            section = line.rstrip(":").strip()
            continue
        if section != "AC Power":
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("-").isdigit():
            values[" ".join(parts[:-1])] = int(parts[-1])
    return values


def status(custom_text: str | None = None) -> dict[str, Any]:
    if custom_text is None:
        try:
            custom_text = _RUN(
                ["pmset", "-g", "custom"], capture_output=True, text=True,
                timeout=10, check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            custom_text = ""
    values = parse_custom(custom_text)
    sleep = values.get("sleep")
    events = cached_sleep_events()
    if sleep is None:
        return {"state": "unknown", "sleep_minutes": None, "sleep_events": events}
    return {
        "state": "ok" if sleep == 0 else "sleeps",
        "sleep_minutes": sleep,
        "autorestart": values.get("autorestart"),
        "sleep_events": events,
    }


def events_text(value: dict[str, Any]) -> str:
    events = value.get("sleep_events") or {}
    if events.get("state") == "measured":
        return f"; {events['count']} sleep(s) in the last hour"
    if events.get("state") == "stale":
        return f"; sleep count STALE ({events['age_secs'] // 60} min old)"
    if events.get("state") == "unknown":
        return f"; sleep count unknown ({events.get('error')})"
    return ""


def slept_recently(value: dict[str, Any]) -> bool:
    events = value.get("sleep_events") or {}
    return events.get("state") == "measured" and events.get("count", 0) > 0


def describe(value: dict[str, Any]) -> str:
    state = value.get("state")
    if state == "ok" and slept_recently(value):
        return ("power: WARN never sleeps on AC by setting, but the host slept"
                + events_text(value))
    if state == "ok":
        return "power: ok (never sleeps on AC)" + events_text(value)
    if state == "sleeps":
        return (f"power: WARN sleeps after {value['sleep_minutes']} min idle on AC; runners, SSH "
                "and leases go with it (System Settings > Energy > Prevent automatic sleeping "
                "when the display is off)" + events_text(value))
    return "power: unknown (no AC Power profile from pmset)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="power_status")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    value = status()
    print(json.dumps(value, sort_keys=True) if args.json else describe(value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
