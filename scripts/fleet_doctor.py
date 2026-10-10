#!/usr/bin/env python3
"""Answer "how is this fleet configured, is it working, and if not why" in one query.

Every fact here is reconstructible by hand from launchd, a receipt, a pool file
and the GitHub API. Reconstructing it by hand is how hosts stay broken: each
individual command returns a confident answer that is true about the narrow
thing it measured and misleading about the fleet, and nothing in any of those
outputs says which.

So the contract of this module is narrow and absolute: a check that cannot
determine its answer reports UNKNOWN with a code. It never falls back to the
reassuring value. The failures this exists to catch all wore a reassuring value
as a disguise -- an installer that exited 0 having changed nothing an agent
execs, a drain that refused after it had already opted the host out, a runner
census that read one of the two scopes runners register in, a readiness verdict
that flipped depending on which copy of the CLI asked.

Checks are pure functions over injected inputs so both cells of every test --
the fault present and the fault absent -- can be exercised without a fleet host.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import time

import host_profile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]

# A check reports exactly one of these. `unknown` is a first-class answer and
# outranks any default: the whole point is that "could not tell" must never be
# rendered as "fine". `not_applicable` is narrower and requires positive proof
# that the subject is absent, not merely that it was not found.
OK = "ok"
PROBLEM = "problem"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"

PERSISTENT_LABEL_PREFIX = "actions.runner."
HELD_IDLE = "held-idle"
REASONS_PATH = Path(__file__).resolve().parent / "fleet_reasons.json"

# Stable reason codes. Each one must carry a row in fleet_reasons.json saying
# why the state exists and what to do about it; a code without a row is a code
# whose meaning lives only in whoever wrote it.
CODES: tuple[str, ...] = (
    "agents_dir_unreadable",
    "census_complete",
    "census_identity_authenticated",
    "census_identity_unauthenticated",
    "census_identity_unknown",
    "census_incomplete",
    "census_module_unavailable",
    "census_repo_unknown",
    "delivery_unknown",
    "disk_axis_unread",
    "disk_floor_refusing",
    "effective_generation_matches",
    "effective_generation_mismatch",
    "fleet_not_ready",
    "fleet_ready",
    "gate_reserve_fits",
    "gate_reserve_not_applicable",
    "gate_reserve_overcommitted",
    "gate_reserve_unknown",
    "generation_path_exec",
    "hold_receipt_malformed",
    "hold_receipt_present",
    "home_volume_floor_not_judged",
    "home_volume_floor_ok",
    "host_agents_missing",
    "host_agents_not_applicable",
    "host_agents_ok",
    "host_agents_unreadable",
    "installed_generation_unknown",
    "lane_lease_never_fits",
    "lane_python_no_tomllib",
    "lane_python_not_applicable",
    "lane_python_tomllib",
    "lane_python_unknown",
    "lanes_exceed_lease_capacity",
    "launchd_registration_leaked",
    "launchd_registrations_ok",
    "launchd_registrations_unreadable",
    "launchd_timers_never",
    "launchd_timers_not_running",
    "launchd_timers_ok",
    "launchd_timers_stalled",
    "launchd_timers_unreadable",
    "lease_fit_ok",
    "lease_fit_unmeasured",
    "no_installed_profile",
    "no_managed_launchagents",
    "no_persistent_runners",
    "peer_reachability_ok",
    "peer_reachability_unreadable",
    "peer_unreachable",
    "peer_unreachable_excluded",
    "pf_not_applicable",
    "pf_boot_holder_missing",
    "pf_pfd_exiting",
    "pf_reference_missing",
    "pf_reference_ok",
    "pf_reference_unknown",
    "persistent_runners_without_hold_receipt",
    "power_ok",
    "power_sleeps",
    "power_unknown",
    "profile_drift",
    "profile_drift_unknown",
    "profile_in_sync",
    "program_unresolvable",
    "readiness_not_managed",
    "readiness_probe_failed",
    "readiness_verdict_depends_on_invocation",
    "reclaim_boot_low",
    "reclaim_failed",
    "reclaim_low_space",
    "reclaim_never_recorded",
    "reclaim_ok",
    "reclaim_pass_degraded",
    "reclaim_stale",
    "reclaim_unreadable",
    "reuse_canary_never",
    "reuse_canary_no_bindable",
    "reuse_canary_not_installed",
    "reuse_canary_off",
    "reuse_canary_ok",
    "reuse_canary_stale",
    "reuse_canary_unreadable",
    "sealed_launcher_bundle",
    "self_update_current",
    "self_update_paused",
    "self_update_problem",
    "self_update_unmeasured",
    "signing_prompts_not_applicable",
    "signing_prompts_ok",
    "signing_prompts_risk",
    "signing_prompts_unknown",
    "supply_match",
    "supply_mismatch",
    "supply_unknown",
    "support_agents_drift",
    "support_agents_never",
    "support_agents_ok",
    "support_agents_pending",
    "support_agents_unreadable",
    "tool_freshness_current",
    "tool_freshness_stale",
    "tool_freshness_unmeasured",
    "undeclared_fleet_agent",
    "undeclared_fleet_agents_none",
    "vm_janitor_loaded",
    "vm_janitor_missing",
    "vm_janitor_unknown",
    "vm_boot_degraded",
    "vm_boot_ok",
    "vm_boot_unmeasured",
    "vm_boot_unreadable",
    "vm_dhcp_bootpd_not_loaded",
    "vm_dhcp_config_disabled",
    "vm_dhcp_ok",
    "vm_dhcp_pfd_crash_loop",
    "vm_dhcp_unanswered",
    "vm_dhcp_unreadable",
    "vm_dhcp_verifying",
    "vm_dhcp_vm_network_missing",
    "warm_vm_none",
    "warm_vm_overdue",
    "warm_vm_parked",
    "warm_vm_stale",
    "warm_vm_unreadable",
    "worktrees_in_tmp",
    "worktrees_in_tmp_none",
    "worktrees_in_tmp_not_checked",
)


@dataclass(frozen=True)
class Finding:
    """One check's verdict, its machine-readable cause, and its evidence."""

    check: str
    state: str
    code: str
    detail: str
    facts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "check": self.check,
            "state": self.state,
            "code": self.code,
            "detail": self.detail,
            "facts": self.facts,
        }


def load_reasons(path: Path = REASONS_PATH) -> dict[str, dict]:
    """Read the reason table. An unreadable table degrades citation, not diagnosis."""
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    reasons = value.get("reasons")
    return reasons if isinstance(reasons, dict) else {}


# ── LaunchAgent exec resolution ────────────────────────────────────────────
#
# What a host runs is the program in the installed plist's ProgramArguments,
# never what an installer most recently staged. The classification of that
# program -- sealed launcher bundle versus generation path -- and the identity
# marker inside the artifact it execs are host_profile's delivery report, which
# is consumed here rather than re-derived. What this module adds is the
# comparison that report does not make: the in-force identity against the
# INSTALL RECEIPT. host_profile measures drift against the checkout's git HEAD,
# which answers "is this artifact old" and not "is this the artifact the last
# install claims to have delivered". The second question is the one a silent
# no-op hides behind.


def persistent_labels(agents_dir: Path) -> list[str]:
    """Stock persistent Actions listener labels installed on this host.

    The suffix filter matters: a `.plist.disabled` sibling is not an installed
    agent, and counting it would claim a drain obligation the host does not have.
    """
    return sorted(
        path.name.removesuffix(".plist")
        for path in agents_dir.glob(f"{PERSISTENT_LABEL_PREFIX}*.plist")
        if path.is_file() and not path.is_symlink()
    )


def installed_generation(receipt: dict | None) -> dict | None:
    """The cohort identity the install receipt claims is current."""
    if not isinstance(receipt, dict):
        return None
    support = receipt.get("support")
    if not isinstance(support, dict):
        return None
    commit = str(support.get("source_commit", "")) or None
    manifest = str(support.get("manifest_sha256", "")) or None
    root = str(support.get("root", "")) or None
    entrypoint = support.get("launch_entrypoint")
    launch_path = None
    if isinstance(entrypoint, dict):
        launch_path = str(entrypoint.get("path", "")) or None
    if commit is None or manifest is None:
        return None
    return {"source_commit": commit, "support_manifest_sha256": manifest,
            "root": root, "launch_entrypoint": launch_path}


def _executes_installed(lane: dict, installed: dict) -> bool | None:
    """Does this lane execute the installed cohort? None when unresolvable."""
    identity = lane.get("in_force") or {}
    commit = identity.get("source_commit")
    if lane.get("delivery") == "unknown" or not commit:
        return None
    if lane["delivery"] == "sealed-bundle":
        manifest = identity.get("support_manifest_sha256")
        if not manifest:
            return None
        return (commit == installed["source_commit"]
                and manifest == installed["support_manifest_sha256"])
    # A generation lane execs that generation's own launch entrypoint, so the
    # receipt's recorded path compares exactly with no naming convention in it.
    expected = installed.get("root") or (
        str(Path(installed["launch_entrypoint"]).parent)
        if installed.get("launch_entrypoint") else None)
    artifact = lane.get("artifact_root")
    if not expected or artifact is None:
        return None
    return Path(artifact) == Path(expected)


