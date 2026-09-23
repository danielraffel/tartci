#!/usr/bin/env python3
"""Refuse a pool mutation that would take the last capacity for a required
label offline.

`tartci pool drain` and `tartci pool off` stop this host from serving. When
this host holds the only runners carrying a required gate label, that mutation
takes the label to zero runners and every pull request waiting on that gate
stalls until someone notices. Nothing about the mutation looks wrong while it
happens: the host reports a clean drain, and the damage is visible only in the
repository's queue.

The guard answers one question first: does a machine other than this one serve
each required label this host declares? A label another host still serves is
free to drain. A label only this host serves refuses, naming the label and the
host, and proceeds only under an explicit `--allow-last-serving-host`.

Every indeterminate answer refuses. An unreachable runner scope, a host with no
resolvable identity, and a persistent runner whose registered name cannot be
derived are all cases where "another host is serving" and "nobody is" look
identical from here, so the guard treats them as the dangerous one.

Which labels are required: the gate-class labels a fleet lane declares in its
`[[lane.tier]]` rows, plus any `--required-label` passed by the caller. The
base capability labels a lane carries (`self-hosted`, `macOS`, …) are not
gate classes and are not protected here.

Who owns a runner: fleet registrations are named `<host-id>-<lane>-<slot>` and
their per-boot registrations extend that name, so a registration carrying this
host's identity prefix belongs to this host and one that does not belongs to
another machine. Persistent Actions services are named by their launchd label
(`actions.runner.<owner>-<repo>.<runner-name>`) and are matched by that name.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runner_census
from runner_census import RunnerCensus, RunnerRecord

DEFAULT_PROFILE = "~/.config/tartci/macos-fleet-profile.toml"

# Verdicts a single (repo, label) pair can reach.
SERVED_ELSEWHERE = "served_elsewhere"
LAST_SERVING_HOST = "last_serving_host"
CAPACITY_UNKNOWN = "capacity_unknown"

# Named refusal reasons. Stable strings: operators and tests grep them.
REASON_LAST_SERVING_HOST = "last_serving_host"
REASON_CAPACITY_UNKNOWN = "capacity_unknown"
REASON_HOST_IDENTITY_UNKNOWN = "host_identity_unknown"
REASON_PERSISTENT_RUNNER_NAME_UNKNOWN = "persistent_runner_name_unknown"
REASON_PROFILE_UNREADABLE = "profile_unreadable"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_LAST_SERVING_HOST = 3
EXIT_INDETERMINATE = 4

_PERSISTENT_LABEL = re.compile(r"^actions\.runner\.[A-Za-z0-9_.-]+\.(?P<name>[A-Za-z0-9_.-]+)$")


class GuardError(RuntimeError):
    """A refusal that is decided before any census is attempted."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ── profile reading ─────────────────────────────────────────────────────────


