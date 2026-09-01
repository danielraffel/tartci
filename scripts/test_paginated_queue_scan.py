#!/usr/bin/env python3
"""Behavioral coverage for the shared macOS/Linux/Windows queue scanner."""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "queue_scan.py"
SPEC = importlib.util.spec_from_file_location("queue_scan", MODULE_PATH)
assert SPEC and SPEC.loader
queue_scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue_scan)

PROVIDERS = ("tart-macos", "tart-linux", "qemu-windows")
RUNNERS = (
    ROOT / "providers" / "tart-macos" / "runner.sh",
    ROOT / "providers" / "tart-linux" / "runner.sh",
    ROOT / "providers" / "qemu-windows" / "runner.sh",
)


def _run(run_id: int, when: datetime) -> dict[str, Any]:
    timestamp = when.isoformat().replace("+00:00", "Z")
    return {
        "id": run_id,
        "name": "Build and Test",
        "status": "queued",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


class PaginatedQueueScanTests(unittest.TestCase):
    def _scanner(
        self,
        provider: str,
        state_file: Path,
        api: Callable[[str], dict[str, Any]],
        **overrides: Any,
    ) -> Any:
        values = {
            "repo": "owner/repo",
            "workflow": "Build and Test",
            "labels": "self-hosted,macOS,ARM64,pulp-build-vm",
            "job_statuses": "queued",
            "state_file": str(state_file),
            "shared_cache_file": str(state_file.parent / "shared-discovery.json"),
            "observation_lock_file": str(state_file.parent / "host-observation.lock"),
            "observation_lock_timeout": 2.0,
            "provider": provider,
            "lane_id": "test-slot-1",
            "gh_cli": "unused",
            "gh_timeout": 5,
            "max_run_pages": 2,
            "max_job_fetches": 5,
            "newest_quota": 2,
            "max_api_calls": 12,
            "workflow_cache_ttl": 86400,
            "discovery_ttl": 120,
            "force_refresh": False,
            "expected_fleet_hosts": 3,
            "expected_discovery_namespaces": 4,
            "page_offset_cap": 64,
            "fleet_api_budget_per_hour": 4000,
            "stagger_max_seconds": 0,
            "negative_ttl": 300,
            "max_age_seconds": 0,
            "min_age_seconds": 0,
            "match_labels": 1,
        }
        values.update(overrides)
        scanner = queue_scan.QueueScanner(argparse.Namespace(**values))
        scanner._gh = api
        return scanner

    @staticmethod
    def _base_api(runs: list[dict[str, Any]], eligible_id: int | None) -> Callable[[str], dict[str, Any]]:
        def api(path: str) -> dict[str, Any]:
            if path.endswith("/actions/workflows?per_page=100"):
                return {"workflows": [{"id": 99, "name": "Build and Test"}]}
            if "status=queued" in path:
                return {"workflow_runs": runs}
            if "status=in_progress" in path:
                return {"workflow_runs": []}
            if f"/actions/runs/{eligible_id}/jobs?" in path:
                return {
                    "jobs": [
                        {
                            "status": "queued",
                            "labels": [
                                "self-hosted",
                                "macOS",
                                "ARM64",
                                "pulp-build-vm",
                            ],
                        }
                    ]
                }
            if "/jobs?" in path:
                return {"jobs": []}
            raise AssertionError(f"unexpected API path: {path}")

        return api

    def test_newest_latency_quota_serves_fresh_job_for_every_provider(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        runs = [_run(index, origin + timedelta(minutes=index)) for index in range(40)]
        eligible = _run(999, origin + timedelta(days=1))
        runs.append(eligible)
        for provider in PROVIDERS:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                scanner = self._scanner(
                    provider,
                    Path(directory) / "state.json",
                    self._base_api(runs, eligible["id"]),
                )
                self.assertEqual(scanner.scan(), 1)

    def test_minimum_queue_age_delays_then_admits_the_same_job(self) -> None:
        created = datetime.now(timezone.utc) - timedelta(seconds=30)
        queued = _run(999, created)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            api = self._base_api([queued], queued["id"])
            early = self._scanner(
                "tart-macos",
                state,
                api,
                min_age_seconds=60,
                negative_ttl=1,
            )
            self.assertEqual(early.scan(), 0)
            late = self._scanner(
                "tart-macos",
                state,
                api,
                min_age_seconds=60,
                negative_ttl=1,
            )
            late.now = early.now + 31
            self.assertEqual(late.scan(), 1)

    def test_rotating_cursor_eventually_reaches_middle_backlog_for_every_provider(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        runs = [_run(index, origin + timedelta(minutes=index)) for index in range(70)]
        eligible_id = 35
        for provider in PROVIDERS:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                state_file = Path(directory) / "state.json"
                api = self._base_api(runs, eligible_id)
                first = self._scanner(provider, state_file, api)
                self.assertEqual(first.scan(), 0)
                for iteration in range(12):
                    scanner = self._scanner(provider, state_file, api)
                    scanner.now += (iteration + 1) * 121
                    if scanner.scan() == 1:
                        break
                else:
                    self.fail(f"{provider} cursor never reached eligible backlog run")

    def test_run_page_cursor_rotates_beyond_first_five_pages(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        pages = {
            page: [
                _run((page * 1000) + index, origin + timedelta(minutes=index))
                for index in range(100)
            ]
            for page in range(1, 7)
        }
        eligible = _run(999, origin + timedelta(days=1))
        calls: list[str] = []

        def api(path: str) -> dict[str, Any]:
            calls.append(path)
            if path.endswith("/actions/workflows?per_page=100"):
                return {"workflows": [{"id": 99, "name": "Build and Test"}]}
            if "status=queued" in path:
                page = int(path.rsplit("&page=", 1)[1])
                return {"workflow_runs": [eligible] if page == 7 else pages.get(page, [])}
            if "status=in_progress" in path:
                return {"workflow_runs": []}
            if f"/actions/runs/{eligible['id']}/jobs?filter=latest&per_page=100" in path:
                return {
                    "jobs": [
                        {
                            "status": "queued",
                            "labels": [
                                "self-hosted",
                                "macOS",
                                "ARM64",
                                "pulp-build-vm",
                            ],
                        }
                    ]
                }
            if "/jobs?filter=latest&per_page=100" in path:
                return {"jobs": []}
            raise AssertionError(f"unexpected API path: {path}")

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            for iteration in range(6):
                scanner = self._scanner("tart-macos", state_file, api)
                scanner.now += (iteration + 1) * 121
                if scanner.scan() == 1:
                    break
            else:
                self.fail("rotating page cursor never reached page 7")
        self.assertTrue(any("&page=7" in path for path in calls))
        self.assertTrue(any("jobs?filter=latest&per_page=100" in path for path in calls))

    def test_rotating_full_pages_advance_within_each_page(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        pages: dict[int, list[dict[str, Any]]] = {}
        for page in range(1, 7):
            pages[page] = [
                _run(
                    page * 1000 + index,
                    origin - timedelta(minutes=(page - 1) * 100 + index),
                )
                for index in range(100)
            ]
        eligible_id = 2050
        fetched_page_two: list[int] = []

        def api(path: str) -> dict[str, Any]:
            if path.endswith("/actions/workflows?per_page=100"):
                return {"workflows": [{"id": 99, "name": "Build and Test"}]}
            if "status=in_progress" in path:
                return {"workflow_runs": []}
            if "status=queued" in path:
                page = int(path.rsplit("&page=", 1)[1])
                return {"workflow_runs": pages.get(page, [])}
            if "/jobs?filter=latest&per_page=100" in path:
                run_id = int(path.split("/actions/runs/", 1)[1].split("/", 1)[0])
                if 2000 <= run_id < 2100:
                    fetched_page_two.append(run_id)
                return {
                    "jobs": (
                        [
                            {
                                "status": "queued",
                                "labels": [
                                    "self-hosted",
                                    "macOS",
                                    "ARM64",
                                    "pulp-build-vm",
                                ],
                            }
                        ]
                        if run_id == eligible_id
                        else []
                    )
                }
            raise AssertionError(f"unexpected API path: {path}")

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            found_at: int | None = None
            for cycle in range(120):
                scanner = self._scanner("tart-macos", state_file, api)
                scanner.now += (cycle + 1) * 121
                if scanner.scan() == 1:
                    found_at = cycle
                    break
                if cycle == 29:
                    self.assertGreater(
                        len(set(fetched_page_two)),
                        3,
                        "page 2 repeated its first three backlog entries",
                    )
            self.assertIsNotNone(
                found_at,
                "eligible middle entry on full page 2 was never fetched",
            )

    def test_page_offsets_are_bounded_and_evicted_page_revisit_advances(
        self,
    ) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        pages = {
            page: [
                _run(
                    page * 1000 + index,
                    origin - timedelta(minutes=(page - 1) * 100 + index),
                )
                for index in range(100)
            ]
            for page in range(1, 106)
        }
        fetched_page_two: list[int] = []

        def api(path: str) -> dict[str, Any]:
            if path.endswith("/actions/workflows?per_page=100"):
                return {"workflows": [{"id": 99, "name": "Build and Test"}]}
            if "status=in_progress" in path:
                return {"workflow_runs": []}
            if "status=queued" in path:
                page = int(path.rsplit("&page=", 1)[1])
                return {"workflow_runs": pages.get(page, [])}
            if "/jobs?filter=latest&per_page=100" in path:
                run_id = int(path.split("/actions/runs/", 1)[1].split("/", 1)[0])
                if 2000 <= run_id < 2100:
                    fetched_page_two.append(run_id)
                return {"jobs": []}
            raise AssertionError(f"unexpected API path: {path}")

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            scanner = None
            # Page 2 through page 105, then empty page 106 completes the sweep.
            for cycle in range(105):
                scanner = self._scanner("tart-macos", state_file, api)
                scanner.now += (cycle + 1) * 121
                self.assertEqual(scanner.scan(), 0)
            assert scanner is not None
            discovery = json.loads(
                scanner.discovery_path.read_text(encoding="utf-8")
            )
            offsets = discovery.get("run_item_offsets", {})
            self.assertLessEqual(len(offsets), 64)
            self.assertNotIn(
                "queued:2",
                offsets,
                "old page should be deterministically evicted at the cap",
            )

            revisit = self._scanner("tart-macos", state_file, api)
            revisit.now += 106 * 121
            self.assertEqual(revisit.scan(), 0)

        self.assertEqual(len(fetched_page_two), 6)
        self.assertNotEqual(
            fetched_page_two[:3],
            fetched_page_two[3:],
            "evicted page restarted at its first backlog entries",
        )

    def test_state_is_namespaced_by_lane_and_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "state.json"
            api = self._base_api([], None)
            first = self._scanner("tart-macos", base, api, lane_id="slot-1")
            same = self._scanner("tart-macos", base, api, lane_id="slot-1")
            other_lane = self._scanner("tart-macos", base, api, lane_id="slot-2")
            other_labels = self._scanner(
                "tart-macos",
                base,
                api,
                lane_id="slot-1",
                labels="self-hosted,macOS,ARM64,pulp-preamble",
            )
            self.assertEqual(first.state_path, same.state_path)
            self.assertNotEqual(first.state_path, other_lane.state_path)
            self.assertNotEqual(first.state_path, other_labels.state_path)

    def test_multi_workflow_scan_aggregates_and_deduplicates_matching_jobs(
        self,
    ) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        workflows = ["Release CLI", "Release-path PR gate", "Sign and Release"]
        runs = [
            {**_run(index + 1, origin + timedelta(minutes=index)), "name": name}
            for index, name in enumerate(workflows)
        ]
        runs.append({**_run(99, origin + timedelta(minutes=4)), "name": "Unrelated"})
        jobs = {
            1: [
                {"id": 10, "status": "queued", "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]},
                {"id": 11, "status": "queued", "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]},
            ],
            2: [
                {"id": 11, "status": "queued", "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]},
                {"id": 12, "status": "queued", "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]},
            ],
            3: [
                {"id": 13, "status": "queued", "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]},
                {"id": 14, "status": "queued", "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]},
            ],
        }
        calls: list[str] = []

        def api(path: str) -> dict[str, Any]:
            calls.append(path)
            if "/actions/runs?status=queued" in path:
                return {"workflow_runs": runs}
            if "/actions/runs?status=in_progress" in path:
                return {"workflow_runs": []}
            if "/jobs?" in path:
                run_id = int(path.split("/actions/runs/", 1)[1].split("/", 1)[0])
                return {"jobs": jobs.get(run_id, [])}
            raise AssertionError(f"unexpected API path: {path}")

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            scanner = self._scanner(
                "tart-macos",
                state_file,
                api,
                workflow=[*workflows, "Release CLI"],
            )
            self.assertEqual(scanner.scan(), 5)
            reordered = self._scanner(
                "tart-macos",
                state_file,
                api,
                workflow=list(reversed(workflows)),
            )
            self.assertEqual(scanner.state_path, reordered.state_path)
            self.assertEqual(scanner.discovery_path, reordered.discovery_path)

        self.assertFalse(
            any(path.endswith("/actions/workflows?per_page=100") for path in calls)
        )
        self.assertTrue(any("/actions/runs?status=queued" in path for path in calls))

    def test_workflow_id_cache_avoids_repeated_lookup(self) -> None:
        calls: list[str] = []

        def api(path: str) -> dict[str, Any]:
            calls.append(path)
            return self._base_api([], None)(path)

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            self.assertEqual(self._scanner("tart-macos", state_file, api).scan(), 0)
            self.assertEqual(self._scanner("tart-macos", state_file, api).scan(), 0)
        workflow_calls = [
            path for path in calls if path.endswith("/actions/workflows?per_page=100")
        ]
        self.assertEqual(len(workflow_calls), 1)

    def test_force_refresh_bypasses_fresh_shared_discovery_cache(self) -> None:
        calls: list[str] = []

        def api(path: str) -> dict[str, Any]:
            calls.append(path)
            return self._base_api([], None)(path)

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            self.assertEqual(self._scanner("tart-macos", state_file, api).scan(), 0)
            self.assertEqual(
                self._scanner(
                    "tart-macos", state_file, api, force_refresh=True
                ).scan(),
                0,
            )
        workflow_calls = [
            path for path in calls if path.endswith("/actions/workflows?per_page=100")
        ]
        self.assertEqual(len(workflow_calls), 1)
        queued_calls = [path for path in calls if "status=queued" in path]
        self.assertEqual(len(queued_calls), 2)

    def test_deleted_cached_workflow_id_is_reresolved_after_404(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scanner = self._scanner(
                "tart-macos",
                Path(directory) / "state.json",
                lambda _path: {},
            )
            calls: list[str] = []

            def api(path: str) -> dict[str, Any]:
                calls.append(path)
                if "/actions/workflows/41/runs?" in path:
                    raise queue_scan.GitHubApiError(path, 1, "HTTP 404: Not Found")
                if path.endswith("/actions/workflows?per_page=100"):
                    return {
                        "workflows": [{"id": 99, "name": "Build and Test"}]
                    }
                if "/actions/workflows/99/runs?" in path:
                    return {"workflow_runs": []}
                raise AssertionError(f"unexpected API path: {path}")

            scanner._gh = api
            discovery = {
                "workflow_id": 41,
                "workflow_name": "Build and Test",
                "workflow_checked_at": scanner.now,
                "run_page_cursor": {},
            }
            self.assertEqual(scanner._runs(discovery), [])
            self.assertEqual(discovery["workflow_id"], 99)
            self.assertTrue(any("/workflows/41/runs?" in call for call in calls))
            self.assertTrue(any("/workflows/99/runs?" in call for call in calls))

    def test_priority_scan_counts_queued_and_in_progress_jobs(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        run = _run(999, origin)

        def api(path: str) -> dict[str, Any]:
            if path.endswith("/actions/workflows?per_page=100"):
                return {"workflows": [{"id": 99, "name": "Build and Test"}]}
            if "status=queued" in path:
                return {"workflow_runs": []}
            if "status=in_progress" in path:
                return {"workflow_runs": [run]}
            if "/jobs?" in path:
                return {
                    "jobs": [
                        {
                            "status": "in_progress",
                            "labels": [
                                "self-hosted",
                                "macOS",
                                "ARM64",
                                "pulp-build-vm",
                            ],
                        }
                    ]
                }
            raise AssertionError(f"unexpected API path: {path}")

        with tempfile.TemporaryDirectory() as directory:
            scanner = self._scanner(
                "tart-macos-priority",
                Path(directory) / "priority.json",
                api,
                job_statuses="queued,in_progress",
            )
            self.assertEqual(scanner.scan(), 1)

    def test_concurrent_lanes_share_one_aggregate_bounded_discovery(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        runs = [_run(index, origin + timedelta(minutes=index)) for index in range(100)]
        eligible = _run(999, origin + timedelta(days=1))
        runs.append(eligible)
        calls: list[str] = []
        calls_lock = threading.Lock()
        barrier = threading.Barrier(9)

        def api(path: str) -> dict[str, Any]:
            with calls_lock:
                calls.append(path)
            return self._base_api(runs, eligible["id"])(path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def scan_lane(index: int) -> int:
                scanner = self._scanner(
                    PROVIDERS[index % len(PROVIDERS)],
                    root / f"lane-{index}.json",
                    api,
                    lane_id=f"slot-{index}",
                    shared_cache_file=str(root / "host-discovery.json"),
                )
                barrier.wait()
                return scanner.scan()

            with concurrent.futures.ThreadPoolExecutor(max_workers=9) as executor:
                results = list(executor.map(scan_lane, range(9)))

        self.assertEqual(results, [1] * 9)
        self.assertLessEqual(len(calls), 11)
        fleet_hourly_upper_bound = ceil(3600 / 120) * 11 * 3
        ghapp_installation_core_limit = 12_500
        reserved_for_other_traffic = 8_500
        discovery_budget = (
            ghapp_installation_core_limit - reserved_for_other_traffic
        )
        # Worst intended topology: Pulp Build/Test, Release, Sanitizers/priority,
        # plus Forge Build. Not every host currently serves every namespace, but
        # the enforced default budgets all four on all three hosts.
        fleet_hourly_upper_bound *= 4
        self.assertEqual(fleet_hourly_upper_bound, 3960)
        self.assertEqual(discovery_budget, 4000)
        self.assertLessEqual(fleet_hourly_upper_bound, discovery_budget)
        self.assertEqual(
            ghapp_installation_core_limit - fleet_hourly_upper_bound,
            8_540,
        )

    def test_cross_process_cache_lock_recovers_and_only_publishes_atomic_json(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "host-discovery.json"
            counter = root / "api-calls"
            stub = root / "stub-gh"
            stub.write_text(
                """#!/usr/bin/env python3
import fcntl, json, os, sys
path = sys.argv[-1]
with open(os.environ["CALL_COUNTER"], "a+", encoding="utf-8") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    handle.write(path + "\\n")
if path.endswith("/actions/workflows?per_page=100"):
    payload = {"workflows": [{"id": 99, "name": "Build and Test"}]}
elif "status=queued" in path:
    payload = {"workflow_runs": [{"id": 999, "name": "Build and Test", "status": "queued", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}]}
elif "status=in_progress" in path:
    payload = {"workflow_runs": []}
elif "/jobs?" in path:
    payload = {"jobs": [{"status": "queued", "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]}]}
else:
    raise SystemExit(2)
print(json.dumps(payload))
""",
                encoding="utf-8",
            )
            stub.chmod(0o755)

            probe = self._scanner(
                "tart-macos",
                root / "probe.json",
                lambda _path: {},
                shared_cache_file=str(shared),
            )
            lock_holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import fcntl, sys, time; "
                        "h=open(sys.argv[1],'a+'); "
                        "fcntl.flock(h.fileno(),fcntl.LOCK_EX); "
                        "print('locked', flush=True); time.sleep(60)"
                    ),
                    str(probe.discovery_lock_path),
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            assert lock_holder.stdout is not None
            self.assertEqual(lock_holder.stdout.readline().strip(), "locked")

            common = [
                sys.executable,
                str(MODULE_PATH),
                "--repo",
                "owner/repo",
                "--workflow",
                "Build and Test",
                "--labels",
                "self-hosted,macOS,ARM64,pulp-build-vm",
                "--provider",
                "tart-macos",
                "--shared-cache-file",
                str(shared),
                "--observation-lock-file",
                str(root / "host-observation.lock"),
                "--stagger-max-seconds",
                "0",
            ]
            env = {**os.environ, "CALL_COUNTER": str(counter)}
            malformed: list[str] = []
            stop_reader = threading.Event()

            def observe_cache() -> None:
                while not stop_reader.is_set():
                    for path in root.glob("host-discovery.*.json"):
                        try:
                            json.loads(path.read_text(encoding="utf-8"))
                        except (json.JSONDecodeError, OSError) as error:
                            malformed.append(str(error))

            reader = threading.Thread(target=observe_cache)
            reader.start()
            processes = [
                subprocess.Popen(
                    [
                        *common,
                        "--lane-id",
                        f"process-{index}",
                        "--state-file",
                        str(root / f"lane-{index}.json"),
                        "--gh-cli",
                        str(stub),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                for index in range(9)
            ]
            lock_holder.terminate()
            lock_holder.wait(timeout=5)
            lock_holder.stdout.close()
            results = [process.communicate(timeout=15) for process in processes]
            stop_reader.set()
            reader.join(timeout=5)

            self.assertEqual([process.returncode for process in processes], [0] * 9)
            self.assertEqual([stdout.strip() for stdout, _ in results], ["1"] * 9)
            self.assertEqual(malformed, [])
            self.assertLessEqual(
                len(counter.read_text(encoding="utf-8").splitlines()),
                10,
            )

    def test_distinct_namespaces_share_one_host_observation_slot(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        eligible = _run(999, origin)
        active = 0
        peak = 0
        guard = threading.Lock()

        def api(path: str) -> dict[str, Any]:
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.01)
                return self._base_api([eligible], eligible["id"])(path)
            finally:
                with guard:
                    active -= 1

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scanners = [
                self._scanner(
                    "tart-macos",
                    root / f"lane-{index}.json",
                    api,
                    repo=f"owner/repo-{index}",
                    shared_cache_file=str(root / "shared-discovery.json"),
                    observation_lock_file=str(root / "host-observation.lock"),
                )
                for index in range(3)
            ]
            barrier = threading.Barrier(len(scanners))

            def scan(scanner: Any) -> int:
                barrier.wait()
                return scanner.scan()

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                results = list(executor.map(scan, scanners))

        self.assertEqual(results, [1, 1, 1])
        self.assertEqual(peak, 1)

    def test_distinct_process_namespaces_bound_actual_github_children(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observation_lock = root / "host-observation.lock"
            activity = root / "activity.json"
            stub = root / "counted-gh"
            stub.write_text(
                """#!/usr/bin/env python3
import fcntl, json, os, sys, time
activity = os.environ['ACTIVITY']
with open(activity, 'a+', encoding='utf-8') as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    handle.seek(0); raw = handle.read().strip()
    state = json.loads(raw) if raw else {'active': 0, 'peak': 0}
    state['active'] += 1; state['peak'] = max(state['peak'], state['active'])
    handle.seek(0); handle.truncate(); json.dump(state, handle); handle.flush()
try:
    time.sleep(0.02)
    path = sys.argv[-1]
    if path.endswith('/actions/workflows?per_page=100'):
        payload = {'workflows': [{'id': 99, 'name': 'Build and Test'}]}
    elif 'status=queued' in path or 'status=in_progress' in path:
        payload = {'workflow_runs': []}
    else:
        raise SystemExit(2)
    print(json.dumps(payload))
finally:
    with open(activity, 'r+', encoding='utf-8') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = json.load(handle); state['active'] -= 1
        handle.seek(0); handle.truncate(); json.dump(state, handle); handle.flush()
""",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            env = {**os.environ, "ACTIVITY": str(activity)}
            processes = []
            for index in range(3):
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable, str(MODULE_PATH),
                            "--repo", f"owner/repo-{index}",
                            "--workflow", "Build and Test",
                            "--labels", "self-hosted,macOS",
                            "--provider", "tart-macos",
                            "--lane-id", f"lane-{index}",
                            "--state-file", str(root / f"lane-{index}.json"),
                            "--shared-cache-file", str(root / "discovery.json"),
                            "--observation-lock-file", str(observation_lock),
                            "--stagger-max-seconds", "0",
                            "--gh-cli", str(stub),
                        ],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, env=env,
                    )
                )
            results = [process.communicate(timeout=10) for process in processes]
            self.assertEqual([process.returncode for process in processes], [0, 0, 0])
            self.assertEqual([stdout.strip() for stdout, _ in results], ["0", "0", "0"])
            self.assertEqual(json.loads(activity.read_text(encoding="utf-8"))["peak"], 1)

    def test_host_observation_lock_timeout_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scanner = self._scanner(
                "tart-macos",
                root / "lane.json",
                lambda _path: self.fail("API must not run without the lock"),
                observation_lock_file=str(root / "host-observation.lock"),
                observation_lock_timeout=0.05,
            )
            with scanner.observation_lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                with self.assertRaisesRegex(RuntimeError, "observation lock timed out"):
                    scanner.scan()

    def test_namespace_lock_wait_shares_the_bounded_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scanner = self._scanner(
                "tart-macos",
                root / "lane.json",
                lambda _path: self.fail("API must not run while state is locked"),
                observation_lock_timeout=0.05,
            )
            with scanner.lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                with self.assertRaisesRegex(RuntimeError, "queue state lock timed out"):
                    scanner.scan()

    def test_non_finite_observation_timeout_is_rejected(self) -> None:
        result = subprocess.run(
            [
                sys.executable, str(MODULE_PATH), "--repo", "owner/repo",
                "--workflow", "Build and Test", "--labels", "self-hosted",
                "--provider", "test", "--state-file", "/tmp/unused-state.json",
                "--observation-lock-timeout", "nan",
            ],
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("observation-lock-timeout must be positive", result.stderr)

    def test_api_calls_are_hard_capped(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        runs = [_run(index, origin + timedelta(minutes=index)) for index in range(100)]
        calls: list[str] = []

        def api(path: str) -> dict[str, Any]:
            calls.append(path)
            return self._base_api(runs, None)(path)

        with tempfile.TemporaryDirectory() as directory:
            scanner = self._scanner(
                "tart-macos",
                Path(directory) / "state.json",
                api,
            )
            self.assertEqual(scanner.scan(), 0)
        self.assertLessEqual(len(calls), 12)

    def test_newest_quota_must_leave_backlog_fetch_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "less than max_job_fetches"):
                self._scanner(
                    "tart-macos",
                    Path(directory) / "state.json",
                    self._base_api([], None),
                    newest_quota=6,
                    max_job_fetches=6,
                )

    def test_job_fetch_failure_is_blind_not_an_empty_queue(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        runs = [_run(1, origin)]

        def api(path: str) -> dict[str, Any]:
            if path.endswith("/actions/workflows?per_page=100"):
                return {"workflows": [{"id": 99, "name": "Build and Test"}]}
            if "status=queued" in path:
                return {"workflow_runs": runs}
            if "status=in_progress" in path:
                return {"workflow_runs": []}
            if "/jobs?" in path:
                raise subprocess.CalledProcessError(1, ["gh", "api", path])
            raise AssertionError(f"unexpected API path: {path}")

        with tempfile.TemporaryDirectory() as directory:
            scanner = self._scanner(
                "tart-macos",
                Path(directory) / "state.json",
                api,
            )
            with self.assertRaises(subprocess.CalledProcessError):
                scanner.scan()

    def test_all_providers_use_the_shared_scanner_and_map_failure_to_err(self) -> None:
        for runner in RUNNERS:
            body = runner.read_text(encoding="utf-8")
            self.assertIn('scripts/queue_scan.py"', body, runner)
            self.assertIn("--shared-cache-file", body, runner)
            self.assertIn("TARTCI_SHARED_QUEUE_CACHE", body, runner)
            self.assertIn("2>/dev/null || echo ERR", body, runner)

    def test_macos_supervisor_passes_opt_in_minimum_queue_age(self) -> None:
        body = RUNNERS[0].read_text(encoding="utf-8")
        self.assertIn("TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS:-0", body)
        self.assertGreaterEqual(body.count('--min-age-seconds "$MIN_QUEUED_AGE"'), 2)

    def test_linux_and_windows_do_not_age_out_compatible_queued_jobs(self) -> None:
        for runner in RUNNERS[1:]:
            body = runner.read_text(encoding="utf-8")
            self.assertIn("PULP_RUNNER_MAX_QUEUED_AGE_SECONDS:-0", body, runner)
            self.assertNotIn("PULP_RUNNER_MAX_QUEUED_AGE_SECONDS:-21600", body, runner)


if __name__ == "__main__":
    unittest.main(verbosity=2)