def check_executed_generation(report: dict, installed: dict | None) -> Finding:
    """Report the cohort the host EXECUTES against the one it records as installed."""
    check = "executed_generation"
    lanes = report.get("lanes") or []
    if not lanes and not report.get("plists_seen"):
        return Finding(check, UNKNOWN, "agents_dir_unreadable",
                       "no plist at all was readable in the LaunchAgent directory",
                       {"agents_dir": report.get("agents_dir")})
    if not lanes and installed is None:
        return Finding(check, NOT_APPLICABLE, "no_managed_launchagents",
                       "this host installs no managed fleet LaunchAgents and "
                       "holds no install receipt")
    if installed is None:
        return Finding(
            check, UNKNOWN, "installed_generation_unknown",
            "managed LaunchAgents are installed but no readable install receipt "
            "says which cohort they should execute",
            {"labels": sorted(lane["label"] for lane in lanes)})
    if not lanes:
        return Finding(
            check, PROBLEM, "no_managed_launchagents",
            "an install receipt records a cohort but no managed fleet "
            "LaunchAgent is installed to execute it",
            {"installed_generation": installed})

    effective: dict[str, dict] = {}
    mismatched: list[str] = []
    unresolved: list[str] = []
    for lane in sorted(lanes, key=lambda row: row["label"]):
        verdict = _executes_installed(lane, installed)
        identity = lane.get("in_force") or {}
        effective[lane["label"]] = {
            "delivery": lane.get("delivery"),
            "artifact_root": lane.get("artifact_root"),
            "source_commit": identity.get("source_commit"),
            "support_manifest_sha256": identity.get("support_manifest_sha256"),
            "executes_installed": verdict,
            "detail": identity.get("detail") or lane.get("detail"),
        }
        if verdict is None:
            unresolved.append(lane["label"])
        elif not verdict:
            mismatched.append(lane["label"])

    facts = {"installed_generation": installed, "effective_generation": effective}
    if mismatched:
        return Finding(
            check, PROBLEM, "effective_generation_mismatch",
            "the generation this host EXECUTES is not the generation it records "
            f"as installed ({len(mismatched)} of {len(lanes)} lanes): "
            + ", ".join(mismatched),
            facts)
    if unresolved:
        return Finding(
            check, UNKNOWN, "program_unresolvable",
            "the executed generation could not be resolved for: "
            + ", ".join(unresolved),
            facts)
    return Finding(check, OK, "effective_generation_matches",
                   f"all {len(lanes)} managed lanes execute the installed cohort",
                   facts)


def check_generation_delivery(report: dict) -> Finding:
    """Report whether staging a generation can change what this host executes.

    The verdict is host_profile's own `accepts_generation_install`, so there is
    exactly one classifier of delivery shape on this host and no second opinion
    to drift. What is added here is the aggregate: one lane that cannot receive
    a generation makes the host undeliverable by that route, whatever its
    siblings do.
    """
    check = "generation_delivery"
    lanes = report.get("lanes") or []
    if not lanes and not report.get("plists_seen"):
        return Finding(check, UNKNOWN, "agents_dir_unreadable",
                       "no plist at all was readable in the LaunchAgent directory",
                       {"can_receive_generation": None,
                        "agents_dir": report.get("agents_dir")})
    if not lanes:
        return Finding(check, NOT_APPLICABLE, "no_managed_launchagents",
                       "this host installs no managed fleet LaunchAgents",
                       {"can_receive_generation": None})
    unknown = sorted(lane["label"] for lane in lanes
                     if lane.get("accepts_generation_install") is None)
    sealed = sorted(lane["label"] for lane in lanes
                    if lane.get("accepts_generation_install") is False)
    remedies = sorted({lane["how_to_update"] for lane in lanes
                       if lane["label"] in sealed and lane.get("how_to_update")})
    if unknown:
        return Finding(
            check, UNKNOWN, "delivery_unknown",
            "some lanes declare no recognised launch entrypoint, so it is not "
            "known whether a staged generation would reach them: "
            + ", ".join(unknown),
            {"can_receive_generation": None, "unresolved": unknown,
             "sealed_bundle_lanes": sealed})
    if sealed:
        roots = sorted({lane["artifact_root"] for lane in lanes
                        if lane["label"] in sealed and lane.get("artifact_root")})
        return Finding(
            check, PROBLEM, "sealed_launcher_bundle",
            "this host executes a sealed launcher bundle, so a generation stage "
            "alone cannot change what it runs: " + ", ".join(roots),
            {"can_receive_generation": False, "sealed_bundle_lanes": sealed,
             "bundles": roots, "how_to_update": remedies})
    return Finding(
        check, OK, "generation_path_exec",
        f"all {len(lanes)} managed lanes exec a generation path directly, so a "
        "staged generation reaches them",
        {"can_receive_generation": True})


# ── Drain capability ───────────────────────────────────────────────────────


def read_hold_receipt(path: Path) -> tuple[bool | None, str]:
    """Is the held-idle receipt present and exact? None when it cannot be read."""
    try:
        if not path.is_file():
            return False, "absent"
    except OSError as exc:
        return None, f"unreadable: {exc}"
    try:
        value = path.read_text()
    except OSError as exc:
        return None, f"unreadable: {exc}"
    stripped = "".join(value.split())
    if stripped == HELD_IDLE:
        return True, "held-idle"
    return False, f"present but not exactly {HELD_IDLE!r}"


def check_drain_capability(agents_dir: Path, hold_file: Path,
                           *, agents_dir_readable: bool = True) -> Finding:
    """Report whether `tartci pool drain` can complete, BEFORE a deploy starts.

    Drain is not atomic. It writes participation=0 and pool-state=draining and
    only then discovers it cannot quiesce a persistent Actions listener, so a
    refusal leaves the host opted out with its agents still loaded. Knowing the
    answer in advance is the only way to avoid entering that state.
    """
    check = "drain_capability"
    if not agents_dir_readable:
        return Finding(check, UNKNOWN, "agents_dir_unreadable",
                       "the LaunchAgent directory could not be listed",
                       {"can_drain": None})
    persistent = persistent_labels(agents_dir)
    if not persistent:
        return Finding(check, OK, "no_persistent_runners",
                       "no persistent Actions listener is installed, so drain "
                       "terminalizes on provider agents alone",
                       {"can_drain": True, "persistent_runners": []})
    held, detail = read_hold_receipt(hold_file)
    facts = {"can_drain": None, "persistent_runners": persistent,
             "hold_receipt_path": str(hold_file), "hold_receipt": detail}
    if held is None:
        return Finding(check, UNKNOWN, "hold_receipt_malformed",
                       f"the held-idle receipt could not be read: {detail}", facts)
    if held:
        facts["can_drain"] = True
        return Finding(check, OK, "hold_receipt_present",
                       f"{len(persistent)} persistent listeners are covered by an "
                       "exact held-idle receipt", facts)
    facts["can_drain"] = False
    return Finding(
        check, PROBLEM, "persistent_runners_without_hold_receipt",
        f"drain will REFUSE: {len(persistent)} persistent Actions listeners are "
        "installed and no authoritative held-idle receipt covers them; the "
        "refusal happens after the host is already opted out",
        facts)


# ── Runner census ──────────────────────────────────────────────────────────
#
# Registrations here are ephemeral and minted per job, so zero at idle is the
# expected reading rather than a dead host. That caveat travels with the
# numbers because the numbers are read without it and acted on.

IDLE_ZERO_NOTE = (
    "runners are ephemeral and minted per job, so ZERO ONLINE AT IDLE IS "
    "NORMAL and is not evidence that this host is dead"
)


def check_runner_census(census: Any, *, repo: str = "", error_code: str | None = None,
                        error_detail: str = "") -> Finding:
    """Report the runner census across BOTH registration scopes.

    A repository-scoped listing silently omits organization-scoped runners, so
    a one-endpoint census answers a capacity question with a confident smaller
    number. The verdict here is about scope COMPLETENESS, not about counts:
    with ephemeral runners no count at idle is by itself good or bad.
    """
    check = f"runner_census[{repo}]" if repo else "runner_census"
    if error_code is not None:
        return Finding(check, UNKNOWN, error_code,
                       error_detail or "the runner census could not be taken",
                       {"idle_zero_is_normal": True, "note": IDLE_ZERO_NOTE})
    # Counts are read from the census objects, never from their serialized
    # form. A serialization that renames or omits a field would make every
    # count here a silent zero, which is the same confident-undercount failure
    # the dual-scope census exists to prevent. Reading attributes fails loudly.
    try:
        scopes = {
            scope.scope: {
                "endpoint": scope.endpoint,
                "reachable": scope.reachable,
                "applicable": scope.applicable,
                "registered": len(scope.runners) if scope.reachable else None,
                "error": scope.error or None,
            }
            for scope in census.scopes
        }
        applicable_scopes = sum(1 for scope in census.scopes if scope.applicable)
        records = list(census.runners)
        complete = census.complete
        facts = {
            "repo": census.repo,
            "scopes": scopes,
            "total_registered": len(records),
            "online": sum(1 for record in records if record.online),
            "idle_zero_is_normal": True,
            "note": IDLE_ZERO_NOTE,
        }
    except AttributeError as exc:
        return Finding(
            check, UNKNOWN, "census_incomplete",
            f"the census object does not expose the expected interface: {exc}",
            {"idle_zero_is_normal": True, "note": IDLE_ZERO_NOTE})
    if not complete:
        return Finding(
            check, UNKNOWN, "census_incomplete",
            "a registration scope could not be read, so no count here is a "
            f"capacity answer: {census.unreachable_detail()}", facts)
    return Finding(
        check, OK, "census_complete",
        f"{facts['total_registered']} registration(s) across "
        f"{applicable_scopes} scope(s), {facts['online']} online. {IDLE_ZERO_NOTE}",
        facts)


