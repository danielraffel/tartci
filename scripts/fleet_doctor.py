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

FLEET_LABEL_PREFIX = "com.danielraffel.tartci.tart-runner-macos-fleet."
PERSISTENT_LABEL_PREFIX = "actions.runner."
SEALED_METADATA = "Contents/Resources/bundle.json"
SEALED_SUPPORT = "Contents/Resources/support"
LAUNCH_NAME = ".tartci-launch"
HELD_IDLE = "held-idle"
REASONS_PATH = Path(__file__).resolve().parent / "fleet_reasons.json"

# Stable reason codes. Each one must carry a row in fleet_reasons.json saying
# why the state exists and what to do about it; a code without a row is a code
# whose meaning lives only in whoever wrote it.
CODES: tuple[str, ...] = (
    "agents_dir_unreadable",
    "census_complete",
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
    "no_persistent_runners",
    "persistent_runners_without_hold_receipt",
    "program_unresolvable",
    "readiness_not_managed",
    "readiness_probe_failed",
    "readiness_verdict_depends_on_invocation",
    "sealed_launcher_bundle",
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
# never what an installer most recently staged. The two diverge silently: a
# signed launcher bundle execs its own sealed copy of the support cohort, so
# staging a newer generation beside it changes nothing that runs.


def fleet_labels(agents_dir: Path) -> list[str]:
    """Managed fleet LaunchAgent labels installed on this host."""
    return sorted(
        path.name.removesuffix(".plist")
        for path in agents_dir.glob(f"{FLEET_LABEL_PREFIX}*.plist")
        if path.is_file() and not path.is_symlink()
    )


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


def sealed_bundle_root(program: Path) -> Path | None:
    """The app bundle whose sealed cohort `program` would execute, if any."""
    parents = program.parents
    if len(parents) < 3:
        return None
    if parents[0].name != "MacOS" or parents[1].name != "Contents":
        return None
    bundle = parents[2]
    return bundle if bundle.suffix == ".app" else None


def resolve_exec(plist_path: Path) -> dict:
    """Resolve one LaunchAgent to the cohort identity it actually executes.

    Identity is the (source_commit, support_manifest_sha256) pair the executed
    cohort carries, not a directory name. Generation directories are named from
    that pair today, but deriving identity from a naming convention would make
    this report a mismatch the moment the convention moved.
    """
    try:
        value = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        return {"kind": "unresolved", "detail": f"unreadable LaunchAgent: {exc}"}
    arguments = value.get("ProgramArguments")
    if (not isinstance(arguments, list) or not arguments
            or not isinstance(arguments[0], str) or not arguments[0]):
        return {"kind": "unresolved", "detail": "LaunchAgent declares no program"}
    program = Path(arguments[0])
    bundle = sealed_bundle_root(program)
    if bundle is None:
        return {"kind": "generation-path", "program": str(program),
                "launch_entrypoint": str(program)}
    sealed_launch = bundle / SEALED_SUPPORT / LAUNCH_NAME
    try:
        metadata = json.loads((bundle / SEALED_METADATA).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"kind": "unresolved", "program": str(program),
                "bundle": str(bundle),
                "detail": f"sealed cohort metadata is unreadable: {exc}"}
    if not isinstance(metadata, dict):
        return {"kind": "unresolved", "program": str(program),
                "bundle": str(bundle),
                "detail": "sealed cohort metadata is malformed"}
    return {
        "kind": "sealed-bundle",
        "program": str(program),
        "bundle": str(bundle),
        "launch_entrypoint": str(sealed_launch),
        "source_commit": str(metadata.get("source_commit", "")) or None,
        "support_manifest_sha256": str(
            metadata.get("support_manifest_sha256", "")) or None,
    }


def exec_map(agents_dir: Path) -> dict[str, dict]:
    """Resolve every managed fleet LaunchAgent once, for the checks that share it."""
    return {
        label: resolve_exec(agents_dir / f"{label}.plist")
        for label in fleet_labels(agents_dir)
    }


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


def _executes_installed(resolved: dict, installed: dict) -> bool | None:
    """Does this agent execute the installed cohort? None when unresolvable."""
    if resolved["kind"] == "unresolved":
        return None
    if resolved["kind"] == "sealed-bundle":
        commit = resolved.get("source_commit")
        manifest = resolved.get("support_manifest_sha256")
        if commit is None or manifest is None:
            return None
        return (commit == installed["source_commit"]
                and manifest == installed["support_manifest_sha256"])
    # A direct exec names the generation's own launch entrypoint, so the
    # receipt's recorded path is an exact comparison with no convention in it.
    expected = installed.get("launch_entrypoint")
    if not expected:
        return None
    return resolved["launch_entrypoint"] == expected


def check_executed_generation(execs: dict[str, dict],
                              installed: dict | None,
                              *, agents_dir_readable: bool = True) -> Finding:
    """Report the cohort the host EXECUTES against the one it records as installed."""
    check = "executed_generation"
    if not agents_dir_readable:
        return Finding(check, UNKNOWN, "agents_dir_unreadable",
                       "the LaunchAgent directory could not be listed")
    if not execs and installed is None:
        return Finding(check, NOT_APPLICABLE, "no_managed_launchagents",
                       "this host installs no managed fleet LaunchAgents and "
                       "holds no install receipt")
    if installed is None:
        return Finding(
            check, UNKNOWN, "installed_generation_unknown",
            "managed LaunchAgents are installed but no readable install receipt "
            "says which cohort they should execute",
            {"labels": sorted(execs)})
    if not execs:
        return Finding(
            check, PROBLEM, "no_managed_launchagents",
            "an install receipt records a cohort but no managed fleet "
            "LaunchAgent is installed to execute it",
            {"installed_generation": installed})

    effective: dict[str, dict] = {}
    mismatched: list[str] = []
    unresolved: list[str] = []
    for label, resolved in sorted(execs.items()):
        verdict = _executes_installed(resolved, installed)
        effective[label] = {
            "kind": resolved["kind"],
            "launch_entrypoint": resolved.get("launch_entrypoint"),
            "source_commit": resolved.get("source_commit"),
            "support_manifest_sha256": resolved.get("support_manifest_sha256"),
            "executes_installed": verdict,
            "detail": resolved.get("detail"),
        }
        if verdict is None:
            unresolved.append(label)
        elif not verdict:
            mismatched.append(label)

    facts = {"installed_generation": installed, "effective_generation": effective}
    if mismatched:
        return Finding(
            check, PROBLEM, "effective_generation_mismatch",
            "the generation this host EXECUTES is not the generation it records "
            f"as installed ({len(mismatched)} of {len(execs)} agents): "
            + ", ".join(mismatched),
            facts)
    if unresolved:
        return Finding(
            check, UNKNOWN, "program_unresolvable",
            "the executed generation could not be resolved for: "
            + ", ".join(unresolved),
            facts)
    return Finding(check, OK, "effective_generation_matches",
                   f"all {len(execs)} managed agents execute the installed cohort",
                   facts)


def check_generation_delivery(execs: dict[str, dict],
                              *, agents_dir_readable: bool = True) -> Finding:
    """Report whether staging a generation can change what this host executes.

    A host whose agents exec a sealed launcher bundle cannot be updated by a
    generation stage at all: the stage succeeds, the receipt is written, and the
    bundle keeps executing its own sealed copy. Answering this before a deploy
    is the difference between a no-op nobody notices and a deploy nobody starts.
    """
    check = "generation_delivery"
    if not agents_dir_readable:
        return Finding(check, UNKNOWN, "agents_dir_unreadable",
                       "the LaunchAgent directory could not be listed",
                       {"can_receive_generation": None})
    if not execs:
        return Finding(check, NOT_APPLICABLE, "no_managed_launchagents",
                       "this host installs no managed fleet LaunchAgents",
                       {"can_receive_generation": None})
    sealed = sorted(l for l, r in execs.items() if r["kind"] == "sealed-bundle")
    unresolved = sorted(l for l, r in execs.items() if r["kind"] == "unresolved")
    if unresolved:
        return Finding(
            check, UNKNOWN, "delivery_unknown",
            "some agents declare no resolvable program, so it is not known "
            "whether a staged generation would reach them: " + ", ".join(unresolved),
            {"can_receive_generation": None, "unresolved": unresolved,
             "sealed_bundle_agents": sealed})
    if sealed:
        bundles = sorted({execs[label]["bundle"] for label in sealed})
        return Finding(
            check, PROBLEM, "sealed_launcher_bundle",
            "this host executes a sealed launcher bundle, so a generation stage "
            "alone cannot change what it runs: " + ", ".join(bundles),
            {"can_receive_generation": False, "sealed_bundle_agents": sealed,
             "bundles": bundles})
    return Finding(
        check, OK, "generation_path_exec",
        f"all {len(execs)} managed agents exec a generation path directly, so a "
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
    for label in fleet_labels(agents_dir):
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
            probe: Callable[[Path], dict] | None = None) -> list[Finding]:
    """Run every check against this host."""
    agents_dir = agents_dir or (home / "Library" / "LaunchAgents")
    config_dir = config_dir or (home / ".config" / "tartci")
    readable = agents_dir.is_dir()
    execs = exec_map(agents_dir) if readable else {}

    receipt_path = config_dir / "macos-fleet-install.json"
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError):
        receipt = None
    installed = installed_generation(receipt)

    findings = [
        check_executed_generation(execs, installed, agents_dir_readable=readable),
        check_generation_delivery(execs, agents_dir_readable=readable),
        check_drain_capability(
            agents_dir, config_dir / "persistent-runner-admission-hold",
            agents_dir_readable=readable),
    ]

    participating, pool_state = read_pool_records(config_dir)
    config = config_dir / "macos-fleet-profile.toml"
    installed_root = Path(installed["root"]) if installed and installed.get("root") \
        else None
    if probe is None:
        python = toml_python()

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
