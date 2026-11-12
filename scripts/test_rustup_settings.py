#!/usr/bin/env python3
"""Tests for scripts/rustup_settings.py: a host's rustup never self-updates."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rustup_settings  # noqa: E402
import testing_support  # noqa: E402

# A stand-in rustup: `set auto-self-update <mode>` records <mode> where real
# rustup does, unless FAKE_RUSTUP_IGNORE is set (a rustup that accepts the
# command but records nothing).
FAKE = """#!/bin/sh
echo "$@" >> "$RUSTUP_HOME/calls"
if [ "$1 $2" = "set auto-self-update" ] && [ -z "${FAKE_RUSTUP_IGNORE:-}" ]; then
  printf 'version = "12"\\nauto_self_update = "%s"\\n' "$3" > "$RUSTUP_HOME/settings.toml"
fi
"""


class RustupSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        self.rustup = self.root / ".cargo/bin/rustup"
        self.rustup.parent.mkdir(parents=True)
        self.rustup.write_text(FAKE, encoding="utf-8")
        self.rustup.chmod(0o755)
        self.rustup_home = self.root / ".rustup"
        self.rustup_home.mkdir()
        self.env = dict(os.environ, RUSTUP_HOME=str(self.rustup_home))
        self.table = {"path": str(self.rustup), "auto_self_update": "disable"}

    def test_apply_disables_self_update_and_reads_it_back(self) -> None:
        self.assertIsNone(rustup_settings.apply(self.table, self.env, self.root))
        self.assertEqual("disable", rustup_settings.recorded_mode(self.rustup_home / "settings.toml"))
        self.assertIn("set auto-self-update disable", (self.rustup_home / "calls").read_text(encoding="utf-8"))

    def test_a_rustup_that_records_nothing_is_refused(self) -> None:
        # Control on the same instrument: the command "succeeds" but nothing changed.
        env = dict(self.env, FAKE_RUSTUP_IGNORE="1")
        problem = rustup_settings.apply(self.table, env, self.root)
        self.assertIn("records auto_self_update = None, expected 'disable'", problem)

    def test_a_missing_rustup_names_the_fix(self) -> None:
        table = dict(self.table, path=str(self.root / "none/rustup"))
        self.assertIn("is not installed at", rustup_settings.apply(table, self.env, self.root))

    def test_schema(self) -> None:
        self.assertEqual([], rustup_settings.table_errors(self.table))
        self.assertTrue(rustup_settings.table_errors(dict(self.table, auto_self_update="enable")))
        self.assertTrue(rustup_settings.table_errors(dict(self.table, path="rustup")))
        self.assertTrue(rustup_settings.table_errors({"path": str(self.rustup)}))

    @testing_support.requires_tomllib
    def test_cli(self) -> None:
        profile = self.root / "profile.toml"
        profile.write_text('schema = 1\n', encoding="utf-8")
        self.assertEqual(0, rustup_settings.main(["apply", str(profile)]))
        self.assertFalse((self.rustup_home / "calls").exists())  # no table: host untouched
        profile.write_text(f'schema = 1\n[rustup]\npath = "{self.rustup}"\nauto_self_update = "disable"\n',
                           encoding="utf-8")
        old = os.environ.get("RUSTUP_HOME")
        os.environ["RUSTUP_HOME"] = str(self.rustup_home)
        try:
            self.assertEqual(0, rustup_settings.main(["apply", str(profile)]))
        finally:
            if old is None:
                os.environ.pop("RUSTUP_HOME")
            else:
                os.environ["RUSTUP_HOME"] = old
        self.assertEqual("disable", rustup_settings.recorded_mode(self.rustup_home / "settings.toml"))

    def test_the_installer_applies_it_before_writing_agents(self) -> None:
        source = (Path(__file__).resolve().parent / "install_macos_fleet.sh").read_text(encoding="utf-8")
        apply = source.index('"$ROOT/scripts/rustup_settings.py" apply "$CONFIG"')
        self.assertLess(apply, source.index("replacements=()"))


if __name__ == "__main__":
    unittest.main()