# ── Readiness, and whether the instruments agree ───────────────────────────


def check_readiness(probes: dict[str, dict], *, authority: str) -> Finding:
    """Report fleet readiness and reconcile the instruments that answer it.

    Readiness is computed against a support root, and the support root comes
    from whichever copy of the CLI asked. A checkout and the installed
    generation therefore return opposite verdicts for the same host at the same
    instant, each correct about the tree it measured and each read as a
    statement about the fleet. The authoritative verdict is the installed
    generation's, because that is the tree the running supervisors execute.
    """
    check = "readiness"
    verdicts = {
        root: (None if not isinstance(value, dict) else value.get("fleet_ready"))
        for root, value in probes.items()
    }
    facts = {
        "authority_support_root": authority,
        "probes": {
            root: {
                "fleet_ready": verdicts[root],
                "managed": (value or {}).get("managed"),
                "verified_running_supervisors": (value or {}).get(
                    "verified_running_supervisors"),
                "expected_supervisors": (value or {}).get("expected_supervisors"),
                "problems": (value or {}).get("problems") or [],
                "error": (value or {}).get("error"),
            }
            for root, value in probes.items()
        },
    }
    # Only roots that produced a real verdict can disagree. A root whose probe
    # failed has no opinion, and reporting that as a disagreement would name the
    # wrong fault and send an operator looking for a split that is not there.
    answered = {root: value for root, value in verdicts.items()
                if isinstance(value, bool)}
    if len(answered) > 1 and len(set(answered.values())) > 1:
        rendering = "; ".join(
            f"{root} says fleet_ready={answered[root]}" for root in sorted(answered))
        return Finding(
            check, PROBLEM, "readiness_verdict_depends_on_invocation",
            "two support roots on this host return different readiness verdicts "
            f"for the same fleet: {rendering}", facts)

    authoritative = probes.get(authority)
    if not isinstance(authoritative, dict) or authoritative.get("error"):
        detail = (authoritative or {}).get("error") or "the readiness probe produced no result"
        return Finding(check, UNKNOWN, "readiness_probe_failed",
                       f"readiness could not be determined: {detail}", facts)
    if authoritative.get("managed") is False:
        return Finding(check, NOT_APPLICABLE, "readiness_not_managed",
                       "this host carries no managed fleet profile", facts)
    ready = authoritative.get("fleet_ready")
    problems = authoritative.get("problems") or []
    if ready is True:
        return Finding(check, OK, "fleet_ready",
                       "the installed generation reports the fleet ready", facts)
    if ready is False:
        codes = ", ".join(
            str(problem.get("code")) for problem in problems if isinstance(problem, dict)
        ) or "no problem code was reported"
        return Finding(check, PROBLEM, "fleet_not_ready",
                       f"the fleet is not ready: {codes}", facts)
    return Finding(check, UNKNOWN, "readiness_probe_failed",
                   "the readiness probe returned no fleet_ready verdict", facts)


# ── Assembly ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Diagnosis:
    host: str
    findings: tuple[Finding, ...]
    reasons: dict = field(default_factory=dict)

    @property
    def worst(self) -> str:
        states = {finding.state for finding in self.findings}
        for state in (PROBLEM, UNKNOWN, OK):
            if state in states:
                return state
        return NOT_APPLICABLE

    def exit_code(self) -> int:
        """0 healthy, 1 a problem was found, 2 something could not be determined.

        Unknown gets its own code so a caller can tell "nothing is wrong" from
        "nothing could be measured"; collapsing them is the mistake this whole
        command exists to stop.
        """
        return {PROBLEM: 1, UNKNOWN: 2}.get(self.worst, 0)

    def as_dict(self) -> dict:
        rows = []
        for finding in self.findings:
            row = finding.as_dict()
            reason = self.reasons.get(finding.code)
            if reason:
                row["why"] = reason
            rows.append(row)
        return {"schema": 1, "host": self.host, "state": self.worst,
                "findings": rows}


def check_profile_drift(result: dict | None, *, installed_present: bool,
                        error: str = "") -> Finding:
    """Installed fleet profile vs the checked-in profile carrying its name.

    Every receipt check binds the installed copy to itself, so a host whose
    profile was edited in place, or installed before the checked-in source
    moved on, verifies clean everywhere else.
    """
    if not installed_present:
        return Finding("profile_drift", NOT_APPLICABLE, "no_installed_profile",
                       "no installed macOS fleet profile on this host")
    if result is None or result.get("state") not in ("in_sync", "drift"):
        reason = error or (result or {}).get("reason") or "drift check produced no verdict"
        return Finding("profile_drift", UNKNOWN, "profile_drift_unknown", reason,
                       {"result": result})
    if result["state"] == "in_sync":
        return Finding("profile_drift", OK, "profile_in_sync",
                       f"installed profile matches {result.get('checked_in')}",
                       {"checked_in": result.get("checked_in")})
    keys = sorted([*result.get("missing_in_installed", {}),
                   *result.get("extra_in_installed", {}),
                   *result.get("changed", {})])
    return Finding("profile_drift", PROBLEM, "profile_drift",
                   f"installed profile differs from {result.get('checked_in')} at: "
                   + ", ".join(keys),
                   {k: result.get(k) for k in (
                       "checked_in", "missing_in_installed",
                       "extra_in_installed", "changed")})


