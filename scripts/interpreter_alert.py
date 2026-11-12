#!/usr/bin/env python3
"""Raise one issue while a tartci LaunchAgent on this host refuses to run.

providers/common/toml-python.lib.sh refuses an unpinned interpreter under
launchd (or a pin whose binary is gone) and records the first refusal in
state/interpreter-refused; the installer removes it once a working pin is in
place. The watchdog runs this every pass. When the watchdog itself is the job
refused, `tartci launchd heal` runs this module alone under /usr/bin/python3,
Apple's Python, which Homebrew never replaces: stdlib only, no tomllib, so a
host whose pin is broken still reports it instead of going dark.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import host_off  # noqa: E402 - sibling module
import tartci_incident  # noqa: E402 - sibling module

TITLE = "[tartci] {host} refused to run an unpinned interpreter"
CLASS = "interpreter_unpinned"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _home() -> Path:
    return Path(os.environ.get("TARTCI_HOME") or os.path.join(os.path.expanduser("~"), ".tartci"))


def read_refusal(home: Optional[Path] = None) -> Tuple[str, str, str]:
    """(time, job, reason) of the first recorded refusal, or empty strings."""
    marker = (home or _home()) / "state" / "interpreter-refused"
    try:
        fields = marker.read_text().strip().split("\t")
    except OSError:
        return "", "", ""
    return tuple((fields + ["", "", ""])[:3])  # type: ignore[return-value]


def render(host: str, stamp: str, job: str, reason: str, marker: Path,
           detected: str) -> Tuple[str, str]:
    body = "\n".join([
        "## What happened",
        f"A tartci LaunchAgent on `{host}` ({job}) refused to start at {stamp}: {reason}. "
        "It will not run until the interpreter is pinned. If it is the watchdog or "
        "self-update, the host stops healing and updating itself.", "",
        "## Evidence", f"- `{marker}`: {stamp} {job} {reason}",
        "- the job's own log has the line `refusing an unpinned interpreter` or "
        "`the pinned interpreter ... is unusable`",
        "- `tartci doctor fleet` reports the agent's nonzero last exit", "",
        "## Likely cause",
        "The host has no `~/.config/tartci/python.pin`, or the pinned binary is gone "
        "(a Homebrew upgrade or cleanup removed its Cellar directory).", "",
        "## Fix", "```sh",
        "cat ~/.config/tartci/python.pin; ls -l \"$(cat ~/.config/tartci/python.pin)\"",
        "brew install python@3.12 && brew pin python@3.12   # if the binary is gone",
        "# set host.python in this host's fleet profile, then reinstall from a checkout:",
        "tartci fleet-macos install <profile> --apply",
        "```", "",
        "## Safe to auto-apply",
        "No. Choosing the interpreter is a host decision, and a new binary needs its "
        "privacy grants (Local Network) again.", "",
        "## How to verify",
        f"`cat ~/.config/tartci/python.pin` names an existing binary and `{marker}` is gone "
        "(the installer removes it); this issue then closes itself.",
    ])
    line = tartci_incident.marker(host, CLASS, stamp or detected, detected)
    return TITLE.format(host=host), tartci_incident.with_marker(body, line)


def host_id() -> str:
    import vm_boot_alert  # noqa: PLC0415 - owns this host's id (works without tomllib)
    return vm_boot_alert._alert_host()[0]


SWITCH_TITLE = "[tartci] {host} kept its lane interpreter: {new} failed the launchd preflight"
SWITCH_CLASS = "interpreter_switch_refused"


def read_switch(home: Optional[Path] = None) -> Tuple[str, str, str, str]:
    """(time, new, kept, reason) of an interpreter switch the installer refused."""
    record = (home or _home()) / "state" / "interpreter-switch-refused"
    try:
        fields = record.read_text().strip().split("\t")
    except OSError:
        return "", "", "", ""
    return tuple((fields + ["", "", "", ""])[:4])  # type: ignore[return-value]


def render_switch(host: str, stamp: str, new: str, kept: str, reason: str, record: Path,
                  detected: str) -> Tuple[str, str]:
    body = "\n".join([
        "## What happened",
        f"An install on `{host}` at {stamp} was to switch the lanes to `{new}`, but that "
        f"binary failed the launchd preflight, so the lanes keep `{kept}`. The host is "
        "serving; the requested interpreter (host.python) is not in effect.", "",
        "## Evidence", f"- `{record}`: {reason}",
        "- the preflight starts the new binary as a launchd job, as the lanes would be, "
        "and has it write, read back and remove one file in the profile's tart_home", "",
        "## Likely cause",
        "macOS grants belong to the binary: the new interpreter was never granted access to "
        "the VM store's volume (Full Disk Access, or Files & Folders > Removable Volumes for a "
        "store under /Volumes), so its first write waits on a consent nobody answers.", "",
        "## Fix", "```sh",
        f"# at {host}: System Settings > Privacy & Security > Full Disk Access, add {new}",
        "#   (Cmd-Shift-G in the + picker and paste the path); also Local Network > Python",
        "tartci fleet-macos self-update --apply   # the next install retries the switch",
        "```", "",
        "## Safe to auto-apply",
        "No. Privacy grants are given at the machine; nothing here changes them.", "",
        "## How to verify",
        f"The next install reports no `keeps the lane interpreter` line, the lanes' "
        f"TARTCI_PYTHON is `{new}`, and `{record}` is gone; this issue then closes itself.",
    ])
    line = tartci_incident.marker(host, SWITCH_CLASS, stamp or detected, detected)
    return SWITCH_TITLE.format(host=host, new=new), tartci_incident.with_marker(body, line)


def alert_pass(now: Optional[float] = None, issue: Optional[Callable[..., Any]] = None,
               close: Optional[Callable[..., Any]] = None) -> Optional[str]:
    """One pass: the WARN line(s) to log, or None when nothing is refused."""
    now = time.time() if now is None else now
    home = _home()
    stamp, job, reason = read_refusal(home)
    marker = home / "state" / "interpreter-refused"
    host = host_id()
    detected = _iso(now)
    out = host_off.episode_alert(
        home / "state" / "interpreter-refused-alert.json", active=bool(stamp),
        resolved=not stamp, since=stamp or None,
        raise_event=lambda: host_off.event(home / "state", "interpreter_refused",
                                           f"{job}: {reason}", {"job": job}, now),
        render=lambda: render(host, stamp, job, reason, marker, detected),
        issue=issue, close=close)
    lines = []
    if stamp:
        lines.append(f"{detected} launchd-watchdog: WARN interpreter: {job} refused to run an "
                     f"unpinned interpreter since {stamp}: {reason}"
                     + (f" (issue #{out.get('issue')})" if out.get("issue") else "")
                     + (f" (issue not opened: {out.get('issue_error') or ''})"
                        if not out.get("issue") else ""))
    sw_stamp, new, kept, sw_reason = read_switch(home)
    record = home / "state" / "interpreter-switch-refused"
    sw = host_off.episode_alert(
        home / "state" / "interpreter-switch-alert.json", active=bool(sw_stamp),
        resolved=not sw_stamp, since=sw_stamp or None,
        raise_event=lambda: host_off.event(home / "state", "interpreter_switch_refused",
                                           f"kept {kept}; {new} failed: {sw_reason}",
                                           {"new": new, "kept": kept}, now),
        render=lambda: render_switch(host, sw_stamp, new, kept, sw_reason, record, detected),
        issue=issue, close=close)
    if sw_stamp:
        lines.append(f"{detected} launchd-watchdog: WARN interpreter: the lanes kept {kept}; "
                     f"{new} failed the launchd preflight at {sw_stamp}: {sw_reason}"
                     + (f" (issue #{sw.get('issue')})" if sw.get("issue") else ""))
    return "\n".join(lines) or None


def main() -> int:
    line = alert_pass()
    if line:
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
