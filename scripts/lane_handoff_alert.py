#!/usr/bin/env python3
"""Tell someone, once per episode, that a lane's supervisor handoff is stuck.

A lane has one supervisor (runner.sh, tartci_lane_claim). A replacement that
finds a live incumbent waits for it to exit and writes <runner>.handoff while
it waits; the incumbent leaves between jobs. When the wait passes the
runner's own limit (TARTCI_LANE_HANDOFF_MAX_WAIT, 6 h) the lane is served by
an old supervisor that will not leave, or by none if that one is wedged, and
before this only the lane's event log said so. The launchd watchdog runs
`alert_pass` every pass: a handoff file older than the limit, whose waiting
supervisor is still alive, opens one GitHub issue per host through host_off's
once-per-episode path. The issue closes when no handoff on the host is
stuck.

Reads only small files in the state directory (never the Tart store) and
runs `ps` on the two pids involved, so a wedged store cannot hang it.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import subprocess
import tempfile
import time
from typing import Any

import host_off

try:
    import tomllib
except ModuleNotFoundError:  # macOS 3.9 system python: name the host by node
    tomllib = None  # type: ignore[assignment]

MAX_WAIT_SECONDS = 21600


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _host() -> tuple[str, str]:
    """(host id, ssh target): the profile's host.id and host.ssh."""
    host: dict[str, Any] = {}
    try:
        if tomllib is None:
            raise ModuleNotFoundError("tomllib")
        path = pathlib.Path(os.environ.get(
            "TARTCI_FLEET_PROFILE",
            str(pathlib.Path.home() / ".config" / "tartci" / "macos-fleet-profile.toml")))
        host = tomllib.loads(path.read_text()).get("host") or {}
    except Exception:  # noqa: BLE001 - fall back to the node name
        host = {}
    name = str(host.get("id") or os.uname().nodename.split(".")[0])
    return name, str(host.get("ssh") or f"tartci-{name}")


def _pid_of(path: pathlib.Path) -> int | None:
    try:
        first = path.read_text().split()[0]
    except (OSError, IndexError):
        return None
    return int(first) if first.isdigit() else None


def _live(pid: int | None) -> bool:
    """Alive and not a zombie (a zombie answers kill -0 but serves nothing)."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                               text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return True
    return bool(state) and not state.startswith("Z")


def stuck_handoffs(state_root: pathlib.Path, now: float,
                   max_wait: float = MAX_WAIT_SECONDS) -> list[dict[str, Any]]:
    """Each lane whose replacement has waited at least `max_wait` and is alive."""
    found = []
    for handoff in sorted([*state_root.glob("*/*.handoff"), *state_root.glob("*/*/*.handoff")]):
        try:
            since = handoff.stat().st_mtime
        except OSError:
            continue
        waiting = _pid_of(handoff)
        if now - since < max_wait or not _live(waiting):
            continue
        lane = handoff.name.removesuffix(".handoff")
        incumbent = _pid_of(handoff.with_name(f"{lane}.supervisor"))
        found.append({"lane": lane, "state_dir": str(handoff.parent), "waiting": waiting,
                      "incumbent": incumbent, "incumbent_live": _live(incumbent),
                      "since": _iso(since), "waited_s": int(now - since)})
    return found


def alert_text(stuck: list[dict[str, Any]], host: str, target: str,
               since: str) -> tuple[str, str]:
    """Readable from a phone notification: host first, then what to run."""
    title = f"[tartci] {host}: lane supervisor handoff stuck since {since}"
    lines = [f"On {host}, {len(stuck)} lane(s) have a replacement supervisor that has waited "
             "longer than the 6 h limit for the old one to exit. Only one supervisor serves a "
             "lane, so each serves through the old supervisor, or not at all if it is wedged.",
             f"Run: ssh {target} 'tartci pool status'", ""]
    for item in stuck:
        incumbent = item["incumbent"]
        lines.append(f"- lane `{item['lane']}`: replacement pid {item['waiting']} waiting since "
                     f"{item['since']} ({item['waited_s'] // 3600} h) for pid {incumbent}.")
        if item["incumbent_live"]:
            lines.append(f"  Fix: the old supervisor leaves when its current job ends. If that "
                         f"job is stuck, stop it: ssh {target} 'kill -TERM {incumbent}' (this "
                         "tears down its VM and gives up that job); the replacement takes the "
                         "lane within one poll.")
        else:
            lines.append("  Fix: the old supervisor is gone; the replacement takes the lane "
                         "within one poll. If it does not, restart the lane: "
                         f"ssh {target} 'tartci launchd reload <this lane's label>'.")
    lines += ["", "Verify: `supervisor_handoff_done` in the lane's events.jsonl. This issue "
              "closes itself when no handoff on the host is stuck."]
    return title, "\n".join(lines)


def _scratch(directory: pathlib.Path) -> bool:
    """State under the temp dir is a test's: it never reaches GitHub unless the
    test passes its own issue opener."""
    scratch = os.path.realpath(tempfile.gettempdir())
    return os.path.realpath(str(directory)).startswith(scratch + os.sep)


def alert_pass(now: float | None = None, state_root: pathlib.Path | None = None,
               issue: Any = None, close: Any = None, host: str | None = None,
               target: str | None = None) -> dict[str, Any]:
    """The watchdog's pass: one GitHub issue per stuck-handoff episode."""
    now = time.time() if now is None else now
    state_root = state_root or pathlib.Path.home() / ".tartci" / "state"
    max_wait = float(os.environ.get("TARTCI_LANE_HANDOFF_MAX_WAIT", MAX_WAIT_SECONDS))
    stuck = stuck_handoffs(state_root, now, max_wait)
    if host is None or target is None:
        host, target = _host()
    directory = state_root / "lane-handoff"
    state_path = directory / "alert.json"
    before = host_off._read_json(state_path) or {}
    since = str(before.get("since") or (min(i["since"] for i in stuck) if stuck else "")) or None

    def raise_event() -> None:
        directory.mkdir(parents=True, exist_ok=True)
        host_off.event(directory, "lane_handoff_stuck", f"{host}: {len(stuck)} lane(s)",
                       {"lanes": [i["lane"] for i in stuck], "since": since}, now)

    switched_on = os.environ.get("TARTCI_LANE_HANDOFF_ISSUE", "1") != "0"
    out = host_off.episode_alert(
        state_path, active=bool(stuck), resolved=not stuck, since=since,
        raise_event=raise_event, render=lambda: alert_text(stuck, host, target, since or ""),
        issue=issue, close=close,
        issues_enabled=switched_on and (issue is not None or not _scratch(state_root)))
    return {"stuck": stuck, **out}
