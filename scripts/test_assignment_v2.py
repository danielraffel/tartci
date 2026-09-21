#!/usr/bin/env python3
"""Behavioral coverage for exclusive V2 macOS JIT assignment classes."""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
SCANNER = ROOT / "scripts" / "assignment_scan.py"
TEMPLATE = ROOT / "launchd" / "com.danielraffel.pulp.tart-runner-macos.plist.template"
BASE = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]

# The scanners default their observation lock to the HOST's real one, so a test
# that forgets --observation-lock-file serializes against whatever fleet lanes
# are scanning on this machine -- adding contention to the exact resource these
# tests measure, and slowing a production lane to do it. Redirecting the
# default here covers every test in the run, including ones added later that
# forget the flag. An explicit environment value still wins, so a deliberate
# integration test can opt out.
os.environ.setdefault(
    "TARTCI_QUEUE_OBSERVATION_LOCK_FILE",
    str(Path(tempfile.mkdtemp(prefix="tartci-test-observation-")) / "queue-observation.lock"),
)
HOST_OBSERVATION_LOCK = Path.home() / ".tartci/state/queue-observation.lock"

# The workflow-id cache needs the same treatment for two reasons. Its default
# file is the HOST's shared one, so a test that forgets the flag would publish
# fixture workflow ids where every live fleet lane on this machine reads them,
# aiming production scans at a workflow that does not exist. And a cache shared
# between tests is a channel between them: one test's resolved id satisfies the
# next test's lookup, so the listing call that test was written to exercise is
# never made. Both are closed by defaulting the cache OFF; a test that wants it
# passes its own --workflow-id-cache-file and a non-zero TTL.
os.environ.setdefault(
    "TARTCI_ASSIGNMENT_WORKFLOW_ID_CACHE_FILE",
    str(Path(tempfile.mkdtemp(prefix="tartci-test-workflow-ids-")) / "workflow-ids.json"),
)
os.environ.setdefault("TARTCI_ASSIGNMENT_WORKFLOW_ID_CACHE_TTL_SECS", "0")
HOST_WORKFLOW_ID_CACHE = Path.home() / ".tartci/state/assignment-workflow-ids.json"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


FAKE_GH = r'''#!/usr/bin/env python3
import datetime as dt
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

state = json.load(open(os.environ["ASSIGNMENT_STATE"], encoding="utf-8"))
path = sys.argv[-1]
if state.get("api_fail") and "/runs?" in path:
    raise SystemExit(9)
parsed = urlparse("https://example.invalid/" + path)
query = parse_qs(parsed.query)
page = int(query.get("page", ["1"])[0])
status = query.get("status", [""])[0]

jobs = []
if state.get("merge"):
    jobs.append({"id": 201, "status": "queued", "labels": %s + ["pulp-build-merge-group"]})
if state.get("pr"):
    jobs.append({"id": 202, "status": "queued", "labels": %s + ["pulp-build-pr-head"]})
if state.get("legacy"):
    jobs.append({"id": 203, "status": "queued", "labels": %s + ["pulp-gate-fast"]})
if state.get("malformed"):
    jobs.append({"id": 204, "status": "queued", "labels": %s + [{"bad": "label"}]})

timestamp = (dt.datetime.now(dt.timezone.utc).strftime("%%Y-%%m-%%dT%%H:%%M:%%SZ")
             if state.get("fresh") else "2026-08-25T00:00:00Z")


def runs_page(run_id, name):
    runs = ([{"id": run_id, "name": name, "status": status,
              "created_at": timestamp, "updated_at": timestamp}]
            if status == "queued" and jobs else [])
    return json.dumps({"total_count": len(runs),
                       "workflow_runs": runs if page == 1 else []})


# Fail exactly one workflow's run listing, so a single tier can be made blind
# while every other tier stays observable. `api_fail` blinds them all at once.
blind = state.get("blind_workflow")
if blind and "/actions/workflows/" + str(blind) + "/runs" in parsed.path:
    raise SystemExit(9)

if parsed.path.endswith("/actions/workflows"):
    print(json.dumps({"total_count": 2, "workflows": [
        {"id": 99, "name": "Build and Test"},
        {"id": 98, "name": "Merge Gate"}]}))
elif "/actions/workflows/99/runs" in parsed.path or parsed.path.endswith("/actions/runs"):
    print(runs_page(101, "Build and Test"))
elif "/actions/workflows/98/runs" in parsed.path:
    print(runs_page(102, "Merge Gate"))
elif "/actions/runs/101/jobs" in parsed.path or "/actions/runs/102/jobs" in parsed.path:
    print(json.dumps({"total_count": len(jobs), "jobs": jobs if page == 1 else []}))
else:
    raise SystemExit("unexpected API path: " + path)
''' % (repr(BASE), repr(BASE), repr(BASE), repr(BASE))


