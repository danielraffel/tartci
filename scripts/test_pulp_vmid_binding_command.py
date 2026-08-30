#!/usr/bin/env python3
"""Executable behavioral tests for the root-to-ci QGA nonce payload."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "providers/common/pulp-vmid-binding-command.py"
SPEC = importlib.util.spec_from_file_location("pulp_vmid_binding", MODULE)
assert SPEC is not None and SPEC.loader is not None
binding = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(binding)


class VmidBindingCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.log = self.state / "operations"
        self.target = self.root / "run/tartci-pulp-golden-9006.binding"
        self.target.parent.mkdir()
        self.nonce = "a" * 64
        self.write_tool(
            "id",
            """#!/bin/sh
set -eu
[ "$2" = -- ] && [ "$3" = ci ]
case "$1" in
  -u) printf '4242\\n'; printf 'id-u ci\\n' >>"$STATE/operations" ;;
  -g) printf '4343\\n'; printf 'id-g ci\\n' >>"$STATE/operations" ;;
  *) exit 2 ;;
esac
""",
        )
        self.write_tool(
            "chown",
            """#!/bin/sh
set -eu
[ "$1" = 4242:4343 ]
[ -f "$2" ]
printf 'chown 4242:4343 %s\\n' "$2" >>"$STATE/operations"
printf '4242:4343' >"$STATE/owner"
""",
        )
        self.write_tool(
            "chmod",
            """#!/bin/sh
set -eu
[ "$1" = 0400 ]
[ -f "$2" ]
printf 'chmod 0400 %s\\n' "$2" >>"$STATE/operations"
printf '0400' >"$STATE/mode"
""",
        )
        self.write_tool(
            "mv",
            """#!/bin/sh
set -eu
[ "$1" = -f ] && [ "$2" = -- ]
[ "$(cat "$STATE/owner")" = 4242:4343 ]
[ "$(cat "$STATE/mode")" = 0400 ]
printf 'mv %s %s\\n' "$3" "$4" >>"$STATE/operations"
/bin/mv -f -- "$3" "$4"
""",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_tool(self, name: str, source: str) -> None:
        path = self.bin / name
        path.write_text(source)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def execute(self, payload: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {"PATH": f"{self.bin}:{environment['PATH']}", "STATE": str(self.state)}
        )
        return subprocess.run(
            ["/bin/sh", "-c", payload], text=True, capture_output=True, env=environment
        )

    def valid_payload(self) -> str:
        # The production renderer restricts /run. Substitute only the test root
        # after rendering so the exact command sequence remains under test.
        payload = binding.render(
            "ci", "/run/tartci-pulp-golden-9006.binding", self.nonce
        )
        return payload.replace(
            "/run/tartci-pulp-golden-9006.binding", str(self.target)
        )

    def test_payload_orders_exact_owner_mode_before_atomic_move(self) -> None:
        result = self.execute(self.valid_payload())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_text(), self.nonce)
        operations = self.log.read_text().splitlines()
        self.assertEqual(operations[0:2], ["id-u ci", "id-g ci"])
        self.assertTrue(operations[2].startswith("chown 4242:4343 "))
        self.assertTrue(operations[3].startswith("chmod 0400 "))
        self.assertTrue(operations[4].startswith("mv "))
        self.assertEqual(
            list(self.target.parent.glob(f"{self.target.name}.tmp.*")), []
        )

    def test_broken_owner_or_mode_cannot_reach_atomic_move(self) -> None:
        for broken in (
            self.valid_payload().replace('chown "$uid:$gid" "$tmp"', ":"),
            self.valid_payload().replace("chmod 0400", "chmod 0644"),
        ):
            self.state.joinpath("owner").unlink(missing_ok=True)
            self.state.joinpath("mode").unlink(missing_ok=True)
            self.log.unlink(missing_ok=True)
            result = self.execute(broken)
            self.assertNotEqual(result.returncode, 0)
            operations = self.log.read_text() if self.log.exists() else ""
            self.assertNotIn("\nmv ", "\n" + operations)

    def test_renderer_rejects_unsafe_quoting_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical VMID-scoped"):
            binding.render("ci", "/run/x'; touch /tmp/owned; #", self.nonce)
        with self.assertRaisesRegex(ValueError, "canonical protected user ci"):
            binding.render(
                "ci'; id; #", "/run/tartci-pulp-golden-9006.binding", self.nonce
            )
        with self.assertRaisesRegex(ValueError, "lowercase 64-hex"):
            binding.render("ci", "/run/tartci-pulp-golden-9006.binding", "$(id)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
