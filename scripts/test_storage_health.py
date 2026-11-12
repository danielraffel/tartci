#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "storage_health.py"
sys.path.insert(0, str(ROOT / "scripts"))
import storage_health  # noqa: E402


class StorageHealthTests(unittest.TestCase):
    def run_probe(self, path: Path, state: Path, **extra):
        args = [sys.executable, str(SCRIPT), "--path", str(path), "--state", str(state)]
        for key, value in extra.items():
            args.extend([f"--{key.replace('_', '-')}", str(value)])
        return subprocess.run(args, text=True, capture_output=True, check=False, timeout=10)

    def test_metadata_probe_is_healthy_without_creating_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            result = self.run_probe(root, state)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["reason"], "directory_metadata_ok")
            self.assertEqual(list(root.iterdir()), [])

    def test_missing_path_is_not_created(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "missing-volume"
            state = Path(temp) / "state.json"
            result = self.run_probe(root, state)
            self.assertEqual(result.returncode, 10, result.stderr)
            self.assertFalse(root.exists())

    def test_two_failures_quarantine_and_three_passes_recover(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            root.rmdir()
            first = self.run_probe(root, state, failure_threshold=2, recovery_threshold=3)
            second = self.run_probe(root, state, failure_threshold=2, recovery_threshold=3)
            root.mkdir()
            self.assertEqual(first.returncode, 10)
            self.assertEqual(second.returncode, 20)
            for index in (1, 2):
                recovering = self.run_probe(root, state, failure_threshold=2, recovery_threshold=3)
                self.assertEqual(recovering.returncode, 20, index)
            recovered = self.run_probe(root, state, failure_threshold=2, recovery_threshold=3)
            self.assertEqual(recovered.returncode, 0)
            self.assertEqual(json.loads(recovered.stdout)["status"], "healthy")

    def test_quarantine_marker_survives_state_loss(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            root.rmdir()
            self.assertEqual(self.run_probe(root, state).returncode, 10)
            self.assertEqual(self.run_probe(root, state).returncode, 20)
            root.mkdir()
            self.assertEqual(self.run_probe(root, state).returncode, 20)
            state.unlink()
            still_closed = self.run_probe(root, state)
            self.assertEqual(still_closed.returncode, 20)

    def test_quarantine_marker_overrides_healthy_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            self.assertEqual(self.run_probe(root, state).returncode, 0)
            Path(str(state) + ".quarantine").touch()
            recovering = self.run_probe(root, state, recovery_threshold=2)
            self.assertEqual(recovering.returncode, 20)
            self.assertEqual(json.loads(recovering.stdout)["reason"], "recovery_check_pending")

    def test_durable_quarantine_keeps_marker_when_state_write_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            args = argparse.Namespace(failure_threshold=2)
            with patch.object(storage_health, "save_state", side_effect=OSError("wedged state")):
                result = storage_health.durable_quarantine(root, state, args, "health_worker_failed")
            self.assertEqual(result["status"], "quarantined")
            self.assertTrue(Path(str(state) + ".quarantine").exists())

    def test_unbounded_configuration_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            result = self.run_probe(root, state, timeout="61")
            self.assertEqual(result.returncode, 20)

    def test_probe_timeout_is_bounded_and_degraded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            result = self.run_probe(root, state, timeout="0.001")
            self.assertEqual(result.returncode, 10)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["reason"], "probe_timeout")
            self.assertLessEqual(payload["elapsed_seconds"], 0.001)

    def test_runner_guards_once_before_pool_admission(self):
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        once = runner.rsplit("else\n", 1)[1]
        self.assertLess(once.index("tartci_storage_health_check"), once.index("tartci_pool_admission_open"))
        self.assertLess(once.index("tartci_claim_macos_slot"), once.index("run_one 1"))
        self.assertIn("running_macos_vms", once)
        self.assertIn('[ -z "$CURRENT_VM" ] || continue', runner)

    def test_loop_guards_storage_before_warm_reconciliation(self):
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        loop = runner[runner.index("heartbeat loop\n  while true; do"):]
        loop = loop.split("\nelse\n", 1)[0]
        self.assertLess(loop.index("tartci_storage_health_check"), loop.index("tartci_warm_tick"))
        self.assertEqual(loop.count("tartci_storage_health_check"), 1)
        self.assertIn("heartbeat yielded-storage-health", runner)
        self.assertIn("--observe-only", runner)

    def test_runner_has_one_storage_gate_and_uses_read_only_probe(self):
        """The #625 runner gate must not be shadowed by a sourced duplicate."""
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        self.assertEqual(runner.count("tartci_storage_health_check(){"), 1)
        self.assertNotIn("providers/common/storage-health.lib.sh", runner)
        body = runner[runner.index("tartci_storage_health_check(){"):]
        body = body[:body.index("\nruntime_emit_complete(){")]
        self.assertIn("scripts/storage_health.py", body)
        self.assertIn("--failure-threshold", body)
        self.assertIn("--recovery-threshold", body)
        probe = (ROOT / "scripts" / "storage_health.py").read_text()
        self.assertIn("O_RDONLY", probe)
        self.assertIn("O_DIRECTORY", probe)
        self.assertIn("os.fstat", probe)
        self.assertNotIn("mkstemp(prefix=\".tartci-storage-probe-\"", probe)
        self.assertNotIn("write_fsync_unlink_ok", probe)

    def test_storage_gate_does_not_change_capacity_or_active_vm_state(self):
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        body = runner[runner.index("tartci_storage_health_check(){"):]
        body = body[:body.index("\nruntime_emit_complete(){")]
        self.assertNotIn("tartci_vm_lease", body)
        self.assertNotIn("tartci_effective_cap", body)
        self.assertNotIn("tart stop", body)
        self.assertNotIn("tart delete", body)

    def test_heartbeat_fsyncs_file_and_state_directory(self):
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        heartbeat = runner[runner.index("heartbeat(){"):runner.index("tartci_storage_health_check(){")]
        self.assertIn("os.fsync(fd)", heartbeat)
        self.assertIn('mv -f "$tmp_file" "$state_file"', heartbeat)

    def test_unreadable_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            state.write_text("not json")
            result = self.run_probe(root, state)
            self.assertEqual(result.returncode, 20)
            self.assertEqual(json.loads(result.stdout)["reason"], "health_state_unreadable")

    def test_observe_only_probes_without_advancing_or_writing_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            initial = self.run_probe(root, state)
            before = state.read_bytes()
            observed = subprocess.run(
                [sys.executable, str(SCRIPT), "--path", str(root),
                 "--state", str(state), "--observe-only"],
                text=True, capture_output=True, check=False, timeout=10,
            )
            self.assertEqual(initial.returncode, 0)
            self.assertEqual(observed.returncode, 0, observed.stderr)
            self.assertEqual(state.read_bytes(), before)
            self.assertEqual(json.loads(observed.stdout)["reason"],
                             "directory_metadata_ok")

    def test_timeout_with_explicit_state_never_writes_that_path(self):
        class TimedOutProcess:
            pid = 12345
            stdout = None
            stderr = None

            def communicate(self, **_kwargs):
                raise subprocess.TimeoutExpired("probe", 1)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            args = argparse.Namespace(path=str(root), state=str(state), timeout=1,
                                      failure_threshold=2, recovery_threshold=3)
            with patch.object(storage_health.subprocess, "Popen",
                              return_value=TimedOutProcess()), \
                 patch.object(storage_health, "reap_after_kill", return_value=True):
                result = storage_health.run(args)
            self.assertEqual(result, storage_health.QUARANTINED)
            self.assertFalse(state.exists())

    def test_worker_failure_with_explicit_state_never_writes_that_path(self):
        class FailedProcess:
            returncode = 99
            stdout = None
            stderr = None

            def communicate(self, **_kwargs):
                return "", "worker failed"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            args = argparse.Namespace(path=str(root), state=str(state), timeout=1,
                                      failure_threshold=2, recovery_threshold=3)
            with patch.object(storage_health.subprocess, "Popen",
                              return_value=FailedProcess()):
                result = storage_health.run(args)
            self.assertEqual(result, storage_health.QUARANTINED)
            self.assertFalse(state.exists())

    def test_state_path_inside_probe_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = root / ".tartci" / "state.json"
            result = self.run_probe(root, state)
            self.assertEqual(result.returncode, 20)
            self.assertEqual(json.loads(result.stdout)["reason"], "health_state_path_unsafe")
            self.assertEqual(list(root.iterdir()), [])

    def test_symlinked_state_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "volume"
            root.mkdir()
            state_root = temp_path / "state-root"
            state_root.mkdir()
            alias = temp_path / "alias"
            alias.symlink_to(state_root, target_is_directory=True)
            result = self.run_probe(root, alias / "state.json")
            self.assertEqual(result.returncode, 20)
            self.assertEqual(json.loads(result.stdout)["reason"], "health_state_path_unsafe")
            self.assertEqual(list(state_root.iterdir()), [])

    def test_state_alias_into_probe_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "volume"
            root.mkdir()
            alias = temp_path / "alias"
            alias.symlink_to(root, target_is_directory=True)
            result = self.run_probe(root, alias / "state.json")
            self.assertEqual(result.returncode, 20)
            self.assertEqual(json.loads(result.stdout)["reason"], "health_state_path_unsafe")
            self.assertEqual(list(root.iterdir()), [])

    def test_target_alias_and_state_target_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "volume"
            root.mkdir()
            target_alias = temp_path / "target-alias"
            target_alias.symlink_to(root, target_is_directory=True)
            state = root / "state.json"
            result = self.run_probe(target_alias, state)
            self.assertEqual(result.returncode, 20)
            self.assertEqual(json.loads(result.stdout)["reason"], "health_state_path_unsafe")
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
