#!/usr/bin/env python3
"""Hermetic tests for the dual-scope runner census."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner_census

REPO = "Generous-Corp/pulp"
REPO_ENDPOINT = "repos/Generous-Corp/pulp/actions/runners"
ORG_ENDPOINT = "orgs/Generous-Corp/actions/runners"

GATE_LABEL = "pulp-build-pr-head"
BASE_LABELS = ["self-hosted", "macOS", "ARM64"]


def runner(runner_id: int, name: str, *, status: str = "online", busy: bool = False,
           labels: list[str] | None = None) -> dict:
    return {
        "id": runner_id,
        "name": name,
        "status": status,
        "busy": busy,
        "labels": [{"name": label} for label in (labels if labels is not None else BASE_LABELS)],
    }


def fetcher(pages: dict[str, list[dict]], *, fail: dict[str, str] | None = None):
    """A census fetcher over canned per-endpoint payloads."""
    failures = fail or {}

    def fetch(scope: str, endpoint: str) -> list[dict]:
        if endpoint in failures:
            raise runner_census.CensusScopeError(scope, endpoint, "http_403", failures[endpoint])
        return pages.get(endpoint, [])

    return fetch


class ScopeCoverageTests(unittest.TestCase):
    """A runner registered only on the organization is real capacity."""

    def setUp(self) -> None:
        # The live shape this guards: the repository listing holds the ephemeral
        # gate runners, and a separate machine's runner is registered on the
        # organization and appears in neither total the other endpoint reports.
        self.pages = {
            REPO_ENDPOINT: [
                runner(31239, "studio-pulp-gate-01-612-7", labels=BASE_LABELS + [GATE_LABEL]),
            ],
            ORG_ENDPOINT: [
                runner(27620, "pulp-intel-macmini", labels=BASE_LABELS + [GATE_LABEL]),
            ],
        }

    def test_repository_only_census_misses_the_organization_runner(self) -> None:
        # The control for the dual-scope assertion below: reading one scope
        # returns a complete-looking census that reports the organization
        # runner as absent and the label as UNSERVED. Nothing in that answer
        # says a whole scope went unread — which is exactly why a single-scope
        # census is unsafe to decide capacity from.
        census = runner_census.collect(
            REPO, fetcher(self.pages), scopes=(runner_census.REPOSITORY_SCOPE,)
        )
        names = [record.name for record in census.runners]

        self.assertEqual(names, ["studio-pulp-gate-01-612-7"])
        self.assertNotIn("pulp-intel-macmini", names)
        self.assertTrue(census.complete)
        status = runner_census.label_status(
            census, GATE_LABEL, exclude=lambda record: record.name.startswith("studio-")
        )
        self.assertEqual(status.status, runner_census.UNSERVED)

    def test_dual_scope_census_finds_the_organization_only_runner(self) -> None:
        census = runner_census.collect(REPO, fetcher(self.pages))
        names = [record.name for record in census.runners]

        self.assertIn("pulp-intel-macmini", names)
        self.assertEqual(len(names), 2)
        self.assertTrue(census.complete)

    def test_organization_only_runner_answers_the_label_as_served(self) -> None:
        census = runner_census.collect(REPO, fetcher(self.pages))

        status = runner_census.label_status(
            census, GATE_LABEL, exclude=lambda record: record.name.startswith("studio-")
        )

        self.assertEqual(status.status, runner_census.SERVED)
        self.assertEqual([record.name for record in status.online], ["pulp-intel-macmini"])

    def test_each_record_carries_the_endpoint_that_owns_it(self) -> None:
        census = runner_census.collect(REPO, fetcher(self.pages))
        by_name = {record.name: record for record in census.runners}

        self.assertEqual(by_name["studio-pulp-gate-01-612-7"].endpoint, REPO_ENDPOINT)
        self.assertEqual(by_name["pulp-intel-macmini"].endpoint, ORG_ENDPOINT)
        self.assertEqual(by_name["pulp-intel-macmini"].scope, runner_census.ORGANIZATION_SCOPE)

    def test_same_id_in_both_scopes_stays_two_registrations(self) -> None:
        pages = {
            REPO_ENDPOINT: [runner(7, "repo-runner")],
            ORG_ENDPOINT: [runner(7, "org-runner")],
        }

        census = runner_census.collect(REPO, fetcher(pages))

        self.assertEqual(
            sorted(record.name for record in census.runners), ["org-runner", "repo-runner"]
        )


class FailClosedTests(unittest.TestCase):
    def test_unreachable_scope_reports_unknown_not_unserved(self) -> None:
        census = runner_census.collect(
            REPO,
            fetcher({REPO_ENDPOINT: []}, fail={ORG_ENDPOINT: "Resource not accessible"}),
        )

        status = runner_census.label_status(census, GATE_LABEL)

        self.assertFalse(census.complete)
        self.assertEqual(status.status, runner_census.UNKNOWN)
        self.assertIn("organization", status.detail)

    def test_an_unreachable_scope_never_hides_the_reachable_one(self) -> None:
        census = runner_census.collect(
            REPO,
            fetcher(
                {REPO_ENDPOINT: [runner(1, "studio-pulp-gate-01", labels=BASE_LABELS + [GATE_LABEL])]},
                fail={ORG_ENDPOINT: "boom"},
            ),
        )

        self.assertEqual([record.name for record in census.runners], ["studio-pulp-gate-01"])
        self.assertEqual(runner_census.label_status(census, GATE_LABEL).status, runner_census.SERVED)
        self.assertFalse(census.complete)

    def test_found_label_stays_served_even_when_a_scope_is_unread(self) -> None:
        census = runner_census.collect(
            REPO,
            fetcher({ORG_ENDPOINT: [runner(2, "peer", labels=[GATE_LABEL])]}, fail={REPO_ENDPOINT: "boom"}),
        )

        self.assertEqual(runner_census.label_status(census, GATE_LABEL).status, runner_census.SERVED)

    def test_offline_runner_is_not_online_capacity(self) -> None:
        census = runner_census.collect(
            REPO,
            fetcher({REPO_ENDPOINT: [runner(3, "peer", status="offline", labels=[GATE_LABEL])]}),
        )

        status = runner_census.label_status(census, GATE_LABEL)

        self.assertEqual(status.status, runner_census.UNSERVED)
        self.assertEqual([record.name for record in status.offline], ["peer"])

    def test_busy_runner_still_counts_as_capacity(self) -> None:
        census = runner_census.collect(
            REPO, fetcher({REPO_ENDPOINT: [runner(4, "peer", busy=True, labels=[GATE_LABEL])]})
        )

        self.assertEqual(runner_census.label_status(census, GATE_LABEL).status, runner_census.SERVED)


class ShapeTests(unittest.TestCase):
    def test_endpoints_are_derived_from_the_repository(self) -> None:
        self.assertEqual(
            runner_census.scope_endpoint(runner_census.REPOSITORY_SCOPE, REPO), REPO_ENDPOINT
        )
        self.assertEqual(
            runner_census.scope_endpoint(runner_census.ORGANIZATION_SCOPE, REPO), ORG_ENDPOINT
        )

    def test_malformed_repository_is_rejected(self) -> None:
        for value in ("", "pulp", "a/b/c", "owner/"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    runner_census.scope_endpoint(runner_census.REPOSITORY_SCOPE, value)

    def test_extract_handles_single_page_and_slurped_pages(self) -> None:
        self.assertEqual(
            [row["name"] for row in runner_census.extract_runners({"runners": [{"name": "a"}]})], ["a"]
        )
        self.assertEqual(
            [
                row["name"]
                for row in runner_census.extract_runners(
                    [{"runners": [{"name": "a"}]}, {"runners": [{"name": "b"}]}]
                )
            ],
            ["a", "b"],
        )

    def test_cli_fetcher_turns_a_failed_call_into_an_unread_scope(self) -> None:
        def run_json(argv: list[str]) -> object:
            raise RuntimeError("HTTP 403")

        census = runner_census.collect(REPO, runner_census.cli_fetcher("ghapp", run_json=run_json))

        self.assertFalse(census.complete)
        self.assertEqual(len(census.unreachable), 2)
        self.assertEqual(runner_census.label_status(census, GATE_LABEL).status, runner_census.UNKNOWN)

    def test_cli_fetcher_reads_both_endpoints(self) -> None:
        seen: list[str] = []

        def run_json(argv: list[str]) -> object:
            seen.append(argv[2])
            return {"runners": []}

        runner_census.collect(REPO, runner_census.cli_fetcher("ghapp", run_json=run_json))

        self.assertEqual(
            seen, [f"{REPO_ENDPOINT}?per_page=100", f"{ORG_ENDPOINT}?per_page=100"]
        )


class CliTests(unittest.TestCase):
    def test_cli_exits_nonzero_on_an_incomplete_census(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ghapp"
            fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake.chmod(0o755)
            proc = subprocess.run(
                [sys.executable, str(Path(runner_census.__file__)), "--repo", REPO,
                 "--label", GATE_LABEL, "--json", "--gh-cli", str(fake)],
                capture_output=True, text=True, timeout=30,
            )

            self.assertEqual(proc.returncode, 4, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["complete"])
            self.assertEqual(payload["labels"][0]["status"], runner_census.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
