#!/usr/bin/env python3
"""Tell someone when a peer's launchd has stopped starting its timer jobs.

A stalled macOS automatic install can leave launchd refusing every
non-demand spawn (launchd_interval_guard.py). The guard keeps the timers
running from inside a lane supervisor, but it pauses self-update for the whole
episode, and nothing on the stalled host opened an issue: m3 sat stalled from
2026-10-05T05:41Z for 97 h, 44 commits behind, while every view said it was
serving. A host in that state is the wrong one to rely on for its own alert.

So every fleet host reads its peers. The launchd watchdog runs `alert_pass`
every 300 s; at most once per READ_SECS it reads each published peer's guard
receipt over SSH, together with the peer's clock, and judges it with the
guard's own `classify` on that clock. A stall episode open for STALL_ALERT_SECS
opens one GitHub issue per peer and episode, titled

    [tartci] <peer> launchd stalled / self-update paused since <episode start>

Every watchdog runs on the same cadence, so the reads of one peer align by
construction. One host acts for each stalled peer: its primary reader, the
lowest published host id other than the peer. Every other reader reads the
primary's own last pass along with the primary's guard receipt (the same SSH
command), and acts only when that pass has been missing, stale or unable to
read the peer for PRIMARY_MISSES consecutive reads. Before opening, any reader
looks for an open issue with the exact title and adopts it, which backs up the
rule when a fallback and a returning primary overlap. The issue closes when a
reader sees a fresh receipt, written after the episode began, that reports no
stall. An unreachable peer, an unreadable or
stale receipt, or a guard that has never run decides nothing: the issue stays
as it is (an unreachable host is self-update's peer-reachability record, and
a stale receipt is the doctor's launchd_timers check on that host).

It runs wherever the watchdog does, including the system python3 (3.9). The
peer list is main's published supply in the self-update checkout; without
tomllib this host's id falls back to its node name (vm_boot_alert), and when
that names no published host it reads every published host, itself included.
"""
from __future__ import annotations

import calendar
import json
import os
import pathlib
import subprocess
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import host_off

STALL_ALERT_SECS = 6 * 3600
READ_SECS = 1800
SSH_TIMEOUT_SECS = 30
TITLE = "[tartci] {peer} launchd stalled / self-update paused since {since}"
PRIMARY_MISSES = 2
SEPARATOR = "--- guard ---"
LAST_SEPARATOR = "--- peer-stall ---"
# Printed by the peer: its clock, its guard receipt, then its own last
# peer-stall pass (each empty when absent).
PEER_READ = ('date +%s; echo "' + SEPARATOR + '"; '
             'cat "${TARTCI_HOME:-$HOME/.tartci}/state/launchd-interval-guard/status.json" '
             '2>/dev/null || true; echo; echo "' + LAST_SEPARATOR + '"; '
             'cat "${TARTCI_PEER_STALL_DIR:-${TARTCI_HOME:-$HOME/.tartci}/state/peer-stall}'
             '/last-read.json" 2>/dev/null || true')

Runner = Callable[[List[str]], Tuple[int, str, str]]


def setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


