#!/usr/bin/env python3
"""tart_image_prune: which images and VM husks a plan deletes, and why not."""

from __future__ import annotations

import os
import pathlib
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import tart_image_prune as tip  # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400


def vm(name: str, days: float = 40, state: str = "stopped", size: int = 45) -> dict:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - days * DAY))
    return {"Name": name, "Source": "local", "State": state,
            "Running": state == "running", "Accessed": stamp, "Size": size}


class Plan(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.vms = pathlib.Path(self._tmp.name) / "vms"
        self.vms.mkdir()

    def plan(self, vms, *, profile=("pulp-build-runner:latest",), manifest=(), text="",
             opened=("/dev/null",)):
        return tip.plan(vms, vms_dir=self.vms, profile_values=set(profile),
                        manifest=set(manifest), text=text,
                        opened=None if opened is None else list(opened), now=NOW,
                        min_idle_days=14)

    def verdicts(self, report) -> dict:
        return {i["name"]: i["reason"] if i["verdict"] == "keep" else "DELETE"
                for i in report["images"]}

    def test_superseded_tags_go_and_every_reference_keeps(self):
        report = self.plan([
            vm("pulp-build-runner:latest"),                      # profile golden
            vm("pulp-build-runner:2026-08-11-runner-2.336.0"),   # superseded
            vm("pulp-build-runner-upgrade-20260811"),            # golden-base variant
            vm("pulp-build-runner:m1-rollback-20260801"),        # older rollback
            vm("pulp-build-runner:m1-rollback-20260826"),        # newest rollback
            vm("macos-build-base:latest"),                       # a bake tier
            vm("pulp-build-base:old"),                           # manifest image
            vm("pulp-linux-build:gh-20260809"),                  # named by state
            vm("pulp-build-runner:m3-chrome-quarantine-1"),      # evidence
            vm("pulp-build-runner:fresh", days=3),               # recent
            vm("pulp-build-runner:busy", state="running"),
        ], manifest=("pulp-build-base",), text='{"rollback": "pulp-linux-build:gh-20260809"}')
        self.assertEqual(self.verdicts(report), {
            "pulp-build-runner:latest": "named_by_profile",
            "pulp-build-runner:2026-08-11-runner-2.336.0": "DELETE",
            "pulp-build-runner-upgrade-20260811": "DELETE",
            "pulp-build-runner:m1-rollback-20260801": "DELETE",
            "pulp-build-runner:m1-rollback-20260826": "newest_rollback",
            "macos-build-base:latest": "latest_tag",
            "pulp-build-base:old": "vm_image_manifest",
            "pulp-linux-build:gh-20260809": "named_by_state",
            "pulp-build-runner:m3-chrome-quarantine-1": "evidence_name",
            "pulp-build-runner:fresh": "recent",
            "pulp-build-runner:busy": "running",
        })
        self.assertEqual(report["delete_bytes"], 3 * 45 * tip.GIB)

    def test_a_lane_vm_is_listed_never_planned(self):
        report = self.plan([vm("m1-pulp-gate-slot2-02-52028-7", days=60)])
        self.assertEqual(report["images"], [])
        self.assertEqual(report["slot_vms"][0]["name"], "m1-pulp-gate-slot2-02-52028-7")

    def test_open_files_or_a_blind_lsof_keep_the_image(self):
        name = "pulp-build-runner:old"
        held = self.plan([vm(name)], opened=[str(self.vms / name / "disk.img")])
        self.assertEqual(self.verdicts(held), {name: "open_files"})
        blind = self.plan([vm(name)], opened=None)
        self.assertEqual(self.verdicts(blind), {name: "open_files_unknown"})

    def test_a_name_inside_a_longer_name_is_not_a_reference(self):
        report = self.plan([vm("pulp-build-runner:old")],
                           text="pulp-build-runner:old-but-kept")
        self.assertEqual(self.verdicts(report), {"pulp-build-runner:old": "DELETE"})

    def test_only_old_socket_only_husks_are_empty_dirs(self):
        husk = self.vms / "studio-pulp-gate-slot2-02-15416-1"
        husk.mkdir()
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        short = pathlib.Path(tempfile.mkdtemp(prefix="s", dir="/tmp"))
        self.addCleanup(lambda: os.rmdir(short))
        sock.bind(str(short / "c"))
        os.rename(short / "c", husk / "control.sock")
        real = self.vms / "pulp-build-runner:latest"
        real.mkdir()
        (real / "config.json").write_text("{}")
        young = self.vms / "slot-being-cloned"
        young.mkdir()
        internal = self.vms / "cache"
        internal.mkdir()
        payload = self.vms / "something"
        payload.mkdir()
        (payload / "notes.txt").write_text("x")
        for path in (husk, real, internal, payload):
            os.utime(path, (NOW - DAY, NOW - DAY))
        os.utime(young, (NOW - 60, NOW - 60))
        report = self.plan([vm("pulp-build-runner:latest")])
        self.assertEqual(report["empty_dirs"], [str(husk)])


if __name__ == "__main__":
    unittest.main()
