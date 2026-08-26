#!/usr/bin/env python3
"""Hermetic tests for the tartci VM janitor."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import macos_observe
import vm_reap
from bounded_subprocess import (
    DESCENDANT_LEAK_EXIT_CODE,
    ObservationError,
    TIMEOUT_EXIT_CODE,
    run_bounded,
)


def write_state(path: Path, **fields: object) -> None:
    data = {
        "ts": "2020-01-01T00:00:00Z",
        "repo": "danielraffel/pulp",
        "labels": "self-hosted",
        "supervisor_pid": "999999",
        "supervisor_pid_started_at": "",
    }
    data.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class VmReapTests(unittest.TestCase):
    def test_macos_observer_digest_process_deadline_is_independent_of_probe_budget(self) -> None:
        args = macos_observe.parse_args(
            ["--digest-timeout", "55", "--digest-process-timeout", "123"]
        )

        self.assertEqual(args.digest_timeout, 55)
        self.assertEqual(args.digest_process_timeout, 123)

    def test_observation_budget_bounds_extra_running_vm_probes(self) -> None:
        budget = vm_reap.ObservationBudget(0.1)
        running = [
            {"Name": f"extra-{index}", "State": "running"}
            for index in range(20)
        ]

        with mock.patch.object(
            vm_reap,
            "observe_json",
            side_effect=lambda *_args, **_kwargs: time.sleep(0.04) or {"OS": "macos"},
        ):
            with self.assertRaises(ObservationError) as raised:
                vm_reap.macos_running_count(running, timeout=1, budget=budget)

        self.assertEqual(raised.exception.problem_code, "tart_get_timeout")

    def test_observation_budget_does_not_charge_non_observation_work(self) -> None:
        budget = vm_reap.ObservationBudget(0.2)
        time.sleep(0.05)
        with mock.patch.object(vm_reap, "observe_json", return_value={}) as observe:
            budget.run_json(["fake"], 1, "github_runners")

        self.assertGreater(observe.call_args.kwargs["timeout"], 0.19)

    def test_macos_observer_outer_timeout_returns_typed_valid_digest(self) -> None:
        args = macos_observe.parse_args([])
        timeout = subprocess.CompletedProcess([], TIMEOUT_EXIT_CODE, "", "timed out")
        with mock.patch.object(macos_observe, "run", return_value=timeout):
            digest, returncode = macos_observe.load_digest(args)

        self.assertEqual(returncode, TIMEOUT_EXIT_CODE)
        self.assertEqual(digest["problems"], ["observe_digest_timeout"])
        self.assertEqual(json.loads(json.dumps(digest)), digest)

    def assert_process_gone(self, pid: int) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail(f"process {pid} survived bounded observation cleanup")

    def test_bounded_observation_times_out_hanging_leaf(self) -> None:
        started = time.monotonic()
        result = run_bounded(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout=0.1,
            operation="hanging_leaf",
        )

        self.assertEqual(result.returncode, TIMEOUT_EXIT_CODE)
        self.assertIn("timed out", result.stderr)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_bounded_observation_kills_grandchild_that_retains_stdout(self) -> None:
        source = """
import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print(child.pid, flush=True)
"""
        result = run_bounded(
            [sys.executable, "-c", source],
            timeout=1,
            operation="retained_stdout",
        )

        self.assertEqual(result.returncode, DESCENDANT_LEAK_EXIT_CODE)
        self.assertIn("unexpected descendant", result.stderr)
        grandchild_pid = int(result.stdout.strip())
        self.assert_process_gone(grandchild_pid)

    def test_clone_lock_transition_returns_typed_json_then_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bin_dir = root / "bin"
            state_root = root / "state"
            lock = root / "clone.lock"
            bin_dir.mkdir()
            lock.write_text("held", encoding="utf-8")
            tart = bin_dir / "tart"
            tart.write_text(
                """#!/usr/bin/env python3
import json, os, sys, time
lock = os.environ['FAKE_TART_CLONE_LOCK']
if sys.argv[1] == 'list':
    while os.path.exists(lock):
        time.sleep(0.02)
    print(json.dumps([]))
elif sys.argv[1] == 'get':
    print(json.dumps({'OS': 'macos'}))
else:
    raise SystemExit(2)
