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
from unittest import mock

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None  # type: ignore[assignment]

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
                ttl: int = 1800, labels: str = LABELS, vm: str | None = None,
                peers: list[dict] | None = None) -> tuple[dict, int]:
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
        if peers is not None:
            path = self.tmp / f"peers-{claim_id}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in peers))
            argv += ["--fleet-claims-file", str(path)]
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


def key(labels: str = LABELS) -> str:
    sys.path.insert(0, str(ROOT / "scripts"))
    import job_claim  # noqa: PLC0415
    return job_claim.claim_key(REPO, labels)


def peer(host: str, *claims: tuple[str, float], max_age: int | None = None,
         labels: str = LABELS) -> dict:
    """One gather-peers line: `host` published these (vm, age_s) claims."""
    status: dict = {"host": host, "claims": [
        {"key": key(labels), "vm": vm, "age_s": age} for vm, age in claims]}
    if max_age is not None:
        status["max_age_s"] = max_age
    return {"host": host, "ok": True, "status": status}


class FleetPeerTests(StoreTests):
    """Another host's lane that has claimed but not minted covers the job too."""

    def test_a_peer_claim_covers_the_job(self) -> None:
        result, rc = self.acquire("a", 1, peers=[peer("m3", ("studio-vm-1", 40))])
        self.assertEqual((result["verdict"], rc), ("contended", 3))
        self.assertEqual(result["fleet_booting"], ["studio-vm-1"])
        self.assertEqual(result["peers_read"], ["m3"])

    def test_the_control_a_second_job_still_boots(self) -> None:
        self.assertEqual(self.acquire("a", 2, peers=[peer("m3", ("studio-vm-1", 40))])[1], 0)

    def test_another_class_on_a_peer_does_not_count(self) -> None:
        other = LABELS.replace("pulp-build-pr-head", "pulp-build-merge-group")
        self.assertEqual(self.acquire("a", 1, peers=[peer("m3", ("vm", 40), labels=other)])[1], 0)

    def test_an_aged_claim_counts_only_within_the_age_its_host_declares(self) -> None:
        # Undeclared: the consumer default bounds it.
        self.assertEqual(self.acquire("a", 1, peers=[peer("m1", ("m1-vm", 1200))])[1], 0)
        self.release("a")
        # m1 declares 1800 because its lanes wait for a lease after claiming.
        result, rc = self.acquire("b", 1, peers=[peer("m1", ("m1-vm", 1200), max_age=1800)])
        self.assertEqual(rc, 3, result)
        # A declaration can never stretch past the claim TTL.
        self.assertEqual(self.acquire("c", 1, ttl=1000,
                                      peers=[peer("m1", ("m1-vm", 1200), max_age=1800)])[1], 0)

    def test_a_claim_dated_in_the_future_does_not_count(self) -> None:
        # A negative age is clock skew between hosts, not a claim in flight.
        self.assertEqual(self.acquire("a", 1, peers=[peer("m3", ("studio-vm-1", -5))])[1], 0)
        self.release("a")
        # The control: the same claim at age zero stands.
        self.assertEqual(self.acquire("b", 1, peers=[peer("m3", ("studio-vm-1", 0))])[1], 3)

    def test_a_peer_claim_that_already_minted_is_counted_once(self) -> None:
        # The runner name IS the VM name (generate-jitconfig name=$vm), so the
        # same identity appears in both inputs and stands once.
        idle = {"name": "studio-vm-1", "labels": LABELS.split(",")}
        result, rc = self.acquire("a", 2, runners=[idle], peers=[peer("m3", ("studio-vm-1", 40))])
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["standing_claims"], 1)
        self.assertEqual(result["fleet_booting"], [])

    def test_unread_or_garbled_peers_count_nothing(self) -> None:
        stale = peer("m6", ("m6-vm", 10))
        stale["ok"] = False   # a peer marked unread never counts, whatever it carries
        rows = [{"host": "m5", "ok": False, "reason": "timeout"},
                {"host": "m3", "ok": True, "status": "nope"},
                stale, {"garbage": 1}]
        result, rc = self.acquire("a", 1, peers=rows)
        self.assertEqual(rc, 0, result)
        self.assertEqual(sorted(result["peers_unread"]), ["m3", "m5", "m6"])
        self.assertEqual(result["fleet_booting"], [])
        (self.tmp / "broken.jsonl").write_text("not json\n")
        self.assertEqual(self.acquire("b", 1, peers=None)[1], 3, "local claim a still stands")

    def test_two_hosts_never_both_refuse_one_job(self) -> None:
        # Each host writes its claim only after reading the other. Every
        # interleaving of (read A, write A, read B, write B) that keeps each
        # host's read before its write leaves at least one claimant.
        orders = [("rA", "wA", "rB", "wB"), ("rA", "rB", "wA", "wB"), ("rA", "rB", "wB", "wA"),
                  ("rB", "wB", "rA", "wA"), ("rB", "rA", "wB", "wA"), ("rB", "rA", "wA", "wB")]
        for order in orders:
            with self.subTest(order=order):
                stores = {"A": self.tmp / f"A-{'-'.join(order)}", "B": self.tmp / f"B-{'-'.join(order)}"}
                seen: dict[str, list] = {}
                verdicts: dict[str, int] = {}
                for step in order:
                    host, other = step[1], "B" if step[1] == "A" else "A"
                    if step[0] == "r":
                        live = subprocess.run(
                            [sys.executable, "-B", str(CLAIM), "status", "--dir", str(stores[other])],
                            capture_output=True, text=True, check=True)
                        claims = json.loads(live.stdout)["claims"]
                        seen[host] = [{"host": other, "ok": True, "status": {"claims": claims}}]
                    else:
                        self.dir = stores[host]
                        verdicts[host] = self.acquire(f"lane-{host}", 1, vm=f"vm-{host}",
                                                      peers=seen[host])[1]
                self.assertIn(0, verdicts.values(), (order, verdicts))


