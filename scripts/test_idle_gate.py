#!/usr/bin/env python3
"""Behavioral tests for the priority-aware idle gate in providers/tart-macos/runner.sh.

The idle gate lets a SECONDARY macOS lane (e.g. a sanitizer or coverage VM)
yield its VM slot to a higher-priority lane (the required build gate) so it can
never starve it on a shared-cap host — the failure that backed out the Pulp
coverage VM lane. The decision hinges on `priority_demand()`, which counts how
many priority-lane jobs are waiting/running by checking whether each job's
requested labels are a SUBSET of the priority lane's advertised labels (GitHub's
assignment rule: a runner serves a job iff it advertises every requested label).

Rather than assert the source contains a string (which gave false confidence
before — see the inverted-subset bug fixed in Pulp #4087), we EXTRACT the exact
pure matcher snippet from the script and run it against synthetic job data, so
an inverted/incorrect subset check is caught by behavior. No gh/tart needed, so
this runs on any platform in CI.

Run:  python3 scripts/test_idle_gate.py
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "providers" / "tart-macos" / "runner.sh"


class IdleGateWiringTests(unittest.TestCase):
    """Pin the gate-decision contract: the loop must consult priority_demand and
    the feature must default OFF so the primary gate runner is unaffected."""

    def setUp(self) -> None:
        self.body = SCRIPT.read_text(encoding="utf-8")

    def test_script_syntax_is_valid(self) -> None:
        r = subprocess.run(["bash", "-n", str(SCRIPT)],
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_feature_defaults_off(self) -> None:
        # YIELD_WORKFLOW empty by default → priority_demand short-circuits to 0
        # → the boot condition `p -eq 0` is always true → no behavior change for
        # the primary gate runner / release lane.
        self.assertIn('YIELD_WORKFLOW="${TARTCI_YIELD_TO_WORKFLOW_NAME:-}"', self.body)
        self.assertIn('[ -n "$YIELD_WORKFLOW" ] || { printf \'%s\\n\' 0; return 0; }',
                      self.body)

    def test_loop_gate_consults_priority_demand(self) -> None:
        # The boot decision must include the priority-demand==0 clause.
        self.assertIn('p="$(priority_demand)"', self.body)
        self.assertIn('[ "${p:-0}" -eq 0 ]', self.body)

    def test_priority_demand_uses_shared_bounded_scanner(self) -> None:
        self.assertIn('scripts/queue_scan.py"', self.body)
        self.assertIn("--job-statuses queued,in_progress", self.body)
        self.assertIn("--shared-cache-file", self.body)
        self.assertNotIn('actions/runs?status=$st', self.body)


class IdleGateFailClosed(unittest.TestCase):
    """priority_demand must FAIL CLOSED: a gh error → report demand (yield), not 0.

    gh errors cluster during the rate-limit storms when the gate most needs its
    slot, so a fail-open guard would let the secondary boot exactly when that is
    most harmful. Driven through the real `--print-priority-demand` hook with stub
    CLIs so no gh/tart/network is needed.
    """

    def _run(
        self,
        gh_stub_body: str,
        *,
        yield_labels: str = "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm",
    ) -> subprocess.CompletedProcess:
        import stat
        import tempfile
        d = tempfile.mkdtemp()
        tmp = Path(d)
        def _exe(name: str, body: str) -> None:
            p = tmp / name
            p.write_text(body, encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _exe("tart", "#!/usr/bin/env bash\nexit 0\n")
        _exe("stubgh", gh_stub_body)
        base = [b for b in ("/bin", "/usr/bin", "/opt/homebrew/bin", "/usr/local/bin")
                if Path(b).exists()]
        env = {
            "HOME": str(tmp),
            "PATH": os.pathsep.join([str(tmp), *base]),
            "TART_HOME": str(tmp / "vms"),
            "TARTCI_STATE_DIR": str(tmp / "state"),
            "TARTCI_GH_CLI": "stubgh",
            # Idle gate ON so priority_demand actually probes.
            "TARTCI_YIELD_TO_WORKFLOW_NAME": "Build and Test",
            "TARTCI_YIELD_TO_LABELS": yield_labels,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), "--print-priority-demand",
             "--labels", "self-hosted,macOS,ARM64,pulp-coverage-vm-macos"],
            capture_output=True, text=True, check=False, env=env,
        )

    def test_gh_error_reports_demand_not_zero(self) -> None:
        # Stub gh exits non-zero (simulates rate-limit / 5xx).
        r = self._run("#!/usr/bin/env bash\nexit 1\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1",
                         "priority_demand must report demand (yield) on a gh error, "
                         "not fail open to 0")

    def test_no_priority_runs_reports_zero(self) -> None:
        # Stub gh returns a real workflow ID and empty run pages → no demand.
        r = self._run(
            """#!/usr/bin/env bash
