#!/usr/bin/env python3
"""Behavioral tests for the GitHub identity preflight and failure reasons.

Two failures used to be indistinguishable in a supervisor log: a credential
that stopped working, and a queue that had nothing in it. Both of these
assertions therefore come in pairs — an anonymous identity against an
authenticated one, a 60/hour ceiling against a 5000/15000 one — because a
check that can only pass proves nothing about the case it was written for.

Every GitHub response here is a fixture. Nothing in this file makes a network
call, authenticated or otherwise.

Run:  python3 scripts/test_gh_identity.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gh_identity  # noqa: E402


# GitHub's own wording for an unauthenticated rejection. The invitation to
# authenticate is served only when the request carried no credential, which is
# what makes it usable as proof rather than a hint.
ANONYMOUS_403 = (
    "gh: API rate limit exceeded for 203.0.113.7. (But here's the good news: "
    "Authenticated requests get a higher rate limit. Check out the "
    "documentation for more details.) (HTTP 403)"
)
AUTHENTICATED_403 = (
    "gh: API rate limit exceeded for user ID 12345. (HTTP 403)"
)


def _rate_limit(limit: int, remaining: int = 0) -> str:
    return json.dumps(
        {
            "resources": {
                "core": {"limit": limit, "remaining": remaining, "reset": 1},
            },
            "rate": {"limit": limit, "remaining": remaining, "reset": 1},
        }
    )


def _fetch(limit: int | None):
    """Stand in for the scanner's own `rate_limit` read, counting its calls."""
    calls: list[int] = []

    def fetch():
        calls.append(1)
        if limit is None:
            return {"resources": {}}
        return json.loads(_rate_limit(limit))

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


