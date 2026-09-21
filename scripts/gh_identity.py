#!/usr/bin/env python3
"""GitHub identity preflight and named failure reasons for queue scanners.

A queue scanner that cannot reach GitHub reports an empty observation or a
bare failure, and both read in the operator log as "the queue is quiet". The
most expensive version of that ambiguity is an unauthenticated caller: `gh`
falls back to anonymous requests, GitHub serves 60 core requests per hour per
IP, every host behind one address shares that allowance, and the scan fails
closed with no statement of cause.

This module supplies the two facts that end the ambiguity. `resolve_identity`
measures the effective identity before a scan reads the queue, so an anonymous
caller is refused by name instead of silently spending a shared 60/hour
allowance.
`classify_failure` turns a scanner error into a reason code, and reads a
403 against the anonymous ceiling as an authentication fault rather than a
generic rate limit.
"""
from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


# GitHub serves 60 core requests per hour per IP to an unauthenticated caller.
# Observing that ceiling is positive proof the request carried no credential:
# no authenticated identity is issued this allowance.
ANONYMOUS_CORE_LIMIT = 60
# A user or OAuth credential is issued 5000/hour. An app installation token is
# issued at least that and scales with installation size (15000/hour is the
# ceiling this fleet's installation reports).
USER_CORE_LIMIT = 5000

ANONYMOUS = "anonymous"
USER = "user"
APP_INSTALLATION = "app-installation"
AUTHENTICATED = "authenticated"

NO_VALID_CREDENTIALS = "no_valid_credentials"
RATE_LIMITED = "rate_limited"
IDENTITY_PREFLIGHT_UNAVAILABLE = "identity_preflight_unavailable"
TIMEOUT = "timeout"
LOCK_CONTENTION = "lock_contention"
PAGINATION = "pagination"
BUDGET_EXHAUSTED = "budget_exhausted"
API_ERROR = "api_error"

REASON_CODES = (
    NO_VALID_CREDENTIALS,
    RATE_LIMITED,
    IDENTITY_PREFLIGHT_UNAVAILABLE,
    TIMEOUT,
    LOCK_CONTENTION,
    PAGINATION,
    BUDGET_EXHAUSTED,
    API_ERROR,
)

# GitHub appends this invitation only to an UNAUTHENTICATED rejection, so its
# presence alone identifies the caller as anonymous.
_ANONYMOUS_MARKER = "authenticated requests get a higher rate limit"
_RATE_LIMIT_MARKERS = (
    "api rate limit exceeded",
    "rate limit exceeded",
    "secondary rate limit",
)
_BAD_CREDENTIAL_MARKERS = (
    "bad credentials",
    "requires authentication",
    "http 401",
    "401 unauthorized",
    "must authenticate",
)
_TIMEOUT_MARKERS = ("timed out", "timeout", ":timeout:")
_LOCK_MARKERS = ("observation lock", "lock timed out", "lock contention")
_PAGINATION_MARKERS = (
    "pagination",
    "total_count",
    "first page changed",
    "duplicate id",
)

RECEIPT_ENV = "TARTCI_GH_IDENTITY_RECEIPT_FILE"
RECEIPT_TTL_ENV = "TARTCI_GH_IDENTITY_RECEIPT_TTL_SECS"
DEFAULT_RECEIPT_TTL_SECONDS = 600