def load_profile(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - interpreter floor
        raise GuardError(
            REASON_PROFILE_UNREADABLE,
            "reading the fleet profile requires Python 3.11+ with tomllib; set TARTCI_PYTHON",
        ) from exc
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except Exception as exc:  # noqa: BLE001
        raise GuardError(
            REASON_PROFILE_UNREADABLE, f"fleet profile {path} could not be parsed: {exc}"
        ) from exc


def host_identity(profile: dict, *, override: str = "", hostname: str = "") -> str:
    if override:
        return override
    host = profile.get("host")
    if isinstance(host, dict):
        value = host.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    if profile:
        raise GuardError(
            REASON_HOST_IDENTITY_UNKNOWN,
            "fleet profile declares no host.id, so this host's own runner "
            "registrations cannot be told apart from another host's",
        )
    resolved = (hostname or socket.gethostname() or "").split(".")[0].strip()
    if not resolved:
        raise GuardError(
            REASON_HOST_IDENTITY_UNKNOWN,
            "this host has no resolvable identity, so its own runner "
            "registrations cannot be told apart from another host's",
        )
    return resolved


def lanes(profile: dict) -> list[dict]:
    rows = profile.get("lane")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def owned_name_prefixes(profile: dict, host_id: str) -> tuple[str, ...]:
    """Registration-name prefixes this host mints, longest identity first."""
    prefixes = {host_id}
    for lane in lanes(profile):
        lane_id = lane.get("id")
        if not isinstance(lane_id, str) or not lane_id:
            continue
        supervisors = lane.get("supervisors", 1)
        supervisors = supervisors if isinstance(supervisors, int) and supervisors > 0 else 1
        for slot in range(1, supervisors + 1):
            identity = lane_id if slot == 1 else f"{lane_id}-slot{slot}"
            prefixes.add(f"{host_id}-{identity}")
    return tuple(sorted(prefixes))


def persistent_runner_names(profile: dict) -> tuple[str, ...]:
    """Registered names of this host's persistent Actions services.

    The launchd label carries the registered runner name as its final
    component. A label that does not carry one leaves a registration this host
    owns unattributable, which would let another of this host's own runners be
    counted as somebody else's capacity — so it refuses instead.
    """
    host = profile.get("host")
    declared = host.get("persistent_runner_labels") if isinstance(host, dict) else None
    if not declared:
        return ()
    if not isinstance(declared, list):
        raise GuardError(
            REASON_PERSISTENT_RUNNER_NAME_UNKNOWN,
            "host.persistent_runner_labels is not a list, so this host's "
            "persistent runner registrations cannot be identified",
        )
    names: list[str] = []
    for label in declared:
        match = _PERSISTENT_LABEL.fullmatch(label) if isinstance(label, str) else None
        if match is None:
            raise GuardError(
                REASON_PERSISTENT_RUNNER_NAME_UNKNOWN,
                f"persistent runner label {label!r} does not carry a registered "
                "runner name, so this host's own registration cannot be identified",
            )
        names.append(match.group("name"))
    return tuple(names)


@dataclass(frozen=True)
class Protected:
    repo: str
    label: str


def protected_labels(profile: dict, extra: Sequence[str] = ()) -> tuple[Protected, ...]:
    """Gate-class labels this host declares, plus caller-supplied ones.

    A `--required-label` value is `LABEL` (protected in every repository this
    host declares a lane for) or `REPO=LABEL` for one repository.
    """
    found: list[Protected] = []
    seen: set[tuple[str, str]] = set()

    def add(repo: str, label: str) -> None:
        key = (repo, label)
        if key in seen:
            return
        seen.add(key)
        found.append(Protected(repo=repo, label=label))

    lane_repos: list[str] = []
    for lane in lanes(profile):
        repo = lane.get("repo")
        if not isinstance(repo, str) or not repo:
            continue
        if repo not in lane_repos:
            lane_repos.append(repo)
        for tier in lane.get("tier") or []:
            if not isinstance(tier, dict):
                continue
            label = tier.get("label")
            if isinstance(label, str) and label:
                add(repo, label)

    for value in extra:
        text = value.strip()
        if not text:
            continue
        if "=" in text:
            repo, _, label = text.partition("=")
            repo, label = repo.strip(), label.strip()
            if not repo or not label:
                raise GuardError(
                    REASON_CAPACITY_UNKNOWN, f"malformed required label: {value!r}"
                )
            add(repo, label)
            continue
        if not lane_repos:
            raise GuardError(
                REASON_CAPACITY_UNKNOWN,
                f"required label {text!r} names no repository and this host "
                "declares no lane; pass it as REPO=LABEL",
            )
        for repo in lane_repos:
            add(repo, text)
    return tuple(found)


def owner_matcher(
    host_id: str, prefixes: Sequence[str], persistent: Sequence[str]
) -> Callable[[RunnerRecord], bool]:
    """A registration belongs to this host when it carries this host's identity."""
    owned_exact = {host_id, *prefixes, *persistent}

    def owned(record: RunnerRecord) -> bool:
        name = record.name
        if not name:
            return False
        if name in owned_exact:
            return True
        return any(name.startswith(f"{prefix}-") for prefix in owned_exact)

    return owned


# ── decision core (pure; no I/O) ────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    repo: str
    label: str
    verdict: str
    remaining: tuple[str, ...] = ()
    owned_here: tuple[str, ...] = ()
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "label": self.label,
            "verdict": self.verdict,
            "remaining": list(self.remaining),
            "owned_here": list(self.owned_here),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    message: str = ""
    overridden: bool = False
    host: str = ""
    action: str = ""
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    # Why a census could not answer, when it names an identity fault.
    census_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "census_reason": self.census_reason,
            "message": self.message,
            "overridden": self.overridden,
            "host": self.host,
            "action": self.action,
            "findings": [finding.as_dict() for finding in self.findings],
        }

    def exit_code(self) -> int:
        if self.allowed:
            return EXIT_OK
        if self.reason == REASON_LAST_SERVING_HOST:
            return EXIT_LAST_SERVING_HOST
        return EXIT_INDETERMINATE


