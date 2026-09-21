#!/usr/bin/env python3
"""Cover every fleet_doctor check with the fault present AND the fault absent.

A check that can only pass is not a check. Each condition below is exercised
twice against the same code path: once on a fixture carrying the fault, which
must be reported, and once on a fixture without it, which must not raise an
alarm. The good cell is what proves the check discriminates rather than fires.
"""

from __future__ import annotations

import json
import os
import pathlib
import plistlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fleet_doctor as fd  # noqa: E402
import host_profile as hp  # noqa: E402

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
MANIFEST_A = "1" * 64
MANIFEST_B = "2" * 64
FLEET_LABEL = hp.FLEET_LABEL_PREFIX + "studio.pulp-gate"


def gen_root(home: pathlib.Path) -> pathlib.Path:
    """The generations root shape host_profile's classifier recognises."""
    return home / ".local" / "share" / "tartci-generations"


def installed(home: pathlib.Path, commit: str = COMMIT_A,
              manifest: str = MANIFEST_A) -> dict:
    root = gen_root(home) / f"{commit}-{manifest[:16]}"
    return {"source_commit": commit, "support_manifest_sha256": manifest,
            "root": str(root), "launch_entrypoint": str(root / ".tartci-launch")}


def write_generation(home: pathlib.Path, commit: str, manifest: str) -> str:
    """Stage a generation on disk and return the entrypoint a lane would exec."""
    root = gen_root(home) / f"{commit}-{manifest[:16]}"
    root.mkdir(parents=True, exist_ok=True)
    (root / hp.GENERATION_MANIFEST).write_text(json.dumps({
        "schema": 1, "repository": "danielraffel/tartci",
        "source_commit": commit, "members": [{"name": "tartci"}]}))
    entrypoint = root / ".tartci-launch"
    entrypoint.write_text("#!/bin/sh\n")
    return str(entrypoint)


def write_bundle(home: pathlib.Path, commit: str, manifest: str) -> str:
    """Install a sealed launcher bundle and return the program a lane would exec."""
    bundle = home / ".local" / "libexec" / "TartCILauncher.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (bundle / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    (bundle / hp.SEALED_BUNDLE_MARKER).write_text(json.dumps({
        "schema": 1, "source_commit": commit,
        "support_manifest_sha256": manifest,
        "profile_policy_sha256": "0" * 64, "tart_home": "/Volumes/Workshop/VMs"}))
    program = bundle / "Contents" / "MacOS" / "tartci-launcher"
    program.write_text("#!/bin/sh\n")
    return str(program)


def write_agent(agents: pathlib.Path, label: str, program: str,
                environment: dict | None = None) -> None:
    agents.mkdir(parents=True, exist_ok=True)
    job = {"Label": label, "ProgramArguments": [program, "--lane", "x"]}
    if environment is not None:
        job["EnvironmentVariables"] = environment
    (agents / f"{label}.plist").write_bytes(plistlib.dumps(job))


def delivery(home: pathlib.Path) -> dict:
    """The real host_profile delivery report over a fixture host."""
    return hp.build_delivery_report(
        agents=home / "Library" / "LaunchAgents", repo_root=home)


# ── 1. Which generation the host actually execs ────────────────────────────


