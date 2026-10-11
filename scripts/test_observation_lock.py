import threading
import time
import tempfile
import os
import unittest
from pathlib import Path

from observation_lock import FairObservationLock, ObservationBackoff, _process_start


class ObservationLockTests(unittest.TestCase):
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
