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

    def test_qemu_snapshot_outside_job_never_unseals(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.object(vm_reap.os, "chflags", create=True) as chflags:
            root = Path(td)
            work = root / "tartci-win" / "win-ephr-host-outside"
            outside = root / "other" / "golden.qcow2"
            work.mkdir(parents=True)
            outside.parent.mkdir()
            outside.write_bytes(b"fixture")
            (outside.parent / "golden.qcow2.sha256").write_text("fixture\n")

            with self.assertRaisesRegex(ValueError, "unsafe Windows golden snapshot"):
                vm_reap.unseal_windows_snapshot(
                    {"runner": work.name, "work_dir": str(work), "golden_snapshot": str(outside)}
                )

            chflags.assert_not_called()

    def test_qemu_snapshot_symlink_never_unseals(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.object(vm_reap.os, "chflags", create=True) as chflags:
            root = Path(td)
            work = root / "tartci-win" / "win-ephr-host-symlink"
            target = root / "outside.qcow2"
            work.mkdir(parents=True)
            target.write_bytes(b"fixture")
            snapshot = work / "golden.qcow2"
            snapshot.symlink_to(target)

            with self.assertRaisesRegex(ValueError, "linked Windows golden snapshot"):
                vm_reap.unseal_windows_snapshot(
                    {"runner": work.name, "work_dir": str(work), "golden_snapshot": str(snapshot)}
                )

            chflags.assert_not_called()

    def test_qemu_snapshot_through_linked_parent_never_unseals(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.object(vm_reap.os, "chflags", create=True) as chflags:
            root = Path(td)
            real_parent = root / "real"
            linked_parent = root / "linked"
            real_work = real_parent / "tartci-win" / "win-ephr-host-parent"
            work = linked_parent / "tartci-win" / "win-ephr-host-parent"
            real_work.mkdir(parents=True)
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            snapshot = work / "golden.qcow2"
            snapshot.write_bytes(b"fixture")

            with self.assertRaisesRegex(ValueError, "linked Windows job directory"):
                vm_reap.unseal_windows_snapshot(
                    {"runner": work.name, "work_dir": str(work), "golden_snapshot": str(snapshot)}
                )

            chflags.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "immutable snapshot uses macOS flags")
    def test_qemu_windows_reap_unseals_recorded_private_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            job = "tartci-win-job-555"
            work = Path(td) / "tartci-win" / job
            work.mkdir(parents=True)
            snapshot = work / "golden.qcow2"
            sidecar = work / "golden.qcow2.sha256"
            snapshot.write_bytes(b"fixture")
            sidecar.write_text("0" * 64 + "  fixture\n")
            subprocess.run(["chflags", "uchg", str(snapshot), str(sidecar)], check=True)
            self.addCleanup(lambda: subprocess.run(
                ["chflags", "nouchg", str(snapshot), str(sidecar)],
                capture_output=True, check=False))
            write_state(root / "windows" / f"{job}.state.json",
                        provider="qemu-windows", runner=job, vm=job,
                        work_dir=str(work), golden_snapshot=str(snapshot),
                        qemu_pid="999998", qemu_pid_started_at="")

            digest, rc = self.run_digest(root, "--fix")

            self.assertEqual(rc, 0, digest)
            self.assertFalse(work.exists())
            self.assertFalse((root / "windows" / f"{job}.state.json").exists())

    def test_unseal_refuses_any_path_but_this_jobs_snapshot(self) -> None:
        # A state file names the path; a stale or corrupted one must never get
        # the shared golden (or any other file) unsealed.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            job = "tartci-win-job-557"
            work = base / "tartci-win" / job
            work.mkdir(parents=True)
            (work / "golden.qcow2").write_bytes(b"private")
            shared = base / "goldens" / "windows.qcow2"
            shared.parent.mkdir()
            shared.write_bytes(b"shared golden")
            Path(f"{shared}.sha256").write_text("0" * 64 + "  windows.qcow2\n")
            linked_snapshot = base / "tartci-win" / "job-558"
            linked_snapshot.mkdir()
            (linked_snapshot / "golden.qcow2").symlink_to(shared)
            linked_work = base / "tartci-win" / "job-559"
            linked_work.symlink_to(base / "goldens", target_is_directory=True)
            (base / "goldens" / "golden.qcow2").write_bytes(b"other")
            cases = {
                "outside the work dir": (work, shared),
                "a sibling of the snapshot": (work, work / "other.qcow2"),
                "a linked snapshot": (linked_snapshot, linked_snapshot / "golden.qcow2"),
                "through a linked work dir": (linked_work, linked_work / "golden.qcow2"),
            }
            (work / "other.qcow2").write_bytes(b"other")
            for name, (work_dir, snapshot) in cases.items():
                with self.subTest(name), mock.patch.object(vm_reap.os, "chflags",
                                                           create=True) as chflags:
                    with self.assertRaises(ValueError):
                        vm_reap.unseal_windows_snapshot(
                            {"runner": job, "work_dir": str(work_dir),
                             "golden_snapshot": str(snapshot)})
                    chflags.assert_not_called()

    def test_unseal_clears_only_this_jobs_snapshot_and_sidecar(self) -> None:
        # Control, same instrument: the recorded private snapshot is unsealed.
        with tempfile.TemporaryDirectory() as td:
            job = "tartci-win-job-560"
            work = Path(td) / "tartci-win" / job
            work.mkdir(parents=True)
            snapshot = work / "golden.qcow2"
            snapshot.write_bytes(b"private")
            Path(f"{snapshot}.sha256").write_text("0" * 64 + "  golden.qcow2\n")
            with mock.patch.object(vm_reap.os, "chflags", create=True) as chflags:
                vm_reap.unseal_windows_snapshot(
                    {"runner": job, "work_dir": str(work), "golden_snapshot": str(snapshot)})
            self.assertEqual([c.args for c in chflags.call_args_list],
                             [(snapshot, 0), (Path(f"{snapshot}.sha256"), 0)])

    @unittest.skipUnless(sys.platform == "darwin", "immutable snapshot uses macOS flags")
    def test_a_refused_unseal_leaves_the_shared_golden_immutable(self) -> None:
        import stat  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as td:
            job = "tartci-win-job-561"
            work = Path(td) / "tartci-win" / job
            work.mkdir(parents=True)
            shared = Path(td) / "windows.qcow2"
            shared.write_bytes(b"shared golden")
            subprocess.run(["chflags", "uchg", str(shared)], check=True)
            self.addCleanup(lambda: subprocess.run(["chflags", "nouchg", str(shared)],
                                                   capture_output=True, check=False))
            with self.assertRaises(ValueError):
                vm_reap.unseal_windows_snapshot(
                    {"runner": job, "work_dir": str(work), "golden_snapshot": str(shared)})
            self.assertTrue(shared.stat().st_flags & stat.UF_IMMUTABLE)

    def test_qemu_snapshot_with_missing_pid_is_preserved(self) -> None:
        for pid, started in (("", ""), ("999998", "")):
            with self.subTest(pid=pid), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "state"
                job = "tartci-win-job-556"
                work = Path(td) / "tartci-win" / job
                work.mkdir(parents=True)
                snapshot = work / "golden.qcow2"
                snapshot.write_bytes(b"fixture")
                state_file = root / "windows" / f"{job}.state.json"
                write_state(state_file, provider="qemu-windows", runner=job, vm=job,
                            phase="preparing", work_dir=str(work),
                            golden_snapshot=str(snapshot), qemu_pid=pid,
                            qemu_pid_started_at=started)

                digest, _ = self.run_digest(root, "--fix")

                self.assertTrue(work.exists())
                self.assertTrue(state_file.exists())
                self.assertIn(f"qemu_pid_unknown_preserve:{job}", digest["problems"])

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


class ForeignRunnerTests(unittest.TestCase):
    """A janitor deletes only offline registrations this host could have minted.

    Replays 2026-09-29, found on m3: its janitor saw the bare-metal
    `pulp-intel-macmini` runner (registered from another host) read offline for
    one pass, found no local state for it, and deleted the registration.
    """

    MACMINI_LABELS = ["self-hosted", "X64", "macOS", "pulp-intel-native", "pulp-host-macmini"]
    T0 = dt.datetime(2026, 10, 15, 4, 0, 0, tzinfo=dt.timezone.utc)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.agents = self.tmp / "LaunchAgents"
        self.agents.mkdir()

    def reap(self, runner: dict, *extra: str, at: float = 0.0) -> tuple[dict, mock.MagicMock]:
        args = vm_reap.parse_args([
            "--repo", "Generous-Corp/pulp",
            "--state-root", str(self.tmp / "state"),
            "--launch-agents-dir", str(self.agents),
            "--fix",
            *extra,
        ])
        with mock.patch.object(vm_reap, "utcnow", return_value=self.T0 + dt.timedelta(seconds=at)), \
             mock.patch.object(vm_reap.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(vm_reap, "tart_vms", return_value=[]), \
             mock.patch.object(vm_reap, "github_runners", return_value=[runner]), \
             mock.patch.object(vm_reap, "delete_runner", return_value="deleted") as delete_runner:
            digest, _ = vm_reap.build_digest(args)
        self.digest = digest
        return digest["github_runners"][0], delete_runner

    def reap_passes(self, runner: dict, *extra: str, passes: int = 3, step: float = 450.0):
        row = delete_runner = None
        for i in range(passes):
            row, delete_runner = self.reap(runner, *extra, at=i * step)
        return row, delete_runner

    def offline(self, name: str, labels: list, status: str = "offline") -> dict:
        return {"id": 27620, "name": name, "status": status, "busy": False, "labels": labels}

    def install_linux_lane(self, host_label: str) -> None:
        """Render the real Linux lane template the way its install recipe does."""
        template = (Path(vm_reap.__file__).resolve().parent.parent
                    / "launchd/com.danielraffel.pulp.tart-runner-linux.plist.template").read_text()
        text = (template.replace("$TARTCI_HOST_LABEL", host_label)
                .replace("$HOME", "/Users/ci").replace("$TART_HOME", "/Users/ci/VMs"))
        (self.agents / "com.danielraffel.pulp.tart-runner-linux.plist").write_text(text)

    def test_incident_persistent_runner_on_another_host_is_kept(self) -> None:
        row, delete_runner = self.reap_passes(self.offline("pulp-intel-macmini", self.MACMINI_LABELS))
        delete_runner.assert_not_called()
        self.assertEqual(row["action"], "keep_not_minted_here")
        self.assertEqual(row["refusal"], "not_a_per_boot_name")

    def test_raw_api_label_objects_are_read(self) -> None:
        labels = [{"id": i, "name": n, "type": "custom"} for i, n in enumerate(self.MACMINI_LABELS)]
        name = "pulp-intel-macmini-20261015034211-73971-1"
        row, delete_runner = self.reap_passes(
            self.offline(name, labels), "--own-name-prefixes", "pulp-intel-macmini-",
            "--host-labels", "pulp-host-macstudio")
        delete_runner.assert_not_called()
        self.assertEqual(row["refusal"], "bound_to_other_host:pulp-host-macmini")

    def test_another_hosts_macos_per_boot_runner_is_kept_by_name(self) -> None:
        # The reviewer's probe A: unlabelled, per-boot shaped, offline for many
        # passes, minted by another host's lane.
        row, delete_runner = self.reap_passes(
            self.offline("m5-pulp-gate-01-17725-47", ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]),
            "--own-name-prefixes", "studio-pulp-gate-01-", "--prefixes", "m5-", passes=5)
        delete_runner.assert_not_called()
        self.assertEqual(row["refusal"], "not_this_hosts_prefix")

    def test_unknown_own_prefixes_keep_everything(self) -> None:
        row, delete_runner = self.reap_passes(
            self.offline("studio-pulp-gate-01-12345-1", ["self-hosted"]), "--prefixes", "studio-")
        delete_runner.assert_not_called()
        self.assertEqual(row["refusal"], "not_this_hosts_prefix")

    def test_declared_persistent_runner_is_kept_even_with_a_per_boot_shape(self) -> None:
        name = "pulp-studio-01-12345-1"
        row, delete_runner = self.reap_passes(
            self.offline(name, ["self-hosted"]), "--persistent-runners", f"other,{name}",
            "--own-name-prefixes", "pulp-studio-01-")
        delete_runner.assert_not_called()
        self.assertEqual(row["refusal"], "persistent_runner")

    def test_one_offline_reading_never_deletes(self) -> None:
        runner = self.offline("studio-pulp-gate-01-12345-1", ["self-hosted", "macOS"])
        own = ("--own-name-prefixes", "studio-pulp-gate-01-")
        row, delete_runner = self.reap(runner, *own, at=0)
        delete_runner.assert_not_called()
        self.assertEqual((row["action"], row["refusal"]), ("wait_offline_age", "offline_too_recent"))
        # Three passes inside the age are still too recent.
        row, delete_runner = self.reap_passes(runner, *own, passes=3, step=60)
        delete_runner.assert_not_called()

    def test_the_age_alone_is_not_enough_without_three_passes(self) -> None:
        runner = self.offline("studio-pulp-gate-01-12345-1", ["self-hosted", "macOS"])
        row, delete_runner = self.reap_passes(
            runner, "--own-name-prefixes", "studio-pulp-gate-01-", passes=2, step=1800)
        delete_runner.assert_not_called()
        self.assertEqual(row["refusal"], "offline_too_recent")

    def test_own_ghost_is_deleted_after_three_passes_and_the_age(self) -> None:
        # Positive control: the guard must not blind the janitor to its own residue.
        runner = self.offline("studio-pulp-gate-01-12345-1", ["self-hosted", "macOS"])
        row, delete_runner = self.reap_passes(runner, "--own-name-prefixes", "studio-pulp-gate-01-")
        delete_runner.assert_called_once()
        self.assertEqual(row["action"], "delete_offline_runner")
        self.assertEqual(row["offline_passes"], 3)

    def test_an_online_reading_resets_the_offline_clock(self) -> None:
        name = "studio-pulp-gate-01-12345-1"
        own = ("--own-name-prefixes", "studio-pulp-gate-01-")
        self.reap(self.offline(name, ["self-hosted"]), *own, at=0)
        self.reap(self.offline(name, ["self-hosted"]), *own, at=450)
        self.reap(self.offline(name, ["self-hosted"], status="online"), *own, at=900)
        row, delete_runner = self.reap(self.offline(name, ["self-hosted"]), *own, at=1350)
        delete_runner.assert_not_called()
        self.assertEqual(row["offline_passes"], 1)

    def test_own_linux_ghost_is_deleted_with_the_installed_lane_environment(self) -> None:
        # The installed reap agent carries no TARTCI_HOST_LABEL: this host's
        # label is read from its installed Linux lane plist, nothing by hand.
        self.install_linux_lane("pulp-host-macstudio")
        runner = self.offline("linux-ephr-4242-1",
                              ["self-hosted", "Linux", "ARM64", "pulp-build-linux", "pulp-host-macstudio"])
        with mock.patch.dict(os.environ, {"TARTCI_HOST_LABEL": "", "TARTCI_REAP_OWN_NAME_PREFIXES": ""}):
            row, delete_runner = self.reap_passes(runner)
        delete_runner.assert_called_once()
        self.assertEqual(self.digest["config"]["host_labels"], ["pulp-host-macstudio"])
        self.assertIn("linux-ephr-", self.digest["config"]["own_name_prefixes"])

    def test_a_conflicting_host_label_set_owns_no_host_bound_runner(self) -> None:
        # m5's installed Linux plist advertised both its own label and the
        # studio's; Linux names carry no host, so only the label separates them.
        self.install_linux_lane("pulp-host-m5,pulp-host-macstudio")
        studio = self.offline("linux-ephr-5150-1",
                              ["self-hosted", "Linux", "ARM64", "pulp-build-linux", "pulp-host-macstudio"])
        row, delete_runner = self.reap_passes(studio)
        delete_runner.assert_not_called()
        self.assertEqual(row["refusal"], "bound_to_other_host:pulp-host-macstudio")
        self.assertIn("host_label_conflict:pulp-host-m5,pulp-host-macstudio", self.digest["problems"])
        self.assertEqual(self.digest["config"]["host_labels"], [])

    def test_a_plist_label_that_disagrees_with_the_declared_label_owns_nothing(self) -> None:
        self.install_linux_lane("pulp-host-macstudio")
        mine = self.offline("linux-ephr-4242-1", ["self-hosted", "Linux", "pulp-host-macstudio"])
        row, delete_runner = self.reap_passes(mine, "--host-labels", "pulp-host-m5")
        delete_runner.assert_not_called()
        self.assertIn("host_label_conflict:pulp-host-m5,pulp-host-macstudio", self.digest["problems"])

    def test_another_hosts_linux_ghost_is_kept_by_label(self) -> None:
        self.install_linux_lane("pulp-host-macstudio")
        runner = self.offline("linux-ephr-4242-1",
                              ["self-hosted", "Linux", "ARM64", "pulp-build-linux", "pulp-host-m5"])
        row, delete_runner = self.reap_passes(runner)
        delete_runner.assert_not_called()
        self.assertEqual(row["refusal"], "bound_to_other_host:pulp-host-m5")

    def test_own_lane_prefixes_are_host_specific_never_the_shared_identity(self) -> None:
        from types import SimpleNamespace
        lanes = [SimpleNamespace(runner_name="studio-pulp-gate-01", identity="pulp-gate"),
                 SimpleNamespace(runner_name="studio-pulp-gate-slot2-02", identity="pulp-gate.slot2")]
        self.assertEqual(vm_reap.own_lane_name_prefixes(lanes),
                         ["studio-pulp-gate-01-", "studio-pulp-gate-slot2-02-"])

    def test_reap_template_does_not_hard_code_a_host_label(self) -> None:
        # The janitor reads this host's label from its lane plists, so the
        # shared reap template must not pin one host's label for every host.
        template = (Path(vm_reap.__file__).resolve().parent.parent
                    / "launchd/com.danielraffel.tartci.reap.plist.template").read_text()
        self.assertNotIn("pulp-host-", template)
        self.assertIn("TARTCI_REAP_PREFIXES", template)


class OrphanedLaneVmTests(unittest.TestCase):
    """Stopped lane VMs whose supervisor is gone are deleted; any doubt keeps one.

    No state file names such a VM (a lane's state file names only its current
    VM), so the marker rule never owned them: on 2026-09-30 m3 held four
    (250 GB), m1 two (94 GB), and nothing had deleted a stopped VM since 08-15.
    """

    RUNNERS = {"studio-pulp-gate-01"}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def bundle(self, name: str, age: float) -> Path:
        path = self.tmp / "vms" / name
        path.mkdir(parents=True, exist_ok=True)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def verdict(self, name="studio-pulp-gate-01-11643-2", running=False, states=None,
                leases=frozenset(), alive=lambda pid: False, age=7200):
        return vm_reap.orphan_verdict(name, running, self.RUNNERS, states or {},
                                      None if leases is None else set(leases),
                                      self.bundle(name, age), time.time(), 3600, alive)

    def test_a_stopped_lane_vm_with_a_dead_supervisor_is_deleted(self) -> None:
        self.assertTrue(self.verdict()[0])

    def test_every_doubt_keeps_it(self) -> None:
        cases = {
            "not_a_lane_vm": dict(name="pulp-preamble-m1-target-06-99292-1"),
            "running": dict(running=True),
            "state_file_names_it": dict(states={"studio-pulp-gate-01-11643-2": {}}),
            "lease_store_unreadable": dict(leases=None),
            "lease_names_it": dict(leases={"vm-tart-macos-vm-studio-pulp-gate-01-11643-2"}),
            "supervisor_pid_alive": dict(alive=lambda pid: pid == 11643),
            "recent": dict(age=60),
        }
        for reason, kwargs in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(self.verdict(**kwargs), (False, reason))
        self.assertEqual(vm_reap.orphan_verdict(
            "studio-pulp-gate-01-11643-2", False, self.RUNNERS, {}, set(),
            self.tmp / "vms" / "absent", time.time(), 3600, lambda pid: False),
            (False, "vm_bundle_unreadable"))

    def retired(self, name: str, age: float, host_id="m1", prefixes=("pulp-",),
                alive=lambda pid: False, leases=frozenset(), named=lambda n: False,
                record=lambda vm, runner, pid: True, enabled=True):
        return vm_reap.orphan_verdict(name, False, self.RUNNERS, {}, set(leases),
                                      self.bundle(name, age), time.time(), 3600, alive,
                                      host_id=host_id, prefixes=list(prefixes), named=named,
                                      record=record, retired_enabled=enabled)

    def test_a_retired_lanes_clone_on_this_host_is_deleted_after_a_day(self) -> None:
        # m1 kept pulp-preamble-m1-target-06-99292-1 for two months: its lane
        # was retired, so no loaded runner matched it.
        name = "pulp-preamble-m1-target-06-99292-1"
        verdict = self.retired(name, 2 * 86400)
        self.assertTrue(verdict[0], verdict)
        self.assertIn("retired lane", verdict[1])
        self.assertTrue(self.retired("m1-old-lane-01-5-1", 2 * 86400)[0])

    def test_a_retired_lane_clone_keeps_every_doubt(self) -> None:
        name = "pulp-preamble-m1-target-06-99292-1"
        cases = {
            "recent": dict(age=6 * 3600),
            "supervisor_pid_alive": dict(age=2 * 86400, alive=lambda pid: pid == 99292),
            "lease_names_it": dict(age=2 * 86400, leases={name}),
            "named_by_tartci": dict(age=2 * 86400, named=lambda n: n == name),
            "no_retired_lane_record": dict(age=2 * 86400, record=lambda vm, r, pid: False),
        }
        for reason, kwargs in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(self.retired(name, **kwargs), (False, reason))
        # Not provably this host's: another host's id, no host id word, an
        # unlisted prefix, or no known host id at all.
        for other, kwargs in (("pulp-build-runner:pre-m1-rollback-1-2", {}),  # an image, never a clone
                              ("pulp-preamble-m5-target-06-99292-1", {}),
                              ("pulp-linux-build-proof-2-20260811-1", {}),
                              ("vellum-m1-scratch-01-7-1", {}),
                              ("pulp-preamble-m1-target-06-99292-1", {"host_id": None})):
            with self.subTest(name=other, **{k: str(v) for k, v in kwargs.items()}):
                self.assertEqual(self.retired(other, 60 * 86400, **kwargs),
                                 (False, "not_a_lane_vm"))

    @unittest.skipIf(sys.version_info < (3, 11), "reads the fleet profile (tomllib)")
    def test_tartci_names_uses_the_image_pruners_ownership_rule(self) -> None:
        home = self.tmp / "home"
        (home / ".config/tartci").mkdir(parents=True)
        (home / "Library/LaunchAgents").mkdir(parents=True)
        profile = home / ".config/tartci/macos-fleet-profile.toml"
        profile.write_text('[[lane]]\ngolden = "pulp-build-runner:latest"\n')
        (home / "Library/LaunchAgents/com.danielraffel.x.plist").write_text(
            "<string>pulp-linux-golden-7-1</string>")
        with mock.patch.dict(os.environ, {"HOME": str(home),
                                          "TARTCI_FLEET_PROFILE": str(profile)}), \
             mock.patch.object(vm_reap.pathlib.Path, "home", return_value=home):
            names = vm_reap.TartciNames()
            self.assertTrue(names("pulp-build-runner:latest"))
            self.assertTrue(names("pulp-linux-golden-7-1"))
            self.assertFalse(names("pulp-preamble-m1-target-06-99292-1"))
            profile.write_text("not = [toml")
            self.assertTrue(vm_reap.TartciNames()("pulp-preamble-m1-target-06-99292-1"))

    def test_an_agent_named_vm_is_never_a_retired_clone_without_the_lanes_record(self) -> None:
        # infra-review2's cases: plausible agent VM names with this host's word.
        state = self.tmp / "state-root"
        record = lambda vm, runner, pid: vm_reap.retired_lane_record(state, vm, runner, pid)  # noqa: E731
        for name in ("pulp-m1-repro-20261016-1", "tartci-m1-bisect-1-2", "m1-scratch-repro-12-3"):
            with self.subTest(name=name):
                self.assertEqual(self.retired(name, 60 * 86400, record=record,
                                              prefixes=("pulp-", "tartci-")),
                                 (False, "no_retired_lane_record"))

    def test_the_retired_lanes_record_must_name_vm_runner_pid_and_a_tart_provider(self) -> None:
        state = self.tmp / "state-root" / "macos-preamble-target-06"
        state.mkdir(parents=True)
        good = {"provider": "tart-macos", "runner": "pulp-preamble-m1-target-06",
                "vm": "pulp-preamble-m1-target-06-99292-1", "supervisor_pid": "99292"}
        path = state / "pulp-preamble-m1-target-06.state.json"
        args = (self.tmp / "state-root", "pulp-preamble-m1-target-06-99292-1",
                "pulp-preamble-m1-target-06", "99292")
        path.write_text(json.dumps(good))
        self.assertTrue(vm_reap.retired_lane_record(*args))
        # A symlinked record is no proof: it may point anywhere.
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text(json.dumps(good))
        path.unlink()
        path.symlink_to(elsewhere)
        self.assertFalse(vm_reap.retired_lane_record(*args))
        path.unlink()
        path.write_text(json.dumps(good))
        for key, wrong in (("vm", "pulp-preamble-m1-target-06-99292-2"),
                           ("runner", "pulp-preamble-m1-target-05"),
                           ("supervisor_pid", "1"), ("provider", "qemu-windows")):
            with self.subTest(key=key):
                path.write_text(json.dumps(dict(good, **{key: wrong})))
                self.assertFalse(vm_reap.retired_lane_record(*args))

    def test_the_retired_clone_reap_is_report_only_until_the_host_opts_in(self) -> None:
        verdict = self.retired("pulp-preamble-m1-target-06-99292-1", 2 * 86400, enabled=False)
        self.assertFalse(verdict[0])
        self.assertTrue(verdict[1].startswith("retired_clone_reap_off: would delete"), verdict)

    def m1_digest(self, *argv: str, enabled: bool,
                  real_ownership: bool = False) -> tuple[dict, list, str]:
        import contextlib
        import io
        import fleet_lane_discovery as fld
        # m1's layout: ~/.tartci/state/<lane>/, ~/.config/tartci, LaunchAgents.
        home = self.tmp / "home"
        state = home / ".tartci" / "state"
        lane = fld.Lane("com.danielraffel.tartci.tart-runner-macos-fleet.m1.pulp-gate",
                        "pulp-gate", state / "macos-fleet" / "pulp-gate", "m1-pulp-gate-01")
        (state / "macos-fleet" / "pulp-gate").mkdir(parents=True, exist_ok=True)
        retired_dir = state / "macos-preamble-target-06"
        retired_dir.mkdir(parents=True, exist_ok=True)
        # m1's real record, as found on 2026-10-16.
        (retired_dir / "pulp-preamble-m1-target-06.state.json").write_text(json.dumps({
            "ts": "2026-07-26T07:16:35Z", "provider": "tart-macos", "host": "laptop-host",
            "runner": "pulp-preamble-m1-target-06", "vm": "pulp-preamble-m1-target-06-99292-1",
            "vm_ip": "192.168.64.6", "phase": "job-running", "lifecycle": "ephemeral",
            "labels": "self-hosted,macOS,ARM64,pulp-preamble", "repo": "Generous-Corp/pulp",
            "run_id": "{", "job_id": "{", "supervisor_pid": "99292",
            "supervisor_pid_started_at": "Sun Jul 26 00:15:54 2026"}))
        (retired_dir / "events.jsonl").write_text(
            '{"event":"boot_ok","vm":"pulp-preamble-m1-target-06-99292-1"}\n')
        config = home / ".config" / "tartci"
        config.mkdir(parents=True, exist_ok=True)
        profile = config / "macos-fleet-profile.toml"
        profile.write_text('[host]\nid = "m1"\n[[lane]]\nid = "pulp-gate"\n'
                           'golden = "pulp-build-runner:latest"\n')
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / f"{lane.label}.plist").write_text(
            "<plist><dict><key>TARTCI_GOLDEN</key><string>pulp-build-runner:latest</string>"
            "</dict></plist>")
        (self.tmp / "leases").mkdir(exist_ok=True)
        (self.tmp / "leases" / "leases.json").write_text("[]")
        flag = self.tmp / "reap-retired-clones.enabled"
        if enabled:
            flag.write_text("")
        else:
            flag.unlink(missing_ok=True)
        vms = [{"Name": "pulp-preamble-m1-target-06-99292-1", "State": "stopped", "Size": 44},
               {"Name": "pulp-m1-repro-20261016-1", "State": "stopped", "Size": 20},
               {"Name": "tartci-m1-bisect-1-2", "State": "stopped", "Size": 20},
               {"Name": "m1-scratch-repro-12-3", "State": "stopped", "Size": 20},
               {"Name": "pulp-linux-build-proof-2.336.0-20260811", "State": "stopped", "Size": 18},
               {"Name": "vellum-pr19-linux-20260811", "State": "stopped", "Size": 6}]
        for vm in vms:
            self.bundle(vm["Name"], 60 * 86400)
        deleted: list = []
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TART_HOME": str(self.tmp),
                                          "TARTCI_LEASE_DIR": str(self.tmp / "leases"),
                                          "TARTCI_REAP_RETIRED_ENABLE_FILE": str(flag)}), \
             mock.patch.object(vm_reap, "scoped_view", return_value=False), \
             mock.patch.object(fld, "discover_lanes", return_value=([lane], [])), \
             mock.patch.object(vm_reap.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(vm_reap, "tart_vms", return_value=vms), \
             mock.patch.object(vm_reap, "macos_running_count", return_value=0), \
             mock.patch.object(vm_reap, "github_runners", return_value=[]), \
             mock.patch.object(vm_reap, "pid_alive", return_value=False), \
             mock.patch.object(vm_reap.pathlib.Path, "home", return_value=home), \
             mock.patch.dict(os.environ, {"TARTCI_FLEET_PROFILE": str(profile), "HOME": str(home)}), \
             (contextlib.nullcontext() if real_ownership else
              mock.patch.object(vm_reap, "TartciNames", return_value=lambda name: False)), \
             mock.patch.object(vm_reap, "delete_vm",
                               side_effect=lambda name, running: deleted.append(name) or
                               [f"vm_deleted:{name}"]), \
             contextlib.redirect_stdout(out):
            if "--json" in argv:
                digest, _ = vm_reap.build_digest(vm_reap.parse_args(
                    ["--repo", "danielraffel/pulp", "--state-root", str(state),
                     *[a for a in argv if a != "--json"]]))
            else:
                vm_reap.main(["--repo", "danielraffel/pulp", "--state-root",
                              str(state), *argv])
                digest = {}
        return digest, deleted, out.getvalue()

    def test_the_digest_on_m1_reaps_only_the_recorded_retired_clone(self) -> None:
        digest, deleted, _ = self.m1_digest("--fix", "--json", enabled=True)
        self.assertEqual(deleted, ["pulp-preamble-m1-target-06-99292-1"])
        self.assertEqual(digest["orphans"]["delete"], ["pulp-preamble-m1-target-06-99292-1"])
        self.assertEqual(digest["orphans"]["kept"]["pulp-m1-repro-20261016-1"],
                         "no_retired_lane_record")

    @unittest.skipIf(sys.version_info < (3, 11), "reads the fleet profile (tomllib)")
    def test_with_the_real_ownership_check_on_m1s_layout(self) -> None:
        # No mocked ownership: the real retired clone, recorded by its own
        # lane's state file, is deleted; agent-shaped names are kept.
        digest, deleted, _ = self.m1_digest("--fix", "--json", enabled=True,
                                            real_ownership=True)
        self.assertEqual(deleted, ["pulp-preamble-m1-target-06-99292-1"])
        kept = digest["orphans"]["kept"]
        for name in ("pulp-m1-repro-20261016-1", "tartci-m1-bisect-1-2", "m1-scratch-repro-12-3"):
            self.assertEqual(kept[name], "no_retired_lane_record", name)
        self.assertNotIn("pulp-linux-build-proof-2.336.0-20260811", deleted)
        self.assertNotIn("vellum-pr19-linux-20260811", deleted)

    def test_off_the_digest_deletes_nothing_and_names_what_it_would(self) -> None:
        digest, deleted, _ = self.m1_digest("--fix", "--json", enabled=False)
        self.assertEqual(deleted, [])
        self.assertEqual(digest["orphans"]["retired_would_delete"],
                         ["pulp-preamble-m1-target-06-99292-1"])

    def test_dry_run_leaves_the_runner_offline_tracker_unchanged(self) -> None:
        tracker = self.tmp / "home" / ".tartci" / "state" / "reap" / "runner-offline.json"
        tracker.parent.mkdir(parents=True, exist_ok=True)
        before = '{"m1-old-runner": {"first_offline": 1, "passes": 2}}'
        tracker.write_text(before)
        self.m1_digest("--dry-run", enabled=False)
        self.assertEqual(tracker.read_text(), before)
        # Control, same instrument: a plain report pass rewrites the clocks.
        self.m1_digest(enabled=False)
        self.assertNotEqual(tracker.read_text(), before)

    def test_dry_run_prints_and_never_deletes_even_with_fix(self) -> None:
        _, deleted, out = self.m1_digest("--fix", "--dry-run", enabled=True)
        self.assertEqual(deleted, [])
        self.assertIn("would delete_orphaned_stopped_vm: pulp-preamble-m1-target-06-99292-1", out)
        _, deleted, out = self.m1_digest("--dry-run", enabled=False)
        self.assertEqual(deleted, [])
        self.assertIn("would delete once ~/.config/tartci/reap-retired-clones.enabled exists: "
                      "pulp-preamble-m1-target-06-99292-1", out)

class SupervisorServingDigestTests(unittest.TestCase):
    """The digest every observer reads has to carry the serve-less streak.

    `observe macos` and the lane reports render whatever lands here. A state
    file can record three hours of taking work and serving none, and if the
    digest drops the field the tools downstream can only show a fresh
    heartbeat, which reads as health.
    """

    def run_digest(self, root: Path, *extra: str):
        args = vm_reap.parse_args([
            "--repo", "danielraffel/pulp", "--state-root", str(root),
            "--prefixes", "pulp-,linux-ephr-,win-ephr-,tartci-", *extra,
        ])
        with mock.patch.object(vm_reap.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(vm_reap, "tart_vms", return_value=[]), \
             mock.patch.object(vm_reap, "github_runners", return_value=[]):
            return vm_reap.build_digest(args)

    def test_the_digest_carries_the_serve_less_streak(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            root.mkdir(parents=True)
            (root / "lane-01.state.json").write_text(json.dumps({
                "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "provider": "tart-macos", "runner": "lane-01", "vm": "",
                "phase": "waiting", "lifecycle": "ephemeral",
                "serving_blocked_since": "2026-09-20T00:00:00Z",
                "serving_blocked_streak": 143,
                "serving_blocked_last_phase": "admission-error",
                "supervisor_pid": "101",
            }))
            digest, _ = self.run_digest(root)
        supervisors = {item["runner"]: item for item in digest["supervisors"]}
        self.assertIn("lane-01", supervisors)
        lane = supervisors["lane-01"]
        self.assertEqual(lane["serving_blocked_since"], "2026-09-20T00:00:00Z")
        self.assertEqual(lane["serving_blocked_streak"], 143)
        self.assertEqual(lane["serving_blocked_last_phase"], "admission-error")

    def test_a_generation_predating_the_counter_reads_as_absent_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            root.mkdir(parents=True)
            (root / "lane-02.state.json").write_text(json.dumps({
                "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "provider": "tart-macos", "runner": "lane-02", "vm": "",
                "phase": "waiting", "lifecycle": "ephemeral",
                "supervisor_pid": "102",
            }))
            digest, _ = self.run_digest(root)
        lane = {item["runner"]: item for item in digest["supervisors"]}["lane-02"]
        self.assertIsNone(lane["serving_blocked_streak"])

class RunnerCensusScopeTests(unittest.TestCase):
    """The janitor reads both registration scopes, never one."""

    def payload(self, rows):
        return mock.Mock(returncode=0, stdout=json.dumps([{"runners": rows}]), stderr="")

    def test_both_scopes_are_read_and_rows_carry_their_endpoint(self) -> None:
        repo_row = {"id": 1, "name": "studio-pulp-gate-01", "status": "online"}
        org_row = {"id": 2, "name": "pulp-intel-macmini", "status": "online"}
        responses = {
            "repos/danielraffel/pulp/actions/runners?per_page=100": self.payload([repo_row]),
            "orgs/danielraffel/actions/runners?per_page=100": self.payload([org_row]),
        }
        seen = []

        def fake_run_bounded(argv, **kwargs):
            seen.append(argv[2])
            return responses[argv[2]]

        with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": "ghapp"}), \
             mock.patch.object(vm_reap, "run_bounded", side_effect=fake_run_bounded):
            rows = vm_reap.github_runners("danielraffel/pulp")

        self.assertEqual(len(seen), 2)
        names = [row["name"] for row in rows]
        self.assertIn("pulp-intel-macmini", names)
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(
            by_name["studio-pulp-gate-01"]["_endpoint"],
            "repos/danielraffel/pulp/actions/runners",
        )
        self.assertEqual(
            by_name["pulp-intel-macmini"]["_endpoint"], "orgs/danielraffel/actions/runners"
        )

    def test_one_unreadable_scope_is_reported_not_silently_empty(self) -> None:
        good = self.payload([{"id": 1, "name": "studio-pulp-gate-01", "status": "online"}])

        def fake_run_bounded(argv, **kwargs):
            if argv[2].startswith("orgs/"):
                return mock.Mock(returncode=1, stdout="", stderr="HTTP 403")
            return good

        problems: list[str] = []
        with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": "ghapp"}), \
             mock.patch.object(vm_reap, "run_bounded", side_effect=fake_run_bounded):
            rows = vm_reap.github_runners("danielraffel/pulp", problems=problems)

        self.assertEqual([row["name"] for row in rows], ["studio-pulp-gate-01"])
        self.assertEqual(problems, ["github_runners_scope_unreadable:organization"])

    def test_every_scope_unreadable_is_an_observation_failure(self) -> None:
        failure = mock.Mock(returncode=1, stdout="", stderr="HTTP 403")

        with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": "ghapp"}), \
             mock.patch.object(vm_reap, "run_bounded", return_value=failure):
            with self.assertRaises(ObservationError):
                vm_reap.github_runners("danielraffel/pulp")

    def test_delete_addresses_a_runner_through_its_own_scope(self) -> None:
        response = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": "ghapp"}), \
             mock.patch.object(vm_reap, "run", return_value=response) as mutate_run:
            vm_reap.delete_runner(
                "danielraffel/pulp", 2, "pulp-intel-macmini", "orgs/danielraffel/actions/runners"
            )

        self.assertEqual(
            mutate_run.call_args.args[0][-1], "orgs/danielraffel/actions/runners/2"
        )

    def test_delete_runs_under_the_same_identity_binding_as_the_census(self) -> None:
        """A fake gh records the repo binding it sees on the DELETE itself."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "calls.log"
            fake = Path(td) / "fakegh"
            fake.write_text(
                "#!/bin/sh\n"
                f'printf "GH_REPO=%s SHIPYARD_GHAPP_REPO=%s args=%s\\n" '
                f'"$GH_REPO" "$SHIPYARD_GHAPP_REPO" "$*" >> "{log}"\n'
            )
            fake.chmod(0o755)
            env = {"TARTCI_GH_CLI": str(fake)}
            with mock.patch.dict(os.environ, env):
                os.environ.pop("GH_REPO", None)
                os.environ.pop("SHIPYARD_GHAPP_REPO", None)
                vm_reap.delete_runner("Generous-Corp/pulp", 7, "m5-pulp-gate-01-1-1",
                                      "orgs/Generous-Corp/actions/runners")
                # The binding does not outlive the call.
                self.assertNotIn("GH_REPO", os.environ)
            line = log.read_text()
            self.assertIn("GH_REPO=Generous-Corp/pulp SHIPYARD_GHAPP_REPO=Generous-Corp/pulp", line)
            self.assertIn("-X DELETE orgs/Generous-Corp/actions/runners/7", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
