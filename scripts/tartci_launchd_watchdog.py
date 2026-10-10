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
  1. Invisible crash-loop — exited non-zero AND its log has gone stale AND no VM
     is building. The VM condition matters as much here as in (2): a supervisor
     exits EX_TEMPFAIL by design for its App-auth refresh and launchd reports
     that code for the whole life of the respawn, so a quiet 30 minutes inside a
     long build looks identical to a crash-loop on exit code and log age alone.
  2. Alive-but-frozen — the process is UP (state=running) but its log has gone
     stale AND no VM is building (a hung `tart`/frozen run_one the in-supervisor
     self-heal can't catch). Gated on state=running + no running VM so a
     deliberately-stopped agent is never resurrected and a legit long build
     (which blocks the loop quietly) is never falsely healed.
  3. Missing-while-enabled — a runner plist exists and durable pool
     participation is ON, but the service is absent from launchd. Participation
     OFF remains authoritative and the watchdog never reloads those runners.
It is rate-limited so a genuinely-broken plist logs loudly instead of thrashing.

The same pass audits persistent GitHub Actions runner LaunchAgents
(`actions.runner.*`). If one still has a plist but its declared executable has
disappeared, reload cannot recreate the runner installation. That condition is
reported as non-healable `broken` drift with a versioned recovery pointer.

Modes
-----
  (default)         scan managed TartCI + Actions LaunchAgents; heal reloadable wedges
  --status          report health only; never act (exit 0)
  --reload LABEL    full bootout+bootstrap+kickstart of one label. Refuses
                    (exit 3) while that lane is mid-job - its supervisor owns a
                    `tart run` VM or `Runner.Worker` - or its busy state is
                    unknown, unless --allow-mid-job. Exit 1 = a step failed.
  --dry-run         run every precondition and print the plan; never act.
                    With --reload: exit 0 would proceed, 3 would refuse
  --json            machine-readable output

The pure decision helpers (`parse_launchctl_print`, `classify`) take plain
strings so they can be unit-tested with no launchd present
(see scripts/test_tartci_launchd_watchdog.py).
"""

from __future__ import annotations

import re
import argparse
import datetime as dt
import glob
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from typing import Any, NamedTuple

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only by macOS system Python < 3.11
    tomllib = None  # type: ignore[assignment]

# A LaunchAgent is considered tartci-owned if its Label starts with any of these.
TARTCI_LABEL_PREFIXES = (
    "com.danielraffel.pulp.tart-runner",
    "com.danielraffel.pulp.qemu-runner",
    "com.danielraffel.forge.tart-runner",
    "com.danielraffel.tartci.",
)
ACTIONS_RUNNER_LABEL_PREFIX = "actions.runner."
# The watchdog never heals itself (avoid a watchdog reload storm).
SELF_LABEL = "com.danielraffel.tartci.launchd-watchdog"

# A stale log older than this (seconds) is the shared staleness input to both wedge
# signatures (non-zero-exit crash-loop, and alive-but-frozen): a healthy serve loop
# writes a "waiting"/"SCAN BLIND" line every poll (~10-20s), so its log is never this
# old. During a legit build the log DOES go this stale (run_one is quiet until the job
# ends), which is why the alive-but-frozen signature also requires no running VM.
DEFAULT_STALE_LOG_S = 1800  # 30 min



def default_stale_log_seconds() -> int:
    """TARTCI_WATCHDOG_STALE_LOG_SECONDS (rendered from the fleet profile's
    [launchd_watchdog] stale_log_seconds), else DEFAULT_STALE_LOG_S. A
    malformed value keeps the default rather than failing the pass."""
    raw = os.environ.get("TARTCI_WATCHDOG_STALE_LOG_SECONDS", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_STALE_LOG_S
# `serve ... --loop` deliberately exits EX_TEMPFAIL after sustained GitHub
# observation blindness so launchd can give it a fresh App-auth environment.
# If launchd does not respawn it, that explicit restart contract has failed; do
# not make a known-idle lane wait for the generic 30-minute crash-loop bound.
DEFAULT_RESTART_GRACE_S = 60
# Exit codes with which a tartci agent reports APPLICATION state: the program
# ran to completion and is reporting a condition a reload cannot fix. Treating
# those as the crash-loop signature boots out a working agent every hour and
# buries the condition it was reporting. Everything NOT listed here stays on
# the wedge path on purpose - 126/127 (not executable, not found) and
# signal-derived exits are exactly the no-Full-Disk-Access wedge class.
# Labels whose single run cannot be interrupted at an arbitrary point. A
# bootout mid-run leaves state no later pass can classify: the reclaimer is
# mid-rmtree, so the tree it was removing is left half-deleted. Membership is
# declared rather than inferred from the plist carrying a StartInterval,
# because every supervisor tick on this host is also an interval agent and
# those ARE safe to cut - inferring it would silently retire the
# alive-but-frozen heal for all of them, which is the watchdog's main job.
UNINTERRUPTIBLE_AGENTS: frozenset[str] = frozenset({
    "com.danielraffel.tartci.reclaim",
    # Quiet for up to 90 minutes while it waits for lanes to go idle, and
    # mid-install after that: a bootout there strands the host drained.
    "com.danielraffel.tartci.self-update",
    # Up to three hours inside `shipyard run`: a run cut mid-way leaves a
    # half-run no later pass can classify.
    "com.danielraffel.tartci.reuse-canary",
})
APPLICATION_EXIT_CODES: dict[str, dict[int, str]] = {
    "com.danielraffel.tartci.reclaim": {
        2: "unusable scan root or bad arguments",
        3: "free space still below the floor after reclaiming",
        4: "process table unreadable, so no build directory could be proven idle",
        5: "boot data volume still below its own floor after reclaiming",
    },
    "com.danielraffel.tartci.self-update": {
        3: ("a precondition refused (capacity floor, rate limit, halt), or the host has been "
            "deferred in the update queue past the starvation bound; host untouched"),
        4: "an update failed and the host was restored to the previous generation",
        5: "tartci skew could not be measured",
    },
    "com.danielraffel.tartci.reuse-canary": {
        3: "a gate refused (pool off or draining, Shipyard not in shadow_compare); host untouched",
        4: "shipyard run failed or exceeded its bound",
        5: "shipyard reuse records unreadable, or the [reuse_canary] profile table is invalid",
        6: "origin/main's head or the canary worktree could not be prepared",
    },
}
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
    # "attention" is neither: the agent ran and reported a condition of its
    # own. It is never healed (a reload would just repeat it) but it IS
    # reported, and --status exits non-zero on it.
    verdict: str               # "healthy" | "attention" | "wedged" | "broken" | "unknown"
    reason: str


class TartVMProbe(NamedTuple):
    running: bool | None       # None means inventory unavailable, not busy or idle
    reason: str
    executable: str | None
    tart_home: str | None


def parse_launchctl_print(text: str) -> tuple[str | None, int | None]:
    """Extract (state, last_exit_code) from `launchctl print` output.

    Pure: takes the raw text so it is unit-testable. Returns (None, None) when a
    field is absent (e.g. the service is not bootstrapped)."""
    state: str | None = None
    last_exit: int | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("state = "):
            # Only the JOB's state, which launchd prints first. Nested blocks
            # (coalition, endpoints) print their own `state = active` deeper in
            # the tree, and stripping indentation makes them indistinguishable —
            # last-wins reported a dead job as "active".
            if state is None:
                state = line[len("state = "):].strip() or None
        elif line.startswith("last exit code = "):
            val = line[len("last exit code = "):].strip()
            # launchd prints "(never exited)" for a never-failed service, and a
            # NAMED sysexits code for others: "75: EX_TEMPFAIL". A bare int()
            # raises on the named form and yielded None, which downstream read
            # as "no non-zero exit recorded" — so the one restart code our
            # runners actually use was the one this could not see.
            match = re.match(r"-?\d+", val)
            last_exit = int(match.group()) if match else None
    return state, last_exit


def parse_launchctl_exit_timeout(text: str) -> float | None:
    """Extract the loaded job's effective launchd teardown allowance."""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("exit timeout = "):
            try:
                value = float(line[len("exit timeout = "):].strip())
            except ValueError:
                return None
            return value if value >= 0 else None
    return None


def owes_exit75_respawn(
    state: str | None,
    last_exit_code: int | None,
    age_s: float | None,
    expected_loaded: bool,
    restart_grace_s: int,
) -> bool:
    """Whether launchd owes this agent the respawn its exit 75 asked for. Pure.

    A lane supervisor exits 75 (EX_TEMPFAIL) only after its fail-closed
    restart contract has run, expecting KeepAlive to start it again. Past the
    grace, an agent still not running has been owed that respawn, whatever
    launchd's reason. The watchdog's wedged verdict and the interval guard's
    lane kick (`launchd_interval_guard.py`) share this one definition.
    """
    return (
        expected_loaded
        and last_exit_code == 75
        and state in {"not running", "spawn scheduled"}
        and age_s is not None
        and age_s > restart_grace_s
    )


def classify(
    state: str | None,
    last_exit_code: int | None,
    log_age_s: float | None,
    stale_log_s: int,
    vm_running: bool | None = True,
    expected_loaded: bool = False,
    restart_grace_s: int = DEFAULT_RESTART_GRACE_S,
    vm_probe_reason: str = "Tart VM inventory unavailable",
) -> tuple[str, str]:
    """Decide healthy / wedged / unknown from parsed signals. Pure.

    Two independent wedge signatures:

    1. **Invisible crash-loop** := exited non-zero AND its log has gone stale (or
       is missing) AND no VM is building. The first two distinguish the
       crash-loop from a healthy between-jobs idle (running, fresh "waiting"
       log) and a momentary restart (non-zero exit, log still being written).
       The third carries the same weight it does in (2), because a sticky
       non-zero exit is not evidence of a crash: `serve --loop` exits
       EX_TEMPFAIL deliberately and launchd reports that code for the life of
       the respawned job, so a supervisor that logs nothing for the stale
       threshold while a required gate job builds matches this signature
       exactly. Healing is a bootout, which SIGTERMs that supervisor and takes
       its guest with it.
    2. **Alive-but-frozen** := the process is up (no non-zero exit) BUT its log has
       gone stale AND no VM is building. A healthy serve loop writes a "waiting"/
       "SCAN BLIND" line every poll (~10-20s), so a stale log while alive means the
       loop stopped iterating — a hung `tart`/boot or a frozen loop the in-supervisor
       self-heal can't catch (it never gets back to the top to increment `blind`).
       The `vm_running` guard is load-bearing: a legit long build blocks the loop
       quietly for up to hours, so we only call it frozen when NO VM is running.

    Both signatures fail safe on the probe: `vm_running is None` means the Tart
    inventory could not be read (an unset `TART_HOME` under launchd is enough to
    make `tart list` blind), and an unreadable inventory returns "unknown" — a
    verdict `main` never heals — rather than the "no VM running" it superficially
    resembles."""
    if state is None and expected_loaded:
        return "wedged", "not loaded while pool participation is enabled"
    if owes_exit75_respawn(state, last_exit_code, log_age_s, expected_loaded, restart_grace_s):
        return (
            "wedged",
            f"EX_TEMPFAIL self-restart did not respawn within {restart_grace_s}s "
            f"(state={state}, log age {int(log_age_s)}s)",
        )
    if last_exit_code is None or last_exit_code == 0:
        # Alive / cleanly-restarting. The alive-but-frozen signature applies ONLY when the process is
        # genuinely UP: state == "running". A frozen run_one still reports "running" (the process is
        # up, just stuck), so that's the case we want. A None/absent state means the agent is NOT
        # loaded — deliberately stopped (pool-off) or a staged-but-unloaded plist — and must never be
        # resurrected; `launchctl print` returns rc!=0 for those, which gather_health maps to
        # state=None. So `state == "running"` is the load-bearing guard against reviving a stopped host.
        if (state == "running" and log_age_s is not None and log_age_s > stale_log_s):
            if vm_running is None:
                return (
                    "unknown",
                    f"{vm_probe_reason}; refusing alive-but-frozen recovery",
                )
            if not vm_running:
                return ("wedged",
                        f"alive but frozen: log stale {int(log_age_s)}s (> {stale_log_s}s) "
                        "and no VM building")
        if last_exit_code is None:
            return "healthy", "no non-zero exit recorded"
        return "healthy", "clean last exit"
    # last_exit_code != 0 from here.
    # Non-zero exit but the log is fresh → a live restart, give it time.
    if log_age_s is not None and log_age_s <= stale_log_s:
        return "healthy", f"exited {last_exit_code} but log fresh ({int(log_age_s)}s)"
    # The `vm_running` guard is load-bearing HERE TOO, for the same reason it is
    # in the alive-but-frozen branch above. `serve --loop` exits EX_TEMPFAIL by
    # design so launchd hands the respawn a fresh App-auth environment, and
    # launchd then reports that non-zero code for the whole life of the
    # respawned supervisor. A long build blocks the loop quietly, so the log
    # goes stale while everything is healthy — making "sticky non-zero exit +
    # stale log" indistinguishable from a crash-loop on those two signals alone.
    # Healing is a bootout, which SIGTERMs the supervisor under a live job and
    # takes its guest with it, so only call it a crash-loop when NO VM is
    # running, and refuse rather than guess when the inventory is unavailable.
    if vm_running is None:
        return (
            "unknown",
            f"{vm_probe_reason}; refusing crash-loop recovery for exit "
            f"{last_exit_code}",
        )
    if vm_running:
        age = "missing" if log_age_s is None else f"stale {int(log_age_s)}s"
        return (
            "healthy",
            f"exited {last_exit_code} and log {age}, but a VM is building — "
            "not a crash-loop",
        )
    if log_age_s is None:
        return "wedged", f"exited {last_exit_code}, log missing"
    return (
        "wedged",
        f"exited {last_exit_code}, log stale {int(log_age_s)}s "
        f"(> {stale_log_s}s)",
    )


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _domain() -> str:
    return f"gui/{os.getuid()}"


def parse_disabled_services(text: str) -> set[str]:
    """Return exact labels launchd reports as durably disabled."""
    disabled: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line.endswith("=> disabled") or not line.startswith('"'):
            continue
        end = line.find('"', 1)
        if end > 1:
            disabled.add(line[1:end])
    return disabled


def disabled_services() -> set[str] | None:
    """Read launchd's enablement authority once; unknown fails closed to no heal."""
    rc, out, _ = _run(["launchctl", "print-disabled", _domain()])
    return parse_disabled_services(out) if rc == 0 else None


def discover_agents(launch_agents_dir: str) -> list[tuple[str, str]]:
    """Return every tartci or persistent Actions runner LaunchAgent plist."""
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
        if label.startswith(TARTCI_LABEL_PREFIXES) or label.startswith(
            ACTIONS_RUNNER_LABEL_PREFIX
        ):
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


def _start_interval_from_plist(plist_path: str) -> int | None:
    """The agent's own StartInterval in seconds, or None when it has no usable one.

    An interval agent is SUPPOSED to be quiet between runs, so the shared
    30-minute staleness bound calls an hourly agent frozen on every other pass.
    The bound has to come from the plist rather than a second flag, because the
    plist is what actually decides how often the log can be written.

    None (not zero) for an absent, non-integer, or non-positive value: a zero
    would collapse the staleness bound and make every agent read as wedged.
    """
    try:
        with open(plist_path, "rb") as fh:
            data = plistlib.load(fh)
    except Exception:
        return None
    interval = data.get("StartInterval")
    if isinstance(interval, bool) or not isinstance(interval, int):
        return None
    return interval if interval > 0 else None


def _program_path_from_plist(plist_path: str) -> str | None:
    """Return the executable declared by a LaunchAgent, if one is explicit."""
    try:
        with open(plist_path, "rb") as fh:
            data = plistlib.load(fh)
    except Exception:
        return None
    program = data.get("Program")
    if isinstance(program, str) and program:
        return program
    arguments = data.get("ProgramArguments")
    if isinstance(arguments, list) and arguments and isinstance(arguments[0], str):
        return arguments[0]
    return None


def resolve_tart_cli() -> tuple[str | None, str]:
    """Resolve Tart without assuming an interactive shell has Homebrew on PATH."""
    configured = os.environ.get("TARTCI_TART_CLI", "").strip()
    if configured:
        resolved = configured if os.path.isabs(configured) else shutil.which(configured)
        if resolved and os.path.isfile(resolved) and os.access(resolved, os.X_OK):
            return resolved, f"resolved from TARTCI_TART_CLI={configured}"
        return None, f"TARTCI_TART_CLI is not an executable: {configured}"

    resolved = shutil.which("tart")
    if resolved and os.path.isfile(resolved) and os.access(resolved, os.X_OK):
        return resolved, f"resolved from PATH: {resolved}"
    for candidate in ("/opt/homebrew/bin/tart", "/usr/local/bin/tart"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate, f"resolved from canonical install path: {candidate}"
    return None, (
        "Tart executable unavailable from PATH or canonical install paths; "
        "set TARTCI_TART_CLI to its absolute path"
    )


def _profile_tart_home(profile_path: str) -> str | None:
    """Read the installed fleet profile without requiring interactive shell state."""
    # Do not implement a partial TOML parser on Apple's older system Python. A
    # launchd service already carries explicit TART_HOME; an interactive caller
    # without tomllib must supply it too. Partial parsing could accept a torn or
    # otherwise malformed profile and turn corruption into a false idle result.
    if tomllib is None:
        return None
    try:
        with open(profile_path, "rb") as fh:
            parsed = tomllib.load(fh)
            host = parsed.get("host")
            if not isinstance(host, dict):
                return None
            value = host.get("tart_home")
            return value.strip() if isinstance(value, str) and value.strip() else None
    except (OSError, ValueError):
        return None


def resolve_tart_home() -> tuple[str | None, str]:
    """Resolve the declared Tart store; never silently inspect Tart's default store."""
    configured = os.environ.get("TART_HOME", "").strip()
    if configured:
        expanded = os.path.abspath(os.path.expanduser(configured))
        if os.path.isabs(configured) and os.path.isdir(expanded):
            return expanded, f"resolved from TART_HOME={configured}"
        return None, f"TART_HOME is not an existing absolute directory: {configured}"

    profile_path = os.environ.get(
        "TARTCI_MACOS_FLEET_PROFILE",
        os.path.expanduser("~/.config/tartci/macos-fleet-profile.toml"),
    )
    profile_home = _profile_tart_home(profile_path)
    if profile_home:
        expanded = os.path.abspath(os.path.expanduser(profile_home))
        if os.path.isabs(profile_home) and os.path.isdir(expanded):
            return expanded, f"resolved from installed fleet profile: {profile_path}"
        return None, (
            f"installed fleet profile declares unavailable Tart store: {profile_home} "
            f"({profile_path})"
        )
    return None, (
        "Tart store unavailable: set TART_HOME or install a fleet profile with "
        "[host].tart_home"
    )


def _run_tart_inventory(tart_cli: str, tart_home: str) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["TART_HOME"] = tart_home
    process = subprocess.run(
        [tart_cli, "list", "--format", "json"],
        capture_output=True,
        text=True,
        env=env,
    )
    return process.returncode, process.stdout, process.stderr


def probe_tart_vm_running() -> TartVMProbe:
    """Return running/idle/unavailable without conflating probe failure with load."""
    tart_cli, resolution = resolve_tart_cli()
    if tart_cli is None:
        return TartVMProbe(None, resolution, None, None)
    tart_home, home_resolution = resolve_tart_home()
    if tart_home is None:
        return TartVMProbe(None, home_resolution, tart_cli, None)
    try:
        rc, out, err = _run_tart_inventory(tart_cli, tart_home)
        if rc != 0:
            detail = err.strip() or f"exit {rc}"
            return TartVMProbe(
                None,
                f"Tart VM inventory failed via {tart_cli} in {tart_home}: {detail}",
                tart_cli,
                tart_home,
            )
        inventory = json.loads(out)
        if not isinstance(inventory, list):
            return TartVMProbe(
                None,
                f"Tart VM inventory was not a JSON list via {tart_cli} in {tart_home}",
                tart_cli,
                tart_home,
            )
        if not all(isinstance(vm, dict) for vm in inventory):
            return TartVMProbe(
                None,
                f"Tart VM inventory contained a non-object entry via {tart_cli} in {tart_home}",
                tart_cli,
                tart_home,
            )
        running = any(
            str(vm.get("State", vm.get("state", ""))).lower().startswith("run")
            for vm in inventory
        )
        return TartVMProbe(
            running,
            f"Tart VM inventory {'has a running VM' if running else 'is idle'}; "
            f"{resolution}; {home_resolution}",
            tart_cli,
            tart_home,
        )
    except (OSError, json.JSONDecodeError) as exc:
        return TartVMProbe(
            None,
            f"Tart VM inventory unavailable via {tart_cli} in {tart_home}: {exc}",
            tart_cli,
            tart_home,
        )


def any_tart_vm_running() -> bool | None:
    """Compatibility projection: True=busy, False=idle, None=probe unavailable."""
    return probe_tart_vm_running().running


def gather_health(label: str, plist_path: str, stale_log_s: int,
                  vm_running: bool | None = True,
                  pool_participating: bool = False,
                  restart_grace_s: int = DEFAULT_RESTART_GRACE_S,
                  service_enabled: bool | None = True,
                  vm_probe_reason: str = "Tart VM inventory unavailable") -> AgentHealth:
    rc, out, _ = _run(["launchctl", "print", f"{_domain()}/{label}"])
    state, last_exit = parse_launchctl_print(out) if rc == 0 else (None, None)
    log_path = _log_path_from_plist(plist_path)
    log_age: float | None = None
    if log_path and os.path.exists(log_path):
        log_age = max(0.0, utcnow() - os.path.getmtime(log_path))
    program_path = _program_path_from_plist(plist_path)
    if (
        label.startswith(ACTIONS_RUNNER_LABEL_PREFIX)
        and program_path
        and os.path.isabs(program_path)
        and not os.path.isfile(program_path)
    ):
        return AgentHealth(
            label, plist_path, log_path, state, last_exit, log_age, "broken",
            f"declared runner executable is missing: {program_path}; reload cannot repair "
            "a deleted installation — follow launchd/README.md#persistent-actions-runner-install-missing",
        )
    if label.startswith(ACTIONS_RUNNER_LABEL_PREFIX):
        # Persistent Actions runners do not emit TartCI's poll heartbeat. Their
        # logs may be quiet while idle or throughout a long job, so applying
        # the stale-log classifier below could bootout a healthy runner. This
        # watchdog owns only the fail-closed installation-presence audit for
        # these services; Actions runtime/job health stays with Shipyard.
        #
        # That delegation used to end here, as a bare sentence, and it was
        # wrong for three months: this branch printed a checkmark over a
        # service in a `spawn scheduled` crash loop with 3,684 launches and no
        # `.runner` registration file, while Shipyard knew nothing about this
        # host at all. Both halves passed their own check by pointing at the
        # other.
        #
        # The rule now: a delegation may only pass when it names the ARTIFACT
        # carrying the other side's verdict, and absence of that artifact is a
        # fault. `tartci_host_attestation.py` writes it; Shipyard's landability
        # preflight reads it and reports Unknown - never Served - when it is
        # missing or stale. The verdict is still not computed here, because
        # that is genuinely Shipyard's half; what changed is that the reader is
        # told where to look and can tell absence from health.
        return AgentHealth(
            label, plist_path, log_path, state, last_exit, log_age, "healthy",
            "declared runner executable exists; runtime health is owned by Shipyard "
            f"via {attestation_reference()}",
        )
    pool_runner = is_pool_runner(label)
    if pool_runner and service_enabled is False:
        return AgentHealth(
            label, plist_path, log_path, state, last_exit, log_age, "healthy",
            "explicitly disabled in launchd; durable lane authority preserved",
        )
    if pool_runner and service_enabled is None:
        return AgentHealth(
            label, plist_path, log_path, state, last_exit, log_age, "unknown",
            "launchd enablement state unavailable; refusing automatic recovery",
        )
    documented = APPLICATION_EXIT_CODES.get(label, {})
    if last_exit is not None and last_exit in documented:
        return AgentHealth(
            label, plist_path, log_path, state, last_exit, log_age, "attention",
            f"exited {last_exit}: {documented[last_exit]}. The agent ran and "
            "reported this itself, so a reload would only repeat it",
        )
    # An interval agent is quiet by design between runs, so the shared bound
    # would call an hourly agent frozen on every other pass. Two intervals is
    # the smallest bound that survives one skipped run; never SHORTER than the
    # shared bound, so a fast agent keeps the 30-minute floor.
    interval_s = _start_interval_from_plist(plist_path)
    effective_stale_s = stale_log_s
    if interval_s is not None:
        effective_stale_s = max(stale_log_s, 2 * interval_s)
    # An uninterruptible interval agent stuck in one run is never healed (a
    # bootout mid-run is unsafe), and launchd starts no later run while it is
    # alive, so "the next interval starts it cleanly" never happens. On
    # 2026-10-02 m1's reclaim sat 14 h in one run while this pass logged it as
    # heal-failed and then rate-limited. Say what it is instead.
    if (label in UNINTERRUPTIBLE_AGENTS and interval_s is not None and state == "running"
            and log_age is not None and log_age > effective_stale_s):
        return AgentHealth(
            label, plist_path, log_path, state, last_exit, log_age, "attention",
            f"one run has gone {int(log_age)}s without a log line (> {effective_stale_s}s, "
            f"at least twice its {interval_s}s interval); launchd starts no later run while it lives and "
            "this agent is never interrupted automatically — read its log, then stop "
            "the run by hand",
        )
    # classify() reads "non-zero exit, fresh log" as a KeepAlive job mid-restart.
    # An interval agent does not restart: it ran, wrote its log, and exited
    # non-zero, so the fresh log is the failing run itself. That used to print
    # a checkmark over it ("reap exited 1 but log fresh"). It is reported, and
    # never healed, because a reload would only repeat the run.
    if (interval_s is not None and last_exit not in (None, 0)
            and state != "running" and log_age is not None
            and log_age <= effective_stale_s):
        return AgentHealth(
            label, plist_path, log_path, state, last_exit, log_age, "attention",
            f"exited {last_exit} on its last run (an interval agent, so the fresh "
            f"log is that run, {int(log_age)}s ago, not a restart); a reload would "
            "only repeat it — read its log",
        )
    expected_loaded = pool_participating and pool_runner
    verdict, reason = classify(
        state, last_exit, log_age, effective_stale_s, vm_running, expected_loaded,
        restart_grace_s, vm_probe_reason
    )
    return AgentHealth(label, plist_path, log_path, state, last_exit,
                       log_age, verdict, reason)


def attestation_reference() -> str:
    """Name the artifact this watchdog delegates runtime health to.

    Reports the path and, when the file is present, its age - so a reader of
    this line can tell "delegated and the other side is looking" apart from
    "delegated into the void", which is what the bare sentence could not say.
    """
    root = os.environ.get("TARTCI_HOME") or os.path.join(os.path.expanduser("~"), ".tartci")
    path = os.path.join(root, "state", "host-attestation.json")
    if not os.path.exists(path):
        return f"{path} (MISSING - delegation is unverified)"
    age = int(max(0.0, utcnow() - os.path.getmtime(path)))
    return f"{path} ({age}s old)"


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


def launchctl_reports_absent(rc: int, stderr: str) -> bool:
    """Whether launchctl specifically proved that a service is not loaded."""
    return rc == 113 and "Could not find service" in stderr


def wait_until_unloaded(label: str, timeout_s: float = 10.0,
                        poll_s: float = 0.1) -> bool:
    """Wait for launchd to finish an asynchronous service teardown."""
    deadline = time.monotonic() + timeout_s
    while True:
        rc, _, err = _run(["launchctl", "print", f"{_domain()}/{label}"])
        if launchctl_reports_absent(rc, err):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


class ReloadPlan(NamedTuple):
    """What a full reload would do, decided without changing anything."""
    proceed: bool
    reason: str
    steps: tuple[str, ...]
    exit_timeout: float | None = None


def plan_reload(label: str, plist_path: str) -> ReloadPlan:
    """Run every reload precondition read-only and return the plan.

    Shared by the real reload and `--dry-run`, so a dry run can never report
    success for a reload the real one would refuse.
    """
    dom = _domain()
    target = f"{dom}/{label}"
    bootstrap = f"bootstrap {dom} {plist_path}"
    kickstart = f"kickstart -k {target}"
    loaded_rc, loaded_out, loaded_err = _run(["launchctl", "print", target])
    if loaded_rc == 0:
        state, _ = parse_launchctl_print(loaded_out)
        if state == "running" and label in UNINTERRUPTIBLE_AGENTS:
            # This agent's run cannot be cut anywhere. Refuse loudly and let
            # the next interval start it cleanly.
            return ReloadPlan(False, f"{label} is running and its run cannot be "
                              "interrupted; the next interval starts it cleanly", ())
        exit_timeout = parse_launchctl_exit_timeout(loaded_out)
        if exit_timeout is None or exit_timeout == 0:
            # Zero is infinite; a missing value is likewise not a safe bound.
            # Refuse before bootout because no bounded reload can prove when it
            # is safe to bootstrap the replacement.
            return ReloadPlan(False, f"{label} has no finite ExitTimeOut, so no "
                              "bounded reload can prove its teardown finished", ())
        return ReloadPlan(True, "loaded", (f"bootout {target}", bootstrap, kickstart),
                          exit_timeout)
    if not launchctl_reports_absent(loaded_rc, loaded_err):
        # A permission/domain/IPC error is not proof that bootstrap is safe.
        return ReloadPlan(False, f"launchctl print {target} failed (exit "
                          f"{loaded_rc}): {loaded_err.strip()[:200]}", ())
    return ReloadPlan(True, "not loaded", (bootstrap, kickstart))


def reload_agent(label: str, plist_path: str, dry_run: bool = False) -> bool:
    """Full bootout+bootstrap+kickstart — the ONLY thing that clears a stale
    cached job spec. `kickstart -k` alone re-runs the stale spec, so we never
    use it in isolation."""
    dom = _domain()
    plan = plan_reload(label, plist_path)
    if dry_run or not plan.proceed:
        return plan.proceed
    if plan.exit_timeout is not None:
        _run(["launchctl", "bootout", f"{dom}/{label}"])
        # launchctl bootout can return before the cached job's ExitTimeOut
        # teardown completes. Prove it is gone before loading the new plist.
        if not wait_until_unloaded(label, timeout_s=plan.exit_timeout + 5.0):
            return False
    rc, _, _ = _run(["launchctl", "bootstrap", dom, plist_path])
    if rc != 0:
        return False
    rc2, _, _ = _run(["launchctl", "kickstart", "-k", f"{dom}/{label}"])
    if rc2 != 0:
        return False
    # Prove launchd now owns the service. It may still be starting, so loaded
    # (rather than state=running) is the correct immediate postcondition.
    rc3, _, _ = _run(["launchctl", "print", f"{dom}/{label}"])
    return rc3 == 0


# Exit codes of the explicit `--reload` entry.
RELOAD_OK = 0          # reloaded, or (--dry-run) every precondition passed
RELOAD_FAILED = 1      # a mutation ran and failed its postcondition
RELOAD_REFUSED = 3     # a precondition refused; nothing was changed


def mid_job_refusal(label: str, launch_agents_dir: str | None = None) -> str | None:
    """Why an operator reload of LABEL must not proceed now, or None.

    Per label, not host-wide: a sibling lane building must not block reloading
    an idle one. Unknown refuses like busy.
    """
    import lane_busy

    from pathlib import Path
    row = lane_busy.probe([label], run=_run, agents_dir=Path(launch_agents_dir)
                          if launch_agents_dir else None)[0]
    if row.state == lane_busy.BUSY:
        return (f"lane {label} is mid-job ({row.detail}: {row.worker_command}); "
                "a bootout would kill that work")
    if row.state == lane_busy.UNKNOWN:
        return (f"lane {label} busy state is unknown ({row.detail}); refusing "
                "rather than risk killing a running job")
    return None


def reload_command(label: str, launch_agents_dir: str, *, dry_run: bool,
                   allow_mid_job: bool) -> int:
    """The explicit `tartci launchd reload LABEL` entry, with every guard."""
    plist = os.path.join(launch_agents_dir, f"{label}.plist")
    verb = "would" if dry_run else "will"
    if not os.path.exists(plist):
        print(f"launchd-watchdog: REFUSE: no plist for {label} at {plist}",
              file=sys.stderr)
        return RELOAD_REFUSED if dry_run else RELOAD_FAILED
    if not allow_mid_job:
        refusal = mid_job_refusal(label, launch_agents_dir)
        if refusal is not None:
            print(f"launchd-watchdog: REFUSE: {refusal}.\n"
                  "  Instead: wait for the lane to go idle and re-run, or run "
                  "`tartci pool drain` so it finishes its job and stops.\n"
                  "  Override (kills the job): tartci launchd reload "
                  f"{label} --allow-mid-job", file=sys.stderr)
            return RELOAD_REFUSED
    plan = plan_reload(label, plist)
    if not plan.proceed:
        print(f"launchd-watchdog: REFUSE: {plan.reason}", file=sys.stderr)
        return RELOAD_REFUSED
    for step in plan.steps:
        print(f"launchd-watchdog: {verb} {step}")
    if dry_run:
        return RELOAD_OK
    ok = reload_agent(label, plist)
    print(f"launchd-watchdog: reloaded {label} — {'ok' if ok else 'FAILED'}")
    return RELOAD_OK if ok else RELOAD_FAILED


# ── configuration drift (report only) ───────────────────────────────────────
#
# The heal pass runs on its own StartInterval, so it is the one place that
# sees installed-vs-declared drift without anyone remembering to ask. It only
# ever logs: acting on drift (a reload, a refusal) would turn a configuration
# difference into an outage.

DEFAULT_CONFIG_WARN_INTERVAL_S = 21600  # re-warn an unchanged verdict every 6h
_TOML_PYTHONS = ("python3.12", "python3.11", "python3", "/opt/homebrew/bin/python3.12",
                 "/opt/homebrew/bin/python3.11", "/opt/homebrew/bin/python3",
                 "/usr/local/bin/python3")


def _toml_python() -> str | None:
    for candidate in _TOML_PYTHONS:
        resolved = shutil.which(candidate) or (candidate if os.path.isfile(candidate) else None)
        if resolved and subprocess.run([resolved, "-c", "import tomllib"],
                                        capture_output=True).returncode == 0:
            return resolved
    return None


def config_verdicts(config: str, receipt: str, support_root: str | None = None) -> dict:
    """Profile-drift + supply verdicts from this support root. Never raises."""
    if not os.path.isfile(config):
        return {"profile_drift": {"state": "not_applicable"},
                "supply": {"state": "not_applicable"}}
    root = support_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    unknown = lambda why: {"profile_drift": {"state": "unknown", "reason": why},  # noqa: E731
                           "supply": {"state": "unknown", "reason": why}}
    python = _toml_python()
    if python is None:
        return unknown("no Python 3.11+ interpreter with tomllib")
    try:
        proc = subprocess.run(
            [python, os.path.join(root, "scripts", "macos_fleet_lanes.py"),
             "config-verdicts", "--config", config, "--support-root", root,
             "--receipt", receipt, "--json"],
            capture_output=True, text=True, timeout=60)
        value = json.loads(proc.stdout)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except Exception as exc:  # noqa: BLE001 - an unread verdict is unknown
        return unknown(f"config check failed: {exc}")


def refresh_skew(interval_s: int = 1800) -> None:
    """Re-measure tartci's skew against main at most every interval_s.

    Read-only (a git fetch into the tartci-owned update checkout). Keeps the
    skew line in pool status, doctor and this log current even on a host
    that never runs the self-update agent.
    """
    python = _toml_python()
    if python is None:
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        subprocess.run([python, os.path.join(root, "scripts", "fleet_self_update.py"),
                        "--refresh-skew", "--if-older", str(interval_s)],
                       capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        pass


def refresh_tools(interval_s: int = 1800, timeout_s: int = 600) -> str | None:
    """Re-measure Shipyard and pulp CLI freshness at most every interval_s.

    Also where an automatic tool update runs (tool_freshness.py applies a
    behind tool whose settings allow it, then re-reads and verifies it), so a
    merged release reaches this host without anyone logging in.

    Returns why the refresh itself failed, or None. Exit 1 with no traceback
    is tool_freshness reporting a stale tool, which the config WARN already
    carries; a crash, a timeout or a missing interpreter is this pass failing,
    and used to be swallowed without a trace.
    """
    python = _toml_python()
    if python is None:
        return "no Python 3.11+ interpreter with tomllib"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        proc = subprocess.run([python, os.path.join(root, "scripts", "tool_freshness.py"),
                               "--refresh", "--if-older", str(interval_s)],
                              capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout_s}s"
    except OSError as exc:
        return f"could not run: {exc}"
    if proc.returncode not in (0, 1) or "Traceback" in (proc.stderr or ""):
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return f"exit {proc.returncode}: {tail[-1] if tail else 'no output'}"
    return None


def keychain_unlock_agent_pass(home: str | None = None,
                               run=subprocess.run) -> str | None:
    """Reinstall the keychain-unlock agent where keychain.env exists.

    The installer is idempotent (a current, loaded agent is left alone), so a
    host whose agent was removed, never installed, or rendered from an older
    template gets it back on the next heal pass. Returns the line to log.
    """
    home = home or os.path.expanduser("~")
    if not os.path.isfile(os.path.join(home, ".config", "pulp", "secrets", "keychain.env")):
        return None
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        proc = run(["/bin/bash", os.path.join(root, "scripts", "install_keychain_unlock_agent.sh"),
                    "--install"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{_iso(utcnow())} launchd-watchdog: WARN keychain-unlock agent install FAILED ({exc})"
    text = (proc.stdout or "").strip()
    if proc.returncode != 0:
        detail = (proc.stderr or text).strip().splitlines()
        return (f"{_iso(utcnow())} launchd-watchdog: WARN keychain-unlock agent install FAILED "
                f"(exit {proc.returncode}: {detail[-1] if detail else 'no output'})")
    if "already installed and loaded" in text:
        return None
    return f"{_iso(utcnow())} launchd-watchdog: keychain-unlock agent (re)installed"


ATTESTATION_LABEL = "com.danielraffel.shipyard.host-attestation"


def _sha256(path: str) -> str | None:
    import hashlib  # noqa: PLC0415 - only this pass hashes
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def host_attestation_pass(home: str | None = None, run=subprocess.run,
                          loaded=None) -> str | None:
    """Redeploy a loaded host-attestation writer that is not this tartci's.

    install_host_attestation.sh copies the writer out of the generation into
    ~/.local/share/pulp-landing, so a self-update never reached it: on
    2026-10-02 m3 and m5 still ran a 2026-09-12 writer that reported its own
    previous exit as a finding, a bug main had fixed two days earlier. The
    installer is re-run with the arguments the installed plist already carries
    (every --advertise, the generation and the interval), so a host keeps
    attesting exactly what it attested. A host without the agent, or with it
    unloaded, is left alone: installing or re-enabling it is an operator's call.
    Returns the line to log, or None when there is nothing to say. Never raises.
    """
    home = home or os.path.expanduser("~")
    plist_path = os.path.join(home, "Library", "LaunchAgents", f"{ATTESTATION_LABEL}.plist")
    if not os.path.isfile(plist_path):
        return None
    is_loaded = loaded if loaded is not None else (
        lambda label: _run(["launchctl", "print", f"{_domain()}/{label}"])[0] == 0)
    if not is_loaded(ATTESTATION_LABEL):
        return None
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = _sha256(os.path.join(root, "scripts", "tartci_host_attestation.py"))
    installed = _sha256(os.path.join(home, ".local", "share", "pulp-landing", "current",
                                     "tartci_host_attestation.py"))
    if source is None or source == installed:
        return None
    try:
        with open(plist_path, "rb") as fh:
            plist = plistlib.load(fh)
        argv = [str(a) for a in plist.get("ProgramArguments") or []]
        args: list[str] = []
        generation = (plist.get("EnvironmentVariables") or {}).get("PULP_ATTESTATION_GENERATION")
        if generation:
            args += ["--generation", str(generation)]
        if isinstance(plist.get("StartInterval"), int):
            args += ["--interval", str(plist["StartInterval"])]
        for i, value in enumerate(argv[:-1]):
            if value == "--advertise":
                args += ["--advertise", argv[i + 1]]
    except Exception as exc:  # noqa: BLE001 - the heal pass must go on
        return (f"{_iso(utcnow())} launchd-watchdog: WARN host-attestation writer is stale "
                f"but its plist is unreadable ({exc}); not redeployed")
    try:
        proc = run(["/bin/bash", os.path.join(root, "scripts", "install_host_attestation.sh"),
                    *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{_iso(utcnow())} launchd-watchdog: WARN host-attestation redeploy FAILED ({exc})"
    was = (installed or "absent")[:12]
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return (f"{_iso(utcnow())} launchd-watchdog: WARN host-attestation redeploy {was} -> "
                f"{source[:12]} FAILED (exit {proc.returncode}: "
                f"{detail[-1] if detail else 'no output'})")
    return (f"{_iso(utcnow())} launchd-watchdog: host-attestation writer redeployed "
            f"{was} -> {source[:12]}")


QUEUE_SATURATION_LABEL = "com.danielraffel.pulp.queue-saturation"


SCHEDULE_BACKSTOP_LABEL = "com.danielraffel.pulp.schedule-backstop"


def desired_agent_plist(label: str, home: str, installed: dict, keep_prefix: str) -> dict:
    """`label`'s template rendered for `home`, keeping the host's own `keep_prefix` values."""
    import re  # noqa: PLC0415
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "launchd", f"{label}.plist.template")
    with open(path, "rb") as fh:
        source = re.sub(rb"<!--.*?-->", b"", fh.read(), flags=re.DOTALL)
    desired = plistlib.loads(source.replace(b"$HOME", home.encode()))
    env = desired.setdefault("EnvironmentVariables", {})
    for key, value in (installed.get("EnvironmentVariables") or {}).items():
        if key.startswith(keep_prefix) and value:
            env[key] = value
    return desired


def desired_queue_saturation_plist(home: str, installed: dict) -> dict:
    """The template rendered for `home`, keeping the host's own PULP_SAT_* values."""
    return desired_agent_plist(QUEUE_SATURATION_LABEL, home, installed, "PULP_SAT_")


def template_agent_pass(label: str, keep_prefix: str, name: str, home: str | None = None,
                        run=subprocess.run, loaded=None) -> str | None:
    """Re-render a loaded hand-installed agent that no longer matches its template.

    Nothing re-rendered these after install: m5's queue-saturation copy from
    2026-07-19 predated the required PULP_SAT_GH_CLI and failed on every run
    for two months, and agents kept running scripts from a stale checkout
    after their templates moved onto the installed generation (m3's schedule
    backstop, on 2026-10-03). The host's `keep_prefix` tuning (a live apply or
    authority switch, for one) is kept; an absent or unloaded agent is left
    alone. Returns the line to log, or None.
    """
    home = home or os.path.expanduser("~")
    plist_path = os.path.join(home, "Library", "LaunchAgents", f"{label}.plist")
    if not os.path.isfile(plist_path):
        return None
    is_loaded = loaded if loaded is not None else (
        lambda target: _run(["launchctl", "print", f"{_domain()}/{target}"])[0] == 0)
    if not is_loaded(label):
        return None
    try:
        with open(plist_path, "rb") as fh:
            installed = plistlib.load(fh)
        desired = desired_agent_plist(label, home, installed, keep_prefix)
    except Exception as exc:  # noqa: BLE001 - the heal pass must go on
        return (f"{_iso(utcnow())} launchd-watchdog: WARN {name} agent unreadable "
                f"({exc}); not re-rendered")
    if installed == desired:
        return None
    try:
        tmp = f"{plist_path}.tmp"
        with open(tmp, "wb") as fh:
            plistlib.dump(desired, fh)
        os.replace(tmp, plist_path)
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", f"{domain}/{label}"],
            capture_output=True, text=True, timeout=30)
        boot = run(["launchctl", "bootstrap", domain, plist_path],
                   capture_output=True, text=True, timeout=30)
        if boot.returncode == 0:
            # A RunAtLoad launch is speculative and launchd can defer it
            # indefinitely on a busy host; kickstart makes it on-demand.
            run(["launchctl", "kickstart", f"{domain}/{label}"],
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{_iso(utcnow())} launchd-watchdog: WARN {name} re-render FAILED ({exc})"
    if boot.returncode != 0:
        return (f"{_iso(utcnow())} launchd-watchdog: WARN {name} re-rendered but "
                f"bootstrap failed (exit {boot.returncode}: {(boot.stderr or '').strip()[:200]})")
    return f"{_iso(utcnow())} launchd-watchdog: {name} agent re-rendered from its template"


def queue_saturation_pass(home: str | None = None, run=subprocess.run,
                          loaded=None) -> str | None:
    return template_agent_pass(QUEUE_SATURATION_LABEL, "PULP_SAT_", "queue-saturation",
                               home, run, loaded)


def schedule_backstop_pass(home: str | None = None, run=subprocess.run,
                           loaded=None) -> str | None:
    return template_agent_pass(SCHEDULE_BACKSTOP_LABEL, "TARTCI_BACKSTOP_", "schedule-backstop",
                               home, run, loaded)


def queue_tick_pass() -> str | None:
    """Reinstall a loaded Shipyard queue tick whose copy is not this tartci's.

    The installer copies the tick out of the generation, so a self-update never
    reached it (scripts/queue_tick_refresh.py). Returns the line to log, or None
    when there is nothing to say. Never raises.
    """
    try:
        import queue_tick_refresh  # noqa: PLC0415 - sibling module
        result = queue_tick_refresh.refresh(fix=True)
    except Exception as exc:  # noqa: BLE001 - the heal pass must go on
        return f"{_iso(utcnow())} launchd-watchdog: WARN queue-tick refresh FAILED ({exc})"
    state = result.get("state")
    # An unloaded stale copy was switched off by someone: pool status names it,
    # and repeating it every five minutes here would only bury the log.
    if state in ("current", "not_installed", "drift_unloaded"):
        return None
    if state == "refreshed":
        return (f"{_iso(utcnow())} launchd-watchdog: queue tick reinstalled from this tartci "
                f"({', '.join(result.get('files', []))}; {' '.join(result.get('args', []))})")
    return (f"{_iso(utcnow())} launchd-watchdog: WARN queue tick {state}: "
            f"{result.get('detail')}")


def config_problem(value: dict) -> str | None:
    """One-line summary when anything is not ok, else None."""
    parts = []
    for key, good in (("profile_drift", "in_sync"), ("supply", "match")):
        row = value.get(key) if isinstance(value.get(key), dict) else {}
        state = row.get("state") or "unknown"
        if state in (good, "not_applicable"):
            continue
        detail = row.get("keys") or row.get("mismatched") or ([row["reason"]] if row.get("reason") else [])
        parts.append(f"{key}={state.upper()}" + (f" ({', '.join(detail)})" if detail else ""))
    # tartci's own skew goes last: it must never push the profile/supply
    # verdicts out of the WARN line. The WARN itself is rate-limited per
    # distinct summary, so an unchanged skew is not repeated every pass.
    self_update = value.get("self_update") if isinstance(value.get("self_update"), dict) else {}
    if self_update.get("problem"):
        parts.append(f"self_update={self_update['problem']}")
    for key in ("gate_reserve", "tool_freshness", "host_vitals"):
        row = value.get(key) if isinstance(value.get(key), dict) else {}
        if row.get("problem"):
            parts.append(f"{key}={row['problem']}")
    return "; ".join(parts) or None


def build_disagreement_pass(fleet_config: str, timeout_s: float = 300) -> dict:
    """One report-only cross-host build disagreement cycle. Never raises.

    `scripts/build_disagreement_watch.py` owns enablement (the fleet profile's
    `[build_disagreement] enabled = true`), the 15-minute floor, the detector's
    budgets and the per-(host, fingerprint) alarm dedup. This pass only runs it
    under a tomllib-capable interpreter and hands back its report; nothing it
    returns can heal, reset or reschedule anything.
    """
    if not os.path.isfile(fleet_config):
        return {"state": "disabled", "ran": False, "reason": "no installed fleet profile"}
    python = _toml_python()
    if python is None:
        return {"state": "unknown", "ran": False, "code": "no_tomllib",
                "detail": "no Python 3.11+ interpreter with tomllib"}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        proc = subprocess.run(
            [python, os.path.join(root, "scripts", "build_disagreement_watch.py"),
             "cycle", "--profile-file", fleet_config, "--json"],
            capture_output=True, text=True, timeout=timeout_s)
        value = json.loads(proc.stdout)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except Exception as exc:  # noqa: BLE001 - an unread cycle is unknown
        return {"state": "unknown", "ran": False, "code": "watch_unreadable",
                "detail": f"{type(exc).__name__}: {exc}"}


def should_warn_config(summary: str | None, state: dict, now: float,
                       interval_s: int) -> bool:
    if summary is None:
        return False
    return state.get("summary") != summary or now - float(state.get("warned_at", 0)) >= interval_s


def _config_state_file() -> str:
    return os.path.join(os.path.dirname(_state_file()), "launchd-watchdog-config.json")


# ── host left OFF by a failed self-update ───────────────────────────────────

def _installed_tartci() -> str:
    shim = os.path.expanduser("~/.local/bin/tartci")
    if os.path.isfile(shim):
        return shim
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tartci")


def _pool_on() -> tuple[int, str]:
    try:
        proc = subprocess.run(["/bin/bash", _installed_tartci(), "pool", "on"],
                              cwd=os.path.expanduser("~"), capture_output=True, text=True,
                              timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stderr or proc.stdout).strip()


def vm_boot_pass(status_only: bool = False, now: float | None = None) -> str | None:
    """Tell someone, once per outage, that this host cannot boot VMs.

    The VM-DHCP breaker stops the lanes cloning; this raises a GitHub issue
    naming the host, the doctor command and the remedy, and closes it when a
    VM gets an address (scripts/vm_boot_alert.py). Prints a WARN
    every pass while the outage is due. Never raises.
    """
    try:
        import vm_boot_alert  # noqa: PLC0415 - sibling module
        import vm_dhcp_breaker  # noqa: PLC0415 - sibling module
        now = utcnow() if now is None else now
        if status_only:
            due, why = vm_dhcp_breaker.alert_due(vm_dhcp_breaker.status(), now)
        else:
            out = vm_boot_alert.alert_pass(now=now)
            due, why = out["due"], out["why"]
    except Exception as exc:  # noqa: BLE001 - the heal pass must go on
        return (f"{_iso(utcnow())} launchd-watchdog: WARN vm-boot check FAILED "
                f"({type(exc).__name__}: {exc}); no alert was raised for a host that "
                "cannot boot VMs")
    if due:
        return f"{_iso(now)} launchd-watchdog: WARN vm-boot: this host cannot boot VMs ({why})"
    return None


def peer_stall_pass(now: float | None = None) -> str | None:
    """Tell someone when a PEER's launchd has stalled (scripts/peer_stall_alert.py).

    A stalled host cannot be relied on to alert about itself, so each host
    reads its peers' guard receipts over SSH, at most every 30 min, and opens
    (or adopts) one issue per stalled peer. Runs after the heal work so a slow
    peer never delays it. Prints a WARN for each peer past the threshold.
    Never raises.
    """
    try:
        import peer_stall_alert  # noqa: PLC0415 - sibling module
        out = peer_stall_alert.alert_pass(now=utcnow() if now is None else now)
    except Exception as exc:  # noqa: BLE001 - the heal pass must go on
        return (f"{_iso(utcnow())} launchd-watchdog: WARN peer-stall check FAILED "
                f"({type(exc).__name__}: {exc}); a stalled peer would not be reported")
    if out.get("skipped"):
        return None
    stalled = [f"{peer} since {v.get('since')} ({v.get('hours')} h)"
               for peer, v in sorted((out.get("peers") or {}).items()) if v.get("active")]
    if stalled:
        return (f"{_iso(utcnow() if now is None else now)} launchd-watchdog: WARN peer-stall: "
                f"launchd stalled on {'; '.join(stalled)}")
    return None


def host_off_pass(status_only: bool = False, now: float | None = None) -> str | None:
    """Recover and alert for a host a failed self-update left OFF.

    Returns the line to log, or None when there is nothing to say. Every pass
    while the host is unexpectedly OFF prints a WARN (not rate-limited: this
    is an outage, not drift). Never raises.
    """
    try:
        import host_off  # noqa: PLC0415 - sibling module
        sdir, pool_file = host_off.state_dir(), host_off.pool_state_file()
        now = utcnow() if now is None else now
        outcome = None
        if not status_only:
            outcome = host_off.recover(sdir, pool_file, _pool_on, now=now,
                                       who="launchd-watchdog")
            host_off.alert(sdir, pool_file, host=os.uname().nodename.split(".")[0], now=now)
        current = host_off.status(sdir, pool_file, now)
    except Exception as exc:  # noqa: BLE001 - the heal pass must go on
        return (f"{_iso(utcnow())} launchd-watchdog: WARN host-off check FAILED "
                f"({type(exc).__name__}: {exc}); recovery of a host left OFF did not run")
    if outcome and outcome.get("attempted") and outcome.get("ok"):
        return f"{_iso(now)} launchd-watchdog: host was left OFF by a failed self-update; pool on succeeded"
    if current.get("unexpected"):
        tried = ""
        if outcome and outcome.get("attempted"):
            tried = f"; pool on failed: {outcome.get('reason')}"
        elif outcome:
            tried = f"; {outcome.get('reason')}"
        return f"{_iso(now)} launchd-watchdog: WARN host-off: {current['detail']}{tried}"
    return None


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
                    help="report what heal/reload would do; take no action. "
                    "With --reload: exit 0 if it would proceed, 3 if refused")
    ap.add_argument("--allow-mid-job", action="store_true",
                    help="--reload only: proceed even though the lane is "
                    "mid-job (kills the running VM/job)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--stale-log-seconds", type=int, default=default_stale_log_seconds())
    ap.add_argument("--restart-grace-seconds", type=int,
                    default=DEFAULT_RESTART_GRACE_S)
    ap.add_argument("--max-heals", type=int, default=DEFAULT_MAX_HEALS)
    ap.add_argument("--heal-window-seconds", type=int,
                    default=DEFAULT_HEAL_WINDOW_S)
    ap.add_argument("--launch-agents-dir",
                    default=os.path.join(
                        os.environ.get("HOME", os.path.expanduser("~")),
                        "Library", "LaunchAgents"))
    ap.add_argument("--fleet-config",
                    default=os.path.join(
                        os.environ.get("HOME", os.path.expanduser("~")),
                        ".config", "tartci", "macos-fleet-profile.toml"),
                    help="installed fleet profile checked for drift (report only)")
    ap.add_argument("--fleet-receipt",
                    default=os.path.join(
                        os.environ.get("HOME", os.path.expanduser("~")),
                        ".config", "tartci", "macos-fleet-install.json"))
    ap.add_argument("--config-warn-interval-seconds", type=int,
                    default=DEFAULT_CONFIG_WARN_INTERVAL_S)
    ap.add_argument("--participation-file",
                    default=os.path.join(
                        os.environ.get("HOME", os.path.expanduser("~")),
                        ".config", "tartci", "native-build-participation"))
    args = ap.parse_args(argv)

    if args.reload:
        return reload_command(args.reload, args.launch_agents_dir,
                              dry_run=args.dry_run,
                              allow_mid_job=args.allow_mid_job)

    # First, before anything that can block on a slow volume: a host that a
    # failed self-update left OFF is put back in service and, past 15 min,
    # reported loudly (scripts/host_off.py).
    host_off_line = host_off_pass(status_only=args.status or args.dry_run)
    if host_off_line:
        print(host_off_line)
    vm_boot_line = vm_boot_pass(status_only=args.status or args.dry_run)
    if vm_boot_line:
        print(vm_boot_line)

    agents = discover_agents(args.launch_agents_dir)
    # Compute the VM-running guard ONCE per pass. It is host-wide on purpose and
    # deliberately imprecise in the safe direction: neither wedge signature fires
    # while anything is building, so a busy host defers a heal to its next idle
    # window rather than risking a bootout under someone else's live job.
    vm_probe = probe_tart_vm_running()
    participating = pool_participating(args.participation_file)
    disabled = disabled_services()
    health = [
        gather_health(lbl, p, args.stale_log_seconds, vm_probe.running, participating,
                      args.restart_grace_seconds,
                      None if disabled is None else lbl not in disabled,
                      vm_probe.reason)
        for lbl, p in agents
    ]

    now = utcnow()
    heal_log = load_heal_log(_state_file())
    results: list[dict[str, Any]] = []
    acted = False
    heal_failed = False
    for h in health:
        entry: dict[str, Any] = {
            "label": h.label, "plist": h.plist,
            "verdict": h.verdict, "reason": h.reason,
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

    unhealthy = [r for r in results
                 if r["verdict"] in {"wedged", "broken", "attention"}]
    if not args.status and not args.dry_run and os.path.isfile(args.fleet_config):
        refresh_skew()
        tools_error = refresh_tools()
        if tools_error:
            print(f"{_iso(utcnow())} launchd-watchdog: WARN tool-freshness refresh failed: "
                  f"{tools_error}")
        tick_line = queue_tick_pass()
        if tick_line:
            print(tick_line)
        unlock_line = keychain_unlock_agent_pass()
        if unlock_line:
            print(unlock_line)
        saturation_line = queue_saturation_pass()
        if saturation_line:
            print(saturation_line)
        backstop_line = schedule_backstop_pass()
        if backstop_line:
            print(backstop_line)
        attestation_line = host_attestation_pass()
        if attestation_line:
            print(attestation_line)
        peer_stall_line = peer_stall_pass()
        if peer_stall_line:
            print(peer_stall_line)
        try:
            import power_status  # noqa: PLC0415 - sibling module
            power_status.refresh_sleep_events()
        except Exception as exc:  # noqa: BLE001 - the heal pass must go on
            print(f"{_iso(utcnow())} launchd-watchdog: WARN sleep count refresh failed: {exc}")
    config = config_verdicts(args.fleet_config, args.fleet_receipt)
    config_summary = config_problem(config)
    config["warned"] = False
    if not args.status and not args.dry_run:
        path = _config_state_file()
        try:
            with open(path) as fh:
                warn_state = json.load(fh)
        except Exception:
            warn_state = {}
        if should_warn_config(config_summary, warn_state if isinstance(warn_state, dict) else {},
                              now, args.config_warn_interval_seconds):
            config["warned"] = True
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".tmp", "w") as fh:
                    json.dump({"summary": config_summary, "warned_at": now}, fh)
                os.replace(path + ".tmp", path)
            except OSError:
                pass
    # Cross-host build disagreement: report only, after every heal so a slow
    # GitHub read can never delay one. Status and dry-run never run it.
    if not args.status and not args.dry_run:
        disagreement = build_disagreement_pass(args.fleet_config)
    else:
        import build_disagreement_watch as bdw
        last = bdw.load_state(bdw.default_state_path())
        disagreement = {"state": "not_run", "ran": False,
                        "last_state": last.get("last_state"),
                        "open_alarms": len(last.get("alarms") or {})}
    if args.json:
        print(json.dumps({
            "ts": _iso(now),
            "tart_vm_probe": {
                "state": (
                    "unavailable" if vm_probe.running is None
                    else "running" if vm_probe.running else "idle"
                ),
                "reason": vm_probe.reason,
                "executable": vm_probe.executable,
                "tart_home": vm_probe.tart_home,
            },
            "agents": results,
            "config": config,
            "build_disagreement": disagreement,
        }, indent=2))
    else:
        if not results:
            print(f"{_iso(now)} launchd-watchdog: no managed runner "
                  f"LaunchAgents found")
        for r in results:
            mark = {"healthy": "✓", "attention": "!", "wedged": "✗",
                    "broken": "✗", "unknown": "?"}.get(
                r["verdict"], "?")
            act = f" [{r['action']}]" if "action" in r else ""
            print(f"{_iso(now)}   {mark} {r['label']}: {r['reason']}{act}")
        if config["warned"] or (args.status and config_summary):
            # Report only: the watchdog never acts on configuration drift.
            print(f"{_iso(now)} launchd-watchdog: WARN config: {config_summary} "
                  "(report only; see `tartci fleet-macos verify-supply` / `profile-drift`)")
        # no_tomllib is already covered by the config WARN; printing it here
        # would repeat every pass rather than every due cycle.
        if disagreement.get("ran") or (disagreement.get("state") == "unknown"
                                       and disagreement.get("code") != "no_tomllib"):
            import build_disagreement_watch as bdw
            for line in bdw.render(disagreement, now):
                print(line)
        elif args.status and disagreement.get("open_alarms"):
            print(f"{_iso(now)} build-disagreement: last={disagreement.get('last_state')} "
                  f"open_alarms={disagreement['open_alarms']} (report only)")
    # Status reports unresolved wedges. Healing reports failure only when a
    # reload failed its postcondition; successful recovery exits zero.
    if args.status and unhealthy:
        return 1
    return 1 if heal_failed else 0


if __name__ == "__main__":
    sys.exit(main())
