#!/usr/bin/env python3
"""Census GitHub self-hosted runners across both registration scopes.

`repos/<owner>/<repo>/actions/runners` lists only the runners registered on the
repository. Runners registered on the organization serve the same repository
but live at `orgs/<owner>/actions/runners`, and the repository listing omits
them silently — no error, no marker, just a shorter list. A census that reads
one endpoint therefore answers "how many runners serve this label" with a
confident wrong number, and the wrong number is usually smaller than the truth.
Zero is the dangerous answer: it reads as "there is nothing here to protect".

Every census here reads both endpoints. A scope that cannot be read is recorded
as unreachable and makes the whole census incomplete, so a caller can tell
"no runner carries this label" apart from "no runner was observed". An
incomplete census answers UNKNOWN for a label it did not find, never UNSERVED.

Registrations are per-scope: a repository runner and an organization runner can
share a numeric id and are still two different machines, so records are keyed by
(scope, id) and each record carries the endpoint that owns it. Delete or inspect
a runner through its own `endpoint`, never through the other scope's URL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

REPOSITORY_SCOPE = "repository"
ORGANIZATION_SCOPE = "organization"
SCOPES: tuple[str, ...] = (REPOSITORY_SCOPE, ORGANIZATION_SCOPE)

SERVED = "served"
UNSERVED = "unserved"
UNKNOWN = "unknown"

_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$")

# Named census failures. A census that fails for an identity reason must say
# so: both of these read as "capacity unknown" otherwise, and both have been
# misdiagnosed as a missing GitHub permission.
CENSUS_UNAUTHENTICATED = "census_unauthenticated"
CENSUS_IDENTITY_LACKS_ACCESS = "census_identity_lacks_access"
_ANONYMOUS_RATE_LIMIT = re.compile(
    r"rate limit exceeded for \d{1,3}(?:\.\d{1,3}){3}\b"
    r"|authenticated requests get a higher rate limit", re.I)
_UNAUTHENTICATED = re.compile(
    r"bad credentials|requires authentication|http 401|401 unauthorized"
    r"|must authenticate|gh auth login|not logged in", re.I)
_INTEGRATION_FORBIDDEN = re.compile(r"resource not accessible by integration", re.I)

# The environment a GitHub App wrapper binds its installation from. Shipyard's
# ghapp reads SHIPYARD_GHAPP_REPO, then GH_REPO, and only then falls back to
# the checkout it runs in; SHIPYARD_GH_APP_REPO is the name tartci's own lane
# plists and runner.sh export. Setting all three pins the installation to the
# queried repository wherever the census happens to be run from.
IDENTITY_ENV_NAMES = ("SHIPYARD_GHAPP_REPO", "GH_REPO", "SHIPYARD_GH_APP_REPO")


def identity_env(repo: str) -> dict[str, str]:
    split_repo(repo)
    return {name: repo for name in IDENTITY_ENV_NAMES}


def classify_census_failure(text: str, *, cli: str, repo: str) -> tuple[str, str] | None:
    """(reason, operator message) for an identity failure, else None."""
    message = (text or "").strip()
    excerpt = message[:300]
    if _ANONYMOUS_RATE_LIMIT.search(message) or _UNAUTHENTICATED.search(message):
        return CENSUS_UNAUTHENTICATED, (
            f"`{cli}` reached GitHub without a valid credential while reading runners "
            f"for {repo} (anonymous requests share one 60/hour allowance per IP "
            f"across every host behind it). Fix: run with TARTCI_GH_CLI=ghapp, or "
            f"repair `{cli}` authentication. This is not a capacity or permission "
            f"problem. GitHub said: {excerpt}"
        )
    if _INTEGRATION_FORBIDDEN.search(message):
        return CENSUS_IDENTITY_LACKS_ACCESS, (
            f"the GitHub App identity `{cli}` used, requested for {repo}, cannot read "
            f"that scope's runners. Check which installation `{cli}` minted a token "
            f"for: the census binds it to {repo} through "
            f"{'/'.join(IDENTITY_ENV_NAMES)}, and a token minted for another "
            f"repository's installation is refused here even when {repo}'s "
            f"installation holds org runner read. GitHub said: {excerpt}"
        )
    return None


class CensusScopeError(RuntimeError):
    """One scope could not be read. Carries a stable, greppable code."""

    def __init__(self, scope: str, endpoint: str, code: str, detail: str = "") -> None:
        super().__init__(f"{scope} scope unreadable ({code}): {detail or endpoint}")
        self.scope = scope
        self.endpoint = endpoint
        self.code = code
        self.detail = detail


def split_repo(repo: str) -> tuple[str, str]:
    text = (repo or "").strip()
    if not _REPO.fullmatch(text):
        raise ValueError(f"expected OWNER/REPO, got {repo!r}")
    owner, name = text.split("/", 1)
    return owner, name


def scope_endpoint(scope: str, repo: str) -> str:
    owner, name = split_repo(repo)
    if scope == REPOSITORY_SCOPE:
        return f"repos/{owner}/{name}/actions/runners"
    if scope == ORGANIZATION_SCOPE:
        return f"orgs/{owner}/actions/runners"
    raise ValueError(f"unknown runner scope: {scope!r}")


def extract_runners(payload: Any) -> list[dict]:
    """Pull runner objects out of a single page or a `--slurp`ed page list."""
    if isinstance(payload, dict):
        rows = payload.get("runners")
        return [row for row in rows or [] if isinstance(row, dict)]
    rows: list[dict] = []
    if isinstance(payload, list):
        for page in payload:
            if isinstance(page, dict):
                rows.extend(
                    row for row in (page.get("runners") or []) if isinstance(row, dict)
                )
            elif isinstance(page, list):
                rows.extend(row for row in page if isinstance(row, dict))
    return rows


@dataclass(frozen=True)
class RunnerRecord:
    """One registration. `raw` keeps the API payload a caller may need to
    re-emit, and `endpoint` is the only URL this registration answers on."""

    id: Any
    name: str
    status: str
    busy: bool
    labels: tuple[str, ...]
    scope: str
    endpoint: str
    raw: dict = field(default_factory=dict)

    @property
    def online(self) -> bool:
        return self.status == "online"

    def carries(self, label: str) -> bool:
        return label in self.labels

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "busy": self.busy,
            "labels": list(self.labels),
            "scope": self.scope,
            "endpoint": self.endpoint,
        }


def normalise(payload: dict, scope: str, endpoint: str) -> RunnerRecord:
    labels: list[str] = []
    for entry in payload.get("labels") or []:
        if isinstance(entry, dict):
            name = entry.get("name")
        else:
            name = entry
        if isinstance(name, str) and name:
            labels.append(name)
    return RunnerRecord(
        id=payload.get("id"),
        name=str(payload.get("name") or ""),
        status=str(payload.get("status") or ""),
        busy=bool(payload.get("busy")),
        labels=tuple(labels),
        scope=scope,
        endpoint=endpoint,
        raw=dict(payload),
    )


@dataclass(frozen=True)
class ScopeCensus:
    scope: str
    endpoint: str
    reachable: bool
    error: str = ""
    runners: tuple[RunnerRecord, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "endpoint": self.endpoint,
            "reachable": self.reachable,
            "error": self.error,
            "count": len(self.runners) if self.reachable else None,
        }


@dataclass(frozen=True)
class RunnerCensus:
    repo: str
    scopes: tuple[ScopeCensus, ...]

    @property
    def runners(self) -> tuple[RunnerRecord, ...]:
        seen: set[tuple[str, Any]] = set()
        merged: list[RunnerRecord] = []
        for scope in self.scopes:
            for record in scope.runners:
                key = (record.scope, record.id)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(record)
        return tuple(merged)

    @property
    def complete(self) -> bool:
        return all(scope.reachable for scope in self.scopes)

    @property
    def unreachable(self) -> tuple[ScopeCensus, ...]:
        return tuple(scope for scope in self.scopes if not scope.reachable)

    def unreachable_detail(self) -> str:
        return "; ".join(
            f"{scope.scope} scope ({scope.endpoint}): {scope.error or 'unreadable'}"
            for scope in self.unreachable
        )

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "complete": self.complete,
            "scopes": [scope.as_dict() for scope in self.scopes],
            "runners": [record.as_dict() for record in self.runners],
        }


Fetch = Callable[[str, str], Iterable[dict]]


def collect(repo: str, fetch: Fetch, *, scopes: Sequence[str] = SCOPES) -> RunnerCensus:
    """Read every scope. One scope failing never aborts the others."""
    results: list[ScopeCensus] = []
    # A fetcher that can bind its GitHub identity binds it to THIS repo, so an
    # App wrapper never mints for whatever checkout the caller stands in.
    bind = getattr(fetch, "bind_repo", None)
    if callable(bind):
        fetch = bind(repo)
    for scope in scopes:
        endpoint = scope_endpoint(scope, repo)
        try:
            rows = fetch(scope, endpoint)
        except CensusScopeError as exc:
            detail = f"{exc.code}: {exc.detail}" if exc.detail else exc.code
            results.append(ScopeCensus(scope, endpoint, reachable=False, error=detail))
            continue
        except Exception as exc:  # noqa: BLE001 — any failure is an unread scope
            results.append(
                ScopeCensus(scope, endpoint, reachable=False, error=f"{type(exc).__name__}: {exc}")
            )
            continue
        results.append(
            ScopeCensus(
                scope,
                endpoint,
                reachable=True,
                runners=tuple(normalise(row, scope, endpoint) for row in rows),
            )
        )
    return RunnerCensus(repo=repo, scopes=tuple(results))


def cli_fetcher(cli: str, *, run_json: Callable[[list[str]], Any], per_page: int = 100) -> Fetch:
    """Build a fetcher over a GitHub CLI wrapper, using the caller's runner.

    The caller supplies `run_json` so each consumer keeps its own subprocess
    policy (observation budget, timeout, App wrapper) instead of this module
    imposing one.
    """

    def fetcher_for(repo: str | None):
        def fetch(scope: str, endpoint: str) -> list[dict]:
            argv = [cli, "api", f"{endpoint}?per_page={per_page}", "--paginate", "--slurp"]
            if repo is not None:
                # `env` rather than a subprocess env= so every caller's own
                # runner (bounded, budgeted) carries the binding unchanged.
                argv = ["/usr/bin/env",
                        *(f"{k}={v}" for k, v in identity_env(repo).items()), *argv]
            try:
                payload = run_json(argv)
            except Exception as exc:  # noqa: BLE001
                named = classify_census_failure(str(exc), cli=cli, repo=repo or "?")
                if named is not None:
                    raise CensusScopeError(scope, endpoint, named[0], named[1]) from exc
                code = getattr(exc, "problem_code", "") or type(exc).__name__
                raise CensusScopeError(scope, endpoint, str(code), str(exc)) from exc
            return extract_runners(payload)

        return fetch

    fetch = fetcher_for(None)
    fetch.bind_repo = fetcher_for  # type: ignore[attr-defined]
    return fetch


@dataclass(frozen=True)
class LabelStatus:
    label: str
    status: str
    online: tuple[RunnerRecord, ...]
    offline: tuple[RunnerRecord, ...]
    detail: str = ""

    @property
    def served(self) -> bool:
        return self.status == SERVED

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "status": self.status,
            "online": [record.name for record in self.online],
            "offline": [record.name for record in self.offline],
            "detail": self.detail,
        }


def label_status(
    census: RunnerCensus,
    label: str,
    *,
    exclude: Callable[[RunnerRecord], bool] | None = None,
) -> LabelStatus:
    """Answer whether `label` is served, fail-closed on an incomplete census.

    `exclude` drops runners the caller does not want to count as capacity — a
    host asking "does anyone ELSE serve this" excludes its own registrations.
    A label found nowhere in an incomplete census is UNKNOWN, never UNSERVED,
    because the scope that was not read is exactly where it might live.
    """
    online: list[RunnerRecord] = []
    offline: list[RunnerRecord] = []
    for record in census.runners:
        if not record.carries(label):
            continue
        if exclude is not None and exclude(record):
            continue
        (online if record.online else offline).append(record)
    if online:
        return LabelStatus(label, SERVED, tuple(online), tuple(offline))
    if not census.complete:
        return LabelStatus(
            label, UNKNOWN, tuple(online), tuple(offline), census.unreachable_detail()
        )
    return LabelStatus(label, UNSERVED, tuple(online), tuple(offline))


# ── CLI ─────────────────────────────────────────────────────────────────────


def _default_run_json(argv: list[str]) -> Any:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}")
    return json.loads(proc.stdout or "null")


def github_cli(env: dict[str, str] | None = None) -> str:
    """TARTCI_GH_CLI, else ghapp (PATH or ~/.local/bin), else gh.

    Bare `gh` is the last resort, not the default: on a host whose gh login is
    broken it silently goes anonymous and spends the fleet's shared 60/hour
    allowance. The App wrapper is the identity the fleet actually runs as.
    """
    environ = os.environ if env is None else env
    configured = (environ.get("TARTCI_GH_CLI") or "").strip()
    if configured:
        return configured
    found = shutil.which("ghapp", path=environ.get("PATH"))
    if found:
        return found
    home = environ.get("HOME") or os.path.expanduser("~")
    local = os.path.join(home, ".local", "bin", "ghapp")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return "gh"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Census self-hosted runners across repository and organization scope."
    )
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument("--label", action="append", default=[], help="report this label's status")
    parser.add_argument("--gh-cli", default="",
                        help="GitHub CLI wrapper (default: $TARTCI_GH_CLI, else ghapp, else gh)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    fetch = cli_fetcher(args.gh_cli or github_cli(), run_json=_default_run_json)
    census = collect(args.repo, fetch)
    statuses = [label_status(census, label) for label in args.label]

    if args.json:
        payload = census.as_dict()
        payload["labels"] = [status.as_dict() for status in statuses]
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for scope in census.scopes:
            state = f"{len(scope.runners)} runner(s)" if scope.reachable else f"UNREACHABLE ({scope.error})"
            print(f"{scope.scope:<13} {scope.endpoint}: {state}")
        for status in statuses:
            names = ", ".join(record.name for record in status.online) or "-"
            print(f"label {status.label}: {status.status} [{names}] {status.detail}".rstrip())

    if not census.complete:
        print(f"census incomplete: {census.unreachable_detail()}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
