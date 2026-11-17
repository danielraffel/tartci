#!/usr/bin/env python3
"""The idle teardown deletes the JIT runner first, and keeps a busy one.

A runner GitHub assigned a job to at the idle deadline was torn down and the
job failed with 0 steps after ~10 min (10 Vellum GPU jobs in 4.4 days). Before
killing on idle, the supervisor now DELETEs the registration: success means it
can no longer be assigned (kill), HTTP 422 means it is busy (keep), anything
else keeps today's behaviour and is logged. The fake gh answers the runner list
and the DELETE; the listener function is runner.sh's own.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_guest_heartbeat import RUNNER, Listener, function  # noqa: E402

LISTENS_THEN_JOB = ("echo 'Listening for Jobs'\nsleep 6\necho 'Running job: build'\n"
                    "sleep 6\nexit 0\n")
LISTENS_ONLY = "echo 'Listening for Jobs'\nexec sleep 300\n"


class IdleDeleteTests(unittest.TestCase):
    def run_listener(self, guest: str, delete: str, runners: str = "4242"):
        listener = Listener(self, guest)
        calls = listener.tmp / "gh-calls"
        gh = listener.tmp / "fake-gh"
        gh.write_text(
            "#!/bin/bash\n"
            f"echo \"$*\" >>{str(calls)!r}\n"
            "case \"$*\" in\n"
            f"  *--paginate*) printf '%s' {runners!r}; exit 0;;\n"
            f"  *'-X DELETE'*) {delete};;\n"
            "esac\n")
        gh.chmod(0o755)
        prelude = (f"JIT_GH_CLI={str(gh)!r}; REPO=Org/repo; "
                   "CURRENT_RUNNER_API_ROOT=repos/Org/repo/actions/runners; "
                   "CURRENT_REGISTERED_RUNNER=vm1\n"
                   + function(RUNNER.read_text(), "tartci_idle_delete_runner") + "\n")
        result, _ = listener.run(stale=600, idle=4, cap=60, prelude=prelude)
        return result, listener.event_text(), calls.read_text() if calls.exists() else ""

    def test_delete_success_kills_the_idle_vm(self) -> None:
        result, events, calls = self.run_listener(LISTENS_ONLY, "exit 0")
        self.assertIn("rc=124", result.stdout, result.stderr)
        self.assertIn("idle_runner_deleted", events)
        self.assertIn("idle_timeout", events)
        self.assertIn("-X DELETE repos/Org/repo/actions/runners/4242", calls)

    def test_422_means_assigned_and_the_vm_is_kept(self) -> None:
        # The job line appears 6 s after listening, a second after the idle
        # deadline; the runner list/DELETE says busy in between.
        result, events, calls = self.run_listener(
            LISTENS_THEN_JOB,
            "echo 'gh: Bad request - Runner is still running a job. (HTTP 422)'; exit 1")
        self.assertIn("rc=0", result.stdout, result.stderr)
        self.assertIn("idle_timeout_overtaken", events)
        self.assertIn("job_assigned", events)
        self.assertNotIn("idle_timeout ", events)
        self.assertIn("-X DELETE", calls)

    def test_any_other_error_keeps_todays_behaviour_and_is_logged(self) -> None:
        result, events, _ = self.run_listener(
            LISTENS_ONLY, "echo 'gh: Server Error (HTTP 500)'; exit 1")
        self.assertIn("rc=124", result.stdout, result.stderr)
        self.assertIn("idle_delete_failed", events)
        self.assertIn("HTTP 500", events)
        self.assertIn("idle_timeout", events)

    def test_a_runner_gitHub_no_longer_lists_is_not_assignable(self) -> None:
        result, events, calls = self.run_listener(LISTENS_ONLY, "exit 0", runners="")
        self.assertIn("rc=124", result.stdout, result.stderr)
        self.assertIn("idle_runner_deleted absent", events)
        self.assertNotIn("-X DELETE", calls)


class IdleRetargetDeleteTests(unittest.TestCase):
    """The idle-retarget kill has the same race and uses the same gate."""

    RETARGET = ("IDLE_RETARGET_RC=75; ASSIGNMENT_V2_IDLE_RETARGET_SECS=1\n"
                "tartci_assignment_v2_idle_retarget(){ return 0; }\n")

    def run_retarget(self, delete: str):
        listener = Listener(self, "echo 'Listening for Jobs'\nsleep 12\nexit 0\n")
        calls = listener.tmp / "gh-calls"
        gh = listener.tmp / "fake-gh"
        gh.write_text("#!/bin/bash\n"
                      f"echo \"$*\" >>{str(calls)!r}\n"
                      "case \"$*\" in\n"
                      "  *--paginate*) printf '%s' 4242; exit 0;;\n"
                      f"  *'-X DELETE'*) {delete};;\n"
                      "esac\n")
        gh.chmod(0o755)
        prelude = (f"JIT_GH_CLI={str(gh)!r}; REPO=Org/repo; "
                   "CURRENT_RUNNER_API_ROOT=repos/Org/repo/actions/runners; "
                   "CURRENT_REGISTERED_RUNNER=vm1\n" + self.RETARGET
                   + function(RUNNER.read_text(), "tartci_idle_delete_runner") + "\n")
        result, _ = listener.run(stale=600, idle=600, cap=600, prelude=prelude)
        return result, listener.event_text(), calls.read_text() if calls.exists() else ""

    def test_retarget_deletes_the_registration_then_retargets(self) -> None:
        result, events, calls = self.run_retarget("exit 0")
        self.assertIn("rc=75", result.stdout, result.stderr)
        self.assertIn("idle_runner_deleted", events)
        self.assertIn("-X DELETE", calls)

    def test_retarget_keeps_a_runner_the_delete_says_is_busy(self) -> None:
        result, events, _ = self.run_retarget(
            "echo 'gh: Bad request - Runner is still running a job. (HTTP 422)'; exit 1")
        self.assertNotIn("rc=75", result.stdout)
        self.assertIn("assignment_v2_idle_retarget_overtaken", events)
        self.assertIn("busy", events)


if __name__ == "__main__":
    unittest.main(verbosity=2)