class ExecutedGenerationTests(unittest.TestCase):
    def test_bad_sealed_bundle_carries_an_older_cohort(self):
        """The silent failure: a receipt names a cohort nothing execs."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            write_generation(home, COMMIT_A, MANIFEST_A)
            program = write_bundle(home, COMMIT_B, MANIFEST_B)
            write_agent(home / "Library" / "LaunchAgents", FLEET_LABEL, program)
            finding = fd.check_executed_generation(
                delivery(home), installed(home, COMMIT_A, MANIFEST_A))
        self.assertEqual(finding.state, fd.PROBLEM)
        self.assertEqual(finding.code, "effective_generation_mismatch")
        effective = finding.facts["effective_generation"][FLEET_LABEL]
        self.assertEqual(effective["source_commit"], COMMIT_B)
        self.assertFalse(effective["executes_installed"])

    def test_good_sealed_bundle_carries_the_installed_cohort(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            write_generation(home, COMMIT_A, MANIFEST_A)
            program = write_bundle(home, COMMIT_A, MANIFEST_A)
            write_agent(home / "Library" / "LaunchAgents", FLEET_LABEL, program)
            finding = fd.check_executed_generation(
                delivery(home), installed(home, COMMIT_A, MANIFEST_A))
        self.assertEqual(finding.state, fd.OK)
        self.assertEqual(finding.code, "effective_generation_matches")

    def test_bad_direct_exec_points_at_a_superseded_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            write_generation(home, COMMIT_A, MANIFEST_A)
            stale = write_generation(home, COMMIT_B, MANIFEST_B)
            write_agent(home / "Library" / "LaunchAgents", FLEET_LABEL, stale)
            finding = fd.check_executed_generation(delivery(home), installed(home))
        self.assertEqual(finding.state, fd.PROBLEM)
        self.assertEqual(finding.code, "effective_generation_mismatch")

    def test_good_direct_exec_points_at_the_installed_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            current = write_generation(home, COMMIT_A, MANIFEST_A)
            write_agent(home / "Library" / "LaunchAgents", FLEET_LABEL, current)
            finding = fd.check_executed_generation(delivery(home), installed(home))
        self.assertEqual(finding.state, fd.OK)

    def test_unreadable_sealed_metadata_is_unknown_not_agreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            write_generation(home, COMMIT_A, MANIFEST_A)
            program = write_bundle(home, COMMIT_A, MANIFEST_A)
            (home / ".local" / "libexec" / "TartCILauncher.app"
             / hp.SEALED_BUNDLE_MARKER).write_text("{not json")
            write_agent(home / "Library" / "LaunchAgents", FLEET_LABEL, program)
            finding = fd.check_executed_generation(delivery(home), installed(home))
        self.assertEqual(finding.state, fd.UNKNOWN)
        self.assertEqual(finding.code, "program_unresolvable")

    def test_missing_receipt_with_agents_is_unknown_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            current = write_generation(home, COMMIT_A, MANIFEST_A)
            write_agent(home / "Library" / "LaunchAgents", FLEET_LABEL, current)
            finding = fd.check_executed_generation(delivery(home), None)
        self.assertEqual(finding.state, fd.UNKNOWN)
        self.assertEqual(finding.code, "installed_generation_unknown")

    def test_unmanaged_host_is_not_applicable_not_a_fault(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            write_agent(home / "Library" / "LaunchAgents",
                        "com.danielraffel.pulp.tart-runner-linux", "/bin/true")
            finding = fd.check_executed_generation(delivery(home), None)
        self.assertEqual(finding.state, fd.NOT_APPLICABLE)

    def test_unreadable_agents_dir_is_unknown_not_an_empty_host(self):
        """No plists at all is blindness; no FLEET plists is a bare host."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            finding = fd.check_executed_generation(delivery(home), None)
        self.assertEqual(finding.state, fd.UNKNOWN)
        self.assertEqual(finding.code, "agents_dir_unreadable")


# ── 2. Can this host receive a deploy at all ───────────────────────────────


class GenerationDeliveryTests(unittest.TestCase):
    def test_bad_sealed_bundle_cannot_receive_a_staged_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            program = write_bundle(home, COMMIT_A, MANIFEST_A)
            write_agent(home / "Library" / "LaunchAgents", FLEET_LABEL, program)
            finding = fd.check_generation_delivery(delivery(home))
        self.assertEqual(finding.state, fd.PROBLEM)
        self.assertEqual(finding.code, "sealed_launcher_bundle")
        self.assertIs(finding.facts["can_receive_generation"], False)
        self.assertTrue(finding.facts["how_to_update"])

    def test_good_direct_exec_can_receive_a_staged_generation(self):
        """A sealed bundle is the fault; a generation-path exec must not fire."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            current = write_generation(home, COMMIT_A, MANIFEST_A)
            write_agent(home / "Library" / "LaunchAgents", FLEET_LABEL, current)
            finding = fd.check_generation_delivery(delivery(home))
        self.assertEqual(finding.state, fd.OK)
        self.assertIs(finding.facts["can_receive_generation"], True)

    def test_one_sealed_lane_makes_the_whole_host_undeliverable(self):
        """A mixed host cannot be updated by a stage, whatever its other lanes do."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            agents = home / "Library" / "LaunchAgents"
            current = write_generation(home, COMMIT_A, MANIFEST_A)
            write_agent(agents, FLEET_LABEL, current)
            program = write_bundle(home, COMMIT_A, MANIFEST_A)
            write_agent(agents, hp.FLEET_LABEL_PREFIX + "studio.forge-gate", program)
            finding = fd.check_generation_delivery(delivery(home))
        self.assertEqual(finding.state, fd.PROBLEM)
        self.assertIs(finding.facts["can_receive_generation"], False)

    def test_unresolvable_program_reports_unknown_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            write_agent(home / "Library" / "LaunchAgents", FLEET_LABEL,
                        "/usr/local/bin/something-else")
            finding = fd.check_generation_delivery(delivery(home))
        self.assertEqual(finding.state, fd.UNKNOWN)
        self.assertIsNone(finding.facts["can_receive_generation"])


