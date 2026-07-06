#!/usr/bin/env python3
"""Hermetic tests for the tartci VM janitor."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_reap


def write_state(path: Path, **fields: object) -> None:
    data = {
        "ts": "2020-01-01T00:00:00Z",
        "repo": "danielraffel/pulp",
        "labels": "self-hosted",
        "supervisor_pid": "999999",
        "supervisor_pid_started_at": "",
    }
    data.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class VmReapTests(unittest.TestCase):
    def run_digest(self, root: Path, *extra: str):
        args = vm_reap.parse_args(
            [
                "--repo",
                "danielraffel/pulp",
                "--state-root",
                str(root),
                "--prefixes",
                "pulp-,linux-ephr-,win-ephr-,tartci-",
                *extra,
            ]
        )
        with mock.patch.object(vm_reap.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(vm_reap, "tart_vms", return_value=[]), \
             mock.patch.object(vm_reap, "github_runners", return_value=[]):
            return vm_reap.build_digest(args)

    def run_digest_with_missing_tart(self, root: Path, *extra: str):
        args = vm_reap.parse_args(
            [
                "--repo",
                "danielraffel/pulp",
                "--state-root",
                str(root),
                "--prefixes",
                "pulp-,linux-ephr-,win-ephr-,tartci-",
                *extra,
            ]
        )

        def which(tool: str):
            return None if tool == "tart" else "/usr/bin/tool"

        with mock.patch.object(vm_reap.shutil, "which", side_effect=which), \
             mock.patch.object(vm_reap, "github_runners", return_value=[]):
            return vm_reap.build_digest(args)

    def test_qemu_windows_state_reaps_ownerless_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            work = Path(td) / "tartci-win" / "win-ephr-host-1"
            port_lock = Path(td) / "tartci-win" / "port-locks" / "2222.lock"
            work.mkdir(parents=True)
            port_lock.mkdir(parents=True)
            write_state(
                root / "windows" / "win-ephr-host-1.state.json",
                provider="qemu-windows",
                runner="win-ephr-host-1",
                vm="win-ephr-host-1",
                work_dir=str(work),
                port_lock=str(port_lock),
                qemu_pid="999998",
                qemu_pid_started_at="",
            )

            digest, rc = self.run_digest(root, "--fix")

            self.assertEqual(rc, 0, digest)
            self.assertFalse(work.exists())
            self.assertFalse(port_lock.exists())
            self.assertFalse((root / "windows" / "win-ephr-host-1.state.json").exists())
            self.assertIn("path_deleted", "\n".join(digest["fixed"]))

    def test_qemu_reap_still_reports_when_tart_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            work = Path(td) / "tartci-win" / "win-ephr-host-3"
            work.mkdir(parents=True)
            write_state(
                root / "windows" / "win-ephr-host-3.state.json",
                provider="qemu-windows",
                runner="win-ephr-host-3",
                vm="win-ephr-host-3",
                work_dir=str(work),
                qemu_pid="999998",
                qemu_pid_started_at="",
            )

            digest, rc = self.run_digest_with_missing_tart(root, "--fix")

            self.assertEqual(rc, 2, digest)
            self.assertIn("missing_tool:tart", digest["problems"])
            self.assertFalse(work.exists())
            self.assertFalse((root / "windows" / "win-ephr-host-3.state.json").exists())

    def test_qemu_keep_failed_live_vm_is_not_reaped_inside_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            pid = os.getpid()
            write_state(
                root / "windows" / "win-ephr-host-2.state.json",
                provider="qemu-windows",
                runner="win-ephr-host-2",
                vm="win-ephr-host-2",
                qemu_pid=str(pid),
                qemu_pid_started_at=vm_reap.pid_start(pid),
                keep_failed=True,
            )

            digest, rc = self.run_digest(root, "--keep-failed-age-secs", "999999999")

            self.assertEqual(rc, 0, digest)
            row = next(row for row in digest["vms"] if row["name"] == "win-ephr-host-2")
            self.assertEqual(row["action"], "kept_failed_wait_for_operator")
            self.assertFalse(row["stale"])

    def test_tart_linux_missing_state_is_deleted_when_owner_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state"
            state = root / "linux" / "linux-ephr-123.state.json"
            write_state(
                state,
                provider="tart-linux",
                runner="linux-ephr-123",
                vm="linux-ephr-123",
            )

            digest, rc = self.run_digest(root, "--fix")

            self.assertEqual(rc, 0, digest)
            self.assertFalse(state.exists())
            self.assertIn(f"state_deleted:{state}", digest["fixed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
