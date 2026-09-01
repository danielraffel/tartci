#!/usr/bin/env python3
"""Hermetic tests for the tartci LaunchAgent self-heal watchdog.

Exercises the pure decision logic — parsing `launchctl print` output, the
healthy/wedged classifier, and the heal rate-limiter — with synthetic inputs, so
no launchd, no subprocess, no filesystem is required. This runs on any platform
in CI (the wedge-detection contract is what we must not regress).

Run:  python3 scripts/test_tartci_launchd_watchdog.py
"""

from __future__ import annotations

import os
import plistlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tartci_launchd_watchdog as wd  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


# ── parse_launchctl_print ────────────────────────────────────────────────────
# The exact shape observed on a wedged required-gate agent: crash-looping,
# scheduled to respawn, last exit 126.
WEDGED_PRINT = """\
com.danielraffel.pulp.tart-runner = {
\tstate = spawn scheduled
\tprogram = /bin/bash
\truns = 1928
\tlast exit code = 126
\tpid = (not running)
}
"""
HEALTHY_PRINT = """\
com.danielraffel.pulp.tart-runner = {
\tstate = running
\tpid = 35016
\truns = 1
\tlast exit code = (never exited)
}
"""

state, exit_code = wd.parse_launchctl_print(WEDGED_PRINT)
check(state == "spawn scheduled", f"wedged state parse: {state!r}")
check(exit_code == 126, f"wedged exit parse: {exit_code!r}")

state, exit_code = wd.parse_launchctl_print(HEALTHY_PRINT)
check(state == "running", f"healthy state parse: {state!r}")
check(exit_code is None, f"healthy '(never exited)' → None, got {exit_code!r}")

state, exit_code = wd.parse_launchctl_print("")
check(state is None and exit_code is None, "empty print → (None, None)")
check(wd.parse_launchctl_exit_timeout("exit timeout = 5\n") == 5.0,
      "loaded exit timeout must parse")
check(wd.parse_launchctl_exit_timeout("exit timeout = 0\n") == 0.0,
      "infinite loaded exit timeout must remain distinguishable")
check(wd.parse_launchctl_exit_timeout("") is None,
      "missing loaded exit timeout must remain unknown")
check(wd.launchctl_reports_absent(
          113, 'Could not find service "x" in domain for user gui: 501'),
      "launchctl not-found response must prove absence")
check(not wd.launchctl_reports_absent(1, "permission denied"),
      "generic launchctl failure must not prove absence")


# ── classify ─────────────────────────────────────────────────────────────────
STALE = wd.DEFAULT_STALE_LOG_S
RESTART_GRACE = wd.DEFAULT_RESTART_GRACE_S

# The incident signature: non-zero exit + a 2-week-stale log → wedged.
v, _ = wd.classify("spawn scheduled", 126, log_age_s=14 * 24 * 3600,
                   stale_log_s=STALE)
check(v == "wedged", f"exit126 + stale log must be wedged, got {v}")

# Missing log alongside a non-zero exit → wedged (died before it could log).
v, _ = wd.classify("spawn scheduled", 126, log_age_s=None, stale_log_s=STALE)
check(v == "wedged", f"exit126 + missing log must be wedged, got {v}")

# Non-zero exit but a FRESH log → a live restart, not the invisible wedge.
v, _ = wd.classify("running", 1, log_age_s=5.0, stale_log_s=STALE)
check(v == "healthy", f"non-zero exit + fresh log must be healthy, got {v}")

# The exact M1 incident: `serve --loop` exited 75 to refresh App auth, but
# launchd retained a loaded, not-running job instead of honoring KeepAlive.
v, reason = wd.classify(
    "not running", 75, log_age_s=RESTART_GRACE + 1,
    stale_log_s=STALE, expected_loaded=True,
)
check(v == "wedged", f"stalled EX_TEMPFAIL restart must be wedged, got {v}: {reason}")