class PeerGatherTests(unittest.TestCase):
    """gather-peers: every other host, never this one, within a hard budget."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.supply = self.tmp / "supply.json"
        self.supply.write_text(json.dumps({"hosts": [
            {"host_id": "m1", "ssh": "m1"}, {"host_id": "studio", "ssh": "m3"},
            {"host_id": "m5"}, {"host_id": "m5studio", "ssh": "m5s"}]}))
        self.calls = self.tmp / "ssh-calls"
        self.ssh = self.tmp / "ssh"
        good = json.dumps({"host": "x", "claims": [], "max_age_s": None})
        self.ssh.write_text(
            "#!/bin/bash\n"
            f"printf '%s\\n' \"$*\" >> {str(self.calls)!r}\n"
            "target=\"${@: -2:1}\"\n"
            f"cat >> {str(self.tmp / 'stdin-seen')!r}\n"
            "case \"$target\" in\n"
            f"  m1) echo '{good}' ;;\n"
            "  m3) sleep 31.731 ;;\n"
            "  tartci-m5) exit 255 ;;\n"
            "  m5s) echo 'not json' ;;\n"
            "esac\n")
        self.ssh.chmod(0o755)

    def gather(self, me: str, read_secs: float = 1.5) -> tuple[list[dict], float]:
        out = self.tmp / "peers.jsonl"
        started = time.monotonic()
        subprocess.run([sys.executable, "-B", str(CLAIM), "gather-peers", "--out", str(out),
                        "--self-host", me, "--supply", str(self.supply),
                        "--read-secs", str(read_secs), "--ssh", str(self.ssh)],
                       capture_output=True, text=True, check=True, timeout=30)
        elapsed = time.monotonic() - started
        return [json.loads(line) for line in out.read_text().splitlines()], elapsed

    def test_never_reads_itself_and_bounds_every_fault(self) -> None:
        rows, elapsed = self.gather("studio")
        by = {row["host"]: row for row in rows}
        self.assertEqual(sorted(by), ["m1", "m5", "m5studio"], "this host is never a peer")
        self.assertTrue(by["m1"]["ok"])
        self.assertEqual(by["m5"]["reason"], "exit_255")
        self.assertEqual(by["m5studio"]["reason"], "unparsable")
        self.assertNotIn(" m3 ", " " + self.calls.read_text().replace("\n", " ") + " ")
        self.assertLess(elapsed, 10, "a hung peer must not hold the gather past its budget")

    def test_a_hung_peer_is_killed_at_the_budget(self) -> None:
        rows, elapsed = self.gather("m1", read_secs=1.0)
        by = {row["host"]: row for row in rows}
        self.assertEqual(by["studio"]["reason"], "timeout")
        self.assertLess(elapsed, 6)
        time.sleep(0.3)
        left = subprocess.run(["pgrep", "-f", "sleep 31.731"], capture_output=True, text=True)
        self.assertEqual(left.stdout.strip(), "", "the straggler's children outlived the budget")

    def test_a_peer_read_never_takes_the_callers_stdin(self) -> None:
        out = self.tmp / "peers.jsonl"
        subprocess.run([sys.executable, "-B", str(CLAIM), "gather-peers", "--out", str(out),
                        "--self-host", "studio", "--supply", str(self.supply),
                        "--read-secs", "1.5", "--ssh", str(self.ssh)],
                       input="the rest of the caller's loop\n", capture_output=True,
                       text=True, check=True, timeout=30)
        self.assertEqual((self.tmp / "stdin-seen").read_text(), "",
                         "a peer's ssh drained the caller's stdin")
        for line in self.calls.read_text().splitlines():
            self.assertEqual(line.split()[0], "-n", line)
        lib = (ROOT / "providers" / "tart-macos" / "job-claim.lib.sh").read_text()
        self.assertIn('</dev/null >/dev/null 2>&1 || : >"$out_file"', lib)

    def test_the_read_is_the_published_status_and_nothing_else(self) -> None:
        self.gather("studio")
        for line in self.calls.read_text().splitlines():
            self.assertIn("BatchMode=yes", line)
            self.assertTrue(line.endswith("~/.local/bin/tartci job-claim status --publish"), line)


class PublishTests(unittest.TestCase):
    @unittest.skipUnless(tomllib, "the host profile is read with tomllib (Python 3.11+)")
    def test_status_publishes_live_claims_with_age_host_and_declared_max_age(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        owner = Sleeper()
        self.addCleanup(owner.stop)
        subprocess.run([sys.executable, "-B", str(CLAIM), "acquire", "--dir", str(tmp / "c"),
                        "--repo", REPO, "--labels", LABELS, "--claim-id", "a", "--lane", "a",
                        "--vm", "vm-a", "--pid", str(owner.pid), "--queued", "1"],
                       check=True, capture_output=True)
        dead = subprocess.Popen(["true"])
        dead.wait()
        other = LABELS.replace("pulp-build-pr-head", "pulp-build-merge-group")
        subprocess.run([sys.executable, "-B", str(CLAIM), "acquire", "--dir", str(tmp / "c"),
                        "--repo", REPO, "--labels", other, "--claim-id", "b", "--lane", "b",
                        "--vm", "vm-b", "--pid", str(dead.pid), "--queued", "1"],
                       check=True, capture_output=True)
        profile = tmp / "profile.toml"
        profile.write_text('schema = 1\n[host]\nid = "m1"\njob_claim_max_age_seconds = 1800\n')
        env = {**os.environ, "TARTCI_FLEET_PROFILE": str(profile)}
        env.pop("TARTCI_RECEIPT_HOST_ID", None)
        out = json.loads(subprocess.run(
            [sys.executable, "-B", str(CLAIM), "status", "--publish", "--dir", str(tmp / "c")],
            capture_output=True, text=True, check=True, env=env).stdout)
        self.assertEqual(out["host"], "m1")
        self.assertEqual(out["max_age_s"], 1800)
        self.assertEqual([c["vm"] for c in out["claims"]], ["vm-a"], "a dead owner is never published")
        self.assertEqual(out["claims"][0]["key"], key())
        self.assertGreaterEqual(out["claims"][0]["age_s"], 0)
        profile.write_text('schema = 1\n[host]\nid = "m3"\n')
        out = json.loads(subprocess.run(
            [sys.executable, "-B", str(CLAIM), "status", "--publish", "--dir", str(tmp / "c")],
            capture_output=True, text=True, check=True, env=env).stdout)
        self.assertIsNone(out["max_age_s"], "a host that declares nothing publishes no age")


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

    def peer_env(self, ssh_body: str) -> dict:
        supply = self.tmp / "supply.json"
        supply.write_text(json.dumps({"hosts": [{"host_id": "me", "ssh": "me"},
                                                {"host_id": "m3", "ssh": "m3"}]}))
        ssh = self.tmp / "ssh"
        ssh.write_text("#!/bin/bash\n"
                       f"printf '%s\\n' \"$*\" >> {str(self.tmp / 'ssh-calls')!r}\n" + ssh_body)
        ssh.chmod(0o755)
        return {"TARTCI_JOB_CLAIM_SSH": str(ssh), "TARTCI_JOB_CLAIM_SUPPLY": str(supply),
                "TARTCI_RECEIPT_HOST_ID": "me", "TARTCI_JOB_CLAIM_FLEET_READ_SECS": "3"}

    def published(self) -> str:
        return json.dumps({"host": "m3", "max_age_s": None, "claims": [
            {"key": key(), "vm": "studio-vm-9", "age_s": 30}]})

    def test_a_peer_claim_stops_the_boot_when_the_lane_opted_in(self) -> None:
        self.gh("exit 0\n")
        env = {**self.peer_env(f"echo {self.published()!r}\n"), "TARTCI_JOB_CLAIM_FLEET_PEERS": "1"}
        proc = self.run_bash(self.script(self.acquire_line(), mode="legacy"), env=env)
        self.assertIn("rc=75 contended=1", proc.stdout, proc.stderr)
        line = [l for l in self.events.read_text().splitlines() if l.startswith("job_claim_contended")][0]
        self.assertIn("fleet_booting=1 peers_unread=0", line)
        calls = (self.tmp / "ssh-calls").read_text()
        self.assertNotIn(" me ", " " + calls.replace("\n", " ") + " ", "this host never reads itself")

    def test_knob_off_reads_no_peer_at_all(self) -> None:
        # Negative control: without the lane opting in there is no outbound
        # read and no fleet_booting, whatever the peers hold.
        self.gh("exit 0\n")
        env = self.peer_env(f"echo {self.published()!r}\n")
        proc = self.run_bash(self.script(self.acquire_line(), mode="legacy"), env=env)
        self.assertIn("rc=0 contended=0", proc.stdout, proc.stderr)
        self.assertFalse((self.tmp / "ssh-calls").exists(), "a knob-off lane made an SSH read")
        self.assertIn("fleet_booting=0", self.events.read_text())

    def test_an_unread_peer_boots_and_is_counted_every_attempt_but_logged_hourly(self) -> None:
        self.gh("exit 0\n")
        env = {**self.peer_env("exit 255\n"), "TARTCI_JOB_CLAIM_FLEET_PEERS": "1"}
        for _ in range(2):
            proc = self.run_bash(self.script(self.acquire_line() + "tartci_job_claim_release\n",
                                             mode="legacy"), env=env)
            self.assertIn("rc=0 contended=0", proc.stdout, proc.stderr)
        text = self.events.read_text()
        self.assertEqual(text.count("job_claim_peer_unread\tpeer=m3 reason=exit_255"), 1)
        self.assertEqual(text.count("peers_unread=1"), 2, "the per-attempt count is on every claim event")

    def test_no_host_identity_reads_no_peer(self) -> None:
        self.gh("exit 0\n")
        env = {**self.peer_env(f"echo {self.published()!r}\n"), "TARTCI_JOB_CLAIM_FLEET_PEERS": "1",
               "TARTCI_RECEIPT_HOST_ID": ""}
        proc = self.run_bash(self.script(self.acquire_line(), mode="legacy"), env=env)
        self.assertIn("rc=0 contended=0", proc.stdout, proc.stderr)
        self.assertFalse((self.tmp / "ssh-calls").exists())

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
        # run_one leases, clones and boots through boot_vm_to_ssh.
        lease = clone = source.index('boot_vm_to_ssh "$i"', start)
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
                "tartci_boundary_proof_abandon(){ :; }\n"
                "tartci_job_claim_release(){ :; }\n"
                'SERVING_BLOCKED_SINCE="x"\nSERVING_BLOCKED_STREAK=3\n'
                'SERVING_BLOCKED_LAST_PHASE="p"\nLAST_HEARTBEAT_PHASE=q\n'
                f"CURRENT_SERVED=0\nJOB_CLAIM_CONTENDED={contended}\nrun_rc=75\n"
                + block + 'echo "$SERVING_BLOCKED_STREAK"\n')
            return subprocess.run(["/bin/bash", "-c", script], capture_output=True,
                                  text=True, check=True).stdout.strip()

        self.assertEqual(streak("1"), "0")
        self.assertEqual(streak("0"), "4", "the control: an unserved entry still counts")



class PeerSupplyTests(unittest.TestCase):
    """gather-peers reads the selected fleet source's supply."""

    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        import job_claim  # noqa: PLC0415
        import test_fleet_source  # noqa: PLC0415 - owns what a valid source is
        self.job_claim, self.fixture = job_claim, test_fleet_source
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        names = ("TARTCI_FLEET_SOURCE_ROOT", "TARTCI_FLEET_SOURCE_COMMIT",
                 "TARTCI_FLEET_SOURCE_REPOSITORY", "TARTCI_ADVERTISED_LABELS")
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in names:
            os.environ.pop(name, None)

    def test_without_a_source_the_checkout_copy_is_read(self) -> None:
        self.assertEqual(ROOT / "fleet" / "advertised-labels.json",
                         self.job_claim.peer_supply(None))
        self.assertEqual(Path("/named.json"), self.job_claim.peer_supply("/named.json"))

    def test_a_configured_source_is_the_default(self) -> None:
        os.environ.update(self.fixture.configured_source(self.tmp / "source"))
        self.assertEqual((self.tmp / "source").resolve() / "fleet" / "advertised-labels.json",
                         self.job_claim.peer_supply(None))

    def test_a_named_supply_other_than_the_source_is_refused(self) -> None:
        os.environ.update(self.fixture.configured_source(self.tmp / "source"))
        with self.assertRaisesRegex(ValueError, "is not the configured fleet source"):
            self.job_claim.peer_supply(str(ROOT / "fleet" / "advertised-labels.json"))

    def test_the_lane_passes_no_supply_unless_one_is_set(self) -> None:
        text = LIB.read_text()
        self.assertIn('${TARTCI_JOB_CLAIM_SUPPLY:+--supply "$TARTCI_JOB_CLAIM_SUPPLY"}', text)
        self.assertNotIn("$TARTCI_ROOT/fleet/advertised-labels.json", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