# ── 3. Can this host drain ─────────────────────────────────────────────────


class DrainCapabilityTests(unittest.TestCase):
    def test_bad_persistent_listeners_without_a_hold_receipt(self):
        """Drain refuses AFTER opting the host out, so this must be known first."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            agents = root / "LaunchAgents"
            agents.mkdir()
            write_agent(agents, "actions.runner.org-repo.studio-01", "/bin/true")
            finding = fd.check_drain_capability(agents, root / "hold")
        self.assertEqual(finding.state, fd.PROBLEM)
        self.assertEqual(finding.code, "persistent_runners_without_hold_receipt")
        self.assertIs(finding.facts["can_drain"], False)

    def test_good_persistent_listeners_with_an_exact_hold_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            agents = root / "LaunchAgents"
            agents.mkdir()
            write_agent(agents, "actions.runner.org-repo.studio-01", "/bin/true")
            hold = root / "hold"
            hold.write_text("held-idle\n")
            finding = fd.check_drain_capability(agents, hold)
        self.assertEqual(finding.state, fd.OK)
        self.assertIs(finding.facts["can_drain"], True)

    def test_good_host_with_no_persistent_listeners_can_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            agents = root / "LaunchAgents"
            agents.mkdir()
            write_agent(agents, FLEET_LABEL, "/bin/true")
            finding = fd.check_drain_capability(agents, root / "hold")
        self.assertEqual(finding.state, fd.OK)
        self.assertEqual(finding.code, "no_persistent_runners")

    def test_bad_near_miss_receipt_is_not_a_weaker_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            agents = root / "LaunchAgents"
            agents.mkdir()
            write_agent(agents, "actions.runner.org-repo.studio-01", "/bin/true")
            hold = root / "hold"
            hold.write_text("held-idle-ish\n")
            finding = fd.check_drain_capability(agents, hold)
        self.assertEqual(finding.state, fd.PROBLEM)
        self.assertIs(finding.facts["can_drain"], False)

    def test_disabled_sibling_plist_is_not_an_installed_listener(self):
        """A `.plist.disabled` file must not invent a drain obligation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            agents = root / "LaunchAgents"
            agents.mkdir()
            (agents / "actions.runner.org-repo.studio-01.plist.disabled").write_text("x")
            finding = fd.check_drain_capability(agents, root / "hold")
        self.assertEqual(finding.state, fd.OK)
        self.assertEqual(finding.code, "no_persistent_runners")


# ── 4. Runner census across both scopes ────────────────────────────────────


class FakeRecord:
    """Mirrors runner_census.RunnerRecord's interface, not its serialization.

    `online` is a property derived from status there and is absent from
    as_dict(), so a check that read the serialized form would count zero online
    forever. The fake carries the interface so the test can catch that.
    """

    def __init__(self, name, status="online", labels=()):
        self.name, self.status, self.labels = name, status, tuple(labels)

    @property
    def online(self):
        return self.status == "online"


class FakeScope:
    def __init__(self, scope, endpoint, reachable, runners=(), error=""):
        self.scope, self.endpoint = scope, endpoint
        self.reachable, self.runners, self.error = reachable, tuple(runners), error


class FakeCensus:
    def __init__(self, repo, scopes):
        self.repo, self.scopes = repo, tuple(scopes)

    @property
    def runners(self):
        return tuple(record for scope in self.scopes for record in scope.runners)

    @property
    def complete(self):
        return all(scope.reachable for scope in self.scopes)

    def unreachable_detail(self):
        return "; ".join(f"{s.scope} scope ({s.endpoint}): {s.error}"
                         for s in self.scopes if not s.reachable)