class IdentityPreflight(unittest.TestCase):
    """An anonymous caller is refused by name; an authenticated one proceeds."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.env = {
            "HOME": self.temp.name,
            gh_identity.RECEIPT_ENV: str(Path(self.temp.name) / "identity.json"),
            "GH_TOKEN": "",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_anonymous_ceiling_fails_the_preflight_by_name(self) -> None:
        fetch = _fetch(gh_identity.ANONYMOUS_CORE_LIMIT)
        with self.assertRaises(gh_identity.AuthPreflightError) as caught:
            gh_identity.resolve_identity(fetch, env=self.env)
        self.assertEqual(caught.exception.reason_code, "no_valid_credentials")
        self.assertIn("unauthenticated", str(caught.exception))
        self.assertIn("anonymous", str(caught.exception))
        self.assertIn("60/hour per IP", str(caught.exception))
        self.assertEqual(len(fetch.calls), 1)

    def test_authenticated_ceilings_pass_the_preflight(self) -> None:
        for limit, kind in ((5000, "user"), (15000, "app-installation")):
            with self.subTest(limit=limit):
                env = dict(self.env)
                env[gh_identity.RECEIPT_ENV] = str(
                    Path(self.temp.name) / f"identity-{limit}.json"
                )
                identity = gh_identity.resolve_identity(
                    _fetch(limit), env=env
                )
                self.assertTrue(identity.authenticated)
                self.assertEqual(identity.kind, kind)
                self.assertEqual(identity.core_limit, limit)

    def test_unreadable_ceiling_refuses_to_assume_authentication(self) -> None:
        with self.assertRaises(gh_identity.AuthPreflightError) as caught:
            gh_identity.resolve_identity(_fetch(None), env=self.env)
        self.assertEqual(
            caught.exception.reason_code, "identity_preflight_unavailable"
        )

    def test_receipt_spares_a_probe_but_never_survives_a_refusal(self) -> None:
        first = _fetch(15000)
        gh_identity.resolve_identity(first, env=self.env)
        second = _fetch(15000)
        gh_identity.resolve_identity(second, env=self.env)
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(
            len(second.calls), 0, "a fresh receipt should answer without a probe"
        )

        # A refusal leaves nothing behind that could vouch for the identity
        # on the next poll, so the fault is re-measured rather than cached.
        refused = dict(self.env)
        refused[gh_identity.RECEIPT_ENV] = str(
            Path(self.temp.name) / "refused.json"
        )
        anonymous = _fetch(gh_identity.ANONYMOUS_CORE_LIMIT)
        with self.assertRaises(gh_identity.AuthPreflightError):
            gh_identity.resolve_identity(anonymous, env=refused)
        self.assertEqual(len(anonymous.calls), 1)
        self.assertFalse(
            Path(refused[gh_identity.RECEIPT_ENV]).exists(),
            "a refused identity must not leave a receipt vouching for itself",
        )


class RateLimitAttribution(unittest.TestCase):
    """A 403 is only a rate limit when an identity had an allowance to spend."""

    ANONYMOUS = gh_identity.GitHubIdentity(
        kind="anonymous",
        core_limit=60,
        core_remaining=0,
        token_source="gh-stored-credential",
    )
    APP = gh_identity.GitHubIdentity(
        kind="app-installation",
        core_limit=15000,
        core_remaining=0,
        token_source="GH_TOKEN",
    )
    USER = gh_identity.GitHubIdentity(
        kind="user",
        core_limit=5000,
        core_remaining=0,
        token_source="GH_TOKEN",
    )

    def test_sixty_per_hour_is_reported_as_an_authentication_failure(self) -> None:
        by_ceiling = gh_identity.classify_failure(AUTHENTICATED_403, self.ANONYMOUS)
        self.assertEqual(by_ceiling.reason_code, "no_valid_credentials")
        self.assertIn("60/hour", str(by_ceiling))
        self.assertIn("ANONYMOUS", str(by_ceiling))

        # GitHub's invitation to authenticate identifies the caller on its own,
        # so the fault is named even when no identity was measured.
        by_text = gh_identity.classify_failure(ANONYMOUS_403, None)
        self.assertEqual(by_text.reason_code, "no_valid_credentials")

    def test_large_ceilings_are_reported_as_a_genuine_rate_limit(self) -> None:
        for identity, ceiling in ((self.APP, "15000"), (self.USER, "5000")):
            with self.subTest(kind=identity.kind):
                failure = gh_identity.classify_failure(AUTHENTICATED_403, identity)
                self.assertEqual(failure.reason_code, "rate_limited")
                self.assertIn(identity.kind, str(failure))
                self.assertIn(ceiling, str(failure))

    def test_the_two_verdicts_are_distinguishable(self) -> None:
        anonymous = gh_identity.classify_failure(AUTHENTICATED_403, self.ANONYMOUS)
        authenticated = gh_identity.classify_failure(AUTHENTICATED_403, self.APP)
        self.assertNotEqual(anonymous.reason_code, authenticated.reason_code)
        self.assertNotEqual(str(anonymous), str(authenticated))
        # The anonymous verdict must not read as a capacity problem, and the
        # authenticated one must not send an operator hunting for a credential.
        self.assertNotIn("no_valid_credentials", str(authenticated))
        self.assertIn("not a capacity problem", str(anonymous))

    def test_other_causes_keep_their_own_names(self) -> None:
        cases = (
            ("host queue observation lock timed out after 120s", "lock_contention"),
            (
                "assignment_scan_github_api:timeout:killed at 15s",
                "timeout",
            ),
            (
                "GitHub API total_count changed during pagination for runs",
                "pagination",
            ),
            ("assignment scan API budget exhausted (1200)", "budget_exhausted"),
            ("GitHub API failed for repos/x/y: HTTP 502", "api_error"),
        )
        for text, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    gh_identity.classify_failure(text, self.APP).reason_code, expected
                )

    def test_an_already_named_reason_is_not_renamed(self) -> None:
        once = gh_identity.classify_failure(ANONYMOUS_403, self.ANONYMOUS)
        twice = gh_identity.classify_failure(str(once), self.APP)
        self.assertEqual(twice.reason_code, once.reason_code)
        self.assertEqual(twice.detail, once.detail)


class CeilingClassification(unittest.TestCase):
    def test_ceilings_map_to_credential_grades(self) -> None:
        self.assertEqual(gh_identity.classify_core_limit(60), "anonymous")
        self.assertEqual(gh_identity.classify_core_limit(5000), "user")
        self.assertEqual(gh_identity.classify_core_limit(15000), "app-installation")

    def test_token_source_is_named_never_read(self) -> None:
        self.assertEqual(gh_identity.token_source({"GH_TOKEN": "x"}), "GH_TOKEN")
        self.assertEqual(
            gh_identity.token_source({"GITHUB_TOKEN": "x"}), "GITHUB_TOKEN"
        )
        self.assertEqual(gh_identity.token_source({}), "gh-stored-credential")
        described = gh_identity.GitHubIdentity(
            kind="user", core_limit=5000, core_remaining=1, token_source="GH_TOKEN"
        ).describe()
        self.assertIn("token_source=GH_TOKEN", described)
        self.assertNotIn("ghs_", described)


if __name__ == "__main__":
    os.environ.pop("GH_TOKEN", None)
    unittest.main(verbosity=2)
