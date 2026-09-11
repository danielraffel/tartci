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
from unittest import mock

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
disabled = wd.parse_disabled_services("""\
disabled services = {\n
\t\"com.example.retired\" => disabled\n
\t\"com.example.live\" => enabled\n
}\n
""")
check(disabled == {"com.example.retired"},
      f"launchd disabled map must preserve only exact disabled labels: {disabled!r}")


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

# Inventory failure is neither idle nor busy. It suppresses unsafe healing while
# retaining the cause for callers that must explain why admission was refused.
v, reason = wd.classify(
    "running", None, log_age_s=STALE + 1, stale_log_s=STALE,
    vm_running=None, vm_probe_reason="Tart executable unavailable",
)
check(v == "unknown", f"unavailable VM inventory must be unknown, got {v}")
check("Tart executable unavailable" in reason,
      f"unavailable inventory cause must survive classification: {reason}")

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


# ── Tart executable + inventory probe ───────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    tart_home = Path(td) / "VMs"
    tart_home.mkdir()
    fake_tart = Path(td) / "tart"
    fake_tart.write_text("#!/bin/sh\nprintf '[]\\n'\n")
    fake_tart.chmod(0o755)
    with mock.patch.dict(
        os.environ,
        {
            "PATH": "/usr/bin:/bin",
            "TARTCI_TART_CLI": str(fake_tart),
            "TART_HOME": str(tart_home),
        },
    ):
        probe = wd.probe_tart_vm_running()
    check(probe.running is False, f"explicit Tart under minimal PATH must prove idle: {probe}")
    check(probe.executable == str(fake_tart),
          f"probe must report the explicit executable it ran: {probe}")
    check(probe.tart_home == str(tart_home),
          f"probe must report the exact Tart store it inspected: {probe}")

with tempfile.TemporaryDirectory() as td:
    tart_home = Path(td) / "custom-store"
    tart_home.mkdir()
    observed_home = Path(td) / "observed-home"
    fake_tart = Path(td) / "tart"
    fake_tart.write_text(
        f"#!/bin/sh\nprintf '%s' \"$TART_HOME\" > '{observed_home}'\nprintf '[null]\\n'\n"
    )
    fake_tart.chmod(0o755)
    profile = Path(td) / "macos-fleet-profile.toml"
    profile.write_text(f'[host]\ntart_home = "{tart_home}"\n')
    with mock.patch.dict(
        os.environ,
        {
            "PATH": "/usr/bin:/bin",
            "TARTCI_TART_CLI": str(fake_tart),
            "TART_HOME": "",
            "TARTCI_MACOS_FLEET_PROFILE": str(profile),
        },
    ):
        probe = wd.probe_tart_vm_running()
    check(observed_home.read_text() == str(tart_home),
          "inventory command must receive the installed profile's custom Tart store")
    check(probe.running is None,
          f"malformed inventory entries must be unavailable, not idle: {probe}")
    check("non-object entry" in probe.reason,
          f"malformed inventory cause must survive the probe: {probe.reason}")

with tempfile.TemporaryDirectory() as td:
    fake_tart = Path(td) / "tart"
    fake_tart.write_text("#!/bin/sh\nexit 99\n")
    fake_tart.chmod(0o755)
    profile = Path(td) / "macos-fleet-profile.toml"
    profile.write_text('host = "malformed"\n')
    with mock.patch.dict(
        os.environ,
        {
            "PATH": "/usr/bin:/bin",
            "TARTCI_TART_CLI": str(fake_tart),
            "TART_HOME": "",
            "TARTCI_MACOS_FLEET_PROFILE": str(profile),
        },
    ):
        probe = wd.probe_tart_vm_running()
    check(probe.running is None,
          f"malformed profile shape must be unavailable, not crash or idle: {probe}")
    check("Tart store unavailable" in probe.reason,
          f"malformed profile must preserve a typed store cause: {probe.reason}")