case "$*" in
  *rate_limit*) printf '%s\n' '{"resources":{"core":{"limit":15000,"remaining":14999}}}' ;;
  *actions/workflows?per_page=100*) printf '%s\n' '{"workflows":[{"id":99,"name":"Build and Test"}]}' ;;
  *actions/workflows/99/runs*) printf '%s\n' '{"workflow_runs":[]}' ;;
  *) exit 1 ;;
esac
"""
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")

    def test_event_class_selector_yields_to_both_required_classes(self) -> None:
        # Pulp's advisory sanitizer shares M1 with the event-class-v2 gate. Its
        # selector includes both required classes. The idle gate cannot preempt
        # an advisory VM, so PR demand must also preserve the free slot for a
        # merge group that can arrive later.
        stub = """#!/usr/bin/env bash
case "$*" in
  *rate_limit*) printf '%s\n' '{"resources":{"core":{"limit":15000,"remaining":14999}}}' ;;
  *actions/workflows?per_page=100*) printf '%s\n' '{"workflows":[{"id":99,"name":"Build and Test"}]}' ;;
  *actions/workflows/99/runs*status=pending*) printf '%s\n' '{"workflow_runs":[]}' ;;
  *actions/workflows/99/runs*status=queued*) printf '%s\n' '{"workflow_runs":[{"id":101,"name":"Build and Test","created_at":"2099-01-01T00:00:00Z","updated_at":"2099-01-01T00:00:00Z"}]}' ;;
  *actions/workflows/99/runs*status=in_progress*) printf '%s\n' '{"workflow_runs":[]}' ;;
  *actions/runs/101/jobs*) printf '%s\n' '${JOBS_JSON}' ;;
  *) exit 1 ;;
esac
"""
        merge_labels = (
            "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm,"
            "pulp-build-merge-group,pulp-build-pr-head"
        )
        merge_job = (
            '{"jobs":[{"id":201,"status":"queued",'
            '"created_at":"2099-01-01T00:00:00Z","labels":['
            '"self-hosted","macOS","ARM64","pulp-build","pulp-build-vm",'
            '"pulp-build-merge-group"]}]}'
        )
        pr_job = merge_job.replace("pulp-build-merge-group", "pulp-build-pr-head")

        r = self._run(
            stub.replace("${JOBS_JSON}", merge_job), yield_labels=merge_labels
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1")

        r = self._run(
            stub.replace("${JOBS_JSON}", pr_job), yield_labels=merge_labels
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1")

        base_labels = "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm"
        r = self._run(
            stub.replace("${JOBS_JSON}", merge_job), yield_labels=base_labels
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.strip(),
            "0",
            "base-only selector must not claim an event-class-v2 job",
        )


class BoundedYieldTests(unittest.TestCase):
    """TARTCI_YIELD_MAX_WAIT_SECONDS lifts the priority yield once one of this
    lane's OWN queued jobs has waited at least that long.

    Driven through the real `--print-yield-bound` hook, which runs the loop's
    own `select_work` then `yield_bound_reached`, against a stub GitHub that
    serves one queued release job whose created_at is set per case. The release
    lane's workflow-tier shape is used because that is the lane the bound exists
    for (the M5 release lane that never won a slot under steady PR load).
    """

    RELEASE_LABELS = "self-hosted,macOS,ARM64,pulp-build-vm-release"

    def _run(
        self,
        *,
        queued_seconds_ago: int,
        bound: str | None,
        gh_fails: bool = False,
    ) -> subprocess.CompletedProcess:
        import json
        import stat
        import tempfile
        from datetime import datetime, timedelta, timezone

        created = (
            datetime.now(timezone.utc) - timedelta(seconds=queued_seconds_ago)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        run = {"id": 101, "name": "Release CLI",
               "created_at": created, "updated_at": created}
        jobs = {"jobs": [{
            "id": 201, "status": "queued", "created_at": created,
            "labels": self.RELEASE_LABELS.split(",") + ["pulp-release-tagged"],
        }]}
        stub = f"""#!/usr/bin/env bash