class RunnerCensusTests(unittest.TestCase):
    def test_bad_organization_scope_unreachable_is_never_a_count(self):
        """Repo scope alone silently omits org runners, so it cannot be quoted."""
        census = FakeCensus("org/repo", [
            FakeScope("repository", "repos/org/repo/actions/runners", True,
                      [FakeRecord("r1")]),
            FakeScope("organization", "orgs/org/actions/runners", False,
                      error="http_403"),
        ])
        finding = fd.check_runner_census(census, repo="org/repo")
        self.assertEqual(finding.state, fd.UNKNOWN)
        self.assertEqual(finding.code, "census_incomplete")
        self.assertIn("orgs/org/actions/runners", finding.detail)

    def test_good_both_scopes_read_reports_the_merged_population(self):
        census = FakeCensus("org/repo", [
            FakeScope("repository", "repos/org/repo/actions/runners", True,
                      [FakeRecord("r1"), FakeRecord("r2", status="offline")]),
            FakeScope("organization", "orgs/org/actions/runners", True,
                      [FakeRecord("o1")]),
        ])
        finding = fd.check_runner_census(census, repo="org/repo")
        self.assertEqual(finding.state, fd.OK)
        self.assertEqual(finding.facts["total_registered"], 3)
        self.assertEqual(finding.facts["online"], 2)

    def test_zero_online_at_idle_is_reported_as_normal_not_as_a_dead_host(self):
        """Zero is the expected idle reading for ephemeral runners."""
        census = FakeCensus("org/repo", [
            FakeScope("repository", "repos/org/repo/actions/runners", True),
            FakeScope("organization", "orgs/org/actions/runners", True),
        ])
        finding = fd.check_runner_census(census, repo="org/repo")
        self.assertEqual(finding.state, fd.OK)
        self.assertEqual(finding.facts["total_registered"], 0)
        self.assertTrue(finding.facts["idle_zero_is_normal"])
        self.assertIn("ZERO ONLINE AT IDLE IS NORMAL", finding.detail)

    def test_unreachable_scope_reports_an_unknown_count_not_zero(self):
        """A scope that was not read has no count; zero would read as measured."""
        census = FakeCensus("org/repo", [
            FakeScope("repository", "repos/org/repo/actions/runners", True,
                      [FakeRecord("r1")]),
            FakeScope("organization", "orgs/org/actions/runners", False,
                      error="http_403"),
        ])
        facts = fd.check_runner_census(census, repo="org/repo").facts
        self.assertIsNone(facts["scopes"]["organization"]["registered"])
        self.assertEqual(facts["scopes"]["repository"]["registered"], 1)

    def test_online_is_counted_from_the_record_interface_not_a_payload_key(self):
        """RunnerRecord.online is a property absent from its serialized form.

        Counting it off a serialized key yields zero online forever, which is
        the same confident-undercount this check exists to prevent.
        """
        census = FakeCensus("org/repo", [
            FakeScope("repository", "repos/org/repo/actions/runners", True,
                      [FakeRecord("r1"), FakeRecord("r2", status="offline"),
                       FakeRecord("r3")]),
            FakeScope("organization", "orgs/org/actions/runners", True),
        ])
        facts = fd.check_runner_census(census, repo="org/repo").facts
        self.assertEqual(facts["total_registered"], 3)
        self.assertEqual(facts["online"], 2)

    def test_census_object_missing_the_interface_is_unknown_not_zero(self):
        class Drifted:
            repo = "org/repo"
            scopes = ()

        finding = fd.check_runner_census(Drifted(), repo="org/repo")
        self.assertEqual(finding.state, fd.UNKNOWN)
        self.assertEqual(finding.code, "census_incomplete")
        self.assertNotIn("total_registered", finding.facts)

    def test_absent_census_module_is_unknown_not_zero_runners(self):
        finding = fd.check_runner_census(
            None, repo="org/repo", error_code="census_module_unavailable")
        self.assertEqual(finding.state, fd.UNKNOWN)
        self.assertNotIn("total_registered", finding.facts)