def classify(
    *,
    host: str,
    action: str,
    protected: Sequence[Protected],
    censuses: dict[str, RunnerCensus],
    owned: Callable[[RunnerRecord], bool],
    allow_last_serving_host: bool = False,
) -> Decision:
    """Decide whether `action` may proceed on `host`.

    Capacity that survives the mutation is an online runner carrying the label
    that this host does not own. Offline registrations do not count: a drained
    or disconnected peer serves nothing.
    """
    findings: list[Finding] = []
    unknown: Finding | None = None
    last: Finding | None = None

    for entry in protected:
        census = censuses.get(entry.repo)
        if census is None:
            finding = Finding(
                entry.repo,
                entry.label,
                CAPACITY_UNKNOWN,
                detail=f"no runner census was collected for {entry.repo}",
            )
            findings.append(finding)
            unknown = unknown or finding
            continue
        owned_here = tuple(
            record.name for record in census.runners if record.carries(entry.label) and owned(record)
        )
        status = runner_census.label_status(census, entry.label, exclude=owned)
        remaining = tuple(record.name for record in status.online)
        if status.status == runner_census.SERVED:
            findings.append(
                Finding(entry.repo, entry.label, SERVED_ELSEWHERE, remaining, owned_here)
            )
            continue
        if status.status == runner_census.UNKNOWN:
            finding = Finding(
                entry.repo,
                entry.label,
                CAPACITY_UNKNOWN,
                remaining,
                owned_here,
                detail=status.detail or "runner census incomplete",
            )
            findings.append(finding)
            unknown = unknown or finding
            continue
        offline = tuple(record.name for record in status.offline)
        if offline:
            detail = (
                "every other registration carrying this label is offline: "
                + ", ".join(offline)
            )
        elif owned_here:
            detail = (
                "the only registrations carrying this label are this host's: "
                + ", ".join(owned_here)
            )
        else:
            detail = (
                "no host currently registers a runner carrying this label, and "
                "this host is the one declaring it"
            )
        finding = Finding(entry.repo, entry.label, LAST_SERVING_HOST, remaining, owned_here, detail)
        findings.append(finding)
        last = last or finding

    if unknown is not None:
        census_reason = next(
            (code for code in (runner_census.CENSUS_UNAUTHENTICATED,
                               runner_census.CENSUS_IDENTITY_LACKS_ACCESS)
             if code in unknown.detail), "")
        return Decision(
            allowed=False,
            reason=REASON_CAPACITY_UNKNOWN,
            census_reason=census_reason,
            message=(
                f"refusing pool {action}: capacity for required label "
                f"'{unknown.label}' ({unknown.repo}) could not be determined"
                f"{' [' + census_reason + ']' if census_reason else ''}: "
                f"{unknown.detail}. --allow-last-serving-host does NOT override "
                "an unknown answer: it accepts a known zero, not an unread one. "
                "Fix the census (see the reason above) and re-run."
            ),
            host=host,
            action=action,
            findings=tuple(findings),
        )
    if last is not None:
        message = (
            f"refusing pool {action}: no host other than {host} serves required "
            f"label '{last.label}' ({last.repo}), so {action} leaves it at zero "
            f"runners — {last.detail}. Pass --allow-last-serving-host to take it "
            f"to zero deliberately."
        )
        if allow_last_serving_host:
            return Decision(
                allowed=True,
                reason=REASON_LAST_SERVING_HOST,
                message=(
                    f"proceeding with pool {action} under --allow-last-serving-host: "
                    f"no host other than {host} serves required label "
                    f"'{last.label}' ({last.repo}), which goes to zero runners"
                ),
                overridden=True,
                host=host,
                action=action,
                findings=tuple(findings),
            )
        return Decision(
            allowed=False,
            reason=REASON_LAST_SERVING_HOST,
            message=message,
            host=host,
            action=action,
            findings=tuple(findings),
        )

    served = [finding for finding in findings if finding.verdict == SERVED_ELSEWHERE]
    if served:
        message = (
            f"pool {action} keeps every required label served: "
            + "; ".join(
                f"{finding.label} ({finding.repo}) by {', '.join(finding.remaining)}"
                for finding in served
            )
        )
    else:
        message = f"pool {action} protects no required label: this host declares none"
    return Decision(
        allowed=True, message=message, host=host, action=action, findings=tuple(findings)
    )


