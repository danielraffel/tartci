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
        delete = body.index('tartci_tart_delete "$CURRENT_VM"')
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
            for name in (
                "bounded_teardown_command",
                "tartci_tart_delete",
                "terminate_current_guardian",
                "stop_current_aqua_runner",
                "discard_current_vm",
            )
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
                "TEARDOWN_STEP_TIMEOUT=1\n"
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

    def _signal_teardown(self, run_id: str) -> tuple[bool, str]:
        """Deliver INT/TERM to the supervisor and report (vm_deleted, output).

        Composes the real signal handler, cleanup, and teardown functions so the
        decision under test is the shipped one, not a paraphrase.
        """
        source = RUNNER.read_text()
        functions = "\n".join(
            f"{name}(){{\n{function_body(source, name)}}}"
            for name in (
                "bounded_teardown_command",
                "tartci_tart_delete",
                "terminate_current_guardian",
                "stop_current_aqua_runner",
                "discard_current_vm",
                "cleanup",
                "handle_supervisor_signal",
            )
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
                "TEARDOWN_STEP_TIMEOUT=20\n"
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


class PendingDeleteTests(unittest.TestCase):
    """An unproved `tart delete` parks the VM as pending-delete in-loop.

    The lane must keep the VM name, its lease and its reservation until the
    delete is proved, and must not need a supervisor restart to get there.
    """

    FUNCTIONS = (
        "bounded_teardown_command",
        "tartci_tart_delete",
        "terminate_current_guardian",
        "stop_current_aqua_runner",
        "discard_current_vm",
        "tart_vm_proved_absent",
        "tartci_release_pending_delete",
        "tartci_record_incident",
        "tartci_held_slot_self_check",
        "reconcile_pending_delete",
    )

    def _run(
        self,
        *,
        delete_failures: int,
        listed: bool,
        steps: str,
        delete_hangs: bool = False,
        list_hangs: bool = False,
        store: str = "missing",
        queued: int = 0,
        incidents: list[str] | None = None,
        delete_sleep: int = 0,
        list_hangs_once: bool = False,
    ) -> tuple[int, str, bool]:
        """Run the shipped teardown functions against a fake tart.

        `tart delete` fails for its first `delete_failures` calls and then
        succeeds; with `delete_hangs` it instead outlives every bound, like a
        delete of a large clone on a loaded host. While undeleted, `tart list`
        names the VM when `listed`; with `list_hangs` the listing times out.
        `store` shapes TART_HOME/vms: "missing" (no store), "without-vm" (a
        populated store whose VM directory is gone) or "with-vm".
        """
        source = RUNNER.read_text()
        functions = "\n".join(
            f"{name}(){{\n{function_body(source, name)}}}" for name in self.FUNCTIONS
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fakebin = root / "bin"
            fakebin.mkdir()
            count = root / "delete-count"
            deleted = root / "deleted"
            listing = '[{"Name":"test-vm","State":"stopped"}]' if listed else "[]"
            tart = fakebin / "tart"
            tart.write_text(
                "#!/bin/bash\n"
                "case \"$1\" in\n"
                "  stop) exit 0 ;;\n"
                "  delete)\n"
                + ("    exec sleep 30 ;;\n" if delete_hangs else
                   f"    sleep {int(delete_sleep)}\n"
                   f"    n=$(cat {str(count)!r} 2>/dev/null || echo 0); n=$((n + 1)); echo \"$n\" > {str(count)!r}\n"
                   f"    [ \"$n\" -gt {delete_failures} ] || exit 1\n"
                   f"    touch {str(deleted)!r} ;;\n")
                + "  list)\n"
                + ("    exec sleep 30 ;;\n" if list_hangs else
                   f"    [ -e {str(root / 'listed-once')!r} ] || {{ touch {str(root / 'listed-once')!r}; exec sleep 30; }}\n"
                   "    echo '[]' ;;\n" if list_hangs_once else
                   f"    if [ -e {str(deleted)!r} ]; then echo '[]'; else echo '{listing}'; fi ;;\n")
                + "esac\n"
            )
            tart_home = root / "tart-home"
            if store != "missing":
                (tart_home / "vms" / "golden:latest").mkdir(parents=True)
                if store == "with-vm":
                    (tart_home / "vms" / "test-vm").mkdir()
            incidents_file = root / "incidents.jsonl"
            tart.chmod(0o755)
            resv = root / "resv.test"
            resv.write_text("1 1")
            harness = root / "harness.sh"
            harness.write_text(
                "#!/bin/bash\nset -u\n"
                f"export PATH={str(fakebin)!r}:$PATH\n"
                f"TARTCI_ROOT={str(ROOT)!r}\n"
                + ("TEARDOWN_STEP_TIMEOUT=1\nPENDING_DELETE_TIMEOUT=1\n"
                   if (delete_hangs or list_hangs or list_hangs_once) else
                   "TEARDOWN_STEP_TIMEOUT=5\nPENDING_DELETE_TIMEOUT=5\n")
                + f"TART_HOME={str(tart_home)!r}\n"
                f"TARTCI_INCIDENTS_FILE={str(incidents_file)!r}\n"
                "DELETE_PROOF_ATTEMPTS=2\nDELETE_PROOF_BACKOFF_SECS=0\n"
                "HELD_SLOT_SELF_CHECK_SECS=300\nRUNNER_NAME=test-lane\n"
                "CURRENT_VM=test-vm\nCURRENT_IP=\nCURRENT_AQUA_LABEL=\nCURRENT_RPID=\n"
                f"CURRENT_RESV={str(resv)!r}\n"
                "CURRENT_TEARDOWN_PENDING=\nPENDING_DELETE_ATTEMPTS=0\n"
                "PENDING_DELETE_SINCE=\nPENDING_DELETE_LAST_SEEN=\n"
                "PENDING_DELETE_MAX_ATTEMPTS=3\n"
                f"queued_work(){{ echo {int(queued)}; }}\n"
                "note(){ printf 'note: %s\\n' \"$*\"; }\n"
                "event(){ printf 'event: %s\\n' \"$*\"; }\n"
                "tartci_release_vm_lease(){ printf 'lease-released\\n'; }\n"
                f"{functions}\n"
                f"{steps}\n"
            )
            harness.chmod(0o755)
            result = subprocess.run(
                [str(harness)], text=True, capture_output=True, check=False, timeout=60
            )
            # Read inside the temporary directory: it is gone after the block.
            if incidents is not None and incidents_file.exists():
                incidents.extend(incidents_file.read_text().splitlines())
            return result.returncode, result.stdout + result.stderr, resv.exists()

    def test_unproved_delete_parks_then_reconciles_without_restart(self) -> None:
        rc, out, _ = self._run(
            delete_failures=2,
            listed=True,
            steps=(
                "discard_current_vm && exit 10\n"
                "[ \"$CURRENT_TEARDOWN_PENDING\" = delete ] || exit 11\n"
                "[ \"$CURRENT_VM\" = test-vm ] || exit 12\n"
                "[ -e \"$CURRENT_RESV\" ] || exit 13\n"
                "echo PARKED\n"
                # First in-loop retry still fails: capacity stays held.
                "rc=0; reconcile_pending_delete || rc=$?\n"
                "[ \"$rc\" = 1 ] || exit 14\n"
                "[ \"$CURRENT_VM\" = test-vm ] || exit 15\n"
                "[ -e \"$CURRENT_RESV\" ] || exit 16\n"
                "echo STILL-PENDING\n"
                # Second retry proves the delete and releases everything.
                "reconcile_pending_delete || exit 17\n"
                "[ -z \"$CURRENT_VM\" ] || exit 18\n"
                "[ -z \"$CURRENT_RESV\" ] || exit 19\n"
                "[ -z \"$CURRENT_TEARDOWN_PENDING\" ] || exit 20\n"
                "echo RECONCILED\n"
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("reason=delete_unproved", out)
        self.assertIn("teardown_reconciled", out)
        # The lease is released exactly once, and only after the proof.
        self.assertEqual(out.count("lease-released"), 1, out)
        self.assertLess(out.index("STILL-PENDING"),
                        out.index("lease-released"))

    def test_reservation_file_is_removed_only_after_the_proof(self) -> None:
        rc, out, resv_exists = self._run(
            delete_failures=1, listed=True,
            steps="discard_current_vm; reconcile_pending_delete || exit 30",
        )
        self.assertEqual(rc, 0, out)
        self.assertFalse(resv_exists, "a reconciled pending-delete must free its reservation")

    def test_bound_exhausted_falls_back_to_restart_holding_capacity(self) -> None:
        rc, out, resv_exists = self._run(
            delete_failures=99,
            listed=True,
            steps=(
                "discard_current_vm\n"
                "for _ in 1 2 3; do\n"
                "  rc=0; reconcile_pending_delete || rc=$?\n"
                "done\n"
                "echo \"final=$rc attempts=$PENDING_DELETE_ATTEMPTS vm=$CURRENT_VM\"\n"
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("final=2 attempts=3 vm=test-vm", out)
        self.assertNotIn("lease-released", out)
        self.assertTrue(resv_exists, "an unproved VM must keep its reservation")

    def test_failed_delete_of_an_already_absent_vm_is_proved_by_inventory(self) -> None:
        rc, out, _ = self._run(
            delete_failures=99,
            listed=False,
            steps=(
                "discard_current_vm || exit 40\n"
                "[ -z \"$CURRENT_VM\" ] || exit 41\n"
                "[ -z \"$CURRENT_TEARDOWN_PENDING\" ] || exit 42\n"
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertNotIn("delete_unproved", out)

    def test_a_live_guardian_is_never_parked_as_pending_delete(self) -> None:
        # Only a terminal guardian qualifies: a live one may still run the guest.
        rc, out, _ = self._run(
            delete_failures=0,
            listed=True,
            steps=(
                "terminate_current_guardian(){ return 1; }\n"
                "CURRENT_RPID=424242\n"
                "discard_current_vm && exit 50\n"
                "[ -z \"$CURRENT_TEARDOWN_PENDING\" ] || exit 51\n"
                "rc=0; reconcile_pending_delete || rc=$?\n"
                "[ \"$rc\" = 2 ] || exit 52\n"
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("reason=guardian_live", out)

    # A `tart delete` that outlives its bound on a loaded host, followed by a
    # VM that is in fact gone, must release the slot instead of holding it
    # until the fail-closed restart.
    def test_timed_out_delete_of_an_absent_vm_is_released(self) -> None:
        rc, out, resv_exists = self._run(
            delete_failures=0, listed=False, delete_hangs=True,
            steps=(
                "CURRENT_TEARDOWN_PENDING=delete\n"
                "reconcile_pending_delete || exit 60\n"
                "[ -z \"$CURRENT_VM\" ] || exit 61\n"
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("teardown_reconciled", out)
        self.assertEqual(out.count("lease-released"), 1, out)
        self.assertFalse(resv_exists)

    def test_timed_out_delete_and_listing_with_the_vm_directory_gone_is_released(self) -> None:
        # Both the delete and the listing time out (the m3 incident under load
        # 21); the VM directory is gone from a populated store, which proves it.
        rc, out, resv_exists = self._run(
            delete_failures=0, listed=True, delete_hangs=True, list_hangs=True,
            store="without-vm",
            steps=(
                "CURRENT_TEARDOWN_PENDING=delete\n"
                "reconcile_pending_delete || exit 70\n"
                "[ -z \"$CURRENT_VM\" ] || exit 71\n"
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("teardown_reconciled", out)
        self.assertFalse(resv_exists)

    def test_pending_delete_gets_a_longer_bound_than_teardown(self) -> None:
        # A delete that needs 2 s is killed by a 1 s teardown bound every time;
        # the in-loop retry's own bound lets it finish.
        rc, out, resv_exists = self._run(
            delete_failures=0, listed=True, delete_sleep=2,
            steps=(
                "TEARDOWN_STEP_TIMEOUT=1; PENDING_DELETE_TIMEOUT=6\n"
                "CURRENT_TEARDOWN_PENDING=delete\n"
                "reconcile_pending_delete || exit 130\n"
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("teardown_reconciled", out)
        self.assertFalse(resv_exists)

    def test_absence_proof_rechecks_a_timed_out_listing(self) -> None:
        # The first listing times out under load; the re-check reads it.
        rc, out, resv_exists = self._run(
            delete_failures=0, listed=False, delete_hangs=True, list_hangs_once=True,
            steps=(
                "CURRENT_TEARDOWN_PENDING=delete\n"
                "reconcile_pending_delete || exit 140\n"
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("teardown_reconciled", out)
        self.assertFalse(resv_exists)

    def test_timed_out_listing_with_no_store_proves_nothing(self) -> None:
        # Control for the directory proof: no readable store, no proof.
        rc, out, resv_exists = self._run(
            delete_failures=0, listed=True, delete_hangs=True, list_hangs=True,
            store="missing",
            steps=(
                "CURRENT_TEARDOWN_PENDING=delete\n"
                "rc=0; reconcile_pending_delete || rc=$?\n"
                "[ \"$rc\" = 1 ] || exit 80\n"
                "[ \"$CURRENT_VM\" = test-vm ] || exit 81\n"
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("absence=unreadable", out)
        self.assertNotIn("lease-released", out)
        self.assertTrue(resv_exists)

    def test_timed_out_delete_of_a_present_vm_stays_held_with_an_incident(self) -> None:
        incidents: list[str] = []
        rc, out, resv_exists = self._run(
            delete_failures=0, listed=True, delete_hangs=True, store="with-vm",
            queued=2, incidents=incidents,
            steps=(
                "CURRENT_TEARDOWN_PENDING=delete\n"
                "for _ in 1 2 3; do\n"
                "  rc=0; reconcile_pending_delete || rc=$?\n"
                "done\n"
                "echo \"final=$rc vm=$CURRENT_VM\"\n"
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("final=2 vm=test-vm", out)
        self.assertIn("absence=present", out)
        self.assertIn("delete_unproved_incident", out)
        self.assertNotIn("lease-released", out)
        self.assertTrue(resv_exists, "a VM still present must keep its reservation")
        self.assertEqual(len(incidents), 1, incidents)
        self.assertIn('"incident":"delete_unproved"', incidents[0])

    # Held-slot self-check: a held slot with no VM anyone can see is released
    # after 5 minutes while this lane has queued work.
    STALE = (
        "CURRENT_TEARDOWN_PENDING=delete\n"
        "PENDING_DELETE_SINCE=$(( $(date +%s) - 400 ))\n"
    )

    def test_held_slot_without_any_vm_is_released_while_jobs_queue(self) -> None:
        incidents: list[str] = []
        # Nothing can be read (listing hangs, no store), so nothing proves the
        # delete, but nothing has seen the VM for 400 s either.
        rc, out, resv_exists = self._run(
            delete_failures=0, listed=True, delete_hangs=True, list_hangs=True,
            store="missing", queued=2, incidents=incidents,
            steps=self.STALE + "reconcile_pending_delete || exit 90\n"
            "[ -z \"$CURRENT_VM\" ] || exit 91\n",
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("held_slot_released", out)
        self.assertEqual(out.count("lease-released"), 1, out)
        self.assertFalse(resv_exists)
        self.assertEqual(len(incidents), 1, incidents)
        self.assertIn('"incident":"held_slot_released"', incidents[0])
        self.assertIn('"runner":"test-lane"', incidents[0])

    def test_running_vm_keeps_its_slot(self) -> None:
        # Negative control: the VM is still listed (and its directory exists),
        # so however long the hold and however deep the queue, the slot stays.
        incidents: list[str] = []
        rc, out, resv_exists = self._run(
            delete_failures=99, listed=True, store="with-vm", queued=5,
            incidents=incidents,
            steps=self.STALE + "rc=0; reconcile_pending_delete || rc=$?\n"
            "[ \"$rc\" = 1 ] || exit 100\n"
            "[ \"$CURRENT_VM\" = test-vm ] || exit 101\n",
        )
        self.assertEqual(rc, 0, out)
        self.assertNotIn("held_slot_released", out)
        self.assertNotIn("lease-released", out)
        self.assertTrue(resv_exists)
        self.assertEqual(incidents, [])

    def test_no_queued_work_keeps_the_hold(self) -> None:
        rc, out, resv_exists = self._run(
            delete_failures=0, listed=True, delete_hangs=True, list_hangs=True,
            store="missing", queued=0,
            steps=self.STALE + "rc=0; reconcile_pending_delete || rc=$?\n"
            "[ \"$rc\" = 1 ] || exit 110\n",
        )
        self.assertEqual(rc, 0, out)
        self.assertNotIn("held_slot_released", out)
        self.assertTrue(resv_exists)

    def test_a_recent_hold_is_not_released(self) -> None:
        rc, out, resv_exists = self._run(
            delete_failures=0, listed=True, delete_hangs=True, list_hangs=True,
            store="missing", queued=3,
            steps="CURRENT_TEARDOWN_PENDING=delete\n"
            "rc=0; reconcile_pending_delete || rc=$?\n"
            "[ \"$rc\" = 1 ] || exit 120\n",
        )
        self.assertEqual(rc, 0, out)
        self.assertNotIn("held_slot_released", out)
        self.assertTrue(resv_exists)


class PendingDeleteLoopWiringTests(unittest.TestCase):
    """The supervisor loop reconciles a pending-delete instead of exiting 75."""

    def _loop(self) -> str:
        source = RUNNER.read_text()
        start = source.index('if [ "$LOOP" = 1 ]; then')
        return source[start:source.index("\nelse\n", start)]

    def test_loop_top_reconciles_before_any_new_admission(self) -> None:
        loop = self._loop()
        body = loop[loop.index("while true; do"):]
        self.assertLess(body.index("reconcile_pending_delete"),
                        body.index("tartci_pool_admission_open"))
        # The slot claim (tartci_warm_or_claim_slot, which falls through to
        # tartci_claim_macos_slot unless a warm VM is parked).
        self.assertLess(body.index("reconcile_pending_delete"),
                        body.index('resv="$('))

    def test_pending_delete_continues_instead_of_restarting(self) -> None:
        loop = self._loop()
        after_run = loop[loop.index('run_one "$i"'):]
        park = after_run.index('[ "$CURRENT_TEARDOWN_PENDING" = delete ]')
        restart = after_run.index("teardown remained nonterminal")
        self.assertLess(park, restart)
        branch = after_run[park:restart]
        self.assertIn("continue", branch)
        self.assertNotIn("exit 75", branch)
        # The reservation must survive the park: it is removed by
        # reconcile_pending_delete only after the proof.
        self.assertNotIn('rm -f "$resv"', branch)


if __name__ == "__main__":
    unittest.main()
