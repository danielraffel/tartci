#!/usr/bin/env python3
"""Behavioral tests for scripts/goldens.sh (`tartci goldens`).

These exercise the OFFLINE paths only — canonical/drift detection over a synthetic
$TARTCI_GOLDENS and the arg/help/error handling — so they need no ssh/tart/gh and
run on the ubuntu lint host. The `sync` transfer path (rsync/verify/reload) needs
a peer host and is covered by the manual runbook in docs/golden-sync.md, not here.

Run:  python3 scripts/test_goldens.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDENS_SH = os.path.join(HERE, "goldens.sh")


def run(args, goldens_dir):
    env = dict(os.environ, TARTCI_GOLDENS=goldens_dir)
    return subprocess.run(
        ["bash", GOLDENS_SH, *args],
        env=env, capture_output=True, text=True,
    )


class GoldensCli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tartci-goldens-test-")
        # two windows goldens with distinct mtimes; the newer is canonical
        self.old = os.path.join(self.dir, "pulp-windows-build-24h2-arm64-2026-06-01.qcow2")
        self.new = os.path.join(self.dir, "pulp-windows-build-24h2-arm64-2026-06-12-cacheopt.qcow2")
        for p in (self.old, self.new):
            with open(p, "w") as fh:
                fh.write("stub")
        now = time.time()
        os.utime(self.old, (now - 1000, now - 1000))
        os.utime(self.new, (now, now))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_list_picks_newest_as_canonical(self):
        r = run(["list"], self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2026-06-12-cacheopt", r.stdout)
        self.assertIn("canonical", r.stdout)

    def test_list_flags_missing_sha(self):
        r = run(["list"], self.dir)
        # neither golden has a .sha256 sidecar → must warn (stderr)
        self.assertIn("sha256 sidecar MISSING", r.stdout + r.stderr)

    def test_list_reports_superseded_prune_candidate(self):
        r = run(["list"], self.dir)
        self.assertIn("superseded", (r.stdout + r.stderr))
        self.assertIn("2026-06-01", (r.stdout + r.stderr))

    def test_sha_present_is_reported(self):
        with open(self.new + ".sha256", "w") as fh:
            fh.write("deadbeef  " + os.path.basename(self.new) + "\n")
        r = run(["list"], self.dir)
        self.assertIn("sha256 sidecar present", r.stdout)

    def test_sync_requires_a_direction(self):
        r = run(["sync"], self.dir)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--to HOST", r.stdout + r.stderr)
        self.assertIn("--from HOST", r.stdout + r.stderr)

    def test_sync_to_and_from_are_mutually_exclusive(self):
        r = run(["sync", "--to", "a", "--from", "b"], self.dir)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("mutually exclusive", r.stdout + r.stderr)

    def test_unknown_os_is_rejected(self):
        r = run(["list", "--os", "macos"], self.dir)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("windows only", r.stdout + r.stderr)

    def test_unknown_subcommand_is_rejected(self):
        r = run(["frobnicate"], self.dir)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown goldens subcommand", r.stdout + r.stderr)

    def test_help_prints_usage(self):
        r = run([], self.dir)
        self.assertIn("tartci goldens", r.stdout + r.stderr)

    def test_empty_dir_is_graceful(self):
        empty = tempfile.mkdtemp(prefix="tartci-goldens-empty-")
        try:
            r = run(["list"], empty)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("no windows golden found", r.stdout + r.stderr)
        finally:
            import shutil
            shutil.rmtree(empty, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
