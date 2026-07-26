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
    def test_acquire_and_release_records_vm_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                TARTCI_ROOT={ROOT}
                export TARTCI_ROOT
                export TARTCI_LEASE_DIR={Path(td) / "leases"}
                export TARTCI_HOST_CORES=8
                export TARTCI_ROLE=light
                export TARTCI_VM_LEASE_HEARTBEAT_SECS=1
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                tartci_acquire_vm_lease unit-vm 2 tart-linux-vm vm self-hosted,Linux
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
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                tartci_profile_value() {{ echo 3; }}   # force non-gate budget = 3
                # explicit tiny mem so the core clamp is isolated from the memory axis
                tartci_acquire_vm_lease unit-vm 8 tart-linux-vm vm self-hosted,Linux 1024
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
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                tartci_profile_value() {{ echo 3; }}   # non-gate budget = 3 (must be ignored for gate)
                tartci_acquire_vm_lease gate-vm 5 tart-macos-vm gate pulp-build 1024
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
                note() {{ :; }}
                source {HELPER}
                trap tartci_release_vm_lease EXIT
                tartci_acquire_vm_lease unit-vm 2 tart-linux-vm vm self-hosted,Linux 9000
                python3 "$TARTCI_ROOT/scripts/leases.py" status --store-dir "$TARTCI_LEASE_DIR" --json |
                  python3 -c 'import json,sys; print(json.load(sys.stdin)["leases"][0]["lease_size_mem_mb"])'
                """
            )
            proc = _run_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The VM lease charged its real memory footprint (9000 MB), not a
        # cores*per-job estimate.
        self.assertEqual(proc.stdout.strip(), "9000")

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