with (
    mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "TARTCI_TART_CLI": ""}),
    mock.patch.object(wd.shutil, "which", return_value=None),
    mock.patch.object(wd.os.path, "isfile", return_value=False),
):
    probe = wd.probe_tart_vm_running()
check(probe.running is None, f"missing Tart must be unavailable, not busy: {probe}")
check("set TARTCI_TART_CLI" in probe.reason,
      f"unavailable Tart probe must explain the explicit override: {probe.reason}")

with (
    mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "TARTCI_TART_CLI": ""}),
    mock.patch.object(wd.shutil, "which", return_value=None),
    mock.patch.object(wd.os.path, "isfile", side_effect=lambda p: p == "/opt/homebrew/bin/tart"),
    mock.patch.object(wd.os, "access", side_effect=lambda p, _: p == "/opt/homebrew/bin/tart"),
):
    resolved, reason = wd.resolve_tart_cli()
check(resolved == "/opt/homebrew/bin/tart",
      f"minimal PATH must fall back to canonical Apple Silicon Tart: {resolved}, {reason}")


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

# Per-lane launchd disablement is more specific than host participation. A
# profile may keep the host on while an advisory/legacy lane remains disabled.
with tempfile.TemporaryDirectory() as td:
    plist = Path(td) / "com.danielraffel.pulp.tart-runner-linux.plist"
    with plist.open("wb") as fh:
        plistlib.dump({"Label": plist.stem}, fh)
    original_run = wd._run
    try:
        wd._run = lambda _cmd: (113, "", "Could not find service")
        health = wd.gather_health(
            plist.stem, str(plist), STALE, vm_running=False,
            pool_participating=True, service_enabled=False,
        )
        unknown = wd.gather_health(
            plist.stem, str(plist), STALE, vm_running=False,
            pool_participating=True, service_enabled=None,
        )
    finally:
        wd._run = original_run
    check(health.verdict == "healthy" and "explicitly disabled" in health.reason,
          f"disabled lane must not be healed: {health}")
    check(unknown.verdict == "unknown" and "refusing" in unknown.reason,
          f"unknown enablement must fail closed without heal: {unknown}")

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


# ── interval agents: staleness bound, application exits, reload refusal ──

def _agent_plist(directory: Path, label: str, log: Path,
                 interval: int | None = None) -> Path:
    """Write a LaunchAgent plist the way a rendered tartci template writes one."""
    data: dict = {"Label": label, "ProgramArguments": ["/bin/true"],
                  "StandardOutPath": str(log)}
    if interval is not None:
        data["StartInterval"] = interval
    path = directory / f"{label}.plist"
    with path.open("wb") as fh:
        plistlib.dump(data, fh)
    return path


def _print_output(state: str, exit_code: str) -> str:
    return f"state = {state}\nlast exit code = {exit_code}\nexit timeout = 5\n"


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    log = root / "reclaim.log"
    log.write_text("a pass ran\n")

    # The reader itself, before anything depends on it.
    hourly = _agent_plist(root, "com.danielraffel.tartci.reclaim", log, interval=3600)
    check(wd._start_interval_from_plist(str(hourly)) == 3600,
          "control: a declared StartInterval must be read back verbatim")
    no_interval = _agent_plist(root, "com.danielraffel.tartci.orchard-worker", log)
    check(wd._start_interval_from_plist(str(no_interval)) is None,
          "an agent with no StartInterval has no interval bound")
    for bad, why in ((0, "zero"), (-5, "negative"), ("3600", "a string"),
                     (True, "a bool")):
        junk = root / "junk.plist"
        with junk.open("wb") as fh:
            plistlib.dump({"Label": "com.danielraffel.tartci.junk",
                           "StartInterval": bad}, fh)
        check(wd._start_interval_from_plist(str(junk)) is None,
              f"{why} StartInterval must be None, never a collapsed bound")
    check(wd._start_interval_from_plist(str(root / "absent.plist")) is None,
          "an unreadable plist must not raise")

