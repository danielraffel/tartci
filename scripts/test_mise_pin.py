#!/usr/bin/env python3
"""Tests for scripts/mise_pin.py: the pinned mise binary a profile names."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mise_pin  # noqa: E402
import testing_support  # noqa: E402

VERSION = "2026.10.8"


class MisePinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        self.binary = self.root / f"opt/mise-{VERSION}/bin/mise"
        self.binary.parent.mkdir(parents=True)
        self.write_binary(VERSION)
        self.profile = self.root / "profile.toml"

    def write_binary(self, reports: str) -> None:
        self.binary.write_text(f"#!/bin/sh\necho '{reports} macos-arm64 (2026-10-13)'\n", encoding="utf-8")
        self.binary.chmod(0o755)

    def digest(self) -> str:
        return hashlib.sha256(self.binary.read_bytes()).hexdigest()

    def table(self, **overrides) -> dict:
        return dict({"version": VERSION, "path": str(self.binary), "sha256": self.digest()}, **overrides)

    def write_profile(self, table: dict | None) -> None:
        body = 'schema = 1\n[host]\nid = "m1"\n'
        if table is not None:
            body += "[mise]\n" + "".join(f'{k} = "{v}"\n' for k, v in table.items())
        self.profile.write_text(body, encoding="utf-8")

    def test_the_pinned_binary_verifies_and_a_changed_one_does_not(self) -> None:
        table = self.table()
        self.assertIsNone(mise_pin.verify(table))
        # Control on the same table: one changed byte is a different binary.
        self.write_binary(VERSION + " ")
        problem = mise_pin.verify(table)
        self.assertIn(f"has sha256 {self.digest()}, but the profile pins {table['sha256']}", problem)
        self.assertIn("fix: copy the pinned binary", problem)

    def test_a_missing_binary_names_the_fix(self) -> None:
        table = self.table(path=str(self.root / f"none/mise-{VERSION}/bin/mise"))
        self.assertIn("is not installed at", mise_pin.verify(table))

    def test_the_version_probe_never_asks_for_a_newer_mise(self) -> None:
        # Reports the pinned version only when the update check is off.
        self.binary.write_text(
            '#!/bin/sh\n[ "$MISE_DISABLE_UPDATE_WARNING" = 1 ] && echo "%s macos-arm64" || echo "0.0.0"\n'
            % VERSION, encoding="utf-8")
        self.binary.chmod(0o755)
        self.assertIsNone(mise_pin.verify(self.table()))

    def test_the_binary_must_report_the_pinned_version(self) -> None:
        self.write_binary("2026.5.16")
        self.assertIn("--version reports 2026.5.16", mise_pin.verify(self.table()))

    def test_schema(self) -> None:
        self.assertEqual([], mise_pin.table_errors(self.table()))
        cases = (
            ({"version": "latest"}, "exact mise release"),
            ({"path": "opt/mise"}, "absolute path"),
            ({"path": "/opt/homebrew/bin/mise"}, "must contain mise.version"),
            ({"sha256": "ABC"}, "64-hex"),
        )
        for override, needle in cases:
            with self.subTest(override=override):
                self.assertIn(needle, "; ".join(mise_pin.table_errors(self.table(**override))))
        self.assertTrue(mise_pin.table_errors(dict(self.table(), extra="x")))

    @testing_support.requires_tomllib
    def test_check_and_write(self) -> None:
        pin = self.root / "config/mise.pin"
        self.write_profile(self.table())
        self.assertEqual(0, mise_pin.main(["check", str(self.profile)]))
        self.assertFalse(pin.exists())  # check never writes
        self.assertEqual(0, mise_pin.main(["write", str(self.profile), str(pin)]))
        record = json.loads(pin.read_text(encoding="utf-8"))
        self.assertEqual((VERSION, str(self.binary), self.digest()),
                         (record["version"], record["path"], record["sha256"]))
        self.assertIn("verified_at", record)

    @testing_support.requires_tomllib
    def test_a_refused_binary_writes_no_pin(self) -> None:
        pin = self.root / "config/mise.pin"
        self.write_profile(self.table(sha256="0" * 64))
        self.assertEqual(1, mise_pin.main(["check", str(self.profile)]))
        self.assertEqual(1, mise_pin.main(["write", str(self.profile), str(pin)]))
        self.assertFalse(pin.exists())

    @testing_support.requires_tomllib
    def test_removing_the_table_removes_the_pin(self) -> None:
        pin = self.root / "config/mise.pin"
        self.write_profile(self.table())
        mise_pin.main(["write", str(self.profile), str(pin)])
        self.assertTrue(pin.exists())
        self.write_profile(None)
        self.assertEqual(0, mise_pin.main(["write", str(self.profile), str(pin)]))
        self.assertFalse(pin.exists())

    @testing_support.requires_tomllib
    def test_a_malformed_table_is_a_usage_error(self) -> None:
        self.write_profile(self.table(version="2026"))
        self.assertEqual(2, mise_pin.main(["check", str(self.profile)]))
        self.assertEqual(2, mise_pin.main(["write", str(self.profile)]))

    @testing_support.requires_tomllib
    def test_the_fleet_loader_validates_the_table(self) -> None:
        import macos_fleet_lanes as lanes

        self.assertIn("mise", lanes.TOP_KEYS)
        source = (Path(lanes.__file__)).read_text(encoding="utf-8")
        self.assertIn("mise_pin.table_errors", source)

    @testing_support.requires_tomllib
    def test_every_fleet_profile_pin_is_well_formed(self) -> None:
        import tomllib

        profiles = sorted((Path(__file__).resolve().parents[1] / "profiles").glob("*.toml"))
        self.assertGreater(len(profiles), 3)  # control: the scan reads real profiles
        for path in profiles:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            if "mise" in data:
                self.assertEqual([], mise_pin.table_errors(data["mise"]), path.name)

    @unittest.skipUnless(os.access("/bin/sh", os.X_OK), "the stand-in binary is a shell script")
    def test_the_installer_refuses_before_writing_agents(self) -> None:
        source = (Path(__file__).resolve().parent / "install_macos_fleet.sh").read_text(encoding="utf-8")
        check = source.index('"$ROOT/scripts/mise_pin.py" check "$CONFIG"')
        self.assertLess(check, source.index('"$PY" render "$CONFIG"'))
        self.assertLess(check, source.index("replacements=()"))
        self.assertIn('"$ROOT/scripts/mise_pin.py" write "$CONFIG" "$HOME/.config/tartci/mise.pin"', source)


if __name__ == "__main__":
    unittest.main()