def state_root() -> pathlib.Path:
    override = os.environ.get("TARTCI_PEER_STALL_DIR")
    if override:
        return pathlib.Path(override)
    home = os.environ.get("TARTCI_HOME") or str(pathlib.Path.home() / ".tartci")
    return pathlib.Path(home) / "state" / "peer-stall"


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _run(argv: List[str]) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=SSH_TIMEOUT_SECS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 255, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def read_peer(target: str, run: Optional[Runner] = None) -> Dict[str, Any]:
    """A peer's guard state judged on its own clock:
    {"readable": bool, "clock": epoch | None, "guard": classify() | None,
     "error": str}."""
    import launchd_interval_guard  # noqa: PLC0415 - imports the watchdog
    rc, out, err = (run or _run)(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                  target, PEER_READ])
    first, _, rest = out.partition("\n")
    if rc != 0 or not first.strip().isdigit():
        return {"readable": False, "clock": None, "guard": None,
                "error": f"exit {rc}: {(err or out).strip()[:200]}"}
    clock = int(first.strip())
    _, _, body = rest.partition(SEPARATOR)
    body, _, last_text = body.partition(LAST_SEPARATOR)
    try:
        last = json.loads(last_text) if last_text.strip() else None
    except ValueError:
        last = None
    body = body.strip()
    if not body:
        receipt = None
    else:
        try:
            receipt = json.loads(body)
        except ValueError as exc:
            return {"readable": True, "clock": clock, "last": last,
                    "guard": {"state": "unreadable", "error": str(exc)}, "error": ""}
    return {"readable": True, "clock": clock, "last": last if isinstance(last, dict) else None,
            "guard": launchd_interval_guard.classify(
                receipt, float(clock),
                receipt_path=f"{target}:launchd-interval-guard/status.json"),
            "error": ""}


def _epoch(stamp: Any) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError, OverflowError):
        return None


def judge(info: Dict[str, Any], threshold: int,
          episode_since: Optional[str] = None) -> Dict[str, Any]:
    """{"active", "resolved", "since", "hours"} for one peer read. Only a
    readable, fresh receipt decides anything, and only a receipt written
    after `episode_since` (the open alert's start) can end it."""
    guard = info.get("guard") or {}
    state = guard.get("state")
    out: Dict[str, Any] = {"active": False, "resolved": False, "since": None, "hours": None,
                           "state": state if info.get("readable") else "unreachable"}
    if state == "ok":
        written = (float(info["clock"]) - float(guard.get("age_seconds") or 0)
                   if info.get("clock") is not None else None)
        began = _epoch(episode_since) if episode_since else None
        out["resolved"] = (began is None
                           or (written is not None and written > began))
    elif state == "stalled":
        started = (guard.get("episode") or {}).get("started_ts")
        if isinstance(started, (int, float)):
            open_s = float(info["clock"]) - float(started)
            out.update(since=iso(float(started)), hours=round(open_s / 3600, 1),
                       active=open_s >= threshold)
    return out


def alert_text(peer: str, target: str, me: str, info: Dict[str, Any],
               verdict: Dict[str, Any], now: float) -> Tuple[str, str]:
    guard = info.get("guard") or {}
    stalled = [str(row.get("label")) for row in guard.get("stalled") or []
               if isinstance(row, dict)]
    paused = [str(row.get("label")) for row in guard.get("paused") or []
              if isinstance(row, dict)]
    held = any("self-update" in label for label in paused)
    title = TITLE.format(peer=peer, since=verdict["since"])
    body = "\n".join([
        f"{peer}'s launchd has not started its timer jobs on its own since {verdict['since']} "
        f"({verdict['hours']} h). "
        + ("The guard lists self-update as paused, so its tartci is not updating."
           if held else "The guard is starting the timers; self-update is not listed as paused."),
        f"Run: ssh {target} 'tartci doctor fleet'",
        f"Fix: reboot {peer} when its lanes are idle; a reboot clears the stall. "
        "Do not kickstart self-update while the stall lasts.",
        "",
        f"Stalled agents ({len(stalled)}): {', '.join(stalled[:8])}"
        + (" ..." if len(stalled) > 8 else ""),
        f"Paused: {', '.join(paused) or 'none'}",
        f"Guard: runner={guard.get('runner')} receipt_age_s={guard.get('age_seconds')}",
        f"Read by {me} over SSH at {iso(now)}.",
        "",
        "A peer opens this because a stalled host cannot be relied on to alert about itself. "
        "Every fleet host reads its peers; the first to see the stall opens this issue and the "
        "rest adopt it by its title. It closes itself when a host reads the stall over.",
    ])
    return title, body


