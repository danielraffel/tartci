#!/usr/bin/env python3
"""The capacity census binds its GitHub identity to the queried repository.

Three live failures on 2026-09-23 made this necessary: a census run from a
tartci checkout minted a tartci-installation token and read the Generous-Corp
org runners as 403 "Resource not accessible by integration"; a host with no
TARTCI_GH_CLI used a logged-out `gh` and spent the fleet's anonymous 60/hour
allowance; and the --plan verdict offered --allow-last-serving-host for a
capacity-unknown refusal it cannot override.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capacity_floor
import fleet_doctor
import runner_census

ROOT = Path(__file__).resolve().parents[1]
REPO = "Generous-Corp/pulp"
RATE_LIMIT_STDERR = (
    "gh: API rate limit exceeded for 73.189.56.227. (But here's the good news: "
    "Authenticated requests get a higher rate limit. Check out the documentation "
    "for more details.) (HTTP 403)"
)
RATE_LIMIT_BARE = "API rate limit exceeded for 73.189.56.227"
INTEGRATION_STDERR = (
    '{"message":"Resource not accessible by integration","documentation_url":'
    '"https://docs.github.com/rest/actions/self-hosted-runners#list-self-hosted-'
    'runners-for-an-organization","status":"403"}gh: Resource not accessible by '
    "integration (HTTP 403)"
)


def fake_gh(td: Path, *, org_stderr: str = "", repo_stderr: str = "") -> Path:
    """A `gh` that logs its identity env and answers each scope."""
    script = td / "fakegh"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        printf 'cwd=%s SHIPYARD_GHAPP_REPO=%s GH_REPO=%s SHIPYARD_GH_APP_REPO=%s args=%s\\n' \\
          "$PWD" "$SHIPYARD_GHAPP_REPO" "$GH_REPO" "$SHIPYARD_GH_APP_REPO" "$*" >> "{td}/calls.log"
        case "$2" in
          orgs/*) if [ -n '{org_stderr}' ]; then printf '%s\\n' '{org_stderr}' >&2; exit 1; fi ;;
          repos/*) if [ -n '{repo_stderr}' ]; then printf '%s\\n' '{repo_stderr}' >&2; exit 1; fi ;;
        esac
        echo '[{{"total_count":1,"runners":[{{"id":1,"name":"m5-pulp-gate-01-1-1","status":"online","busy":false,"labels":[{{"name":"pulp-build-pr-head"}}]}}]}}]'
        """))
    script.chmod(0o755)
    return script


class IdentityBindingTests(unittest.TestCase):
    def test_every_call_binds_the_queried_repo_regardless_of_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            gh = fake_gh(td)
            # Run from inside a tartci checkout: the cwd that used to pick the
            # wrong App installation.
            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "runner_census.py"), "--repo", REPO,
                 "--gh-cli", str(gh)], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            calls = (td / "calls.log").read_text().splitlines()
            self.assertEqual(len(calls), 2)  # repository + organization scope
            for line in calls:
                self.assertIn(f"cwd={ROOT}", line)
                self.assertIn(f"SHIPYARD_GHAPP_REPO={REPO} GH_REPO={REPO} "
                              f"SHIPYARD_GH_APP_REPO={REPO}", line)
            self.assertTrue(any("orgs/Generous-Corp/actions/runners" in line for line in calls))

    def test_each_repo_in_a_multi_repo_census_gets_its_own_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            gh = fake_gh(td)
            capacity_floor.collect_censuses([REPO, "Generous-Corp/forge"], gh_cli=str(gh),
                                            timeout=10)
            calls = (td / "calls.log").read_text().splitlines()
            forge = [c for c in calls if "Generous-Corp/forge/" in c or "GH_REPO=Generous-Corp/forge" in c]
            self.assertEqual(len(forge), 2)
            for line in forge:
                self.assertIn("GH_REPO=Generous-Corp/forge ", line)


class BindingHygieneTests(unittest.TestCase):
    def test_binding_is_scoped_to_the_call_and_argv_is_unchanged(self) -> None:
        seen = []

        def run_json(argv):
            seen.append((list(argv), os.environ.get("GH_REPO"), os.environ.get("SHIPYARD_GHAPP_REPO")))
            if "orgs/" in argv[2]:
                raise RuntimeError(INTEGRATION_STDERR)
            return {"runners": []}

        with mock.patch.dict(os.environ, {"GH_REPO": "someone/else"}, clear=False):
            os.environ.pop("SHIPYARD_GHAPP_REPO", None)
            runner_census.collect(REPO, runner_census.cli_fetcher("ghapp", run_json=run_json))
            # Restored after success AND after a raising call.
            self.assertEqual(os.environ.get("GH_REPO"), "someone/else")
            self.assertNotIn("SHIPYARD_GHAPP_REPO", os.environ)
        for argv, gh_repo, ghapp_repo in seen:
            self.assertEqual(argv[:2], ["ghapp", "api"])  # callers rely on this shape
            self.assertEqual((gh_repo, ghapp_repo), (REPO, REPO))


