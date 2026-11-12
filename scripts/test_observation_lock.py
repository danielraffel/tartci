import threading
import time
import tempfile
import os
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

from bounded_subprocess import ObservationError
from assignment_scan import TransientApiFault
from observation_lock import FairObservationLock, ObservationBackoff, _process_start


class ObservationLockTests(unittest.TestCase):
    def _real_timeout_scan(self, scanner: str, extra: list[str], td: str, expected_rc: int = 2) -> dict[str, float]:
        fake = Path(td) / "sleeping-gh"
        fake.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(2)\n")
        fake.chmod(0o755)
        lock = Path(td) / "observation.lock"
        backoff = Path(td) / "backoff.json"
        result = subprocess.run(
            ["python3", str(Path(__file__).with_name(scanner)), "--gh-cli", str(fake),
             "--observation-lock-file", str(lock), "--observation-backoff-file", str(backoff),
             "--gh-timeout", "1", *extra],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parent)},
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, expected_rc, result.stderr)
        return __import__("json").loads(backoff.read_text())

    def test_assignment_real_wrapper_timeout_records_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            values = self._real_timeout_scan(
                "assignment_scan.py",
                ["--repo", "forge/repo", "--workflow", "Build", "--labels", "x",
                 "--require-label", "x", "--scan-timeout", "5", "--max-workers", "1"], td)
            self.assertIn("forge/repo", values)

    def test_queue_real_wrapper_timeout_records_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            values = self._real_timeout_scan(
                "queue_scan.py",
                ["--repo", "forge/repo", "--workflow", "Build", "--labels", "x",
                 "--state-file", str(Path(td) / "state.json"), "--provider", "p",
                 "--max-job-fetches", "3", "--newest-quota", "1", "--force-refresh"], td, 1)
            self.assertIn("forge/repo", values)

    def test_current_job_real_wrapper_timeout_records_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            values = self._real_timeout_scan(
                "current_job_scan.py",
                ["--repo", "spectr/repo", "--runner", "r", "--workflow", "Build",
                 "--scan-timeout", "5"], td)
            self.assertIn("spectr/repo", values)

    def test_assignment_overall_deadline_records_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            values = self._real_timeout_scan(
                "assignment_scan.py",
                ["--repo", "forge/deadline", "--workflow", "Build", "--labels", "x",
                 "--require-label", "x", "--scan-timeout", "1", "--gh-timeout", "5"], td)
            self.assertIn("forge/deadline", values)

    def test_current_job_overall_deadline_records_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            values = self._real_timeout_scan(
                "current_job_scan.py",
                ["--repo", "spectr/deadline", "--runner", "r", "--workflow", "Build",
                 "--scan-timeout", "0.1", "--gh-timeout", "5"], td)
            self.assertIn("spectr/deadline", values)

    def test_fifo_order_honors_older_ticket(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "observation.lock"
            queue = Path(str(lock) + ".fifo")
            queue.mkdir()
            older = queue / f"00000000000000000000.{os.getpid()}"
            older.write_text(__import__("json").dumps({"pid": os.getpid(), "start": _process_start(os.getpid())}))
            entered = threading.Event()

            def waiter():
                with FairObservationLock(str(lock), 1, "repo").hold():
                    entered.set()

            thread = threading.Thread(target=waiter)
            thread.start()
            time.sleep(0.1)
            self.assertFalse(entered.is_set())
            older.unlink()
            thread.join(2)
            self.assertTrue(entered.is_set())

    def test_reused_pid_orphan_is_removed_by_start_time(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "observation.lock"
            queue = Path(str(lock) + ".fifo")
            queue.mkdir()
            orphan = queue / f"00000000000000000000.{os.getpid()}"
            orphan.write_text(__import__("json").dumps({"pid": os.getpid(), "start": "old-process-start"}))
            with FairObservationLock(str(lock), 1, "repo").hold():
                pass
            self.assertFalse(orphan.exists())

    def test_concurrent_backoff_marks_preserve_all_repositories(self):
        with tempfile.TemporaryDirectory() as td:
            lock = str(Path(td) / "observation.lock")
            backoff = str(Path(td) / "backoff.json")
            locks = [FairObservationLock(lock, 1, f"repo-{i}", backoff, 30) for i in range(20)]
            threads = [threading.Thread(target=item._mark_timeout) for item in locks]
            for thread in threads: thread.start()
            for thread in threads: thread.join(2)
            values = __import__("json").loads(Path(backoff).read_text())
            self.assertEqual(set(values), {f"repo-{i}" for i in range(20)})

    def test_timeout_sets_repo_backoff_without_blocking_other_repo(self):
        with tempfile.TemporaryDirectory() as td:
            lock = str(Path(td) / "observation.lock")
            backoff = str(Path(td) / "backoff.json")
            with self.assertRaises(TimeoutError):
                with FairObservationLock(lock, 1, "slow", backoff, 30).hold():
                    raise TimeoutError("scan exceeded its overall deadline")
            with self.assertRaises(ObservationBackoff):
                with FairObservationLock(lock, 1, "slow", backoff, 30).hold():
                    pass
            with FairObservationLock(lock, 1, "other", backoff, 30).hold():
                pass

    def test_queue_scan_real_timeout_shape_sets_repo_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            lock = str(Path(td) / "observation.lock")
            backoff = str(Path(td) / "backoff.json")
            error = ObservationError(
                "queue_scan_github_api", "timeout", "timed out after 30s"
            )
            wrapped = RuntimeError(str(error))
            wrapped.__cause__ = error
            with self.assertRaises(RuntimeError):
                with FairObservationLock(lock, 1, "forge/repo", backoff, 30).hold():
                    raise wrapped
            self.assertIn("forge/repo", __import__("json").loads(Path(backoff).read_text()))

    def test_assignment_scan_real_timeout_shape_sets_repo_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            lock = str(Path(td) / "observation.lock")
            backoff = str(Path(td) / "backoff.json")
            cause = ObservationError(
                "assignment_scan_github_api", "timeout", "timed out after 30.0s"
            )
            error = TransientApiFault(str(cause))
            error.__cause__ = cause
            with self.assertRaises(TransientApiFault):
                with FairObservationLock(lock, 1, "vellum/repo", backoff, 30).hold():
                    raise error
            self.assertIn("vellum/repo", __import__("json").loads(Path(backoff).read_text()))

    def test_backoff_expires_and_repository_can_scan_again(self):
        with tempfile.TemporaryDirectory() as td:
            lock = str(Path(td) / "observation.lock")
            backoff = str(Path(td) / "backoff.json")
            with self.assertRaises(TimeoutError):
                with FairObservationLock(lock, 1, "repo", backoff, 0.05).hold():
                    raise TimeoutError("scan exceeded its overall deadline")
            with self.assertRaises(ObservationBackoff):
                with FairObservationLock(lock, 1, "repo", backoff, 0.05).hold():
                    pass
            time.sleep(0.08)
            with FairObservationLock(lock, 1, "repo", backoff, 0.05).hold():
                pass

    def test_inherited_child_past_deadline_reports_owner_and_escalates(self):
        with tempfile.TemporaryDirectory() as td:
            lock = str(Path(td) / "observation.lock")
            ready = Path(td) / "child-ready"
            owner_code = """
import os, sys, time
from observation_lock import FairObservationLock
lock, ready = sys.argv[1:]
with FairObservationLock(lock, 5, "owner/repo", lane="lane-1", generation="gen-7").hold():
    pid = os.fork()
    if pid == 0:
        open(ready, "w").write(str(os.getpid()))
        time.sleep(3)
        os._exit(0)
    os._exit(0)
"""
            owner = subprocess.Popen(
                [sys.executable, "-c", owner_code, lock, str(ready)],
                env={**os.environ, "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
                     "PYTHONPATH": str(Path(__file__).parent)},
            )
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            try:
                with self.assertRaises(TimeoutError) as raised:
                    with FairObservationLock(
                        lock, 0.05, "contender/repo", max_age=0.01
                    ).hold():
                        pass
                message = str(raised.exception)
                self.assertIn("owner_pid=", message)
                self.assertIn(f"owner_pid={int(ready.read_text())}", message)
                self.assertIn("lane=lane-1", message)
                self.assertIn("generation=gen-7", message)
                self.assertIn("age=", message)
                self.assertIn("escalation=owner_age_exceeded", message)
                self.assertTrue(Path(lock + ".escalation.json").exists())
            finally:
                child = int(ready.read_text())
                os.kill(child, 15)
                time.sleep(0.05)
                owner.wait(timeout=3)

    def test_execed_child_holding_inherited_fd_is_named_by_live_holder_probe(self):
        with tempfile.TemporaryDirectory() as td:
            lock = str(Path(td) / "observation.lock")
            ready = Path(td) / "child-ready"
            owner_code = """
import os, subprocess, sys
from observation_lock import FairObservationLock
lock, ready = sys.argv[1:]
with FairObservationLock(lock, 5, "owner/repo", lane="lane-exec", generation="gen-exec").hold() as handle:
    inherited = os.dup(handle.fileno())
    os.set_inheritable(inherited, True)
    child = subprocess.Popen(["/bin/sleep", "3"], close_fds=False)
    open(ready, "w").write(str(child.pid))
    os.close(inherited)
    os._exit(0)
"""
            owner = subprocess.Popen(
                [sys.executable, "-c", owner_code, lock, str(ready)],
                env={**os.environ, "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
                     "PYTHONPATH": str(Path(__file__).parent)},
            )
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            try:
                import observation_lock as module
                with mock.patch.object(
                    module, "_live_lock_holders",
                    return_value=[os.getpid(), int(ready.read_text())],
                ):
                    with self.assertRaises(TimeoutError) as raised:
                        with FairObservationLock(lock, 0.05, "contender/repo", max_age=0.01).hold():
                            pass
                message = str(raised.exception)
                self.assertIn(f"owner_pid={int(ready.read_text())}", message)
                self.assertIn("lane=lane-exec", message)
                self.assertIn("generation=gen-exec", message)
                self.assertTrue(Path(lock + ".owner.json").exists())
            finally:
                child = int(ready.read_text())
                os.kill(child, 15)
                owner.wait(timeout=3)

    def test_released_lock_has_no_previous_owner_or_escalation(self):
        with tempfile.TemporaryDirectory() as td:
            lock = str(Path(td) / "observation.lock")
            with FairObservationLock(lock, 1, "repo", lane="lane", generation="gen").hold():
                pass
            self.assertFalse(Path(lock + ".owner.json").exists())
            queue = Path(lock + ".fifo")
            queue.mkdir(exist_ok=True)
            blocker = queue / f"00000000000000000000.{os.getpid()}"
            blocker.write_text(__import__("json").dumps({"pid": os.getpid(), "start": _process_start(os.getpid())}))
            try:
                with self.assertRaises(TimeoutError) as raised:
                    with FairObservationLock(lock, 0.01, "repo", max_age=0).hold():
                        pass
            finally:
                blocker.unlink()
            self.assertIn("owner=unknown age=unknown", str(raised.exception))
            self.assertFalse(Path(lock + ".escalation.json").exists())

    def test_pid_reuse_does_not_reuse_stale_lane_metadata(self):
        import observation_lock as module
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "observation.lock"
            owner_path = Path(str(lock) + ".owner.json")
            owner_path.write_text(__import__("json").dumps({
                "pid": 123, "process_start": "old", "lane": "stale-lane",
                "generation": "stale-generation", "started_epoch": time.time() - 10,
                "started_at": "old",
            }))
            item = FairObservationLock(str(lock), 1, "repo")
            with mock.patch.object(module, "_live_lock_holders", return_value=[123]), \
                 mock.patch.object(module, "_process_start", return_value="new"):
                detail = item._owner_detail()
            self.assertIn("owner_pid=123", detail)
            self.assertIn("lane=unknown", detail)
            self.assertIn("generation=unknown", detail)
            self.assertNotIn("stale-lane", detail)

    def test_holder_probe_excludes_contender_and_uses_last_live_candidate(self):
        import observation_lock as module
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "observation.lock"
            item = FairObservationLock(str(lock), 1, "repo")
            with mock.patch.object(module, "_live_lock_holders",
                                   return_value=[os.getpid(), 111, 222]), \
                 mock.patch.object(module, "_fifo_pids", return_value={os.getpid()}):
                detail = item._owner_detail()
            self.assertIn("owner_pid=222", detail)
            self.assertNotIn("owner_pid=111", detail)

    def test_holder_probe_excludes_self_and_fifo_waiters(self):
        import observation_lock as module
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "observation.lock"
            item = FairObservationLock(str(lock), 1, "repo")
            with mock.patch.object(module, "_live_lock_holders",
                                   return_value=[111, os.getpid()]), \
                 mock.patch.object(module, "_fifo_pids", return_value=set()):
                detail = item._owner_detail()
            self.assertIn("owner_pid=111", detail)
            with mock.patch.object(module, "_live_lock_holders",
                                   return_value=[111, 222]), \
                 mock.patch.object(module, "_fifo_pids", return_value={222}):
                detail = item._owner_detail()
            self.assertIn("owner_pid=111", detail)

    def test_fifo_pids_and_lsof_use_real_holder_shapes(self):
        import observation_lock as module
        if module._lsof_path() is None:
            self.skipTest("requires lsof")
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "observation.lock"
            queue = Path(str(lock) + ".fifo")
            queue.mkdir()
            with lock.open("a+") as handle:
                child = subprocess.Popen(["/bin/sleep", "3"],
                                         pass_fds=(handle.fileno(),))
                try:
                    holders = module._live_lock_holders(lock)
                    self.assertIn(child.pid, holders)
                    (queue / f"00000000000000000007.{child.pid}").write_text("ticket")
                    (queue / "not-a-ticket").write_text("ignored")
                    self.assertEqual(module._fifo_pids(lock), {child.pid})
                finally:
                    child.terminate()
                    child.wait(timeout=3)
    def test_unavailable_holder_probe_is_reported(self):
        import observation_lock as module
        with tempfile.TemporaryDirectory() as td:
            item = FairObservationLock(str(Path(td) / "observation.lock"), 1, "repo")
            with mock.patch.object(module, "_lsof_path", return_value=None), \
                 mock.patch.object(module, "_live_lock_holders", return_value=[]):
                detail = item._owner_detail()
            self.assertEqual(detail, "owner=unknown age=unknown holders=unavailable")

    def test_zombie_acquirer_is_not_reported_as_holder(self):
        import observation_lock as module
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "observation.lock"
            item = FairObservationLock(str(lock), 1, "repo")
            owner_path = Path(str(lock) + ".owner.json")
            owner_path.write_text(__import__("json").dumps({
                "pid": 123, "process_start": "start", "lane": "lane",
                "generation": "gen", "started_epoch": time.time() - 10,
                "started_at": "started",
            }))
            with mock.patch.object(module, "_live_lock_holders", return_value=[123]), \
                 mock.patch.object(module, "_pid_is_zombie", return_value=True):
                detail = item._owner_detail()
            self.assertEqual(detail, "owner=unknown age=unknown")

    def test_queue_and_current_job_entries_pass_lane_and_generation(self):
        import contextlib
        import importlib
        calls = []
        class ProbeLock:
            def __init__(self, *args):
                calls.append(args)
            def hold(self):
                class Handle:
                    def fileno(self):
                        return os.open(os.devnull, os.O_RDONLY)
                return contextlib.nullcontext(Handle())
        for module_name, method_name in (("queue_scan", "_observation_lock"), ("current_job_scan", "observation_lock")):
            module = importlib.import_module(module_name)
            old = module.FairObservationLock
            module.FairObservationLock = ProbeLock
            try:
                scanner = (module.QueueScanner if module_name == "queue_scan" else module.CurrentJobScanner).__new__(
                    module.QueueScanner if module_name == "queue_scan" else module.CurrentJobScanner)
                scanner.observation_lock_path = Path("/tmp/probe-observation.lock")
                scanner.args = type("Args", (), {
                    "observation_lock_timeout": 1, "repo": "owner/repo",
                    "observation_backoff_file": None, "observation_backoff_seconds": 1,
                    "lane_id": "lane-probe", "generation": "generation-probe",
                })()
                with getattr(scanner, method_name)():
                    pass
            finally:
                module.FairObservationLock = old
        self.assertEqual(calls[0][-2:], ("lane-probe", "generation-probe"))
        self.assertEqual(calls[1][-2:], ("lane-probe", "generation-probe"))

    def test_assignment_entry_point_passes_lane_and_generation_to_lock(self):
        import importlib
        module = importlib.import_module("assignment_scan")
        calls = []

        class ProbeLock:
            def __init__(self, *args):
                calls.append(args)
            def hold(self):
                class Handle:
                    def fileno(self):
                        return os.open(os.devnull, os.O_RDONLY)
                return __import__("contextlib").nullcontext(Handle())

        old = module.FairObservationLock
        module.FairObservationLock = ProbeLock
        try:
            scanner = module.AssignmentScanner.__new__(module.AssignmentScanner)
            scanner.observation_lock_path = Path("/tmp/assignment-probe.lock")
            scanner.args = type("Args", (), {
                "observation_lock_timeout": 4,
                "repo": "owner/repo",
                "observation_backoff_file": None,
                "observation_backoff_seconds": 30,
                "lane_id": "m5-pulp-gate-01",
                "generation": "gen-123",
                "scan_timeout": 10,
            })()
            with scanner._observation_lock():
                pass
            self.assertEqual(calls[0][-2:], ("m5-pulp-gate-01", "gen-123"))
        finally:
            module.FairObservationLock = old


if __name__ == "__main__":
    unittest.main()

class ObservationLockOverrideTests(unittest.TestCase):
    def _parse_with_env(self, module_name, argv, env_name):
        import importlib
        import os
        import sys
        module = importlib.import_module(module_name)
        old_argv = sys.argv
        old = os.environ.get("TARTCI_QUEUE_OBSERVATION_LOCK_FILE")
        try:
            os.environ["TARTCI_QUEUE_OBSERVATION_LOCK_FILE"] = "/tmp/explicit-observation.lock"
            sys.argv = [module_name, *argv]
            return module.parse_args().observation_lock_file
        finally:
            sys.argv = old_argv
            if old is None:
                os.environ.pop("TARTCI_QUEUE_OBSERVATION_LOCK_FILE", None)
            else:
                os.environ["TARTCI_QUEUE_OBSERVATION_LOCK_FILE"] = old

    def test_assignment_env_override_survives(self):
        self.assertEqual(self._parse_with_env("assignment_scan", [
            "--repo", "o/r", "--workflow", "W", "--labels", "x", "--require-label", "x"
        ], "TARTCI_QUEUE_OBSERVATION_LOCK_FILE"), "/tmp/explicit-observation.lock")

    def test_queue_env_override_survives(self):
        self.assertEqual(self._parse_with_env("queue_scan", [
            "--repo", "o/r", "--workflow", "W", "--labels", "x", "--state-file", "/tmp/state.json", "--provider", "p"
        ], "TARTCI_QUEUE_OBSERVATION_LOCK_FILE"), "/tmp/explicit-observation.lock")

    def test_current_job_env_override_survives(self):
        self.assertEqual(self._parse_with_env("current_job_scan", [
            "--repo", "o/r", "--runner", "runner", "--workflow", "W"
        ], "TARTCI_QUEUE_OBSERVATION_LOCK_FILE"), "/tmp/explicit-observation.lock")
