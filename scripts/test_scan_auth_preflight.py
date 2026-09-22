#!/usr/bin/env python3
"""End-to-end coverage for the identity preflight and named scan failures.

A fleet lost hours to a supervisor that could say it was blind but never why.
Two of the causes look identical from outside: an exhausted allowance an
authenticated identity was actually issued, and an unauthenticated caller
being metered at the 60/hour anonymous ceiling shared by every host behind one
IP. These tests drive the real scanner and the real supervisor entry point
against fixture GitHub responses, and assert the two are told apart.

Every case comes in a pair. A preflight that only ever passes, or a 403 that
is always called a rate limit, would prove nothing about the case it exists
for.

Run:  python3 scripts/test_scan_auth_preflight.py
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts" / "assignment_scan.py"
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
BASE = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]

# GitHub serves this parenthetical only to an unauthenticated caller.
ANONYMOUS_403 = (
    "gh: API rate limit exceeded for 203.0.113.7. (But here's the good news: "
    "Authenticated requests get a higher rate limit. Check out the "
    "documentation for more details.) (HTTP 403)"
)
AUTHENTICATED_403 = "gh: API rate limit exceeded for user ID 4242. (HTTP 403)"

FAKE_GH = r'''#!/usr/bin/env python3
"""A gh that answers who is calling, then serves the queue from fixtures."""
import json
import os
import sys
from urllib.parse import urlparse

path = sys.argv[-1]
with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as handle:
    handle.write(path + "\n")

ceiling = int(os.environ["CORE_LIMIT"])
refusal = os.environ.get("PROBE_REFUSAL", "")
if path == "rate_limit" and refusal:
    print(refusal, file=sys.stderr)
    raise SystemExit(1)
if path == "rate_limit":
    print(json.dumps({"resources": {"core": {
        "limit": ceiling, "remaining": int(os.environ.get("CORE_REMAINING", "0")),
    }}}))
    raise SystemExit(0)

failure = os.environ.get("QUEUE_FAILURE", "")
if failure:
    print(failure, file=sys.stderr)
    raise SystemExit(1)

parsed = urlparse("https://example.invalid/" + path)
jobs = [{"id": 201, "status": "queued", "labels": LABELS + ["pulp-build-merge-group"]}]
if parsed.path.endswith("/actions/workflows"):
    print(json.dumps({"total_count": 1,
                      "workflows": [{"id": 99, "name": "Build and Test"}]}))
elif "/actions/workflows/99/runs" in parsed.path:
    runs = ([{"id": 101, "name": "Build and Test", "status": "queued",
              "created_at": "2026-08-25T00:00:00Z",
              "updated_at": "2026-08-25T00:00:00Z"}]
            if "status=queued" in path else [])
    page = "page=1" in path
    print(json.dumps({"total_count": len(runs),
                      "workflow_runs": runs if page else []}))
elif "/actions/runs/101/jobs" in parsed.path:
    page = "page=1" in path
    print(json.dumps({"total_count": len(jobs), "jobs": jobs if page else []}))
else:
    raise SystemExit("unexpected API path: " + path)
'''.replace("LABELS", repr(BASE))


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class ScannerHarness(unittest.TestCase):
    """Drive the real scanner CLI against a fake GitHub."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gh = self.root / "fake-gh"
        _write_exec(self.gh, FAKE_GH)
        _write_exec(self.root / "tart", "#!/usr/bin/env bash\nexit 0\n")
        self.call_log = self.root / "calls"
        self.call_log.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _env(
        self,
        core_limit: int,
        queue_failure: str = "",
        probe_refusal: str = "",
    ) -> dict[str, str]:
        base = [
            directory
            for directory in ("/bin", "/usr/bin", "/opt/homebrew/bin", "/usr/local/bin")
            if Path(directory).exists()
        ]
        return {
            "HOME": str(self.root),
            "PATH": os.pathsep.join([str(self.root), *base]),
            "CALL_LOG": str(self.call_log),
            "CORE_LIMIT": str(core_limit),
            "CORE_REMAINING": "0",
            "QUEUE_FAILURE": queue_failure,
            "PROBE_REFUSAL": probe_refusal,
        }

    def _scan(
        self,
        core_limit: int,
        queue_failure: str = "",
        probe_refusal: str = "",
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable, str(SCANNER),
                "--repo", "owner/repo",
                "--workflow", "Build and Test",
                "--labels", ",".join(BASE + ["pulp-build-merge-group"]),
                "--require-label", "pulp-build-merge-group",
                "--gh-cli", str(self.gh),
                "--observation-lock-file", str(self.root / "observation.lock"),
            ],
            capture_output=True, text=True, check=False,
            env=self._env(core_limit, queue_failure, probe_refusal),
        )

    def _calls(self) -> list[str]:
        return [
            line
            for line in self.call_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class ScanAuthPreflight(ScannerHarness):
    """An anonymous identity is refused; an authenticated one proceeds."""

    def test_anonymous_identity_is_refused_and_authenticated_proceeds(self) -> None:
        """The refusal is named, and it happens before the queue is read."""
        refused = self._scan(60)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("no_valid_credentials", refused.stderr)
        self.assertIn("unauthenticated", refused.stderr)
        self.assertEqual(
            self._calls(),
            ["rate_limit"],
            "an unauthenticated scan must stop at the identity probe rather "
            "than spend the shared anonymous allowance on queue pages",
        )

        self.call_log.write_text("", encoding="utf-8")
        admitted = self._scan(15000)
        self.assertEqual(admitted.returncode, 0, admitted.stderr)
        self.assertEqual(admitted.stdout.strip(), "1")
        self.assertGreater(
            len([path for path in self._calls() if "actions" in path]),
            0,
            "an authenticated scan must go on to read the queue",
        )

    def test_a_403_is_attributed_to_the_identity_that_hit_it(self) -> None:
        """60/hour is an authentication fault; 15000/hour is a rate limit."""
        anonymous = self._scan(15000, ANONYMOUS_403)
        self.assertEqual(anonymous.returncode, 2)
        self.assertIn("no_valid_credentials", anonymous.stderr)
        self.assertIn("not a capacity problem", anonymous.stderr)

        limited = self._scan(15000, AUTHENTICATED_403)
        self.assertEqual(limited.returncode, 2)
        self.assertIn("rate_limited", limited.stderr)
        self.assertIn("15000/hour", limited.stderr)
        self.assertNotIn(
            "no_valid_credentials",
            limited.stderr,
            "an identity that was issued 15000/hour and spent it has a "
            "capacity problem, not a credential one",
        )

    def test_an_authentication_fault_is_not_retried(self) -> None:
        """More attempts cannot authenticate, and each spends the shared 60."""
        self._scan(15000, ANONYMOUS_403)
        anonymous_attempts = len([p for p in self._calls() if "actions" in p])

        self.call_log.write_text("", encoding="utf-8")
        self._scan(15000, AUTHENTICATED_403)
        retried_attempts = len([p for p in self._calls() if "actions" in p])

        self.assertEqual(anonymous_attempts, 1)
        self.assertGreater(
            retried_attempts,
            anonymous_attempts,
            "a transient fault is still retried; only the authentication "
            "fault stops on the first attempt",
        )


class ProbeRefusalDoesNotBlindALane(ScannerHarness):
    """A CLI that will not answer the probe is not proof of anything.

    `ghapp` serves a path carrying a repository but refuses `rate_limit`
    unless it can derive provenance, and the fleet's lanes run from a home
    directory rather than a checkout. Reading that refusal as a failed
    preflight would blind every lane on the fleet -- the exact outage the
    preflight exists to prevent -- so it is carried as an unproven identity
    and the queue is still read.
    """

    # What the fleet's wrapper actually prints for an endpoint with no repo.
    PROVENANCE_REFUSAL = (
        "ghapp: exact repository provenance is required; use --repo OWNER/REPO"
    )

    def test_an_unanswerable_probe_still_reads_the_queue(self) -> None:
        served = self._scan(15000, probe_refusal=self.PROVENANCE_REFUSAL)
        self.assertEqual(served.returncode, 0, served.stderr)
        self.assertEqual(served.stdout.strip(), "1")
        self.assertIn("identity unproven", served.stderr)
        self.assertIn("cli_refused", served.stderr)
        # The refusal never reached GitHub, so it is asked once, not retried.
        self.assertEqual(
            len([path for path in self._calls() if path == "rate_limit"]),
            1,
        )

    def test_an_unproven_identity_still_names_an_anonymous_403(self) -> None:
        refused = self._scan(
            15000,
            queue_failure=ANONYMOUS_403,
            probe_refusal=self.PROVENANCE_REFUSAL,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("no_valid_credentials", refused.stderr)


class ReasonReachesTheSupervisor(unittest.TestCase):
    """The supervisor must receive the CAUSE, not just the failure.

    The supervisor captures the scanner's stderr and publishes it. What
    arrived there for six days of this incident was a rate-limit line that
    named no identity, so every reader spent the session auditing capacity
    instead of credentials. What must arrive is the reason code.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gh = self.root / "fake-gh"
        _write_exec(self.gh, FAKE_GH)
        _write_exec(self.root / "tart", "#!/usr/bin/env bash\nexit 0\n")
        self.call_log = self.root / "calls"
        self.call_log.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _select(
        self, core_limit: int, queue_failure: str = ""
    ) -> subprocess.CompletedProcess:
        base = [
            directory
            for directory in ("/bin", "/usr/bin", "/opt/homebrew/bin", "/usr/local/bin")
            if Path(directory).exists()
        ]
        env = {
            "HOME": str(self.root),
            "PATH": os.pathsep.join([str(self.root), *base]),
            "TART_HOME": str(self.root / "vms"),
            "TARTCI_STATE_DIR": str(self.root / "state"),
            "TARTCI_GH_CLI": str(self.gh),
            "CALL_LOG": str(self.call_log),
            "CORE_LIMIT": str(core_limit),
            "CORE_REMAINING": "0",
            "QUEUE_FAILURE": queue_failure,
            "TARTCI_QUEUE_STAGGER_MAX_SECS": "0",
            "TARTCI_RUNNER_LABELS": ",".join(BASE + ["pulp-gate-fast"]),
            "TARTCI_RUNNER_WORKFLOW_TIERS": "pulp-build-merge-group|Build and Test",
            "TARTCI_RUNNER_ASSIGNMENT_MODE": "event-class-v2",
        }
        return subprocess.run(
            ["bash", str(RUNNER), "--print-selection"],
            capture_output=True, text=True, check=False, env=env,
        )

    def _events(self) -> str:
        events = self.root / "state" / "events.jsonl"
        return events.read_text(encoding="utf-8") if events.exists() else ""

    def test_a_blind_scan_publishes_the_reason_it_went_blind(self) -> None:
        # The lane goes blind either way. What has to differ is whether the
        # supervisor is told which of the two causes it was.
        blind = self._select(60, ANONYMOUS_403)
        self.assertEqual(blind.returncode, 0, blind.stderr)
        self.assertTrue(blind.stdout.startswith("ERR"), blind.stdout)
        self.assertIn("assignment_scan_error", self._events())
        self.assertIn(
            "no_valid_credentials",
            self._events() + blind.stderr,
            "the supervisor received a failure with no cause attached; that "
            "omission is what makes a credential fault read as a quiet queue",
        )

    def test_a_serving_lane_publishes_no_credential_fault(self) -> None:
        """The control: the reason appears because it happened, not always."""
        serving = self._select(15000)
        self.assertEqual(serving.returncode, 0, serving.stderr)
        self.assertTrue(serving.stdout.startswith("1\t"), serving.stdout)
        self.assertNotIn("no_valid_credentials", self._events() + serving.stderr)
        self.assertNotIn("assignment_scan_error", self._events())


if __name__ == "__main__":
    unittest.main(verbosity=2)