def profile_drift_probe(support_root: Path, installed: Path,
                        python: str | None, timeout: int = 30) -> tuple[dict | None, str]:
    if python is None:
        return None, "no Python 3.11+ interpreter with tomllib is available"
    script = support_root / "scripts" / "macos_fleet_lanes.py"
    try:
        proc = subprocess.run(
            [python, str(script), "profile-drift", "--installed", str(installed),
             "--profiles-dir", str(support_root / "profiles"), "--json"],
            capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"drift check did not complete: {exc}"
    try:
        return json.loads(proc.stdout), ""
    except json.JSONDecodeError:
        return None, (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"


def check_supply(result: dict | None, *, installed_present: bool,
                 error: str = "") -> Finding:
    """This host's installed registrations vs the published declared supply."""
    if not installed_present:
        return Finding("supply", NOT_APPLICABLE, "no_installed_profile",
                       "no installed macOS fleet profile on this host")
    if result is None or result.get("state") not in ("match", "mismatch"):
        reason = error or (result or {}).get("reason") or "supply check produced no verdict"
        return Finding("supply", UNKNOWN, "supply_unknown", reason, {"result": result})
    lanes = result.get("lanes", [])
    if result["state"] == "match":
        return Finding("supply", OK, "supply_match",
                       f"{len(lanes)} installed registrations match the published "
                       f"supply for host_id {result.get('host_id')}",
                       {"lanes": lanes})
    bad = [f"{row['lane']}{'[' + row['class_label'] + ']' if row.get('class_label') else ''}"
           f"={row['verdict']}" for row in lanes if row.get("verdict") != "MATCH"]
    return Finding("supply", PROBLEM, "supply_mismatch",
                   "installed registrations differ from the published supply: "
                   + ", ".join(bad), {"lanes": lanes})


def supply_probe(support_root: Path, installed: Path, python: str | None,
                 timeout: int = 30) -> tuple[dict | None, str]:
    if python is None:
        return None, "no Python 3.11+ interpreter with tomllib is available"
    script = support_root / "scripts" / "macos_fleet_lanes.py"
    try:
        proc = subprocess.run(
            [python, str(script), "verify-supply", "--installed", str(installed), "--json"],
            capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"supply check did not complete: {exc}"
    try:
        return json.loads(proc.stdout), ""
    except json.JSONDecodeError:
        return None, (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"


def check_lease_fit(records: list[dict], missing: list[str], *,
                    managed: bool) -> Finding:
    """Can every managed lane lease its VM, and can identical lanes run together?

    Read from each lane's own last lease-fit verdict (lease-fit.lib.sh), so the
    answer uses the capacity model acquisition uses instead of a re-derivation.
    """
    import lease_fit

    check = "lease_fit"
    if not managed:
        return Finding(check, NOT_APPLICABLE, "no_managed_launchagents",
                       "no managed macOS fleet lanes are installed")
    findings = lease_fit.configuration_findings(records)
    facts = {"lanes_measured": len(records), "lanes_unmeasured": missing,
             **findings}
    if findings["never"]:
        names = ", ".join(
            f"{row.get('lane') or row.get('label')} ({row.get('requested_cores')} cores "
            f"> budget {row.get('core_budget')})" for row in findings["never"])
        return Finding(check, PROBLEM, "lane_lease_never_fits",
                       f"these lanes' VM leases can never be granted here: {names}",
                       facts)
    if findings["oversubscribed"]:
        parts = []
        for group in findings["oversubscribed"]:
            parts.append(
                f"{len(group['lanes'])} lanes of {group['vm_cores']}-core VMs "
                f"({', '.join(group['lanes'])}) but the {group['core_budget']}-core "
                f"budget fits {group['max_concurrent']} at once")
        return Finding(check, PROBLEM, "lanes_exceed_lease_capacity",
                       "; ".join(parts), facts)
    if missing and not records:
        return Finding(check, UNKNOWN, "lease_fit_unmeasured",
                       "no lane has recorded a lease-fit verdict yet: "
                       + ", ".join(missing), facts)
    return Finding(check, OK, "lease_fit_ok",
                   f"{len(records)} lanes can lease their VMs"
                   + (f" ({len(missing)} not yet measured)" if missing else ""),
                   facts)


def check_self_update(summary: dict | None) -> Finding:
    """tartci's own skew against main and the last self-update attempt."""
    if not isinstance(summary, dict) or summary.get("skew") is None:
        return Finding("self_update", UNKNOWN, "self_update_unmeasured",
                       "tartci's skew against main was never measured on this host "
                       "(run `tartci fleet-macos self-update --plan`)")
    lines = "; ".join(summary.get("lines") or [])
    if summary.get("paused"):
        # Not a failed update: launchd is not starting it and the interval
        # guard holds it for the stall, so "read the receipt" finds nothing.
        return Finding("self_update", PROBLEM, "self_update_paused",
                       f"{summary['paused']} ({lines})", {"skew": summary.get("skew"),
                                                          "last": summary.get("last")})
    if summary.get("problem"):
        return Finding("self_update", PROBLEM, "self_update_problem",
                       f"{summary['problem']} ({lines})", {"skew": summary.get("skew"),
                                                          "last": summary.get("last")})
    return Finding("self_update", OK, "self_update_current", lines,
                   {"skew": summary.get("skew"), "last": summary.get("last")})


def check_gate_reserve(value: dict | None, *, installed_present: bool) -> Finding:
    """Each gate lane against this host's live gate reserve (gate_reserve_fit.py)."""
    if not installed_present:
        return Finding("gate_reserve", NOT_APPLICABLE, "gate_reserve_not_applicable",
                       "no installed fleet profile")
    value = value or {}
    lines = value.get("lines") or []
    if value.get("problem"):
        return Finding("gate_reserve", PROBLEM, "gate_reserve_overcommitted",
                       f"{value['problem']}; resizing is a profile decision with the host's "
                       "owner and must not take agent cores", {"gate_reserve": value})
    if not lines or "UNKNOWN" in lines[0]:
        return Finding("gate_reserve", UNKNOWN, "gate_reserve_unknown",
                       lines[0] if lines else "not computed", {"gate_reserve": value})
    if lines[0].startswith("gate reserve: n/a"):
        # Gate lanes with no reserve to fit them in: unmeasurable, not a fit.
        return Finding("gate_reserve", NOT_APPLICABLE, "gate_reserve_not_applicable",
                       lines[0], {"gate_reserve": value})
    return Finding("gate_reserve", OK, "gate_reserve_fits", "; ".join(lines),
                   {"gate_reserve": value})


def check_home_volume(value: dict | None, *, lanes: int, now: float | None = None,
                      unread_after_s: float = 3600) -> Finding:
    """The home-volume admission floor (scripts/home_volume_floor.py).

    A floor that refuses everything looks exactly like a full disk from
    outside, so a refusal streak as long as this host's lane count is its own
    problem; so is an axis that has not been readable for a reclaim cadence,
    because every admission in that time skipped it.
    """
    now = time.time() if now is None else now
    if not value:
        return Finding("home_volume", NOT_APPLICABLE, "home_volume_floor_not_judged",
                       "no VM admission has judged the home volume (it is the Tart store's "
                       "own volume, or no lease has been taken since this check existed)")
    facts = {"home_volume": {k: v for k, v in value.items() if k != "samples"}}
    since = value.get("unread_since")
    if isinstance(since, (int, float)) and now - since >= unread_after_s:
        return Finding("home_volume", PROBLEM, "disk_axis_unread",
                       f"the home volume has not been readable for {(now - since) / 3600:.1f} h; "
                       f"every VM admission in that time skipped it "
                       f"({value.get('unread_reason')})", facts)
    last = value.get("last") or {}
    streak = int(value.get("consecutive_denials") or 0)
    gib = 1024 ** 3
    text = (f"free {last.get('free_bytes', 0) / gib:.0f} GiB, floor "
            f"{last.get('floor_bytes', 0) / gib:.0f} GiB")
    if streak >= max(1, lanes):
        return Finding("home_volume", PROBLEM, "disk_floor_refusing",
                       f"{streak} VM admissions in a row below the home-volume floor "
                       f"({text}; refused, or would-refuse in report mode); reclaim the "
                       f"volume, or the floor is wrong", facts)
    return Finding("home_volume", OK, "home_volume_floor_ok",
                   f"{text}; {streak} consecutive refusals", facts)


def check_tool_freshness(summary: dict | None) -> Finding:
    """Shipyard and the pulp CLI against their latest releases."""
    if not isinstance(summary, dict) or summary.get("state") is None:
        return Finding("tool_freshness", UNKNOWN, "tool_freshness_unmeasured",
                       "tool freshness was never measured on this host "
                       "(run `tartci fleet-macos tool-freshness --refresh`)")
    lines = "; ".join(summary.get("lines") or [])
    if summary.get("problem"):
        return Finding("tool_freshness", PROBLEM, "tool_freshness_stale",
                       f"{summary['problem']} ({lines})", {"state": summary["state"]})
    return Finding("tool_freshness", OK, "tool_freshness_current", lines,
                   {"state": summary["state"]})


def check_warm_vm(value: dict | None) -> Finding:
    """The host's parked warm gate VM (opt-in; scripts/warm_vm_status.py)."""
    state = (value or {}).get("state")
    facts = {"warm_vm": value}
    if state == "none":
        return Finding("warm_vm", NOT_APPLICABLE, "warm_vm_none", "no warm VM parked", facts)
    if state == "parked":
        return Finding("warm_vm", OK, "warm_vm_parked",
                       f"{value.get('vm')} parked {value.get('parked_seconds')}s of "
                       f"{value.get('max_park_seconds')}s, 0 cores reserved", facts)
    if state == "stale":
        return Finding("warm_vm", PROBLEM, "warm_vm_stale",
                       f"record for {value.get('vm')} is not being refreshed by supervisor "
                       f"pid {value.get('supervisor_pid')}", facts)
    if state == "overdue":
        return Finding("warm_vm", PROBLEM, "warm_vm_overdue",
                       f"{value.get('vm')} parked {value.get('parked_seconds')}s, past its "
                       f"{value.get('max_park_seconds')}s max", facts)
    return Finding("warm_vm", UNKNOWN, "warm_vm_unreadable",
                   f"warm VM record unreadable: {(value or {}).get('error')}", facts)


# LaunchAgent labels tartci installs. A loaded job under one of these whose
# plist is not in the account's own LaunchAgents directory was registered by
# something other than an installer run as this account: in practice a test or
# script that ran an installer with a temporary HOME. The label then SHADOWS
# the real agent, and every "is it loaded" view still says yes.
TARTCI_AGENT_PREFIXES = (
    "com.danielraffel.tartci.",
    "com.danielraffel.pulp.",
    "com.danielraffel.shipyard.",
    "com.danielraffel.forge.",
)


def check_launchd_registrations(rows: list[dict] | None, *, home: Path,
                                 error: str = "") -> Finding:
    """Every loaded tartci LaunchAgent must come from <home>/Library/LaunchAgents.

    `rows` is [{"label", "path"}] for each loaded job whose label carries a
    tartci prefix; `path` is launchd's own record of the plist it loaded (None
    when launchd holds no path for it). None means launchd could not be asked.
    """
    if rows is None:
        return Finding("launchd_registrations", UNKNOWN, "launchd_registrations_unreadable",
                       f"could not list this account's launchd jobs: {error or 'no launchctl'}")
    agents = str(home / "Library" / "LaunchAgents") + "/"
    leaked = [row for row in rows
              if not (isinstance(row.get("path"), str) and row["path"].startswith(agents)
                      and "/" not in row["path"][len(agents):])]
    facts = {"loaded": len(rows), "leaked": leaked, "expected_dir": agents.rstrip("/")}
    if leaked:
        names = ", ".join(f"{row['label']} <- {row.get('path') or 'no plist path'}"
                          for row in leaked)
        return Finding("launchd_registrations", PROBLEM, "launchd_registration_leaked",
                       f"{len(leaked)} loaded tartci job(s) registered from outside "
                       f"{agents.rstrip('/')}: {names}", facts)
    return Finding("launchd_registrations", OK, "launchd_registrations_ok",
                   f"{len(rows)} loaded tartci job(s), all from {agents.rstrip('/')}", facts)


# The agents that make a fleet host self-maintaining: the watchdog heals lanes
# and refreshes skew and tool freshness; self-update installs tartci from main.
# A host brought up without them runs, serves, and silently never updates.
REQUIRED_HOST_AGENTS = (
    "com.danielraffel.tartci.launchd-watchdog",
    "com.danielraffel.tartci.self-update",
)


def check_host_agents(rows: list[dict] | None, *, managed: bool,
                      error: str = "") -> Finding:
    """A managed fleet host must have the watchdog and self-update agents loaded."""
    if not managed:
        return Finding("host_agents", NOT_APPLICABLE, "host_agents_not_applicable",
                       "no installed fleet profile: not a managed fleet host")
    if rows is None:
        return Finding("host_agents", UNKNOWN, "host_agents_unreadable",
                       f"could not list this account's launchd jobs: {error or 'no launchctl'}")
    loaded = {row.get("label") for row in rows}
    missing = [label for label in REQUIRED_HOST_AGENTS if label not in loaded]
    facts = {"required": list(REQUIRED_HOST_AGENTS), "missing": missing}
    if missing:
        return Finding("host_agents", PROBLEM, "host_agents_missing",
                       f"not loaded: {', '.join(missing)}; this host never refreshes skew "
                       "or tool freshness and never updates itself", facts)
    return Finding("host_agents", OK, "host_agents_ok",
                   "watchdog and self-update agents are loaded", facts)


def launchd_registrations(run: Callable[[list[str]], tuple[int, str, str]] | None = None
                          ) -> tuple[list[dict] | None, str]:
    """(rows, error) for every loaded job with a tartci label, read from launchd."""
    import re

    def default_run(argv: list[str]) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, "", str(exc)
        return proc.returncode, proc.stdout, proc.stderr

    run = run or default_run
    code, out, err = run(["/bin/launchctl", "list"])
    if code != 0:
        return None, (err or out).strip()[:200] or f"launchctl list exit {code}"
    labels = sorted({line.split("\t")[-1].strip() for line in out.splitlines()[1:]
                     if line.split("\t")[-1].strip().startswith(TARTCI_AGENT_PREFIXES)})
    rows = []
    for label in labels:
        code, text, err = run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"])
        if code != 0:
            continue  # listed in another domain or gone since the list
        match = re.search(r"^\tpath = (.+)$", text, re.M)
        rows.append({"label": label, "path": match.group(1).strip() if match else None})
    return rows, ""


def check_worktrees_in_tmp(value: dict | None, *, reason: str = "") -> Finding:
    """Pulp worktrees under /tmp (report only; pulp_reapers.tmp_worktrees)."""
    import pulp_reapers

    if value is None:
        return Finding("worktrees_in_tmp", NOT_APPLICABLE, "worktrees_in_tmp_not_checked",
                       f"not checked: {reason or '[reclaim] not enabled'}")
    facts = {"worktrees_in_tmp": value}
    if value.get("count") is None:
        return Finding("worktrees_in_tmp", UNKNOWN, "worktrees_in_tmp_not_checked",
                       f"could not list worktrees: {value.get('error')}", facts)
    if value["count"] == 0:
        return Finding("worktrees_in_tmp", OK, "worktrees_in_tmp_none",
                       "no Pulp worktree under /tmp or /private/tmp", facts)
    oldest = value.get("oldest_mtime")
    age = "unknown" if oldest is None else \
        f"{(time.time() - oldest) / 86400:.1f}d"
    return Finding("worktrees_in_tmp", PROBLEM, "worktrees_in_tmp",
                   f"{value['count']} Pulp worktree(s) under /tmp or /private/tmp, "
                   f"size {value.get('size')}, oldest {age} old. "
                   f"{pulp_reapers.TMP_WORKTREE_RULE}. Report only: nothing moves "
                   "or deletes them; their owners must.", facts)


def check_launchd_timers(value: dict | None) -> Finding:
    """Whether launchd still starts the fleet's timer jobs (launchd_interval_guard.py)."""
    import launchd_interval_guard

    value = value or {"state": "unreadable", "error": "no status"}
    state = value.get("state")
    facts = {"launchd_timers": value}
    if state == "unreadable":
        return Finding("launchd_timers", UNKNOWN, "launchd_timers_unreadable",
                       f"interval guard status unreadable: {value.get('error')}", facts)
    detail = launchd_interval_guard.describe(value)
    if state == "stalled":
        return Finding("launchd_timers", PROBLEM, "launchd_timers_stalled", detail, facts)
    if state == "stale":
        return Finding("launchd_timers", UNKNOWN, "launchd_timers_not_running", detail, facts)
    if state == "never":
        return Finding("launchd_timers", UNKNOWN, "launchd_timers_never", detail, facts)
    return Finding("launchd_timers", OK, "launchd_timers_ok", detail, facts)


def check_reclaim(value: dict | None) -> Finding:
    """The disk reclaimer's last pass, from its receipt (scripts/reclaim_status.py)."""
    import reclaim_status

    value = value or {"state": "unreadable", "error": "no status"}
    state = value.get("state")
    detail = reclaim_status.describe(value) if state != "unreadable" or value.get("receipt") \
        else f"reclaim status unreadable: {value.get('error')}"
    facts = {"reclaim": value}
    if state == "ok" and reclaim_status.degraded(value):
        return Finding("reclaim", PROBLEM, "reclaim_pass_degraded", detail, facts)
    if state == "ok":
        return Finding("reclaim", OK, "reclaim_ok", detail, facts)
    if state == "low_space":
        return Finding("reclaim", PROBLEM, "reclaim_low_space", detail, facts)
    if state == "boot_low":
        return Finding("reclaim", PROBLEM, "reclaim_boot_low", detail, facts)
    if state == "failed":
        return Finding("reclaim", PROBLEM, "reclaim_failed", detail, facts)
    if state == "stale":
        return Finding("reclaim", PROBLEM, "reclaim_stale", detail, facts)
    if state == "never":
        return Finding("reclaim", UNKNOWN, "reclaim_never_recorded", detail, facts)
    return Finding("reclaim", UNKNOWN, "reclaim_unreadable", detail, facts)


def check_support_agents(value: dict | None) -> list[Finding]:
    """The declared support agents' last pass, and any undeclared fleet agent.

    Both come from the receipt `support_agents.py` writes after every
    self-update (scripts/support_agents.py).
    """
    value = value or {"state": "unreadable", "error": "no status"}
    state = value.get("state")
    facts = {"support_agents": value}
    changes = ", ".join(value.get("changes") or [])
    if state == "ok":
        agents = value.get("agents") or {}
        found = Finding("support_agents", OK, "support_agents_ok",
                        f"{len(agents)} declared support agents match their renders", facts)
    elif state == "pending":
        found = Finding("support_agents", UNKNOWN, "support_agents_pending",
                        f"bootstrap is off; would install or change: {changes}", facts)
    elif state == "drift":
        found = Finding("support_agents", PROBLEM, "support_agents_drift",
                        f"declared support agents not converged: {changes}", facts)
    elif state == "never":
        found = Finding("support_agents", UNKNOWN, "support_agents_never",
                        "no support-agents receipt yet", facts)
    else:
        found = Finding("support_agents", UNKNOWN, "support_agents_unreadable",
                        f"support-agents status unreadable: {value.get('error')}", facts)
    undeclared = [u.get("label") for u in value.get("undeclared") or []]
    if undeclared:
        extra = Finding("undeclared_fleet_agent", PROBLEM, "undeclared_fleet_agent",
                        f"installed but declared nowhere (reported, never removed): "
                        f"{', '.join(undeclared)}", facts)
    else:
        extra = Finding("undeclared_fleet_agent", OK, "undeclared_fleet_agents_none",
                        "every tartci-prefix agent is declared or owned by a named installer"
                        if state in ("ok", "pending", "drift") else
                        "no receipt to scan yet", facts)
    return [found, extra]


VM_JANITOR = "reap"


def check_vm_janitor(value: dict | None) -> Finding:
    """Whether the VM janitor (com.danielraffel.tartci.reap) is installed and loaded.

    Without it a stale VM or overlay stays until someone notices: on
    2026-10-09 it was loaded on m3 only, while every profile declared it,
    because `bootstrap = false` kept the support-agents step to a plan.
    """
    value = value or {"state": "unreadable"}
    facts = {"vm_janitor": (value.get("agents") or {}).get(VM_JANITOR)}
    if value.get("state") in (None, "unreadable", "never"):
        return Finding("vm_janitor", UNKNOWN, "vm_janitor_unknown",
                       "no readable support-agents receipt to say whether the VM janitor "
                       "is installed", facts)
    if VM_JANITOR not in (value.get("declared") or []):
        return Finding("vm_janitor", PROBLEM, "vm_janitor_missing",
                       "the VM janitor (reap) is not declared in this host's profile", facts)
    entry = (value.get("agents") or {}).get(VM_JANITOR) or {}
    # `changes` is support_agents.status's verdict, which accounts for what an
    # apply pass installed; the per-agent state is what the pass found before.
    if VM_JANITOR not in (value.get("changes") or []):
        return Finding("vm_janitor", OK, "vm_janitor_loaded",
                       "the VM janitor (reap) is installed and loaded", facts)
    return Finding("vm_janitor", PROBLEM, "vm_janitor_missing",
                   f"the VM janitor (reap) is declared but {entry.get('state') or 'unknown'}"
                   f"{'' if entry.get('loaded') else ', not loaded'}", facts)


def check_reuse_canary(value: dict | None) -> Finding:
    """Whether the reuse canary keeps a bindable record (scripts/reuse_canary.py)."""
    value = value or {"state": "unreadable", "error": "no status"}
    state = value.get("state")
    facts = {"reuse_canary": value}

    def hours(key: str) -> str:
        at = value.get(key)
        return "never" if not at else f"{(float(value.get('now') or 0) - float(at)) / 3600:.1f} h ago"

    if state == "off":
        return Finding("reuse_canary", OK, "reuse_canary_off",
                       "reuse canary not enabled on this host", facts)
    if state == "not_installed":
        return Finding("reuse_canary", PROBLEM, "reuse_canary_not_installed",
                       "the profile enables the reuse canary but its LaunchAgent is not installed",
                       facts)
    if state == "never":
        return Finding("reuse_canary", UNKNOWN, "reuse_canary_never",
                       "reuse canary installed; no pass recorded yet", facts)
    if state == "stale":
        return Finding("reuse_canary", PROBLEM, "reuse_canary_stale",
                       f"no reuse canary pass for over 13 h (last {hours('newest_receipt_at')})",
                       facts)
    if state == "no_bindable":
        return Finding("reuse_canary", PROBLEM, "reuse_canary_no_bindable",
                       "no bindable reuse record for 36 h (last bindable "
                       f"{hours('newest_bindable_at')}; newest pass "
                       f"{value.get('newest_outcome')})", facts)
    if state == "ok":
        return Finding("reuse_canary", OK, "reuse_canary_ok",
                       f"last bindable reuse record {hours('newest_bindable_at')}", facts)
    return Finding("reuse_canary", UNKNOWN, "reuse_canary_unreadable",
                   f"reuse canary status unreadable: {value.get('error')}", facts)


def check_lane_python(value: dict | None) -> Finding:
    """`python3` on each lane's PATH imports tomllib (scripts/lane_python.py)."""
    if value is None or value.get("error"):
        return Finding("lane_python", UNKNOWN, "lane_python_unknown",
                       f"lane python3 not probed: {(value or {}).get('error', 'no status')}",
                       {"lane_python": value})
    rows = value.get("rows") or []
    facts = {"lane_python": value}
    if not rows:
        return Finding("lane_python", NOT_APPLICABLE, "lane_python_not_applicable",
                       "no installed lane plist names a PATH", facts)
    bad = [row for row in rows if row.get("tomllib") is False]
    if bad:
        parts = [(f"{row['python']} {row.get('version') or '(version unread)'}"
                  if row.get("python") else "no python3")
                 + f" for {', '.join(row.get('labels') or [])}" for row in bad]
        return Finding("lane_python", PROBLEM, "lane_python_no_tomllib",
                       "python3 on the lane PATH cannot import tomllib: " + "; ".join(parts)
                       + ". Lanes run gate_supply decide, macos_fleet_lanes render and "
                       "host_profile with it, and those die on import tomllib", facts)
    unread = [row for row in rows if row.get("tomllib") is None]
    if unread:
        return Finding("lane_python", UNKNOWN, "lane_python_unknown",
                       "; ".join(f"{row.get('python')}: {row.get('error')}" for row in unread),
                       facts)
    return Finding("lane_python", OK, "lane_python_tomllib",
                   "; ".join(f"{row['python']} {row['version']}" for row in rows), facts)


def check_vm_dhcp(value: dict | None) -> Finding:
    """The host's VM-DHCP breaker (scripts/vm_dhcp_breaker.py owns its codes)."""
    import vm_dhcp_breaker  # noqa: PLC0415 - sibling module; owns the codes
    value = value or {"state": "unreadable", "error": "no status"}
    state, code, detail = vm_dhcp_breaker.doctor_code(value)
    return Finding("vm_dhcp", {"ok": OK, "problem": PROBLEM}.get(state, UNKNOWN), code, detail,
                   {"vm_dhcp": value})


def check_vm_boot(value: dict | None, *, lanes: int, now: float | None = None) -> Finding:
    """The host's VM boot success and outage record (vm_dhcp_breaker.boot_health)."""
    import vm_dhcp_breaker  # noqa: PLC0415 - sibling module; owns the codes
    value = value or {"state": "unreadable", "error": "no status"}
    state, code, detail = vm_dhcp_breaker.boot_health(
        value, time.time() if now is None else now, lanes)
    return Finding("vm_boot", {"ok": OK, "problem": PROBLEM,
                               "not_applicable": NOT_APPLICABLE}.get(state, UNKNOWN),
                   code, detail, {"lanes": lanes, "hourly_hours": len(value.get("hourly") or {}),
                                  "outages": (value.get("outages") or [])[-5:]})


def check_peer_reachability(value: dict | None) -> Finding:
    """Peers this host could not read at its last self-update survey."""
    value = value or {"state": "unreadable", "error": "no status", "peers": {}}
    facts = {"peer_reachability": value}
    if value.get("state") == "unreadable":
        return Finding("peer_reachability", UNKNOWN, "peer_reachability_unreadable",
                       f"the unreachable-peer record is unreadable: {value.get('error')}", facts)
    peers = value.get("peers") or {}
    if not peers:
        return Finding("peer_reachability", OK, "peer_reachability_ok",
                       "every peer was readable at the last self-update survey", facts)

    def since(row: dict) -> str:
        ts = row.get("since")
        return (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
                if isinstance(ts, (int, float)) else "?")

    excluded = sorted(p for p, row in peers.items() if row.get("excluded"))
    rows = "; ".join(f"{p} since {since(row)} ({row.get('reads')} reads"
                     + (", excluded from update turns)" if row.get("excluded") else ")")
                     for p, row in sorted(peers.items()))
    if excluded:
        return Finding("peer_reachability", PROBLEM, "peer_unreachable_excluded",
                       f"unreachable peers no longer hold the update turn: {rows}", facts)
    return Finding("peer_reachability", PROBLEM, "peer_unreachable",
                   f"unreachable peers still hold the update turn: {rows}", facts)


def check_power(value: dict | None) -> Finding:
    """Whether the host stays awake on AC (scripts/power_status.py)."""
    import power_status

    value = value or {"state": "unknown"}
    detail = power_status.describe(value)
    facts = {"power": value}
    if value.get("state") == "ok":
        return Finding("power", OK, "power_ok", detail, facts)
    if value.get("state") == "sleeps":
        return Finding("power", PROBLEM, "power_sleeps", detail, facts)
    return Finding("power", UNKNOWN, "power_unknown", detail, facts)


def check_pf_reference(value: dict | None) -> Finding:
    """Whether pf holds an enable reference for the VM network (scripts/pf_reference.py)."""
    import pf_reference  # noqa: PLC0415 - sibling module; owns the states

    value = value or {"state": "unknown"}
    detail = pf_reference.describe(value)
    facts = {"pf_reference": value}
    state = value.get("state")
    if state == "ok":
        return Finding("pf_reference", OK, "pf_reference_ok", detail, facts)
    if state == "no_reference":
        return Finding("pf_reference", PROBLEM, "pf_reference_missing", detail, facts)
    if state == "pfd_exiting":
        return Finding("pf_reference", PROBLEM, "pf_pfd_exiting", detail, facts)
    if state == "holder_missing":
        return Finding("pf_reference", PROBLEM, "pf_boot_holder_missing", detail, facts)
    if state == "not_applicable":
        return Finding("pf_reference", NOT_APPLICABLE, "pf_not_applicable", detail, facts)
    return Finding("pf_reference", UNKNOWN, "pf_reference_unknown", detail, facts)


def check_signing_prompts(value: dict | None, home: Path) -> Finding:
    """Whether the keychain setup can raise a password dialog (signing_prompt_guard.py)."""
    import signing_prompt_guard

    if value is None:
        try:
            value = signing_prompt_guard.status(home)
        except Exception as exc:  # noqa: BLE001 - reported as unknown
            import secret_files
            value = {"state": "unknown", "detail": secret_files.redact(exc, home)}
    facts = {"signing_prompts": value}
    state = value.get("state")
    if state == "not_applicable":
        return Finding("signing_prompts", NOT_APPLICABLE, "signing_prompts_not_applicable",
                       "no dedicated signing keychain configured", facts)
    detail = signing_prompt_guard.describe(value)
    if state == "ok":
        return Finding("signing_prompts", OK, "signing_prompts_ok", detail, facts)
    if state == "risk":
        return Finding("signing_prompts", PROBLEM, "signing_prompts_risk", detail, facts)
    return Finding("signing_prompts", UNKNOWN, "signing_prompts_unknown", detail, facts)


def render(diagnosis: Diagnosis) -> str:
    glyph = {OK: "ok      ", PROBLEM: "PROBLEM ", UNKNOWN: "UNKNOWN ",
             NOT_APPLICABLE: "n/a     "}
    lines = [f"tartci fleet doctor — host: {diagnosis.host}",
             f"overall: {diagnosis.worst.upper()}", ""]
    for finding in diagnosis.findings:
        mark = glyph.get(finding.state, f"{finding.state:<8}")
        lines.append(f"{mark}{finding.check}  [{finding.code}]")
        lines.append(f"          {finding.detail}")
        reason = diagnosis.reasons.get(finding.code)
        if reason:
            if reason.get("why"):
                lines.append(f"          why: {reason['why']}")
            if finding.state in (PROBLEM, UNKNOWN) and reason.get("remedy"):
                lines.append(f"          remedy: {reason['remedy']}")
            if finding.state in (PROBLEM, UNKNOWN) and reason.get("do_not"):
                lines.append(f"          DO NOT: {reason['do_not']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def diagnose(host: str, findings: Iterable[Finding],
             reasons: dict | None = None) -> Diagnosis:
    return Diagnosis(host=host, findings=tuple(findings),
                     reasons=reasons if reasons is not None else load_reasons())


# ── Host collection ────────────────────────────────────────────────────────
#
# Collection reads the host; the checks above stay pure so both cells of every
# test can be written without one.

TOML_PYTHONS = (
    "python3.12", "python3.11", "python3",
    "/opt/homebrew/bin/python3.12", "/opt/homebrew/bin/python3.11",
    "/opt/homebrew/bin/python3", "/usr/local/bin/python3.12",
    "/usr/local/bin/python3.11", "/usr/local/bin/python3",
)


def toml_python() -> str | None:
    """An interpreter with tomllib, which the readiness probe needs.

    macOS ships 3.9, so the probe fails on the stock interpreter. Reporting that
    as "not ready" would invent a fleet fault out of a missing interpreter.
    """
    import shutil

    for candidate in TOML_PYTHONS:
        resolved = shutil.which(candidate) or (
            candidate if Path(candidate).is_file() else None)
        if resolved is None:
            continue
        probe = subprocess.run([resolved, "-c", "import tomllib"],
                               capture_output=True, check=False)
        if probe.returncode == 0:
            return resolved
    return None


def read_pool_records(config_dir: Path) -> tuple[str, str]:
    """Pool participation and state, matching the pool library's own defaults.

    An absent or unparsable participation record means participating: opting a
    host out is an explicit act, and a missing file must never read as opted out.
    """
    participating = "1"
    path = config_dir / "native-build-participation"
    try:
        if path.is_file() and "".join(path.read_text().split()) == "0":
            participating = "0"
    except OSError:
        pass
    state = None
    path = config_dir / "pool-state"
    try:
        if path.is_file():
            value = "".join(path.read_text().split())
            if value in ("on", "draining", "off"):
                state = value
    except OSError:
        pass
    if state is None:
        state = "on" if participating == "1" else "off"
    return participating, state


def readiness_probe(support_root: Path, *, receipt_path: Path, config: Path,
                    agents_dir: Path, participating: str, pool_state: str,
                    python: str | None, timeout: int = 60) -> dict:
    """Ask one support root's own CLI what it thinks readiness is.

    Each tree grades itself with its own script, because that is the instrument
    an operator standing in that tree would actually use.
    """
    if python is None:
        return {"error": "no Python 3.11+ interpreter with tomllib is available"}
    script = support_root / "scripts" / "macos_fleet_lanes.py"
    if not script.is_file():
        return {"error": f"support root carries no readiness script: {script}"}
    argv = [python, str(script), "fleet-readiness", str(receipt_path),
            "--config", str(config), "--agents-dir", str(agents_dir),
            "--support-root", str(support_root),
            "--participating", participating, "--pool-state", pool_state]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"readiness probe did not complete: {exc}"}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        return {"error": detail}
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        return {"error": f"readiness probe emitted no JSON: {exc}"}


def lane_registrations(agents_dir: Path) -> dict[str, dict]:
    """The repo and labels each managed lane registers under, from its own plist.

    The plist is the authority because it is what launchd hands the supervisor.
    A profile says what was intended; only the installed plist says what runs.
    """
    out: dict[str, dict] = {}
    for label in sorted(
            path.name.removesuffix(".plist")
            for path in agents_dir.glob(f"{host_profile.FLEET_LABEL_PREFIX}*.plist")
            if path.is_file() and not path.is_symlink()):
        try:
            value = plistlib.loads((agents_dir / f"{label}.plist").read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            out[label] = {"repo": None, "labels": []}
            continue
        environment = value.get("EnvironmentVariables") or {}
        repo = environment.get("TARTCI_RUNNER_REPO")
        raw = environment.get("TARTCI_RUNNER_LABELS") or ""
        out[label] = {
            "repo": repo if isinstance(repo, str) and repo else None,
            "labels": [part for part in raw.split(",") if part],
        }
    return out


def check_census_identity(cli: str, repo: str, run: Callable[[list[str]], tuple[int, str, str]]
                          | None = None) -> Finding:
    """Would the census CLI reach GitHub as an authenticated identity?

    `<cli> api rate_limit` is free (it does not count against the allowance)
    and its core ceiling names the identity: 60/hour is anonymous. An
    anonymous census spends the fleet's shared per-IP allowance and then
    reports every label as capacity-unknown.
    """
    check = f"census_identity[{repo}]"
    try:
        import runner_census
        env = [f"{k}={v}" for k, v in runner_census.identity_env(repo).items()]
    except Exception as exc:  # noqa: BLE001
        return Finding(check, UNKNOWN, "census_identity_unknown", f"{type(exc).__name__}: {exc}")
    argv = ["/usr/bin/env", *env, cli, "api", "rate_limit"]
    if run is None:
        def run(command: list[str]) -> tuple[int, str, str]:  # noqa: F811
            try:
                proc = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                      check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return 127, "", str(exc)
            return proc.returncode, proc.stdout, proc.stderr
    rc, out, err = run(argv)
    facts = {"cli": cli, "repo": repo}
    try:
        limit = json.loads(out)["resources"]["core"]["limit"]
        if not isinstance(limit, int):
            raise TypeError("limit is not an integer")
    except Exception:  # noqa: BLE001 - no ceiling read means unproven
        return Finding(check, UNKNOWN, "census_identity_unknown",
                       f"`{cli} api rate_limit` gave no core ceiling (exit {rc}): "
                       f"{(err or out).strip()[:200]}", facts)
    facts["core_limit"] = limit
    if limit <= 60:
        return Finding(check, PROBLEM, "census_identity_unauthenticated",
                       f"`{cli}` reaches GitHub ANONYMOUSLY (core limit {limit}/hour) for "
                       f"{repo}: the capacity census would spend the shared per-IP "
                       "allowance and report capacity unknown. Fix: TARTCI_GH_CLI=ghapp, "
                       f"or repair `{cli}` authentication.", facts)
    return Finding(check, OK, "census_identity_authenticated",
                   f"`{cli}` is authenticated for {repo} (core limit {limit}/hour)", facts)


def collect_census(repo: str, gh_cli: str | None) -> tuple[Any, str | None, str]:
    """Take a dual-scope census, consuming the census module when it is present."""
    try:
        import runner_census
    except ImportError:
        return None, "census_module_unavailable", (
            "the dual-scope runner census module is not installed in this "
            "support root, so runner capacity was not measured")
    cli = gh_cli or runner_census.github_cli()
    try:
        fetch = runner_census.cli_fetcher(
            cli, run_json=runner_census._default_run_json)
        return runner_census.collect(repo, fetch), None, ""
    except Exception as exc:  # noqa: BLE001 — any failure is an unread census
        return None, "census_incomplete", f"{type(exc).__name__}: {exc}"


def collect(*, home: Path, agents_dir: Path | None = None,
            config_dir: Path | None = None, support_root: Path = ROOT,
            repos: Sequence[str] | None = None, gh_cli: str | None = None,
            skip_census: bool = False,
            identity_run: Callable[[list[str]], tuple[int, str, str]] | None = None,
            probe: Callable[[Path], dict] | None = None,
            drift_probe: Callable[[Path], tuple[dict | None, str]] | None = None,
            self_update_summary: dict | None = None,
            tool_freshness_summary: dict | None = None,
            supply_check: Callable[[Path], tuple[dict | None, str]] | None = None,
            launchd_run: Callable[[list[str]], tuple[int, str, str]] | None = None,
            reclaim_value: dict | None = None,
            launchd_timers_value: dict | None = None,
            vm_dhcp_value: dict | None = None,
            lane_python_value: dict | None = None,
            peer_reachability_value: dict | None = None,
            support_agents_value: dict | None = None,
            reuse_canary_value: dict | None = None,
            power_value: dict | None = None,
            pf_value: dict | None = None,
            signing_prompts_value: dict | None = None,
            tmp_worktrees_probe: Callable[[Path], tuple[dict | None, str]] | None = None,
            ) -> list[Finding]:
    """Run every check against this host."""
    agents_dir = agents_dir or (home / "Library" / "LaunchAgents")
    config_dir = config_dir or (home / ".config" / "tartci")
    readable = agents_dir.is_dir()
    # One delivery classification per run, shared by both generation checks, so
    # they cannot disagree about what a lane execs.
    delivery = host_profile.build_delivery_report(
        agents=agents_dir, repo_root=support_root)

    receipt_path = config_dir / "macos-fleet-install.json"
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError):
        receipt = None
    installed = installed_generation(receipt)

    findings = [
        check_executed_generation(delivery, installed),
        check_generation_delivery(delivery),
        check_drain_capability(
            agents_dir, config_dir / "persistent-runner-admission-hold",
            agents_dir_readable=readable),
    ]

    participating, pool_state = read_pool_records(config_dir)
    config = config_dir / "macos-fleet-profile.toml"
    installed_root = Path(installed["root"]) if installed and installed.get("root") \
        else None
    python = toml_python() if probe is None or drift_probe is None else None
    if config.is_file():
        drift_result, drift_error = (
            drift_probe(config) if drift_probe is not None
            else profile_drift_probe(support_root, config, python))
        findings.append(check_profile_drift(
            drift_result, installed_present=True, error=drift_error))
    else:
        findings.append(check_profile_drift(None, installed_present=False))
    if config.is_file():
        supply_result, supply_error = (
            supply_check(config) if supply_check is not None
            else supply_probe(support_root, config, python or toml_python()))
        findings.append(check_supply(
            supply_result, installed_present=True, error=supply_error))
    else:
        findings.append(check_supply(None, installed_present=False))
    if self_update_summary is None:
        try:
            import fleet_self_update
            self_update_summary = fleet_self_update.summary(home)
        except Exception:  # noqa: BLE001 - reported as unmeasured
            self_update_summary = None
    findings.append(check_self_update(self_update_summary))
    if config.is_file():
        try:
            import macos_fleet_lanes
            reserve_value = macos_fleet_lanes.gate_reserve_summary(config)
        except Exception as exc:  # noqa: BLE001 - reported as unknown
            reserve_value = {"lines": [f"gate reserve: UNKNOWN ({exc})"], "problem": None}
    else:
        reserve_value = None
    findings.append(check_gate_reserve(reserve_value, installed_present=config.is_file()))
    if tool_freshness_summary is None:
        try:
            import tool_freshness
            tool_freshness_summary = tool_freshness.summary(home)
        except Exception:  # noqa: BLE001 - reported as unmeasured
            tool_freshness_summary = None
    findings.append(check_tool_freshness(tool_freshness_summary))
    import lease_fit
    if readable:
        fit_records, fit_missing = lease_fit.lane_records(
            agents_dir, host_profile.FLEET_LABEL_PREFIX)
        managed = bool(fit_records or fit_missing)
    else:
        fit_records, fit_missing, managed = [], [], False
    findings.append(check_lease_fit(fit_records, fit_missing, managed=managed))
    if lane_python_value is None:
        if readable:
            try:
                import lane_python
                lane_python_value = lane_python.status(
                    agents_dir, host_profile.FLEET_LABEL_PREFIX)
            except Exception as exc:  # noqa: BLE001 - reported as unprobed
                lane_python_value = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            lane_python_value = {"error": "agents directory unreadable"}
    findings.append(check_lane_python(lane_python_value))
    try:
        import home_volume_floor
        import leases
        home_value = home_volume_floor.status(leases.default_store_dir())
    except Exception:  # noqa: BLE001 - an unreadable state reads as not judged
        home_value = {}
    findings.append(check_home_volume(home_value, lanes=len(fit_records)))
    try:
        import warm_vm_status
        warm_value = warm_vm_status.status(home / ".tartci/state/warm-vm")
    except Exception as exc:  # noqa: BLE001 - reported as unreadable
        warm_value = {"state": "unreadable", "error": str(exc)}
    findings.append(check_warm_vm(warm_value))
    rows, launchd_error = launchd_registrations(launchd_run)
    findings.append(check_launchd_registrations(rows, home=home, error=launchd_error))
    findings.append(check_host_agents(rows, managed=config.is_file(), error=launchd_error))
    if reclaim_value is None:
        try:
            import reclaim_status
            # An explicit TARTCI_RECLAIM_STATE_DIR wins, as it does for the
            # reclaim pass that writes the receipt; otherwise read `home`'s.
            reclaim_value = reclaim_status.status(
                None if os.environ.get("TARTCI_RECLAIM_STATE_DIR")
                else home / ".tartci" / "state" / "reclaim",
                log_path=home / "Library" / "Logs" / "tartci" / "tartci-reclaim.log")
        except Exception as exc:  # noqa: BLE001 - reported as unreadable
            reclaim_value = {"state": "unreadable", "error": str(exc)}
    findings.append(check_reclaim(reclaim_value))
    if launchd_timers_value is None:
        try:
            import launchd_interval_guard
            launchd_timers_value = launchd_interval_guard.status(
                None if os.environ.get("TARTCI_INTERVAL_GUARD_DIR")
                else home / ".tartci" / "state" / "launchd-interval-guard")
        except Exception as exc:  # noqa: BLE001 - reported as unreadable
            launchd_timers_value = {"state": "unreadable", "error": str(exc)}
    findings.append(check_launchd_timers(launchd_timers_value))
    if vm_dhcp_value is None:
        try:
            import vm_dhcp_breaker
            vm_dhcp_value = vm_dhcp_breaker.status(home / ".tartci" / "state" / "vm-dhcp")
        except Exception as exc:  # noqa: BLE001 - reported as unreadable
            vm_dhcp_value = {"state": "unreadable", "error": str(exc)}
    findings.append(check_vm_dhcp(vm_dhcp_value))
    findings.append(check_vm_boot(vm_dhcp_value, lanes=len(fit_records)))
    if peer_reachability_value is None:
        try:
            import fleet_self_update
            peer_reachability_value = fleet_self_update.peer_reachability(home)
        except Exception as exc:  # noqa: BLE001 - reported as unreadable
            peer_reachability_value = {"state": "unreadable", "error": str(exc), "peers": {}}
    findings.append(check_peer_reachability(peer_reachability_value))
    if support_agents_value is None:
        try:
            import support_agents
            support_agents_value = support_agents.status(
                home / ".tartci" / "state" / "support-agents")
        except Exception as exc:  # noqa: BLE001 - reported as unreadable
            support_agents_value = {"state": "unreadable", "error": str(exc)}
    findings.extend(check_support_agents(support_agents_value))
    findings.append(check_vm_janitor(support_agents_value))
    if reuse_canary_value is None:
        try:
            import reuse_canary
            settings, _ = reuse_canary.load_settings(config_dir / "macos-fleet-profile.toml")
            reuse_canary_value = reuse_canary.status(
                home / ".tartci" / "state" / "reuse-canary", settings,
                plist=agents_dir / f"{reuse_canary.LABEL}.plist")
        except Exception as exc:  # noqa: BLE001 - reported as unreadable
            reuse_canary_value = {"state": "unreadable", "error": str(exc)}
    findings.append(check_reuse_canary(reuse_canary_value))
    if power_value is None:
        try:
            import power_status
            power_value = power_status.status()
        except Exception as exc:  # noqa: BLE001 - reported as unknown
            power_value = {"state": "unknown", "error": str(exc)}
    findings.append(check_power(power_value))
    if pf_value is None:
        try:
            import pf_reference
            pf_value = pf_reference.status(len(fit_records), vm_dhcp_value)
        except Exception as exc:  # noqa: BLE001 - reported as unknown
            pf_value = {"state": "unknown", "error": str(exc)}
    findings.append(check_pf_reference(pf_value))
    findings.append(check_signing_prompts(signing_prompts_value, home))

    def default_tmp_probe(profile: Path) -> tuple[dict | None, str]:
        import pulp_reapers
        settings, why = pulp_reapers.load_settings(profile)
        if settings is None:
            return None, why
        return pulp_reapers.tmp_worktrees(Path(settings["repo"])), ""
    try:
        tmp_value, tmp_reason = (tmp_worktrees_probe or default_tmp_probe)(config)
    except Exception as exc:  # noqa: BLE001 - reported as not checked
        tmp_value, tmp_reason = {"count": None, "error": str(exc)}, ""
    findings.append(check_worktrees_in_tmp(tmp_value, reason=tmp_reason))
    if probe is None:

        def probe(root: Path) -> dict:  # noqa: F811 — the host-reading default
            return readiness_probe(
                root, receipt_path=receipt_path, config=config,
                agents_dir=agents_dir, participating=participating,
                pool_state=pool_state, python=python)

    roots: list[Path] = []
    if installed_root is not None:
        roots.append(installed_root)
    if support_root.resolve() not in {root.resolve() for root in roots}:
        roots.append(support_root)
    authority = str(roots[0]) if roots else str(support_root)
    findings.append(check_readiness(
        {str(root): probe(root) for root in roots}, authority=authority))

    if not skip_census:
        registrations = lane_registrations(agents_dir) if readable else {}
        wanted = list(repos) if repos else sorted(
            {row["repo"] for row in registrations.values() if row["repo"]})
        if not wanted:
            findings.append(Finding(
                "runner_census", UNKNOWN, "census_repo_unknown",
                "no managed lane declares a runner repository, so no census "
                "target could be derived",
                {"idle_zero_is_normal": True, "note": IDLE_ZERO_NOTE}))
        for repo in wanted:
            try:
                import runner_census
                cli = gh_cli or runner_census.github_cli()
            except ImportError:
                cli = gh_cli or "gh"
            findings.append(check_census_identity(cli, repo, identity_run))
            census, code, detail = collect_census(repo, gh_cli)
            findings.append(check_runner_census(
                census, repo=repo, error_code=code, error_detail=detail))
    return findings


def main(argv: list[str] | None = None) -> int:
    import argparse
    import platform

    parser = argparse.ArgumentParser(
        prog="tartci doctor fleet",
        description="Diagnose how this fleet host is configured and whether it works.")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--repo", action="append", default=[],
                        help="census this OWNER/REPO (default: every managed lane's)")
    parser.add_argument("--gh-cli", default="", help="GitHub CLI wrapper")
    parser.add_argument("--no-census", action="store_true",
                        help="skip the runner census (no GitHub API calls)")
    parser.add_argument("--home", default="", help="host home directory to inspect")
    args = parser.parse_args(argv)

    home = Path(args.home) if args.home else Path.home()
    findings = collect(home=home, repos=args.repo or None,
                       gh_cli=args.gh_cli or None, skip_census=args.no_census)
    diagnosis = diagnose(platform.node(), findings)
    if args.json:
        print(json.dumps(diagnosis.as_dict(), indent=2, sort_keys=True))
    else:
        print(render(diagnosis), end="")
    return diagnosis.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