# Preserve a bounded launchd respawn window so a normal exit/restart transition
# is not booted out while it is still progressing.
v, _ = wd.classify(
    "not running", 75, log_age_s=RESTART_GRACE,
    stale_log_s=STALE, expected_loaded=True,
)
check(v == "healthy", f"EX_TEMPFAIL at restart grace must remain healthy, got {v}")

# Durable participation remains the authority: the watchdog must not revive an
# intentionally disabled lane merely because its last exit was EX_TEMPFAIL.
v, _ = wd.classify(
    "not running", 75, log_age_s=RESTART_GRACE + 1,
    stale_log_s=STALE, expected_loaded=False,
)
check(v == "healthy", f"disabled EX_TEMPFAIL lane must remain stopped, got {v}")

# Never exited non-zero → healthy regardless of state.
v, _ = wd.classify("running", None, log_age_s=None, stale_log_s=STALE)
check(v == "healthy", f"never-exited must be healthy, got {v}")

# Clean last exit → healthy.
v, _ = wd.classify("spawn scheduled", 0, log_age_s=999999, stale_log_s=STALE)
check(v == "healthy", f"exit 0 must be healthy, got {v}")

# Exactly at the threshold is NOT yet stale (> is strict).
v, _ = wd.classify("spawn scheduled", 126, log_age_s=STALE, stale_log_s=STALE)
check(v == "healthy", f"exit126 at exactly stale threshold not yet wedged, got {v}")

# Alive-but-frozen: up (no non-zero exit) + stale log + NO VM building → wedged.
v, _ = wd.classify("running", None, log_age_s=STALE + 1, stale_log_s=STALE, vm_running=False)
check(v == "wedged", f"alive + stale log + no VM must be wedged, got {v}")

# The VM guard: same stale log but a VM IS building → healthy (a legit long build, don't heal).
v, _ = wd.classify("running", None, log_age_s=STALE + 1, stale_log_s=STALE, vm_running=True)
check(v == "healthy", f"alive + stale log but VM building must be healthy, got {v}")

# Alive + FRESH log + no VM → healthy (a healthy idle loop writes every poll).
v, _ = wd.classify("running", None, log_age_s=5.0, stale_log_s=STALE, vm_running=False)
check(v == "healthy", f"alive + fresh log must be healthy, got {v}")

# Same alive-but-frozen signature for a clean-exit (0) respawn that then froze.
v, _ = wd.classify("running", 0, log_age_s=STALE + 1, stale_log_s=STALE, vm_running=False)
check(v == "wedged", f"exit0 + stale log + no VM must be wedged, got {v}")

# NOT-loaded (deliberately stopped / staged-but-unloaded) → state is None → NEVER resurrect, even
# with a stale log and no VM. The alive-but-frozen signature is gated on state == "running".
v, _ = wd.classify(None, None, log_age_s=STALE + 1, stale_log_s=STALE, vm_running=False)
check(v == "healthy", f"unloaded agent (state None) must be healthy, not resurrected, got {v}")

# Same for a stopped agent that last exited 0 while unloaded.
v, _ = wd.classify(None, 0, log_age_s=STALE + 1, stale_log_s=STALE, vm_running=False)
check(v == "healthy", f"unloaded exit0 agent must be healthy, got {v}")


