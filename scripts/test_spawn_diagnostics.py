"""Guest diagnostics are captured when the runner could not start a tool.

A merge-group job on m5 failed its last post step with "An error occurred
trying to start process '.../externals/node24/bin/node' ... Exec format
error", and the evidence was lost when the VM was discarded. The capture reads
the guest's worker logs before teardown and saves bounded diagnostics when
they show such a failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "providers" / "tart-macos" / "spawn-diagnostics.lib.sh"

# A stand-in for ssh that runs the remote script as the guest would, against a
# fake guest home and a fake guest PATH, logging each script it was given.
FAKE_SSH = r"""#!/bin/sh
for last; do :; done
printf '%s\n---\n' "$last" >> "$FAKE_CALLS"
exec env HOME="$FAKE_GUEST_HOME" PATH="$FAKE_GUEST_BIN:/usr/bin:/bin" /bin/sh -c "$last"
"""

GUEST_TOOLS = {
    "vm_stat": '#!/bin/sh\n[ "${FAKE_HANG:-0}" = 1 ] && sleep 30\necho "Pages free: 1234."\n'
               'head -c "${FAKE_BODY_BYTES:-0}" /dev/zero | tr "\\0" x\n',
    "log": '#!/bin/sh\necho "kernel: fake unified log line"\n',
    "codesign": "#!/bin/sh\nexit 0\n",
    # The guest is macOS; Linux CI has no Mach-O to describe, so these answer
    # as the guest's own tools would.
    "file": '#!/bin/sh\necho "$1: Mach-O 64-bit executable arm64"\n',
    "shasum": '#!/bin/sh\necho "0123abcd  $3"\n',
}

ERROR_LINE = ("[2026-10-04 16:48:05Z ERR  StepsRunner] An error occurred trying to start "
              "process '/Users/admin/actions-runner/externals/node24/bin/node' with working "
              "directory '/Users/admin/actions-runner/_work/pulp/pulp'. Exec format error\n")


class SpawnDiagnosticsTests(unittest.TestCase):
    def run_capture(self, worker_log: str, **env: str):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        bindir = tmp / "bin"
        bindir.mkdir()
        ssh = bindir / "ssh"
        ssh.write_text(FAKE_SSH)
        ssh.chmod(0o755)
        guest_home = tmp / "guest-home"
        diag = guest_home / "actions-runner" / "_diag"
        diag.mkdir(parents=True)
        (diag / "Worker_20261004-161846-utc.log").write_text(worker_log)
        node = guest_home / "actions-runner" / "externals" / "node24" / "bin" / "node"
        node.parent.mkdir(parents=True)
        node.write_bytes(b"\xcf\xfa\xed\xfe")
        guest_bin = tmp / "guest-bin"
        guest_bin.mkdir()
        for name, body in GUEST_TOOLS.items():
            (guest_bin / name).write_text(body)
            (guest_bin / name).chmod(0o755)
        state = tmp / "state"
        state.mkdir()
        events = tmp / "events.jsonl"
        script = f"""
set -u
TARTCI_ROOT={str(ROOT)!r}
STATE_DIR={str(state)!r}
SSH_OPTS=(-o BatchMode=yes)
SSH_KEY_PRIV=/dev/null
VM_USER=admin
event(){{ printf '{{"event":"%s","detail":"%s"}}\\n' "$1" "$2" >> {str(events)!r}; }}
source {str(LIB)!r}
tartci_capture_guest_spawn_errors vm-1 192.168.64.4
echo "rc=$?"
"""
        environment = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin",
                       "FAKE_CALLS": str(tmp / "calls"),
                       "FAKE_GUEST_HOME": str(guest_home), "FAKE_GUEST_BIN": str(guest_bin),
                       **env}
        started = time.monotonic()
        result = subprocess.run(["/bin/bash", "-c", script], env=environment,
                                capture_output=True, text=True, timeout=60)
        elapsed = time.monotonic() - started
        saved = sorted((state / "spawn-diagnostics").glob("*.txt")) \
            if (state / "spawn-diagnostics").exists() else []
        recorded = [json.loads(line) for line in events.read_text().splitlines()] \
            if events.exists() else []
        calls = (tmp / "calls").read_text() if (tmp / "calls").exists() else ""
        return result, elapsed, saved, recorded, calls

    def test_a_spawn_error_is_captured_before_teardown(self) -> None:
        result, _, saved, recorded, calls = self.run_capture(ERROR_LINE)
        self.assertIn("rc=0", result.stdout)
        self.assertEqual(len(saved), 1)
        text = saved[0].read_text()
        self.assertIn("Exec format error", text)
        self.assertIn("Pages free: 1234.", text)
        self.assertIn("node: Mach-O 64-bit executable arm64", text)
        self.assertIn("0123abcd", text)
        self.assertIn("codesign ok:", text)
        self.assertIn("fake unified log line", text)
        self.assertEqual([event["event"] for event in recorded], ["guest_spawn_error_diagnostics"])
        self.assertIn("capture_rc=0", recorded[0]["detail"])
        for needed in ("vm_stat", "codesign -v", "file \"$f\"", "log show --last 2m"):
            self.assertIn(needed, calls)

    def test_a_clean_job_costs_one_probe_and_records_nothing(self) -> None:
        # Control, same instrument: no spawn error in the worker log.
        result, _, saved, recorded, calls = self.run_capture("[INFO] Job completed\n")
        self.assertIn("rc=0", result.stdout)
        self.assertEqual(saved, [])
        self.assertEqual(recorded, [])
        self.assertEqual(calls.count("---"), 1)

    def test_the_saved_output_is_capped(self) -> None:
        _, _, saved, _, _ = self.run_capture(
            ERROR_LINE, FAKE_BODY_BYTES="400000", TARTCI_SPAWN_DIAG_MAX_BYTES="65536")
        self.assertEqual(saved[0].stat().st_size, 65536)

    def test_a_hung_guest_costs_at_most_the_capture_budget(self) -> None:
        result, elapsed, saved, recorded, _ = self.run_capture(
            ERROR_LINE, FAKE_HANG="1", TARTCI_SPAWN_DIAG_CAPTURE_TIMEOUT_SECS="2")
        self.assertIn("rc=0", result.stdout)
        self.assertLess(elapsed, 15)
        self.assertEqual(len(saved), 1)
        self.assertNotIn("capture_rc=0", recorded[0]["detail"])

    def test_the_runner_captures_before_it_discards_the_vm(self) -> None:
        body = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        run = body.index('run_runner_until_done "$vm" "$ip" "$jit" "$selected_tier" || rc=$?')
        capture = body.index('tartci_capture_guest_spawn_errors "$vm" "$ip"', run)
        discard = body.index("discard_current_vm", run)
        self.assertLess(capture, discard)
        self.assertIn('source "$TARTCI_ROOT/providers/tart-macos/spawn-diagnostics.lib.sh"', body)


if __name__ == "__main__":
    unittest.main()
