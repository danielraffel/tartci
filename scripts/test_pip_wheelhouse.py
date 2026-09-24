#!/usr/bin/env python3
"""Tests for the optional read-only pip wheelhouse served to macOS guests."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers/tart-macos/pip-wheelhouse.lib.sh"
SYNC = ROOT / "scripts/pip-wheelhouse.sh"
MAC_JIT = ROOT / "providers/tart-macos/runner.sh"

HASHED_LOCK = "numpy==2.5.3 \\\n    --hash=sha256:" + "0" * 64 + "\n"


class WheelhouseReadyTests(unittest.TestCase):
    def _ready(self, path: str) -> bool:
        proc = subprocess.run(
            ["bash", "-c", f'source "{LIB}"; pip_wheelhouse_ready "$1"', "_", path],
            capture_output=True, text=True, check=False,
        )
        return proc.returncode == 0

    def test_directory_with_a_wheel_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "numpy-2.5.3-cp314-cp314-macosx_14_0_arm64.whl").write_bytes(b"x")
            self.assertTrue(self._ready(tmp))

    def test_absent_or_empty_directory_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self._ready(tmp))
            self.assertFalse(self._ready(str(Path(tmp) / "missing")))
            (Path(tmp) / "README").write_text("not a wheel")
            self.assertFalse(self._ready(tmp))

    def test_unshareable_paths_are_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            colon = Path(tmp) / "a:b"
            colon.mkdir()
            (colon / "x-1-py3-none-any.whl").write_bytes(b"x")
            self.assertFalse(self._ready(str(colon)))
        self.assertFalse(self._ready("relative/wheelhouse"))
        self.assertFalse(self._ready(""))


class WheelhouseSyncTests(unittest.TestCase):
    """Drive the sync script with a stub interpreter standing in for pip."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.argv_log = self.tmp / "argv.log"
        self.stub = self.tmp / "stub-python"
        # Writes one wheel per --dest, named by STUB_WHEEL, with STUB_BYTES.
        self.stub.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            printf '%s\\n' "$*" >> {self.argv_log}
            dest=""
            while [ "$#" -gt 0 ]; do [ "$1" = "--dest" ] && dest="$2"; shift; done
            [ -n "${{STUB_FAIL:-}}" ] && exit 1
            [ -n "${{STUB_WHEEL:-}}" ] && printf '%s' "${{STUB_BYTES:-a}}" > "$dest/$STUB_WHEEL"
            exit 0
            """))
        self.stub.chmod(0o755)
        self.lock = self.tmp / "requirements.lock"
        self.lock.write_text(HASHED_LOCK)
        self.house = self.tmp / "wheelhouse"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _sync(self, *extra: str, **env: str) -> subprocess.CompletedProcess:
        args = [
            "bash", str(SYNC), "sync", "--lock", str(self.lock),
            "--python-version", "3.14", "--platform", "macosx_14_0_arm64",
            "--dir", str(self.house), "--python", str(self.stub), *extra,
        ]
        return subprocess.run(
            args, capture_output=True, text=True, check=False,
            env={**os.environ, "STUB_WHEEL": "numpy-2.5.3-cp314-cp314-macosx_14_0_arm64.whl", **env},
        )

    def test_downloads_hash_pinned_binaries_for_the_guest_interpreter(self) -> None:
        proc = self._sync()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.argv_log.read_text()
        for flag in ("-m pip download", "--require-hashes", "--only-binary=:all:",
                     "--no-deps", "--python-version 3.14",
                     "--platform macosx_14_0_arm64"):
            self.assertIn(flag, argv)
        self.assertTrue((self.house / "numpy-2.5.3-cp314-cp314-macosx_14_0_arm64.whl").is_file())

    def test_staging_is_removed(self) -> None:
        self.assertEqual(self._sync().returncode, 0)
        self.assertEqual([p.name for p in self.house.iterdir()],
                         ["numpy-2.5.3-cp314-cp314-macosx_14_0_arm64.whl"])

    def test_resync_is_additive_and_idempotent(self) -> None:
        self.assertEqual(self._sync().returncode, 0)
        proc = self._sync()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("0 added, 1 already present", proc.stdout)
        proc = self._sync(STUB_WHEEL="scipy-1.18.1-cp314-cp314-macosx_14_0_arm64.whl")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(list(self.house.glob("*.whl"))), 2)

    def test_never_rewrites_a_wheel_a_guest_may_be_reading(self) -> None:
        self.assertEqual(self._sync().returncode, 0)
        proc = self._sync(STUB_BYTES="different")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refusing to replace", proc.stderr)
        self.assertEqual(
            (self.house / "numpy-2.5.3-cp314-cp314-macosx_14_0_arm64.whl").read_text(), "a")

    def test_rejects_a_lock_without_hashes(self) -> None:
        self.lock.write_text("numpy==2.5.3\n")
        proc = self._sync()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--generate-hashes", proc.stderr)
        self.assertFalse(self.argv_log.exists())

    def test_rejects_an_unshareable_directory(self) -> None:
        self.house = self.tmp / "a:b"
        proc = self._sync()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cannot share", proc.stderr)

    def test_pip_failure_fails_the_sync(self) -> None:
        proc = self._sync(STUB_FAIL="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(list(self.house.glob("*.whl")), [])


class RunnerWiringTests(unittest.TestCase):
    """The JIT runner mounts the wheelhouse and declares the guest's lease."""

    body = MAC_JIT.read_text(encoding="utf-8")

    def test_runner_sources_the_library(self) -> None:
        self.assertIn('source "$TARTCI_ROOT/providers/tart-macos/pip-wheelhouse.lib.sh"', self.body)

    def test_mount_is_gated_on_a_ready_wheelhouse(self) -> None:
        self.assertIn('if pip_wheelhouse_ready "$PIP_WHEELHOUSE_ROOT"; then', self.body)
        self.assertIn('--dir="pip-wheelhouse:$PIP_WHEELHOUSE_ROOT:ro"', self.body)

    def test_job_env_declares_lease_and_wheelhouse(self) -> None:
        self.assertIn("TARTCI_GUEST_CORES=%s", self.body)
        self.assertIn("TARTCI_GUEST_MEM_MB=%s", self.body)
        self.assertIn("TARTCI_PIP_WHEELHOUSE=%s", self.body)
        self.assertIn('GUEST_PIP_WHEELHOUSE="/Volumes/My Shared Files/pip-wheelhouse"', self.body)

    def test_preserved_env_cannot_forge_the_declarations(self) -> None:
        self.assertIn("TARTCI_GUEST_CORES|TARTCI_GUEST_MEM_MB|TARTCI_PIP_WHEELHOUSE)$/", self.body)

    def test_declared_lease_is_the_size_applied_to_the_clone(self) -> None:
        sized = self.body.index('tartci_set_tart_vm_size "$vm" "$lease_cores" "$lease_mem"')
        declared = self.body.index('CURRENT_GUEST_CORES="$lease_cores"')
        self.assertLess(sized, declared)
        self.assertIn('CURRENT_GUEST_MEM_MB="$lease_mem"', self.body)


if __name__ == "__main__":
    unittest.main()