class AuthPreflightError(RuntimeError):
    """The effective GitHub identity is not fit to observe the queue."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


@dataclasses.dataclass(frozen=True)
class GitHubIdentity:
    """What the effective credential is, measured rather than assumed."""

    kind: str
    core_limit: int | None
    core_remaining: int | None
    token_source: str

    @property
    def authenticated(self) -> bool:
        return self.kind != ANONYMOUS

    def describe(self) -> str:
        limit = "unknown" if self.core_limit is None else f"{self.core_limit}/hour"
        remaining = (
            "unknown" if self.core_remaining is None else str(self.core_remaining)
        )
        return (
            f"identity={self.kind} ceiling={limit} remaining={remaining} "
            f"token_source={self.token_source}"
        )


@dataclasses.dataclass(frozen=True)
class ScanFailure:
    """A scanner failure carrying the cause an operator can act on."""

    reason_code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.reason_code}: {self.detail}"


def classify_core_limit(limit: int) -> str:
    """Name the credential grade an observed core ceiling implies."""

    if limit <= ANONYMOUS_CORE_LIMIT:
        return ANONYMOUS
    if limit == USER_CORE_LIMIT:
        return USER
    if limit > USER_CORE_LIMIT:
        return APP_INSTALLATION
    return AUTHENTICATED


def token_source(env: Mapping[str, str] | None = None) -> str:
    """Name where the credential comes from. Never reads or returns its value."""

    environ = os.environ if env is None else env
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if (environ.get(name) or "").strip():
            return name
    return "gh-stored-credential"


def identity_from_rate_limit(
    payload: Any, *, source: str | None = None
) -> GitHubIdentity:
    """Read an identity out of a `rate_limit` payload, or refuse to guess."""

    core: Any = None
    if isinstance(payload, dict):
        resources = payload.get("resources")
        if isinstance(resources, dict):
            core = resources.get("core")
        if core is None and isinstance(payload.get("rate"), dict):
            core = payload.get("rate")
    if not isinstance(core, dict) or not isinstance(core.get("limit"), int):
        raise AuthPreflightError(
            IDENTITY_PREFLIGHT_UNAVAILABLE,
            "gh returned no core rate-limit ceiling, so the effective identity "
            "cannot be proven",
        )
    remaining = core.get("remaining")
    return GitHubIdentity(
        kind=classify_core_limit(int(core["limit"])),
        core_limit=int(core["limit"]),
        core_remaining=remaining if isinstance(remaining, int) else None,
        token_source=token_source() if source is None else source,
    )


def require_authenticated(identity: GitHubIdentity) -> GitHubIdentity:
    """Admit an authenticated identity; refuse an anonymous one by name."""

    if identity.authenticated:
        return identity
    raise AuthPreflightError(
        NO_VALID_CREDENTIALS,
        "gh is unauthenticated; requests would fall back to anonymous "
        f"({identity.describe()}); the anonymous allowance is "
        f"{ANONYMOUS_CORE_LIMIT}/hour per IP and is shared by every host "
        "behind this address",
    )


def _receipt_path(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    configured = (environ.get(RECEIPT_ENV) or "").strip()
    if configured:
        return Path(configured)
    home = environ.get("HOME") or str(Path.home())
    return Path(home) / ".tartci/state/gh-identity.json"


def _receipt_ttl(env: Mapping[str, str] | None = None) -> int:
    environ = os.environ if env is None else env
    raw = (environ.get(RECEIPT_TTL_ENV) or "").strip()
    if not raw:
        return DEFAULT_RECEIPT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RECEIPT_TTL_SECONDS
    return max(value, 0)


def _read_receipt(
    path: Path, gh_cli: str, source: str, ttl: int
) -> GitHubIdentity | None:
    """Reuse a recent measurement, or report none rather than assume one."""

    if ttl <= 0:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("gh_cli") != gh_cli or payload.get("token_source") != source:
        return None
    stamped = payload.get("ts")
    kind = payload.get("kind")
    if not isinstance(stamped, (int, float)) or kind not in (
        ANONYMOUS,
        USER,
        APP_INSTALLATION,
        AUTHENTICATED,
    ):
        return None
    age = time.time() - float(stamped)
    if age < 0 or age >= ttl:
        return None
    limit = payload.get("core_limit")
    remaining = payload.get("core_remaining")
    return GitHubIdentity(
        kind=str(kind),
        core_limit=limit if isinstance(limit, int) else None,
        core_remaining=remaining if isinstance(remaining, int) else None,
        token_source=source,
    )


def _write_receipt(path: Path, gh_cli: str, identity: GitHubIdentity) -> None:
    payload = {
        "ts": int(time.time()),
        "gh_cli": gh_cli,
        "kind": identity.kind,
        "core_limit": identity.core_limit,
        "core_remaining": identity.core_remaining,
        "token_source": identity.token_source,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=str(path.parent))
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        os.replace(temp_name, path)
    except OSError:
        # The receipt only spares a probe. Losing it costs one API call.
        return


def forget_identity(env: Mapping[str, str] | None = None) -> None:
    """Drop the measurement so the next preflight measures again.

    A credential that stops working mid-window must not keep being vouched for
    by the receipt it wrote while it still worked.
    """

    try:
        _receipt_path(env).unlink()
    except OSError:
        return


def resolve_identity(
    fetch: Callable[[], Any],
    *,
    gh_cli: str = "gh",
    env: Mapping[str, str] | None = None,
) -> GitHubIdentity:
    """Prove the effective identity, then admit only an authenticated one.

    `fetch` reads GitHub's `rate_limit` endpoint, which is served without
    spending quota and answers even when the allowance is gone -- so the
    ceiling can always be read, including in the exhausted state this exists
    to diagnose. Callers pass their own API path so the probe is serialized by
    the same host observation lock, counted against the same budget, and
    bounded by the same timeout as every other call the scan makes.
    """

    source = token_source(env)
    path = _receipt_path(env)
    ttl = _receipt_ttl(env)
    cached = _read_receipt(path, gh_cli, source, ttl)
    if cached is not None and cached.authenticated:
        return cached
    identity = identity_from_rate_limit(fetch(), source=source)
    if ttl > 0:
        _write_receipt(path, gh_cli, identity)
    try:
        return require_authenticated(identity)
    except AuthPreflightError:
        # Nothing may stay behind that would vouch for a refused identity on
        # the next poll; the fault is re-measured instead of remembered.
        forget_identity(env)
        raise


def _matches(text: str, markers: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def _tail(text: str, limit: int = 200) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[-limit:]


def already_coded(text: str) -> bool:
    """True when a message already begins with one of this module's codes."""

    head = text.split(":", 1)[0].strip()
    return head in REASON_CODES


