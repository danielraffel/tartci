#!/usr/bin/env python3
"""Prove, about hourly, that this host's CURRENT interpreters can still use the
VM store from launchd.

The install preflight (scripts/interpreter_preflight.py) proves a NEW lane
interpreter once, at switch time. A grant that is later revoked or reset (a
macOS update, a TCC reset, someone unticking a box) is not seen until a lane or
a pool-on hangs on its first write to the store, as on m5s on 2026-10-15, when
the host stayed out of the pool for hours. This check runs the same one-shot
launchd probe for every interpreter the host runs now: each installed fleet
lane's TARTCI_PYTHON and the host's python.pin (the watchdog, self-update and
pool-on run that one). Whatever those paths are, Homebrew Cellar or a
mise-installed Python, the probe launches exactly the path the lanes launch,
so the grant it exercises is the one the lanes need.

It is off unless ~/.config/tartci/interpreter-grant-check.enabled exists, so
it can be enabled one host at a time; it probes at most MAX_PROBES_PER_PASS
interpreters per run and records the probe time.

A failure records state/interpreter-grant-lost and opens one issue naming the
host, the binary and the grant to check. It never takes the host off: the
lanes may still be serving, and an operator decides. The record and the issue
clear on the next passing check.
"""
from __future__ import annotations

import json
import os
import plistlib
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import host_off  # noqa: E402 - sibling module
import tartci_incident  # noqa: E402 - sibling module

LABEL = "com.danielraffel.tartci.interpreter-grant-check"
INTERVAL_SECONDS = 3600
MAX_PROBES_PER_PASS = 3
TIMEOUT_SECONDS = 30.0
TITLE = "[tartci] {host} lane interpreter can no longer use the VM store: {python}"
CLASS = "interpreter_grant_lost"
LANE_GLOB = "com.danielraffel.tartci.tart-runner-macos-fleet.*.plist"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _home() -> Path:
    return Path(os.environ.get("TARTCI_HOME") or os.path.join(os.path.expanduser("~"), ".tartci"))


def current_interpreters(user_home: Optional[Path] = None) -> list[str]:
    """The absolute interpreters this host runs now: every installed lane's
    TARTCI_PYTHON, then python.pin, each once, in that order."""
    user_home = user_home or Path(os.path.expanduser("~"))
    agents = Path(os.environ.get("TARTCI_LAUNCH_AGENTS_DIR")
                  or user_home / "Library" / "LaunchAgents")
    found: list[str] = []
    for plist in sorted(agents.glob(LANE_GLOB)):
        try:
            value = plistlib.loads(plist.read_bytes()).get("EnvironmentVariables", {}).get(
                "TARTCI_PYTHON")
        except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
            continue
        if isinstance(value, str) and value.startswith("/") and value not in found:
            found.append(value)
    pin = Path(os.environ.get("TARTCI_PYTHON_PIN")
               or user_home / ".config" / "tartci" / "python.pin")
    try:
        value = pin.read_text().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        value = ""
    if value.startswith("/") and value not in found:
        found.append(value)
    return found


def enabled(user_home: Optional[Path] = None) -> bool:
    """Per-host opt-in: the check runs only where this file exists, so it can
    be enabled one host at a time (docs/runbook.md, "Pinned interpreter")."""
    flag = os.environ.get("TARTCI_GRANT_CHECK_ENABLE_FILE") or str(
        (user_home or Path(os.path.expanduser("~"))) / ".config" / "tartci"
        / "interpreter-grant-check.enabled")
    return Path(flag).is_file()