# An hourly agent is quiet between runs by design. The shared 1800s bound calls
# it frozen on every other pass and boots out an agent that is working.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    log = root / "reclaim.log"
    log.write_text("last pass\n")
    os.utime(log, (wd.utcnow() - 2400, wd.utcnow() - 2400))
    label = "com.danielraffel.tartci.reap"        # not in APPLICATION_EXIT_CODES
    hourly = _agent_plist(root, label, log, interval=3600)
    shared = _agent_plist(root, label + "-noint", log)

    original_run = wd._run
    try:
        wd._run = lambda _cmd: (0, _print_output("running", "(never exited)"), "")
        # Control FIRST, on the same instrument and the same log age: without a
        # declared interval this agent reads as alive-but-frozen.
        control = wd.gather_health(label + "-noint", str(shared), STALE,
                                   vm_running=False)
        bounded = wd.gather_health(label, str(hourly), STALE, vm_running=False)
        # And the bound never SHORTENS. A one-minute agent keeps the 1800s
        # floor, so a 600s gap is still healthy; 2 x 60s alone would call it
        # frozen five times over.
        brief = root / "brief.log"
        brief.write_text("last pass\n")
        os.utime(brief, (wd.utcnow() - 600, wd.utcnow() - 600))
        fast = _agent_plist(root, label + "-fast", brief, interval=60)
        floored = wd.gather_health(label + "-fast", str(fast), STALE,
                                   vm_running=False)
        # ... and the widening is 2 x interval, not unbounded patience.
        stretched = _agent_plist(root, label + "-stretched", log, interval=60)
        still_wedged = wd.gather_health(label + "-stretched", str(stretched),
                                        STALE, vm_running=False)
    finally:
        wd._run = original_run
    check(control.verdict == "wedged",
          f"control: no interval + 2400s stale log must be wedged, got {control}")
    check(bounded.verdict == "healthy",
          f"an hourly agent quiet for 2400s must not be wedged, got {bounded}")
    check(floored.verdict == "healthy",
          f"2 x 60s must never shorten the 1800s floor, got {floored}")
    check(still_wedged.verdict == "wedged",
          f"a 2400s gap must stay wedged at the 1800s floor, got {still_wedged}")

# An exit code the reclaimer DOCUMENTS is the agent reporting a condition, not
# a crash loop. Healing it reboots a working agent every hour and buries the
# condition; calling it healthy hides it. It gets its own verdict.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    log = root / "reclaim.log"
    log.write_text("pass finished\n")
    os.utime(log, (wd.utcnow() - 9999, wd.utcnow() - 9999))
    label = "com.danielraffel.tartci.reclaim"
    plist = _agent_plist(root, label, log, interval=3600)
    other = _agent_plist(root, "com.danielraffel.tartci.orchard-worker", log)

    original_run = wd._run
    try:
        verdicts = {}
        for code in (2, 3, 4):
            wd._run = lambda _cmd, c=code: (
                0, _print_output("not running", str(c)), "")
            verdicts[code] = wd.gather_health(label, str(plist), STALE,
                                              vm_running=False)
        # Controls, same instrument, same stale log:
        # 126 is the no-Full-Disk-Access wedge class and must stay healable.
        wd._run = lambda _cmd: (0, _print_output("not running", "126"), "")
        wedge_control = wd.gather_health(label, str(plist), STALE, vm_running=False)
        # and the map is per-label, so exit 3 from another agent is still a wedge.
        wd._run = lambda _cmd: (0, _print_output("not running", "3"), "")
        label_control = wd.gather_health("com.danielraffel.tartci.orchard-worker",
                                         str(other), STALE, vm_running=False)
    finally:
        wd._run = original_run
    for code, health in verdicts.items():
        check(health.verdict == "attention",
              f"documented exit {code} must be attention, got {health}")
        check("reload would only repeat it" in health.reason,
              f"exit {code} must say why it is not healed: {health.reason}")
    check(wedge_control.verdict == "wedged",
          f"control: exit 126 must stay on the wedge path, got {wedge_control}")
    check(label_control.verdict == "wedged",
          f"control: exit 3 from another label is still a wedge, got {label_control}")

