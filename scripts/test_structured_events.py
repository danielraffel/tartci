#!/usr/bin/env python3
"""Mechanisms the macOS supervisor must record as typed events.jsonl fields.

Four decisions used to leave no machine-readable trace:

* job_claim detail read `detail=unreadable` on every event on every host
  (a dynamic-scoping bug hid the claim result from its caller);
* a lease denial reached only the note stream, never events.jsonl;
* an admission contention wait lived only in an envelope file that the next
  attempt overwrote;
* assignment_v2_pre_mint_denied did not name the class that caused it.

Each is exercised through the shipped shell, not a transcription of it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers/tart-macos/runner.sh"
JOB_CLAIM_LIB = ROOT / "providers/tart-macos/job-claim.lib.sh"
VM_LEASE_LIB = ROOT / "providers/common/vm-lease.lib.sh"
ADMISSION_LIB = ROOT / "providers/common/admission-clean.lib.sh"
ASSIGNMENT_LIB = ROOT / "providers/tart-macos/assignment-v2.lib.sh"

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_assignment_v2 import RunnerFixture  # noqa: E402


def shell_function(path: Path, name: str) -> str:
    """The exact text of a top-level shell function in a shipped file."""
    body = path.read_text()
    match = re.search(rf"^{re.escape(name)}\(\)\s*\{{.*?^\}}\n", body, re.S | re.M)
    if match is None:
        raise AssertionError(f"{name} not found in {path}")
    return match.group(0)


def run_bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full = os.environ.copy()
    full.update(env or {})
    return subprocess.run(["/bin/bash", "-c", script], env=full, capture_output=True,
                          text=True, check=False, timeout=60)


def events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class EventFieldsTests(unittest.TestCase):
    """The runner's own event() writer."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log = Path(self._tmp.name) / "events.jsonl"
        self.prelude = (
            shell_function(RUNNER, "json_sanitize")
            + shell_function(RUNNER, "event")
            + f"EVENT_LOG={str(self.log)!r}\nRUNNER_NAME=lane\nCURRENT_VM=vm-1\n"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fields_are_typed_and_detail_is_kept(self) -> None:
        proc = run_bash(self.prelude + (
            "event lease_denied 'axis=disk reason=disk_capacity_exceeded' "
            "axis=cores+disk reason=disk_capacity_exceeded requested_cores=12 "
            "ratio=0.5 zero=0 padded=007 negative=-3 'path=a\\b\"c' "
            "Bad-Key=1 empty= novalue\n"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (row,) = events(self.log)
        self.assertEqual(row["event"], "lease_denied")
        self.assertEqual(row["detail"], "axis=disk reason=disk_capacity_exceeded")
        self.assertEqual(row["fields"], {
            "axis": "cores+disk", "reason": "disk_capacity_exceeded",
            "requested_cores": 12, "ratio": 0.5, "zero": 0, "padded": "007",
            "negative": -3, "path": "a\\b c",
        })

    def test_an_event_without_fields_keeps_the_old_shape(self) -> None:
        proc = run_bash(self.prelude + "event loop 'queued=1'\nevent bare\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = events(self.log)
        self.assertEqual([sorted(r) for r in rows],
                         [["detail", "event", "runner", "ts", "vm"]] * 2)
        self.assertEqual(rows[1]["detail"], "")


class JobClaimEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log = self.tmp / "events.jsonl"
        (self.tmp / "bin").mkdir()
        gh = self.tmp / "bin" / "stub-gh"
        gh.write_text("#!/bin/bash\nexit 0\n")
        gh.chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _script(self, body: str) -> str:
        return (
            "set -euo pipefail\n"
            f"TARTCI_ROOT={str(ROOT)!r}\n"
            f"source {str(JOB_CLAIM_LIB)!r}\n"
            + shell_function(RUNNER, "json_sanitize")
            + shell_function(RUNNER, "event")
            + f"EVENT_LOG={str(self.log)!r}\nCURRENT_VM=\n"
            "note(){ :; }\n"
            "REPO=Generous-Corp/pulp\nRUNNER_NAME=lane\nSLOT=1\nGH_CLI=stub-gh\n"
            "ASSIGNMENT_MODE=legacy\n"
            + body
        )

    def _env(self) -> dict:
        return {"PATH": f"{self.tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
                "TARTCI_JOB_CLAIM_DIR": str(self.tmp / "claims")}

    def test_claim_result_reaches_the_caller(self) -> None:
        """The scoping bug itself: the helper's local shadowed the caller's."""
        proc = run_bash(self._script(
            "f(){ local out=''; _tartci_job_claim_call out acquire --repo x --labels a "
            "--claim-id c --lane l --vm v --pid $$ --queued 1 "
            f"--dir {str(self.tmp / 'claims')!r} || true; printf '[%s]' \"$out\"; }}\nf\n"),
            self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stdout, r'^\[\{.*"verdict"')

    def test_job_claim_event_carries_queued_and_standing(self) -> None:
        proc = run_bash(self._script(
            "tartci_job_claim_acquire vm-me self-hosted,pulp-build-pr-head 1 3 "
            "repos/x/actions/runners\n"), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (row,) = [r for r in events(self.log) if r["event"] == "job_claim"]
        self.assertNotIn("unreadable", row["detail"])
        self.assertEqual(row["fields"]["queued"], 3)
        self.assertEqual(row["fields"]["standing"], 0)
        self.assertIn("local", row["fields"])
        self.assertIn("fleet_idle", row["fields"])


DISK_DENIAL = {
    "ok": False, "reason": "disk_capacity_exceeded",
    "exceeded_axis": {"cores": False, "memory": False, "disk": True},
    "requested_cores": 12, "requested_mem_mb": 24576,
    "disk": {"requested_bytes": 25769803776, "free_bytes": 1000, "required_bytes": 78383153152},
}


class LeaseDeniedEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log = Path(self._tmp.name) / "events.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, out: str, with_event: bool = True) -> subprocess.CompletedProcess:
        writer = (shell_function(RUNNER, "json_sanitize") + shell_function(RUNNER, "event")
                  if with_event else "")
        return run_bash(
            f"TARTCI_ROOT={str(ROOT)!r}\n"
            + shell_function(VM_LEASE_LIB, "tartci_vm_lease_denied_event")
            + writer
            + f"EVENT_LOG={str(self.log)!r}\nRUNNER_NAME=lane\nCURRENT_VM=\n"
            + f"tartci_vm_lease_denied_event {shlex.quote(out)} 75 tart-macos-vm 12 24576 50\n")

    def test_a_disk_denial_is_an_event_with_its_axis_and_request(self) -> None:
        proc = self._run(json.dumps(DISK_DENIAL))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (row,) = events(self.log)
        self.assertEqual(row["event"], "lease_denied")
        f = row["fields"]
        self.assertEqual((f["axis"], f["reason"]), ("disk", "disk_capacity_exceeded"))
        self.assertEqual((f["requested_cores"], f["requested_mem_mb"]), (12, 24576))
        self.assertEqual(f["requested_disk_bytes"], 25769803776)
        self.assertEqual((f["rc"], f["kind"], f["priority"]), (75, "tart-macos-vm", 50))

    def test_multiple_axes_and_non_capacity_denials(self) -> None:
        both = dict(DISK_DENIAL, reason="capacity_exceeded",
                    exceeded_axis={"cores": True, "memory": False, "disk": True})
        self._run(json.dumps(both))
        self._run(json.dumps({"ok": False, "reason": "legacy_vm_disk_accounting_unknown"}))
        self._run("not json")
        rows = events(self.log)
        self.assertEqual([r["fields"]["axis"] for r in rows], ["cores+disk", "none", "none"])
        self.assertEqual(rows[1]["fields"]["reason"], "legacy_vm_disk_accounting_unknown")
        self.assertEqual(rows[2]["fields"]["reason"], "unreadable")

    def test_a_provider_without_event_is_untouched(self) -> None:
        proc = self._run(json.dumps(DISK_DENIAL), with_event=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(events(self.log), [])

    def test_the_denial_branch_emits_it(self) -> None:
        acquire = shell_function(VM_LEASE_LIB, "tartci_acquire_vm_lease")
        denial = acquire[acquire.index('tartci_vm_lease_note "lease denied'):]
        denial = denial[:denial.index("return")]
        self.assertIn("tartci_vm_lease_denied_event", denial)


class AdmissionContentionEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log = Path(self._tmp.name) / "events.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, envelope: dict | str) -> None:
        text = envelope if isinstance(envelope, str) else json.dumps(envelope)
        proc = run_bash(
            shell_function(ADMISSION_LIB, "tartci_admission_contention_event")
            + shell_function(RUNNER, "json_sanitize") + shell_function(RUNNER, "event")
            + f"EVENT_LOG={str(self.log)!r}\nRUNNER_NAME=lane\nCURRENT_VM=\n"
            + f"tartci_admission_contention_event {shlex.quote(text)} boundary\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_waited_verdict_is_recorded(self) -> None:
        self._run({"verdict": "admit", "reason": "clean", "tartci_contention_waits": 4,
                   "tartci_contention_wait_secs": 21.5})
        (row,) = events(self.log)
        self.assertEqual(row["event"], "admission_contention_waited")
        self.assertEqual(row["fields"], {"waits": 4, "secs": 21.5, "verdict": "admit",
                                         "reason": "clean", "stage": "boundary"})

    def test_no_wait_no_event(self) -> None:
        self._run({"verdict": "admit", "reason": "clean"})
        self._run({"verdict": "defer", "tartci_contention_waits": 0})
        self._run("garbage")
        self.assertEqual(events(self.log), [])

    def test_both_admission_sites_record_it(self) -> None:
        body = RUNNER.read_text()
        self.assertIn('tartci_admission_contention_event "$precheck_json" precheck', body)
        self.assertIn('tartci_admission_contention_event "$admission_json" boundary', body)


class PreMintBlockerTests(RunnerFixture, unittest.TestCase):
    def _denied(self, tier: str) -> dict:
        result = self._runner("--print-pre-mint-selection", tier)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0", result.stderr)
        line = result.stderr.strip().splitlines()[-1]
        return dict(token.split("=", 1) for token in line.split())

    def test_higher_class_demand_names_that_class(self) -> None:
        self._state(merge=True, pr=True)
        blocker = self._denied("1")
        self.assertEqual(blocker["blocker_reason"], "higher_class_demand")
        self.assertEqual(blocker["blocker_class"], "pulp-build-merge-group")
        self.assertEqual(blocker["blocker_tier"], "0")
        self.assertEqual(blocker["blocker_queued"], "1")

    def test_own_class_gone_names_the_selected_class(self) -> None:
        self._state()
        blocker = self._denied("1")
        self.assertEqual(blocker["blocker_reason"], "own_class_empty")
        self.assertEqual(blocker["blocker_class"], "pulp-build-pr-head")

    def test_admitted_prints_no_blocker(self) -> None:
        self._state(pr=True)
        result = self._runner("--print-pre-mint-selection", "1")
        self.assertEqual(result.stdout.strip(), "1", result.stderr)
        self.assertNotIn("blocker_", result.stderr)

    def test_the_denied_event_carries_the_blocker(self) -> None:
        self.assertIn('tartci_assignment_v2_pre_mint_denied_event "$selected_tier" "$selected_labels"',
                      RUNNER.read_text())
        log = self.root / "events.jsonl"
        proc = run_bash(
            shell_function(ASSIGNMENT_LIB, "tartci_assignment_v2_pre_mint_denied_event")
            + shell_function(RUNNER, "json_sanitize") + shell_function(RUNNER, "event")
            + f"EVENT_LOG={str(log)!r}\nRUNNER_NAME=lane\nCURRENT_VM=vm\n"
            "ASSIGNMENT_V2_PRE_MINT_BLOCKER='blocker_class=pulp-build-merge-group "
            "blocker_tier=0 blocker_reason=higher_class_demand blocker_queued=2'\n"
            "tartci_assignment_v2_pre_mint_denied_event 1 a,b\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (row,) = events(log)
        self.assertEqual(row["event"], "assignment_v2_pre_mint_denied")
        self.assertEqual(row["fields"], {
            "selected_tier": 1, "blocker_class": "pulp-build-merge-group",
            "blocker_tier": 0, "blocker_reason": "higher_class_demand", "blocker_queued": 2})


if __name__ == "__main__":
    unittest.main()
