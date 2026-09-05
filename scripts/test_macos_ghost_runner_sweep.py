#!/usr/bin/env python3
"""A per-boot registration is never reused, so an offline one is residue.

Ghosts are tolerated by design -- a never-reused name cannot collide, which is
what stops a SIGKILLed VM from wedging the whole gate -- but an offline
registration still advertises its labels, so a lane served only by ghosts reads
as served. These tests pin the sweep to its own lane.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).parents[1] / "providers" / "tart-macos" / "runner.sh"


def extract(name: str) -> str:
    text = RUNNER.read_text()
    start = text.index(f"\n{name}(){{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


class GhostRunnerSweepTests(unittest.TestCase):
    LANE = "m5-forge-gate-01"

    RUNNERS = {
        "runners": [
            # this lane's residue -- both must go
            {"id": 1, "name": "m5-forge-gate-01-75546-7", "status": "offline"},
            {"id": 2, "name": "m5-forge-gate-01-75546-9", "status": "offline"},
            # this lane, but the boot we are standing up right now
            {"id": 3, "name": "m5-forge-gate-01-99999-1", "status": "offline"},
            # this lane, alive -- never touch a live registration
            {"id": 4, "name": "m5-forge-gate-01-75546-11", "status": "online"},
            # ANOTHER lane's residue -- isolation
            {"id": 5, "name": "studio-forge-gate-01-3771-1", "status": "offline"},
            # a lane whose name merely starts the same way
            {"id": 6, "name": "m5-forge-gate-01-extra-2-1", "status": "offline"},
            # a statically named runner, not per-boot shaped
            {"id": 7, "name": "pulp-intel-macmini", "status": "offline"},
        ]
    }

    def run_sweep(self, keep: str, fail_delete: bool = False):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = root / "runners.json"
            payload.write_text(json.dumps(self.RUNNERS))
            deleted = root / "deleted.txt"
            gh = root / "fake-gh"
            gh.write_text(
                "#!/bin/bash\n"
                'if [ "$1" = "api" ] && [ "$2" = "-X" ] && [ "$3" = "DELETE" ]; then\n'
                f'  echo "$4" >> "{deleted}"\n'
                f'  exit {1 if fail_delete else 0}\n'
                "fi\n"
                "filter=\"\"\n"
                'while [ $# -gt 0 ]; do\n'
                '  if [ "$1" = "--jq" ]; then filter="$2"; shift; fi\n'
                "  shift\n"
                "done\n"
                f'exec jq -r "$filter" "{payload}"\n'
            )
            gh.chmod(0o755)
            script = (
                "set -u\n"
                f'RUNNER_NAME="{self.LANE}"\n'
                f'GH_CLI="{gh}"\n'
                "note(){ printf 'NOTE %s\\n' \"$*\"; }\n"
                + extract("sweep_lane_ghost_runners")
                + f'\nsweep_lane_ghost_runners "https://api/runners" "{keep}"\n'
            )
            proc = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, env=dict(os.environ)
            )
            ids = []
            if deleted.exists():
                ids = [int(line.rsplit("/", 1)[-1])
                       for line in deleted.read_text().split() if line.strip()]
            return sorted(ids), proc.stdout, proc.stderr

    def test_sweeps_only_this_lanes_finished_boots(self) -> None:
        ids, out, err = self.run_sweep(keep="m5-forge-gate-01-99999-1")
        self.assertEqual(err.strip(), "")
        # POSITIVE CONTROL: it deleted something, so the filter is not inert.
        self.assertTrue(ids, "swept nothing -- the selector matched no fixture")
        self.assertEqual(ids, [1, 2])
        self.assertIn("swept ghost runner registration", out)

    def test_never_touches_another_lane_or_a_live_registration(self) -> None:
        ids, _, _ = self.run_sweep(keep="m5-forge-gate-01-99999-1")
        for forbidden, why in (
            (3, "the boot being stood up right now"),
            (4, "an ONLINE registration"),
            (5, "another lane's residue"),
            (6, "a lane that merely shares a name prefix"),
            (7, "a statically named runner"),
        ):
            self.assertNotIn(forbidden, ids, why)

    def test_a_sweep_that_cannot_delete_says_so(self) -> None:
        _, out, _ = self.run_sweep(
            keep="m5-forge-gate-01-99999-1", fail_delete=True
        )
        self.assertIn("could not be swept", out)
        self.assertNotIn("swept ghost runner registration", out)

    def test_a_deregistration_that_never_succeeded_leaves_a_trace(self) -> None:
        source = extract("reclaim_runner_name")
        self.assertIn("could not be deleted", source)
        self.assertRegex(source, r"id=\"\"; break")


if __name__ == "__main__":
    unittest.main()
