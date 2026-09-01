#!/usr/bin/env python3
"""Structural guards for the macOS guardian-first teardown contract."""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"


def function_body(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\{{\n(.*?)^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group(1)


class MacosTeardownOrderTests(unittest.TestCase):
    def test_guardian_is_proved_terminal_before_tart_mutations(self) -> None:
        body = function_body(RUNNER.read_text(), "discard_current_vm")
        guardian = body.index("terminate_current_guardian")
        stop = body.index("bounded_teardown_command tart-stop")
        delete = body.index("bounded_teardown_command tart-delete")
        self.assertLess(guardian, stop)
        self.assertLess(stop, delete)
        self.assertNotIn("tart stop", body.replace("bounded_teardown_command tart-stop tart stop", ""))

    def test_cleanup_releases_capacity_only_after_terminal_teardown(self) -> None:
        body = function_body(RUNNER.read_text(), "cleanup")
        gate = body.index('if [ "$teardown_terminal" = 1 ]')
        self.assertGreater(body.index("tartci_release_vm_lease"), gate)
        self.assertGreater(body.index('rm -f "$CURRENT_RESV"'), gate)
        self.assertIn('reclaim_runner_name "$RUNNER_NAME" "$CURRENT_RUNNER_API_ROOT" 1', body)

    def test_hanging_tart_stop_cannot_hold_guardian_or_skip_delete(self) -> None:
        source = RUNNER.read_text()
        functions = "\n".join(
            f"{name}(){{\n{function_body(source, name)}}}"
            for name in (
                "bounded_teardown_command",
                "terminate_current_guardian",
                "stop_current_aqua_runner",
                "discard_current_vm",
            )
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fakebin = root / "bin"
            fakebin.mkdir()
            delete_marker = root / "deleted"
            tart = fakebin / "tart"
            tart.write_text(
                "#!/bin/bash\n"
                "case \"$1\" in\n"
                "  stop) sleep 60 ;;\n"
                f"  delete) touch {str(delete_marker)!r} ;;\n"
                "esac\n"
            )
            tart.chmod(0o755)
            harness = root / "harness.sh"
            harness.write_text(
                "#!/bin/bash\nset -u\n"
                f"export PATH={str(fakebin)!r}:$PATH\n"
                f"TARTCI_ROOT={str(ROOT)!r}\n"
                "TEARDOWN_STEP_TIMEOUT=1\n"
                "CURRENT_VM=test-vm\nCURRENT_IP=\nCURRENT_AQUA_LABEL=\n"
                "note(){ :; }\nevent(){ :; }\n"
                f"{functions}\n"
                "sleep 60 & CURRENT_RPID=$!\n"
                "guardian=$CURRENT_RPID\n"
                "discard_current_vm\n"
                "! kill -0 \"$guardian\" 2>/dev/null\n"
                "[ -z \"$CURRENT_VM\" ]\n"
            )
            harness.chmod(0o755)
            started = time.monotonic()
            result = subprocess.run(
                [str(harness)], text=True, capture_output=True, check=False, timeout=5
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(time.monotonic() - started, 3)
            self.assertTrue(delete_marker.exists(), "bounded delete was skipped")


if __name__ == "__main__":
    unittest.main()