def store_path() -> Optional[str]:
    """The VM store: TART_HOME (every tartci LaunchAgent carries it), else the
    installed fleet profile's host.tart_home."""
    configured = os.environ.get("TART_HOME", "").strip()
    if configured:
        return configured
    try:
        import tomllib  # noqa: PLC0415 - the watchdog runs the pinned 3.11+
        profile = Path(os.environ.get("TARTCI_MACOS_FLEET_PROFILE") or os.path.expanduser(
            "~/.config/tartci/macos-fleet-profile.toml"))
        with profile.open("rb") as handle:
            value = (tomllib.load(handle).get("host") or {}).get("tart_home")
    except (ImportError, OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value.startswith("/") else None


def _probe(python: str, store: str) -> Optional[str]:
    import interpreter_preflight  # noqa: PLC0415 - sibling module
    return interpreter_preflight.run({"host": {"tart_home": store}}, python,
                                     timeout=TIMEOUT_SECONDS, label=LABEL)


def check(now: float, probe: Callable[[str, str], Optional[str]] = _probe,
          home: Optional[Path] = None, user_home: Optional[Path] = None,
          interval: float = INTERVAL_SECONDS) -> dict[str, Any]:
    """Run the probe when due. Returns {"ran": bool, "failures": [...]}; a run
    rewrites state/interpreter-grant-lost (present only while one fails)."""
    home = home or _home()
    sdir = home / "state"
    last_path = sdir / "interpreter-grant-check.json"
    record = sdir / "interpreter-grant-lost"
    if not enabled(user_home):
        # Off: probe nothing, and let an earlier episode's issue close.
        record.unlink(missing_ok=True)
        return {"ran": False, "enabled": False, "failures": []}
    try:
        last = json.loads(last_path.read_text()).get("checked_at", 0)
    except (OSError, ValueError, AttributeError):
        last = 0
    if isinstance(last, (int, float)) and 0 <= now - last < interval:
        return {"ran": False, "failures": []}
    pythons = current_interpreters(user_home)
    store = store_path()
    failures: list[dict[str, str]] = []
    started = time.monotonic()
    if pythons and store:
        # Each probe can take the full timeout inside the heal pass: bound it.
        for python in pythons[:MAX_PROBES_PER_PASS]:
            reason = probe(python, store)
            if reason:
                failures.append({"python": python, "real": os.path.realpath(python),
                                 "store": store, "reason": reason})
    sdir.mkdir(parents=True, exist_ok=True)
    last_path.write_text(json.dumps({
        "checked_at": now, "pythons": pythons, "store": store,
        "probed": pythons[:MAX_PROBES_PER_PASS],
        "probe_seconds": round(time.monotonic() - started, 1),
        "failed": [f["python"] for f in failures]}) + "\n")
    if failures:
        # The first failure's time is the episode: keep it while it lasts.
        since = _iso(now)
        try:
            since = record.read_text().split("\t", 1)[0] or since
        except OSError:
            pass
        first = failures[0]
        record.write_text("\t".join([since, first["python"], first["real"], first["store"],
                                     " | ".join(f"{f['python']}: {f['reason']}"
                                                for f in failures).replace("\t", " ")
                                     .replace("\n", " ")]) + "\n")
    else:
        record.unlink(missing_ok=True)
    return {"ran": True, "failures": failures, "pythons": pythons, "store": store}


def read_record(home: Optional[Path] = None) -> tuple[str, str, str, str, str]:
    """(since, python, real path, store, reason) while a grant is lost."""
    try:
        fields = ((home or _home()) / "state" / "interpreter-grant-lost").read_text() \
            .rstrip("\n").split("\t")
    except OSError:
        return "", "", "", "", ""
    return tuple((fields + [""] * 5)[:5])  # type: ignore[return-value]


def grant_name(store: str) -> str:
    return ("Full Disk Access, or Files & Folders > Removable Volumes"
            if store.startswith("/Volumes/") else "Full Disk Access")


def render(host: str, since: str, python: str, real: str, store: str, reason: str,
           record: Path, detected: str) -> tuple[str, str]:
    binary = real or python
    body = "\n".join([
        "## What happened",
        f"Since {since}, `{python}` on `{host}` cannot write, read back and remove a file "
        f"in the VM store `{store}` when launchd starts it, within {TIMEOUT_SECONDS:g}s. "
        "That is how the fleet lanes (and pool-on, through python.pin) run it, so the next "
        "VM start or pool-on on this host would hang on the same write. The host has NOT "
        "been taken off; it keeps serving what it can.", "",
        "## Evidence", f"- `{record}`: {reason}",
        "- the hourly watchdog check (`scripts/interpreter_grant_check.py`) starts that exact "
        "path as a one-shot launchd job, as the lanes would be",
        f"- the binary macOS attributes the access to: `{binary}`", "",
        "## Likely cause",
        f"The {grant_name(store)} grant for `{binary}` was revoked or reset (a macOS "
        "update, `tccutil reset`, or a box unticked in System Settings), so its write to "
        "the store waits on a consent nobody answers.", "",
        "## Fix", "```sh",
        f"# at {host}: System Settings > Privacy & Security > {grant_name(store)}:",
        f"#   add or re-enable {binary} (Cmd-Shift-G in the + picker, paste the path)",
        "# then re-run the check now instead of waiting an hour:",
        "rm -f ~/.tartci/state/interpreter-grant-check.json",
        "launchctl kickstart -k gui/$(id -u)/com.danielraffel.tartci.launchd-watchdog",
        "```", "",
        "## Safe to auto-apply",
        "No. Privacy grants are given at the machine; nothing here changes them.", "",
        "## How to verify",
        f"`{record}` is gone after the next check and the watchdog log has no "
        "`WARN interpreter-grant` line; this issue then closes itself.",
    ])
    line = tartci_incident.marker(host, CLASS, since or detected, detected)
    return (TITLE.format(host=host, python=binary), tartci_incident.with_marker(body, line))


def alert_pass(now: Optional[float] = None, probe: Callable[[str, str], Optional[str]] = _probe,
               issue: Optional[Callable[..., Any]] = None,
               close: Optional[Callable[..., Any]] = None) -> Optional[str]:
    """One watchdog pass: check when due, keep the issue in step with the
    record, and return the WARN line to log (None while every grant holds)."""
    import interpreter_alert  # noqa: PLC0415 - owns this host's id
    now = time.time() if now is None else now
    home = _home()
    check(now, probe=probe, home=home)
    since, python, real, store, reason = read_record(home)
    record = home / "state" / "interpreter-grant-lost"
    host = interpreter_alert.host_id()
    detected = _iso(now)
    out = host_off.episode_alert(
        home / "state" / "interpreter-grant-alert.json", active=bool(since),
        resolved=not since, since=since or None,
        raise_event=lambda: host_off.event(home / "state", CLASS, f"{python}: {reason}",
                                           {"python": python, "store": store}, now),
        render=lambda: render(host, since, python, real, store, reason, record, detected),
        issue=issue, close=close)
    if not since:
        return None
    return (f"{detected} launchd-watchdog: WARN interpreter-grant: {python} cannot use "
            f"{store} from launchd since {since}: {reason}"
            + (f" (issue #{out.get('issue')})" if out.get("issue") else
               f" (issue not opened: {out.get('issue_error') or ''})"))


def main() -> int:
    line = alert_pass()
    if line:
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