class CliResolutionTests(unittest.TestCase):
    def _bin(self, td: Path, name: str) -> Path:
        path = td / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def test_resolution_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            onpath = td / "bin"
            onpath.mkdir()
            home = td / "home"
            (home / ".local" / "bin").mkdir(parents=True)
            base = {"PATH": str(onpath), "HOME": str(home)}
            self.assertEqual(runner_census.github_cli(base), "gh")
            local = self._bin(home / ".local" / "bin", "ghapp")
            self.assertEqual(runner_census.github_cli(base), str(local))
            found = self._bin(onpath, "ghapp")
            self.assertEqual(runner_census.github_cli(base), str(found))
            self.assertEqual(runner_census.github_cli({**base, "TARTCI_GH_CLI": "mygh"}), "mygh")


class FailureClassificationTests(unittest.TestCase):
    def test_recorded_stderr_is_named(self) -> None:
        for text in (RATE_LIMIT_STDERR, RATE_LIMIT_BARE, "HTTP 401: Bad credentials"):
            with self.subTest(text=text):
                code, message = runner_census.classify_census_failure(text, cli="gh", repo=REPO)
                self.assertEqual(code, runner_census.CENSUS_UNAUTHENTICATED)
                self.assertIn("`gh`", message)
                self.assertIn(REPO, message)
                self.assertIn("TARTCI_GH_CLI=ghapp", message)
        code, message = runner_census.classify_census_failure(
            INTEGRATION_STDERR, cli="ghapp", repo=REPO)
        self.assertEqual(code, runner_census.CENSUS_IDENTITY_LACKS_ACCESS)
        self.assertIn("which installation `ghapp` minted", message)
        self.assertIn(REPO, message)

    def test_other_failures_are_not_relabelled(self) -> None:
        # Control: the classifier does not claim unrelated errors.
        for text in ("HTTP 502: Bad Gateway", "timed out after 15s",
                     "API rate limit exceeded for installation ID 123"):
            self.assertIsNone(runner_census.classify_census_failure(text, cli="gh", repo=REPO))

    def test_census_carries_the_named_reason_into_the_floor_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            gh = fake_gh(td, org_stderr=INTEGRATION_STDERR)
            census = capacity_floor.collect_censuses([REPO], gh_cli=str(gh), timeout=10)[REPO]
            self.assertFalse(census.complete)
            self.assertIn(runner_census.CENSUS_IDENTITY_LACKS_ACCESS, census.unreachable_detail())
            decision = capacity_floor.classify(
                host="studio", action="off",
                protected=[capacity_floor.Protected(REPO, "pulp-build-merge-group")],
                censuses={REPO: census}, owned=lambda record: False)
            self.assertEqual(decision.reason, capacity_floor.REASON_CAPACITY_UNKNOWN)
            self.assertEqual(decision.census_reason, runner_census.CENSUS_IDENTITY_LACKS_ACCESS)
            self.assertIn("does NOT override", decision.message)
            self.assertIn("minted a token", decision.message)


