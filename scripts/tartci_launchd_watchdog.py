#!/usr/bin/env python3
"""Self-heal watchdog for tartci LaunchAgents (macOS).

Why this exists
---------------
A tartci serve LaunchAgent (e.g. the required macOS build gate,
`com.danielraffel.pulp.tart-runner`) can wedge into an INVISIBLE crash-loop:
launchd keeps a job's spec in memory, and `KeepAlive`/`launchctl kickstart -k`
respawn that CACHED spec — they never re-read the plist from disk. If a plist is
edited (a CI routing / label change) or the tartci tree is moved/re-installed
WITHOUT a full `bootout`+`bootstrap`, launchd goes on respawning the stale spec.
When that stale spec points at a now-unreadable path (the classic LaunchAgent
`/Volumes` no-Full-Disk-Access case), every respawn exits 126 BEFORE the script
runs — so nothing is logged, the log freezes, and `runs=` climbs into the
thousands. Only `bootout`+`bootstrap` (which re-reads the plist) heals it. This
silently took a required CI gate offline for ~2 weeks.

No amount of in-script logging can catch this class (the script never runs), so
the recovery has to live OUTSIDE the wedged agent: this watchdog, on its own
`StartInterval` LaunchAgent, detects two wedge signatures and runs the one thing
that heals them — bootout+bootstrap:
  1. Invisible crash-loop — exited non-zero AND its log has gone stale.
  2. Alive-but-frozen — the process is UP (state=running) but its log has gone
     stale AND no VM is building (a hung `tart`/frozen run_one the in-supervisor
     self-heal can't catch). Gated on state=running + no running VM so a
     deliberately-stopped agent is never resurrected and a legit long build
     (which blocks the loop quietly) is never falsely healed.
  3. Missing-while-enabled — a runner plist exists and durable pool
     participation is ON, but the service is absent from launchd. Participation
     OFF remains authoritative and the watchdog never reloads those runners.
It is rate-limited so a genuinely-broken plist logs loudly instead of thrashing.

Modes
-----
  (default)         scan all tartci LaunchAgents, heal the wedged ones
  --status          report health only; never act (exit 0)
  --reload LABEL    force a full bootout+bootstrap+kickstart of one label
  --dry-run         report what heal WOULD do; never act
  --json            machine-readable output

The pure decision helpers (`parse_launchctl_print`, `classify`) take plain
strings so they can be unit-tested with no launchd present
(see scripts/test_tartci_launchd_watchdog.py).
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import plistlib
import subprocess
import sys
import time
from typing import Any, NamedTuple

# A LaunchAgent is considered tartci-owned if its Label starts with any of these.
TARTCI_LABEL_PREFIXES = (
    "com.danielraffel.pulp.tart-runner",
    "com.danielraffel.pulp.qemu-runner",
    "com.danielraffel.forge.tart-runner",
    "com.danielraffel.tartci.",
)
# The watchdog never heals itself (avoid a watchdog reload storm).
SELF_LABEL = "com.danielraffel.tartci.launchd-watchdog"

# A stale log older than this (seconds) is the shared staleness input to both wedge
# signatures (non-zero-exit crash-loop, and alive-but-frozen): a healthy serve loop
# writes a "waiting"/"SCAN BLIND" line every poll (~10-20s), so its log is never this
# old. During a legit build the log DOES go this stale (run_one is quiet until the job
# ends), which is why the alive-but-frozen signature also requires no running VM.
DEFAULT_STALE_LOG_S = 1800  # 30 min
# Rate limit: at most this many heals per label inside the window.
DEFAULT_MAX_HEALS = 3
DEFAULT_HEAL_WINDOW_S = 3600  # 1 hour


def utcnow() -> float:
    return time.time()


class AgentHealth(NamedTuple):
    label: str
    plist: str
    log_path: str | None
    state: str | None          # "running" | "spawn scheduled" | None (not loaded)
    last_exit_code: int | None
    log_age_s: float | None    # None when the log is missing
    verdict: str               # "healthy" | "wedged" | "unknown"
    reason: str


def parse_launchctl_print(text: str) -> tuple[str | None, int | None]:
    """Extract (state, last_exit_code) from `launchctl print` output.

    Pure: takes the raw text so it is unit-testable. Returns (None, None) when a
    field is absent (e.g. the service is not bootstrapped)."""
    state: str | None = None
    last_exit: int | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("state = "):
            state = line[len("state = "):].strip() or None
        elif line.startswith("last exit code = "):
            val = line[len("last exit code = "):].strip()
            # launchd prints "(never exited)" for a never-failed service.
            try:
                last_exit = int(val)
            except ValueError:
                last_exit = None
    return state, last_exit


def classify(
    state: str | None,
    last_exit_code: int | None,
    log_age_s: float | None,
    stale_log_s: int,
    vm_running: bool = True,
    expected_loaded: bool = False,
) -> tuple[str, str]:
    """Decide healthy / wedged / unknown from parsed signals. Pure.

    Two independent wedge signatures:

    1. **Invisible crash-loop** := exited non-zero AND its log has gone stale (or
       is missing). Distinguishes the crash-loop from a healthy between-jobs idle
       (running, fresh "waiting" log) and a momentary restart (non-zero exit,
       log still being written).
    2. **Alive-but-frozen** := the process is up (no non-zero exit) BUT its log has
       gone stale AND no VM is building. A healthy serve loop writes a "waiting"/
       "SCAN BLIND" line every poll (~10-20s), so a stale log while alive means the
       loop stopped iterating — a hung `tart`/boot or a frozen loop the in-supervisor
       self-heal can't catch (it never gets back to the top to increment `blind`).
       The `vm_running` guard is load-bearing: a legit long build blocks the loop
       quietly for up to hours, so we only call it frozen when NO VM is running."""
    if state is None and expected_loaded:
        return "wedged", "not loaded while pool participation is enabled"
    if last_exit_code is None or last_exit_code == 0:
        # Alive / cleanly-restarting. The alive-but-frozen signature applies ONLY when the process is
        # genuinely UP: state == "running". A frozen run_one still reports "running" (the process is
        # up, just stuck), so that's the case we want. A None/absent state means the agent is NOT
        # loaded — deliberately stopped (pool-off) or a staged-but-unloaded plist — and must never be
        # resurrected; `launchctl print` returns rc!=0 for those, which gather_health maps to
        # state=None. So `state == "running"` is the load-bearing guard against reviving a stopped host.
        if (state == "running" and not vm_running
                and log_age_s is not None and log_age_s > stale_log_s):
            return ("wedged",
                    f"alive but frozen: log stale {int(log_age_s)}s (> {stale_log_s}s) "
                    "and no VM building")
        if last_exit_code is None:
            return "healthy", "no non-zero exit recorded"
        return "healthy", "clean last exit"
    # last_exit_code != 0 from here.
    if log_age_s is None:
        return "wedged", f"exited {last_exit_code}, log missing"
    if log_age_s > stale_log_s:
        return (
            "wedged",
            f"exited {last_exit_code}, log stale {int(log_age_s)}s "
            f"(> {stale_log_s}s)",
        )
    # Non-zero exit but the log is fresh → a live restart, give it time.
    return "healthy", f"exited {last_exit_code} but log fresh ({int(log_age_s)}s)"


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _domain() -> str:
    return f"gui/{os.getuid()}"


def discover_agents(launch_agents_dir: str) -> list[tuple[str, str]]:
    """Return (label, plist_path) for every tartci-owned LaunchAgent plist."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path in sorted(glob.glob(os.path.join(launch_agents_dir, "*.plist"))):
        try:
            with open(path, "rb") as fh:
                data = plistlib.load(fh)
        except Exception:
            continue
        label = data.get("Label", "")
        if label == SELF_LABEL or label in seen:
            continue
        if label.startswith(TARTCI_LABEL_PREFIXES):
            seen.add(label)
            out.append((label, path))
    return out


