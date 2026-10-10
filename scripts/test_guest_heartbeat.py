#!/usr/bin/env python3
"""A frozen guest is torn down at the heartbeat threshold, not the job timeout.

A guest that hangs mid-job can keep its ssh session open, so the listener
waited for JOB_TIMEOUT (7200 s) and held the slot for two hours. The guest
launcher now writes a TARTCI_GUEST_HEARTBEAT line every 30 s through that
session; the host tears the VM down once the log has been silent for
TARTCI_GUEST_HEARTBEAT_STALE_SECS. These tests run runner.sh's own listener
function against a fake ssh whose guest stops heartbeating.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
GUEST = ROOT / "providers" / "tart-macos" / "guest-aqua-runner.sh"


def function(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\{{\n.*?^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group(0)


class Listener:
    def __init__(self, test: unittest.TestCase, guest_body: str) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.events = self.tmp / "events"
        self.events.write_text("")
        ssh = self.bin / "ssh"
        # First call streams the JIT config (stdin); the second is the runner.
        ssh.write_text("#!/bin/bash\ncase \"$*\" in *jit.cfg*) cat >/dev/null; exit 0;; esac\n"
                       + guest_body)
        ssh.chmod(0o755)

    def run(self, stale: int | None, timeout: float = 60,
            env_stale: str | None = None) -> tuple[subprocess.CompletedProcess, float]:
        """`stale` sets the listener's threshold directly; None takes it from
        runner.sh's own top-level assignment and TARTCI_GUEST_HEARTBEAT_STALE_SECS."""
        source = RUNNER.read_text()
        if stale is None:
            start = source.index('GUEST_HEARTBEAT_STALE="${TARTCI_GUEST_HEARTBEAT_STALE_SECS')
            end = source.index("esac\n", start) + len("esac\n")
            threshold = source[start:end]
        else:
            threshold = f"GUEST_HEARTBEAT_STALE={stale}\n"
        script = (
            "set -uo pipefail\n"
            f"STATE_DIR={str(self.tmp)!r}\n"
            "SSH_OPTS=(-o BatchMode=yes); SSH_KEY_PRIV=/dev/null; VM_USER=admin\n"
            "ASSIGNMENT_V2_IDLE_RETARGET_SECS=0; IDLE_TIMEOUT=600; JOB_WARN=600; JOB_TIMEOUT=7200\n"
            + threshold +
            "CCACHE_MAX_SIZE=1G; CCACHE_LAYER_GUEST_PREP=''; CCACHE_LAYER_GUEST_ENV=''\n"
            "GUEST_HTTP_PROXY=''; CURRENT_GUEST_CORES=''; CURRENT_GUEST_MEM_MB=''\n"
            "CURRENT_PIP_WHEELHOUSE=0; CURRENT_ARTIFACT_CACHE=0; GUEST_PIP_WHEELHOUSE=''; GUEST_ARTIFACT_CACHE=''\n"
            "CURRENT_RUN_ID=''; CURRENT_JOB_ID=''; CURRENT_JOB_CAPTURE_STATUS=''\n"
            f"event(){{ echo \"$*\" >>{str(self.events)!r}; }}\n"
            "note(){ :; }; heartbeat(){ :; }; stop_current_aqua_runner(){ :; }\n"
            "tartci_pool_lock_handoff_to_listener(){ return 0; }\n"
            "capture_current_job(){ return 1; }; tartci_job_claim_release(){ :; }\n"
            f"cancel_current_run(){{ echo cancel >>{str(self.events)!r}; }}\n"
            "finalize_listener_receipt(){ :; }\n"
            + function(source, "tartci_guest_silent_secs") + "\n"
            + function(source, "run_runner_until_done_unlayered") + "\n"
            "run_runner_until_done_unlayered vm1 192.0.2.1 jit 0; echo \"rc=$?\"\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "TARTCI_GUEST_HEARTBEAT_STALE_SECS"}
        env["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        if env_stale is not None:
            env["TARTCI_GUEST_HEARTBEAT_STALE_SECS"] = env_stale
        started = time.monotonic()
        result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                                env=env, timeout=timeout, check=False)
        return result, time.monotonic() - started

    def event_text(self) -> str:
        return self.events.read_text()


FROZEN = ("echo 'TARTCI_GUEST_HEARTBEAT 1'\n"
          "echo 'Running job: build'\n"
          "exec sleep 300\n")
ALIVE = ("echo 'Running job: build'\n"
         "for i in $(seq 1 12); do echo \"TARTCI_GUEST_HEARTBEAT $i\"; sleep 1; done\n"
         "exit 0\n")


class GuestHeartbeatTests(unittest.TestCase):
    def test_a_guest_that_stops_heartbeating_is_torn_down_at_the_threshold(self) -> None:
        listener = Listener(self, FROZEN)
        result, elapsed = listener.run(stale=3)
        self.assertIn("rc=124", result.stdout, result.stderr)
        # Threshold plus one 5 s poll, never the 2 h job timeout.
        self.assertLess(elapsed, 20)
        events = listener.event_text()
        self.assertIn("guest_heartbeat_stale", events)
        self.assertIn("assigned=1", events)
        self.assertIn("cancel", events, "an assigned job's run is cancelled")
        self.assertNotIn("TARTCI_GUEST_HEARTBEAT", result.stderr, "heartbeats are not echoed")

    def test_the_rendered_threshold_reaches_the_listener(self) -> None:
        # The profile renders TARTCI_GUEST_HEARTBEAT_STALE_SECS; a listener
        # that ignored it would wait the 600 s default and outlive this test.
        listener = Listener(self, FROZEN)
        result, elapsed = listener.run(stale=None, env_stale="4", timeout=40)
        self.assertIn("rc=124", result.stdout, result.stderr)
        self.assertLess(elapsed, 20)
        self.assertIn("stale_after=4s", listener.event_text())

    def test_a_malformed_threshold_falls_back_to_the_default(self) -> None:
        source = RUNNER.read_text()
        self.assertIn('case "$GUEST_HEARTBEAT_STALE" in \'\'|*[!0-9]*|0) GUEST_HEARTBEAT_STALE=600', source)

    def test_a_guest_that_keeps_heartbeating_is_left_alone(self) -> None:
        # Control, same instrument and threshold: only the guest's beats differ.
        listener = Listener(self, ALIVE)
        result, _ = listener.run(stale=3)
        self.assertIn("rc=0", result.stdout, result.stderr)
        self.assertNotIn("guest_heartbeat_stale", listener.event_text())

    def test_no_heartbeat_yet_is_not_a_frozen_guest(self) -> None:
        # Before the guest launcher's first beat (an old guest, or still
        # booting the runner) silence proves nothing; idle/job timeouts apply.
        listener = Listener(self, "echo 'starting'\nsleep 9\nexit 0\n")
        result, _ = listener.run(stale=3)
        self.assertIn("rc=0", result.stdout, result.stderr)
        self.assertNotIn("guest_heartbeat_stale", listener.event_text())


class GuestLauncherTests(unittest.TestCase):
    def test_the_launcher_beats_inside_its_wait_loop(self) -> None:
        body = function(GUEST.read_text(), "run_runner")
        loop = body[body.index('while [ ! -s "$root/exit" ]; do'):]
        self.assertIn("printf 'TARTCI_GUEST_HEARTBEAT %s\\n'", loop)
        self.assertIn('TARTCI_GUEST_HEARTBEAT_SECS:-30', body)

    def test_the_threshold_comes_from_the_profile(self) -> None:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import macos_fleet_lanes as fleet
        for profile in sorted((ROOT / "profiles").glob("*-macos-fleet.toml")):
            data = fleet.load(profile)
            for name, body in fleet.rendered_plists(data).items():
                if ".pulp-gate" in name:
                    with self.subTest(plist=name):
                        self.assertIn(b"TARTCI_GUEST_HEARTBEAT_STALE_SECS", body)


if __name__ == "__main__":
    unittest.main()