def classify_failure(
    text: str, identity: GitHubIdentity | None = None
) -> ScanFailure:
    """Name why an observation failed, with the identity that hit the limit.

    A 403 is only a rate limit when an authenticated identity ran out of an
    allowance it was actually issued. Against the anonymous ceiling the same
    403 is an authentication fault, and saying so is the difference between a
    one-minute diagnosis and a lost session.
    """

    message = (text or "").strip()
    if already_coded(message):
        code, _, detail = message.partition(":")
        return ScanFailure(code.strip(), detail.strip())
    described = "" if identity is None else f" ({identity.describe()})"
    anonymous_ceiling = (
        identity is not None
        and identity.core_limit is not None
        and identity.core_limit <= ANONYMOUS_CORE_LIMIT
    )
    if _matches(message, _LOCK_MARKERS):
        return ScanFailure(
            LOCK_CONTENTION,
            f"another observation held the host queue lock: {_tail(message)}",
        )
    if _matches(message, _RATE_LIMIT_MARKERS):
        if _matches(message, (_ANONYMOUS_MARKER,)) or anonymous_ceiling:
            ceiling = (
                ANONYMOUS_CORE_LIMIT
                if identity is None or identity.core_limit is None
                else identity.core_limit
            )
            return ScanFailure(
                NO_VALID_CREDENTIALS,
                f"GitHub refused the request at the {ceiling}/hour ANONYMOUS "
                "ceiling, which no authenticated identity is issued: the "
                f"caller sent no valid credential{described}. Restore "
                f"authentication; this is not a capacity problem: {_tail(message)}",
            )
        return ScanFailure(
            RATE_LIMITED,
            f"an authenticated identity exhausted its allowance{described}: "
            f"{_tail(message)}",
        )
    if _matches(message, _BAD_CREDENTIAL_MARKERS):
        return ScanFailure(
            NO_VALID_CREDENTIALS,
            f"GitHub rejected the credential{described}: {_tail(message)}",
        )
    if "budget exhausted" in message.lower():
        return ScanFailure(
            BUDGET_EXHAUSTED,
            f"the scan spent its own API call budget: {_tail(message)}",
        )
    if _matches(message, _PAGINATION_MARKERS):
        return ScanFailure(
            PAGINATION,
            f"the queue could not be paged completely: {_tail(message)}",
        )
    if _matches(message, _TIMEOUT_MARKERS):
        return ScanFailure(
            TIMEOUT, f"the observation ran out of time: {_tail(message)}"
        )
    return ScanFailure(API_ERROR, _tail(message) or "no detail reported")
