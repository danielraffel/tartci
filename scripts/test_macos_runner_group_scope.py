#!/usr/bin/env python3
"""Tests for GitHub runner API scope selection in the macOS provider."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "providers" / "tart-macos" / "runner.sh"


def api_root(repo: str, group: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["TARTCI_RUNNER_GROUP_ID"] = group
    return subprocess.run(
        ["/bin/bash", str(RUNNER), "--repo", repo, "--print-runner-api-root"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


class MacOSRunnerGroupScopeTests(unittest.TestCase):
    def test_default_group_preserves_repository_scope(self) -> None:
        result = api_root("Generous-Corp/pulp", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "repos/Generous-Corp/pulp/actions/runners",
        )

    def test_non_default_groups_use_repository_owner_scope(self) -> None:
        for repo, group in (
            ("Generous-Corp/pulp", "3"),
            ("Generous-Corp/vellum", "8"),
            ("Generous-Corp/forge", "11"),
        ):
            with self.subTest(repo=repo, group=group):
                result = api_root(repo, group)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.strip(),
                    "orgs/Generous-Corp/actions/runners",
                )

    def test_all_scopes_reject_malformed_repository(self) -> None:
        for group in ("1", "3"):
            for repo in (
                "pulp",
                "/pulp",
                "Generous-Corp/",
                "a/b/c",
                "Generous Corp/pulp",
                "Generous-Corp/pulp?ref=main",
            ):
                with self.subTest(group=group, repo=repo):
                    result = api_root(repo, group)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("expected OWNER/REPO", result.stderr)

    def test_group_id_must_be_a_positive_integer(self) -> None:
        for group in ("0", "01", "group-3", "-1"):
            with self.subTest(group=group):
                result = api_root("Generous-Corp/pulp", group)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("expected a positive integer", result.stderr)

    def test_create_list_and_delete_share_the_selected_scope(self) -> None:
        body = RUNNER.read_text(encoding="utf-8")
        self.assertIn('api "$RUNNER_API_ROOT" --paginate', body)
        self.assertIn('api -X DELETE "$RUNNER_API_ROOT/$id"', body)
        self.assertIn(
            'api -X POST "$RUNNER_API_ROOT/generate-jitconfig"',
            body,
        )
        self.assertIn(
            'SHIPYARD_GH_APP_REPO="$REPO" GH_REPO="$REPO"',
            body,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
