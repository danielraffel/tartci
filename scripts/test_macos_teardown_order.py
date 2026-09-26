#!/usr/bin/env python3
"""Structural guards for the macOS guardian-first teardown contract."""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"


TEARDOWN_FUNCTIONS = (
    "bounded_teardown_command",
    "tart_vm_directory_absent",
    "delete_current_vm_proved",
    "terminate_current_guardian",
    "stop_current_aqua_runner",
    "discard_current_vm",
)


def function_body(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\{{\n(.*?)^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group(1)


class MacosTeardownOrderTests(unittest.TestCase):
    def test_guardian_is_proved_terminal_before_tart_mutations(self) -> None:
        body = function_body(RUNNER.read_text(), "discard_current_vm")
        guardian = body.index("terminate_current_guardian")
        stop = body.index("bounded_teardown_command tart-stop")
        delete = body.index("delete_current_vm_proved")
        self.assertLess(guardian, stop)
        self.assertLess(stop, delete)
        self.assertNotIn("tart stop", body.replace("bounded_teardown_command tart-stop tart stop", ""))

    def test_cleanup_releases_capacity_only_after_terminal_teardown(self) -> None:
        body = function_body(RUNNER.read_text(), "cleanup")
        gate = body.index('if [ "$teardown_terminal" = 1 ]')
        self.assertGreater(body.index("tartci_release_vm_lease"), gate)
        self.assertGreater(body.index('rm -f "$CURRENT_RESV"'), gate)
        self.assertIn('reclaim_runner_name "$RUNNER_NAME" "$CURRENT_RUNNER_API_ROOT" 1', body)

    def test_hanging_tart_stop_cannot_hold_guardian_or_skip_delete(self) -> None:
        source = RUNNER.read_text()
        functions = "\n".join(
            f"{name}(){{\n{function_body(source, name)}}}"
            for name in TEARDOWN_FUNCTIONS
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fakebin = root / "bin"
            fakebin.mkdir()
            delete_marker = root / "deleted"
            tart = fakebin / "tart"
            tart.write_text(
                "#!/bin/bash\n"
                "case \"$1\" in\n"
                "  stop) sleep 60 ;;\n"
                f"  delete) touch {str(delete_marker)!r} ;;\n"
                "esac\n"
            )
            tart.chmod(0o755)
            harness = root / "harness.sh"
            harness.write_text(
                "#!/bin/bash\nset -u\n"
                f"export PATH={str(fakebin)!r}:$PATH\n"
                f"TARTCI_ROOT={str(ROOT)!r}\n"
                "TEARDOWN_STEP_TIMEOUT=1\nTEARDOWN_DELETE_DEADLINE=5\n"
                f"TART_HOME={str(root)!r}\n"
                "now_epoch(){ date +%s; }\n"
                "CURRENT_VM=test-vm\nCURRENT_IP=\nCURRENT_AQUA_LABEL=\n"
                "note(){ :; }\nevent(){ :; }\n"
                f"{functions}\n"
                "sleep 60 & CURRENT_RPID=$!\n"
                "guardian=$CURRENT_RPID\n"
                "discard_current_vm\n"
                "! kill -0 \"$guardian\" 2>/dev/null\n"
                "[ -z \"$CURRENT_VM\" ]\n"
            )
            harness.chmod(0o755)
            started = time.monotonic()
            result = subprocess.run(
                [str(harness)], text=True, capture_output=True, check=False, timeout=5
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(time.monotonic() - started, 3)
            self.assertTrue(delete_marker.exists(), "bounded delete was skipped")

    def _delete_teardown(
        self,
        delete_script: str,
        *,
        step: int = 1,
        deadline: int = 10,
        tart_home_exists: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], float, Path]:
        """Run the shipped discard_current_vm against a scripted `tart delete`.

        The VM directory is a real directory under a real TART_HOME, so the
        absence proof is exercised against the filesystem, not a stub.
        """
        source = RUNNER.read_text()
        functions = "\n".join(
            f"{name}(){{\n{function_body(source, name)}}}" for name in TEARDOWN_FUNCTIONS
        )
        raw = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", raw], check=False))
        root = Path(raw)
        fakebin = root / "bin"
        fakebin.mkdir()
        tart_home = root / "tart-home"
        vm_dir = tart_home / "vms" / "test-vm"
        if tart_home_exists:
            vm_dir.mkdir(parents=True)
            for name in ("config.json", "nvram.bin", "disk.img"):
                (vm_dir / name).write_text("x")
        tart = fakebin / "tart"
        tart.write_text(
            "#!/bin/bash\n"
            f"VM_DIR={str(vm_dir)!r}\n"
            f"CALLS={str(root / 'delete-calls')!r}\n"
            "case \"$1\" in\n"
            "  stop) exit 1 ;;\n"
            "  delete) echo x >>\"$CALLS\"\n"
            f"{delete_script}\n"
            "  ;;\n"
            "esac\n"
        )
        tart.chmod(0o755)
        harness = root / "harness.sh"
        harness.write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            f"export PATH={str(fakebin)!r}:$PATH\n"
            f"TARTCI_ROOT={str(ROOT)!r}\n"
            f"TART_HOME={str(tart_home)!r}\n"
            f"TEARDOWN_STEP_TIMEOUT={step}\nTEARDOWN_DELETE_DEADLINE={deadline}\n"
            "TEARDOWN_DELETE_ATTEMPTS=0\nTEARDOWN_DELETE_LAST_ERROR=\n"
            "CURRENT_VM=test-vm\nCURRENT_IP=\nCURRENT_AQUA_LABEL=\nCURRENT_RPID=\n"
            "now_epoch(){ date +%s; }\n"
            "note(){ printf 'note: %s\\n' \"$*\"; }\n"
            "event(){ printf 'event: %s\\n' \"$*\"; }\n"
            f"{functions}\n"
            "rc=0; discard_current_vm || rc=$?\n"
            "printf 'current_vm=%s\\n' \"$CURRENT_VM\"\n"
            "exit \"$rc\"\n"
        )
        harness.chmod(0o755)
        started = time.monotonic()
        result = subprocess.run(
            [str(harness)], text=True, capture_output=True, check=False, timeout=60
        )
        return result, time.monotonic() - started, vm_dir

    def test_delete_slower_than_one_step_is_given_the_whole_deadline(self) -> None:
        # Freeing a finished gate VM's disk image routinely outlasts one teardown
        # step. Killing that delete at the step bound is what left the proof
        # unprovable on almost every gate teardown.
        result, _, vm_dir = self._delete_teardown(
            'sleep 2; rm -rf "$VM_DIR"; exit 0', step=1, deadline=10
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("current_vm=\n", result.stdout)
        self.assertFalse(vm_dir.exists())
        self.assertNotIn("delete_unproved", output)

    def test_husk_left_by_an_interrupted_delete_is_retried_to_proof(self) -> None:
        # The first delete frees the disk image and then fails the way a killed
        # or lock-contended delete does; the retry removes the remaining husk.
        result, _, vm_dir = self._delete_teardown(
            'if [ -e "$VM_DIR/disk.img" ]; then rm -f "$VM_DIR/disk.img"; '
            'echo "VM is running" >&2; exit 1; fi; rm -rf "$VM_DIR"; exit 0'
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("current_vm=\n", result.stdout)
        self.assertFalse(vm_dir.exists())
        self.assertIn("teardown_delete_retried vm=test-vm attempts=2", output)

    def test_vm_directory_already_gone_is_proof_of_deletion(self) -> None:
        # A delete whose predecessor already finished the removal reports the
        # VM missing. The directory's absence under TART_HOME is the proof.
        result, _, vm_dir = self._delete_teardown(
            'rm -rf "$VM_DIR"; echo "VM does not exist" >&2; exit 1'
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("current_vm=\n", result.stdout)
        self.assertFalse(vm_dir.exists())

    def test_unprovable_delete_stays_fail_closed_within_its_deadline(self) -> None:
        # Negative control for the retries above: a VM that really stays on disk
        # must keep teardown nonterminal (the caller keeps the lease and exits
        # 75), and the retry loop must not outlive its bounded budget.
        result, elapsed, vm_dir = self._delete_teardown(
            'echo "VM is running" >&2; exit 1', deadline=2
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("current_vm=test-vm\n", result.stdout)
        self.assertTrue(vm_dir.exists())
        self.assertIn("reason=delete_unproved", output)
        self.assertIn("error=VM is running", output)
        calls = (vm_dir.parents[2] / "delete-calls").read_text().count("x")
        self.assertGreaterEqual(calls, 2, "the delete was never retried")
        self.assertLess(elapsed, 2 + 4)

    def test_missing_tart_home_is_not_read_as_absence(self) -> None:
        # Control for the absence proof: a TART_HOME that does not exist would
        # read every VM as absent. It must prove nothing.
        result, _, _ = self._delete_teardown(
            'echo "VM does not exist" >&2; exit 1', deadline=2, tart_home_exists=False
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("current_vm=test-vm\n", result.stdout)
        self.assertIn("reason=delete_unproved", output)

    def test_default_teardown_budget_fits_inside_launchd_exit_timeout(self) -> None:
        # A signalled supervisor runs this teardown in its TERM handler. launchd
        # SIGKILLs the lane ExitTimeOut after SIGTERM, so at their defaults the
        # aqua stop, the tart stop and the whole delete budget must fit inside it.
        source = RUNNER.read_text()
        step = int(re.search(r'TARTCI_TEARDOWN_STEP_TIMEOUT_SECS:-(\d+)', source).group(1))
        deadline = int(
            re.search(r'TARTCI_TEARDOWN_DELETE_DEADLINE_SECS:-(\d+)', source).group(1)
        )
        lanes = (ROOT / "scripts" / "macos_fleet_lanes.py").read_text()
        exit_timeout = int(re.search(r'"ExitTimeOut": (\d+)', lanes).group(1))
        self.assertLess(2 * step + deadline, exit_timeout)
        # One step bound is what the budget replaces; it must be strictly larger.
        self.assertGreater(deadline, step)

    def _signal_teardown(self, run_id: str) -> tuple[bool, str]:
        """Deliver INT/TERM to the supervisor and report (vm_deleted, output).

        Composes the real signal handler, cleanup, and teardown functions so the
        decision under test is the shipped one, not a paraphrase.
        """
        source = RUNNER.read_text()
        functions = "\n".join(
            f"{name}(){{\n{function_body(source, name)}}}"
            for name in (*TEARDOWN_FUNCTIONS, "cleanup", "handle_supervisor_signal")
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fakebin = root / "bin"
            fakebin.mkdir()
            delete_marker = root / "deleted"
            stop_marker = root / "stopped"
            tart = fakebin / "tart"
            tart.write_text(
                "#!/bin/bash\n"
                "case \"$1\" in\n"
                f"  stop) touch {str(stop_marker)!r} ;;\n"
                f"  delete) touch {str(delete_marker)!r} ;;\n"
                "esac\n"
            )
            tart.chmod(0o755)
            harness = root / "harness.sh"
            harness.write_text(
                "#!/bin/bash\nset -u\n"
                f"export PATH={str(fakebin)!r}:$PATH\n"
                f"TARTCI_ROOT={str(ROOT)!r}\n"
                # Generous on purpose: the bounded-timeout contract is proved
                # by the test above. This one must not fail for host load.
                "TEARDOWN_STEP_TIMEOUT=20\nTEARDOWN_DELETE_DEADLINE=20\n"
                f"TART_HOME={str(root)!r}\n"
                "now_epoch(){ date +%s; }\n"
                "CURRENT_VM=test-vm\nCURRENT_IP=\nCURRENT_AQUA_LABEL=\n"
                "CURRENT_RPID=\nCURRENT_SCAN_PID=\nCURRENT_SCAN_TMP=\n"
                "CURRENT_REGISTERED_RUNNER=\nCURRENT_RUNNER_API_ROOT=\n"
                "CURRENT_RESV=\nCURRENT_JOB_ID=job-1\nCLEANED_UP=0\n"
                "RUNNER_NAME=test-runner\n"
                "CURRENT_ASSIGNMENT_QUARANTINE=none\n"
                "CURRENT_JOB_CAPTURE_STATUS=active\nCURRENT_JOB_RECEIPT=\n"
                f"CURRENT_RUN_ID={run_id!r}\n"
                "note(){ printf 'note: %s\\n' \"$*\"; }\n"
                "event(){ printf 'event: %s\\n' \"$*\"; }\n"
                "heartbeat(){ :; }\n"
                "tartci_pool_lock_release(){ :; }\n"
                "tartci_release_vm_lease(){ printf 'lease-released\\n'; }\n"
                "reclaim_runner_name(){ :; }\n"
                f"{functions}\n"
                "handle_supervisor_signal\n"
            )
            harness.chmod(0o755)
            result = subprocess.run(
                [str(harness)], text=True, capture_output=True, check=False, timeout=60
            )
            self.assertEqual(result.returncode, 143, result.stderr)
            self.assertFalse(
                delete_marker.exists() and not stop_marker.exists(),
                "delete without stop would mean the harness bypassed teardown order",
            )
            return delete_marker.exists(), result.stdout + result.stderr

    def test_signal_teardown_refuses_to_delete_a_vm_with_a_live_assignment(self) -> None:
        # A supervisor signal is not proof the job is over. launchd delivers
        # SIGTERM for any bootout — including one the launchd watchdog issues on
        # a misread — while a gate job may still be running inside the guest.
        # Deleting that VM force-fails a live job with no failed step.
        deleted, output = self._signal_teardown("12345")
        self.assertFalse(
            deleted,
            f"deleted a VM whose run was still in flight; output:\n{output}",
        )
        self.assertIn("teardown_refused", output)
        self.assertNotIn(
            "lease-released", output,
            "a refused teardown must keep owning capacity, not hand it back",
        )

    def test_signal_teardown_without_an_assignment_still_reclaims_the_vm(self) -> None:
        # Positive control for the test above: with no live run, the identical
        # harness MUST reach the destructive half. Without this, a refusal that
        # simply never reached `tart delete` would read as a pass.
        deleted, output = self._signal_teardown("")
        self.assertTrue(
            deleted,
            f"an idle supervisor must still tear its VM down; output:\n{output}",
        )


if __name__ == "__main__":
    unittest.main()