def _scratch(directory: pathlib.Path) -> bool:
    scratch = os.path.realpath(tempfile.gettempdir())
    return os.path.realpath(str(directory)).startswith(scratch + os.sep)


def _open_or_adopt(title: str, body: str) -> Tuple[int, str]:
    """The open issue with exactly this title, else a new one."""
    if host_off._scratch_home():
        return 1, "refused: issue writes from a scratch TARTCI_HOME (a test run) never reach GitHub"
    rc, out = host_off._ghapp(
        ["api", f"repos/{host_off.ISSUE_REPO}/issues?state=open&per_page=100",
         "--jq", f'.[] | select(.title == "{title}") | .number'],
        host_off._update_checkout())
    found = [line.strip() for line in out.splitlines() if line.strip().isdigit()] if rc == 0 else []
    if found:
        return 0, found[0]
    return host_off._open_issue(title, body)


SSH_ALIAS_CONVENTION = "tartci-{host_id}"


def primary_reader(peer: str, hosts: List[str]) -> Optional[str]:
    """The host that acts for `peer`: the lowest published id other than it."""
    others = sorted(h for h in hosts if h != peer)
    return others[0] if others else None


def primary_healthy(last: Optional[Dict[str, Any]], clock: Optional[float], peer: str,
                    interval: int) -> bool:
    """Whether the primary's own last pass (read from it with its clock) is
    recent, within two read intervals, and read `peer`."""
    if not isinstance(last, dict) or clock is None or not isinstance(last.get("ts"), (int, float)):
        return False
    if not 0 <= clock - float(last["ts"]) <= 2 * interval:
        return False
    row = (last.get("peers") or {}).get(peer)
    return isinstance(row, dict) and not row.get("error")


def published_peers(home: Optional[pathlib.Path] = None,
                    run: Optional[Runner] = None) -> Tuple[Dict[str, str], Optional[str]]:
    """(host_id -> SSH target, this host's id or None) from main's published
    supply in the self-update checkout: the same hosts and targets
    fleet_self_update.published_peers reads. It is read here rather than
    imported so the watchdog's path stays free of self-update's dependencies
    (test_system_python_tests_run follows it). A `[peers]` override in
    self-update.toml applies when tomllib can read it."""
    home = home or pathlib.Path.home()
    checkout = home / ".local" / "share" / "tartci" / "update-checkout"
    rc, out, err = (run or _run)(["git", "-C", str(checkout), "show",
                                  "origin/main:fleet/advertised-labels.json"])
    try:
        value = json.loads(out) if rc == 0 else None
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise RuntimeError(f"published supply unreadable from {checkout}: {(err or out)[:200]}")
    rows = [r for r in value.get("hosts") or [] if isinstance(r, dict)]
    ssh = {r["host_id"]: r.get("ssh") for r in rows if isinstance(r.get("host_id"), str)}
    ids = {r["host_id"] for r in value.get("registrations") or []
           if isinstance(r, dict) and isinstance(r.get("host_id"), str)} | set(ssh)
    overrides: Dict[str, str] = {}
    try:
        import tomllib  # type: ignore[import-not-found]  # noqa: PLC0415 - 3.11+
        settings = home / ".config" / "tartci" / "self-update.toml"
        if settings.is_file():
            overrides = {str(k): str(v) for k, v in
                         (tomllib.loads(settings.read_text()).get("peers") or {}).items()}
    except Exception:  # noqa: BLE001 - 3.9 or an unreadable override: the published targets
        overrides = {}
    import vm_boot_alert  # noqa: PLC0415 - owns the profile's host id and its fallback
    me, _ = vm_boot_alert._alert_host()
    return ({host_id: overrides.get(host_id) or ssh.get(host_id)
             or SSH_ALIAS_CONVENTION.format(host_id=host_id) for host_id in sorted(ids)},
            me if me in ids else None)