# ── persistent Actions runner installation drift ──────────────────────────
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    agents = root / "LaunchAgents"
    agents.mkdir()
    missing = root / "actions-runner-pulp-preamble" / "runsvc.sh"
    actions_plist = agents / "actions.runner.Generous-Corp-pulp.pulp-preamble-m3.plist"
    with actions_plist.open("wb") as fh:
        plistlib.dump(
            {
                "Label": "actions.runner.Generous-Corp-pulp.pulp-preamble-m3",
                "ProgramArguments": [str(missing)],
                "StandardOutPath": str(root / "runner.log"),
            },
            fh,
        )
    unrelated = agents / "com.apple.unrelated.plist"
    with unrelated.open("wb") as fh:
        plistlib.dump({"Label": "com.apple.unrelated", "Program": "/bin/true"}, fh)

    discovered = wd.discover_agents(str(agents))
    check(
        discovered
        == [("actions.runner.Generous-Corp-pulp.pulp-preamble-m3", str(actions_plist))],
        f"persistent Actions runner must be discovered without unrelated agents: {discovered!r}",
    )
    original_run = wd._run
    try:
        wd._run = lambda _cmd: (0, "state = spawn scheduled\nlast exit code = 78\n", "")
        health = wd.gather_health(
            discovered[0][0], discovered[0][1], STALE, vm_running=False
        )
    finally:
        wd._run = original_run
    check(
        health.verdict == "broken",
        f"missing runsvc must be broken, got {health.verdict}",
    )
    check(str(missing) in health.reason, "missing executable path must be actionable")
    check("reload cannot repair" in health.reason, "must refuse blind kickstart repair")

    missing.parent.mkdir()
    missing.write_text("#!/bin/sh\n")
    old_log = root / "runner.log"
    old_log.write_text("idle runner\n")
    os.utime(old_log, (0, 0))
    original_run = wd._run
    try:
        wd._run = lambda _cmd: (0, "state = running\nlast exit code = (never exited)\n", "")
        health = wd.gather_health(
            discovered[0][0], discovered[0][1], STALE, vm_running=False
        )
    finally:
        wd._run = original_run
    check(
        health.verdict == "healthy",
        f"present runsvc must use normal health path, got {health}",
    )
    check(
        "runtime health is owned by Shipyard" in health.reason,
        "persistent Actions services must never enter TartCI stale-log healing",
    )

# Missing runner + participation ON is the fleet-offline incident: it must heal.
v, reason = wd.classify(None, None, log_age_s=None, stale_log_s=STALE,
                        vm_running=False, expected_loaded=True)
check(v == "wedged", f"enabled but unloaded runner must be wedged, got {v}: {reason}")

# Participation OFF remains authoritative and must never resurrect a runner.
v, _ = wd.classify(None, None, log_age_s=None, stale_log_s=STALE,
                   vm_running=False, expected_loaded=False)
check(v == "healthy", f"disabled unloaded runner must remain healthy, got {v}")

check(wd.is_pool_runner("com.danielraffel.pulp.tart-runner"),
      "Pulp tart runner must be pool-controlled")
check(wd.is_pool_runner("com.danielraffel.forge.tart-runner-macos"),
      "Forge tart runner must be pool-controlled")
check(not wd.is_pool_runner("com.danielraffel.tartci.orchard-worker"),
      "orchard worker must not inherit pool participation")

with tempfile.TemporaryDirectory() as td:
    participation = os.path.join(td, "participate")
    check(wd.pool_participating(participation), "missing participation flag defaults ON")
    with open(participation, "w", encoding="utf-8") as fh:
        fh.write("false\n")
    check(not wd.pool_participating(participation), "false participation flag is OFF")
    with open(participation, "w", encoding="utf-8") as fh:
        fh.write("0\n")
    check(not wd.pool_participating(participation), "numeric zero participation is OFF")
    with open(participation, "w", encoding="utf-8") as fh:
        fh.write("true\n")
    check(wd.pool_participating(participation), "true participation flag is ON")

# Recovery success is verified with a final launchctl print, not inferred from
# bootstrap/kickstart exit status.
reload_plist_dir = tempfile.TemporaryDirectory()
reload_plist = Path(reload_plist_dir.name) / "runner.plist"
with reload_plist.open("wb") as fh:
    plistlib.dump({}, fh)
original_run = wd._run
calls: list[list[str]] = []
reload_state = {"phase": "loaded", "drain_prints": 0}


