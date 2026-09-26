#!/usr/bin/env python3
"""At most one booting VM per queued job, and never fewer than the queue needs.

On the Pulp gate, 72 fully booted VMs in one day were discarded at the pre-mint
recheck (`assignment_v2_pre_mint_denied`) because several free lanes booted for
the same queued job. A lane now claims a queued job of its class before it
clones, and a lane whose class's queued jobs are all covered does not boot.

Two properties are tested from both sides:

* covered demand does not boot (the waste this removes);
* uncovered demand always boots, including whenever the claim store, the
  runner listing or the exact count is unavailable (fail open), so a claim can
  never be the reason a queued job goes unserved.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLAIM = ROOT / "scripts/job_claim.py"
LIB = ROOT / "providers/tart-macos/job-claim.lib.sh"
RUNNER = ROOT / "providers/tart-macos/runner.sh"
REPO = "Generous-Corp/pulp"
LABELS = "self-hosted,macOS,ARM64,pulp-build-vm,pulp-build-pr-head"


class Sleeper:
    """A live process to own a claim, so owner-liveness is real, not mocked."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(["sleep", "60"])

    @property
    def pid(self) -> int:
        return self.proc.pid

    def stop(self) -> None:
        self.proc.kill()
        self.proc.wait()


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = self.tmp / "claims"
        self.owners: list[Sleeper] = []
        self.addCleanup(lambda: [owner.stop() for owner in self.owners])

    def owner(self) -> int:
        sleeper = Sleeper()
        self.owners.append(sleeper)
        return sleeper.pid

    def acquire(self, claim_id: str, queued: int, *, pid: int | None = None,
                lower: bool = False, runners: list[dict] | None = None,
                ttl: int = 1800, labels: str = LABELS, vm: str | None = None) -> tuple[dict, int]:
        argv = [sys.executable, "-B", str(CLAIM), "acquire", "--dir", str(self.dir),
                "--repo", REPO, "--labels", labels, "--claim-id", claim_id,
                "--lane", claim_id, "--vm", vm or f"vm-{claim_id}",
                "--pid", str(pid if pid is not None else self.owner()),
                "--queued", str(queued), "--ttl", str(ttl)]
        if lower:
            argv.append("--lower-bound")
        if runners is not None:
            path = self.tmp / f"runners-{claim_id}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in runners))
            argv += ["--fleet-runners-file", str(path)]
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        return json.loads(proc.stdout), proc.returncode

    def release(self, claim_id: str) -> None:
        subprocess.run([sys.executable, "-B", str(CLAIM), "release", "--dir", str(self.dir),
                        "--repo", REPO, "--labels", LABELS, "--claim-id", claim_id],
                       check=True, capture_output=True)

    def test_one_queued_job_one_boot(self) -> None:
        self.assertEqual(self.acquire("a", 1)[1], 0)
        result, rc = self.acquire("b", 1)
        self.assertEqual((result["verdict"], rc), ("contended", 3))
        self.assertEqual(result["standing_claims"], 1)

    def test_the_control_two_queued_jobs_two_boots(self) -> None:
        self.assertEqual(self.acquire("a", 2)[1], 0)
        self.assertEqual(self.acquire("b", 2)[1], 0)
        self.assertEqual(self.acquire("c", 2)[1], 3)

    def test_a_lower_bound_asks_for_the_exact_count_only_when_contended(self) -> None:
        # Uncontended: "at least one" is enough to claim; no scan is bought.
        self.assertEqual(self.acquire("a", 1, lower=True)[1], 0)
        result, rc = self.acquire("b", 1, lower=True)
        self.assertEqual((result["verdict"], rc), ("need_exact", 4))
        self.assertEqual(self.acquire("b", 2)[1], 0)

    def test_release_and_a_dead_owner_free_the_claim(self) -> None:
        self.assertEqual(self.acquire("a", 1)[1], 0)
        self.release("a")
        self.assertEqual(self.acquire("b", 1)[1], 0, "a released claim still stands")
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.release("b")
        self.assertEqual(self.acquire("c", 1, pid=dead.pid)[1], 0)
        self.assertEqual(self.acquire("d", 1)[1], 0, "a dead supervisor's claim still stands")

    def test_an_expired_claim_stops_standing(self) -> None:
        self.assertEqual(self.acquire("a", 1, ttl=1)[1], 0)
        time.sleep(1.2)
        self.assertEqual(self.acquire("b", 1)[1], 0)

    def test_reacquiring_does_not_count_the_lane_against_itself(self) -> None:
        pid = self.owner()
        self.assertEqual(self.acquire("a", 1, pid=pid)[1], 0)
        self.assertEqual(self.acquire("a", 1, pid=pid)[1], 0)

    def test_classes_do_not_share_claims(self) -> None:
        self.assertEqual(self.acquire("a", 1)[1], 0)
        other = LABELS.replace("pulp-build-pr-head", "pulp-build-merge-group")
        self.assertEqual(self.acquire("b", 1, labels=other)[1], 0)

    def test_an_idle_minted_runner_anywhere_in_the_fleet_covers_a_job(self) -> None:
        idle = {"name": "m3-pulp-gate-01-1-2", "labels": LABELS.split(",") + ["extra"]}
        result, rc = self.acquire("a", 1, runners=[idle])
        self.assertEqual((result["verdict"], rc), ("contended", 3))
        self.assertEqual(result["fleet_idle_runners"], ["m3-pulp-gate-01-1-2"])
        self.assertEqual(self.acquire("a", 2, runners=[idle])[1], 0)

    def test_a_runner_that_cannot_serve_the_class_does_not_count(self) -> None:
        other_class = {"name": "x", "labels": LABELS.replace("pulp-build-pr-head", "pulp-build-merge-group").split(",")}
        self.assertEqual(self.acquire("a", 1, runners=[other_class, {"garbage": 1}])[1], 0)

    def test_a_local_claims_own_registered_runner_is_not_counted_twice(self) -> None:
        self.assertEqual(self.acquire("a", 2, vm="vm-a")[1], 0)
        mine = {"name": "vm-a", "labels": LABELS.split(",")}
        self.assertEqual(self.acquire("b", 2, runners=[mine])[1], 0)

    def test_an_unusable_store_is_an_error_the_caller_fails_open_on(self) -> None:
        self.dir = Path("/dev/null/claims")
        result, rc = self.acquire("a", 1)
        self.assertEqual((result["verdict"], rc), ("error", 1))


