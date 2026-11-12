"""The pinned EFI variable store a Windows golden can ship beside it."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HELPER = Path(__file__).with_name("windows_efi_vars.py")
PROVIDERS = HELPER.parent.parent / "providers/qemu-windows"


class WindowsEfiVarsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.template = self.root / "golden-efi-vars.fd"
        self.template.write_bytes(b"UEFI variable store fixture" * 64)
        self.digest = hashlib.sha256(self.template.read_bytes()).hexdigest()

    def run_helper(self, *args):
        return subprocess.run(
            [sys.executable, str(HELPER), "--template", str(self.template), *args],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )

    def test_copy_matches_the_pin_and_leaves_the_template_untouched(self):
        dest = self.root / "job" / "efivars.fd"
        dest.parent.mkdir()
        result = self.run_helper("--sha256", self.digest, "--dest", str(dest))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dest.read_bytes(), self.template.read_bytes())
        # QEMU writes the copy; writing it must not reach the template.
        dest.write_bytes(b"guest wrote this")
        self.assertEqual(hashlib.sha256(self.template.read_bytes()).hexdigest(), self.digest)

    def test_mismatched_template_refuses_and_leaves_no_copy(self):
        dest = self.root / "efivars.fd"
        result = self.run_helper("--sha256", "0" * 64, "--dest", str(dest))
        self.assertEqual(result.returncode, 1)
        self.assertIn("SHA-256 mismatch", result.stderr)
        self.assertFalse(dest.exists())

    def test_check_mode_verifies_without_copying(self):
        self.assertEqual(self.run_helper("--sha256", self.digest, "--check").returncode, 0)
        failed = self.run_helper("--sha256", "f" * 64, "--check")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("mismatch", failed.stderr)

    def test_linked_template_and_existing_destination_refuse(self):
        link = self.root / "link.fd"
        link.symlink_to(self.template)
        linked = subprocess.run(
            [sys.executable, str(HELPER), "--template", str(link), "--sha256", self.digest, "--check"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(linked.returncode, 1)
        self.assertIn("regular file", linked.stderr)
        existing = self.root / "existing.fd"
        existing.write_bytes(b"keep me")
        result = self.run_helper("--sha256", self.digest, "--dest", str(existing))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(existing.read_bytes(), b"keep me")

    def test_a_failed_copy_removes_the_partial_destination(self):
        # The template becomes unreadable after the startup check: the copy
        # fails part-way and must not leave a file QEMU could boot from.
        dest = self.root / "efivars.fd"
        self.template.chmod(0)
        self.addCleanup(self.template.chmod, 0o644)
        if os.access(self.template, os.R_OK):
            self.skipTest("running as a user who can read mode-0 files")
        result = self.run_helper("--sha256", self.digest, "--dest", str(dest))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(dest.exists())

    def test_malformed_pin_is_an_argument_error(self):
        self.assertEqual(self.run_helper("--sha256", "abc", "--check").returncode, 2)

    def test_both_windows_providers_copy_through_the_pinned_helper(self):
        for name in ("runner.sh", "run.sh"):
            text = (PROVIDERS / name).read_text(encoding="utf-8")
            with self.subTest(provider=name):
                self.assertIn('VARS_TPL="${TARTCI_WIN_VARS_TEMPLATE:-}"', text)
                self.assertIn("TARTCI_WIN_VARS_TEMPLATE requires TARTCI_WIN_VARS_SHA256", text)
                self.assertIn("scripts/windows_efi_vars.py", text)
                self.assertIn("copy_efi_vars ", text)
                # No job path copies the template without going through the helper.
                self.assertNotIn('cp "$VARS_TPL" "$efivars"', text)
                self.assertNotIn('cp "$VARS_TPL" "$EFIVARS"', text)


if __name__ == "__main__":
    unittest.main()