def alert_pass(now: Optional[float] = None, directory: Optional[pathlib.Path] = None,
               peers: Optional[Dict[str, str]] = None, me: Optional[str] = None,
               run: Optional[Runner] = None, issue: Any = None,
               close: Any = None) -> Dict[str, Any]:
    """The watchdog's pass. Returns {"skipped": bool, "peers": {peer: verdict}}."""
    now = time.time() if now is None else now
    directory = directory or state_root()
    threshold = setting("TARTCI_PEER_STALL_ALERT_SECS", STALL_ALERT_SECS)
    interval = setting("TARTCI_PEER_STALL_READ_SECS", READ_SECS)
    directory.mkdir(parents=True, exist_ok=True)
    last_path = directory / "last-read.json"
    last = host_off._read_json(last_path) or {}
    if isinstance(last.get("ts"), (int, float)) and 0 <= now - float(last["ts"]) < interval:
        return {"skipped": True, "peers": last.get("peers") or {}}
    if peers is None:
        checkout = pathlib.Path.home() / ".local" / "share" / "tartci" / "update-checkout"
        if not (checkout / ".git").exists():
            # Not a managed fleet host (a CI runner, a fresh clone): there is
            # no published supply to read peers from, and nothing to say.
            return {"skipped": True, "peers": {}, "reason": "no self-update checkout"}
        peers, me = published_peers()
    result: Dict[str, Any] = {}
    enabled = (os.environ.get("TARTCI_PEER_STALL_ISSUE", "1") != "0"
               and (issue is not None or not _scratch(directory)))
    hosts = sorted(set(peers) | ({me} if me else set()))
    reads = {peer: read_peer(target, run) for peer, target in sorted(peers.items())
             if peer != me}
    misses = (last.get("primary_misses") or {}) if isinstance(last.get("primary_misses"), dict) \
        else {}
    next_misses: Dict[str, int] = {}
    for peer, target in sorted(peers.items()):
        if peer == me:
            continue
        info = reads[peer]
        state_path = directory / f"{peer}.json"
        held = host_off._read_json(state_path) or {}
        verdict = judge(info, threshold, held.get("since"))
        primary = primary_reader(peer, hosts)
        if me is None or primary == me:
            acting, why = True, "primary"
        else:
            pinfo = reads.get(primary) or {}
            if primary_healthy(pinfo.get("last"), pinfo.get("clock"), peer, interval):
                next_misses[peer] = 0
                acting, why = False, f"{primary} is the primary reader"
            else:
                next_misses[peer] = int(misses.get(peer) or 0) + 1
                acting = next_misses[peer] >= PRIMARY_MISSES
                why = (f"{primary} has not read {peer} for {next_misses[peer]} read(s)"
                       + ("; acting" if acting else ""))

        def raise_event(peer: str = peer, verdict: Dict[str, Any] = verdict) -> None:
            host_off.event(directory, "peer_launchd_stalled",
                           f"{peer}: launchd stalled since {verdict['since']} "
                           f"({verdict['hours']} h)",
                           {"peer": peer, "since": verdict["since"], "reader": me}, now)

        out: Dict[str, Any] = {}
        if acting or held:
            # A host that alerted keeps closing what it opened.
            out = host_off.episode_alert(
                state_path, active=verdict["active"] and acting,
                resolved=verdict["resolved"], since=verdict["since"],
                raise_event=raise_event,
                render=lambda peer=peer, target=target, info=info, verdict=verdict: alert_text(
                    peer, target, me or "a peer", info, verdict, now),
                issue=issue or _open_or_adopt, close=close, issues_enabled=enabled)
        result[peer] = {**verdict, "acting": acting, "why": why, "issue": out.get("issue"),
                        "closed": bool(out.get("closed")), "error": info.get("error") or None}
    host_off._write_json(last_path, {"ts": now, "peers": result,
                                     "primary_misses": next_misses})
    return {"skipped": False, "peers": result}
