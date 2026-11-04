#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
DIGEST = ROOT / "scripts" / "cft_tree_digest.py"
APP = "Google Chrome for Testing.app"


def make_tree(root: Path) -> Path:
    exe = root / APP / "Contents" / "MacOS" / "Google Chrome for Testing"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return root


def digest(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(DIGEST), "digest", str(path)],
                          text=True, capture_output=True, check=False)


class CftMountTests(unittest.TestCase):
    def probe(self, d: Path, sha: str, repo: str = "Generous-Corp/vellum",
              version: str = "151.0.7922.47") -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "TARTCI_RUNNER_REPO": repo,
            "TARTCI_RUNNER_GROUP_ID": "8",
            "TARTCI_RUNNER_CFT_DIR": str(d),
            "TARTCI_RUNNER_CFT_VERSION": version,
            "TARTCI_RUNNER_CFT_SHA256": sha,
        })
        return subprocess.run(["/bin/bash", str(RUNNER), "--print-cft-mount"],
                              env=env, text=True, capture_output=True, check=False)

    def test_matching_digest_emits_one_read_only_mount(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = make_tree(Path(td))
            sha = digest(d).stdout.strip()
            r = self.probe(d, sha)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), f"chrome-for-testing:{d}:ro")

    def test_wrong_digest_is_not_mounted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = make_tree(Path(td))
            r = self.probe(d, "0" * 64)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")
            self.assertIn("failed digest verification", r.stderr)

    def test_tampered_content_after_pinning_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = make_tree(Path(td))
            sha = digest(d).stdout.strip()
            (d / APP / "Contents" / "MacOS" / "Google Chrome for Testing").write_text("evil\n")
            self.assertNotEqual(self.probe(d, sha).returncode, 0)

    def test_digest_is_rechecked_across_boot_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = make_tree(Path(td))
            sha = digest(d).stdout.strip()
            script = f"""
set -eu
export TARTCI_ROOT={ROOT!s}
export TARTCI_RUNNER_CFT_DIR={d!s}
export TARTCI_RUNNER_CFT_SHA256={sha}
source {ROOT / 'providers' / 'tart-macos' / 'cft-mount.lib.sh'!s}
boot_attempt() {{ verify_cft_mount; }}
boot_attempt
printf 'tampered\\n' > "$CFT_DIR/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
if boot_attempt; then
  echo 'tampered tree was accepted on the second boot' >&2
  exit 1
fi
"""
            r = subprocess.run(["/bin/bash", "-c", script], text=True,
                               capture_output=True, check=False)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_extra_top_level_entry_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = make_tree(Path(td))
            (d / "stray.txt").write_text("x")
            r = digest(d)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("exactly", r.stderr)

    def test_escaping_symlink_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = make_tree(Path(td))
            os.symlink("../../../..", d / APP / "Contents" / "escape")
            self.assertNotEqual(digest(d).returncode, 0)
            os.remove(d / APP / "Contents" / "escape")
            os.symlink("/etc", d / APP / "Contents" / "abs")
            self.assertNotEqual(digest(d).returncode, 0)

    def test_internal_relative_symlink_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = make_tree(Path(td))
            os.symlink("MacOS", d / APP / "Contents" / "Current")
            self.assertEqual(digest(d).returncode, 0)

    def test_non_vellum_repo_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = make_tree(Path(td))
            r = self.probe(d, digest(d).stdout.strip(), repo="Generous-Corp/pulp")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("restricted to Generous-Corp/vellum", r.stderr)

    def test_bad_version_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = make_tree(Path(td))
            r = self.probe(d, digest(d).stdout.strip(), version="latest")
            self.assertNotEqual(r.returncode, 0)

    def test_preflight_precedes_jit_mint_and_discards_on_failure(self) -> None:
        source = RUNNER.read_text()
        run_one = source[source.index("run_one(){"):]
        self.assertLess(run_one.index('install_and_preflight_cft "$ip"'),
                        run_one.index("generate-jitconfig"))
        self.assertIn('tart_dirs+=(--dir="$CFT_MOUNT_ARG")', source)
        self.assertIn("cft_preflight_failed", source)
        boot_start = source.index("boot_vm_to_ssh(){")
        run_one_start = source.index("run_one(){")
        boot = source[boot_start:run_one_start]
        self.assertLess(boot.index("verify_cft_mount"),
                        boot.index("tartci_vm_lease_guard_exec tart run"))
        cft = (ROOT / "providers" / "tart-macos" / "cft-mount.lib.sh").read_text()
        self.assertLess(cft.index("verify_cft_mount || return 1"), cft.index("ssh -n"))


if __name__ == "__main__":
    unittest.main()
