#!/usr/bin/env python3
"""Behavioral tests for VM provider lease helper wiring."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "providers" / "common" / "vm-lease.lib.sh"
STATE_HELPER = ROOT / "providers" / "common" / "vm-state.lib.sh"
MACOS_RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
LINUX_RUNNER = ROOT / "providers" / "tart-linux" / "runner.sh"
WINDOWS_RUNNER = ROOT / "providers" / "qemu-windows" / "runner.sh"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


class VmLeaseHelperTests(unittest.TestCase):
    def test_apply_false_cannot_enter_cleanup_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"scripts").mkdir(); marker=root/"called"
            _write_exec(root/"scripts/worktree_cleanup.py",f"#!/usr/bin/env python3\nimport pathlib\npathlib.Path({str(marker)!r}).touch()\n")
            result=_run_bash(f'''source {HELPER}; export TARTCI_ROOT={root}
              export TARTCI_WORKTREE_CLEANUP_APPLY=0 TARTCI_WORKTREE_CLEANUP_PROVIDER=merged-main-v1 TARTCI_WORKTREE_CLEANUP_REPO=Generous-Corp/pulp
              export TARTCI_RUNNER_REPO=Generous-Corp/pulp TARTCI_RECEIPT_HOST_ID=studio
              tartci_try_worktree_cleanup '{{"ok":false}}'; test $? -eq 1''')
            self.assertEqual(result.returncode,0,result.stderr); self.assertFalse(marker.exists())

    def test_worktree_cleanup_trigger_rejects_non_disk_and_malformed_denials(self) -> None:
        script = f'''
          source {HELPER}
          export TARTCI_WORKTREE_CLEANUP_PROVIDER=merged-main-v1
          export TARTCI_WORKTREE_CLEANUP_REPO=Generous-Corp/pulp
          export TARTCI_WORKTREE_CLEANUP_APPLY=1
          export TARTCI_RUNNER_REPO=Generous-Corp/pulp
          export TARTCI_RECEIPT_HOST_ID=studio
          for value in 'not-json' '{{"ok":false,"reason":"cpu_capacity_exceeded","exceeded_axis":{{"cores":true,"memory":false,"disk":false}}}}' '{{"ok":false,"reason":"disk_probe_failed"}}'; do
            if tartci_try_worktree_cleanup "$value"; then exit 9; fi
          done
        '''
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_worktree_cleanup_trigger_accepts_only_exact_fresh_disk_axis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scripts").mkdir()
            called = root / "called"
            _write_exec(root / "scripts/worktree_cleanup.py", f"#!/usr/bin/env python3\nimport pathlib,sys\npathlib.Path({str(called)!r}).write_text(' '.join(sys.argv[1:]))\n")
            receipt_dir = root / "receipts"
            attempt = '{"ok":false,"reason":"disk_capacity_exceeded","exceeded_axis":{"cores":false,"memory":false,"disk":true},"disk":{"free_bytes":10,"available_after_reservations_bytes":9,"required_bytes":20}}'
            script = f'''
              source {HELPER}
              export TARTCI_ROOT={root}
              export TARTCI_DISK_DENIAL_RECEIPT_DIR={receipt_dir}
              export TARTCI_WORKTREE_CLEANUP_PROVIDER=merged-main-v1
              export TARTCI_WORKTREE_CLEANUP_REPO=Generous-Corp/pulp
              export TARTCI_WORKTREE_CLEANUP_PRIMARY=/Volumes/Workshop/Code/pulp
              export TARTCI_WORKTREE_CLEANUP_PREFIX=/Volumes/Workshop/Code
              export TARTCI_WORKTREE_CLEANUP_MAIN_REF=origin/main
              export TARTCI_WORKTREE_CLEANUP_APPLY=1
              export TARTCI_RUNNER_REPO=Generous-Corp/pulp
              export TARTCI_RECEIPT_HOST_ID=studio
              export TARTCI_QUEUE_LANE_ID=studio-pulp-gate
              mkdir -p "$TARTCI_DISK_DENIAL_RECEIPT_DIR"
              tartci_try_worktree_cleanup '{attempt}'
            '''
            result = _run_bash(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--before-free-bytes 10 --required-bytes 20", called.read_text())

    def test_worktree_cleanup_trigger_rejects_wrong_or_missing_lane_identity(self) -> None:
        attempt = '{"ok":false,"reason":"disk_capacity_exceeded","exceeded_axis":{"cores":false,"memory":false,"disk":true},"disk":{"free_bytes":10,"available_after_reservations_bytes":9,"required_bytes":20}}'
        for runner_repo, host_id, queue_lane in (("", "studio", "studio-pulp-gate"), ("Generous-Corp/forge", "studio", "studio-pulp-gate"), ("Generous-Corp/pulp", "", "studio-pulp-gate"), ("Generous-Corp/pulp", "m5", "studio-pulp-gate"), ("Generous-Corp/pulp", "studio", "studio-forge-gate"), ("Generous-Corp/pulp", "studio", "")):
            with self.subTest(runner_repo=runner_repo, host_id=host_id, queue_lane=queue_lane), tempfile.TemporaryDirectory() as td:
                root = Path(td); (root / "scripts").mkdir(); called = root / "called"
                _write_exec(root / "scripts/worktree_cleanup.py", f"#!/usr/bin/env python3\nimport pathlib\npathlib.Path({str(called)!r}).touch()\n")
                result = _run_bash(f'''
                  source {HELPER}; export TARTCI_ROOT={root}; export TARTCI_DISK_DENIAL_RECEIPT_DIR={root / "receipts"}
                  export TARTCI_WORKTREE_CLEANUP_PROVIDER=merged-main-v1 TARTCI_WORKTREE_CLEANUP_REPO=Generous-Corp/pulp
                  export TARTCI_WORKTREE_CLEANUP_APPLY=1
                  export TARTCI_RUNNER_REPO={runner_repo!r} TARTCI_RECEIPT_HOST_ID={host_id!r} TARTCI_QUEUE_LANE_ID={queue_lane!r}
                  tartci_try_worktree_cleanup '{attempt}'; test $? -eq 1
                ''')
                self.assertEqual(result.returncode, 0, result.stderr); self.assertFalse(called.exists())

    def test_successful_cleanup_retries_lease_exactly_once_but_nonzero_does_not(self) -> None:
        denial = '{"ok":false,"reason":"disk_capacity_exceeded","exceeded_axis":{"cores":false,"memory":false,"disk":true},"disk":{"free_bytes":10,"available_after_reservations_bytes":9,"required_bytes":20}}'
        for cleanup_rc, expected_calls in ((0, 2), (5, 1)):
            with self.subTest(cleanup_rc=cleanup_rc), tempfile.TemporaryDirectory() as td:
                root = Path(td); (root / "scripts").mkdir(); count = root / "count"
                _write_exec(root / "scripts/leases.py", f'''#!/usr/bin/env python3
import pathlib,sys
p=pathlib.Path({str(count)!r}); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n))
if n==1: print({denial!r}); raise SystemExit(75)
print('{{"ok":true,"reason":"acquired"}}')
''')
                _write_exec(root / "scripts/worktree_cleanup.py", f"#!/usr/bin/env python3\nraise SystemExit({cleanup_rc})\n")
                result = _run_bash(f'''
                  source {HELPER}; tartci_start_vm_lease_heartbeat() {{ :; }}
                  export TARTCI_ROOT={root} TARTCI_DISK_DENIAL_RECEIPT_DIR={root / "receipts"}
                  export TARTCI_WORKTREE_CLEANUP_PROVIDER=merged-main-v1 TARTCI_WORKTREE_CLEANUP_REPO=Generous-Corp/pulp
                  export TARTCI_WORKTREE_CLEANUP_APPLY=1
                  export TARTCI_WORKTREE_CLEANUP_PRIMARY=/Volumes/Workshop/Code/pulp TARTCI_WORKTREE_CLEANUP_PREFIX=/Volumes/Workshop/Code TARTCI_WORKTREE_CLEANUP_MAIN_REF=origin/main
                  export TARTCI_RUNNER_REPO=Generous-Corp/pulp TARTCI_RECEIPT_HOST_ID=studio
                  export TARTCI_QUEUE_LANE_ID=studio-pulp-gate
                  mkdir -p "$TARTCI_DISK_DENIAL_RECEIPT_DIR"
                  tartci_acquire_vm_lease unit 1 test-vm gate labels '' '' provider lane runner
                  rc=$?; test "$(cat {count})" -eq {expected_calls}
                  test "$rc" -eq {0 if cleanup_rc == 0 else 75}
                ''')
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_macos_preflight_cleanup_has_one_exact_retry_and_fail_closed_outcomes(self) -> None:
        for cleanup_rc, retry_rc, expected_calls, expected_rc in ((0, 0, 2, 0), (5, 0, 1, 75), (0, 75, 2, 75)):
            with self.subTest(cleanup_rc=cleanup_rc, retry_rc=retry_rc):
                result = _run_bash(f'''
                  source {HELPER}; calls=0
                  tartci_check_disk_floor_observed() {{
                    calls=$((calls+1)); TARTCI_LAST_DISK_ADMISSION_ATTEMPT_JSON='{{"fresh":true}}'
                    [ "$calls" -eq 1 ] && return 75
                    return {retry_rc}
                  }}
                  tartci_try_worktree_cleanup() {{ return {cleanup_rc}; }}
                  tartci_check_macos_disk_floor_with_cleanup_once /Volumes/Workshop/VMs lane runner
                  rc=$?; test "$calls" -eq {expected_calls}; test "$rc" -eq {expected_rc}
                ''')
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_macos_native_entry_uses_cleanup_wrapper_only_for_tart_store_floor(self) -> None:
        body=MACOS_RUNNER.read_text()
        self.assertEqual(body.count('tartci_check_macos_disk_floor_with_cleanup_once "$TART_HOME"'),1)
        self.assertNotIn('tartci_check_macos_disk_floor_with_cleanup_once "$CACHE_ROOT"',body)
        self.assertNotIn('tartci_check_macos_disk_floor_with_cleanup_once "$logdir"',body)

    def test_observed_preflight_floor_denial_emits_authoritative_frame(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            receipts = tmp / "receipts"
            script = textwrap.dedent(
                f"""
                set -u
                export TARTCI_ROOT={ROOT}
                export TARTCI_VM_DISK_FREE_FLOOR_GB=999999
                export TARTCI_DISK_DENIAL_RECEIPT_DIR={receipts}
                export TARTCI_RECEIPT_HOST_ID=studio
                note() {{ :; }}
                source {HELPER}
                source {STATE_HELPER}
                tartci_check_disk_floor_observed {tmp} tart-macos studio-pulp-gate runner
                rc=$?
                python3 - "$rc" {receipts / "runner.disk-admission.json"} <<'PY'
