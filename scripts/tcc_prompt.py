#!/usr/bin/env python3
"""See a macOS privacy consent alert waiting on this host's screen, and page.

On m3 the Tart store volume stopped answering launchd jobs for hours at a time
(tartci#840). Each episode began when macOS put a modal privacy consent alert
(a CFUserNotification drawn by UserNotificationCenter) on the console and
ended a few minutes after someone clicked it. While the alert waits, every
Removable Volumes authorization that needs a decision queues behind it, so a
launchd job with no stored grant blocks in `open()` on the volume. The alert
has no timeout and the host is usually unattended, so nobody sees it.

This module adds the two readings that name that cause and the page that ends
it quickly:

  * ``read_windows``: whether UserNotificationCenter has a window ordered on
    screen. It reads the window server's window list (owner names and PIDs
    only, which needs no Screen Recording grant) in a bounded child process.
    A window list with no windows at all is ``unavailable``, never ``absent``:
    a reader that cannot see the window server must not read as "no alert".
  * ``shipyard_installs``: when each Shipyard auth generation was installed.
    Every Shipyard install puts the binary at a new path
    (``~/.local/share/shipyard/auth-generations/<sha>/shipyard``), TCC keys
    an unsigned binary's decision by that identity, and so each install can
    raise a fresh alert. An alert first seen within ``INSTALL_WINDOW_S`` of an
    install is attributed to it.

``observe`` combines them once per watchdog pass, keeps the episode start
(the first pass that saw the alert) in ``state/tcc-prompt.json``, and while the
alert is up runs one fresh launchd job against the store (tcc_wedge's
``launchd_probe``): a hang on the volume root is a store open waiting on the
alert. ``tcc_wedge.classify`` reuses that reading to name the watchdog's own
store hang ``tcc_prompt_pending``. ``alert_pass`` opens one GitHub issue per
episode (the fleet's paging path) when the alert is up and either a store open
waits on it or it followed a Shipyard install, and closes it when the alert is
gone.

Nothing here answers, dismisses or changes a privacy decision. The only
fallback is ``tcc_wedge``'s existing scoped heal, which restarts tccd and
sandboxd and so drops the pending prompt; ``fallback_heal`` runs it once per
alert episode, only after a store open has waited on the alert for
``tcc_wedge.PROMPT_HEAL_AFTER_S``.

Stdlib only: it runs under the system Python 3.9 the watchdog may use.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

KIND = "tcc_prompt_pending"
UNC_NAME = "UserNotificationCenter"
UNC_PATH = "/System/Library/CoreServices/UserNotificationCenter.app/Contents/MacOS/UserNotificationCenter"
WINDOW_READ_TIMEOUT_S = 10.0
INSTALL_WINDOW_S = 10 * 60
STATE_NAME = "tcc-prompt.json"
ALERT_STATE_NAME = "tcc-prompt-alert.json"
HEAL_STATE_NAME = "tcc-prompt-heal.json"
TITLE = "[tartci] {host} has a macOS privacy prompt waiting: click Allow"
GENERATION = re.compile(r"^[0-9a-f]{40,64}$")


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _home() -> Path:
    return Path(os.environ.get("TARTCI_HOME") or os.path.join(os.path.expanduser("~"), ".tartci"))


def shipyard_root() -> Path:
    override = os.environ.get("TARTCI_SHIPYARD_GENERATIONS")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".local" / "share" / "shipyard" / "auth-generations"


# ── The window list ─────────────────────────────────────────────────────────

def _window_list_in_process() -> dict[str, Any]:
    """Every window the window server lists, reduced to owner and on-screen state.

    Runs in the bounded child (``--windows-json``). ctypes against
    CoreGraphics and CoreFoundation: no PyObjC, no osascript, no Apple Events.
    """
    import ctypes
    import ctypes.util

    cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
    cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
    vp = ctypes.c_void_p
    cg.CGWindowListCopyWindowInfo.restype = vp
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetCount.argtypes = [vp]
    cf.CFArrayGetValueAtIndex.restype = vp
    cf.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
    cf.CFDictionaryGetValue.restype = vp
    cf.CFDictionaryGetValue.argtypes = [vp, vp]
    cf.CFStringCreateWithCString.restype = vp
    cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cf.CFNumberGetValue.argtypes = [vp, ctypes.c_int, vp]
    cf.CFBooleanGetValue.restype = ctypes.c_bool
    cf.CFBooleanGetValue.argtypes = [vp]
    utf8 = 0x08000100
    sint64 = 4
    keys: dict[str, int] = {}

    def key(name: str) -> int:
        if name not in keys:
            keys[name] = cf.CFStringCreateWithCString(None, name.encode(), utf8)
        return keys[name]

    def text(value: Optional[int]) -> Optional[str]:
        if not value:
            return None
        buffer = ctypes.create_string_buffer(1024)
        return buffer.value.decode("utf-8", "replace") if cf.CFStringGetCString(
            value, buffer, 1024, utf8) else None

    def number(value: Optional[int]) -> Optional[int]:
        if not value:
            return None
        out = ctypes.c_int64()
        return out.value if cf.CFNumberGetValue(value, sint64, ctypes.byref(out)) else None

    libproc = ctypes.cdll.LoadLibrary("/usr/lib/libproc.dylib")
    libproc.proc_pidpath.restype = ctypes.c_int
    libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]

    def pid_path(pid: Optional[int]) -> Optional[str]:
        if not pid:
            return None
        buffer = ctypes.create_string_buffer(4096)
        size = libproc.proc_pidpath(pid, buffer, 4096)
        return buffer.value.decode("utf-8", "replace") if size > 0 else None

    # kCGWindowListOptionAll, kCGNullWindowID
    array = cg.CGWindowListCopyWindowInfo(0, 0)
    if not array:
        return {"total": 0, "windows": []}
    windows = []
    paths: dict[int, Optional[str]] = {}
    total = cf.CFArrayGetCount(array)
    for index in range(total):
        row = cf.CFArrayGetValueAtIndex(array, index)
        owner = text(cf.CFDictionaryGetValue(row, key("kCGWindowOwnerName")))
        pid = number(cf.CFDictionaryGetValue(row, key("kCGWindowOwnerPID")))
        if pid is not None and pid not in paths:
            paths[pid] = pid_path(pid)
        if owner != UNC_NAME and paths.get(pid) != UNC_PATH:
            continue
        onscreen = cf.CFDictionaryGetValue(row, key("kCGWindowIsOnscreen"))
        windows.append({
            "owner": owner, "pid": pid, "path": paths.get(pid),
            "window": number(cf.CFDictionaryGetValue(row, key("kCGWindowNumber"))),
            "layer": number(cf.CFDictionaryGetValue(row, key("kCGWindowLayer"))),
            "onscreen": bool(onscreen and cf.CFBooleanGetValue(onscreen)),
        })
    return {"total": int(total), "windows": windows}


def judge_windows(listing: dict[str, Any]) -> dict[str, Any]:
    """present / absent / unavailable from a window listing.

    Only a UserNotificationCenter window ORDERED ON SCREEN is an alert: the
    process stays resident for days, and an alert that was answered can leave
    an ordered-out window behind. A listing with no windows at all (no window
    server session) is unavailable, never absent.
    """
    total = listing.get("total")
    windows = [w for w in listing.get("windows") or [] if isinstance(w, dict)]
    if not isinstance(total, int) or total <= 0:
        return {"state": "unavailable", "total_windows": total or 0, "windows": windows,
                "detail": "the window server listed no windows from this context"}
    shown = [w for w in windows if w.get("onscreen")]
    return {"state": "present" if shown else "absent", "total_windows": total,
            "windows": windows, "onscreen": len(shown)}


def read_windows(*, runner: Optional[Callable[..., Any]] = None,
                 python: Optional[str] = None,
                 timeout_s: float = WINDOW_READ_TIMEOUT_S) -> dict[str, Any]:
    """The window-list reading, from a bounded child process."""
    from bounded_subprocess import ObservationError, require_success, run_bounded
    from python_child import child_python
    runner = run_bounded if runner is None else runner
    if python is None:
        python = child_python()
    argv = [python, "-I", "-B", os.path.abspath(__file__), "--windows-json"]
    try:
        completed = runner(argv, timeout=timeout_s, operation="tcc_prompt_windows")
        require_success(completed, operation="tcc_prompt_windows")
        listing = json.loads(completed.stdout or "{}")
    except ObservationError as exc:
        return {"state": "unavailable", "detail": f"{exc.kind}: {exc.detail}"[:300]}
    except (OSError, ValueError) as exc:
        return {"state": "unavailable", "detail": str(exc)[:300]}
    if not isinstance(listing, dict):
        return {"state": "unavailable", "detail": "the window list was not an object"}
    return judge_windows(listing)


# ── Shipyard installs ───────────────────────────────────────────────────────

def shipyard_installs(root: Optional[Path] = None) -> list[dict[str, Any]]:
    """Each Shipyard auth generation and when it was installed, newest first.

    The generation directory is created by the install, so its birth time
    (mtime where the filesystem has none) is the install time. Internal
    volume only; nothing here touches the store.
    """
    root = shipyard_root() if root is None else Path(root)
    out = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return out
    for entry in entries:
        if not GENERATION.match(entry.name):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        at = getattr(info, "st_birthtime", None) or info.st_mtime
        out.append({"generation": entry.name, "installed_epoch": float(at),
                    "installed_at": _iso(float(at)),
                    "binary": str(root / entry.name / "shipyard")})
    out.sort(key=lambda row: row["installed_epoch"], reverse=True)
    return out


def install_before(alert_epoch: float, installs: list[dict[str, Any]],
                   window_s: float = INSTALL_WINDOW_S) -> Optional[dict[str, Any]]:
    """The newest install at most `window_s` before the alert was first seen."""
    for row in installs:
        gap = alert_epoch - float(row.get("installed_epoch") or 0)
        if 0 <= gap <= window_s:
            return dict(row, seconds_before_alert=int(gap))
    return None


# ── One reading per watchdog pass ───────────────────────────────────────────

def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True))
    os.replace(temporary, path)


def observe(now: Optional[float] = None, *, state_dir: Optional[Path] = None,
            windows: Optional[Callable[[], dict]] = None,
            installs: Optional[Callable[[], list]] = None,
            launchd: Optional[Callable[[], dict]] = None,
            context: Optional[Callable[[], dict]] = None,
            save: bool = True) -> dict[str, Any]:
    """The consent-alert reading for this pass.

    `since` is the first pass that saw the alert in this episode; an absent
    reading ends the episode and an unavailable one leaves it as it was (it
    says nothing either way). `shipyard_install` names the install the alert
    followed, if one was within INSTALL_WINDOW_S of `since`.

    While the alert is up, `launchd` (tcc_wedge.launchd_probe for the store)
    runs once: a fresh launchd job that answers its internal open and hangs on
    the volume root is the reading that ties the alert to the store. It does
    not depend on the watchdog's own store probe, whose context may already
    hold a grant. `context`, when configured, must answer too (otherwise the
    volume is stalled, not waiting on the alert). `hang` records the
    conclusion and `pending_since` the first pass it held in this episode.
    """
    now = time.time() if now is None else now
    state_dir = (_home() / "state") if state_dir is None else Path(state_dir)
    path = state_dir / STATE_NAME
    reading = dict((windows or read_windows)())
    reading["read_at"] = _iso(now)
    previous = _read_json(path)
    state = reading.get("state")

    def epoch(value: Any) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value > now:
            return None
        return float(value)
    since = epoch(previous.get("since_epoch"))
    pending = epoch(previous.get("pending_since_epoch"))
    if state == "present":
        since = since if since is not None else now
    elif state == "absent":
        since = pending = None
    if since is not None:
        reading["since_epoch"] = since
        reading["since"] = _iso(since)
        install = install_before(since, (installs or shipyard_installs)())
        if install:
            reading["shipyard_install"] = install
    if state == "present" and launchd is None:
        pending = None          # no launchd reading this pass: nothing holds
    elif state == "present":
        probe = dict(launchd() or {})
        reading["launchd_probe"] = probe
        hang = probe.get("outcome") == "hung_on_root"
        if hang and context is not None:
            granted = dict(context() or {})
            reading["tcc_context_probe"] = granted
            hang = bool(granted.get("ok"))
        reading["hang"] = hang
        if hang:
            pending = pending if pending is not None else now
        else:
            pending = None
    if pending is not None and state != "unavailable":
        reading["pending_since_epoch"] = pending
        reading["pending_since"] = _iso(pending)
    if save:
        try:
            if since is None and state == "absent":
                path.unlink(missing_ok=True)
            elif since is not None:
                record = {"since_epoch": since, "state": state, "read_at": _iso(now)}
                if pending is not None:
                    record["pending_since_epoch"] = pending
                _write_json(path, record)
        except OSError as exc:
            reading["state_error"] = str(exc)[:200]
    return reading


# ── The page ────────────────────────────────────────────────────────────────

def pending_verdict(store_state: Optional[dict], now: float, fresh_s: float) -> Optional[dict]:
    """The watchdog's fresh tcc_prompt_pending verdict, if there is one."""
    classification = (store_state or {}).get("classification") or {}
    if classification.get("kind") != KIND:
        return None
    try:
        import calendar
        stamp = calendar.timegm(time.strptime(str(classification.get("classified_at")),
                                              "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None
    return classification if 0 <= now - stamp <= fresh_s else None


def render(host: str, reading: dict, verdict: Optional[dict], heal_after_s: int,
           detected: str) -> tuple[str, str]:
    import tartci_incident
    install = reading.get("shipyard_install")
    since = reading.get("since") or detected
    windows = ", ".join(f"window {w.get('window')} (pid {w.get('pid')})"
                        for w in reading.get("windows") or [] if w.get("onscreen")) or "none listed"
    evidence = [f"- UserNotificationCenter has an alert ordered on screen since {since} "
                f"({windows})"]
    if verdict:
        evidence.append(f"- the launchd watchdog classified the store hang as `{KIND}` at "
                        f"{verdict.get('classified_at')}: {verdict.get('reason')}")
    if install:
        evidence.append(f"- Shipyard installed generation `{install['generation']}` at "
                        f"{install['installed_at']}, {install['seconds_before_alert']} s before "
                        f"the alert: a new binary path, so a new privacy identity")
    cause = ("A new Shipyard binary (`" + install["binary"] + "`) asked for access and "
             "macOS is waiting for an answer." if install else
             "A launchd job with no stored privacy decision asked for access to a removable "
             "volume and macOS is waiting for an answer.")
    body = "\n".join([
        "## What happened",
        f"`{host}` has a macOS privacy consent alert on its screen that nobody has answered. "
        "While it waits, launchd jobs that open the Tart store volume hang, "
        "so the lanes admit no VM.", "",
        "## Evidence", *evidence, "",
        "## Likely cause", cause, "",
        "## Fix",
        f"At `{host}`'s screen (or Screen Sharing): read the alert, note the program it names, "
        "and click **Allow**. Do not click Don't Allow: a refusal is remembered and the job "
        "then fails with a permission error instead of hanging.", "",
        f"If nobody answers within {heal_after_s // 60} min of a store open starting to wait on it, the "
        "watchdog runs `/usr/local/sbin/tartci-heal-tcc` once, which restarts tccd and "
        "sandboxd and drops the alert. That unblocks the host but grants nothing, so the "
        "same alert can come back.", "",
        "## Safe to auto-apply",
        "No. Answering a privacy prompt is the owner's decision; nothing here clicks it.", "",
        "## How to verify",
        "The alert is gone from the screen and the next watchdog pass reads the store "
        "healthy; this issue then closes itself.",
    ])
    line = tartci_incident.marker(host, KIND, since, detected)
    return TITLE.format(host=host), tartci_incident.with_marker(body, line)


def host_id() -> str:
    import vm_boot_alert  # noqa: PLC0415 - owns this host's id (works without tomllib)
    return vm_boot_alert._alert_host()[0]


def alert_pass(reading: dict, store_state: Optional[dict], now: Optional[float] = None, *,
               state_dir: Optional[Path] = None, fresh_s: int = 30 * 60,
               heal_after_s: int = 15 * 60, host: Optional[str] = None,
               issue: Optional[Callable[..., Any]] = None,
               close: Optional[Callable[..., Any]] = None,
               heal: Optional[Callable[..., dict]] = None,
               heal_enabled: bool = True) -> Optional[str]:
    """Page once per alert episode, close the issue when the alert is gone, and
    run the fallback heal once per episode if nobody answers.

    The page is active while the alert is on screen AND a store open waits on
    it (this pass's fresh launchd job hung on the volume root, or the
    watchdog's fresh `tcc_prompt_pending` verdict) or the alert followed a
    Shipyard install. An unavailable window reading neither opens nor closes
    anything. The heal (`heal`, tcc_wedge.heal) runs only once the hang has
    held for `heal_after_s` from `pending_since`, at most once per alert
    episode, under tcc_wedge's own rate limit and the wrapper's root stamp.
    """
    import host_off
    now = time.time() if now is None else now
    state_dir = (_home() / "state") if state_dir is None else Path(state_dir)
    state = reading.get("state")
    if state == "unavailable":
        return None
    verdict = pending_verdict(store_state, now, fresh_s)
    install = reading.get("shipyard_install")
    waiting = state == "present" and (bool(reading.get("hang")) or verdict is not None)
    active = state == "present" and (waiting or install is not None)
    host = host or host_id()
    detected = _iso(now)
    out = host_off.episode_alert(
        state_dir / ALERT_STATE_NAME, active=active, resolved=state == "absent",
        since=reading.get("since"),
        raise_event=lambda: host_off.event(
            state_dir, KIND, "a privacy consent alert is waiting on the console",
            {"since": reading.get("since"),
             "shipyard_generation": (install or {}).get("generation")}, now),
        render=lambda: render(host, reading, verdict, heal_after_s, detected),
        issue=issue, close=close,
        issues_enabled=os.environ.get("TARTCI_TCC_PROMPT_ISSUE", "1") != "0")
    if not active:
        return None
    heal_text = ""
    pending = reading.get("pending_since_epoch")
    if waiting and isinstance(pending, (int, float)):
        record = fallback_heal(reading, store_state, now, state_dir=state_dir,
                               heal_after_s=heal_after_s, heal=heal, enabled=heal_enabled)
        heal_text = f"; heal {record.get('outcome')}"
    why = (f"after Shipyard install {install['generation'][:12]} "
           f"({install['seconds_before_alert']} s before)" if install else "")
    if waiting:
        why = (why + "; " if why else "") + "launchd store opens hang behind it"
    return (f"{detected} launchd-watchdog: WARN tcc-prompt: a privacy consent alert has waited "
            f"on the console since {reading.get('since')} ({why}{heal_text})"
            + (f" (issue #{out.get('issue')})" if out.get("issue") else " (issue not opened)"))


def fallback_heal(reading: dict, store_state: Optional[dict], now: float, *,
                  state_dir: Path, heal_after_s: int,
                  heal: Optional[Callable[..., dict]] = None, enabled: bool = True) -> dict:
    """The scoped heal, once per alert episode, after the page had its time.

    Records go to state/tcc-prompt-heal.json, which outlives the episode so the
    rate limit holds across alerts; the store-health heal record is read too,
    so the two paths share one budget.
    """
    import tcc_wedge
    path = Path(state_dir) / HEAL_STATE_NAME
    saved = _read_json(path)
    episode = reading.get("since_epoch")
    pending = float(reading.get("pending_since_epoch"))
    due = pending + heal_after_s
    if now < due:
        return {"outcome": "waiting_for_answer", "due": _iso(due)}
    if saved.get("episode") == episode:
        return dict(saved.get("record") or {}, outcome="already_ran_this_episode")
    if not enabled:
        return {"outcome": "disabled", "detail": "TARTCI_TCC_HEAL=0: page only"}
    records = [r for r in (saved.get("last_heal"), (store_state or {}).get("last_heal"))
               if isinstance(r, dict) and isinstance(r.get("at_epoch"), (int, float))]
    last = max(records, key=lambda r: r["at_epoch"]) if records else None
    record = (heal or tcc_wedge.heal)(now=now, last_heal=last)
    if record.get("outcome") in tcc_wedge.BUDGET_OUTCOMES:
        saved = {"episode": episode, "record": record,
                 "last_heal": {"at": record.get("at"), "at_epoch": now,
                               "outcome": record.get("outcome")}}
        try:
            _write_json(path, saved)
        except OSError as exc:
            record["state_error"] = str(exc)[:200]
    return record


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--windows-json", action="store_true",
                      help="print the window server's UserNotificationCenter windows (child mode)")
    mode.add_argument("--check", action="store_true",
                      help="read the consent alert and the Shipyard installs now, read only")
    args = parser.parse_args(argv)
    if args.windows_json:
        print(json.dumps(_window_list_in_process(), sort_keys=True))
        return 0
    print(json.dumps(observe(save=False), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
