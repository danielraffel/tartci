"""Fail-closed Windows golden receipt and fast-cache controls."""

import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest


VERIFY = Path(__file__).with_name("verify_windows_golden.py")
SNAPSHOT = VERIFY.parent.parent / "providers/qemu-windows/prepare-golden-snapshot.sh"
RUNNER = VERIFY.parent.parent / "providers/qemu-windows/runner.sh"
RUN = VERIFY.parent.parent / "providers/qemu-windows/run.sh"


class WindowsGoldenAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "base.qcow2"
        self.cache = self.root / "cache"
        self.image.write_bytes(b"small qcow2 fixture bytes")

    def sidecar(self, digest=None):
        if digest is None:
            digest = hashlib.sha256(self.image.read_bytes()).hexdigest()
        Path(f"{self.image}.sha256").write_text(f"{digest}  /old/location/base.qcow2\n")

    def run_verify(self):
        return subprocess.run(
            [sys.executable, str(VERIFY), str(self.image), "--cache-dir", str(self.cache)],
            capture_output=True, text=True, check=False,
        )

    def test_missing_and_mismatched_sidecar_refuse(self):
        self.assertNotEqual(self.run_verify().returncode, 0)
        self.sidecar("0" * 64)
        failed = self.run_verify()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("SHA-256 mismatch", failed.stderr)
        self.assertEqual(list(self.cache.glob("*.json")), [])

    def test_verified_image_reuses_cache_until_bytes_change(self):
        self.sidecar()
        first = self.run_verify()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("(hashed)", first.stdout)
        second = self.run_verify()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("(cached)", second.stdout)

        original_mtime = self.image.stat().st_mtime_ns
        time.sleep(0.02)
        self.image.write_bytes(b"changed qcow2 fixture byt")  # same byte count
        os.utime(self.image, ns=(original_mtime, original_mtime))
        changed = self.run_verify()
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("SHA-256 mismatch", changed.stderr)

    def test_sidecar_change_invalidates_cache(self):
        self.sidecar()
        self.assertEqual(self.run_verify().returncode, 0)
        self.sidecar("f" * 64)
        changed = self.run_verify()
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("SHA-256 mismatch", changed.stderr)

    def test_symlink_image_refuses(self):
        target = self.root / "target.qcow2"
        self.image.rename(target)
        self.image.symlink_to(target)
        self.sidecar(hashlib.sha256(target.read_bytes()).hexdigest())
        failed = self.run_verify()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("symlink", failed.stderr)

    def test_cache_record_symlink_is_not_trusted(self):
        self.sidecar()
        self.assertEqual(self.run_verify().returncode, 0)
        record = next(self.cache.glob("*.json"))
        outside = self.root / "outside.json"
        record.rename(outside)
        record.symlink_to(outside)
        result = self.run_verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(hashed)", result.stdout)
        self.assertFalse(record.is_symlink())

    def test_windows_providers_resolve_pinned_python_for_golden_admission(self):
        runner = RUNNER.read_text()
        run = RUN.read_text()
        snapshot = SNAPSHOT.read_text()
        for script in (runner, run, snapshot):
            self.assertIn("toml-python.lib.sh", script)
            self.assertIn('"$TARTCI_PYTHON" "$', script)
            self.assertNotIn("nice -n 20 python3", script)

    @unittest.skipIf(sys.version_info < (3, 11), "the pinned interpreter must be 3.11+ (tomllib)")
    @unittest.skipUnless(sys.platform == "darwin", "APFS CoW snapshot needs macOS")
    def test_snapshot_uses_explicit_python_when_path_python3_is_shadowed(self):
        self.sidecar()
        snapshot = self.root / "snapshot.qcow2"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        log = self.root / "python.log"
        wrapper = bin_dir / "pinned-python"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        (bin_dir / "python3").write_text("#!/bin/sh\necho shadowed-python >&2\nexit 97\n", encoding="utf-8")
        (bin_dir / "python3").chmod(0o755)
        env = os.environ.copy()
        env.update({"PATH": f"{bin_dir}:/usr/bin:/bin", "TARTCI_PYTHON": str(wrapper)})

        result = subprocess.run(
            [str(SNAPSHOT), str(self.image), str(snapshot)],
            env=env, capture_output=True, text=True, check=False,
        )
        self.addCleanup(lambda: subprocess.run(
            ["chflags", "nouchg", str(snapshot), f"{snapshot}.sha256"],
            capture_output=True, check=False,
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verify_windows_golden.py", log.read_text(encoding="utf-8"))
        self.assertNotIn("shadowed-python", result.stderr)
        # The private snapshot and its sidecar are sealed before QEMU reads them.
        import stat  # noqa: PLC0415
        for path in (snapshot, Path(f"{snapshot}.sha256")):
            with self.subTest(sealed=path.name):
                self.assertTrue(path.stat().st_flags & stat.UF_IMMUTABLE)

    def test_print_host_health_answers_on_an_unpinned_launchd_host(self):
        # The print-exits need no interpreter: a launchd host with no pin must
        # still answer the health probe (exit 0) rather than die on the pin.
        stubs = self.root / "stubs"
        stubs.mkdir()
        for name in ("qemu-system-aarch64", "gh"):
            (stubs / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (stubs / name).chmod(0o755)
        home = self.root / "home"
        home.mkdir()
        env = {"HOME": str(home), "PATH": f"{stubs}:/usr/bin:/bin", "TARTCI_LAUNCHD": "1",
               "TARTCI_HOME": str(home / ".tartci")}
        health = subprocess.run([str(RUNNER), "--print-host-health"], env=env,
                                capture_output=True, text=True, check=False, timeout=120)
        self.assertEqual(health.returncode, 0, health.stderr)
        self.assertNotIn("no pinned Python", health.stderr)
        # Control, same host: going past the print-exits still refuses the
        # unpinned interpreter.
        past = subprocess.run([str(RUNNER), "--once"], env=env,
                              capture_output=True, text=True, check=False, timeout=120)
        self.assertNotEqual(past.returncode, 0)
        self.assertIn("refusing an unpinned interpreter", past.stderr)

    @unittest.skipUnless(sys.platform == "darwin", "APFS CoW snapshot needs macOS")
    def test_private_snapshot_survives_source_swap_and_refuses_bad_receipt(self):
        self.sidecar()
        snapshot = self.root / "snapshot.qcow2"
        original = self.image.read_bytes()
        result = subprocess.run([str(SNAPSHOT), str(self.image), str(snapshot)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: subprocess.run(
            ["chflags", "nouchg", str(snapshot), f"{snapshot}.sha256"],
            capture_output=True, check=False))
        self.image.write_bytes(b"mutated source after verified snapshot")
        self.assertEqual(snapshot.read_bytes(), original)
        checked = subprocess.run(
            [sys.executable, str(VERIFY), "--no-cache", str(snapshot)],
            capture_output=True, text=True, check=False)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        bad = self.root / "bad.qcow2"
        bad.write_bytes(b"other bytes")
        Path(f"{bad}.sha256").write_text("0" * 64 + "  old.qcow2\n")
        refused = subprocess.run([str(SNAPSHOT), str(bad), str(self.root / "refused.qcow2")],
                                 capture_output=True, text=True, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.root / "refused.qcow2").exists())


if __name__ == "__main__":
    unittest.main()
