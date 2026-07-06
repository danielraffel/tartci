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
import sys

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


# ── classify ─────────────────────────────────────────────────────────────────
STALE = wd.DEFAULT_STALE_LOG_S

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