def _log_path_from_plist(plist_path: str) -> str | None:
    try:
        with open(plist_path, "rb") as fh:
            data = plistlib.load(fh)
    except Exception:
        return None
    return data.get("StandardOutPath") or data.get("StandardErrorPath")


def any_tart_vm_running() -> bool:
    """True if any Tart VM is running on this host — a conservative guard for the alive-but-frozen
    signature so the watchdog never heals a supervisor that is quietly blocked on a legitimate long
    build. FAILS SAFE: on any error reading `tart list`, returns True (assume a build is running →
    do not heal on staleness). Host-wide (not per-lane) on purpose: simpler and strictly safer."""
    try:
        rc, out, _ = _run(["tart", "list", "--format", "json"])
        if rc != 0:
            return True
        return any(str(vm.get("State", vm.get("state", ""))).lower().startswith("run")
                   for vm in (json.loads(out) or []))
    except Exception:
        return True


def gather_health(label: str, plist_path: str, stale_log_s: int,
                  vm_running: bool = True,
                  pool_participating: bool = False) -> AgentHealth:
    rc, out, _ = _run(["launchctl", "print", f"{_domain()}/{label}"])
    state, last_exit = parse_launchctl_print(out) if rc == 0 else (None, None)
    log_path = _log_path_from_plist(plist_path)
    log_age: float | None = None
    if log_path and os.path.exists(log_path):
        log_age = max(0.0, utcnow() - os.path.getmtime(log_path))
    expected_loaded = pool_participating and is_pool_runner(label)
    verdict, reason = classify(
        state, last_exit, log_age, stale_log_s, vm_running, expected_loaded
    )
    return AgentHealth(label, plist_path, log_path, state, last_exit,
                       log_age, verdict, reason)