class AssignmentV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _write_exec(self.root / "fake-gh", FAKE_GH)
        _write_exec(self.root / "tart", "#!/usr/bin/env bash\nexit 0\n")
        self.state = self.root / "assignment.json"
        self.env = {
            "HOME": str(self.root),
            "PATH": os.pathsep.join((str(self.root), "/bin", "/usr/bin")),
            "TART_HOME": str(self.root / "vms"),
            "TARTCI_STATE_DIR": str(self.root / "state"),
            "TARTCI_GH_CLI": "fake-gh",
            "ASSIGNMENT_STATE": str(self.state),
            "TARTCI_QUEUE_STAGGER_MAX_SECS": "0",
            "TARTCI_RUNNER_LABELS": ",".join(BASE + ["pulp-gate-fast"]),
            "TARTCI_RUNNER_WORKFLOW_TIERS": (
                "pulp-build-merge-group|Build and Test\n"
                "pulp-build-pr-head|Build and Test"
            ),
            "TARTCI_RUNNER_ASSIGNMENT_MODE": "event-class-v2",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _state(self, **values: bool) -> None:
        self.state.write_text(json.dumps(values), encoding="utf-8")
        for cache in (self.root / "state").glob("*.assignment-v2-selection.cache"):
            cache.unlink()

    def _runner(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(RUNNER), *args],
            text=True,
            capture_output=True,
            check=False,
            env=self.env,
        )

    def test_eligibility_matrix_and_v2_registration_labels(self) -> None:
        cases = (
            ({"merge": True}, "1", "pulp-build-merge-group"),
            ({"pr": True}, "1", "pulp-build-pr-head"),
            ({"legacy": True}, "0", None),
        )
        for state, count, expected_class in cases:
            with self.subTest(state=state):
                self._state(**state)
                result = self._runner("--print-selection")
                self.assertEqual(result.returncode, 0, result.stderr)
                fields = result.stdout.strip().split("\t")
                self.assertEqual(fields[0], count)
                labels = fields[1].split(",")
                self.assertNotIn("pulp-gate-fast", labels)
                if expected_class:
                    self.assertIn(expected_class, labels)
                    self.assertEqual(
                        len({"pulp-build-merge-group", "pulp-build-pr-head"} & set(labels)),
                        1,
                    )

    def test_observe_mode_reports_legacy_generic_match_and_v2_rejection(self) -> None:
        self._state(legacy=True)
        self.env["TARTCI_RUNNER_ASSIGNMENT_MODE"] = "observe"
        result = self._runner("--print-assignment-parity")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("legacy=1|", result.stdout)
        self.assertIn("v2=0|", result.stdout)

    def test_continuous_observe_is_rate_limited(self) -> None:
        self._state(legacy=True)
        self.env["TARTCI_RUNNER_ASSIGNMENT_MODE"] = "observe"
        first = self._runner("--print-selection")
        second = self._runner("--print-selection")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        events = list((self.root / "state").glob("events.jsonl"))
        self.assertEqual(len(events), 1)
        samples = [
            line for line in events[0].read_text(encoding="utf-8").splitlines()
            if '"event":"assignment_v2_observe"' in line
        ]
        self.assertEqual(len(samples), 1)

    def test_cancellation_and_higher_tier_arrival_deny_pre_mint(self) -> None:
        self._state(pr=True)
        selected = self._runner("--print-selection")
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertTrue(selected.stdout.startswith("1\t"))
        self._state()
        cancelled = self._runner("--print-pre-mint-selection", "1")
        self.assertEqual(cancelled.stdout.strip(), "0", cancelled.stderr)

        self._state(merge=True, pr=True)
        preempted = self._runner("--print-pre-mint-selection", "1")
        self.assertEqual(preempted.stdout.strip(), "0", preempted.stderr)

    def test_pre_mint_denial_invalidates_stale_class_and_falls_through(self) -> None:
        self._state(merge=True, pr=True)
        selected = self._runner("--print-selection")
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(selected.stdout.strip().split("\t")[2], "0")

        # Preserve the cached tier-0 selection while the live queue changes:
        # another host claimed the merge-group job, leaving PR-head work.
        self.state.write_text(json.dumps({"pr": True}), encoding="utf-8")
        denied = self._runner("--print-pre-mint-selection", "0")
        self.assertEqual(denied.returncode, 0, denied.stderr)
        self.assertEqual(denied.stdout.strip(), "0")

        replacement = self._runner("--print-selection")
        self.assertEqual(replacement.returncode, 0, replacement.stderr)
        fields = replacement.stdout.strip().split("\t")
        self.assertEqual(fields[0], "1")
        self.assertEqual(fields[2], "1")
        self.assertIn("pulp-build-pr-head", fields[1].split(","))

    def test_top_tier_may_reuse_only_a_fresh_exact_receipt(self) -> None:
        self.env["TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS"] = "180"
        self._state(merge=True)
        selected = self._runner("--print-selection")
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(selected.stdout.strip().split("\t")[2], "0")

        # Cancellation after observation may create one bounded idle tier-zero
        # runner, but it cannot invert priority or authorize a different class.
        self.state.write_text("{}", encoding="utf-8")
        admitted = self._runner("--print-pre-mint-selection", "0")
        self.assertEqual(admitted.stdout.strip(), "1", admitted.stderr)
        events = (self.root / "state" / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event":"assignment_v2_pre_mint_receipt"', events)

        cache = next((self.root / "state").glob("*.assignment-v2-selection.cache"))
        _stamp, value = cache.read_text(encoding="utf-8").split("\t", 1)
        cache.write_text(f"1\t{value}", encoding="utf-8")
        stale = self._runner("--print-pre-mint-selection", "0")
        self.assertEqual(stale.stdout.strip(), "0", stale.stderr)

    def test_top_tier_receipt_policy_is_bounded(self) -> None:
        self._state(merge=True)
        for value in ("-1", "301", "bad"):
            with self.subTest(value=value):
                self.env["TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS"] = value
                result = self._runner("--print-selection")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("TOP_TIER_RECEIPT_MAX_AGE_SECS", result.stderr)

    def test_lower_tier_never_reuses_receipt_when_higher_work_arrives(self) -> None:
        self.env["TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS"] = "180"
        self._state(pr=True)
        selected = self._runner("--print-selection")
        self.assertEqual(selected.stdout.strip().split("\t")[2], "1", selected.stderr)

        self.state.write_text(json.dumps({"merge": True, "pr": True}), encoding="utf-8")
        denied = self._runner("--print-pre-mint-selection", "1")
        self.assertEqual(denied.stdout.strip(), "0", denied.stderr)

    # --- Shipyard push-feed rescue --------------------------------------

    def _feed_daemon(self, *, registered: bool = True, jobs=(), fresh: bool = True):
        """A Unix-socket stand-in for the local Shipyard daemon."""
        sock_path = self.root / "daemon.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(8)
        stop = threading.Event()
        status = {
            "type": "status",
            "registered_repos": ["generous-corp/pulp"] if registered else [],
            "configured_repos": ["generous-corp/pulp"],
            "last_event_at": time.time() if fresh else time.time() - 99999,
            "subscribers": 0,
            "last_error": None,
        }

        def client(conn):
            try:
                conn.settimeout(2.0)
                conn.sendall(
                    json.dumps({"protocol": 3, "type": "hello"}).encode() + b"\n"
                )
                kind = json.loads(conn.makefile("rb").readline() or b"{}").get("type")
                if kind == "status":
                    conn.sendall(json.dumps(status).encode() + b"\n")
                elif kind == "subscribe":
                    for payload in jobs:
                        conn.sendall(
                            json.dumps(
                                {"kind": "workflow_job", "payload": payload}
                            ).encode()
                            + b"\n"
                        )
            except OSError:
                pass
            finally:
                conn.close()

        def serve():
            server.settimeout(0.3)
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except (socket.timeout, OSError):
                    continue
                threading.Thread(target=client, args=(conn,), daemon=True).start()

        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(stop.set)
        self.addCleanup(server.close)
        self.env["TARTCI_SHIPYARD_DAEMON_SOCKET"] = str(sock_path)
        return sock_path

    @staticmethod
    def _queued_job(label: str = "pulp-build-merge-group", job_id: int = 501) -> dict:
        return {
            "repo": "Generous-Corp/pulp",
            "status": "queued",
            "job_id": job_id,
            "run_id": 900,
            "labels": BASE + [label],
        }

    def _events(self) -> str:
        path = self.root / "state" / "events.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_feed_rescues_a_blind_scan_when_the_lane_opts_in(self) -> None:
        """A failed scan plus an independent webhook witness is still demand."""
        self._state(api_fail=True)
        self.env["TARTCI_ASSIGNMENT_FEED_RESCUE"] = "1"
        self._feed_daemon(jobs=[self._queued_job()])
        result = self._runner("--print-selection")
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = result.stdout.strip().split("\t")
        self.assertEqual(fields[0], "1", result.stdout)
        self.assertIn("pulp-build-merge-group", fields[1].split(","))
        self.assertIn('"event":"assignment_feed_rescue"', self._events())

    def test_a_severed_feed_leaves_the_blind_verdict_and_says_so(self) -> None:
        """No events must never become `no work`: the lane stays blind."""
        self._state(api_fail=True)
        self.env["TARTCI_ASSIGNMENT_FEED_RESCUE"] = "1"
        self._feed_daemon(jobs=[])
        result = self._runner("--print-selection")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split("\t", 1)[0], "ERR")
        events = self._events()
        self.assertIn('"event":"assignment_feed_degraded"', events)
        self.assertIn('"event":"assignment_scan_error"', events)

    def test_an_unregistered_repo_cannot_rescue(self) -> None:
        self._state(api_fail=True)
        self.env["TARTCI_ASSIGNMENT_FEED_RESCUE"] = "1"
        self._feed_daemon(registered=False, jobs=[self._queued_job()])
        result = self._runner("--print-selection")
        self.assertEqual(result.stdout.split("\t", 1)[0], "ERR", result.stderr)
        self.assertIn('"event":"assignment_feed_degraded"', self._events())

    def test_a_stale_daemon_cannot_rescue(self) -> None:
        self._state(api_fail=True)
        self.env["TARTCI_ASSIGNMENT_FEED_RESCUE"] = "1"
        self._feed_daemon(fresh=False, jobs=[self._queued_job()])
        result = self._runner("--print-selection")
        self.assertEqual(result.stdout.split("\t", 1)[0], "ERR", result.stderr)

    def test_an_absent_daemon_socket_cannot_rescue(self) -> None:
        self._state(api_fail=True)
        self.env["TARTCI_ASSIGNMENT_FEED_RESCUE"] = "1"
        self.env["TARTCI_SHIPYARD_DAEMON_SOCKET"] = str(self.root / "nowhere.sock")
        result = self._runner("--print-selection")
        self.assertEqual(result.stdout.split("\t", 1)[0], "ERR", result.stderr)
        self.assertIn('"event":"assignment_feed_degraded"', self._events())

    def test_a_witness_younger_than_the_lane_minimum_cannot_rescue(self) -> None:
        self._state(api_fail=True)
        self.env["TARTCI_ASSIGNMENT_FEED_RESCUE"] = "1"
        self.env["TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS"] = "600"
        self._feed_daemon(jobs=[self._queued_job()])
        result = self._runner("--print-selection")
        self.assertEqual(result.stdout.split("\t", 1)[0], "ERR", result.stderr)

    def test_the_rescue_is_off_unless_the_lane_opts_in(self) -> None:
        self._state(api_fail=True)
        self._feed_daemon(jobs=[self._queued_job()])
        result = self._runner("--print-selection")
        self.assertEqual(result.stdout.split("\t", 1)[0], "ERR", result.stderr)
        self.assertNotIn("assignment_feed", self._events())

    def test_a_healthy_scan_never_consults_the_feed(self) -> None:
        """The rescue is additive: a working lane must not change at all."""
        self._state(merge=True)
        self.env["TARTCI_ASSIGNMENT_FEED_RESCUE"] = "1"
        self._feed_daemon(jobs=[self._queued_job()])
        result = self._runner("--print-selection")
        self.assertEqual(result.stdout.strip().split("\t")[0], "1", result.stderr)
        self.assertNotIn("assignment_feed", self._events())

    def test_api_failure_denies_selection(self) -> None:
        self._state(api_fail=True)
        result = self._runner("--print-selection")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split("\t", 1)[0], "ERR")
        events = (self.root / "state" / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event":"assignment_scan_error"', events)
        self.assertIn("scanner_rc=2", events)
        self.assertIn("GitHub API failed", events)

    # Two tiers on DISTINCT workflows, so exactly one of them can be blinded.
    # Sharing a workflow (as every other test here does) makes both tiers issue
    # identical API calls, which cannot separate "the top tier is blind" from
    # "every tier is blind" -- the distinction this pair exists to draw.
    SPLIT_TIERS = (
        "pulp-build-merge-group|Merge Gate\n"
        "pulp-build-pr-head|Build and Test"
    )

    def _split_tier_runner(self, **values: bool) -> subprocess.CompletedProcess[str]:
        self.env["TARTCI_RUNNER_WORKFLOW_TIERS"] = self.SPLIT_TIERS
        self._state(**values)
        return self._runner("--print-selection")

    def test_a_blind_top_tier_never_hands_its_capacity_to_a_lower_class(self) -> None:
        """A scan error at any tier is fail-closed: a blind higher class is never
        skipped so a lower one can take its capacity.

        The top tier's demand is UNKNOWN, not zero. Electing pr-head here would
        mint a runner that cannot serve merge-group work, and -- because the
        supervisor resets its scan-blind counter on any numeric verdict -- would
        also hide the top tier's blindness from the self-heal that exists to
        recover it. Unknown must stay unknown all the way out of the function.
        """
        # blind_workflow=98 is Merge Gate, tier 0. Tier 1 has REAL demand (pr).
        result = self._split_tier_runner(pr=True, fresh=True, blind_workflow=98)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split("\t", 1)[0],
            "ERR",
            "a blind top tier must not elect the lower class: " + result.stdout,
        )
        self.assertNotIn(
            "pulp-build-pr-head",
            result.stdout,
            "the lower class must not be advertised on a blind verdict",
        )

    def test_the_blind_top_tier_control_can_elect_the_lower_class(self) -> None:
        """Control for the test above: same split-workflow config, nothing blind.

        Without this the ERR assertion is vacuous -- it would pass just as well
        if the harness could never reach or elect tier 1 at all. Here tier 0 is
        observable and genuinely empty, so tier 1 MUST win with a real count.
        """
        result = self._split_tier_runner(pr=True, fresh=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        count, labels = result.stdout.split("\t")[0], result.stdout.split("\t")[1]
        self.assertEqual(count, "1", result.stdout)
        self.assertIn("pulp-build-pr-head", labels, result.stdout)

    def test_blind_selection_is_never_published_to_the_selection_cache(self) -> None:
        """A blind scan is an absence of observation, not an observation of absence.

        Publishing `ERR` into the positive selection cache replays one transient
        GitHub failure as a blind verdict for the whole cache TTL. During that
        window the lane performs no observation at all, so it cannot recover on
        the next poll, and a supervisor that restarts for fresh credentials
        re-reads the same stale blind verdict from disk.
        """
        self._state(api_fail=True)
        blind = self._runner("--print-selection")
        self.assertEqual(blind.returncode, 0, blind.stderr)
        self.assertEqual(blind.stdout.split("\t", 1)[0], "ERR")
        # Assert the ABSENCE positively. Iterating the glob and asserting
        # `ERR not in <contents>` passes vacuously on the fixed code, because no
        # cache file is written at all -- the loop body never runs, and a test
        # whose assertion cannot execute is not a control.
        self.assertEqual(
            sorted((self.root / "state").glob("*.assignment-v2-selection.cache")),
            [],
            "a blind verdict created a selection cache",
        )

        # GitHub recovers. Rewrite the scanner state directly rather than via
        # _state(), which clears the cache and would hide the replay under test.
        self.state.write_text(json.dumps({"merge": True}), encoding="utf-8")
        recovered = self._runner("--print-selection")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(recovered.stdout.strip().split("\t")[0], "1")
        # ...and a real observation IS published, so the guard rejects only the
        # blind verdict rather than disabling the cache outright.
        caches = sorted((self.root / "state").glob("*.assignment-v2-selection.cache"))
        self.assertEqual(len(caches), 1, "recovery published no selection cache")
        self.assertRegex(
            caches[0].read_text(encoding="utf-8"), r"^\d+\t1\|", "cached value is not a real observation"
        )

    def test_a_blind_scan_leaves_an_expired_numeric_entry_byte_identical(self) -> None:
        """Declining to cache must not mutate the entry already on disk.

        The fix returns early rather than overwriting, so an expired numeric
        entry survives a blind poll untouched. That is safe only because the
        reader rejects it on age -- if it were ever replayed, declining to write
        would have traded a blind verdict for a stale positive one, which is
        worse. Assert the bytes, so a future refactor that "helpfully" refreshes
        the timestamp is caught here.
        """
        self._state(merge=True)
        seeded = self._runner("--print-selection")
        self.assertEqual(seeded.returncode, 0, seeded.stderr)
        caches = sorted((self.root / "state").glob("*.assignment-v2-selection.cache"))
        self.assertEqual(len(caches), 1, "no selection cache to seed the expiry case")
        cache = caches[0]
        # Backdate well past any TTL so the entry is expired, not merely old.
        stale = "1\t" + cache.read_text(encoding="utf-8").split("\t", 1)[1]
        cache.write_text(stale, encoding="utf-8")

        self.state.write_text(json.dumps({"api_fail": True}), encoding="utf-8")
        blind = self._runner("--print-selection")
        self.assertEqual(blind.returncode, 0, blind.stderr)
        self.assertEqual(blind.stdout.split("\t", 1)[0], "ERR")
        self.assertEqual(
            cache.read_text(encoding="utf-8"), stale, "a blind poll rewrote the cached entry"
        )

    def test_malformed_label_element_denies_selection(self) -> None:
        self._state(malformed=True)
        result = self._runner("--print-selection")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split("\t", 1)[0], "ERR")

    def test_v2_preserves_delayed_fallback_minimum_queue_age(self) -> None:
        self._state(merge=True, fresh=True)
        self.env["TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS"] = "300"
        delayed = self._runner("--print-selection")
        self.assertEqual(delayed.returncode, 0, delayed.stderr)
        self.assertEqual(delayed.stdout.split("\t", 1)[0], "0")

    def test_print_queue_uses_v2_eligibility(self) -> None:
        self._state(legacy=True)
        legacy_only = self._runner("--print-queue")
        self.assertEqual(legacy_only.returncode, 0, legacy_only.stderr)
        self.assertEqual(legacy_only.stdout.strip(), "0")
        self._state(merge=True, pr=True)
        event_jobs = self._runner("--print-queue")
        self.assertEqual(event_jobs.returncode, 0, event_jobs.stderr)
        self.assertEqual(event_jobs.stdout.strip(), "2")

    def test_two_hosts_can_observe_same_job_without_shared_identity(self) -> None:
        self._state(merge=True)
        first = self._runner("--print-selection")
        self.env["TARTCI_RUNNER_SLOT"] = "2"
        second = self._runner("--print-selection")
        self.assertTrue(first.stdout.startswith("1\t"), first.stderr)
        self.assertTrue(second.stdout.startswith("1\t"), second.stderr)
        first_name = self._runner("--print-boot-name", "1").stdout.strip()
        self.env["TARTCI_RUNNER_SLOT"] = "3"
        second_name = self._runner("--print-boot-name", "1").stdout.strip()
        self.assertNotEqual(first_name, second_name)

    def test_template_is_legacy_default_and_reversible(self) -> None:
        body = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("<key>TARTCI_RUNNER_ASSIGNMENT_MODE</key>\n        <string>legacy</string>", body)
        self.assertNotIn("<key>TARTCI_VM_LEASE_PRIORITY</key>", body)
        self.assertIn("TARTCI_ASSIGNMENT_V2_OMIT_LABELS", body)

    def test_v2_rejects_retained_legacy_selector(self) -> None:
        self._state(merge=True)
        self.env["TARTCI_ASSIGNMENT_V2_OMIT_LABELS"] = "pulp-other"
        result = self._runner("--print-name")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retained required legacy selector", result.stderr)