def fake_run_ok(cmd: list[str]) -> tuple[int, str, str]:
    calls.append(cmd)
    action = cmd[1]
    if action == "bootout":
        reload_state["phase"] = "draining"
        return 0, "", ""
    if action == "print" and reload_state["phase"] == "draining":
        reload_state["drain_prints"] += 1
        if reload_state["drain_prints"] == 1:
            return 0, "state = running\n", ""
        reload_state["phase"] = "unloaded"
        return 113, "", "Could not find service"
    if action == "bootstrap":
        reload_state["phase"] = "loaded"
    if action == "print" and reload_state["phase"] == "loaded":
        return 0, "state = running\nexit timeout = 5\n", ""
    return 0, "", ""


wd._run = fake_run_ok
check(wd.reload_agent("com.danielraffel.pulp.tart-runner", str(reload_plist)),
      "reload must succeed when post-recovery launchctl print succeeds")
check(calls[-1][1] == "print", "reload must finish with launchctl print verification")
check(reload_state["drain_prints"] == 2,
      "reload must wait until asynchronous bootout is absent before bootstrap")


def fake_run_missing(cmd: list[str]) -> tuple[int, str, str]:
    return ((113, "", "Could not find service")
            if len(cmd) > 1 and cmd[1] == "print" else (0, "", ""))


wd._run = fake_run_missing
check(not wd.reload_agent("com.danielraffel.pulp.tart-runner", str(reload_plist)),
      "reload must fail when the service is still absent after kickstart")
wd._run = original_run

calls = []
wd._run = lambda cmd: (
    calls.append(cmd)
    or (0, "state = running\nexit timeout = 0\n", "")
)
check(not wd.reload_agent("com.example.infinite", str(reload_plist)),
      "loaded infinite teardown must refuse reload")
check(not any(cmd[1] == "bootout" for cmd in calls),
      "loaded infinite teardown refusal must happen before bootout")
wd._run = original_run

calls = []
absent_state = {"loaded": False}


def fake_absent_then_loaded(cmd: list[str]) -> tuple[int, str, str]:
    calls.append(cmd)
    if cmd[1] == "print":
        if absent_state["loaded"]:
            return 0, "state = running\nexit timeout = 0\n", ""
        return 113, "", "Could not find service"
    if cmd[1] == "bootstrap":
        absent_state["loaded"] = True
    return 0, "", ""


wd._run = fake_absent_then_loaded
check(wd.reload_agent("com.example.absent-infinite", str(reload_plist)),
      "already-absent infinite-timeout job must bootstrap without teardown")
check(not any(cmd[1] == "bootout" for cmd in calls),
      "already-absent job must not be booted out")
wd._run = original_run
reload_plist_dir.cleanup()


# ── rate limiter ─────────────────────────────────────────────────────────────
NOW = 1_000_000.0
WINDOW = 3600
MAXH = 3

# No prior heals → allowed.
check(wd.should_heal([], NOW, WINDOW, MAXH), "first heal allowed")

# Under the cap within the window → allowed.
check(wd.should_heal([NOW - 10, NOW - 20], NOW, WINDOW, MAXH),
      "2 heals in-window (< 3) → allowed")

# At the cap within the window → blocked (log loudly instead of thrashing).
check(not wd.should_heal([NOW - 10, NOW - 20, NOW - 30], NOW, WINDOW, MAXH),
      "3 heals in-window (== cap) → blocked")

# Old heals fall out of the window → allowed again.
check(wd.should_heal([NOW - 4000, NOW - 5000, NOW - 6000], NOW, WINDOW, MAXH),
      "3 heals all outside window → allowed again")

# heals_in_window prunes correctly.
kept = wd.heals_in_window([NOW - 10, NOW - 4000], NOW, WINDOW)
check(kept == [NOW - 10], f"heals_in_window prune: {kept}")


if FAILS:
    print("FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("tartci_launchd_watchdog: all checks passed")