class LaneRegistrationTests(unittest.TestCase):
    def test_census_targets_come_from_the_installed_plists(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = pathlib.Path(tmp) / "LaunchAgents"
            agents.mkdir()
            write_agent(agents, FLEET_LABEL, "/bin/true", {
                "TARTCI_RUNNER_REPO": "org/repo",
                "TARTCI_RUNNER_LABELS": "self-hosted,macOS,pulp-build-vm"})
            rows = fd.lane_registrations(agents)
        self.assertEqual(rows[FLEET_LABEL]["repo"], "org/repo")
        self.assertIn("pulp-build-vm", rows[FLEET_LABEL]["labels"])

    def test_lane_without_a_declared_repo_yields_no_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = pathlib.Path(tmp) / "LaunchAgents"
            agents.mkdir()
            write_agent(agents, FLEET_LABEL, "/bin/true", {})
            rows = fd.lane_registrations(agents)
        self.assertIsNone(rows[FLEET_LABEL]["repo"])


# ── 5. Readiness, and whether the instruments agree ────────────────────────


class ReadinessTests(unittest.TestCase):
    def test_bad_two_support_roots_disagree_for_the_same_host(self):
        """A checkout and the installed generation answering differently."""
        probes = {
            "/gen/root": {"managed": True, "fleet_ready": True,
                          "verified_running_supervisors": 5,
                          "expected_supervisors": 5, "problems": []},
            "/checkout": {"managed": True, "fleet_ready": False,
                          "verified_running_supervisors": 0,
                          "expected_supervisors": None,
                          "problems": [{"code": "receipt_mismatch"}]},
        }
        finding = fd.check_readiness(probes, authority="/gen/root")
        self.assertEqual(finding.state, fd.PROBLEM)
        self.assertEqual(finding.code, "readiness_verdict_depends_on_invocation")
        self.assertIn("/checkout", finding.detail)
        self.assertIn("/gen/root", finding.detail)

    def test_good_two_support_roots_agreeing_reports_the_fleet_verdict(self):
        probes = {
            "/gen/root": {"managed": True, "fleet_ready": True,
                          "verified_running_supervisors": 5,
                          "expected_supervisors": 5, "problems": []},
            "/checkout": {"managed": True, "fleet_ready": True,
                          "verified_running_supervisors": 5,
                          "expected_supervisors": 5, "problems": []},
        }
        finding = fd.check_readiness(probes, authority="/gen/root")
        self.assertEqual(finding.state, fd.OK)
        self.assertEqual(finding.code, "fleet_ready")

    def test_bad_not_ready_surfaces_the_machine_readable_problem_code(self):
        probes = {"/gen/root": {
            "managed": True, "fleet_ready": False,
            "verified_running_supervisors": 0, "expected_supervisors": 5,
            "problems": [{"code": "persistent_loaded_receipt_mismatch"}]}}
        finding = fd.check_readiness(probes, authority="/gen/root")
        self.assertEqual(finding.state, fd.PROBLEM)
        self.assertEqual(finding.code, "fleet_not_ready")
        self.assertIn("persistent_loaded_receipt_mismatch", finding.detail)

    def test_good_ready_single_root_does_not_alarm(self):
        probes = {"/gen/root": {
            "managed": True, "fleet_ready": True,
            "verified_running_supervisors": 5, "expected_supervisors": 5,
            "problems": []}}
        finding = fd.check_readiness(probes, authority="/gen/root")
        self.assertEqual(finding.state, fd.OK)

    def test_one_failed_probe_is_not_reported_as_a_disagreement(self):
        """A root with no verdict has no opinion, so it cannot be a split."""
        probes = {
            "/gen/root": {"managed": True, "fleet_ready": True,
                          "verified_running_supervisors": 5,
                          "expected_supervisors": 5, "problems": []},
            "/checkout": {"error": "support root carries no readiness script"},
        }
        finding = fd.check_readiness(probes, authority="/gen/root")
        self.assertEqual(finding.state, fd.OK)
        self.assertEqual(finding.code, "fleet_ready")

    def test_probe_error_is_unknown_not_not_ready(self):
        probes = {"/gen/root": {"error": "no tomllib interpreter"}}
        finding = fd.check_readiness(probes, authority="/gen/root")
        self.assertEqual(finding.state, fd.UNKNOWN)
        self.assertEqual(finding.code, "readiness_probe_failed")

    def test_unmanaged_host_is_not_applicable(self):
        probes = {"/gen/root": {"managed": False, "fleet_ready": None,
                                "problems": []}}
        finding = fd.check_readiness(probes, authority="/gen/root")
        self.assertEqual(finding.state, fd.NOT_APPLICABLE)


# ── Reason table, rendering, exit codes ────────────────────────────────────


class ReasonTableTests(unittest.TestCase):
    def test_every_emitted_code_carries_a_row(self):
        """A code with no row is a code whose meaning lives in one person's head."""
        reasons = fd.load_reasons()
        self.assertTrue(reasons, "the reason table must be readable")
        missing = [code for code in fd.CODES if code not in reasons]
        self.assertEqual(missing, [])

    def test_no_orphan_rows(self):
        reasons = fd.load_reasons()
        orphans = [code for code in reasons if code not in fd.CODES]
        self.assertEqual(orphans, [])

    def test_destructive_states_carry_a_do_not(self):
        """The states an operator is most tempted to 'fix' must say what not to do."""
        reasons = fd.load_reasons()
        for code in ("persistent_runners_without_hold_receipt",
                     "sealed_launcher_bundle",
                     "effective_generation_mismatch"):
            self.assertTrue(reasons[code].get("do_not"), code)
            self.assertTrue(reasons[code].get("why"), code)

    def test_unreadable_table_degrades_citation_not_diagnosis(self):
        self.assertEqual(fd.load_reasons(pathlib.Path("/nonexistent.json")), {})


class DiagnosisTests(unittest.TestCase):
    def test_problem_outranks_unknown_outranks_ok(self):
        findings = [fd.Finding("a", fd.OK, "fleet_ready", ""),
                    fd.Finding("b", fd.UNKNOWN, "census_incomplete", ""),
                    fd.Finding("c", fd.PROBLEM, "sealed_launcher_bundle", "")]
        self.assertEqual(fd.diagnose("h", findings).worst, fd.PROBLEM)
        self.assertEqual(fd.diagnose("h", findings).exit_code(), 1)

    def test_unknown_gets_its_own_exit_code_distinct_from_healthy(self):
        """'Nothing measured' must not exit the same as 'nothing wrong'."""
        unknown = fd.diagnose("h", [fd.Finding("b", fd.UNKNOWN, "census_incomplete", "")])
        healthy = fd.diagnose("h", [fd.Finding("a", fd.OK, "fleet_ready", "")])
        self.assertEqual(unknown.exit_code(), 2)
        self.assertEqual(healthy.exit_code(), 0)
        self.assertNotEqual(unknown.exit_code(), healthy.exit_code())

    def test_render_cites_the_remedy_only_for_unhealthy_findings(self):
        reasons = fd.load_reasons()
        bad = fd.render(fd.diagnose("h", [fd.Finding(
            "drain_capability", fd.PROBLEM,
            "persistent_runners_without_hold_receipt", "will refuse")], reasons))
        good = fd.render(fd.diagnose("h", [fd.Finding(
            "drain_capability", fd.OK, "hold_receipt_present", "covered")], reasons))
        self.assertIn("DO NOT:", bad)
        self.assertNotIn("DO NOT:", good)

    def test_json_and_text_render_the_same_findings(self):
        findings = [fd.Finding("readiness", fd.PROBLEM, "fleet_not_ready", "x")]
        diagnosis = fd.diagnose("h", findings)
        payload = diagnosis.as_dict()
        self.assertEqual(payload["state"], fd.PROBLEM)
        self.assertEqual(payload["findings"][0]["code"], "fleet_not_ready")
        self.assertIn("fleet_not_ready", fd.render(diagnosis))


class PoolRecordTests(unittest.TestCase):
    def test_absent_participation_record_reads_as_participating(self):
        """Opting out is an explicit act; a missing file must not imply it."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(fd.read_pool_records(pathlib.Path(tmp)), ("1", "on"))

    def test_explicit_opt_out_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "native-build-participation").write_text("0\n")
            self.assertEqual(fd.read_pool_records(root), ("0", "off"))

    def test_explicit_state_wins_over_the_participation_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "native-build-participation").write_text("0\n")
            (root / "pool-state").write_text("draining\n")
            self.assertEqual(fd.read_pool_records(root), ("0", "draining"))


if __name__ == "__main__":
    unittest.main()