class AssignmentScannerPaginationTests(unittest.TestCase):
    def test_run_job_scans_use_bounded_parallel_workers(self) -> None:
        spec = spec_from_file_location("assignment_scan_under_test", SCANNER)
        assert spec is not None and spec.loader is not None
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        scanner = module.AssignmentScanner.__new__(module.AssignmentScanner)
        scanner.args = Namespace(max_workers=3, workflow_id_cache_ttl=0)
        scanner._observation_lock = lambda: contextlib.nullcontext()
        scanner._cached_workflow_ids = lambda: {"Build and Test": 99}
        scanner._ordered_run_listings = lambda _ids: ["runs?status=queued"]
        scanner._walk_listing = (
            lambda _prefix, _key, visit: visit([{"id": run_id} for run_id in range(6)])
        )
        # This scanner is built with __new__, so it carries none of __init__'s
        # state. No stubbed run reports a witness, which is what keeps this an
        # exhaustive-sum assertion.
        scanner.witness = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0

        def scan_run(_run: dict[str, int]) -> int:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return 1

        scanner._scan_run = scan_run
        self.assertEqual(scanner.scan(), 6)
        self.assertEqual(peak, 3)

    def test_cross_process_assignment_scans_share_host_observation_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "entered"
            lock = root / "host-observation.lock"
            fake = root / "slow-gh"
            _write_exec(
                fake,
                """#!/usr/bin/env python3
import json, os, time
from pathlib import Path
Path(os.environ['ENTERED']).write_text('yes', encoding='utf-8')
time.sleep(2)
print(json.dumps({'total_count': 0, 'workflows': []}))
""",
            )
            common = [
                "python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                "--workflow", "Build and Test", "--labels", "pulp-build-merge-group",
                "--require-label", "pulp-build-merge-group", "--gh-cli", str(fake),
                "--observation-lock-file", str(lock), "--scan-timeout", "10",
            ]
            env = {**os.environ, "ENTERED": str(marker)}
            holder = subprocess.Popen(
                [*common, "--observation-lock-timeout", "5"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "first scanner never entered GitHub observation")
            denied = subprocess.run(
                [*common, "--observation-lock-timeout", "0.05"],
                text=True, capture_output=True, check=False, env=env,
            )
            self.assertEqual(denied.returncode, 2)
            self.assertIn("observation lock timed out", denied.stderr)
            holder.terminate()
            holder.communicate(timeout=5)

    # --- the scan budget must not be spent waiting for the lock -------------
    #
    # --scan-timeout budgets the scan. The wait for the host-global observation
    # lock is not scanning: it is queueing behind every other lane on the host,
    # a quantity this scanner does not control. Charging the wait to the scan
    # made the two nest, so a contended lane ran its exhaustive pass on
    # whatever was left and handed that remainder to `gh` as a shortened
    # per-call timeout -- and reported the queue unobservable, which is the one
    # verdict that idles a lane with work waiting.

    _HANGING_JOBS_GH = """#!/usr/bin/env python3
import json, sys, time
from urllib.parse import parse_qs, urlparse
p = urlparse('https://x/' + sys.argv[-1]); q = parse_qs(p.query)
if p.path.endswith('/actions/workflows'):
    print(json.dumps({'total_count': 1, 'workflows': [{'id': 99, 'name': 'Build and Test'}]}))
elif '/actions/workflows/99/runs' in p.path:
    if q.get('status', [''])[0] != 'queued':
        print(json.dumps({'total_count': 0, 'workflow_runs': []}))
    else:
        print(json.dumps({'total_count': 1, 'workflow_runs': [{'id': 7, 'name': 'Build and Test'}]}))
elif '/actions/runs/' in p.path and p.path.endswith('/jobs'):
    time.sleep(9999)
else:
    raise SystemExit(4)
"""

    @staticmethod
    def _hold_lock(lock: Path, seconds: float) -> subprocess.Popen:
        """Own the observation lock for `seconds`, and prove it before returning."""
        holder = subprocess.Popen(
            ["python3", "-c",
             "import fcntl,sys,time\n"
             "fh=open(sys.argv[1],'a+')\n"
             "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
             "sys.stderr.write('held\\n'); sys.stderr.flush()\n"
             "time.sleep(float(sys.argv[2]))\n",
             str(lock), str(seconds)],
            text=True, stderr=subprocess.PIPE,
        )
        assert holder.stderr is not None
        if holder.stderr.readline().strip() != "held":
            holder.kill()
            raise AssertionError("the lock holder never acquired the lock")
        return holder

    def _budget_after_waiting(
        self, hold: float, scan_timeout: int, lock_timeout: float
    ) -> tuple[float, float]:
        """Return (budget the scan had left, wall time the scanner took).

        The scanner clamps every GitHub call to `min(--gh-timeout, remaining)`.
        Setting --gh-timeout far above --scan-timeout makes that clamp exactly
        `remaining`, and the bounded runner reports it verbatim, so the number
        in the failure text IS the budget the scan had when it started work.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "isolated-observation.lock"
            lock.touch()
            gh = root / "hanging-jobs-gh"
            _write_exec(gh, self._HANGING_JOBS_GH)
            holder = self._hold_lock(lock, hold)
            started = time.monotonic()
            # The scanner must enforce its own deadline. This ceiling only
            # stops a broken one hanging the suite, and reports it as the
            # boundedness failure it is rather than as a harness timeout.
            ceiling = hold + scan_timeout + 10
            # Its own session, so blowing the ceiling reaps the never-answering
            # `gh` stand-in too rather than leaving a 9999s sleeper behind.
            scan = subprocess.Popen(
                ["python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                 "--workflow", "Build and Test", "--labels", "pulp-build-merge-group",
                 "--require-label", "pulp-build-merge-group", "--gh-cli", str(gh),
                 "--observation-lock-file", str(lock),
                 "--observation-lock-timeout", str(lock_timeout),
                 "--scan-timeout", str(scan_timeout), "--gh-timeout", "600",
                 "--api-retries", "0", "--pagination-retries", "0"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
            overran = False
            try:
                _, stderr = scan.communicate(timeout=ceiling)
            except subprocess.TimeoutExpired:
                overran = True
                stderr = ""
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(scan.pid), signal.SIGKILL)
                scan.communicate(timeout=10)
            finally:
                elapsed = time.monotonic() - started
                holder.kill()
                holder.communicate(timeout=5)
            if overran:
                self.fail(
                    f"the scan never ended: it outlived its {hold}s lock wait plus its "
                    f"{scan_timeout}s budget by more than 10s, so its deadline is not enforced"
                )
            match = re.search(r"timed out after ([0-9.]+)s", stderr)
            self.assertIsNotNone(
                match,
                "the scan never reached a GitHub call, so it measured no budget: "
                f"rc={scan.returncode} stderr={stderr!r}",
            )
            assert match is not None
            return float(match.group(1)), elapsed

    def test_scan_budget_is_measured_from_lock_acquisition(self) -> None:
        hold, scan_timeout = 2.0, 4
        budget, elapsed = self._budget_after_waiting(hold, scan_timeout, lock_timeout=20.0)
        # Control: a run where the scanner never actually queued would prove
        # nothing about what queueing costs it.
        self.assertGreaterEqual(
            elapsed, hold,
            f"the scanner never waited for the lock ({elapsed:.2f}s < {hold}s), so this proved nothing",
        )
        self.assertGreater(
            budget, scan_timeout - hold + 0.5,
            f"the {hold}s lock wait was charged to the scan: it began work with only "
            f"{budget:.2f}s of its {scan_timeout}s budget",
        )
        # A budget larger than the one configured would mean the deadline is
        # being extended rather than rebased.
        self.assertLessEqual(
            budget, scan_timeout + 0.5,
            f"the scan claimed {budget:.2f}s against a {scan_timeout}s budget",
        )

    def test_waiting_longer_for_the_lock_does_not_shrink_the_scan(self) -> None:
        """The same scan, queued twice as long, must get the same budget."""
        scan_timeout = 4
        brief, _ = self._budget_after_waiting(1.0, scan_timeout, lock_timeout=20.0)
        patient, elapsed = self._budget_after_waiting(3.0, scan_timeout, lock_timeout=20.0)
        self.assertGreaterEqual(elapsed, 3.0, "the long-wait case never queued")
        self.assertAlmostEqual(
            brief, patient, delta=0.75,
            msg=f"budget tracks the lock wait: 1s wait -> {brief:.2f}s, 3s wait -> {patient:.2f}s",
        )

    def test_lock_wait_is_not_truncated_by_the_scan_budget(self) -> None:
        """--observation-lock-timeout means what it says, even when it exceeds --scan-timeout.

        Nesting silently capped the wait at the scan budget, so a lane
        configured to wait 15s gave up after 3 -- and said "timed out after
        15.0s" while doing it.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "isolated-observation.lock"
            lock.touch()
            gh = root / "empty-queue-gh"
            _write_exec(
                gh,
                """#!/usr/bin/env python3
import json, sys
from urllib.parse import parse_qs, urlparse
p = urlparse('https://x/' + sys.argv[-1])
if p.path.endswith('/actions/workflows'):
    print(json.dumps({'total_count': 1, 'workflows': [{'id': 99, 'name': 'Build and Test'}]}))
else:
    print(json.dumps({'total_count': 0, 'workflow_runs': [], 'jobs': []}))
""",
            )
            holder = self._hold_lock(lock, 5.0)
            try:
                result = subprocess.run(
                    ["python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                     "--workflow", "Build and Test", "--labels", "pulp-build-merge-group",
                     "--require-label", "pulp-build-merge-group", "--gh-cli", str(gh),
                     "--observation-lock-file", str(lock),
                     "--observation-lock-timeout", "15", "--scan-timeout", "3"],
                    text=True, capture_output=True, check=False, timeout=60,
                )
            finally:
                holder.kill()
                holder.communicate(timeout=5)
            self.assertEqual(
                result.returncode, 0,
                "a 15s lock wait was cut short by a 3s scan budget: " + result.stderr,
            )
            self.assertEqual(result.stdout.strip(), "0")

    def test_a_scan_stays_bounded_by_its_lock_wait_plus_its_budget(self) -> None:
        """Rebasing the deadline must not make the deadline optional.

        The supervisor's poll loop blocks on this scanner, so an unbounded scan
        stops the lane observing at all.
        """
        hold, scan_timeout = 2.0, 3
        _, elapsed = self._budget_after_waiting(hold, scan_timeout, lock_timeout=20.0)
        self.assertLess(
            elapsed, hold + scan_timeout + 5.0,
            f"the scan ran {elapsed:.2f}s, past its {hold}s wait plus {scan_timeout}s budget",
        )

    def test_tests_never_contend_with_the_hosts_real_observation_lock(self) -> None:
        """Guard the module-level redirect: this suite runs on live fleet hosts."""
        # Resolve it the way the scanner does, so deleting the redirect fails
        # this test rather than silently falling back to the host's lock.
        effective = Path(
            os.environ.get("TARTCI_QUEUE_OBSERVATION_LOCK_FILE") or HOST_OBSERVATION_LOCK
        ).resolve()
        self.assertNotEqual(
            effective, HOST_OBSERVATION_LOCK.resolve(),
            "the test suite is pointed at the host's production observation lock",
        )

    def test_non_finite_observation_timeout_is_rejected(self) -> None:
        result = subprocess.run(
            [
                "python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                "--workflow", "Build and Test", "--labels", "pulp-build-pr-head",
                "--require-label", "pulp-build-pr-head",
                "--observation-lock-timeout", "inf",
            ],
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("observation-lock-timeout must be positive", result.stderr)

    def test_run_and_job_pages_are_exhaustive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fake-gh"
            _write_exec(
                fake,
                """#!/usr/bin/env python3
import json, sys
from urllib.parse import parse_qs, urlparse
p = urlparse('https://x/' + sys.argv[-1]); q = parse_qs(p.query); page = int(q.get('page', ['1'])[0]); status = q.get('status', [''])[0]
if p.path.endswith('/actions/workflows'):
    print(json.dumps({'total_count': 1, 'workflows': [{'id': 99, 'name': 'Build and Test'}]}))
elif '/actions/workflows/99/runs' in p.path:
    if status != 'queued': print(json.dumps({'total_count': 0, 'workflow_runs': []}))
    elif page == 1: print(json.dumps({'total_count': 101, 'workflow_runs': [{'id': i, 'name': 'Other'} for i in range(1, 101)]}))
    else: print(json.dumps({'total_count': 101, 'workflow_runs': [{'id': 101, 'name': 'Build and Test'}]}))
elif '/actions/runs/' in p.path and p.path.endswith('/jobs'):
    run_id = int(p.path.split('/actions/runs/', 1)[1].split('/', 1)[0])
    if run_id != 101: print(json.dumps({'total_count': 0, 'jobs': []}))
    elif page == 1: print(json.dumps({'total_count': 101, 'jobs': [{'id': i, 'status': 'completed', 'labels': []} for i in range(1, 101)]}))
    else: print(json.dumps({'total_count': 101, 'jobs': [{'id': 101, 'status': 'queued', 'labels': %s}]}))
else: raise SystemExit(4)
""" % repr(BASE + ["pulp-build-merge-group"]),
            )
            result = subprocess.run(
                [
                    "python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                    "--workflow", "Build and Test", "--labels", ",".join(BASE + ["pulp-build-merge-group"]),
                    "--require-label", "pulp-build-merge-group", "--gh-cli", str(fake),
                ],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")

    def test_pagination_cap_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake-gh"
            _write_exec(
                fake,
                """#!/usr/bin/env python3
import json, sys
if '/actions/workflows?' in sys.argv[-1]:
    print(json.dumps({'total_count': 1, 'workflows': [{'id': 99, 'name': 'Build and Test'}]}))
elif '/jobs' in sys.argv[-1]:
    print(json.dumps({'total_count': 0, 'jobs': []}))
else:
    print(json.dumps({'total_count': 200, 'workflow_runs': [{'id': i} for i in range(100)]}))
""",
            )
            result = subprocess.run(
                [
                    "python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                    "--workflow", "Build and Test", "--labels", "pulp-build-merge-group",
                    "--require-label", "pulp-build-merge-group", "--gh-cli", str(fake),
                    "--max-pages", "1",
                ],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("pagination truncated", result.stderr)

    def test_short_page_before_total_count_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake-gh"
            _write_exec(
                fake,
                """#!/usr/bin/env python3
import json, sys
if '/actions/workflows?' in sys.argv[-1]:
    print(json.dumps({'total_count': 1, 'workflows': [{'id': 99, 'name': 'Build and Test'}]}))
else:
    print(json.dumps({'total_count': 2, 'workflow_runs': []}))
""",
            )
            result = subprocess.run(
                [
                    "python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                    "--workflow", "Build and Test", "--labels", "pulp-build-merge-group",
                    "--require-label", "pulp-build-merge-group", "--gh-cli", str(fake),
                ],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("ended before total_count", result.stderr)

    def test_duplicate_workflow_display_name_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake-gh"
            _write_exec(
                fake,
                "#!/usr/bin/env python3\nimport json\n"
                "print(json.dumps({'total_count': 2, 'workflows': "
                "[{'id': 1, 'name': 'Build and Test'}, {'id': 2, 'name': 'Build and Test'}]}))\n",
            )
            result = subprocess.run(
                [
                    "python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                    "--workflow", "Build and Test", "--labels", "pulp-build-merge-group",
                    "--require-label", "pulp-build-merge-group", "--gh-cli", str(fake),
                ],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("resolved to 2 ids", result.stderr)

    def test_duplicate_id_from_queue_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake-gh"
            _write_exec(
                fake,
                """#!/usr/bin/env python3
import json, sys
from urllib.parse import parse_qs, urlparse
p = urlparse('https://x/' + sys.argv[-1]); page = int(parse_qs(p.query).get('page', ['1'])[0])
if p.path.endswith('/actions/workflows'):
    print(json.dumps({'total_count': 1, 'workflows': [{'id': 99, 'name': 'Build and Test'}]}))
elif '/actions/workflows/99/runs' in p.path and 'status=queued' in p.query:
    start = 1 if page == 1 else 100
    print(json.dumps({'total_count': 101, 'workflow_runs': [{'id': i} for i in range(start, start + (100 if page == 1 else 1))]}))
elif '/jobs' in p.path:
    print(json.dumps({'total_count': 0, 'jobs': []}))
else:
    print(json.dumps({'total_count': 0, 'workflow_runs': []}))
""",
            )
            result = subprocess.run(
                [
                    "python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                    "--workflow", "Build and Test", "--labels", "pulp-build-merge-group",
                    "--require-label", "pulp-build-merge-group", "--gh-cli", str(fake),
                ],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate id", result.stderr)


class AssignmentScannerTransientFaultTests(unittest.TestCase):
    """A fault that carries no verdict about the queue must not end the scan.

    Unless a test asks for `match=0`, every scan here presents exactly one
    queued job matching the tier, so the only correct complete answer is "1". A
    scan that reports 0 has under-counted the queue and would idle a lane that
    has work; a scan that exits non-zero has failed closed, which is correct
    only when the queue truly could not be observed. The `match=0` cell is the
    control: with nothing to find, the same fault must be survived by looking
    everywhere rather than by stopping early.
    """

    #: One `gh` stub, parameterised by which call it should sabotage and how
    #: many times. It records every request so a test can prove the retry
    #: actually re-issued the failed call rather than skipping it.
    _GH = '''#!/usr/bin/env python3
import json, os, sys
from urllib.parse import parse_qs, urlparse

FAIL_ON = os.environ["FAKE_GH_FAIL_ON"]
FAIL_TIMES = int(os.environ["FAKE_GH_FAIL_TIMES"])
LEDGER = os.environ["FAKE_GH_LEDGER"]
MODE = os.environ.get("FAKE_GH_MODE", "transport")
MATCH = os.environ.get("FAKE_GH_MATCH", "1") == "1"

target = sys.argv[-1]
with open(LEDGER, "a") as handle:
    handle.write(target + "\\n")
with open(LEDGER) as handle:
    seen = [line for line in handle.read().splitlines() if FAIL_ON in line]

p = urlparse("https://x/" + target)
q = parse_qs(p.query)
page = int(q.get("page", ["1"])[0])
status = q.get("status", [""])[0]
sabotage = FAIL_ON in target and len(seen) <= FAIL_TIMES

if sabotage and MODE == "transport":
    # What production emitted 543 times: gh itself fails, stdout is empty.
    sys.stderr.write("net/http: TLS handshake timeout\\n")
    raise SystemExit(1)

if p.path.endswith("/actions/workflows"):
    print(json.dumps({"total_count": 1, "workflows": [{"id": 99, "name": "Build and Test"}]}))
elif "/actions/workflows/99/runs" in p.path:
    if status != "queued":
        print(json.dumps({"total_count": 0, "workflow_runs": []}))
    elif sabotage and MODE == "race":
        # A run left `queued` between the count and the body: total_count says
        # two, the page carries one, and the page is short. Production called
        # this "pagination ended before total_count (N < M)" 231 times.
        print(json.dumps({"total_count": 2, "workflow_runs": [{"id": 101, "name": "Build and Test"}]}))
    else:
        print(json.dumps({"total_count": 1, "workflow_runs": [{"id": 101, "name": "Build and Test"}]}))
elif "/actions/runs/" in p.path and p.path.endswith("/jobs"):
    labels = %s if MATCH else []
    print(json.dumps({"total_count": 1, "jobs": [
        {"id": 1, "status": "queued" if MATCH else "in_progress", "labels": labels}]}))
else:
    raise SystemExit(4)
''' % repr(BASE + ["pulp-build-merge-group"])

    def _scan(self, fail_on: str, fail_times: int, mode: str = "transport",
              extra: list[str] | None = None,
              match: int = 1) -> tuple[subprocess.CompletedProcess, list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fake-gh"
            ledger = root / "ledger"
            ledger.write_text("")
            _write_exec(fake, self._GH)
            environment = dict(
                os.environ,
                FAKE_GH_FAIL_ON=fail_on,
                FAKE_GH_FAIL_TIMES=str(fail_times),
                FAKE_GH_LEDGER=str(ledger),
                FAKE_GH_MODE=mode,
                FAKE_GH_MATCH=str(match),
            )
            result = subprocess.run(
                [
                    "python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                    "--workflow", "Build and Test",
                    "--labels", ",".join(BASE + ["pulp-build-merge-group"]),
                    "--require-label", "pulp-build-merge-group",
                    "--gh-cli", str(fake), "--retry-backoff", "0",
                    "--observation-lock-file", str(root / "observation.lock"),
                    *(extra or []),
                ],
                text=True, capture_output=True, check=False, env=environment,
            )
            return result, ledger.read_text().splitlines()

    def test_a_transient_call_fault_is_retried_rather_than_ending_the_scan(self) -> None:
        """The production defect: one dropped call must not discard the scan."""
        result, requests = self._scan("actions/workflows?", fail_times=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        # The retry must be a real re-issue of the same call, not a skip.
        attempts = [line for line in requests if "actions/workflows?" in line]
        self.assertEqual(len(attempts), 2, requests)

    def test_a_transient_fault_on_a_per_run_jobs_call_is_retried(self) -> None:
        """The per-run fan-out is where N chances to fail live."""
        result, requests = self._scan("/jobs?", fail_times=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        self.assertEqual(len([r for r in requests if "/jobs?" in r]), 2, requests)

    def test_a_pagination_race_restarts_the_whole_pass(self) -> None:
        """A torn listing is retried as a pass, never spliced together.

        Nothing matches here, so the torn page carries no witness and the only
        route to an answer is a clean re-read of the listing. Reporting 0 off
        the torn page would under-count the queue.
        """
        result, requests = self._scan(
            "actions/workflows/99/runs", fail_times=1, mode="race", match=0
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")
        queued = [r for r in requests if "status=queued" in r]
        self.assertEqual(len(queued), 2, requests)

    def test_a_torn_listing_does_not_discard_a_witness_it_already_holds(self) -> None:
        """The other cell: a matching job on the torn page settles the scan.

        Reconciliation exists to make an empty listing believable. Once a run
        on the page has produced a matching queued job, there is no emptiness
        left to establish, so re-reading the listing cannot change the answer
        and is not bought. Production logged this tear 231 times as a blind
        poll while the proof was sitting on the page it had just read.
        """
        result, requests = self._scan(
            "actions/workflows/99/runs", fail_times=1, mode="race", match=1
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        queued = [r for r in requests if "status=queued" in r]
        self.assertEqual(len(queued), 1, requests)

    def test_a_persistent_fault_still_fails_closed(self) -> None:
        """Retry is not failing open: an unobservable queue still exits 2.

        This is the protection the scan exists for. The supervisor reads a
        non-zero exit as the absence of an observation and refuses to idle the
        lane as empty; a zero here would silently strand queued work.
        """
        result, requests = self._scan("actions/workflows?", fail_times=99)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("assignment scan failed closed", result.stderr)
        self.assertIn("after 3 attempt(s)", result.stderr)
        self.assertNotIn("0", result.stdout.strip() or "x")
        # Bounded: three attempts, not an unbounded hammer.
        self.assertEqual(len([r for r in requests if "actions/workflows?" in r]), 3, requests)

    def test_retries_are_configurable_and_zero_restores_the_old_behaviour(self) -> None:
        """`--api-retries 0` is exactly the pre-fix scanner, for bisecting."""
        result, requests = self._scan(
            "actions/workflows?", fail_times=1, extra=["--api-retries", "0"]
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(len([r for r in requests if "actions/workflows?" in r]), 1, requests)

    def test_retry_counts_are_validated(self) -> None:
        for flag, value in (("--api-retries", "-1"), ("--pagination-retries", "9"),
                            ("--retry-backoff", "-1")):
            with self.subTest(flag=flag):
                result = subprocess.run(
                    [
                        "python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                        "--workflow", "Build and Test", "--labels", "pulp-build-pr-head",
                        "--require-label", "pulp-build-pr-head", flag, value,
                    ],
                    text=True, capture_output=True, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(flag, result.stderr)


class AssignmentScannerWitnessTests(unittest.TestCase):
    """Presence may stop early; absence may not.

    The scan is asked whether a class has queued demand. One matching job
    settles that for good, so looking further cannot change the answer and the
    scan stops. Nothing settles the opposite short of looking everywhere, so a
    zero is only ever reported after a complete pass. These tests hold both
    halves, because the saving is only safe while the second half is true.
    """

    #: `RUNS` runs exist; only the last one carries a matching queued job, so a
    #: scan that stops early still has to walk most of them. The ledger records
    #: every request, which is how "did it stop?" becomes measurable.
    RUNS = 8

    _GH = '''#!/usr/bin/env python3
import json, os, sys
from urllib.parse import parse_qs, urlparse

LEDGER = os.environ["FAKE_GH_LEDGER"]
RUNS = int(os.environ["FAKE_GH_RUNS"])
MATCH = os.environ["FAKE_GH_MATCH"]

target = sys.argv[-1]
with open(LEDGER, "a") as handle:
    handle.write(target + "\\n")

p = urlparse("https://x/" + target)
status = parse_qs(p.query).get("status", [""])[0]

if p.path.endswith("/actions/workflows"):
    print(json.dumps({"total_count": 1, "workflows": [{"id": 99, "name": "Build and Test"}]}))
elif "/actions/workflows/99/runs" in p.path:
    if status != "queued":
        print(json.dumps({"total_count": 0, "workflow_runs": []}))
    else:
        runs = [{"id": i, "name": "Build and Test"} for i in range(1, RUNS + 1)]
        print(json.dumps({"total_count": len(runs), "workflow_runs": runs}))
elif "/actions/runs/" in p.path and p.path.endswith("/jobs"):
    run_id = int(p.path.split("/actions/runs/", 1)[1].split("/", 1)[0])
    matches = {"none": set(), "last": {RUNS}, "all": set(range(1, RUNS + 1))}[MATCH]
    if run_id in matches:
        print(json.dumps({"total_count": 1, "jobs": [{"id": run_id, "status": "queued", "labels": %s}]}))
    else:
        print(json.dumps({"total_count": 1, "jobs": [{"id": run_id, "status": "in_progress", "labels": []}]}))
else:
    raise SystemExit(4)
''' % repr(BASE + ["pulp-build-merge-group"])

    def _scan(self, match: str, extra: list[str] | None = None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake, ledger = root / "fake-gh", root / "ledger"
            ledger.write_text("")
            _write_exec(fake, self._GH)
            result = subprocess.run(
                [
                    "python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                    "--workflow", "Build and Test",
                    "--labels", ",".join(BASE + ["pulp-build-merge-group"]),
                    "--require-label", "pulp-build-merge-group",
                    "--gh-cli", str(fake), "--max-workers", "1",
                    "--observation-lock-file", str(root / "observation.lock"),
                    *(extra or []),
                ],
                text=True, capture_output=True, check=False,
                env=dict(os.environ, FAKE_GH_LEDGER=str(ledger),
                         FAKE_GH_RUNS=str(self.RUNS), FAKE_GH_MATCH=match),
            )
            jobs = [r for r in ledger.read_text().splitlines() if "/jobs?" in r]
            return result, jobs

    def test_absence_is_still_proved_exhaustively(self) -> None:
        """The fail-closed half: reporting zero requires looking everywhere.

        This is the guarantee the early exit is allowed to exist alongside. A
        zero that stopped early would idle a lane that has work waiting, which
        is the exact failure this scanner was built to prevent.
        """
        result, jobs = self._scan("none")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")
        self.assertEqual(len(jobs), self.RUNS, jobs)

    def test_one_witness_ends_the_scan(self) -> None:
        """The saving: presence needs one match, not a census."""
        result, jobs = self._scan("all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        self.assertEqual(len(jobs), 1, jobs)

    def test_a_late_witness_still_stops_the_remaining_runs(self) -> None:
        """Only the last run matches: it must still not scan past it."""
        result, jobs = self._scan("last")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        self.assertEqual(len(jobs), self.RUNS, jobs)

    def test_exhaustive_count_opt_out_reports_the_true_magnitude(self) -> None:
        """Diagnosis can still buy the real number."""
        result, jobs = self._scan("all", extra=["--exhaustive-count"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.RUNS))
        self.assertEqual(len(jobs), self.RUNS, jobs)


class AssignmentDemandIsOnlyEverAPredicateTests(unittest.TestCase):
    """Guard the licence that permits the early exit.

    Stopping at the first witness is only sound while no caller needs the
    count's magnitude — the scan reports `1` for any non-zero demand. That is a
    property of the shell that consumes it, not of the scanner, so it cannot be
    enforced where it is relied upon. This reads the consumer and fails if the
    count is ever compared against anything but zero or used in arithmetic,
    which is what would silently invalidate the early exit.
    """

    LIB = ROOT / "providers" / "tart-macos" / "assignment-v2.lib.sh"
    DEMAND = re.compile(r'\[\s*"\$\{?(q|cached_q)\}?"\s+-(?:gt|lt|ge|le|eq|ne)\s+(\S+)\s*\]')
    ARITHMETIC = re.compile(r'\$\(\(([^)]*\b(?:q|cached_q)\b[^)]*)\)\)')

    def test_the_demand_count_is_only_ever_compared_against_zero(self) -> None:
        source = self.LIB.read_text()
        comparisons = self.DEMAND.findall(source)
        # Control: if this finds nothing the regex has drifted and the guard is
        # vacuous, so an empty match set is a failure, not a pass.
        self.assertGreater(len(comparisons), 0, "no demand comparisons found — guard is blind")
        offenders = [(name, operand) for name, operand in comparisons if operand != "0"]
        self.assertEqual(
            offenders, [],
            "assignment demand is compared against a non-zero magnitude, which "
            "revokes the licence for the scanner's first-witness early exit: "
            f"{offenders}",
        )

    def test_arithmetic_on_the_count_only_happens_where_it_is_bought(self) -> None:
        """Summing counts is legitimate, but only with an exhaustive scan.

        The scanner reports 1 for any non-zero demand, so adding those values
        together produces a number that means nothing. The one caller that does
        add them is a report, and it must therefore ask for the true magnitude.
        A new arithmetic caller that does not is the regression this catches.
        """
        source = self.LIB.read_text()
        bodies = dict(re.findall(r"\n(\w+)\(\)\{\n(.*?)\n\}\n", source, re.S))
        # Control: the parser must actually see the function we know does this.
        self.assertIn("tartci_assignment_v2_total_demand", bodies,
                      "function parser found nothing — guard is blind")
        using_arithmetic = {
            name for name, body in bodies.items() if self.ARITHMETIC.search(body)
        }
        self.assertEqual(
            using_arithmetic, {"tartci_assignment_v2_total_demand"},
            f"unexpected arithmetic on assignment demand: {using_arithmetic}",
        )
        for name in using_arithmetic:
            self.assertRegex(
                bodies[name], r'tartci_assignment_v2_tier_demand "\$tier_label" 1',
                f"{name} does arithmetic on a witness count without buying the "
                "exhaustive one",
            )
        # Control: the arithmetic probe does fire on real arithmetic.
        self.assertEqual(self.ARITHMETIC.findall("x=$((q + 1))"), ["q + 1"],
                         "arithmetic probe is broken")


class AssignmentScanCheapPresenceTests(unittest.TestCase):
    """What the walk buys, and what it still refuses to conclude.

    Presence and absence are priced differently on purpose. A matching queued
    job is a complete proof that no further looking can retract, so the walk
    stops on it. Absence is only ever established by looking everywhere, so it
    still costs the whole enumeration and every reconciliation. Each test here
    runs both cells, because a cheap path that can only report presence and a
    fail-closed path that can only report failure are each worthless alone.
    """

    QUEUED = 6
    INPROG = 12

    _GH = '''#!/usr/bin/env python3
import json, os, sys
from urllib.parse import parse_qs, urlparse

LEDGER = os.environ["LEDGER"]
QUEUED = int(os.environ["QUEUED"])
INPROG = int(os.environ["INPROG"])
MATCH = {int(x) for x in os.environ.get("MATCH", "").split(",") if x}
TEAR = os.environ.get("TEAR", "")
GONE = os.environ.get("GONE", "")
FAIL_ON = os.environ.get("FAIL_ON", "")

target = sys.argv[-1]
with open(LEDGER, "a") as handle:
    handle.write(target + "\\n")
if FAIL_ON and FAIL_ON in target:
    sys.stderr.write("net/http: TLS handshake timeout\\n")
    raise SystemExit(1)

p = urlparse("https://x/" + target)
status = parse_qs(p.query).get("status", [""])[0]

if p.path.endswith("/actions/workflows"):
    print(json.dumps({"total_count": 1,
                      "workflows": [{"id": 99, "name": "Build and Test"}]}))
elif "/actions/workflows/" in p.path and p.path.endswith("/runs"):
    workflow = int(p.path.split("/actions/workflows/", 1)[1].split("/", 1)[0])
    if GONE and workflow == int(GONE):
        sys.stderr.write("gh: Not Found (HTTP 404)\\n")
        raise SystemExit(1)
    ids = (list(range(1, QUEUED + 1)) if status == "queued"
           else list(range(1001, 1001 + INPROG)))
    # A torn listing counts one more run than its body carries, which is what
    # a run leaving `queued` between the count and the body looks like.
    print(json.dumps({"total_count": len(ids) + (1 if TEAR == status else 0),
                      "workflow_runs": [{"id": i, "name": "Build and Test"} for i in ids]}))
elif "/actions/runs/" in p.path and p.path.endswith("/jobs"):
    run_id = int(p.path.split("/actions/runs/", 1)[1].split("/", 1)[0])
    if run_id in MATCH:
        print(json.dumps({"total_count": 1, "jobs": [
            {"id": run_id, "status": "queued", "labels": %s}]}))
    else:
        print(json.dumps({"total_count": 1, "jobs": [
            {"id": run_id, "status": "in_progress", "labels": []}]}))
else:
    raise SystemExit(4)
''' % repr(BASE + ["pulp-build-merge-group"])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.fake = self.root / "fake-gh"
        _write_exec(self.fake, self._GH)
        self.addCleanup(self._tmp.cleanup)

    def _scan(self, cache: Path | None = None, ttl: str = "0", **envx):
        """Run one scan and return its result plus a per-endpoint call census."""
        ledger = self.root / f"ledger-{len(list(self.root.glob('ledger-*')))}"
        ledger.write_text("")
        environment = dict(
            os.environ, LEDGER=str(ledger),
            QUEUED=str(self.QUEUED), INPROG=str(self.INPROG),
            **{key: str(value) for key, value in envx.items()},
        )
        result = subprocess.run(
            [
                "python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                "--workflow", "Build and Test",
                "--labels", ",".join(BASE + ["pulp-build-merge-group"]),
                "--require-label", "pulp-build-merge-group",
                "--gh-cli", str(self.fake), "--max-workers", "1",
                "--retry-backoff", "0",
                "--observation-lock-file", str(self.root / "observation.lock"),
                "--workflow-id-cache-file",
                str(cache if cache is not None else self.root / "unused-cache.json"),
                "--workflow-id-cache-ttl", ttl,
            ],
            text=True, capture_output=True, check=False, env=environment,
        )
        calls = [line for line in ledger.read_text().splitlines() if line]
        census = {
            "workflows": len([c for c in calls if "/actions/workflows?" in c]),
            "queued": len([c for c in calls if "status=queued" in c]),
            "in_progress": len([c for c in calls if "status=in_progress" in c]),
            "jobs": len([c for c in calls if "/jobs?" in c]),
            "total": len(calls),
        }
        runs = [int(c.split("/actions/runs/", 1)[1].split("/", 1)[0])
                for c in calls if "/jobs?" in c]
        return result, census, runs

    def test_a_witness_stops_the_walk_before_the_in_progress_listing(self) -> None:
        """A queued run answers the question, so the other listing is not read.

        The control is the same fixture with nothing to find: that scan must
        read the `in_progress` listing, because a queued job can sit in a run
        that is already in progress and absence has to account for it.
        """
        found, census, _ = self._scan(MATCH="1")
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(found.stdout.strip(), "1")
        self.assertEqual(census["in_progress"], 0, census)

        empty, empty_census, _ = self._scan(MATCH="")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(empty.stdout.strip(), "0")
        self.assertEqual(empty_census["in_progress"], 1, empty_census)

    def test_presence_costs_materially_less_than_proving_absence(self) -> None:
        """The saving, as a number rather than an adjective."""
        found, found_census, _ = self._scan(MATCH="1")
        empty, empty_census, _ = self._scan(MATCH="")
        self.assertEqual(found.stdout.strip(), "1")
        self.assertEqual(empty.stdout.strip(), "0")
        # Workflow listing, queued listing, one run's jobs. Nothing else.
        self.assertEqual(found_census["total"], 3, found_census)
        # Both listings, plus every run in both of them.
        self.assertEqual(
            empty_census["total"], 3 + self.QUEUED + self.INPROG, empty_census
        )
        self.assertGreater(empty_census["total"], 5 * found_census["total"])

    def test_absence_is_reported_only_after_every_run_is_examined(self) -> None:
        """The fail-closed half: zero is a claim about everywhere.

        The control proves the assertion can fail: the same fixture with a
        match reports 1 having examined almost none of these runs, so the
        exhaustive set is a property of the absent cell and not of the fixture.
        """
        empty, _, examined = self._scan(MATCH="")
        self.assertEqual(empty.stdout.strip(), "0")
        expected = set(range(1, self.QUEUED + 1)) | set(
            range(1001, 1001 + self.INPROG)
        )
        self.assertEqual(set(examined), expected)

        found, _, found_examined = self._scan(MATCH="1")
        self.assertEqual(found.stdout.strip(), "1")
        self.assertLess(len(found_examined), len(expected))

    def test_a_torn_listing_with_nothing_to_find_is_unknown_not_empty(self) -> None:
        """The error this scanner exists to prevent.

        A listing whose body never adds up to its own count was not read as one
        snapshot, so it cannot support a claim of absence. It must leave the
        scan blind rather than report a zero that would idle a lane. The
        control is the same fixture untorn, which does report zero.
        """
        torn, _, _ = self._scan(MATCH="", TEAR="queued")
        self.assertEqual(torn.returncode, 2, torn.stdout)
        self.assertNotEqual(torn.stdout.strip(), "0")
        self.assertIn("assignment scan failed closed", torn.stderr)

        clean, _, _ = self._scan(MATCH="")
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(clean.stdout.strip(), "0")

    def test_a_restarted_pass_does_not_rescan_what_it_already_scanned(self) -> None:
        """A re-read listing hands back runs whose jobs are already known."""
        torn, census, examined = self._scan(MATCH="", TEAR="queued")
        self.assertEqual(torn.returncode, 2, torn.stdout)
        # Control: the restart has to have happened, or dedup proves nothing.
        self.assertGreater(census["queued"], 1, census)
        self.assertEqual(len(examined), len(set(examined)), examined)

    def test_a_failing_call_fails_closed(self) -> None:
        """A call that never answered is not evidence the queue is empty."""
        broken, _, _ = self._scan(MATCH="1", FAIL_ON="/jobs?")
        self.assertEqual(broken.returncode, 2, broken.stdout)
        self.assertNotEqual(broken.stdout.strip(), "0")

        working, _, _ = self._scan(MATCH="1")
        self.assertEqual(working.returncode, 0, working.stderr)
        self.assertEqual(working.stdout.strip(), "1")

    def test_a_warm_workflow_id_cache_stops_paying_for_the_listing(self) -> None:
        """The one input that does not change between polls is read once."""
        cache = self.root / "workflow-ids.json"
        first, first_census, _ = self._scan(cache=cache, ttl="300", MATCH="1")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first_census["workflows"], 1, first_census)

        second, second_census, _ = self._scan(cache=cache, ttl="300", MATCH="1")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.strip(), "1")
        self.assertEqual(second_census["workflows"], 0, second_census)

    def test_an_expired_workflow_id_cache_is_resolved_again(self) -> None:
        """The staleness window is bounded, so the entry is not permanent."""
        cache = self.root / "workflow-ids.json"
        self._scan(cache=cache, ttl="300", MATCH="1")
        fresh, fresh_census, _ = self._scan(cache=cache, ttl="300", MATCH="1")
        self.assertEqual(fresh_census["workflows"], 0, fresh_census)

        payload = json.loads(cache.read_text())
        payload["Generous-Corp/pulp"]["fetched_at"] -= 10_000
        cache.write_text(json.dumps(payload))
        expired, expired_census, _ = self._scan(cache=cache, ttl="300", MATCH="1")
        self.assertEqual(expired.returncode, 0, expired.stderr)
        self.assertEqual(expired_census["workflows"], 1, expired_census)

    def test_an_unreadable_workflow_id_cache_is_a_miss_not_a_failure(self) -> None:
        """The cache only ever saves a call, so a broken one costs that call."""
        cache = self.root / "workflow-ids.json"
        # Text that is not JSON, and bytes that are not even text. The second
        # raises a decode error rather than a JSON error, so catching only the
        # latter would fail the scan closed and blind the lane over a file
        # whose only job is to save one call.
        for corrupt in (b"{not json at all", b"\xff\xfe\x00binary garbage"):
            with self.subTest(corrupt=corrupt[:8]):
                cache.write_bytes(corrupt)
                result, census, _ = self._scan(cache=cache, ttl="300", MATCH="1")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "1")
                self.assertEqual(census["workflows"], 1, census)

    def test_a_cached_id_that_stops_resolving_fails_the_scan_closed(self) -> None:
        """The cache cannot turn a broken lookup into an empty queue.

        A cached id is only ever spent on that workflow's own listing call, so
        an id that no longer resolves fails there. The control is the same warm
        cache against a workflow that still exists.
        """
        cache = self.root / "workflow-ids.json"
        self._scan(cache=cache, ttl="300", MATCH="1")

        gone, _, _ = self._scan(cache=cache, ttl="300", MATCH="1", GONE="99")
        self.assertEqual(gone.returncode, 2, gone.stdout)
        self.assertNotEqual(gone.stdout.strip(), "0")

        alive, alive_census, _ = self._scan(cache=cache, ttl="300", MATCH="1")
        self.assertEqual(alive.returncode, 0, alive.stderr)
        self.assertEqual(alive.stdout.strip(), "1")
        self.assertEqual(alive_census["workflows"], 0, alive_census)


# Every test class must be defined before the runner starts, so this stays the
# last statement in the file. Placed earlier it silently truncates the suite:
# the classes below the call are never defined and their tests never run.
if __name__ == "__main__":
    unittest.main(verbosity=2)
