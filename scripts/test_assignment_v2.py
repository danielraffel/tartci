#!/usr/bin/env python3
"""Behavioral coverage for exclusive V2 macOS JIT assignment classes."""
from __future__ import annotations

import contextlib
import json
import os
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

if parsed.path.endswith("/actions/workflows"):
    print(json.dumps({"total_count": 1, "workflows": [{"id": 99, "name": "Build and Test"}]}))
elif "/actions/workflows/99/runs" in parsed.path or parsed.path.endswith("/actions/runs"):
    timestamp = (dt.datetime.now(dt.timezone.utc).strftime("%%Y-%%m-%%dT%%H:%%M:%%SZ")
                 if state.get("fresh") else "2026-08-25T00:00:00Z")
    runs = ([{"id": 101, "name": "Build and Test", "status": status,
              "created_at": timestamp, "updated_at": timestamp}]
            if status == "queued" and jobs else [])
    print(json.dumps({"total_count": len(runs), "workflow_runs": runs if page == 1 else []}))
elif "/actions/runs/101/jobs" in parsed.path:
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

    def test_api_failure_denies_selection(self) -> None:
        self._state(api_fail=True)
        result = self._runner("--print-selection")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split("\t", 1)[0], "ERR")
        events = (self.root / "state" / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event":"assignment_scan_error"', events)
        self.assertIn("scanner_rc=2", events)
        self.assertIn("GitHub API failed", events)

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
        scanner.args = Namespace(max_workers=3)
        scanner._observation_lock = lambda: contextlib.nullcontext()
        scanner._runs = lambda: [{"id": run_id} for run_id in range(6)]
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