class VerdictTextTests(unittest.TestCase):
    def _decision(self, **kwargs):
        census = runner_census.RunnerCensus(repo=REPO, scopes=(
            runner_census.ScopeCensus("repository", "r", reachable=True, runners=()),
            runner_census.ScopeCensus("organization", "o", reachable=kwargs.pop("org_ok", True),
                                      runners=(), error=kwargs.pop("error", ""))))
        return capacity_floor.classify(
            host="studio", action="off",
            protected=[capacity_floor.Protected(REPO, "pulp-build-merge-group")],
            censuses={REPO: census}, owned=lambda record: False, **kwargs)

    def test_last_serving_is_overridable_and_unknown_is_not(self) -> None:
        last = self._decision()
        self.assertEqual(last.reason, capacity_floor.REASON_LAST_SERVING_HOST)
        self.assertIn("Pass --allow-last-serving-host", last.message)
        unknown = self._decision(org_ok=False, error=f"{runner_census.CENSUS_UNAUTHENTICATED}: x")
        self.assertEqual(unknown.reason, capacity_floor.REASON_CAPACITY_UNKNOWN)
        self.assertNotIn("Pass --allow-last-serving-host", unknown.message)
        self.assertIn("--allow-last-serving-host does NOT override", unknown.message)
        self.assertEqual(unknown.census_reason, runner_census.CENSUS_UNAUTHENTICATED)
        # And the flag really does not flip an unknown answer.
        forced = self._decision(org_ok=False, error="x", allow_last_serving_host=True)
        self.assertFalse(forced.allowed)

    def test_plan_verdict_line_names_which_refusal(self) -> None:
        text = (ROOT / "tartci").read_text()
        block = text[text.index("tartci_pool_plan() {"):]
        block = block[:block.index("\n}\n")]
        for rc, expect, reject in (
                ("3", "last serving host for a required label; --allow-last-serving-host overrides",
                 "does NOT override"),
                ("4", "capacity UNKNOWN", "--allow-last-serving-host overrides"),
                ("127", "capacity floor could not run (exit 127): floor says no",
                 "capacity UNKNOWN"),
                ("2", "capacity floor could not run (exit 2)", "capacity UNKNOWN")):
            with self.subTest(rc=rc):
                script = textwrap.dedent(f"""\
                    set -u
                    HERE={ROOT}
                    tartci_pool_read_participation() {{ echo 1; }}
                    tartci_pool_read_state() {{ echo on; }}
                    tartci_pool_owned_runner_agents() {{ :; }}
                    tartci_pool_report_unowned() {{ :; }}
                    tartci_pool_agent_loaded() {{ return 1; }}
                    tartci_pool_mid_job() {{ return 0; }}
                    tartci_pool_capacity_floor_preflight() {{
                      TARTCI_POOL_FLOOR_EXIT={rc}; echo "floor says no"; return 11; }}
                    {block}
                    }}
                    tartci_pool_plan drain test-host /nonexistent 0 0
                    """)
                proc = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
                self.assertEqual(proc.returncode, 11, proc.stderr)
                self.assertIn(expect, proc.stdout)
                self.assertNotIn(reject, proc.stdout)


class DoctorIdentityTests(unittest.TestCase):
    def _run(self, payload: dict | None, rc: int = 0):
        seen = []

        def run(argv):
            seen.append(argv)
            return rc, json.dumps(payload) if payload is not None else "", "boom"
        return fleet_doctor.check_census_identity("gh", REPO, run), seen

    def test_anonymous_identity_is_a_problem_naming_the_fix(self) -> None:
        finding, seen = self._run({"resources": {"core": {"limit": 60, "remaining": 0}}})
        self.assertEqual((finding.state, finding.code),
                         (fleet_doctor.PROBLEM, "census_identity_unauthenticated"))
        self.assertIn("TARTCI_GH_CLI=ghapp", finding.detail)
        self.assertIn(f"GH_REPO={REPO}", seen[0])
        self.assertEqual(seen[0][-3:], ["gh", "api", "rate_limit"])

    def test_authenticated_and_unproven(self) -> None:
        ok, _ = self._run({"resources": {"core": {"limit": 15000}}})
        self.assertEqual((ok.state, ok.code), (fleet_doctor.OK, "census_identity_authenticated"))
        blind, _ = self._run(None, rc=1)
        self.assertEqual(blind.code, "census_identity_unknown")

    def test_doctor_collect_reports_it_per_repo(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            (home / "Library" / "LaunchAgents").mkdir(parents=True)
            # No network: the census itself is stubbed; only the identity
            # probe's runner is exercised.
            with mock.patch.object(fleet_doctor, "collect_census",
                                   return_value=(None, "census_incomplete", "stub")):
                rows = fleet_doctor.collect(
                    home=home, repos=[REPO], gh_cli="gh",
                    probe=lambda root: {"error": "stub"},
                    identity_run=lambda argv: (0, '{"resources":{"core":{"limit":60}}}', ""))
            finding = next(r for r in rows if r.check == f"census_identity[{REPO}]")
            self.assertEqual(finding.code, "census_identity_unauthenticated")
            reasons = fleet_doctor.load_reasons()
            for code in ("census_identity_authenticated", "census_identity_unauthenticated",
                         "census_identity_unknown"):
                self.assertIn(code, reasons)


if __name__ == "__main__":
    unittest.main()
