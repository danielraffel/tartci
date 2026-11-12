#!/usr/bin/env python3
"""The public-path lint (scripts/public_path_lint.py)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import public_path_lint as ppl  # noqa: E402

REAL_HOME = "/Users/" + "danielraffel"     # split so this file does not trip the lint
REAL_VOLUME = "/Volumes/" + "Workshop"
REAL_HOST = "Daniels" + "-Mac-Studio"
OTHER_HOST = "Alexs" + "-MacBook-Pro"


class Findings(unittest.TestCase):
    def test_real_paths_and_host_names_are_found(self) -> None:
        text = (f"home = \"{REAL_HOME}/.config\"\nstore = \"{REAL_VOLUME}/VMs\"\n"
                f"host = \"{REAL_HOST}-m3\"\nlaptop = \"{OTHER_HOST}.local\"\n")
        self.assertEqual([n for n, _ in ppl.findings_in(text)], [1, 2, 3, 4])

    def test_host_names_are_found_in_any_case_and_form(self) -> None:
        # Built at run time so this file does not trip the lint itself.
        black = "Black" + "Book"
        for line in (f"{REAL_HOST.lower()}.local",          # mDNS name
                     f"ssh {REAL_HOST.upper()}-M3",
                     f"DNSName: {OTHER_HOST.lower()}-1.example-tailnet.ts.net.",
                     f"{black}-Pro-5", f"HostName: {black} Pro", f"M5/{black.lower()}"):
            with self.subTest(line=line):
                self.assertEqual(1, len(ppl.findings_in(line)), line)

    def test_ordinary_mac_words_pass(self) -> None:
        # Negative control: model names alone, and words that merely end in s.
        for line in ("a MacBook Pro or a Mac Studio", "the mac-studio lane", "macs-mini",
                     "laptop-host-1.example-tailnet.ts.net", "notebook pro"):
            with self.subTest(line=line):
                self.assertEqual([], ppl.findings_in(line), line)

    def test_placeholders_pass(self) -> None:
        # Negative control: the same lines written with placeholders are clean.
        text = ("home = \"/Users/example/.config\"\nstore = \"/Volumes/Example/VMs\"\n"
                "shared = \"/Users/Shared/x\"\nhost = \"studio-host-m3\"\n"
                "checkout = \"<checkout>\"\ncheck = path.startswith(\"/Volumes/\")\n"
                "label = \"com.danielraffel.tartci.reclaim\"\n")
        self.assertEqual(ppl.findings_in(text), [])


class Scan(unittest.TestCase):
    def repo(self, files: dict[str, str]) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(root)], check=False))
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        return root

    def test_a_planted_real_path_fails_and_its_placeholder_passes(self) -> None:
        bad = self.repo({"scripts/x.py": f"STORE = \"{REAL_VOLUME}/VMs\"\n"})
        self.assertEqual(ppl.main(["--root", str(bad)]), 1)
        good = self.repo({"scripts/x.py": "STORE = \"/Volumes/Example/VMs\"\n"})
        self.assertEqual(ppl.main(["--root", str(good)]), 0)

    def test_an_allowlisted_file_with_a_reason_is_skipped_and_only_that_file(self) -> None:
        root = self.repo({
            "profiles/m3.toml": f"tart_home = \"{REAL_VOLUME}/VMs\"\n",
            "scripts/y.py": f"HOME = \"{REAL_HOME}\"\n",
            ppl.ALLOWLIST: "profiles/m3.toml\thost profile; moves to the private source\n",
        })
        problems = ppl.scan(root, ppl.tracked(root))
        self.assertEqual([p.split(":")[0] for p in problems], ["scripts/y.py"])

    # Their host paths now come from the profile, so the lint covers them.
    DELISTED = ("scripts/macos_fleet_lanes.py", "scripts/worktree_cleanup.py",
                "providers/common/vm-lease.lib.sh", "providers/tart-linux/provision.sh",
                "providers/tart-linux/run.sh", "providers/tart-linux/runner.sh",
                "scripts/test_worktree_cleanup.py", "scripts/test_vm_lease_helper.py")

    def test_the_delisted_files_are_linted_again(self) -> None:
        root = Path(ppl.__file__).resolve().parent.parent
        allowed = ppl.allowlisted(root)
        for rel in self.DELISTED:
            with self.subTest(rel=rel):
                self.assertNotIn(rel, allowed)
                text = (root / rel).read_text()
                self.assertEqual([], ppl.findings_in(text), "the file itself is clean")
                # Positive control: one real path planted in the real file,
                # under the real allow-list, fails the lint.
                planted = self.repo({rel: text + f"\n# {REAL_VOLUME}/VMs\n",
                                     ppl.ALLOWLIST: (root / ppl.ALLOWLIST).read_text()})
                problems = ppl.scan(planted, ppl.tracked(planted))
                self.assertEqual([rel], sorted({p.split(":")[0] for p in problems}))
                self.assertEqual(1, ppl.main(["--root", str(planted)]))

    def test_this_repository_is_clean(self) -> None:
        root = Path(ppl.__file__).resolve().parent.parent
        if not (root / ".git").exists():
            self.skipTest("not a git checkout")
        self.assertEqual(ppl.scan(root, ppl.tracked(root)), [])

    def test_every_allowlist_entry_has_a_reason_and_exists(self) -> None:
        root = Path(ppl.__file__).resolve().parent.parent
        for path, reason in ppl.allowlisted(root).items():
            with self.subTest(path=path):
                self.assertTrue(reason, "an allow-list entry needs a reason")
                self.assertTrue((root / path).exists(), "stale allow-list entry")


if __name__ == "__main__":
    unittest.main()
