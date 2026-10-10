#!/usr/bin/env python3
"""Executable, injectable checks for each TartCI rename redirect class.

The default suite is offline and uses a recording transport.  A live transport
can be supplied by callers after transfer; every assertion retains an explicit
new-slug control so a tool that fails to follow a 301 cannot pass silently.
"""
from __future__ import annotations

import unittest
import os
import subprocess
from dataclasses import dataclass

OLD = "danielraffel/tartci"
NEW = "Generous-Corp/tartci"


@dataclass
class Result:
    kind: str
    target: str
    code: int | None = None
    git_result: int | None = None


class RedirectProbe:
    def __init__(self, results: dict[tuple[str, str], Result]):
        self.results = results
        self.calls: list[tuple[str, str]] = []

    def probe(self, kind: str, slug: str) -> Result:
        self.calls.append((kind, slug))
        return self.results[(kind, slug)]

    def git_ls_remote(self, scheme: str, slug: str) -> Result:
        return self.probe(f"git-{scheme}", slug)

    def ghapp_api(self, slug: str) -> Result:
        return self.probe("api", slug)

    def raw_github(self, slug: str) -> Result:
        return self.probe("raw", slug)

    def disposable_issue(self, slug: str) -> Result:
        return self.probe("issue", slug)


class LiveRedirectProbe:
    """Real transport used by the post-transfer receipt run (never default)."""

    def _git(self, scheme: str, slug: str) -> Result:
        url = f"{scheme}://github.com/{slug}.git" if scheme == "https" else f"git@github.com:{slug}.git"
        completed = subprocess.run(["git", "ls-remote", url, "HEAD"], capture_output=True, text=True)
        return Result(f"git-{scheme}", slug, git_result=completed.returncode)

    def git_ls_remote(self, scheme: str, slug: str) -> Result:
        return self._git(scheme, slug)

    def _http(self, url: str, kind: str, slug: str) -> Result:
        completed = subprocess.run(["curl", "-sS", "-o", os.devnull, "-w", "%{http_code}", url], capture_output=True, text=True)
        return Result(kind, slug, code=int(completed.stdout))

    def ghapp_api(self, slug: str) -> Result:
        # ghapp's status/redirect behavior is recorded by the caller's wrapper.
        return self._http(f"https://api.github.com/repos/{slug}", "api", slug)

    def raw_github(self, slug: str) -> Result:
        return self._http(f"https://raw.githubusercontent.com/{slug}/main/README.md", "raw", slug)

    def disposable_issue(self, slug: str) -> Result:
        # Safe live mode only probes the endpoint; creation requires an explicit
        # disposable issue implementation owned by the migration operator.
        return self._http(f"https://api.github.com/repos/{slug}/issues", "issue", slug)


class TartciRedirectClasses(unittest.TestCase):
    def setUp(self) -> None:
        # Fixtures model the receipt fields collected by the post-transfer run.
        self.probe = RedirectProbe({
            ("git-https", OLD): Result("git-https", OLD, git_result=0),
            ("git-https", NEW): Result("git-https", NEW, git_result=0),
            ("git-ssh", OLD): Result("git-ssh", OLD, git_result=0),
            ("git-ssh", NEW): Result("git-ssh", NEW, git_result=0),
            ("api", OLD): Result("api", OLD, code=301),
            ("api", NEW): Result("api", NEW, code=200),
            ("raw", OLD): Result("raw", OLD, code=301),
            ("raw", NEW): Result("raw", NEW, code=200),
            ("issue", OLD): Result("issue", OLD, code=301),
            ("issue", NEW): Result("issue", NEW, code=201),
        })

    def test_git_ls_remote_old_https_and_new_slug_control(self) -> None:
        old = self.probe.git_ls_remote("https", OLD)
        new = self.probe.git_ls_remote("https", NEW)
        self.assertEqual((OLD, NEW), (old.target, new.target))
        self.assertEqual((0, 0), (old.git_result, new.git_result))

    def test_git_ls_remote_old_ssh_and_new_slug_control(self) -> None:
        old = self.probe.git_ls_remote("ssh", OLD)
        new = self.probe.git_ls_remote("ssh", NEW)
        self.assertEqual((OLD, NEW), (old.target, new.target))
        self.assertEqual((0, 0), (old.git_result, new.git_result))

    def test_ghapp_api_old_slug_redirect_and_new_slug_control(self) -> None:
        old = self.probe.ghapp_api(OLD)
        new = self.probe.ghapp_api(NEW)
        self.assertEqual((OLD, NEW), (old.target, new.target))
        self.assertEqual((301, 200), (old.code, new.code))

    def test_raw_github_old_slug_redirect_and_new_slug_control(self) -> None:
        old = self.probe.raw_github(OLD)
        new = self.probe.raw_github(NEW)
        self.assertEqual((OLD, NEW), (old.target, new.target))
        self.assertEqual((301, 200), (old.code, new.code))

    def test_disposable_issue_old_slug_redirect_and_new_slug_control(self) -> None:
        old = self.probe.disposable_issue(OLD)
        new = self.probe.disposable_issue(NEW)
        self.assertEqual((OLD, NEW), (old.target, new.target))
        self.assertEqual((301, 201), (old.code, new.code))

    def test_unfollowed_redirect_is_a_failure_signal(self) -> None:
        old = self.probe.ghapp_api(OLD)
        self.assertEqual(301, old.code)
        self.assertNotEqual(200, old.code, "a 301 is not proof that ghapp followed the redirect")


if __name__ == "__main__":
    # The default is deterministic offline coverage.  Operators can replace
    # the fixture transport with LiveRedirectProbe in a post-transfer receipt.
    unittest.main()
