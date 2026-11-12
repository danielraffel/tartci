#!/usr/bin/env python3
"""Scratch VMs: owner, TTL, warning and deletion (scripts/scratch_vm.py)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scratch_vm as sv  # noqa: E402

NOW = 1792120000.0


class FakeTart:
    def __init__(self) -> None:
        self.vms: set[str] = set()
        self.calls: list[tuple[list, dict]] = []
        self.fail_delete = False

    def __call__(self, argv: list, env: dict) -> "subprocess.CompletedProcess[str]":
        self.calls.append((list(argv), dict(env)))
        verb, name = argv[0], argv[-1]
        if verb == "clone":
            if argv[1] == "missing-image":
                return subprocess.CompletedProcess(argv, 1, "", "image not found")
            self.vms.add(name)
        elif verb == "delete":
            if self.fail_delete:
                return subprocess.CompletedProcess(argv, 1, "", "resource busy")
            if name not in self.vms:
                return subprocess.CompletedProcess(argv, 1, "", f"the VM {name} does not exist")
            self.vms.discard(name)
        return subprocess.CompletedProcess(argv, 0, "", "")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = self.root / "VMs"
        self.store.mkdir()
        patcher = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.root / ".tartci"),
                                               "TART_HOME": str(self.store)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tart = FakeTart()

    def make(self, ttl: float = 2, owner: str = "pulp-impl", **kwargs) -> str:
        rc, text = sv.create(owner, ttl, "ghcr.io/cirruslabs/ubuntu:latest", kwargs.get("name"),
                             NOW, tart=self.tart)
        self.assertEqual(rc, 0, text)
        return text.split()[2]


class Create(Base):
    def test_create_records_owner_ttl_and_store_and_clones_into_the_store(self) -> None:
        name = self.make(ttl=2)
        self.assertTrue(name.startswith("scratch-pulp-impl-"))
        record = sv.read(name)
        self.assertEqual((record["owner"], record["store"], record["expires_at"]),
                         ("pulp-impl", str(self.store), NOW + 7200))
        argv, env = self.tart.calls[0]
        self.assertEqual(argv, ["clone", "ghcr.io/cirruslabs/ubuntu:latest", name])
        self.assertEqual(env, {"TART_HOME": str(self.store)})

    def test_bad_owner_name_ttl_and_a_failed_clone_leave_nothing(self) -> None:
        self.assertEqual(sv.create("bad owner!", 2, "img", None, NOW, tart=self.tart)[0], 2)
        self.assertEqual(sv.create("a", 2, "img", "lv2repro-pulpimpl", NOW, tart=self.tart)[0], 2)
        rc, text = sv.create("a", 2, "missing-image", None, NOW, tart=self.tart)
        self.assertEqual(rc, 1)
        self.assertIn("image not found", text)
        self.assertEqual(sv.records(), [])
        for bad in ("0", "-1", "73"):
            with self.subTest(ttl=bad), self.assertRaises(Exception):
                sv._ttl(bad)
        self.assertEqual(sv._ttl("72"), 72.0)


class Enforce(Base):
    def test_warns_once_at_ttl_minus_30_minutes_then_deletes_at_the_ttl(self) -> None:
        name = self.make(ttl=2)
        self.assertEqual(sv.enforce(NOW + 7200 - 1801, tart=self.tart), [])
        warn = sv.enforce(NOW + 7200 - 1800, tart=self.tart)
        self.assertEqual(len(warn), 1)
        self.assertIn(f"WARN scratch-vm: {name} (owner pulp-impl) is stopped and deleted", warn[0])
        self.assertIn(f"tartci scratch-vm extend {name}", warn[0])
        self.assertEqual(sv.enforce(NOW + 7200 - 60, tart=self.tart), [])   # warned once
        gone = sv.enforce(NOW + 7200, tart=self.tart)
        self.assertIn(f"deleted scratch VM {name} (owner pulp-impl)", gone[0])
        self.assertEqual([c[0][0] for c in self.tart.calls[-2:]], ["stop", "delete"])
        self.assertNotIn(name, self.tart.vms)
        self.assertEqual(sv.records(), [])

    def test_extend_moves_the_deadline_and_rearms_the_warning(self) -> None:
        name = self.make(ttl=1)
        sv.enforce(NOW + 1800, tart=self.tart)
        self.assertEqual(sv.extend(name, 3, NOW + 1800)[0], 0)
        self.assertEqual(sv.enforce(NOW + 3600, tart=self.tart), [])
        self.assertIn(name, self.tart.vms)
        self.assertEqual(len(sv.enforce(NOW + 1800 + 3 * 3600 - 1800, tart=self.tart)), 1)

    def test_a_failed_delete_keeps_the_record_and_retries(self) -> None:
        name = self.make(ttl=1)
        self.tart.fail_delete = True
        line = sv.enforce(NOW + 3600, tart=self.tart)[0]
        self.assertIn("WARN scratch-vm: tart delete", line)
        self.assertIsNotNone(sv.read(name))
        self.tart.fail_delete = False
        sv.enforce(NOW + 3700, tart=self.tart)
        self.assertIsNone(sv.read(name))

    def test_a_vm_already_gone_clears_its_record(self) -> None:
        name = self.make(ttl=1)
        self.tart.vms.discard(name)
        self.assertIn("deleted scratch VM", sv.enforce(NOW + 3600, tart=self.tart)[0])
        self.assertIsNone(sv.read(name))

    def test_only_recorded_scratch_vms_are_ever_touched(self) -> None:
        self.tart.vms.update({"lv2repro-pulpimpl", "m1-pulp-gate-01-1-1"})
        self.make(ttl=1)
        sv.enforce(NOW + 3600, tart=self.tart)
        touched = {c[0][-1] for c in self.tart.calls if c[0][0] in ("stop", "delete")}
        self.assertTrue(all(name.startswith("scratch-") for name in touched), touched)
        self.assertIn("lv2repro-pulpimpl", self.tart.vms)


class Filter(Base):
    def test_a_record_for_a_non_scratch_vm_is_never_deleted(self) -> None:
        # A hand-written or corrupted record naming a lane or an agent's VM.
        self.tart.vms.update({"lv2repro-pulpimpl", "pulp-build-runner:latest"})
        for name in ("lv2repro-pulpimpl", "pulp-build-runner:latest"):
            sv.records_dir().mkdir(parents=True, exist_ok=True)
            (sv.records_dir() / f"scratch-{len(name)}.json").write_text(sv.json.dumps(
                {"name": name, "owner": "x", "store": str(self.store),
                 "expires_at": NOW - 10, "warned_at": None}))
        self.assertEqual(sv.enforce(NOW, tart=self.tart), [])
        self.assertEqual([c for c in self.tart.calls if c[0][0] in ("stop", "delete")], [])
        self.assertEqual(self.tart.vms, {"lv2repro-pulpimpl", "pulp-build-runner:latest"})


class DryRun(Base):
    def test_enforce_dry_run_names_what_it_would_do_and_changes_nothing(self) -> None:
        late = self.make(ttl=1)
        soon = self.make(ttl=1.5, name="scratch-soon")
        lines = sv.enforce(NOW + 3600 + 1, tart=self.tart, dry_run=True)
        self.assertEqual(len(lines), 2)
        self.assertIn(f"would stop and delete scratch VM {late}", lines[0] + lines[1])
        self.assertIn("would warn: scratch VM scratch-soon", lines[0] + lines[1])
        self.assertEqual([c for c in self.tart.calls if c[0][0] in ("stop", "delete")], [])
        self.assertIsNone(sv.read(soon)["warned_at"])

    def test_the_default_image_is_pinned_by_digest(self) -> None:
        self.assertIn("@sha256:", sv.DEFAULT_IMAGE)
        self.assertNotIn(":latest", sv.DEFAULT_IMAGE)


class WatchdogPass(Base):
    def test_the_watchdog_pass_returns_the_lines_enforce_produced(self) -> None:
        # Without the pass's return line every scratch-vm warning and deletion
        # would happen silently.
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        name = self.make(ttl=1)
        warn = watchdog.scratch_vm_pass(now=NOW + 1800, tart=self.tart)
        self.assertIn(f"WARN scratch-vm: {name} (owner pulp-impl)", warn)
        gone = watchdog.scratch_vm_pass(now=NOW + 3600, tart=self.tart)
        self.assertIn(f"deleted scratch VM {name}", gone)
        self.assertIsNone(watchdog.scratch_vm_pass(now=NOW + 3700, tart=self.tart))


class Wiring(unittest.TestCase):
    def test_the_heal_pass_and_the_dispatcher_run_it(self) -> None:
        here = Path(sv.__file__).parent
        self.assertIn("scratch_line = scratch_vm_pass()",
                      (here / "tartci_launchd_watchdog.py").read_text())
        self.assertIn('scratch-vm) shift; tartci_verified_python_run "$HERE/scripts/scratch_vm.py"',
                      (here.parent / "tartci").read_text())


if __name__ == "__main__":
    unittest.main()