import json, sys
d = json.load(open(sys.argv[2]))
assert d["free_bytes"] < d["required_bytes"]
assert d["available_after_reservations_bytes"] < d["required_after_reservations_bytes"]
print(f'rc={{sys.argv[1]}} reason={{d["reason"]}}')
PY
                exit 0
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "rc=75 reason=disk_capacity_insufficient")

    def test_observed_preflight_missing_root_is_probe_denial(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            missing = tmp / "missing"
            receipts = tmp / "receipts"
            script = textwrap.dedent(
                f"""
                set -u
                export TARTCI_ROOT={ROOT}
                export TARTCI_DISK_DENIAL_RECEIPT_DIR={receipts}
                export TARTCI_RECEIPT_HOST_ID=studio
                note() {{ :; }}
                source {HELPER}
                source {STATE_HELPER}
                tartci_check_disk_floor_observed {missing} tart-macos lane runner
                rc=$?
                python3 - "$rc" {receipts / "runner.disk-admission.json"} <<'PY'
import json, sys
d = json.load(open(sys.argv[2]))
print(f'rc={{sys.argv[1]}} status={{d["status"]}} reason={{d["reason"]}}')
PY
                exit 0
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "rc=75 status=denied reason=disk_probe_failed")

    def test_receipt_observer_failure_preserves_disk_denial_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            not_directory = tmp / "not-a-directory"
            not_directory.write_text("do-not-replace\n", encoding="utf-8")
            script = textwrap.dedent(
                f"""
                set -u
                export TARTCI_ROOT={ROOT}
                export TARTCI_LEASE_DIR={tmp / "leases"}
                export TARTCI_HOST_CORES=8
                export TARTCI_HOST_MEM_MB=65536
                export TARTCI_ROLE=light
                export TARTCI_VM_DISK_GROWTH_GB=0
                export TARTCI_VM_DISK_FREE_FLOOR_GB=999999
                export TARTCI_DISK_DENIAL_RECEIPT_DIR={not_directory}
                export TARTCI_RECEIPT_HOST_ID=studio
                note() {{ :; }}
                source {HELPER}
                tartci_acquire_vm_lease unit-vm 1 tart-macos-vm gate labels 1024 {tmp} tart-macos lane runner
                rc=$?
                printf 'rc=%s content=%s\n' "$rc" "$(< {not_directory})"
                exit 0
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "rc=75 content=do-not-replace")

    def test_misconfigured_floor_writes_typed_receipt_and_preserves_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            receipts = tmp / "receipts"
            script = textwrap.dedent(
                f"""
                set -u
                export TARTCI_ROOT={ROOT}
                export TARTCI_VM_DISK_FREE_FLOOR_GB=invalid
                export TARTCI_DISK_DENIAL_RECEIPT_DIR={receipts}
                export TARTCI_RECEIPT_HOST_ID=studio
                note() {{ :; }}
                source {HELPER}
                tartci_acquire_vm_lease unit-vm 1 tart-macos-vm gate labels 1024 {tmp} tart-macos lane runner
                rc=$?
                receipt_state="$(python3 -c 'import json; d=json.load(open("{receipts / "runner.disk-admission.json"}")); print("status=%s reason=%s" % (d["status"], d["reason"]))')"
                printf 'rc=%s %s\n' "$rc" "$receipt_state"
                exit 0
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "rc=75 status=denied reason=disk_floor_misconfigured")

    def test_acquire_and_release_records_vm_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                TARTCI_ROOT={ROOT}
                export TARTCI_ROOT
                export TARTCI_LEASE_DIR={Path(td) / "leases"}
                export TARTCI_HOST_CORES=8
                export TARTCI_HOST_MEM_MB=65536
                export TARTCI_ROLE=light
                export TARTCI_VM_LEASE_HEARTBEAT_SECS=1
                export TARTCI_VM_DISK_GROWTH_GB=0
                export TARTCI_VM_DISK_FREE_FLOOR_GB=0
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                tartci_acquire_vm_lease unit-vm 2 tart-linux-vm vm self-hosted,Linux 8192 {Path(td)}
                python3 "$TARTCI_ROOT/scripts/leases.py" status --store-dir "$TARTCI_LEASE_DIR" --json |
                  python3 -c 'import json,sys; s=json.load(sys.stdin); r=s["leases"][0]; print(r["id"], r["lease_size_cores"], r["command_kind"], r["vm_name"], r["label"])'
                tartci_release_vm_lease
                python3 "$TARTCI_ROOT/scripts/leases.py" status --store-dir "$TARTCI_LEASE_DIR" --json |
                  python3 -c 'import json,sys; print(len(json.load(sys.stdin)["leases"]))'
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines(), ["vm-tart-linux-vm-unit-vm 2 tart-linux-vm unit-vm self-hosted,Linux", "0"])

    def test_disabled_leases_do_not_touch_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                TARTCI_ROOT={ROOT}
                export TARTCI_ROOT
                export TARTCI_LEASE_DIR={Path(td) / "leases"}
                export TARTCI_VM_LEASES=0
                note() {{ :; }}
                source {HELPER}
                tartci_acquire_vm_lease unit-vm 2 tart-linux-vm vm labels
                python3 "$TARTCI_ROOT/scripts/leases.py" status --store-dir "$TARTCI_LEASE_DIR" --json |
                  python3 -c 'import json,sys; print(len(json.load(sys.stdin)["leases"]))'
                test -z "${{TARTCI_ACTIVE_VM_LEASE_ID:-}}"
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0")

    def test_disabled_leases_run_finite_and_exec_guarded_commands_directly(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            export TARTCI_ROOT={ROOT}
            export TARTCI_VM_LEASES=0
            source {HELPER}
            tartci_acquire_vm_lease break-glass 2 tart-linux-vm vm labels
            tartci_vm_lease_guard_run /usr/bin/printf 'run-ok\\n'
            tartci_vm_lease_guard_exec /usr/bin/printf 'exec-ok\\n'
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines(), ["run-ok", "exec-ok"])

    def test_disabled_environment_without_admission_authority_fails_closed(self) -> None:
        for helper in ("tartci_vm_lease_guard_run", "tartci_vm_lease_guard_exec"):
            with self.subTest(helper=helper):
                script = textwrap.dedent(
                    f"""
                    set -euo pipefail
                    export TARTCI_ROOT={ROOT}
                    export TARTCI_VM_LEASES=0
                    export TARTCI_VM_LEASE_BYPASS_AUTHORIZED=1
                    export _tartci_vm_lease_bypass_state=authorized
                    source {HELPER}
                    if {helper} /usr/bin/true; then
                      exit 99
                    else
                      test "$?" -eq 75
                    fi
                    """
                )
                proc = _run_bash(script)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_mode_change_cannot_forget_or_bypass_an_active_lease(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export TARTCI_ROOT={ROOT}
                export TARTCI_LEASE_DIR={Path(td) / "leases"}
                export TARTCI_HOST_CORES=8
                export TARTCI_HOST_MEM_MB=65536
                export TARTCI_ROLE=light
                export TARTCI_VM_LEASE_HEARTBEAT_SECS=1
                export TARTCI_VM_DISK_GROWTH_GB=0
                export TARTCI_VM_DISK_FREE_FLOOR_GB=0
                export TARTCI_VM_LEASES=1
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                tartci_acquire_vm_lease governed 1 tart-linux-vm vm labels 1024 {Path(td)}
                original="$TARTCI_ACTIVE_VM_LEASE_ID"
                TARTCI_VM_LEASES=0
                if tartci_acquire_vm_lease bypass 1 tart-linux-vm vm labels 1024 {Path(td)}; then
                  exit 99
                else
                  test "$?" -eq 75
                fi
                test "$TARTCI_ACTIVE_VM_LEASE_ID" = "$original"
                tartci_release_vm_lease
                python3 "$TARTCI_ROOT/scripts/leases.py" status --store-dir "$TARTCI_LEASE_DIR" --json |
                  python3 -c 'import json,sys; print(len(json.load(sys.stdin)["leases"]))'
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0")

    def test_break_glass_authority_is_revoked_by_release_or_mode_change(self) -> None:
        for revoke in (
            "tartci_release_vm_lease",
            "TARTCI_VM_LEASES=1",
        ):
            with self.subTest(revoke=revoke):
                script = textwrap.dedent(
                    f"""
                    set -euo pipefail
                    export TARTCI_ROOT={ROOT}
                    export TARTCI_VM_LEASES=0
                    source {HELPER}
                    tartci_acquire_vm_lease break-glass 2 tart-linux-vm vm labels
                    {revoke}
                    if tartci_vm_lease_guard_run /usr/bin/true; then
                      exit 99
                    else
                      test "$?" -eq 75
                    fi
                    """
                )
                proc = _run_bash(script)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_enabled_guard_helpers_fail_closed_without_an_active_lease(self) -> None:
        for helper in ("tartci_vm_lease_guard_run", "tartci_vm_lease_guard_exec"):
            with self.subTest(helper=helper):
                script = textwrap.dedent(
                    f"""
                    set -euo pipefail
                    export TARTCI_ROOT={ROOT}
                    export TARTCI_VM_LEASES=1
                    source {HELPER}
                    if {helper} /usr/bin/true; then
                      exit 99
                    else
                      test "$?" -eq 75
                    fi
                    """
                )
                proc = _run_bash(script)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_shared_disk_parser_preserves_disable_spellings_and_rejects_garbage(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source {HELPER}
            for value in 0 false FALSE off OFF no NO; do
              printf '%s=%s\n' "$value" "$(tartci_disk_gb_or_zero TEST_SIZE "$value" 24)"
            done
            printf 'number=%s\n' "$(tartci_disk_gb_or_zero TEST_SIZE 7 24)"
            if tartci_disk_gb_or_zero TEST_SIZE malformed 24 >/dev/null; then
              exit 99
            else
              printf 'invalid_rc=%s\n' "$?"
            fi
            printf 'defaults=%s/%s\n' "$TARTCI_VM_DISK_GROWTH_GB" "$TARTCI_VM_DISK_FREE_FLOOR_GB"
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip().splitlines(),
            [
                "0=0",
                "false=0",
                "FALSE=0",
                "off=0",
                "OFF=0",
                "no=0",
                "NO=0",
                "number=7",
                "invalid_rc=75",
                "defaults=24/25",
            ],
        )

    def test_disable_spellings_flow_through_vm_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export TARTCI_ROOT={ROOT}
                export TARTCI_LEASE_DIR={Path(td) / "leases"}
                export TARTCI_HOST_CORES=8
                export TARTCI_HOST_MEM_MB=65536
                export TARTCI_ROLE=light
                export TARTCI_VM_LEASE_HEARTBEAT_SECS=1
                export TARTCI_VM_DISK_GROWTH_GB=no
                export TARTCI_VM_DISK_FREE_FLOOR_GB=off
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                tartci_acquire_vm_lease disabled-disk 1 tart-linux-vm vm labels 1024 {Path(td)}
                python3 "$TARTCI_ROOT/scripts/leases.py" status --store-dir "$TARTCI_LEASE_DIR" --json |
                  python3 -c 'import json,sys; r=json.load(sys.stdin)["leases"][0]; print(r["disk_growth_bytes"], r["disk_floor_bytes"])'
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0 0")

    def test_configured_storage_root_is_never_created_by_floor_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "offline-volume" / "vm-store"
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export TARTCI_ROOT={ROOT}
                # Disabling the numeric floor is not permission to create a
                # missing configured store on the fallback filesystem.
                export TARTCI_VM_DISK_FREE_FLOOR_GB=off
                note() {{ :; }}
                source {STATE_HELPER}
                if tartci_check_disk_floor {missing}; then
                  exit 99
                else
                  rc=$?
                fi
                test "$rc" -eq 75
                test ! -e {missing}
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(missing.exists())

    def test_prepare_disk_root_creates_cold_leaf_on_validated_parent_device(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "cold-start" / "logs" / "provider"
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export TARTCI_VM_DISK_FREE_FLOOR_GB=0
                source {STATE_HELPER}
                tartci_prepare_disk_root {missing}
                tartci_check_disk_floor {missing}
                test -d {missing}
                """
            )
            proc = _run_bash(script, env={"HOME": td})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(missing.is_dir())

    def test_prepare_disk_root_covers_all_cold_default_path_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as home_td, tempfile.TemporaryDirectory(
            dir="/tmp"
        ) as tmp_td:
            fake_home = Path(home_td)
            fake_tmp = Path(tmp_td)
            roots = (
                ("macos-cache", fake_home / ".cache" / "pulp-ci"),
                ("macos-logs", fake_home / "VMs" / "logs" / "tartci-macos"),
                ("linux-logs", fake_home / "VMs" / "logs" / "tartci-linux"),
                ("linux-cache", fake_home / ".cache" / "tartci" / "ccache-linux"),
                ("windows-work", fake_tmp / "tartci-win"),
                ("windows-logs", fake_tmp / "tartci-win" / "logs"),
            )
            for provider_root, root in roots:
                with self.subTest(provider_root=provider_root, root=root):
                    proc = _run_bash(
                        f"source {STATE_HELPER}; tartci_prepare_disk_root {root}",
                        env={"HOME": str(fake_home), "TMPDIR": str(fake_tmp)},
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertTrue(root.is_dir())

    def test_prepare_disk_root_refuses_missing_tmp_authority(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing_tmp = Path(td) / "reboot-tmp-not-ready"
            target = missing_tmp / "tartci-win"
            proc = _run_bash(
                textwrap.dedent(
                    f"""
                    set -euo pipefail
                    source {STATE_HELPER}
                    if tartci_prepare_disk_root {target}; then
                      exit 99
                    fi
                    test ! -e {missing_tmp}
                    """
                ),
                env={"TMPDIR": str(missing_tmp)},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(missing_tmp.exists())

    def test_prepare_disk_root_refuses_missing_custom_parent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            fake_tmp = Path(td) / "tmp"
            fake_home.mkdir()
            fake_tmp.mkdir()
            missing_parent = Path(td) / "offline-custom-parent"
            target = missing_parent / "provider-leaf"
            proc = _run_bash(
                textwrap.dedent(
                    f"""
                    set -euo pipefail
                    source {STATE_HELPER}
                    if tartci_prepare_disk_root {target}; then
                      exit 99
                    fi
                    test ! -e {missing_parent}
                    """
                ),
                env={"HOME": str(fake_home), "TMPDIR": str(fake_tmp)},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(missing_parent.exists())

    def test_prepare_disk_root_refuses_symlink_escape_beneath_authority(self) -> None:
        with tempfile.TemporaryDirectory() as home_td, tempfile.TemporaryDirectory() as outside_td:
            fake_home = Path(home_td)
            outside = Path(outside_td)
            (fake_home / "cache-link").symlink_to(outside, target_is_directory=True)
            target = fake_home / "cache-link" / "provider"
            proc = _run_bash(
                textwrap.dedent(
                    f"""
                    set -euo pipefail
                    source {STATE_HELPER}
                    if tartci_prepare_disk_root {target}; then
                      exit 99
                    fi
                    """
                ),
                env={"HOME": home_td},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((outside / "provider").exists())

    def test_prepare_disk_root_refuses_missing_external_mount(self) -> None:
        missing = Path("/Volumes") / f"tartci-missing-{os.getpid()}" / "cache"
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source {STATE_HELPER}
            if tartci_prepare_disk_root {missing}; then
              exit 99
            fi
            test ! -e {missing}
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(missing.exists())

    def test_prepare_disk_root_refuses_wrong_declared_device_before_creation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "work"
            actual_device = Path(td).stat().st_dev
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {STATE_HELPER}
                if tartci_prepare_disk_root {missing} '' {actual_device + 1}; then
                  exit 99
                fi
                test ! -e {missing}
                """
            )
            proc = _run_bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(missing.exists())

    def test_prepare_disk_root_refuses_declared_path_that_is_not_a_mount(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_mount = Path(td) / "ordinary-directory"
            fake_mount.mkdir()
            target = fake_mount / "provider"
            proc = _run_bash(
                textwrap.dedent(
                    f"""
                    set -euo pipefail
                    source {STATE_HELPER}
                    if tartci_prepare_disk_root {target} {fake_mount}; then
                      exit 99
                    fi
                    test ! -e {target}
                    """
                )
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(target.exists())

    def test_external_volume_mount_is_inferred_for_identity_pinning(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source {HELPER}
            tartci_vm_lease_disk_expected_mount_path tart-macos /Volumes/Workshop/Code/tart
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "/Volumes/Workshop")

    def test_all_vm_providers_launch_the_writer_through_the_guardian(self) -> None:
        expected = {
            MACOS_RUNNER: (
                "tartci_vm_lease_guard_run tart clone",
                "tartci_vm_lease_guard_exec tart run",
            ),
            LINUX_RUNNER: (
                "tartci_vm_lease_guard_run tart clone",
                "tartci_vm_lease_guard_exec tart run",
            ),
            WINDOWS_RUNNER: (
                "tartci_vm_lease_guard_run qemu-img create",
                "tartci_vm_lease_guard_exec qemu-system-aarch64",
            ),
        }
        for runner, guarded_commands in expected.items():
            with self.subTest(runner=runner):
                body = runner.read_text(encoding="utf-8")
                for guarded_command in guarded_commands:
                    self.assertIn(guarded_command, body)
                self.assertIn("tartci_acquire_vm_lease", body)

    def test_all_vm_providers_prepare_cold_auxiliary_roots(self) -> None:
        expected = {
            MACOS_RUNNER: ('tartci_prepare_and_check_disk_root_observed "$CACHE_ROOT"',),
            LINUX_RUNNER: (
                'tartci_prepare_and_check_disk_root_observed "$LOGROOT"',
                'tartci_prepare_disk_root "$CACHE_ROOT/ccache-linux"',
            ),
            WINDOWS_RUNNER: (
                'tartci_prepare_and_check_disk_root_observed "$WORKROOT"',
                'tartci_prepare_and_check_disk_root_observed "$LOGROOT"',
            ),
        }
        for runner, prepared_roots in expected.items():
            with self.subTest(runner=runner):
                body = runner.read_text(encoding="utf-8")
                for prepared_root in prepared_roots:
                    self.assertIn(prepared_root, body)

    def test_providers_do_not_recreate_validated_host_roots_with_mkdir(self) -> None:
        forbidden = {
            MACOS_RUNNER: (
                'mkdir -p "$MACOS_LOGROOT',
                'mkdir -p "$CACHE_ROOT',
            ),
            LINUX_RUNNER: (
                'mkdir -p "$LOGROOT',
                'mkdir -p "$CACHE_ROOT',
            ),
            WINDOWS_RUNNER: (
                'mkdir -p "$WORKROOT',
                'mkdir -p "$LOGROOT',
                'mkdir -p "$jobdir',
                'mkdir -p "$logdir',
            ),
        }
        for runner, path_writers in forbidden.items():
            body = runner.read_text(encoding="utf-8")
            for path_writer in path_writers:
                with self.subTest(runner=runner, path_writer=path_writer):
                    self.assertNotIn(path_writer, body)

    def test_windows_port_locks_stay_relative_to_open_validated_root(self) -> None:
        body = WINDOWS_RUNNER.read_text(encoding="utf-8")
        allocator = body[body.index("allocate_ssh_port(){") : body.index("PRINT_HOST_HEALTH=0")]
        self.assertNotIn("os.makedirs(root", allocator)
        self.assertNotIn("os.path.realpath(root)", allocator)
        self.assertIn("root_fd = os.open(root, flags)", allocator)
        self.assertIn("os.mkdir(lock_name, dir_fd=root_fd)", allocator)
        self.assertIn("os.rmdir(lock_name, dir_fd=root_fd)", allocator)
        self.assertIn("release_ssh_port_lock", allocator)
        self.assertIn("info.st_ino", allocator)

    def test_provider_core_overrides_and_fallbacks(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            TARTCI_ROOT={ROOT}
            export TARTCI_ROOT
            export TARTCI_MACOS_VM_CORES=12
            export TARTCI_LINUX_VM_CORES=5
            export TARTCI_WIN_VM_CORES=bogus
            source {HELPER}
            printf '%s\\n' "$(tartci_vm_lease_cores tart-macos)"
            printf '%s\\n' "$(tartci_vm_lease_cores tart-linux)"
            printf '%s\\n' "$(tartci_vm_lease_cores qemu-windows 6)"
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines(), ["12", "5", "6"])

    def test_is_non_gate_priority_helper(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source {HELPER}
            for p in gate vm build 100 200 60 0; do
              if tartci_vm_lease_is_non_gate_priority "$p"; then echo "$p nongate"; else echo "$p gate"; fi
            done
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip().splitlines(),
            ["gate gate", "vm nongate", "build nongate", "100 gate", "200 gate", "60 nongate", "0 nongate"],
        )

    def test_tagged_release_gets_gate_lease_but_pr_gate_does_not(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source {HELPER}
            printf 'tagged=%s\n' "$(tartci_vm_lease_priority self-hosted,macOS,ARM64,pulp-build-vm-release,pulp-release-tagged)"
            printf 'pr-gate=%s\n' "$(tartci_vm_lease_priority self-hosted,macOS,ARM64,pulp-build-vm-release,pulp-release-pr-gate)"
            printf 'conflict=%s\n' "$(tartci_vm_lease_priority self-hosted,macOS,ARM64,pulp-release-tagged,pulp-release-pr-gate)"
            printf 'override=%s\n' "$(TARTCI_VM_LEASE_PRIORITY=vm tartci_vm_lease_priority self-hosted,macOS,ARM64,pulp-release-tagged)"
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip().splitlines(),
            ["tagged=gate", "pr-gate=vm", "conflict=vm", "override=vm"],
        )

    def test_merge_group_lease_sorts_above_pr_head(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source {HELPER}
            printf 'merge=%s\n' "$(tartci_vm_lease_priority self-hosted,macOS,ARM64,pulp-build-vm,pulp-build-merge-group)"
            printf 'pr=%s\n' "$(tartci_vm_lease_priority self-hosted,macOS,ARM64,pulp-build-vm,pulp-build-pr-head)"
            printf 'conflict=%s\n' "$(tartci_vm_lease_priority self-hosted,macOS,ARM64,pulp-build-merge-group,pulp-build-pr-head)"
            printf 'explicit=%s\n' "$(TARTCI_VM_LEASE_PRIORITY=vm tartci_vm_lease_priority self-hosted,macOS,ARM64,pulp-build-merge-group)"
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip().splitlines(),
            ["merge=110", "pr=100", "conflict=vm", "explicit=vm"],
        )

    def test_non_gate_lease_clamped_to_budget(self) -> None:
        # A non-gate VM lane requesting more than the non-gate budget is clamped
        # down, so it can never be denied for exceeding it nor touch the gate reserve.
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export TARTCI_ROOT={ROOT}
                export TARTCI_LEASE_DIR={Path(td) / "leases"}
                export TARTCI_HOST_CORES=16
                export TARTCI_HOST_MEM_MB=262144
                export TARTCI_ROLE=dedicated-builder
                export TARTCI_VM_LEASE_HEARTBEAT_SECS=1
                export TARTCI_VM_DISK_GROWTH_GB=0
                export TARTCI_VM_DISK_FREE_FLOOR_GB=0
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                tartci_profile_value() {{ echo 3; }}   # force non-gate budget = 3
                # explicit tiny mem so the core clamp is isolated from the memory axis
                tartci_acquire_vm_lease unit-vm 8 tart-linux-vm vm self-hosted,Linux 1024 {Path(td)}
                python3 "$TARTCI_ROOT/scripts/leases.py" status --store-dir "$TARTCI_LEASE_DIR" --json |
                  python3 -c 'import json,sys; print(json.load(sys.stdin)["leases"][0]["lease_size_cores"])'
                printf 'effective=%s\\n' "$TARTCI_ACTIVE_VM_LEASE_CORES"
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip().splitlines(),
            ["3", "effective=3"],
        )  # 8 clamped to the 3-core budget and exposed to the provider

    def test_gate_lease_not_clamped(self) -> None:
        # The gate lane runs at gate priority and legitimately uses reserved cores;
        # it must NOT be clamped to the non-gate budget.
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export TARTCI_ROOT={ROOT}
                export TARTCI_LEASE_DIR={Path(td) / "leases"}
                export TARTCI_HOST_CORES=16
                export TARTCI_HOST_MEM_MB=262144
                export TARTCI_ROLE=dedicated-builder
                export TARTCI_VM_LEASE_HEARTBEAT_SECS=1
                export TARTCI_VM_DISK_GROWTH_GB=0
                export TARTCI_VM_DISK_FREE_FLOOR_GB=0
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                tartci_profile_value() {{ echo 3; }}   # non-gate budget = 3 (must be ignored for gate)
                tartci_acquire_vm_lease gate-vm 5 tart-macos-vm gate pulp-build 1024 {Path(td)}
                python3 "$TARTCI_ROOT/scripts/leases.py" status --store-dir "$TARTCI_LEASE_DIR" --json |
                  python3 -c 'import json,sys; print(json.load(sys.stdin)["leases"][0]["lease_size_cores"])'
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "5")  # gate lease unclamped

    def test_derived_guest_memory_inverts_the_guest_job_formula(self) -> None:
        """Guest memory is the inverse of Pulp's governed-build.sh bound.

        That script derives jobs = mem_mb * 3 / 4 / per_job and takes
        min(cores, jobs). Sizing at 4/3 * (cores - 1) * per_job therefore makes
        the guest pick cores - 1 jobs: its full vCPU count less the one job of
        slack the guest's own link/LTO peak needs.
        """
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            TARTCI_ROOT={ROOT}
            export TARTCI_ROOT
            source {HELPER}
            tartci_profile_value() {{ echo 1536; }}
            for c in 2 4 7 12; do
              printf '%s\\n' "$(tartci_vm_lease_derived_mem_mb "$c")"
            done
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sizes = [int(line) for line in proc.stdout.strip().splitlines()]
        # 2 and 4 cores fall on the 8192 floor; 7 derives 4/3*6*1536; 12 would
        # derive 22528 and is held at the 16384 ceiling.
        self.assertEqual(sizes, [8192, 8192, 12288, 16384])
        for cores, mem in zip((2, 4, 7, 12), sizes):
            guest_jobs = mem * 3 // 4 // 1536
            self.assertGreaterEqual(guest_jobs, 1)
            # The guest never derives MORE jobs than it has vCPUs, and for any
            # lane the ceiling does not bind it gets cores - 1.
            self.assertLessEqual(min(cores, guest_jobs), cores)
        self.assertEqual(min(7, sizes[2] * 3 // 4 // 1536), 6)

    def test_derived_guest_memory_honours_floor_and_ceiling_overrides(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            TARTCI_ROOT={ROOT}
            export TARTCI_ROOT
            export TARTCI_VM_LEASE_MIN_MEM_MB=4096
            export TARTCI_VM_LEASE_MAX_MEM_MB=10240
            source {HELPER}
            tartci_profile_value() {{ echo 1536; }}
            printf '%s\\n' "$(tartci_vm_lease_derived_mem_mb 2)"
            printf '%s\\n' "$(tartci_vm_lease_derived_mem_mb 24)"
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines(), ["4096", "10240"])

    def test_derived_guest_memory_survives_a_failing_host_profile(self) -> None:
        """A profile hiccup falls back instead of ending the caller.

        Callers run under `set -e`, where a bare assignment from a command
        substitution that exits non-zero ends the enclosing function before any
        fallback can apply. The helper is called DIRECTLY here, not through a
        substitution: a substitution subshell swallows that abort, which would
        make this test unable to fail.
        """
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            TARTCI_ROOT={ROOT}
            export TARTCI_ROOT
            source {HELPER}
            tartci_profile_value() {{ return 3; }}
            tartci_vm_lease_derived_mem_mb 7
            printf '\\nreached-the-end\\n'
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 1536 fallback → 4/3 * 6 * 1536 = 12288, and execution continued.
        self.assertEqual(
            proc.stdout.strip().splitlines(), ["12288", "reached-the-end"]
        )

    def test_guest_memory_is_derived_after_the_non_gate_core_clamp(self) -> None:
        """A clamped lane is charged for the cores it GETS, not the ones it asked for.

        Deriving before the clamp would bill a 8-core request against a 3-core
        non-gate budget as if it had 8 vCPUs.
        """
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export TARTCI_ROOT={ROOT}
                export TARTCI_LEASE_DIR={Path(td) / "leases"}
                export TARTCI_HOST_CORES=16
                export TARTCI_HOST_MEM_MB=262144
                export TARTCI_ROLE=dedicated-builder
                export TARTCI_VM_LEASE_HEARTBEAT_SECS=1
                export TARTCI_VM_DISK_GROWTH_GB=0
                export TARTCI_VM_DISK_FREE_FLOOR_GB=0
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                tartci_profile_value() {{
                  case "$1" in
                    non_gate_capacity_cores) echo 3 ;;
                    per_compile_job_mem_mb) echo 1536 ;;
                    *) echo 0 ;;
                  esac
                }}
                # No explicit memory → the size must come from the clamped cores.
                tartci_acquire_vm_lease unit-vm 8 tart-linux-vm vm self-hosted,Linux "" {Path(td)}
                python3 "$TARTCI_ROOT/scripts/leases.py" status --store-dir "$TARTCI_LEASE_DIR" --json |
                  python3 -c 'import json,sys; r=json.load(sys.stdin)["leases"][0]; print(r["lease_size_cores"], r["lease_size_mem_mb"])'
                printf 'exported=%s\\n' "$TARTCI_ACTIVE_VM_LEASE_MEM_MB"
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Clamped to 3 cores → 4/3 * 2 * 1536 = 4096, held up by the 8192 floor.
        # Derived from the REQUESTED 8 it would have been 4/3*7*1536 = 14336.
        self.assertEqual(
            proc.stdout.strip().splitlines(), ["3 8192", "exported=8192"]
        )

    def test_charged_guest_memory_is_the_memory_applied_to_the_clone(self) -> None:
        """The plumbing invariant: charged memory and booted memory are one number.

        This is the defect the whole change exists to close — admission charged
        a memory figure and then booted the clone at the golden's baked size,
        so the guest's own build governor sized itself from a number the host
        never reserved. The test walks the provider's real sequence: acquire,
        read the exported size back, size the clone, then compare what the
        lease store recorded against what `tart set` received.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bindir = tmp / "bin"
            bindir.mkdir()
            marker = tmp / "tart-args"
            _write_exec(
                bindir / "tart",
                f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {marker}\n",
            )
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export PATH={bindir}:$PATH
                export TARTCI_ROOT={ROOT}
                export TARTCI_LEASE_DIR={tmp / "leases"}
                export TARTCI_HOST_CORES=16
                export TARTCI_HOST_MEM_MB=262144
                export TARTCI_ROLE=dedicated-builder
                export TARTCI_VM_LEASE_HEARTBEAT_SECS=1
                export TARTCI_VM_DISK_GROWTH_GB=0
                export TARTCI_VM_DISK_FREE_FLOOR_GB=0
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                lease_cores=7
                lease_mem=""
                tartci_acquire_vm_lease demo-vm "$lease_cores" tart-macos-vm gate pulp-build "$lease_mem" {tmp}
                lease_cores="${{TARTCI_ACTIVE_VM_LEASE_CORES:-$lease_cores}}"
                lease_mem="${{TARTCI_ACTIVE_VM_LEASE_MEM_MB:-$lease_mem}}"
                tartci_set_tart_vm_size demo-vm "$lease_cores" "$lease_mem"
                python3 "$TARTCI_ROOT/scripts/leases.py" status --store-dir "$TARTCI_LEASE_DIR" --json |
                  python3 -c 'import json,sys; r=json.load(sys.stdin)["leases"][0]; print(r["lease_size_mem_mb"])'
                """
            )
            proc = _run_bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            charged = int(proc.stdout.strip().splitlines()[-1])
            applied_lines = marker.read_text(encoding="utf-8").strip().splitlines()

        self.assertEqual(len(applied_lines), 1, applied_lines)
        match = re.search(r"--memory (\d+)", applied_lines[0])
        self.assertIsNotNone(
            match, f"tart was never given a --memory: {applied_lines[0]!r}"
        )
        applied = int(match.group(1))
        self.assertEqual(
            applied,
            charged,
            "the clone booted at a size the host never reserved",
        )
        # And the control: the number is the derived one, not some default that
        # happens to match. 7 cores → 4/3 * 6 * 1536 = 12288.
        self.assertEqual(charged, 12288)

    def test_release_clears_the_exported_guest_memory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export TARTCI_ROOT={ROOT}
                export TARTCI_LEASE_DIR={tmp / "leases"}
                export TARTCI_HOST_CORES=16
                export TARTCI_HOST_MEM_MB=262144
                export TARTCI_ROLE=dedicated-builder
                export TARTCI_VM_LEASE_HEARTBEAT_SECS=1
                export TARTCI_VM_DISK_GROWTH_GB=0
                export TARTCI_VM_DISK_FREE_FLOOR_GB=0
                note() {{ :; }}
                source {HELPER}
                tartci_acquire_vm_lease demo-vm 7 tart-macos-vm gate pulp-build "" {tmp}
                printf 'held=%s\\n' "$TARTCI_ACTIVE_VM_LEASE_MEM_MB"
                tartci_release_vm_lease
                printf 'released=%s\\n' "$TARTCI_ACTIVE_VM_LEASE_MEM_MB"
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip().splitlines(), ["held=12288", "released="]
        )

    def test_break_glass_still_exports_a_guest_size(self) -> None:
        """Leases off still has to hand the provider a size to boot at."""
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export TARTCI_ROOT={ROOT}
                export TARTCI_VM_LEASES=0
                note() {{ :; }}
                source {HELPER}
                tartci_acquire_vm_lease demo-vm 7 tart-macos-vm gate pulp-build "" {Path(td)}
                printf '%s %s\\n' "$TARTCI_ACTIVE_VM_LEASE_CORES" "$TARTCI_ACTIVE_VM_LEASE_MEM_MB"
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "7 12288")

    def test_provider_mem_overrides_and_fallbacks(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            TARTCI_ROOT={ROOT}
            export TARTCI_ROOT
            export TARTCI_MACOS_VM_MEM_MB=12288
            export TARTCI_LINUX_VM_MEM_MB=bogus
            source {HELPER}
            printf '%s\\n' "$(tartci_vm_lease_mem_mb tart-macos)"
            printf '%s\\n' "$(tartci_vm_lease_mem_mb tart-linux)"
            printf '%s\\n' "$(tartci_vm_lease_mem_mb qemu-windows 8192)"
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # macos override used verbatim; linux bogus → EMPTY, so acquisition
        # derives the size from the cores it actually grants; windows keeps its
        # caller-supplied WIN_MEMORY fallback (that lane sizes its own guest).
        self.assertEqual(proc.stdout.strip().splitlines(), ["12288", "", "8192"])

    def test_acquire_charges_explicit_vm_memory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                TARTCI_ROOT={ROOT}
                export TARTCI_ROOT
                export TARTCI_LEASE_DIR={Path(td) / "leases"}
                export TARTCI_HOST_CORES=8
                export TARTCI_HOST_MEM_MB=65536
                export TARTCI_ROLE=light
                export TARTCI_VM_LEASE_HEARTBEAT_SECS=1
                export TARTCI_VM_DISK_GROWTH_GB=0
                export TARTCI_VM_DISK_FREE_FLOOR_GB=0
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                tartci_acquire_vm_lease unit-vm 2 tart-linux-vm vm self-hosted,Linux 9000 {Path(td)}
                python3 "$TARTCI_ROOT/scripts/leases.py" status --store-dir "$TARTCI_LEASE_DIR" --json |
                  python3 -c 'import json,sys; print(json.load(sys.stdin)["leases"][0]["lease_size_mem_mb"])'
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The VM lease charged its real memory footprint (9000 MB), not a
        # cores*per-job estimate.
        self.assertEqual(proc.stdout.strip(), "9000")

    def test_acquire_atomically_records_vm_store_growth(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store_path = Path(td) / "vm-store"
            store_path.mkdir()
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export TARTCI_ROOT={ROOT}
                export TARTCI_LEASE_DIR={Path(td) / "leases"}
                export TARTCI_HOST_CORES=8
                export TARTCI_HOST_MEM_MB=65536
                export TARTCI_ROLE=light
                export TARTCI_VM_LEASE_HEARTBEAT_SECS=1
                export TARTCI_VM_DISK_GROWTH_GB=1
                export TARTCI_VM_DISK_FREE_FLOOR_GB=0
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                tartci_acquire_vm_lease unit-vm 2 tart-macos-vm gate labels 8192 {store_path}
                python3 "$TARTCI_ROOT/scripts/leases.py" status --store-dir "$TARTCI_LEASE_DIR" --json |
                  python3 -c 'import json,sys; r=json.load(sys.stdin)["leases"][0]; print(r["disk_growth_bytes"], r["disk_reservation_path"])'
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), f"{1024**3} {store_path.resolve()}")

    def test_tart_size_set_applies_both_acquired_axes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            marker = tmp / "tart-args"
            _write_exec(tmp / "tart", f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {marker}\n")
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export PATH={tmp}:$PATH
                TARTCI_ROOT={ROOT}
                export TARTCI_ROOT
                source {HELPER}
                tartci_set_tart_vm_size demo-vm 4 16384
                """
            )
            proc = _run_bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                marker.read_text(encoding="utf-8").strip(),
                "set demo-vm --cpu 4 --memory 16384",
            )

    def test_tart_cpu_set_alias_still_sizes_only_the_cpu_axis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            marker = tmp / "tart-args"
            _write_exec(tmp / "tart", f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {marker}\n")
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export PATH={tmp}:$PATH
                TARTCI_ROOT={ROOT}
                export TARTCI_ROOT
                source {HELPER}
                tartci_set_tart_vm_cpu demo-vm 4
                """
            )
            proc = _run_bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8").strip(), "set demo-vm --cpu 4")

    def test_disk_floor_refuses_vm_admission_when_free_space_is_too_low(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                TARTCI_ROOT={ROOT}
                export TARTCI_ROOT
                export TARTCI_VM_DISK_FREE_FLOOR_GB=999999
                note() {{ :; }}
                source {STATE_HELPER}
                tartci_check_disk_floor {Path(td)}
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 75, proc.stderr)

    def test_disk_floor_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                TARTCI_ROOT={ROOT}
                export TARTCI_ROOT
                export TARTCI_VM_DISK_FREE_FLOOR_GB=0
                source {STATE_HELPER}
                tartci_check_disk_floor {Path(td)}
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class RunningMacosVmsInventoryTests(unittest.TestCase):
    """A slow or failed `tart list` reads as `unknown`, never as a full host."""

    @classmethod
    def setUpClass(cls) -> None:
        body = MACOS_RUNNER.read_text(encoding="utf-8")
        match = re.search(r"(running_macos_vms\(\)\{\n.*?\n\})\n\nqueued_work\(\)", body, re.S)
        if not match:
            raise AssertionError("running_macos_vms function not found")
        cls.function = match.group(1)

    def _run_with_tart_stub(
        self, stub: str, *, timeout: str = "5", retry_timeout: str = "15"
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_exec(tmp / "tart", stub.replace("@TMP@", str(tmp)))
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export PATH={tmp}:$PATH
                export TARTCI_ROOT={ROOT}
                export TARTCI_MACOS_HARD_MAX=2
                export TARTCI_TART_INVENTORY_TIMEOUT_SECS={timeout}
                export TARTCI_TART_INVENTORY_RETRY_TIMEOUT_SECS={retry_timeout}
                {self.function}
                running_macos_vms
                """
            )
            return _run_bash(script)

    def test_tart_list_error_is_unknown_not_the_hard_cap(self) -> None:
        proc = self._run_with_tart_stub("#!/usr/bin/env bash\nexit 9\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "unknown")

    def test_malformed_tart_list_json_is_unknown(self) -> None:
        proc = self._run_with_tart_stub("#!/usr/bin/env bash\nprintf '{bad-json'\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "unknown")

    def test_timed_out_listing_is_unknown(self) -> None:
        proc = self._run_with_tart_stub(
            "#!/usr/bin/env bash\nsleep 30\n", timeout="0.2", retry_timeout="0.3"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "unknown")

    def test_slow_first_listing_is_retried_with_the_longer_budget(self) -> None:
        # The first `tart list` outlives the short budget; the retry answers.
        stub = """#!/usr/bin/env bash
if [ ! -e @TMP@/first ]; then
  : > @TMP@/first
  sleep 30
fi
printf '[{"Name":"idle","State":"stopped"}]'
"""
        proc = self._run_with_tart_stub(stub, timeout="0.3", retry_timeout="5")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0")

    def test_valid_tart_list_counts_only_running_macos_guests(self) -> None:
        stub = """#!/usr/bin/env bash
if [ "$1" = list ]; then
  printf '[{"Name":"mac","State":"running"},{"Name":"linux","State":"running"},{"Name":"stopped","State":"stopped"}]'
elif [ "$1" = get ] && [ "$2" = mac ]; then
  printf '{"OS":"macOS"}'
elif [ "$1" = get ] && [ "$2" = linux ]; then
  printf '{"OS":"linux"}'
else
  exit 1
fi
"""
        proc = self._run_with_tart_stub(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "1")


class ClaimMacosSlotUnknownInventoryTests(unittest.TestCase):
    """With inventory unknown, the reservation files decide occupancy."""

    CAP_LIB = ROOT / "providers" / "tart-macos" / "macos-vm-cap.lib.sh"

    def _claim(self, live_reservations: int, *, inventory: str, via_arg: bool) -> str:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            resv_dir = tmp / "resv"
            resv_dir.mkdir()
            claim = 'tartci_claim_macos_slot 2 "$INV"' if via_arg else "tartci_claim_macos_slot 2"
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export TARTCI_MACOS_RESV_DIR={resv_dir}
                export TARTCI_MACOS_LOCKDIR={tmp}/lock.d
                export TARTCI_MACOS_CAP_FILE={tmp}/no-cap-file
                INV={inventory}
                running_macos_vms(){{ printf '%s\\n' "$INV"; }}
                source {self.CAP_LIB}
                n=0
                while [ "$n" -lt {live_reservations} ]; do
                  n=$((n + 1))
                  printf '%s %s' "$$" "$(date +%s)" > "$TARTCI_MACOS_RESV_DIR/resv.held$n"
                done
                {claim}
                """
            )
            proc = _run_bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout.strip()

    def test_unknown_inventory_with_free_reservations_boots(self) -> None:
        for via_arg in (True, False):
            with self.subTest(via_arg=via_arg):
                self.assertIn("resv.", self._claim(0, inventory="unknown", via_arg=via_arg))
                self.assertIn("resv.", self._claim(1, inventory="unknown", via_arg=via_arg))

    def test_unknown_inventory_with_full_reservations_waits(self) -> None:
        for via_arg in (True, False):
            with self.subTest(via_arg=via_arg):
                self.assertEqual(self._claim(2, inventory="unknown", via_arg=via_arg), "")

    def test_a_readable_full_inventory_still_blocks(self) -> None:
        self.assertEqual(self._claim(0, inventory="2", via_arg=True), "")
        self.assertEqual(self._claim(0, inventory="2", via_arg=False), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