# ── I/O layer ───────────────────────────────────────────────────────────────


def _bounded_run_json(cli_timeout: float) -> Callable[[list[str]], Any]:
    from bounded_subprocess import ObservationError, require_success, run_bounded

    def run_json(argv: list[str]) -> Any:
        proc = require_success(
            run_bounded(argv, timeout=cli_timeout, operation="runner_census"),
            operation="runner_census",
        )
        try:
            return json.loads(proc.stdout or "null")
        except json.JSONDecodeError as exc:
            raise ObservationError("runner_census", "invalid_json", str(exc)) from exc

    return run_json


def collect_censuses(
    repos: Iterable[str], *, gh_cli: str, timeout: float
) -> dict[str, RunnerCensus]:
    fetch = runner_census.cli_fetcher(gh_cli, run_json=_bounded_run_json(timeout))
    return {repo: runner_census.collect(repo, fetch) for repo in dict.fromkeys(repos)}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refuse a pool mutation that takes the last required-label capacity offline."
    )
    parser.add_argument("command", choices=["check"], nargs="?", default="check")
    parser.add_argument("--action", default="drain", help="the mutation being guarded (drain/off)")
    parser.add_argument("--config", default=DEFAULT_PROFILE, help="fleet profile path")
    parser.add_argument("--host-id", default="", help="override this host's identity")
    parser.add_argument(
        "--required-label",
        action="append",
        default=[],
        metavar="[REPO=]LABEL",
        help="protect this label in addition to the profile's gate-class labels",
    )
    parser.add_argument("--gh-cli", default="", help="GitHub CLI wrapper (default: $TARTCI_GH_CLI)")
    parser.add_argument("--timeout", type=float, default=15.0, help="per-scope API timeout")
    parser.add_argument(
        "--allow-last-serving-host",
        action="store_true",
        help="proceed even when this host is the last one serving a required label",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> Decision:
    profile = load_profile(Path(os.path.expanduser(args.config)))
    host = host_identity(profile, override=args.host_id)
    protected = protected_labels(profile, args.required_label)
    if not protected:
        return Decision(
            allowed=True,
            message=f"pool {args.action} protects no required label: this host declares none",
            host=host,
            action=args.action,
        )
    owned = owner_matcher(
        host, owned_name_prefixes(profile, host), persistent_runner_names(profile)
    )
    censuses = collect_censuses(
        (entry.repo for entry in protected),
        gh_cli=args.gh_cli or runner_census.github_cli(),
        timeout=args.timeout,
    )
    return classify(
        host=host,
        action=args.action,
        protected=protected,
        censuses=censuses,
        owned=owned,
        allow_last_serving_host=args.allow_last_serving_host,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        decision = run(args)
    except GuardError as exc:
        decision = Decision(
            allowed=False,
            reason=exc.reason,
            message=f"refusing pool {args.action}: {exc}",
            action=args.action,
        )
    except ValueError as exc:
        print(f"capacity floor: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.json:
        print(json.dumps(decision.as_dict(), indent=2, sort_keys=True))
    elif decision.allowed:
        print(decision.message)
    else:
        print(decision.message, file=sys.stderr)
    return decision.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
