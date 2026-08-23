#!/usr/bin/env python3
"""Regression tests for host cache mounts and ccache correctness policy."""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINUX_DIRECT = ROOT / "providers/tart-linux/run.sh"
LINUX_JIT = ROOT / "providers/tart-linux/runner.sh"
MAC_DIRECT = ROOT / "providers/tart-macos/run.sh"
MAC_JIT = ROOT / "providers/tart-macos/runner.sh"


class CacheRoutingTests(unittest.TestCase):
    def test_provider_shell_is_syntactically_valid(self) -> None:
        for path in (LINUX_DIRECT, LINUX_JIT, MAC_DIRECT, MAC_JIT):
            result = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_live_provider_enables_ccache_depend_mode(self) -> None:
        for path in (LINUX_DIRECT, LINUX_JIT, MAC_DIRECT, MAC_JIT):
            body = path.read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"^(?:export )?CCACHE_DEPEND=true$", body, re.MULTILINE),
                path,
            )
            self.assertIn("CCACHE_NODEPEND=true", body, path)
            self.assertIn("CCACHE_COMPILERCHECK=content", body, path)

    def test_macos_direct_and_jit_mount_canonical_fetchcontent(self) -> None:
        canonical = "$HOME/Library/Caches/Pulp/fetchcontent-src"
        direct = MAC_DIRECT.read_text(encoding="utf-8")
        jit = MAC_JIT.read_text(encoding="utf-8")
        for body in (direct, jit):
            self.assertIn(canonical, body)
            self.assertIn("PULP_SHARED_FETCHCONTENT_SOURCE_DIR", body)
            self.assertIn('--dir="fetchcontent:$FETCHCONTENT_SOURCE_ROOT:ro"', body)
            self.assertNotIn("export FETCHCONTENT_BASE_DIR", body)

    def test_jit_guest_uses_isolated_copy_of_read_only_host_seed(self) -> None:
        body = MAC_JIT.read_text(encoding="utf-8")
        self.assertIn(
            "rsync -a '/Volumes/My Shared Files/fetchcontent/'",
            body,
        )
        self.assertIn(
            'PULP_SHARED_FETCHCONTENT_SOURCE_DIR=\\"\\$HOME/Library/Caches/Pulp/fetchcontent-src\\"',
            body,
        )
        self.assertNotIn("rm -rf ~/Library/Caches/Pulp/fetchcontent-src", body)

    def test_jit_runners_override_preserved_env_after_policy_sanitization(self) -> None:
        linux = LINUX_JIT.read_text(encoding="utf-8")
        mac = MAC_JIT.read_text(encoding="utf-8")
        for body in (linux, mac):
            self.assertIn("CCACHE_DEPEND|CCACHE_NODEPEND|CCACHE_COMPILERCHECK", body)
            self.assertIn("CCACHE_NODEPEND=true", body)
            self.assertIn("CCACHE_COMPILERCHECK=content", body)
            self.assertIn("mv .env.tartci .env", body)
        self.assertIn("PULP_SHARED_FETCHCONTENT_SOURCE_DIR|FETCHCONTENT_BASE_DIR", mac)
        self.assertIn(
            "PULP_SHARED_FETCHCONTENT_SOURCE_DIR=%s",
            mac,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
