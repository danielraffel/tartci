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
            export TARTCI_LINUX_VM_CORES=5
            export TARTCI_WIN_VM_CORES=bogus
            source {HELPER}
            printf '%s\\n' "$(tartci_vm_lease_cores tart-linux)"
            printf '%s\\n' "$(tartci_vm_lease_cores qemu-windows 6)"
            """
        )
        proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines(), ["5", "6"])

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
        # macos override; linux bogus → per-guest default 8192; windows from
        # WIN_MEMORY fallback.
        self.assertEqual(proc.stdout.strip().splitlines(), ["12288", "8192", "8192"])

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

    def test_tart_cpu_set_uses_acquired_core_count(self) -> None:
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


class RunningMacosVmsFailClosedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        body = MACOS_RUNNER.read_text(encoding="utf-8")
        match = re.search(r"(running_macos_vms\(\)\{\n.*?\n\})\n\nqueued_work\(\)", body, re.S)
        if not match:
            raise AssertionError("running_macos_vms function not found")
        cls.function = match.group(1)

    def _run_with_tart_stub(self, stub: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_exec(tmp / "tart", stub)
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export PATH={tmp}:$PATH
                export TARTCI_ROOT={ROOT}
                export TARTCI_MACOS_HARD_MAX=2
                {self.function}
                running_macos_vms
                """
            )
            return _run_bash(script)

    def test_tart_list_error_counts_as_full_hard_cap(self) -> None:
        proc = self._run_with_tart_stub("#!/usr/bin/env bash\nexit 9\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "2")

    def test_malformed_tart_list_json_counts_as_full_hard_cap(self) -> None:
        proc = self._run_with_tart_stub("#!/usr/bin/env bash\nprintf '{bad-json'\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "2")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
