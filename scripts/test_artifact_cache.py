#!/usr/bin/env python3
"""Tests for the optional read-only artifact cache served to macOS guests."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers/tart-macos/artifact-cache.lib.sh"
SYNC = ROOT / "scripts/artifact-cache.sh"
MAC_JIT = ROOT / "providers/tart-macos/runner.sh"
WARM = ROOT / "providers/tart-macos/warm-vm.lib.sh"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()


class ReadyTests(unittest.TestCase):
    def _ready(self, path: str) -> bool:
        proc = subprocess.run(
            ["bash", "-c", f'source "{LIB}"; artifact_cache_ready "$1"', "_", path],
            capture_output=True, text=True, check=False,
        )
        return proc.returncode == 0

    def test_a_blob_or_a_mirror_makes_it_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sha256").mkdir()
            (Path(tmp) / "sha256" / ("0" * 64)).write_bytes(b"x")
            self.assertTrue(self._ready(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "git/owner/repo.git").mkdir(parents=True)
            self.assertTrue(self._ready(tmp))

    def test_absent_empty_or_staging_only_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self._ready(tmp))
            self.assertFalse(self._ready(str(Path(tmp) / "missing")))
            (Path(tmp) / "sha256").mkdir()
            (Path(tmp) / "sha256/.staging.abc").write_bytes(b"partial")
            (Path(tmp) / "git/owner").mkdir(parents=True)
            self.assertFalse(self._ready(tmp))

    def test_unshareable_paths_are_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            colon = Path(tmp) / "a:b"
            (colon / "sha256").mkdir(parents=True)
            (colon / "sha256" / ("0" * 64)).write_bytes(b"x")
            self.assertFalse(self._ready(str(colon)))
        self.assertFalse(self._ready("relative/cache"))
        self.assertFalse(self._ready(""))


class AddTests(unittest.TestCase):
    """`add` stores verified bytes under their digest, through a stub curl."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "curl.calls"
        curl = self.bin / "curl"
        curl.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            echo "$*" >> {self.calls}
            out=""
            while [ "$#" -gt 0 ]; do [ "$1" = "--output" ] && out="$2"; shift; done
            [ -n "${{STUB_FAIL:-}}" ] && exit 22
            printf '%s' "${{STUB_BYTES:-payload}}" > "$out"
            """))
        curl.chmod(0o755)
        self.cache = self.tmp / "cache"
        self.sha = hashlib.sha256(b"payload").hexdigest()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, sha: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SYNC), "add", "--url", "https://example.invalid/a.zip",
             "--sha256", sha, "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", **env},
        )

    def test_verified_bytes_land_under_their_digest(self) -> None:
        proc = self._add(self.sha)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.cache / "sha256" / self.sha).read_bytes(), b"payload")
        self.assertEqual([p.name for p in (self.cache / "sha256").iterdir()], [self.sha])
        self.assertFalse((self.cache / ".lock").exists())

    def test_mismatched_bytes_never_land(self) -> None:
        proc = self._add(self.sha, STUB_BYTES="tampered")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("SHA-256 mismatch", proc.stderr)
        self.assertEqual(list((self.cache / "sha256").iterdir()), [])

    def test_failed_download_leaves_nothing(self) -> None:
        proc = self._add(self.sha, STUB_FAIL="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(list((self.cache / "sha256").iterdir()), [])

    def test_readd_verifies_without_downloading(self) -> None:
        self.assertEqual(self._add(self.sha).returncode, 0)
        self.calls.unlink()
        proc = self._add(self.sha)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already present", proc.stdout)
        self.assertFalse(self.calls.exists())

    def test_a_corrupted_blob_is_reported_not_trusted(self) -> None:
        self.assertEqual(self._add(self.sha).returncode, 0)
        (self.cache / "sha256" / self.sha).write_bytes(b"rot")
        proc = self._add(self.sha)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not hash to its name", proc.stderr)

    def test_rejects_bad_digest_and_plain_http(self) -> None:
        self.assertNotEqual(self._add("ABC").returncode, 0)
        proc = subprocess.run(
            ["bash", str(SYNC), "add", "--url", "http://example.invalid/a",
             "--sha256", self.sha, "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("must be https", proc.stderr)

    def test_prune_removes_only_stale_blobs(self) -> None:
        self.assertEqual(self._add(self.sha).returncode, 0)
        old = self.cache / "sha256" / ("1" * 64)
        old.write_bytes(b"old")
        os.utime(old, (0, 0))
        proc = subprocess.run(
            ["bash", str(SYNC), "prune", "--older-than-days", "7", "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(old.exists())
        self.assertTrue((self.cache / "sha256" / self.sha).exists())


class GitSyncTests(unittest.TestCase):
    """`git-sync` mirrors one branch of a local origin and serves as an alternate."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.base = self.tmp / "remote"
        self.origin = self.base / "owner/repo.git"
        self.origin.mkdir(parents=True)
        _git("init", "--quiet", "--bare", "-b", "main", str(self.origin))
        self.work = self.tmp / "work"
        _git("clone", "--quiet", str(self.origin), str(self.work))
        self._commit("one")
        self.cache = self.tmp / "cache"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _commit(self, name: str) -> str:
        (self.work / name).write_text(name)
        _git("add", name, cwd=self.work)
        _git("commit", "--quiet", "-m", name, cwd=self.work)
        _git("push", "--quiet", "origin", "HEAD:main", cwd=self.work)
        return _git("rev-parse", "HEAD", cwd=self.work)

    def _sync(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SYNC), "git-sync", "--repo", "owner/repo", "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "TARTCI_ARTIFACT_CACHE_GIT_BASE": str(self.base)},
        )

    def test_initial_sync_then_incremental_sync(self) -> None:
        proc = self._sync()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        mirror = self.cache / "git/owner/repo.git"
        first = _git("rev-parse", "refs/heads/main", cwd=mirror)
        self.assertEqual(_git("config", "gc.auto", cwd=mirror), "0")
        self.assertEqual(_git("config", "maintenance.auto", cwd=mirror), "false")
        second = self._commit("two")
        self.assertEqual(self._sync().returncode, 0)
        self.assertNotEqual(first, second)
        self.assertEqual(_git("rev-parse", "refs/heads/main", cwd=mirror), second)
        self.assertEqual(list((self.cache / "git/owner").glob(".staging*")), [])

    def _packs(self) -> list[Path]:
        return list((self.cache / "git/owner/repo.git/objects/pack").glob("*.pack"))

    def _compact(self, running: int) -> subprocess.CompletedProcess:
        tart = self.tmp / "tart"
        vms = ", ".join('{"Name": "v%d", "State": "running"}' % i for i in range(running))
        tart.write_text(f"#!/bin/bash\necho '[{vms}]'\n")
        tart.chmod(0o755)
        return subprocess.run(
            ["bash", str(SYNC), "compact", "--repo", "owner/repo", "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "TARTCI_TART_BIN": str(tart)},
        )

    def test_sync_never_deletes_a_pack(self) -> None:
        self.assertEqual(self._sync().returncode, 0)
        seen = set(self._packs())
        for n in range(18):
            self._commit(f"c{n}")
            self.assertEqual(self._sync().returncode, 0)
            now = set(self._packs())
            self.assertTrue(seen <= now, "a sync removed a pack a guest may be reading")
            seen = now
        self.assertEqual(len(seen), 19)

    def test_compact_refuses_while_a_vm_runs_and_folds_when_idle(self) -> None:
        self.assertEqual(self._sync().returncode, 0)
        for n in range(3):
            self._commit(f"c{n}")
            self.assertEqual(self._sync().returncode, 0)
        before = len(self._packs())
        proc = self._compact(running=1)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refusing to compact", proc.stderr)
        self.assertEqual(len(self._packs()), before)
        proc = self._compact(running=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self._packs()), 1)
        self.assertEqual(_git("fsck", "--connectivity-only", "--no-dangling",
                              cwd=self.cache / "git/owner/repo.git"), "")

    def test_a_clone_using_the_mirror_as_alternate_needs_only_new_objects(self) -> None:
        self.assertEqual(self._sync().returncode, 0)
        head = self._commit("newer")
        guest = self.tmp / "guest"
        _git("init", "--quiet", str(guest))
        (guest / ".git/objects/info/alternates").write_text(
            str(self.cache / "git/owner/repo.git/objects") + "\n")
        _git("fetch", "--quiet", "--no-tags", str(self.origin), "main", cwd=guest)
        self.assertEqual(_git("rev-parse", "FETCH_HEAD", cwd=guest), head)
        # The control: the guest's own store holds only what the mirror lacked.
        local = int(_git("count-objects", "-v", cwd=guest).split("in-pack: ")[1].split()[0] or 0) \
            + int(_git("count-objects", cwd=guest).split()[0])
        self.assertGreater(local, 0)
        self.assertLessEqual(local, 3)

    def test_rejects_a_malformed_repo(self) -> None:
        for bad in ("owner", "owner/repo/extra", "../x/y"):
            proc = subprocess.run(
                ["bash", str(SYNC), "git-sync", "--repo", bad, "--dir", str(self.cache)],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(proc.returncode, 0, bad)


class RunnerWiringTests(unittest.TestCase):
    body = MAC_JIT.read_text(encoding="utf-8")

    def test_runner_sources_the_library(self) -> None:
        self.assertIn('source "$TARTCI_ROOT/providers/tart-macos/artifact-cache.lib.sh"', self.body)

    def test_mount_is_read_only_and_gated_on_a_ready_cache(self) -> None:
        self.assertIn('if artifact_cache_ready "$ARTIFACT_CACHE_ROOT"; then', self.body)
        self.assertIn('--dir="artifact-cache:$ARTIFACT_CACHE_ROOT:ro"', self.body)

    def test_job_env_declares_the_cache_only_when_mounted(self) -> None:
        self.assertIn("if [ '$CURRENT_ARTIFACT_CACHE' = 1 ]; then printf 'TARTCI_ARTIFACT_CACHE=%s", self.body)
        self.assertIn('GUEST_ARTIFACT_CACHE="/Volumes/My Shared Files/artifact-cache"', self.body)

    def test_preserved_env_cannot_forge_the_declaration(self) -> None:
        self.assertIn("|TARTCI_PIP_WHEELHOUSE|TARTCI_ARTIFACT_CACHE)$/", self.body)

    def test_a_warm_vm_keeps_its_declaration(self) -> None:
        warm = WARM.read_text(encoding="utf-8")
        self.assertIn('WARM_ARTIFACT="$CURRENT_ARTIFACT_CACHE"', warm)
        self.assertIn('CURRENT_ARTIFACT_CACHE="$WARM_ARTIFACT"', warm)


if __name__ == "__main__":
    unittest.main()
