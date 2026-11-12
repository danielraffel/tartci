#!/usr/bin/env python3
"""The opt-in pre-clone demand check decides whether a V2 lane clones at all.

A booted VM whose selected class emptied while it was being selected, admitted
and cloned is discarded at the pre-mint check, unused. The pre-clone check asks
the same question before the clone. These tests drive the REAL `run_one` body
(sliced from runner.sh) and the REAL pre-clone functions (sliced from
assignment-v2.lib.sh) with a scripted pre-mint verdict, and observe the clone
through the `clone_start` event, which the harness turns into a distinct exit.

The end-to-end decision against a live scan (fake GitHub, real scanner) is in
test_assignment_v2.py, PreCloneProbeTests.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers/tart-macos/runner.sh"
V2_LIB = ROOT / "providers/tart-macos/assignment-v2.lib.sh"

CLONE_REACHED_EXIT = 17
LABELS = "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm,pulp-build-pr-head"


def function_body(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\{{\n(.*?)^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group(1)


class Harness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.events = tmp / "events.tsv"
        self.calls = tmp / "calls.log"
        self.state = tmp / "state"
        self.state.mkdir()

    def run(self, *, knob: str, verdict: str, mode: str = "event-class-v2",
            receipt_age: str = "180") -> subprocess.CompletedProcess[str]:
        """verdict: `admit` or a blocker reason the stub pre-mint check reports."""
        runner = RUNNER.read_text(encoding="utf-8")
        lib = V2_LIB.read_text(encoding="utf-8")
        pieces = []
        for name in ("tartci_assignment_v2_pre_clone_check_enabled",
                     "tartci_assignment_v2_pre_clone_skip"):
            pieces.append(f"{name}(){{\n{function_body(lib, name)}}}\n")
        script = (
            "set -euo pipefail\n"
            + "".join(pieces)
            # The pre-mint verdict is scripted; the receipt knob it sees is
            # recorded so the test can prove the shortcut was disabled.
            + "tartci_assignment_v2_pre_mint_valid(){\n"
            f"  printf 'pre_mint tier=%s receipt=%s\\n' \"$1\" \"${{TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS:-unset}}\" >>{str(self.calls)!r}\n"
            f"  case {verdict!r} in\n"
            "    admit) return 0 ;;\n"
            f"    *) ASSIGNMENT_V2_PRE_MINT_BLOCKER=\"blocker_class=pulp-build-merge-group blocker_tier=0 blocker_reason={verdict} blocker_queued=0\"; return 1 ;;\n"
            "  esac\n"
            "}\n"
            f"tartci_assignment_v2_invalidate_selection(){{ printf 'invalidate\\n' >>{str(self.calls)!r}; }}\n"
            "ephemeral_boot_name(){ printf 'lane-vm-%s' \"$1\"; }\n"
            "now_epoch(){ printf '0'; }\n"
            "runner_group_id_for_tier(){ printf '1'; }\n"
            "runner_api_root_for_group(){ printf 'repos/o/r'; }\n"
            "jit_admission_denied(){ return 1; }\n"
            "tartci_pool_lock_absent(){ return 0; }\n"
            "tartci_lifecycle_reset(){ :; }\n"
            "tartci_storage_health_check(){ STORAGE_HEALTH_STATE=ok; return 0; }\n"
            "tartci_job_claim_acquire(){ return 0; }\n"
            "tartci_vm_dhcp_check(){ return 0; }\n"
            "tartci_vm_dhcp_record(){ :; }\n"
            "tartci_vm_lease_waiter_register(){ :; }\n"
            "tartci_vm_lease_cores(){ printf '4'; }\n"
            "tartci_vm_lease_mem_mb(){ printf '8192'; }\n"
            "tartci_vm_lease_priority(){ printf '100'; }\n"
            "tartci_admission_clean_enabled(){ return 1; }\n"
            "tartci_check_macos_disk_floor_with_cleanup_once(){ return 0; }\n"
            "tartci_prepare_and_check_disk_root_observed(){ return 0; }\n"
            "reclaim_runner_name(){ :; }\n"
            "sweep_lane_ghost_runners(){ :; }\n"
            "note(){ :; }\n"
            "heartbeat(){ :; }\n"
            "event(){\n"
            f"  printf '%s\\t%s\\n' \"$1\" \"${{2:-}}\" >>{str(self.events)!r}\n"
            "  return 0\n"
            "}\n"
            "boot_vm_to_ssh(){\n"
            f"  printf 'clone_start\\t\\n' >>{str(self.events)!r}\n"
            f"  exit {CLONE_REACHED_EXIT}\n"
            "}\n"
            f"ASSIGNMENT_MODE={mode!r}\n"
            f"TARTCI_ASSIGNMENT_V2_PRE_CLONE_CHECK={knob!r}\n"
            f"TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS={receipt_age!r}\n"
            "RUNNER_NAME='lane-01'; SLOT=1; REPO='o/r'\n"
            f"STATE_DIR={str(self.state)!r}\n"
            f"TART_HOME={str(self.tmp / 'vms')!r}; CACHE_ROOT={str(self.tmp / 'cache')!r}\n"
            "CURRENT_VM=''; CURRENT_IP=''; WARM_VM=''; SERVING_BLOCKED_SINCE=''\n"
            "CURRENT_RUNNER_API_ROOT=''; CURRENT_LABELS=''\n"
            f"run_one(){{\n{function_body(runner, 'run_one')}}}\n"
            "rc=0\n"
            f"run_one 1 {LABELS!r} 1 || rc=$?\n"
            "printf 'rc=%s gone=%s receipt_after=%s\\n' \"$rc\" \"$PRE_CLONE_DEMAND_GONE\" "
            "\"$TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS\"\n"
        )
        path = self.tmp / "harness.sh"
        path.write_text(script, encoding="utf-8")
        return subprocess.run(["/bin/bash", str(path)], text=True, capture_output=True,
                              check=False, timeout=60, env=dict(os.environ))

    def names(self) -> list[str]:
        if not self.events.exists():
            return []
        return [line.split("\t", 1)[0] for line in self.events.read_text().splitlines()]

    def call_log(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""


class PreCloneDemandCheckTests(unittest.TestCase):
    def _run(self, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        harness = Harness(Path(tmp.name))
        return harness, harness.run(**kwargs)

    def test_an_emptied_class_is_not_cloned(self) -> None:
        harness, result = self._run(knob="1", verdict="own_class_empty")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rc=75 gone=1", result.stdout)
        self.assertNotIn("clone_start", harness.names(),
                         "a class proven empty before the clone still paid for a VM")
        self.assertIn("assignment_v2_pre_clone_denied", harness.names())
        self.assertIn("invalidate", harness.call_log(),
                      "a denial must drop the cached selection so the next pass re-selects")

    def test_a_waiting_preferred_class_is_not_cloned(self) -> None:
        harness, result = self._run(knob="1", verdict="higher_class_demand")
        self.assertIn("rc=75 gone=1", result.stdout, result.stderr)
        self.assertNotIn("clone_start", harness.names())
        self.assertIn("assignment_v2_pre_clone_denied", harness.names())

    def test_live_demand_clones(self) -> None:
        """Control: without it, a check that refused everything would pass both
        tests above while starving the lane."""
        harness, result = self._run(knob="1", verdict="admit")
        self.assertEqual(result.returncode, CLONE_REACHED_EXIT, result.stderr)
        self.assertEqual(harness.names(), ["clone_start"])

    def test_an_uncertain_scan_fails_open_and_clones(self) -> None:
        for reason in ("demand_uncertain", "unknown_tier", "selected_class_not_ordered"):
            with self.subTest(reason=reason):
                harness, result = self._run(knob="1", verdict=reason)
                self.assertEqual(result.returncode, CLONE_REACHED_EXIT, result.stderr)
                self.assertIn("assignment_v2_pre_clone_uncertain", harness.names())
                self.assertNotIn("invalidate", harness.call_log())

    def test_the_knob_off_never_asks_and_clones(self) -> None:
        harness, result = self._run(knob="0", verdict="own_class_empty")
        self.assertEqual(result.returncode, CLONE_REACHED_EXIT, result.stderr)
        self.assertEqual(harness.call_log(), "", "the check ran with its knob off")

    def test_a_non_v2_lane_never_asks(self) -> None:
        harness, result = self._run(knob="1", verdict="own_class_empty", mode="legacy")
        self.assertEqual(result.returncode, CLONE_REACHED_EXIT, result.stderr)
        self.assertEqual(harness.call_log(), "")

    def test_the_top_tier_receipt_is_disabled_for_this_call_only(self) -> None:
        """The receipt IS the selection being re-checked; honouring it would
        make the check vacuous on the top tier."""
        harness, result = self._run(knob="1", verdict="admit", receipt_age="180")
        self.assertIn("receipt=0", harness.call_log())
        self.assertNotIn("receipt=180", harness.call_log())
        # The lane's own value survives for the pre-mint check later.
        self.assertEqual(result.returncode, CLONE_REACHED_EXIT, result.stderr)
        _h, denied = self._run(knob="1", verdict="own_class_empty", receipt_age="180")
        self.assertIn("receipt_after=180", denied.stdout, denied.stderr)


class PreCloneWiringTests(unittest.TestCase):
    """Ordering the behavioral harness above cannot see."""

    def setUp(self) -> None:
        self.source = RUNNER.read_text(encoding="utf-8")
        self.body = function_body(self.source, "run_one")

    def test_the_check_follows_admission_and_precedes_the_clone(self) -> None:
        precheck = self.body.index('precheck_json="$(tartci_admission_clean')
        check = self.body.index("tartci_assignment_v2_pre_clone_skip")
        clone = self.body.index('boot_vm_to_ssh "$i"')
        mint = self.body.index("tartci_assignment_v2_pre_mint_admit")
        self.assertLess(precheck, check, "the check must see demand after the slow precheck")
        self.assertLess(check, clone)
        self.assertLess(clone, mint, "the authoritative pre-mint check must stay after boot")

    def test_a_skip_is_an_idle_pass_not_a_blocked_one(self) -> None:
        self.assertIn('|| [ "${PRE_CLONE_DEMAND_GONE:-0}" = 1 ]; then', self.source)

    def test_the_lane_lease_is_still_taken_before_the_clone(self) -> None:
        boot = function_body(self.source, "boot_vm_to_ssh")
        self.assertLess(boot.index("tartci_acquire_vm_lease"), boot.index("event clone_start"))


if __name__ == "__main__":
    unittest.main()
