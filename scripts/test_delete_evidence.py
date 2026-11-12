"""An unproved `tart delete` records why: bound fired or tart refused.

On m3 on 2026-10-04 a lane's delete failed six times in two minutes at load
~15 and the janitor's succeeded first try 85 minutes later. The command's
output went to /dev/null, so whether the 5 s bound or tart itself was at
fault could not be told. These tests pin the fields that tell them apart.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import delete_evidence  # noqa: E402

RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"


def run_delete(fake_tart: str, timeout: str = "1") -> tuple[int, str]:
    """Run the runner's own tartci_tart_delete against a fake `tart`."""
    body = RUNNER.read_text()
    start = body.index('TARTCI_DELETE_EVIDENCE=""\ntartci_tart_delete(){')
    end = body.index("\n}\n", start) + 3
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        (bindir / "tart").write_text(f"#!/bin/sh\n{fake_tart}\n")
        (bindir / "tart").chmod(0o755)
        script = (f"TARTCI_ROOT={str(ROOT)!r}\nTEARDOWN_STEP_TIMEOUT={timeout}\n"
                  f"{body[start:end]}\n"
                  'rc=0; tartci_tart_delete vm-1 || rc=$?\n'
                  'printf "%s\\n%s\\n" "$rc" "$TARTCI_DELETE_EVIDENCE"\n')
        result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                                timeout=30, env={"PATH": f"{bindir}:/usr/bin:/bin",
                                                 "TMPDIR": tmp})
    rc, evidence = (result.stdout.split("\n", 1) + [""])[:2]
    return int(rc), evidence.strip()


class DeleteEvidenceTests(unittest.TestCase):
    def test_a_fired_bound_is_named_apart_from_tarts_own_124(self) -> None:
        fired = delete_evidence.render({"returncode": 124, "timed_out": True, "elapsed_ms": 5003},
                                       "timed out after 5s", 14.8)
        refused = delete_evidence.render({"returncode": 124, "timed_out": False,
                                          "elapsed_ms": 40}, "boom", 0.5)
        self.assertIn("bounded=yes", fired)
        self.assertIn("bounded=no", refused)
        self.assertIn("rc=124", fired)
        self.assertIn("rc=124", refused)
        self.assertIn("elapsed_ms=5003", fired)
        self.assertIn("load1=14.80", fired)

    def test_a_missing_status_is_unknown_not_a_bound_that_did_not_fire(self) -> None:
        rendered = delete_evidence.render({}, "", None)
        self.assertIn("rc=?", rendered)
        self.assertIn("bounded=?", rendered)
        self.assertNotIn("bounded=no", rendered)

    def test_stderr_keeps_its_last_lines_capped_and_quote_safe(self) -> None:
        text = "\n".join(f'line {i} "q"' for i in range(50)) + "\n" + "x" * 1000
        rendered = delete_evidence.render({"returncode": 1}, text, None)
        field = rendered.split('stderr="', 1)[1].rstrip('"')
        self.assertLessEqual(len(field), delete_evidence.STDERR_MAX_CHARS + 1)
        self.assertNotIn('"', field)
        self.assertIn("load1=?", rendered)
        quoted = delete_evidence.render({"returncode": 1}, 'tart: "vm-1" is locked', None)
        self.assertIn('stderr="tart: \'vm-1\' is locked"', quoted)

    def test_the_bounded_command_reports_whether_its_bound_fired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            for argv, fired in (
                (["sleep", "5"], True),
                (["sh", "-c", "echo boom >&2; exit 124"], False),
            ):
                subprocess.run([sys.executable, str(HERE / "bounded_command.py"),
                                "--timeout", "0.5", "--status-file", str(status), "--", *argv],
                               capture_output=True, timeout=30)
                data = json.loads(status.read_text())
                self.assertEqual(data["returncode"], 124, argv)
                self.assertIs(data["timed_out"], fired, argv)
                self.assertIsInstance(data["elapsed_ms"], int)

    def test_the_lane_delete_records_a_fired_bound(self) -> None:
        rc, evidence = run_delete("sleep 10")
        self.assertNotEqual(rc, 0)
        self.assertIn("bounded=yes", evidence)
        self.assertIn("rc=124", evidence)

    def test_the_lane_delete_records_tarts_refusal_and_its_stderr(self) -> None:
        rc, evidence = run_delete('echo "VM is locked by another process" >&2; exit 1')
        self.assertEqual(rc, 1)
        self.assertIn("bounded=no", evidence)
        self.assertIn('stderr="VM is locked by another process"', evidence)

    def test_a_successful_delete_records_nothing(self) -> None:
        # Control, same instrument.
        rc, evidence = run_delete("exit 0")
        self.assertEqual((rc, evidence), (0, ""))

    def test_both_lane_delete_sites_log_an_unproved_delete(self) -> None:
        body = RUNNER.read_text()
        self.assertEqual(body.count("tart delete \"$CURRENT_VM\""), 0,
                         "every lane delete goes through tartci_tart_delete")
        self.assertEqual(body.count('tartci_tart_delete "$CURRENT_VM"'), 2)
        self.assertIn('event delete_unproved "vm=$CURRENT_VM site=teardown $TARTCI_DELETE_EVIDENCE"',
                      body)
        self.assertIn('event delete_unproved "vm=$CURRENT_VM site=pending_delete', body)
        self.assertIn('TEARDOWN_STEP_TIMEOUT="${TARTCI_TEARDOWN_STEP_TIMEOUT_SECS:-5}"', body,
                      "the 5 s bound is unchanged")


if __name__ == "__main__":
    unittest.main()
