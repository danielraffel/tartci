#!/usr/bin/env python3
"""Behavioral regression tests for the Tart Linux JIT runner lifecycle."""
from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-linux" / "runner.sh"
MONITOR = ROOT / "providers" / "common" / "runner-assignment.lib.sh"
CACHE_SETUP = ROOT / "providers" / "tart-linux" / "prepare-ccache.sh"
LAUNCHD_TEMPLATE = (
    ROOT / "launchd" / "com.danielraffel.pulp.tart-runner-linux.plist.template"
)


class RunnerAssignmentTests(unittest.TestCase):
    def _run_harness(self, runner_body: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        script = root / "harness.sh"
        script.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                set -u
                source {MONITOR!s}
                cleanup() {{
                  printf deleted > "$TEST_ROOT/vm.deleted"
                  printf released > "$TEST_ROOT/lease.released"
                }}
                assigned() {{ printf assigned > "$TEST_ROOT/job.assigned"; }}
                (
                  {runner_body}
                ) > "$TEST_ROOT/runner.log" 2>&1 &
                runner_pid=$!
                if tartci_monitor_runner_assignment \
                    "$runner_pid" "$TEST_ROOT/runner.log" 1 cleanup 0.1 assigned; then
                  rc=0
                else
                  rc=$?
                fi
                printf '%s' "$rc" > "$TEST_ROOT/status"
                exit "$rc"
                """
            ),
            encoding="utf-8",
        )
        started = time.monotonic()
        proc = subprocess.run(
            ["bash", str(script)],
            env={**os.environ, "TEST_ROOT": str(root)},
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        proc.elapsed = time.monotonic() - started  # type: ignore[attr-defined]
        return proc, root

    def test_never_assigned_runner_times_out_and_cleans_vm_and_lease(self) -> None:
        proc, root = self._run_harness(
            "trap 'exit 0' TERM; sleep 3; printf should-not-finish > "
            '"$TEST_ROOT/runner.finished"'
        )
        self.assertEqual(proc.returncode, 124, proc.stderr)
        self.assertEqual((root / "status").read_text(), "124")
        self.assertTrue((root / "vm.deleted").is_file())
        self.assertTrue((root / "lease.released").is_file())
        self.assertFalse((root / "runner.finished").exists())
        self.assertFalse((root / "job.assigned").exists())
        self.assertIn("runner_assignment=timeout", proc.stderr)

    def test_assignment_disables_deadline_and_valid_job_finishes(self) -> None:
        proc, root = self._run_harness(
            "printf 'Running job: valid-build\\n'; sleep 2; "
            'printf finished > "$TEST_ROOT/runner.finished"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreater(proc.elapsed, 1.5)  # type: ignore[attr-defined]
        self.assertTrue((root / "runner.finished").is_file())
        self.assertTrue((root / "job.assigned").is_file())
        self.assertTrue((root / "vm.deleted").is_file())
        self.assertTrue((root / "lease.released").is_file())
        self.assertIn("runner_assignment=claimed", proc.stderr)

    def test_assignment_marker_is_preserved_when_job_exits_between_polls(self) -> None:
        proc, root = self._run_harness(
            "printf 'Running job: short-build\\n'; "
            'printf finished > "$TEST_ROOT/runner.finished"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((root / "runner.finished").is_file())
        self.assertTrue((root / "job.assigned").is_file())
        self.assertIn("runner_assignment=claimed", proc.stderr)

    def test_authoritative_busy_verdict_prevents_timeout_kill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = textwrap.dedent(
                f"""\
                source {MONITOR!s}
                cleanup() {{ printf cleaned > "$TEST_ROOT/cleaned"; }}
                assigned() {{ printf assigned > "$TEST_ROOT/assigned"; }}
                authoritative_busy() {{ return 0; }}
                (
                  sleep 2
                  printf finished > "$TEST_ROOT/finished"
                ) > "$TEST_ROOT/runner.log" 2>&1 &
                pid=$!
                tartci_monitor_runner_assignment \
                  "$pid" "$TEST_ROOT/runner.log" 1 cleanup 0.1 assigned \
                  authoritative_busy
                """
            )
            proc = subprocess.run(
                ["bash", "-c", script],
                env={**os.environ, "TEST_ROOT": str(root)},
                capture_output=True,
                text=True,
                check=False,
                timeout=6,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((root / "finished").is_file())
            self.assertTrue((root / "assigned").is_file())
            self.assertTrue((root / "cleaned").is_file())
            self.assertIn("authoritative=github_busy", proc.stderr)

    def test_authoritative_errors_are_retried_but_remain_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = textwrap.dedent(
                f"""\
                source {MONITOR!s}
                cleanup() {{ printf cleaned > "$TEST_ROOT/cleaned"; }}
                authoritative_error() {{ return 2; }}
                (
                  sleep 3
                  printf finished > "$TEST_ROOT/finished"
                ) > "$TEST_ROOT/runner.log" 2>&1 &
                pid=$!
                tartci_monitor_runner_assignment \
                  "$pid" "$TEST_ROOT/runner.log" 1 cleanup 0.1 "" \
                  authoritative_error
                """
            )
            proc = subprocess.run(
                ["bash", "-c", script],
                env={
                    **os.environ,
                    "TEST_ROOT": str(root),
                    "TARTCI_RUNNER_ASSIGNMENT_VERIFY_ATTEMPTS": "2",
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=6,
            )
            self.assertEqual(proc.returncode, 124, proc.stderr)
            self.assertFalse((root / "finished").exists())
            self.assertTrue((root / "cleaned").is_file())
            self.assertIn("retries_exhausted=2", proc.stderr)

    def test_invalid_timeout_and_parallelism_fail_closed(self) -> None:
        proc = subprocess.run(
            [
                "bash",
                "-c",
                f"source {MONITOR!s}; tartci_validate_runner_idle_timeout nope",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must be a positive integer", proc.stderr)

        for value in ("", "0", "many", "65", "999999999999999999999999"):
            with self.subTest(value=value):
                proc = subprocess.run(
                    [
                        "bash",
                        "-c",
                        f"source {MONITOR!s}; "
                        "tartci_validate_bounded_positive_integer "
                        f"TARTCI_LINUX_BUILD_PARALLEL_LEVEL {value!r} 64",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 2, proc.stderr)

        proc = subprocess.run(
            [
                "bash",
                "-c",
                f"source {MONITOR!s}; "
                "tartci_validate_bounded_positive_integer test 0004 64",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


class LinuxCacheBindingTests(unittest.TestCase):
    def test_existing_ephemeral_directory_is_replaced_by_verified_host_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host_cache = root / "host-cache"
            guest_cache = root / "home" / ".ccache"
            host_cache.mkdir()
            guest_cache.mkdir(parents=True)
            (guest_cache / "ephemeral-object.o").write_text("cold", encoding="utf-8")

            proc = subprocess.run(
                ["bash", str(CACHE_SETUP), str(host_cache), str(guest_cache)],
                env={
                    **os.environ,
                    "TARTCI_CCACHE_MOUNT_INFO":
                        "virtiofs com.apple.virtio-fs.automount",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(guest_cache.is_symlink())
            self.assertEqual(guest_cache.resolve(), host_cache.resolve())
            self.assertFalse((guest_cache / "ephemeral-object.o").exists())
            (guest_cache / "warm-object.o").write_text("warm", encoding="utf-8")
            self.assertEqual((host_cache / "warm-object.o").read_text(), "warm")
            self.assertIn("ccache_binding=host", proc.stdout)

    def test_unusable_host_cache_fails_before_replacing_guest_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing_host_cache = root / "missing"
            guest_cache = root / "home" / ".ccache"
            guest_cache.mkdir(parents=True)
            sentinel = guest_cache / "keep"
            sentinel.write_text("present", encoding="utf-8")

            proc = subprocess.run(
                ["bash", str(CACHE_SETUP), str(missing_host_cache), str(guest_cache)],
                env={
                    **os.environ,
                    "TARTCI_CCACHE_MOUNT_INFO":
                        "virtiofs com.apple.virtio-fs.automount",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 70)
            self.assertTrue(sentinel.is_file())
            self.assertIn("ccache_binding=unusable", proc.stderr)

    def test_unrelated_mount_fails_before_replacing_guest_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host_cache = root / "host-cache"
            guest_cache = root / "home" / ".ccache"
            host_cache.mkdir()
            guest_cache.mkdir(parents=True)
            sentinel = guest_cache / "keep"
            sentinel.write_text("present", encoding="utf-8")

            proc = subprocess.run(
                ["bash", str(CACHE_SETUP), str(host_cache), str(guest_cache)],
                env={**os.environ, "TARTCI_CCACHE_MOUNT_INFO": "ext4 /dev/vda1"},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 70)
            self.assertTrue(sentinel.is_file())
            self.assertIn("ccache_binding=wrong_mount", proc.stderr)


class LinuxRunnerWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.body = RUNNER.read_text(encoding="utf-8")

    def test_script_syntax_is_valid(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(RUNNER)], capture_output=True, text=True, check=False
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_monitor_owns_cleanup_and_signal_paths(self) -> None:
        self.assertIn("tartci_monitor_runner_assignment", self.body)
        self.assertIn("discard_current_linux_vm 5 mark_runner_assigned", self.body)
        self.assertIn("linux_runner_authoritatively_busy", self.body)
        self.assertIn("delete_linux_runner_registration", self.body)
        self.assertIn("runner_registration_cleanup=retry", self.body)
        self.assertIn("runner_registration_cleanup=exhausted", self.body)
        self.assertIn("CURRENT_JIT_REGISTERED=1", self.body)
        self.assertIn("trap handle_linux_runner_signal INT TERM", self.body)
        self.assertIn("trap discard_current_linux_vm EXIT", self.body)

    def test_parallelism_defaults_to_four_and_is_capped_by_lease(self) -> None:
        self.assertIn(
            'BUILD_PARALLEL_LEVEL="${TARTCI_LINUX_BUILD_PARALLEL_LEVEL:-4}"',
            self.body,
        )
        self.assertIn(
            '[ "$build_parallel_effective" -gt "$lease_cores" ]', self.body
        )
        self.assertIn(
            'lease_cores="${TARTCI_ACTIVE_VM_LEASE_CORES:-$lease_cores}"',
            self.body,
        )
        self.assertIn(
            "export CMAKE_BUILD_PARALLEL_LEVEL='$build_parallel_effective'", self.body
        )
        self.assertIn('export CCACHE_DIR=\\"\\$HOME/.ccache\\"', self.body)
        self.assertIn("ccache_dir=%s", self.body)

    def test_launchagent_enables_six_hour_queue_age_guard(self) -> None:
        with LAUNCHD_TEMPLATE.open("rb") as handle:
            environment = plistlib.load(handle)["EnvironmentVariables"]
        self.assertEqual(
            environment["TARTCI_RUNNER_MAX_QUEUED_AGE_SECONDS"], "21600"
        )
        self.assertIn("cmake_build_parallel_level=%s", self.body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