def is_pool_runner(label: str) -> bool:
    """Whether LABEL is controlled by the host participation toggle."""
    return ".tart-runner" in label or ".qemu-runner" in label


def pool_participating(path: str) -> bool:
    """Read durable pool intent. Missing/unrecognised values preserve legacy ON.

    `tartci pool` and Shipyard use the numeric 1/0 contract; accepting the old
    true/false spelling keeps deployed hosts compatible.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip().lower() not in {"0", "false", "off", "draining"}
    except OSError:
        return True


def reload_agent(label: str, plist_path: str, dry_run: bool = False) -> bool:
    """Full bootout+bootstrap+kickstart — the ONLY thing that clears a stale
    cached job spec. `kickstart -k` alone re-runs the stale spec, so we never
    use it in isolation."""
    dom = _domain()
    if dry_run:
        return True
    # bootout may fail if not currently loaded — that's fine, bootstrap re-reads.
    _run(["launchctl", "bootout", f"{dom}/{label}"])
    rc, _, err = _run(["launchctl", "bootstrap", dom, plist_path])
    if rc != 0:
        # already-bootstrapped race: kickstart still forces a fresh start.
        pass
    rc2, _, _ = _run(["launchctl", "kickstart", "-k", f"{dom}/{label}"])
    if rc2 != 0:
        return False
    # Prove launchd now owns the service. It may still be starting, so loaded
    # (rather than state=running) is the correct immediate postcondition.
    rc3, _, _ = _run(["launchctl", "print", f"{dom}/{label}"])
    return rc3 == 0


# ── rate limiting ────────────────────────────────────────────────────────────

def _state_file() -> str:
    home = os.environ.get("HOME", os.path.expanduser("~"))
    d = os.environ.get("TARTCI_HOME", os.path.join(home, ".tartci"))
    return os.path.join(d, "state", "launchd-watchdog.json")


def load_heal_log(path: str) -> dict[str, list[float]]:
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def heals_in_window(stamps: list[float], now: float, window_s: int) -> list[float]:
    return [t for t in stamps if now - t < window_s]


def should_heal(
    stamps: list[float], now: float, window_s: int, max_heals: int
) -> bool:
    return len(heals_in_window(stamps, now, window_s)) < max_heals


def save_heal_log(path: str, data: dict[str, list[float]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true",
                    help="report health only; take no action")
    ap.add_argument("--reload", metavar="LABEL",
                    help="force a full bootout+bootstrap+kickstart of one label")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what heal would do; take no action")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--stale-log-seconds", type=int, default=DEFAULT_STALE_LOG_S)
    ap.add_argument("--max-heals", type=int, default=DEFAULT_MAX_HEALS)
    ap.add_argument("--heal-window-seconds", type=int,
                    default=DEFAULT_HEAL_WINDOW_S)
    ap.add_argument("--launch-agents-dir",
                    default=os.path.join(
                        os.environ.get("HOME", os.path.expanduser("~")),
                        "Library", "LaunchAgents"))
    ap.add_argument("--participation-file",
                    default=os.path.join(
                        os.environ.get("HOME", os.path.expanduser("~")),
                        ".config", "tartci", "native-build-participation"))
    args = ap.parse_args(argv)

    if args.reload:
        plist = os.path.join(args.launch_agents_dir, f"{args.reload}.plist")
        if not os.path.exists(plist):
            print(f"launchd-watchdog: no plist for {args.reload} at {plist}",
                  file=sys.stderr)
            return 1
        ok = reload_agent(args.reload, plist, dry_run=args.dry_run)
        print(f"launchd-watchdog: {'would reload' if args.dry_run else 'reloaded'} "
              f"{args.reload} — {'ok' if ok else 'FAILED'}")
        return 0 if ok else 1

    agents = discover_agents(args.launch_agents_dir)
    # Compute the VM-running guard ONCE per pass (host-wide) — the alive-but-frozen signature only
    # fires when nothing is building, so a legit long build is never healed.
    vm_running = any_tart_vm_running()
    participating = pool_participating(args.participation_file)
    health = [
        gather_health(lbl, p, args.stale_log_seconds, vm_running, participating)
        for lbl, p in agents
    ]

    now = utcnow()
    heal_log = load_heal_log(_state_file())
    results: list[dict[str, Any]] = []
    acted = False
    heal_failed = False
    for h in health:
        entry: dict[str, Any] = {
            "label": h.label, "verdict": h.verdict, "reason": h.reason,
            "state": h.state, "last_exit_code": h.last_exit_code,
            "log_age_s": None if h.log_age_s is None else int(h.log_age_s),
        }
        if h.verdict == "wedged" and not args.status:
            stamps = heal_log.get(h.label, [])
            if should_heal(stamps, now, args.heal_window_seconds, args.max_heals):
                ok = reload_agent(h.label, h.plist, dry_run=args.dry_run)
                entry["action"] = "would-heal" if args.dry_run else (
                    "healed" if ok else "heal-failed")
                if not args.dry_run:
                    stamps = heals_in_window(stamps, now, args.heal_window_seconds)
                    stamps.append(now)
                    heal_log[h.label] = stamps
                    acted = True
                    heal_failed = heal_failed or not ok
            else:
                entry["action"] = "rate-limited"
                entry["reason"] += (
                    f"; {args.max_heals} heals already in "
                    f"{args.heal_window_seconds}s — logging loudly instead of "
                    "thrashing (likely a genuinely broken plist)")
        results.append(entry)

    if acted:
        save_heal_log(_state_file(), heal_log)

    wedged = [r for r in results if r["verdict"] == "wedged"]
    if args.json:
        print(json.dumps({"ts": _iso(now), "agents": results}, indent=2))
    else:
        if not results:
            print("launchd-watchdog: no tartci LaunchAgents found")
        for r in results:
            mark = {"healthy": "✓", "wedged": "✗", "unknown": "?"}.get(
                r["verdict"], "?")
            act = f" [{r['action']}]" if "action" in r else ""
            print(f"  {mark} {r['label']}: {r['reason']}{act}")
    # Status reports unresolved wedges. Healing reports failure only when a
    # reload failed its postcondition; successful recovery exits zero.
    if args.status and wedged:
        return 1
    return 1 if heal_failed else 0


if __name__ == "__main__":
    sys.exit(main())
