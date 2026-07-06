#!/usr/bin/env python3
"""Behavioral test for the macOS runner's EPHEMERAL per-boot registration name.

A fixed STATIC name (the bare $RUNNER_NAME, e.g. `pulp-vm-01`) reused across boots
lets a SIGKILL'd VM orphan a GitHub runner registration that lingers "offline but
running a job". The next boot then collides on that name
(`generate-jitconfig` -> HTTP 409 "already exists") and, without repo-admin to
reclaim it, wedges the ENTIRE macOS gate until an admin deletes the ghost by hand
(pulp-runner-ops "Sixth symptom", 2026-07-06). `ephemeral_boot_name` must therefore
produce a name that is (a) never the bare static name, (b) unique per boot index,
and (c) shaped <lane>-<supervisor pid>-<i>, mirroring the qemu-windows lane.

Rather than assert the source contains a string, we EXTRACT the function and run it
in ONE bash process (so both boots share $$, the supervisor PID) — matching the real
supervisor — and also exercise the `--print-boot-name` hook end to end. No gh/tart
needed, so this runs on any platform in CI.

Run:  python3 scripts/test_macos_ephemeral_name.py
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "providers" / "tart-macos" / "runner.sh"
# The one-line function definition; pinned so a refactor that drops it is caught.
FN_RE = re.compile(r"^ephemeral_boot_name\(\)\{.*\}$", re.M)


class EphemeralBootNameTests(unittest.TestCase):
    def setUp(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        m = FN_RE.search(body)
        self.assertIsNotNone(m, "ephemeral_boot_name() must exist in runner.sh")
        self.fn = m.group(0)

    def _names_same_process(self) -> tuple[str, str]:
        # Define the fn + RUNNER_NAME in ONE bash process so both calls share $$,
        # exactly as the long-lived supervisor does.
        # ephemeral_boot_name uses `printf` with no trailing newline (it is captured
        # via $(...) in run_one), so wrap each call in echo to get one name per line.
        script = (
            "RUNNER_NAME=pulp-vm-01\n"
            f"{self.fn}\n"
            'echo "$(ephemeral_boot_name 1)"\n'
            'echo "$(ephemeral_boot_name 2)"\n'
        )
        out = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=True
        )
        b1, b2 = out.stdout.split()
        return b1, b2

    def test_not_bare_static_name(self) -> None:
        b1, b2 = self._names_same_process()
        # The exact regression: the boot registration must never BE the reusable
        # static lane name, or a dead VM's ghost collides with the next boot.
        self.assertNotEqual(b1, "pulp-vm-01")
        self.assertNotEqual(b2, "pulp-vm-01")

    def test_shape_lane_pid_index(self) -> None:
        b1, b2 = self._names_same_process()
        self.assertRegex(b1, r"^pulp-vm-01-\d+-1$")
        self.assertRegex(b2, r"^pulp-vm-01-\d+-2$")

    def test_same_supervisor_distinct_per_boot(self) -> None:
        b1, b2 = self._names_same_process()
        self.assertNotEqual(b1, b2, "distinct boots must yield distinct names (no reuse)")
        # Same process -> shared pid segment; only the trailing boot index differs.
        self.assertEqual(b1.rsplit("-", 1)[0], b2.rsplit("-", 1)[0])

    def test_print_boot_name_hook_end_to_end(self) -> None:
        out = subprocess.run(
            ["bash", str(SCRIPT), "--name", "pulp-vm-01", "--print-boot-name", "1"],
            capture_output=True, text=True, check=True,
        )
        self.assertRegex(out.stdout.strip(), r"^pulp-vm-01-\d+-1$")


if __name__ == "__main__":
    unittest.main()