# A running interval agent is running its ONE job. bootout lands mid-rmtree and
# leaves a half-deleted tree no later pass can classify.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    log = root / "reclaim.log"
    log.write_text("working\n")
    hourly = _agent_plist(root, "com.danielraffel.tartci.reclaim", log,
                          interval=3600)
    plain = _agent_plist(root, "com.danielraffel.tartci.orchard-worker", log)

    def _reloader(state: str):
        seen: list[list[str]] = []
        phase = {"loaded": True}

        def fake(cmd: list[str]) -> tuple[int, str, str]:
            seen.append(cmd)
            if cmd[1] == "bootout":
                phase["loaded"] = False
                return 0, "", ""
            if cmd[1] == "bootstrap":
                phase["loaded"] = True
                return 0, "", ""
            if cmd[1] == "print":
                if phase["loaded"]:
                    return 0, _print_output(state, "(never exited)"), ""
                return 113, "", "Could not find service"
            return 0, "", ""

        return fake, seen

    original_run = wd._run
    try:
        wd._run, calls_running = _reloader("running")
        refused = wd.reload_agent("com.danielraffel.tartci.reclaim", str(hourly))
        # Control 1: the same running state with no declared interval reloads.
        wd._run, calls_plain = _reloader("running")
        plain_ok = wd.reload_agent("com.danielraffel.tartci.orchard-worker",
                                   str(plain))
        # Control 2: the same interval agent NOT running is reloaded, so the
        # refusal above is the running state and not the plist.
        wd._run, calls_idle = _reloader("not running")
        idle_ok = wd.reload_agent("com.danielraffel.tartci.reclaim", str(hourly))
    finally:
        wd._run = original_run
    check(not refused, "a running interval agent must refuse reload")
    check(not any(c[1] == "bootout" for c in calls_running),
          "the refusal must happen before bootout, not after")
    check(plain_ok and any(c[1] == "bootout" for c in calls_plain),
          "control: a running agent with no interval still reloads")
    check(idle_ok and any(c[1] == "bootout" for c in calls_idle),
          "control: an idle interval agent still reloads")

# End to end through main(): attention is reported, never healed, and it makes
# --status exit non-zero. Without the consumer wiring the verdict is invented
# and then silently dropped.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    agents_dir = root / "LaunchAgents"
    agents_dir.mkdir()
    log = root / "reclaim.log"
    log.write_text("pass finished\n")
    os.utime(log, (wd.utcnow() - 9999, wd.utcnow() - 9999))
    _agent_plist(agents_dir, "com.danielraffel.tartci.reclaim", log, interval=3600)

    def _status(exit_code: str) -> tuple[int, str]:
        import io
        from contextlib import redirect_stdout
        original_run = wd._run
        buf = io.StringIO()
        try:
            wd._run = lambda _cmd: (0, _print_output("not running", exit_code), "")
            with mock.patch.object(
                wd, "probe_tart_vm_running",
                return_value=wd.TartVMProbe(False, "idle", "/bin/true", str(root))
            ), redirect_stdout(buf):
                rc = wd.main(["--status", "--launch-agents-dir", str(agents_dir),
                              "--participation-file", str(root / "participate")])
        finally:
            wd._run = original_run
        return rc, buf.getvalue()

    rc_attention, out_attention = _status("3")
    rc_clean, out_clean = _status("(never exited)")
    check(rc_attention == 1,
          "--status must exit non-zero on an agent reporting a condition")
    check("!" in out_attention,
          f"attention needs its own mark, not a tick: {out_attention!r}")
    check("[healed" not in out_attention and "would-heal" not in out_attention,
          f"attention must never be healed: {out_attention!r}")
    check(rc_clean == 0,
          f"control: the same agent exiting cleanly must exit 0, got {rc_clean}")


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