case "$*" in
  *rate_limit*) printf '%s\\n' '{{"resources":{{"core":{{"limit":15000,"remaining":14999}}}}}}' ;;
  *actions/workflows?per_page=100*) printf '%s\\n' '{json.dumps({"workflows": [{"id": 77, "name": "Release CLI"}, {"id": 78, "name": "Release-path PR gate"}]})}' ;;
  *actions/workflows/77/runs*status=queued*) printf '%s\\n' '{json.dumps({"workflow_runs": [run]})}' ;;
  *actions/workflows/*/runs*) printf '%s\\n' '{{"workflow_runs":[]}}' ;;
  *actions/runs/101/jobs*) printf '%s\\n' '{json.dumps(jobs)}' ;;
  *) exit 1 ;;
esac
"""
        if gh_fails:
            stub = "#!/usr/bin/env bash\nexit 1\n"
        tmp = Path(tempfile.mkdtemp())
        for name, body in (("tart", "#!/usr/bin/env bash\nexit 0\n"),
                           ("stubgh", stub)):
            path = tmp / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        base = [b for b in ("/bin", "/usr/bin", "/opt/homebrew/bin", "/usr/local/bin")
                if Path(b).exists()]
        env = {
            "HOME": str(tmp),
            "PATH": os.pathsep.join([str(tmp), *base]),
            "TART_HOME": str(tmp / "vms"),
            "TARTCI_STATE_DIR": str(tmp / "state"),
            "TARTCI_GH_CLI": "stubgh",
            "TARTCI_QUEUE_STAGGER_MAX_SECS": "0",
            "TARTCI_GH_IDENTITY_RECEIPT_TTL_SECS": "0",
            "TARTCI_RUNNER_REPO": "Generous-Corp/pulp",
            "TARTCI_RUNNER_LABELS": self.RELEASE_LABELS,
            "TARTCI_RUNNER_WORKFLOW_TIERS":
                "pulp-release-tagged|Release CLI\n"
                "pulp-release-pr-gate|Release-path PR gate",
            "TARTCI_YIELD_TO_WORKFLOW_NAME": "Build and Test",
            "TARTCI_YIELD_TO_LABELS":
                "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm",
        }
        if bound is not None:
            env["TARTCI_YIELD_MAX_WAIT_SECONDS"] = bound
        return subprocess.run(
            ["bash", str(SCRIPT), "--print-yield-bound"],
            capture_output=True, text=True, check=False, env=env,
        )

    def test_a_job_under_the_bound_still_yields(self) -> None:
        r = self._run(queued_seconds_ago=600, bound="2700")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")

    def test_a_job_over_the_bound_takes_the_slot(self) -> None:
        # Also the control for every "0" above and below: the fixture does
        # produce a matching, selectable release job.
        r = self._run(queued_seconds_ago=3600, bound="2700")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1")

    def test_unset_bound_never_lifts_the_yield(self) -> None:
        # The same ten-hour-old job that the bounded lane would serve.
        for bound in (None, "0"):
            with self.subTest(bound=bound):
                r = self._run(queued_seconds_ago=36000, bound=bound)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.strip(), "0")

    def test_a_failed_age_scan_keeps_yielding(self) -> None:
        r = self._run(queued_seconds_ago=36000, bound="2700", gh_fails=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")

    def test_an_invalid_bound_refuses_to_start(self) -> None:
        for bound in ("-5", "45m", "1.5"):
            with self.subTest(bound=bound):
                r = self._run(queued_seconds_ago=0, bound=bound)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("TARTCI_YIELD_MAX_WAIT_SECONDS", r.stderr)

    def test_loop_gate_consults_the_bound_only_when_yielding(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'if [ "${q:-0}" -gt 0 ] && [ "${p:-0}" -gt 0 ] && '
            '[ "$YIELD_MAX_WAIT" -gt 0 ]; then',
            body,
        )
        self.assertIn('yb="$(yield_bound_reached "$selected_labels")"', body)
        self.assertIn(
            '{ [ "${p:-0}" -eq 0 ] || [ "${yb:-0}" -gt 0 ]; }', body
        )
        self.assertIn('note "yield bound reached:', body)


class ClaimDelayTests(unittest.TestCase):
    """TARTCI_CLAIM_DELAY_SECONDS lets a slower host leave queued work to a
    faster one, but only when the faster hosts' published supply can take it.

    Driven through the real `--print-claim-delay` hook. `sleep` is a PATH stub
    that records its argument and can flip the stub GitHub to "job taken", which
    is what a faster host claiming during the delay looks like. The faster host
    is a stub `ssh` that answers `tartci pool supply` with a fixed report, read
    by the real scripts/gate_supply.py.
    """

    LABELS = "self-hosted,macOS,ARM64,pulp-build-vm-release"
    REPO = "Generous-Corp/pulp"

    def _run(self, *, delay, taken_during_sleep=False, gh_fails_after_sleep=False,
             tier_drains_during_sleep=False, peer_free=1, peer_in_flight=0,
             peer_verdict="ok", queued_jobs=1, delay_file=None):
        import json
        import stat
        import tempfile

        run = {"id": 101, "name": "Release CLI",
               "created_at": "2026-01-01T00:00:00Z",
               "updated_at": "2026-01-01T00:00:00Z"}
        jobs = {"jobs": [{
            "id": 201 + n, "status": "queued", "created_at": "2026-01-01T00:00:00Z",
            "labels": self.LABELS.split(",") + ["pulp-release-tagged"],
        } for n in range(queued_jobs)]}
        run2 = dict(run, id=102, name="Release-path PR gate")
        jobs2 = {"jobs": [{
            "id": 202, "status": "queued", "created_at": "2026-01-01T00:00:00Z",
            "labels": self.LABELS.split(",") + ["pulp-release-pr-gate"],
        }]}
        tmp = Path(tempfile.mkdtemp())
        self.tmp = tmp
        gh = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >>"{tmp}/gh.log"
if [ -e "{tmp}/fail" ]; then exit 1; fi
case "$*" in
  *rate_limit*) printf '%s\\n' '{{"resources":{{"core":{{"limit":15000,"remaining":14999}}}}}}' ;;
  *actions/workflows?per_page=100*) printf '%s\\n' '{json.dumps({"workflows": [{"id": 77, "name": "Release CLI"}, {"id": 78, "name": "Release-path PR gate"}]})}' ;;
  *actions/workflows/77/runs*status=queued*)
    if [ -e "{tmp}/taken" ] || [ -e "{tmp}/tier0gone" ]; then printf '%s\\n' '{{"workflow_runs":[]}}'
    else printf '%s\\n' '{json.dumps({"workflow_runs": [run]})}'; fi ;;
  *actions/workflows/78/runs*status=queued*)
    if [ -e "{tmp}/tier0gone" ]; then printf '%s\\n' '{json.dumps({"workflow_runs": [run2]})}'
    else printf '%s\\n' '{{"workflow_runs":[]}}'; fi ;;
  *actions/workflows/*/runs*) printf '%s\\n' '{{"workflow_runs":[]}}' ;;
  *actions/runs/102/jobs*) printf '%s\\n' '{json.dumps(jobs2)}' ;;
  *actions/runs/101/jobs*) printf '%s\\n' '{json.dumps(jobs)}' ;;
  *) exit 1 ;;
