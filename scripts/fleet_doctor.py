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
import plistlib
import subprocess

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
    "effective_generation_matches",
    "effective_generation_mismatch",
    "fleet_not_ready",
    "fleet_ready",
    "generation_path_exec",
    "hold_receipt_malformed",
    "hold_receipt_present",
    "installed_generation_unknown",
    "no_managed_launchagents",
    "no_installed_profile",
    "no_persistent_runners",
    "persistent_runners_without_hold_receipt",
    "profile_drift",
    "profile_drift_unknown",
    "profile_in_sync",
    "program_unresolvable",
    "readiness_not_managed",
    "readiness_probe_failed",
    "readiness_verdict_depends_on_invocation",
    "sealed_launcher_bundle",
    "self_update_current",
    "self_update_problem",
    "self_update_unmeasured",
    "supply_match",
    "supply_mismatch",
    "supply_unknown",
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
                "registered": len(scope.runners) if scope.reachable else None,
                "error": scope.error or None,
            }
            for scope in census.scopes
        }
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
        f"{len(scopes)} scope(s), {facts['online']} online. {IDLE_ZERO_NOTE}",
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


def check_self_update(summary: dict | None) -> Finding:
    """tartci's own skew against main and the last self-update attempt."""
    if not isinstance(summary, dict) or summary.get("skew") is None:
        return Finding("self_update", UNKNOWN, "self_update_unmeasured",
                       "tartci's skew against main was never measured on this host "
                       "(run `tartci fleet-macos self-update --plan`)")
    lines = "; ".join(summary.get("lines") or [])
    if summary.get("problem"):
        return Finding("self_update", PROBLEM, "self_update_problem",
                       f"{summary['problem']} ({lines})", {"skew": summary.get("skew"),
                                                          "last": summary.get("last")})
    return Finding("self_update", OK, "self_update_current", lines,
                   {"skew": summary.get("skew"), "last": summary.get("last")})


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
            supply_check: Callable[[Path], tuple[dict | None, str]] | None = None,
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