class LibraryTests(unittest.TestCase):
    """The shell side: what run_one sees, including every fail-open path."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.events = self.tmp / "events"
        self.holders: list[subprocess.Popen] = []
        self.addCleanup(self._stop_holders)

    def _stop_holders(self) -> None:
        for proc in self.holders:
            proc.kill()
            proc.wait()

    def gh(self, body: str) -> None:
        path = self.bin / "stub-gh"
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    def script(self, body: str, *, mode: str = "event-class-v2", exact: str = "echo 2") -> str:
        return (
            "set -euo pipefail\n"
            f"TARTCI_ROOT={str(ROOT)!r}\n"
            f"source {str(LIB)!r}\n"
            "note(){ :; }\n"
            f"event(){{ printf '%s\\t%s\\n' \"$1\" \"${{2:-}}\" >>{str(self.events)!r}; }}\n"
            f"tartci_assignment_v2_tier_demand(){{ printf 'demand:%s\\n' \"$*\" >>{str(self.events)!r}; {exact}; }}\n"
            f"REPO={REPO!r}\nRUNNER_NAME=lane\nSLOT=1\nGH_CLI=stub-gh\n"
            f"ASSIGNMENT_MODE={mode}\n"
            "TIER_LABELS_CONFIG=$'pulp-build-merge-group\\npulp-build-pr-head'\n"
            + body
        )

    def run_bash(self, script: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full = os.environ.copy()
        full.update({"PATH": f"{self.bin}{os.pathsep}{full['PATH']}",
                     "TARTCI_JOB_CLAIM_DIR": str(self.tmp / "claims")})
        full.update(env or {})
        return subprocess.run(["/bin/bash", "-c", script], env=full,
                              capture_output=True, text=True, check=False, timeout=60)

    def hold(self, vm: str, queued: int = 1) -> None:
        """Another lane on this host takes a claim and stays alive."""
        ready = self.tmp / f"ready-{vm}"
        proc = subprocess.Popen(
            ["/bin/bash", "-c", self.script(
                f"RUNNER_NAME=other-{vm}\n"
                f"tartci_job_claim_acquire {vm} {LABELS!r} 1 {queued} repos/x/actions/runners\n"
                f"touch {str(ready)!r}\nsleep 60\n")],
            env={**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                 "TARTCI_JOB_CLAIM_DIR": str(self.tmp / "claims")})
        self.holders.append(proc)
        for _ in range(200):
            if ready.exists():
                return
            time.sleep(0.05)
        self.fail("holder never claimed")

    def names(self) -> list[str]:
        if not self.events.exists():
            return []
        return [line.split("\t", 1)[0] for line in self.events.read_text().splitlines()]

    def acquire_line(self, queued: int = 1) -> str:
        return (f"rc=0; tartci_job_claim_acquire vm-me {LABELS!r} 1 {queued} repos/x/actions/runners || rc=$?\n"
                "echo \"rc=$rc contended=$JOB_CLAIM_CONTENDED id=$JOB_CLAIM_ID\"\n")

    def test_covered_demand_does_not_boot(self) -> None:
        self.gh("exit 0\n")
        self.hold("vm-a")
        proc = self.run_bash(self.script(self.acquire_line(), exact="echo 1"))
        self.assertIn("rc=75 contended=1 id=", proc.stdout, proc.stderr)
        self.assertIn("job_claim_contended", self.names())
        # The exact count was bought because a sibling held a claim.
        self.assertIn("demand:pulp-build-pr-head 1", self.events.read_text())

    def test_the_control_a_second_queued_job_boots(self) -> None:
        self.gh("exit 0\n")
        self.hold("vm-a")
        proc = self.run_bash(self.script(self.acquire_line(), exact="echo 2"))
        self.assertIn("rc=0 contended=0 id=lane-1-vm-me", proc.stdout, proc.stderr)
        self.assertIn("job_claim", self.names())

    def test_uncontended_demand_buys_no_exact_count(self) -> None:
        self.gh("exit 0\n")
        proc = self.run_bash(self.script(self.acquire_line()))
        self.assertIn("rc=0 contended=0", proc.stdout, proc.stderr)
        self.assertNotIn("demand:", self.events.read_text())

    def test_fleet_idle_runner_covers_demand(self) -> None:
        runner = json.dumps({"name": "m3-lane-1-1", "labels": LABELS.split(",")})
        self.gh(f"echo {runner!r}\n")
        proc = self.run_bash(self.script(self.acquire_line(), mode="legacy"))
        self.assertIn("rc=75 contended=1", proc.stdout, proc.stderr)

    def test_fleet_listing_can_be_turned_off(self) -> None:
        runner = json.dumps({"name": "m3-lane-1-1", "labels": LABELS.split(",")})
        self.gh(f"echo {runner!r}\n")
        proc = self.run_bash(self.script(self.acquire_line(), mode="legacy"),
                             env={"TARTCI_JOB_CLAIM_FLEET": "0"})
        self.assertIn("rc=0", proc.stdout, proc.stderr)

    def test_fail_open_paths_all_boot(self) -> None:
        cases = {
            "store unavailable": ({"TARTCI_JOB_CLAIM_DIR": "/dev/null/claims"}, "exit 0\n", "echo 1", False),
            "listing fails": ({}, "echo boom >&2; exit 1\n", "echo 1", False),
            "exact count fails": ({}, "exit 0\n", "return 1", True),
            "claims disabled": ({"TARTCI_JOB_CLAIM": "0"}, "exit 0\n", "echo 1", True),
        }
        for name, (env, gh, exact, contend) in cases.items():
            with self.subTest(case=name):
                self.events.unlink(missing_ok=True)
                self.gh(gh)
                if contend:
                    self.hold(f"vm-{len(self.holders)}")
                proc = self.run_bash(self.script(self.acquire_line(), exact=exact), env=env)
                self.assertIn("rc=0 contended=0", proc.stdout, proc.stderr)

    def test_release_frees_the_claim_for_a_sibling(self) -> None:
        self.gh("exit 0\n")
        proc = self.run_bash(self.script(
            self.acquire_line()
            + "tartci_job_claim_release\n"
            + f"python3 {str(CLAIM)!r} status --dir \"$TARTCI_JOB_CLAIM_DIR\"\n"))
        self.assertEqual(json.loads(proc.stdout.splitlines()[-1])["claims"], [], proc.stderr)

    def test_no_queue_count_means_no_claim(self) -> None:
        # --once without tiers passes no count; it boots as before.
        self.gh("exit 0\n")
        proc = self.run_bash(self.script(
            f"tartci_job_claim_acquire vm-me {LABELS!r} 1 '' repos/x && echo boot\n"))
        self.assertIn("boot", proc.stdout, proc.stderr)
        self.assertEqual(self.names(), [])


class RunOneTests(unittest.TestCase):
    """The real run_one body: covered demand stops before admission and the lease."""

    def run_one(self, queued: str, hold: bool) -> tuple[subprocess.CompletedProcess, object]:
        from unittest import mock

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_admission_precheck as precheck

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        claims = tmp / "claims"
        if hold:
            holder = Sleeper()
            self.addCleanup(holder.stop)
            subprocess.run(
                [sys.executable, "-B", str(CLAIM), "acquire", "--dir", str(claims),
                 "--repo", precheck.REPO, "--labels", precheck.LABELS,
                 "--claim-id", "sibling", "--lane", "sibling", "--vm", "sibling-vm",
                 "--pid", str(holder.pid), "--queued", "1"],
                check=True, capture_output=True)
        harness = precheck.RunOneHarness(tmp)
        harness.stub_shipyard(precheck.make_envelope("admit", "clean"), 0)
        env = {"TARTCI_JOB_CLAIM_DIR": str(claims), "GH_CLI": "false",
               "CURRENT_SELECTED_QUEUED": queued}
        with mock.patch.dict(os.environ, env):
            return harness.run(), harness

    def test_covered_demand_returns_before_admission_and_clone(self) -> None:
        result, harness = self.run_one("1", hold=True)
        self.assertEqual(result.returncode, 75, result.stderr)
        names = harness.event_names()
        self.assertIn("job_claim_contended", names)
        self.assertNotIn("admission_precheck", names)
        self.assertNotIn("clone_start", names)
        self.assertIn("job-claim-covered", harness.heartbeats.read_text())

    def test_the_control_uncovered_demand_reaches_the_clone(self) -> None:
        import test_admission_precheck as precheck
        result, harness = self.run_one("2", hold=True)
        self.assertEqual(result.returncode, precheck.CLONE_REACHED_EXIT, result.stderr)
        self.assertIn("job_claim", harness.event_names())


class ProviderWiringTests(unittest.TestCase):
    def test_the_claim_precedes_admission_and_the_clone(self) -> None:
        source = RUNNER.read_text()
        start = source.index("run_one(){")
        claim = source.index("tartci_job_claim_acquire", start)
        precheck = source.index('precheck_json="$(tartci_admission_clean', start)
        lease = source.index("tartci_acquire_vm_lease", start)
        clone = source.index("event clone_start", start)
        self.assertLess(claim, precheck)
        self.assertLess(claim, lease)
        self.assertLess(claim, clone)

    def test_the_claim_is_released_on_assignment_after_run_one_and_in_cleanup(self) -> None:
        source = RUNNER.read_text()
        assigned = source.index("grep -q 'Running job:'")
        event = source.index("event job_assigned", assigned)
        running = source.index("heartbeat job-running", event)
        self.assertIn("tartci_job_claim_release", source[event:running])
        call = source.index('run_one "$i" "$selected_labels" "$selected_tier" || run_rc=$?')
        self.assertIn("tartci_job_claim_release", source[call:call + 2500])
        cleanup = source.index("cleanup(){")
        self.assertIn("tartci_job_claim_release", source[cleanup:source.index("\n}\n", cleanup)])

    def test_covered_demand_clears_the_serving_blocked_streak(self) -> None:
        # Run the shipped accounting block: a lane that declined because its
        # demand was covered must not read as a lane that failed to serve.
        import re
        source = RUNNER.read_text()
        block = re.search(
            r'run_one "\$i" "\$selected_labels" "\$selected_tier" \|\| run_rc=\$\?\n'
            r'(?P<block>(?:.*?\n)*?      fi\n)', source).group("block")

        def streak(contended: str) -> str:
            script = (
                "set -euo pipefail\n"
                'SERVING_BLOCKED_SINCE="x"\nSERVING_BLOCKED_STREAK=3\n'
                'SERVING_BLOCKED_LAST_PHASE="p"\nLAST_HEARTBEAT_PHASE=q\n'
                f"CURRENT_SERVED=0\nJOB_CLAIM_CONTENDED={contended}\nrun_rc=75\n"
                + block + 'echo "$SERVING_BLOCKED_STREAK"\n')
            return subprocess.run(["/bin/bash", "-c", script], capture_output=True,
                                  text=True, check=True).stdout.strip()

        self.assertEqual(streak("1"), "0")
        self.assertEqual(streak("0"), "4", "the control: an unserved entry still counts")


if __name__ == "__main__":
    unittest.main(verbosity=2)