esac
"""
        sleep = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >>"{tmp}/sleep.log"
{f': >"{tmp}/taken"' if taken_during_sleep else ':'}
{f': >"{tmp}/fail"' if gh_fails_after_sleep else ':'}
{f': >"{tmp}/tier0gone"' if tier_drains_during_sleep else ':'}
"""
        report = {"schema": "tartci.gate-supply/v1", "verdict": peer_verdict,
                  "reason": None if peer_verdict == "ok" else "lane blind",
                  "host": "m5studio", "repo": self.REPO, "class": "pulp-release-tagged",
                  "free": peer_free, "in_flight": peer_in_flight}
        ssh = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >>"{tmp}/ssh.log"
printf '%s\\n' '{json.dumps(report)}'
"""
        for name, body in (("tart", "#!/usr/bin/env bash\nexit 0\n"),
                           ("stubgh", gh), ("sleep", sleep), ("stubssh", ssh)):
            path = tmp / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        listing = tmp / "launchctl-list.txt"
        listing.write_text("PID\tStatus\tLabel\n", encoding="utf-8")
        (tmp / "agents").mkdir()
        base = [b for b in ("/bin", "/usr/bin", "/opt/homebrew/bin", "/usr/local/bin")
                if Path(b).exists()]
        env = {
            "HOME": str(tmp),
            "PATH": os.pathsep.join([str(tmp), *base]),
            "TART_HOME": str(tmp / "vms"),
            "TARTCI_STATE_DIR": str(tmp / "state"),
            "TARTCI_GH_CLI": "stubgh",
            "TARTCI_QUEUE_STAGGER_MAX_SECS": "0",
            "TARTCI_GH_IDENTITY_RECEIPT_TTL_SECS": "0",
            "TARTCI_RUNNER_REPO": self.REPO,
            "TARTCI_RUNNER_LABELS": self.LABELS,
            "TARTCI_RUNNER_WORKFLOW_TIERS":
                "pulp-release-tagged|Release CLI\n"
                "pulp-release-pr-gate|Release-path PR gate",
            "TARTCI_CLAIM_DELAY_SSH": str(tmp / "stubssh"),
            "TARTCI_FLEET_LAUNCHCTL_LIST_FILE": str(listing),
            "TARTCI_LANE_AGENTS_DIR": str(tmp / "agents"),
        }
        if peer_free is not None:
            env["TARTCI_CLAIM_DELAY_PEERS"] = "m5studio=tartci-m5studio"
        if delay is not None:
            env["TARTCI_CLAIM_DELAY_SECONDS"] = delay
        if delay_file is not None:
            (tmp / "claim-delay").write_text(delay_file, encoding="utf-8")
            env["TARTCI_CLAIM_DELAY_FILE"] = str(tmp / "claim-delay")
        return subprocess.run(
            ["bash", str(SCRIPT), "--print-claim-delay"],
            capture_output=True, text=True, check=False, env=env,
        )

    def _queued_reads(self) -> int:
        log = self.tmp / "gh.log"
        if not log.exists():
            return 0
        return sum(1 for line in log.read_text().splitlines()
                   if "workflows/77/runs" in line and "status=queued" in line)

    def _sleeps(self) -> list:
        log = self.tmp / "sleep.log"
        return log.read_text().split() if log.exists() else []

    def _events(self) -> list:
        import json
        log = self.tmp / "state" / "events.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line]

    def _event(self, kind: str) -> dict:
        matches = [e for e in self._events() if e["event"] == kind]
        self.assertEqual(len(matches), 1, self._events())
        return matches[0]

    def test_delay_zero_or_unset_claims_without_sleeping_or_scanning(self) -> None:
        for delay in (None, "0"):
            with self.subTest(delay=delay):
                r = self._run(delay=delay)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.strip(), "claim")
                self.assertEqual(self._sleeps(), [])
                self.assertEqual(self._queued_reads(), 0)
                self.assertFalse((self.tmp / "ssh.log").exists())

    def test_delay_waits_then_rechecks_once_and_claims_when_still_queued(self) -> None:
        r = self._run(delay="15")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "claim")
        self.assertEqual(self._sleeps(), ["15"])
        # Control: the fixture really serves the queued job, and the delay
        # costs exactly one queue read beyond the preflight's own
        # selection, not one per second or per poll.
        self.assertEqual(self._queued_reads(), 2)
        self.assertIn("peer_free=1", self._event("claim_delay_wait")["detail"])

    def test_job_taken_during_the_delay_means_no_claim(self) -> None:
        r = self._run(delay="15", taken_during_sleep=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "backoff")
        self.assertEqual(self._sleeps(), ["15"])
        self.assertEqual(self._queued_reads(), 2)

    def test_original_tier_drained_while_lower_tier_remains_means_no_claim(self) -> None:
        # The refreshed count is positive, but for a different class than the
        # one the caller selected; claiming would boot the wrong class.
        r = self._run(delay="15", tier_drains_during_sleep=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "backoff")
        self.assertEqual(self._sleeps(), ["15"])

    def test_a_failed_recheck_fails_open(self) -> None:
        r = self._run(delay="15", gh_fails_after_sleep=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "claim")

    def test_no_faster_host_configured_never_sleeps(self) -> None:
        # The NO-GO shape: a delay with nothing to wait for. It must claim at
        # once and say why, with no extra queue read.
        r = self._run(delay="15", peer_free=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "claim")
        self.assertEqual(self._sleeps(), [])
        self.assertEqual(self._queued_reads(), 1)
        self.assertIn("reason=no_faster_hosts_configured",
                      self._event("claim_delay_skipped")["detail"])

    def test_no_free_faster_slot_never_sleeps(self) -> None:
        # The faster host is busy reaching for work (in flight) but has no free
        # slot. gate_supply counts that as cover (`hold`); the delay must not,
        # because nothing there is free to take this job.
        r = self._run(delay="15", peer_free=0, peer_in_flight=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "claim")
        self.assertEqual(self._sleeps(), [])
        detail = self._event("claim_delay_skipped")["detail"]
        self.assertIn("reason=supply_hold", detail)
        self.assertIn("peer_free=0", detail)
        # Control: the same supply with the slot free does sleep.
        r = self._run(delay="15", peer_free=1, peer_in_flight=0)
        self.assertEqual(self._sleeps(), ["15"])

    def test_more_queued_jobs_than_free_faster_slots_never_sleeps(self) -> None:
        r = self._run(delay="15", peer_free=1, queued_jobs=2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "claim")
        self.assertEqual(self._sleeps(), [])
        detail = self._event("claim_delay_skipped")["detail"]
        self.assertIn("demand=2", detail)
        self.assertIn("peer_free=1", detail)
        # Control: as many free faster slots as queued jobs sleeps.
        self._run(delay="15", peer_free=2, queued_jobs=2)
        self.assertEqual(self._sleeps(), ["15"])

    def test_unreadable_or_unhealthy_faster_host_never_sleeps(self) -> None:
        r = self._run(delay="15", peer_free=1, peer_verdict="unknown")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "claim")
        self.assertEqual(self._sleeps(), [])
        self.assertIn("reason=supply_unknown",
                      self._event("claim_delay_skipped")["detail"])

    def test_the_delay_file_overrides_the_startup_value_every_decision(self) -> None:
        # Off by file although the LaunchAgent says 15: no sleep, no scan.
        r = self._run(delay="15", delay_file="0\n")
        self.assertEqual(r.stdout.strip(), "claim")
        self.assertEqual(self._sleeps(), [])
        self.assertEqual(self._queued_reads(), 0)
        # On by file although the LaunchAgent says nothing.
        r = self._run(delay=None, delay_file="20")
        self.assertEqual(r.stdout.strip(), "claim")
        self.assertEqual(self._sleeps(), ["20"])
        # A file that is not a bounded integer is ignored, not trusted.
        for junk in ("abc", "91", "-5", "1.5"):
            with self.subTest(junk=junk):
                self._run(delay="15", delay_file=junk)
                self.assertEqual(self._sleeps(), ["15"])

    def test_the_delay_is_capped_below_the_heartbeat_staleness_floor(self) -> None:
        # No heartbeat is written during the sleep; readers call one older than
        # 120 s dead. A LaunchAgent value above the cap sleeps the cap.
        r = self._run(delay="300")
        self.assertEqual(r.stdout.strip(), "claim")
        self.assertEqual(self._sleeps(), ["90"])

    def test_an_invalid_delay_refuses_to_start(self) -> None:
        for delay in ("-5", "15s", "1.5"):
            with self.subTest(delay=delay):
                r = self._run(delay=delay)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("TARTCI_CLAIM_DELAY_SECONDS", r.stderr)

    def test_loop_delays_only_at_the_claim_point(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('claim_delay_now="$(tartci_claim_delay_seconds)"', body)
        self.assertIn('if [ "$claim_delay_now" -gt 0 ] && [ "${q:-0}" -gt 0 ]', body)
        # A slot a sibling reserved is not a free slot to delay for.
        self.assertIn('reserved="$(tartci_count_reservations)"', body)
        self.assertIn('[ "$slots_held" -lt "$cap" ]', body)
        self.assertLess(
            body.index('"$(claim_delay_gate "$selected_labels" "$selected_tier" "$q")" = backoff'),
            body.index("# Idle gate: boot only when (1)"))

    def test_v2_recheck_bypasses_the_selection_cache(self) -> None:
        # select_work in event-class-v2 mode replays a 120 s selection cache, so
        # re-running it after the delay could never see that a faster host took
        # the work. The recheck must force a live selection.
        body = SCRIPT.read_text(encoding="utf-8")
        gate = body[body.index("claim_delay_gate(){"):body.index("higher_priority_demand(){")]
        self.assertIn('selection="$(tartci_assignment_v2_select 1)"', gate)
        self.assertLess(gate.index('sleep "$delay"'),
                        gate.index('selection="$(tartci_assignment_v2_select 1)"'))


class HostHealthYieldTests(unittest.TestCase):
    """host_health_yield must FAIL OPEN (the deliberate opposite of
    priority_demand): a missing/broken host_vitals probe → boot (0), never a
    stalled runner. Yields (1) only when the probe reports critical (or warn when
    opted in). Driven through the real `--print-host-health` hook with a stub
    host_vitals.sh so no real host metrics are needed."""

    def _run(self, *, vitals_body: str | None, env_extra: dict) -> subprocess.CompletedProcess:
        import stat
        import tempfile
        d = tempfile.mkdtemp()
        tmp = Path(d)
        # A `tart` stub keeps the script's top-level happy even though the
        # --print-host-health hook exits before the VM path.
        (tmp / "tart").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (tmp / "tart").chmod(0o755)
        if vitals_body is not None:
            hv = tmp / "host_vitals.sh"
            hv.write_text(vitals_body, encoding="utf-8")
            hv.chmod(0o755)
        base = [b for b in ("/bin", "/usr/bin", "/opt/homebrew/bin", "/usr/local/bin")
                if Path(b).exists()]
        env = {
            "HOME": str(tmp),
            "PATH": os.pathsep.join([str(tmp), *base]),
            "TART_HOME": str(tmp / "vms"),
            "TARTCI_STATE_DIR": str(tmp / "state"),
            **env_extra,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), "--print-host-health"],
            capture_output=True, text=True, check=False, env=env,
        )

    _CRIT = "#!/usr/bin/env bash\nexit 20\n"
    _WARN = "#!/usr/bin/env bash\nexit 10\n"
    _GREEN = "#!/usr/bin/env bash\nexit 0\n"

    def test_feature_off_prints_zero_without_probing(self) -> None:
        # No TARTCI_HOST_VITALS_YIELD → short-circuit to 0 even if host_vitals
        # would report critical. This is the primary-gate-runner default.
        r = self._run(vitals_body=self._CRIT, env_extra={})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")

    def test_missing_probe_fails_open(self) -> None:
        # Feature ON but host_vitals.sh absent → fail OPEN (0/boot), never wedge.
        r = self._run(vitals_body=None, env_extra={"TARTCI_HOST_VITALS_YIELD": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")

    def test_critical_yields(self) -> None:
        r = self._run(vitals_body=self._CRIT,
                      env_extra={"TARTCI_HOST_VITALS_YIELD": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1")

    def test_green_boots(self) -> None:
        r = self._run(vitals_body=self._GREEN,
                      env_extra={"TARTCI_HOST_VITALS_YIELD": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")

    def test_warn_does_not_yield_by_default(self) -> None:
        # WARN (10) is below the critical bar; without opt-in we keep booting.
        r = self._run(vitals_body=self._WARN,
                      env_extra={"TARTCI_HOST_VITALS_YIELD": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")

    def test_warn_yields_when_opted_in(self) -> None:
        r = self._run(vitals_body=self._WARN,
                      env_extra={"TARTCI_HOST_VITALS_YIELD": "1",
                                 "TARTCI_HOST_VITALS_YIELD_ON_WARN": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1")

    def test_critical_still_yields_with_warn_opt_in(self) -> None:
        r = self._run(vitals_body=self._CRIT,
                      env_extra={"TARTCI_HOST_VITALS_YIELD": "1",
                                 "TARTCI_HOST_VITALS_YIELD_ON_WARN": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1")


class HostHealthWiringTests(unittest.TestCase):
    """Pin the host-health contract: the gate decision is the SHARED helper and the
    loop consults it. The default-off + fail-open policy itself is asserted in
    test_host_health_lib.py (the single source of the extracted decision)."""

    def setUp(self) -> None:
        self.body = SCRIPT.read_text(encoding="utf-8")

    def test_sources_shared_host_health_lib(self) -> None:
        self.assertIn(
            'source "$TARTCI_ROOT/providers/common/host-health.lib.sh"', self.body
        )

    def test_loop_gate_consults_host_health(self) -> None:
        self.assertIn('hh="$(tartci_host_health_yield)"', self.body)
        self.assertIn('[ "${hh:-0}" -eq 0 ]', self.body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