""",
                encoding="utf-8",
            )
            tart.chmod(0o755)
            ghapp = bin_dir / "ghapp"
            ghapp.write_text("#!/bin/sh\nprintf '%s\\n' '[{\"runners\":[]}]'\n", encoding="utf-8")
            ghapp.chmod(0o755)
            argv = [
                sys.executable,
                str(Path(vm_reap.__file__)),
                "--json",
                "--state-root",
                str(state_root),
                "--tart-timeout-secs",
                "1",
                "--github-timeout-secs",
                "5",
            ]
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{bin_dir}:{env['PATH']}",
                    "TARTCI_GH_CLI": "ghapp",
                    "FAKE_TART_CLONE_LOCK": str(lock),
                }
            )

            started = time.monotonic()
            blocked = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
            blocked_json = json.loads(blocked.stdout)
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertIn("tart_list_timeout", blocked_json["problems"])
            self.assertIsNone(blocked_json["capacity"]["running_macos_vms"])

            lock.unlink()
            recovered = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
            recovered_json = json.loads(recovered.stdout)
            self.assertEqual(recovered.returncode, 0, recovered_json)
            self.assertEqual(recovered_json["problems"], [])
            self.assertEqual(recovered_json["capacity"]["running_macos_vms"], 0)

    def test_observation_error_exposes_stable_timeout_code(self) -> None:
        with self.assertRaises(ObservationError) as raised:
            vm_reap.observe_json(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=0.1,
                operation="github_runners",
            )

        self.assertEqual(raised.exception.problem_code, "github_runners_timeout")

    def test_github_operations_honor_configured_cli(self) -> None:
        response = mock.Mock(returncode=0, stdout='[{"runners": []}]', stderr="")
        with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": "ghapp"}), \
             mock.patch.object(vm_reap, "run_bounded", return_value=response) as observe_run, \
             mock.patch.object(vm_reap, "run", return_value=response) as mutate_run:
            self.assertEqual(vm_reap.github_runners("danielraffel/pulp"), [])
            vm_reap.delete_runner("danielraffel/pulp", 42, "pulp-vm-01")

        self.assertEqual(observe_run.call_args.args[0][0], "ghapp")
        self.assertEqual(mutate_run.call_args.args[0][0], "ghapp")

    def run_digest(self, root: Path, *extra: str):
        args = vm_reap.parse_args(
            [
                "--repo",
                "danielraffel/pulp",
                "--state-root",
                str(root),
                "--prefixes",
                "pulp-,linux-ephr-,win-ephr-,tartci-",
                *extra,
            ]
        )
        with mock.patch.object(vm_reap.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(vm_reap, "tart_vms", return_value=[]), \
             mock.patch.object(vm_reap, "github_runners", return_value=[]):
            return vm_reap.build_digest(args)

    def run_digest_with_missing_tart(self, root: Path, *extra: str):
        args = vm_reap.parse_args(
            [
                "--repo",
                "danielraffel/pulp",
                "--state-root",
                str(root),
                "--prefixes",
                "pulp-,linux-ephr-,win-ephr-,tartci-",
                *extra,
            ]
        )

        def which(tool: str):
            return None if tool == "tart" else "/usr/bin/tool"

        with mock.patch.object(vm_reap.shutil, "which", side_effect=which), \
             mock.patch.object(vm_reap, "github_runners", return_value=[]):
            return vm_reap.build_digest(args)

    def test_qemu_windows_state_reaps_ownerless_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            work = Path(td) / "tartci-win" / "win-ephr-host-1"
            port_lock = Path(td) / "tartci-win" / "port-locks" / "2222.lock"
            work.mkdir(parents=True)
            port_lock.mkdir(parents=True)
            write_state(
                root / "windows" / "win-ephr-host-1.state.json",
                provider="qemu-windows",
                runner="win-ephr-host-1",
                vm="win-ephr-host-1",
                work_dir=str(work),
                port_lock=str(port_lock),
                qemu_pid="999998",
                qemu_pid_started_at="",
            )

            digest, rc = self.run_digest(root, "--fix")

            self.assertEqual(rc, 0, digest)
            self.assertFalse(work.exists())
            self.assertFalse(port_lock.exists())
            self.assertFalse((root / "windows" / "win-ephr-host-1.state.json").exists())
            self.assertIn("path_deleted", "\n".join(digest["fixed"]))

    def test_qemu_reap_still_reports_when_tart_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            work = Path(td) / "tartci-win" / "win-ephr-host-3"
            work.mkdir(parents=True)
            write_state(
                root / "windows" / "win-ephr-host-3.state.json",
                provider="qemu-windows",
                runner="win-ephr-host-3",
                vm="win-ephr-host-3",
                work_dir=str(work),
                qemu_pid="999998",
                qemu_pid_started_at="",
            )

            digest, rc = self.run_digest_with_missing_tart(root, "--fix")

            self.assertEqual(rc, 2, digest)
            self.assertIn("missing_tool:tart", digest["problems"])
            self.assertFalse(work.exists())
            self.assertFalse((root / "windows" / "win-ephr-host-3.state.json").exists())

    def test_qemu_keep_failed_live_vm_is_not_reaped_inside_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            pid = os.getpid()
            write_state(
                root / "windows" / "win-ephr-host-2.state.json",
                provider="qemu-windows",
                runner="win-ephr-host-2",
                vm="win-ephr-host-2",
                qemu_pid=str(pid),
                qemu_pid_started_at=vm_reap.pid_start(pid),
                keep_failed=True,
            )

            digest, rc = self.run_digest(root, "--keep-failed-age-secs", "999999999")

            self.assertEqual(rc, 0, digest)
            row = next(row for row in digest["vms"] if row["name"] == "win-ephr-host-2")
            self.assertEqual(row["action"], "kept_failed_wait_for_operator")
            self.assertFalse(row["stale"])

    def test_tart_linux_missing_state_is_deleted_when_owner_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            state = root / "linux" / "linux-ephr-123.state.json"
            write_state(
                state,
                provider="tart-linux",
                runner="linux-ephr-123",
                vm="linux-ephr-123",
            )

            digest, rc = self.run_digest(root, "--fix")

            self.assertEqual(rc, 0, digest)
            self.assertFalse(state.exists())
            self.assertIn(f"state_deleted:{state}", digest["fixed"])

    def test_no_vm_state_reap_is_scoped_to_configured_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            selected = root / "macos" / "selected-lane.state.json"
            unrelated = root / "macos" / "unrelated-lane.state.json"
            write_state(selected, provider="tart-macos", runner="selected-lane")
            write_state(unrelated, provider="tart-macos", runner="unrelated-lane")
            args = vm_reap.parse_args([
                "--repo", "danielraffel/pulp",
                "--state-root", str(root),
                "--prefixes", "selected-",
                "--fix",
            ])

            with mock.patch.object(vm_reap.shutil, "which", return_value="/usr/bin/tool"), \
                 mock.patch.object(vm_reap, "tart_vms", return_value=[]), \
                 mock.patch.object(vm_reap, "github_runners", return_value=[]):
                digest, rc = vm_reap.build_digest(args)

            self.assertEqual(rc, 0, digest)
            self.assertFalse(selected.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(digest["fixed"], [f"state_deleted:{selected}"])

    def test_prefix_match_does_not_replace_missing_vm_ownership_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            unrelated = root / "macos" / "unrelated-lane.state.json"
            write_state(unrelated, provider="tart-macos", runner="unrelated-lane")
            args = vm_reap.parse_args([
                "--repo", "danielraffel/pulp",
                "--state-root", str(root),
                "--prefixes", "selected-",
                "--fix",
            ])
            stopped_vms = [
                {"Name": "selected-one", "State": "stopped"},
                {"Name": "selected-two", "State": "stopped"},
            ]

            with mock.patch.object(vm_reap.shutil, "which", return_value="/usr/bin/tool"), \
                 mock.patch.object(vm_reap, "tart_vms", return_value=stopped_vms), \
                 mock.patch.object(vm_reap, "github_runners", return_value=[]), \
                 mock.patch.object(vm_reap, "delete_vm") as delete_vm:
                digest, rc = vm_reap.build_digest(args)

            self.assertEqual(rc, 0, digest)
            self.assertTrue(unrelated.exists())
            self.assertEqual([row["owned"] for row in digest["vms"]], [False, False])
            delete_vm.assert_not_called()

    def test_offline_busy_without_local_owner_is_explicitly_orphaned(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            runner = {
                "id": 744,
                "name": "vellum-macos-ephemeral-202",
                "status": "offline",
                "busy": True,
                "labels": [],
            }
            args = vm_reap.parse_args([
                "--repo", "danielraffel/vellum",
                "--state-root", str(root),
                "--prefixes", "vellum-",
            ])
            with mock.patch.object(vm_reap.shutil, "which", return_value="/usr/bin/tool"), \
                 mock.patch.object(vm_reap, "tart_vms", return_value=[]), \
                 mock.patch.object(vm_reap, "github_runners", return_value=[runner]):
                digest, rc = vm_reap.build_digest(args)

            self.assertEqual(rc, 1, digest)
            row = digest["github_runners"][0]
            self.assertEqual(row["action"], "offline_busy_orphaned_no_local_owner")
            self.assertEqual(row["local_ownership"], "offline_busy_orphaned_no_local_owner")
            self.assertIn("offline_busy_orphaned_no_local_owner:vellum-macos-ephemeral-202", digest["problems"])

    def test_fresh_ephemeral_jit_runner_matches_live_owner_by_vm_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            pid = os.getpid()
            ephemeral_name = "pulp-studio-01-12345-1"
            write_state(
                root / "macos" / "pulp-studio-01.state.json",
                ts=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                provider="tart-macos",
                runner="pulp-studio-01",
                vm=ephemeral_name,
                supervisor_pid=str(pid),
                supervisor_pid_started_at=vm_reap.pid_start(pid),
            )
            runner = {
                "id": 745,
                "name": ephemeral_name,
                "status": "offline",
                "busy": False,
                "labels": [],
            }
            args = vm_reap.parse_args([
                "--repo", "danielraffel/pulp",
                "--state-root", str(root),
                "--prefixes", "pulp-studio-01-",
                "--fix",
            ])
            with mock.patch.object(vm_reap.shutil, "which", return_value="/usr/bin/tool"), \
                 mock.patch.object(vm_reap, "tart_vms", return_value=[]), \
                 mock.patch.object(vm_reap, "github_runners", return_value=[runner]), \
                 mock.patch.object(vm_reap, "delete_runner") as delete_runner:
                digest, rc = vm_reap.build_digest(args)

            self.assertEqual(rc, 0, digest)
            row = digest["github_runners"][0]
            self.assertEqual(row["action"], "wait_for_live_supervisor")
            self.assertTrue(row["owner_pid_alive"])
            self.assertEqual(
                row["state_file"],
                str(root / "macos" / "pulp-studio-01.state.json"),
            )
            delete_runner.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
