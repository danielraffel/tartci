#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import tartci_support_manifest as support_manifest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/tartci_support_manifest.py"


def thaw_directories(root: Path) -> None:
    if not root.exists():
        return
    for directory in [root, *root.rglob("*")]:
        if directory.is_dir() and not directory.is_symlink():
            directory.chmod(0o755)


class TartciSupportManifestTests(unittest.TestCase):
    def fixture(self, root: Path) -> None:
        for relative, body, mode in (
            ("tartci", "#!/bin/sh\n", 0o755),
            ("providers/tart-macos/runner.sh", "#!/bin/sh\n", 0o755),
            ("scripts/current_job_scan.py", "print('ok')\n", 0o755),
            ("profiles/example.toml", "schema=1\n", 0o644),
            ("launchd/example.plist.template", "plist\n", 0o644),
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            path.chmod(mode)

    def manifest(self, root: Path, path: Path) -> None:
        members = []
        for relative in (
            "launchd/example.plist.template",
            "profiles/example.toml",
            "providers/tart-macos/runner.sh",
            "scripts/current_job_scan.py",
            "tartci",
        ):
            target = root / relative
            members.append({
                "path": relative,
                "mode": target.stat().st_mode & 0o777,
                "sha256": subprocess.check_output(
                    ["shasum", "-a", "256", str(target)], text=True
                ).split()[0],
            })
        path.write_text(json.dumps({
            "schema": 2,
            "repository": "https://github.com/danielraffel/tartci.git",
            "source_commit": "a" * 40,
            "members": members,
        }))

    def verify(self, root: Path, manifest: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(SCRIPT), "verify", str(manifest), "--root", str(root)],
            text=True, capture_output=True, check=False,
        )

    def git_commit(self, root: Path) -> str:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        subprocess.run([
            "git", "-C", str(root), "remote", "add", "origin",
            "git@github.com:danielraffel/tartci.git",
        ], check=True)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()

    def test_exact_cohort_verifies_and_ignores_non_runtime_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            manifest = root / "manifest.json"
            self.manifest(root, manifest)
            (root / "scripts/__pycache__").mkdir()
            (root / "scripts/__pycache__/cached.pyc").write_bytes(b"cache")
            (root / "scripts/test_fixture.py").write_text("ignored\n")
            result = self.verify(root, manifest)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_required_helper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            manifest = root / "manifest.json"
            self.manifest(root, manifest)
            (root / "scripts/current_job_scan.py").unlink()
            result = self.verify(root, manifest)
            self.assertEqual(result.returncode, 2)
            self.assertIn("missing=['scripts/current_job_scan.py']", result.stderr)

    def test_altered_mode_digest_extra_and_symlink_each_fail_closed(self) -> None:
        mutations = ("mode", "digest", "extra", "symlink")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                self.fixture(root)
                manifest = root / "manifest.json"
                self.manifest(root, manifest)
                target = root / "scripts/current_job_scan.py"
                if mutation == "mode":
                    target.chmod(0o644)
                elif mutation == "digest":
                    target.write_text("changed\n")
                elif mutation == "extra":
                    (root / "scripts/new_runtime.py").write_text("new\n")
                else:
                    target.unlink()
                    os.symlink("../profiles/example.toml", target)
                result = self.verify(root, manifest)
                self.assertEqual(result.returncode, 2, result.stdout)

    def test_stage_install_is_immutable_reusable_and_refuses_dirty_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            source.mkdir()
            self.fixture(source)
            self.git_commit(source)
            manifest = source / support_manifest.MANIFEST_NAME
            support_manifest.write(source, manifest)
            member_paths = {
                member["path"]
                for member in json.loads(manifest.read_text())["members"]
            }
            self.assertIn("providers/tart-macos/runner.sh", member_paths)
            self.assertIn("scripts/current_job_scan.py", member_paths)
            generations = root / "generations"
            first = support_manifest.stage_install(source, manifest, generations)
            self.assertTrue(first["created"])
            installed = Path(str(first["root"]))
            self.assertEqual(
                support_manifest.verify(
                    installed,
                    installed / support_manifest.MANIFEST_NAME,
                    immutable=True,
                )["source_commit"],
                subprocess.check_output(
                    ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
                ).strip(),
            )
            self.assertEqual(
                0o555, (installed / "tartci").stat().st_mode & 0o777
            )
            self.assertEqual(
                0o444,
                (installed / support_manifest.MANIFEST_NAME).stat().st_mode & 0o777,
            )
            second = support_manifest.stage_install(source, manifest, generations)
            self.assertFalse(second["created"])
            self.assertEqual(first["root"], second["root"])
            (source / "scripts/current_job_scan.py").write_text("dirty\n")
            with self.assertRaisesRegex(ValueError, "source commit|failed verification"):
                support_manifest.stage_install(source, manifest, generations)
            (installed / "tartci").chmod(0o755)
            with self.assertRaisesRegex(ValueError, "failed verification"):
                support_manifest.verify(
                    installed,
                    installed / support_manifest.MANIFEST_NAME,
                    immutable=True,
                )
            (installed / "tartci").chmod(0o555)
            installed.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "directory.*0555"):
                support_manifest.verify(
                    installed,
                    installed / support_manifest.MANIFEST_NAME,
                    immutable=True,
                )
            thaw_directories(generations)

    def staged_source(self, root: Path) -> tuple[Path, Path]:
        source = root / "source"
        source.mkdir()
        self.fixture(source)
        self.git_commit(source)
        manifest = source / support_manifest.MANIFEST_NAME
        support_manifest.write(source, manifest)
        return source, manifest

    def test_the_generation_is_renamed_while_writable_then_sealed(self) -> None:
        # macOS 15 refuses to rename a directory whose own mode is 0555
        # (EACCES); macOS 27 allows it. Recording the mode at the rename makes
        # the order checkable on any host.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, manifest = self.staged_source(root)
            generations = root / "generations"
            modes = []
            real_rename = os.rename

            def rename(src, dst):
                modes.append(stat.S_IMODE(os.lstat(src).st_mode))
                return real_rename(src, dst)

            with mock.patch.object(support_manifest.os, "rename", side_effect=rename):
                result = support_manifest.stage_install(source, manifest, generations)
            self.assertEqual(len(modes), 1)
            self.assertTrue(modes[0] & stat.S_IWUSR, oct(modes[0]))
            installed = Path(str(result["root"]))
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o555)
            if os.geteuid() != 0:
                with self.assertRaises(PermissionError):
                    (installed / "added-after-seal").write_text("x")
            thaw_directories(generations)

    def test_install_succeeds_where_a_sealed_directory_cannot_be_renamed(self) -> None:
        # macOS 15's rule, on any host: renaming a directory without its own
        # write bit fails with EACCES.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, manifest = self.staged_source(root)
            generations = root / "generations"
            real_rename = os.rename

            def rename(src, dst):
                if os.path.isdir(src) and not os.lstat(src).st_mode & stat.S_IWUSR:
                    raise PermissionError(13, "Permission denied", str(src))
                return real_rename(src, dst)

            with mock.patch.object(support_manifest.os, "rename", side_effect=rename):
                result = support_manifest.stage_install(source, manifest, generations)
            self.assertTrue(result["created"])
            installed = Path(str(result["root"]))
            support_manifest.verify(installed, installed / support_manifest.MANIFEST_NAME,
                                    immutable=True)
            thaw_directories(generations)

    def test_a_generation_that_fails_its_seal_is_not_left_under_its_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, manifest = self.staged_source(root)
            generations = root / "generations"
            real_verify = support_manifest.verify

            def verify(path, manifest_path, *, immutable=False):
                if immutable:
                    raise ValueError("seal check failed")
                return real_verify(path, manifest_path, immutable=immutable)

            with mock.patch.object(support_manifest, "verify", side_effect=verify), \
                    self.assertRaisesRegex(ValueError, "seal check failed"):
                support_manifest.stage_install(source, manifest, generations)
            self.assertEqual(sorted(p.name for p in generations.iterdir()), [])
            # Control: without the failure the same call installs one generation.
            support_manifest.stage_install(source, manifest, generations)
            self.assertEqual(len(list(generations.iterdir())), 1)
            thaw_directories(generations)

    def test_stage_install_rejects_source_without_repository_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            self.git_commit(root)
            subprocess.run(
                ["git", "-C", str(root), "remote", "remove", "origin"], check=True
            )
            with self.assertRaisesRegex(ValueError, "exact GitHub repository"):
                support_manifest.build(root)

    def test_stage_install_rejects_untrusted_github_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            self.git_commit(root)
            subprocess.run([
                "git", "-C", str(root), "remote", "set-url", "origin",
                "https://github.com/example/tartci.git",
            ], check=True)
            with self.assertRaisesRegex(ValueError, "repository is not trusted"):
                support_manifest.build(root)

    def test_wrapper_atomically_selects_one_exact_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            support_root = root / "generation with space"
            support_root.mkdir()
            (support_root / "tartci").write_text("#!/bin/bash\n")
            entrypoint = root / "bin/tartci"
            support_manifest.write_wrapper(entrypoint, support_root)
            record = support_manifest.wrapper_record(entrypoint, support_root)
            self.assertEqual(str(support_root.resolve()), record["support_root"])
            self.assertIn("'", entrypoint.read_text())
            entrypoint.write_text("#!/bin/bash\nexit 1\n")
            entrypoint.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "does not select"):
                support_manifest.wrapper_record(entrypoint, support_root)

    def test_wrapper_continuous_reader_observes_only_complete_generations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "generation-a"
            second = root / "generation-b"
            first.mkdir()
            second.mkdir()
            (first / "tartci").write_text("a\n")
            (second / "tartci").write_text("b\n")
            entrypoint = root / "bin/tartci"
            support_manifest.write_wrapper(entrypoint, first)
            allowed = {
                support_manifest.canonical_wrapper_bytes(first),
                support_manifest.canonical_wrapper_bytes(second),
            }
            stop = threading.Event()
            failures: list[str] = []

            def read_continuously() -> None:
                while not stop.is_set():
                    try:
                        value = entrypoint.read_bytes()
                    except OSError as exc:
                        failures.append(f"unavailable: {exc}")
                        return
                    if value not in allowed:
                        failures.append(f"mixed: {value!r}")
                        return

            reader = threading.Thread(target=read_continuously)
            reader.start()
            try:
                for index in range(100):
                    support_manifest.write_wrapper(
                        entrypoint, first if index % 2 == 0 else second
                    )
            finally:
                stop.set()
                reader.join()
            self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
