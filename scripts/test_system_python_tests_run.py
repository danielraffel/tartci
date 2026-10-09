#!/usr/bin/env python3
"""Tests of modules a 3.9 interpreter runs must run under 3.9, not skip.

The CI job `python-39-tests` runs this suite under Python 3.9, which has no
tomllib, like the hosts' /usr/bin/python3. A test that skips there for want of
tomllib is a test the 3.9 job never ran. For a module a 3.11 interpreter runs
that is fine; for one a 3.9 interpreter runs, it removes exactly the coverage
the job exists for. This test names the second set and fails on any
tomllib-conditional skip in its tests that is not listed with the 3.11-only
behaviour it guards.

The set ("class 1") is every module reachable by import from:

* an explicit /usr/bin/python3 call site (ROOTS below, each with the line
  that proves it), and
* a module that declares itself "3.9-safe" in its own source, because a 3.9
  caller exists.

Modules reached only through a bare `python3` are not in it: the lane,
watchdog and agent plists put Homebrew's python3 (3.11+) ahead of /usr/bin, so
their tests may skip on 3.9.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent

# module -> (file holding the explicit /usr/bin/python3 site, text that proves it)
ROOTS = {
    "tartci_support_manifest": ("scripts/tartci_support_manifest.py",
                                'f"/usr/bin/python3 {shlex.quote(str(verifier))} verify "'),
    "worktree_cleanup": ("providers/common/vm-lease.lib.sh",
                         '/usr/bin/python3 "$TARTCI_ROOT/scripts/worktree_cleanup.py"'),
    "http_connect_ssh_relay": ("scripts/http_connect_ssh_relay.py",
                               "The deployed interpreter is macOS's /usr/bin/python3 (3.9)"),
}
SELF_DECLARED = "3.9-safe"
# The modules whose own source says SELF_DECLARED. Pinned, so that adding or
# dropping the declaration is a visible edit here rather than a silent change
# to which tests must run on 3.9.
EXPECTED_SELF_DECLARED = frozenset({
    "gate_reserve_fit", "home_volume_floor", "launchd_interval_guard", "state_age",
    "vm_dhcp_breaker",
})

LANES = ("3.11-only: asserts a macos_fleet_lanes surface (pool status, validate, render), "
         "which imports tomllib and runs under the lane PATH's python3")
SHIPPED = "3.11-only: parses the shipped profiles/*.toml with tomllib"


def reads_profile(call: str) -> str:
    return (f"3.11-only: {call} reads the fleet profile with tomllib; without it the pass "
            "is disabled (test_no_tomllib_fallbacks)")


# (test file, site) -> the 3.11-only behaviour the skip guards. A site is
# "<module>" for a module-level skip or a module-level `if` on tomllib, else
# "Class.method", "Class" or "function".
ALLOWED = {
    ("test_vm_boot_alert.py",
     "Alert.test_the_host_and_ssh_target_come_from_the_profile"):
        ("3.11-only: vm_boot_alert._alert_host() reads the host id and ssh target from the "
         "fleet profile with tomllib; without it the alert still runs and names the host by "
         "its node name (test_without_a_profile_the_node_name_names_it)"),
    ("test_boot_usage.py",
     "Run.test_profile_thresholds_apply"):
        reads_profile("boot_usage.run()"),
    ("test_boot_usage.py",
     "Run.test_samples_at_most_once_a_day_and_keeps_history"):
        reads_profile("boot_usage.run()"),
    ("test_build_disagreement_watch.py",
     "Disabled.test_control_enabled_runs"):
        reads_profile("build_disagreement_watch.cycle()"),
    ("test_build_disagreement_watch.py",
     "EnabledIncident.test_a_cycle_inside_fifteen_minutes_does_not_run"):
        reads_profile("build_disagreement_watch.cycle()"),
    ("test_build_disagreement_watch.py",
     "EnabledIncident.test_clean_window_is_zero_alarms"):
        reads_profile("build_disagreement_watch.cycle()"),
    ("test_build_disagreement_watch.py",
     "EnabledIncident.test_never_invokes_a_reset_or_any_other_command"):
        reads_profile("build_disagreement_watch.cycle()"),
    ("test_build_disagreement_watch.py",
     "EnabledIncident.test_one_alarm_deduplicated_across_two_cycles"):
        reads_profile("build_disagreement_watch.cycle()"),
    ("test_build_disagreement_watch.py",
     "EnabledIncident.test_realerts_after_the_pair_has_been_absent"):
        reads_profile("build_disagreement_watch.cycle()"),
    ("test_build_disagreement_watch.py",
     "Unreadable.test_findings_from_an_abnormal_exit_never_alarm"):
        reads_profile("build_disagreement_watch.cycle()"),
    ("test_build_disagreement_watch.py",
     "Unreadable.test_github_unreadable_exit_3_is_unknown_not_a_failure"):
        reads_profile("build_disagreement_watch.cycle()"),
    ("test_build_disagreement_watch.py",
     "Unreadable.test_no_plain_gh_is_unknown_and_the_detector_is_not_run"):
        reads_profile("build_disagreement_watch.cycle()"),
    ("test_build_disagreement_watch.py",
     "Unreadable.test_unparseable_and_timeout_are_unknown"):
        reads_profile("build_disagreement_watch.cycle()"),
    ("test_build_disagreement_watch.py",
     "Unreadable.test_unreadable_logs_are_unknown_without_alarm"):
        reads_profile("build_disagreement_watch.cycle()"),
    ("test_disk_reclaim.py",
     "HomeRootGuardTests.roots"):
        "3.11-only: disk_reclaim reads profile roots and the lease volume with tomllib; "
        "without it both are empty (test_no_tomllib_fallbacks)",
    ("test_disk_reclaim.py",
     "LeaseVolumeFloorTests.test_the_fleet_profile_names_the_lease_volume"):
        "3.11-only: disk_reclaim reads profile roots and the lease volume with tomllib; "
        "without it both are empty (test_no_tomllib_fallbacks)",
    ("test_disk_reclaim.py",
     "RootDiscoveryTests.test_profile_reclaim_paths_add_the_external_volume_root"):
        "3.11-only: disk_reclaim reads profile roots and the lease volume with tomllib; "
        "without it both are empty (test_no_tomllib_fallbacks)",
    ("test_gate_ccache_trim.py",
     "Run"):
        reads_profile("gate_ccache_trim.load_settings()"),
    ("test_gate_reserve_fit.py",
     "FitTests.test_m3_m5studio_and_m5_fit_and_m1_overcommits_cores"):
        SHIPPED,
    ("test_gate_reserve_fit.py",
     "FitTests.test_control_m5_without_the_reserve_share_overcommits_cores"):
        SHIPPED,
    ("test_gate_reserve_fit.py",
     "FitTests.test_m5_slots_are_four_cores_each"):
        SHIPPED,
    ("test_gate_reserve_fit.py",
     "M5DoctorTests"):
        LANES,
    ("test_gate_reserve_fit.py",
     "NoReserveTests.test_summary_and_doctor_say_not_applicable_not_fits"):
        LANES,
    ("test_gate_reserve_fit.py",
     "NoReserveTests.test_summary_carries_the_memory_n_a_beside_the_cores_fit"):
        LANES,
    ("test_gate_reserve_fit.py",
     "RatchetTests.test_a_first_install_reports_and_refuses_nothing"):
        SHIPPED,
    ("test_gate_reserve_fit.py",
     "RatchetTests.test_a_smaller_overcommit_passes_and_reports_smaller"):
        SHIPPED,
    ("test_gate_reserve_fit.py",
     "RatchetTests.test_m1_and_the_old_m5_report_on_every_update_and_never_block"):
        SHIPPED,
    ("test_gate_reserve_fit.py",
     "RatchetTests.test_m5_moving_to_the_reserve_share_is_accepted_and_fits"):
        SHIPPED,
    ("test_gate_reserve_fit.py",
     "RatchetTests.test_the_373_sizing_is_refused_against_the_installed_profile"):
        SHIPPED,
    ("test_gate_reserve_fit.py",
     "ValidateCliTests"):
        LANES,
    ("test_home_volume_floor.py",
     "ProfileModeTests"):
        "3.11-only: parses the shipped profiles/*.toml and renders them through "
        "macos_fleet_lanes",
    ("test_host_off.py",
     "SurfaceTests.test_a_check_that_raises_is_a_problem_not_a_clean_bill"):
        LANES,
    ("test_host_off.py",
     "SurfaceTests.test_pool_status_names_the_reason"):
        LANES,
    ("test_host_vitals_sensor.py",
     "DriftTests.test_pool_status_line_only_when_it_is_not_origin_main"):
        LANES,
    ("test_host_vitals_sensor.py",
     "ReclaimWiringTests.test_every_fleet_host_runs_the_pass_that_refreshes_it"):
        SHIPPED,
    ("test_host_vitals_sensor.py",
     "ReclaimWiringTests.test_the_reclaim_pass_refreshes_even_without_a_worktree_root"):
        reads_profile("pulp_reapers.run()"),
    ("test_http_connect_ssh_relay_config.py",
     "HttpConnectSshRelayConfigTests.test_launchd_covers_protected_macos_bootstrap_host_contract"):
        "3.11-only: parses the bootstrap-host contract TOML the launchd template is "
        "checked against; the relay itself reads no TOML",
    ("test_http_connect_ssh_relay_config.py",
     "HttpConnectSshRelayConfigTests.test_launchd_covers_release_node_bootstrap_host_contract"):
        "3.11-only: parses the bootstrap-host contract TOML the launchd template is "
        "checked against; the relay itself reads no TOML",
    ("test_power_status.py",
     "ReadinessTests.test_a_host_set_to_sleep_on_ac_is_not_ready"):
        LANES,
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_a_child_whose_url_cannot_be_read_is_not_counted_and_is_recorded"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_a_clone_of_another_origin_is_not_counted"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_a_second_clone_of_the_same_origin_is_reaped"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_an_unreadable_configured_origin_runs_nothing_outside_and_says_so"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_discovery_that_did_not_run_reads_differently_from_a_zero"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_each_clones_agent_worktrees_get_one_run_when_present"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_the_configured_clones_agent_worktrees_run_once_when_also_discovered"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_the_discovered_root_run_follows_the_pass_mode"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_the_m5s_boot_volume_root_gets_its_own_coverage_run"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_the_profile_root_is_not_run_twice"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_the_real_reaper_clears_coverage_in_the_discovered_root"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_the_receipt_carries_every_field_a_control_reads"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiscoveredPulpRoots.test_two_discovered_roots_get_one_run_each"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiskReclaimIntegration.test_receipt_event_and_log_line_carry_the_pulp_result"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "DiskReclaimIntegration.test_the_reclaim_pass_hands_its_scan_roots_to_the_reapers"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "EndToEnd.test_build_cov_runs_even_without_pressure_but_worktree_reaper_does_not"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "EndToEnd.test_merged_idle_worktree_build_goes_under_pressure_and_the_guarded_ones_stay"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "EndToEnd.test_two_day_old_build_cov_idle_goes_active_stays"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "Materialize.test_origin_without_tools_ci_is_refused_and_nothing_runs"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "OffByDefault.test_no_profile_runs_nothing"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "OffByDefault.test_profile_without_the_table_or_with_it_false_runs_nothing"):
        reads_profile("pulp_reapers.run()"),
    ("test_pulp_reapers.py",
     "Validation.test_fleet_profile_loader_uses_the_same_validator"):
        LANES,
    ("test_pulp_reapers.py",
     "WorktreesInTmp.test_reclaim_pass_reports_it_as_an_event_field"):
        reads_profile("pulp_reapers.run()"),
    ("test_queue_tick_refresh.py",
     "DriftTests.test_pool_status_names_a_stale_copy"):
        LANES,
    ("test_scratch_dirs.py",
     "Settings.test_idle_hours_are_bounded"):
        reads_profile("scratch_dirs.run()"),
    ("test_scratch_dirs.py",
     "Settings.test_pressure_selects_the_shorter_gate"):
        reads_profile("scratch_dirs.run()"),
    ("test_state_age.py",
     "SurfaceTests.test_host_vitals_the_sensor_stopped_refreshing_reads_stale"):
        LANES,
    ("test_tartci_launchd_watchdog.py",
     "<module>"):
        "3.11-only: the watchdog reads a custom Tart store from the installed profile "
        "with tomllib; without it the store comes from TART_HOME only "
        "(test_no_tomllib_fallbacks)",
    ("test_tmp_checkouts.py",
     "MultiRootTests.test_an_extra_root_reaps_worktrees_and_keeps_every_clone"):
        reads_profile("tmp_checkouts.run()"),
    ("test_tmp_checkouts.py",
     "MultiRootTests.test_both_roots_are_swept_and_reported_per_root"):
        reads_profile("tmp_checkouts.run()"),
    ("test_tmp_checkouts.py",
     "MultiRootTests.test_worktree_root_is_opt_in_and_needs_a_root"):
        reads_profile("tmp_checkouts.run()"),
    ("test_vm_dhcp_breaker.py",
     "Wiring.test_the_profile_key_turns_it_off_and_nothing_else"):
        "3.11-only: edits a shipped profile and validates it through macos_fleet_lanes",
}


def imports(module: str) -> set[str]:
    path = SCRIPTS / f"{module}.py"
    tree = ast.parse(path.read_text(), str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return {name for name in names if (SCRIPTS / f"{name}.py").is_file()}


def self_declared() -> set[str]:
    return {path.stem for path in SCRIPTS.glob("*.py")
            if not path.name.startswith("test_") and path.stem != "testing_support"
            and SELF_DECLARED in path.read_text()}


def class_one() -> dict[str, str]:
    """module -> the root it was reached from."""
    seen: dict[str, str] = {}
    stack = [(root, root) for root in sorted(set(ROOTS) | self_declared())]
    while stack:
        module, root = stack.pop()
        if module in seen:
            continue
        seen[module] = root
        stack.extend((name, root) for name in sorted(imports(module)))
    return seen


def test_files(module: str) -> list[Path]:
    return sorted({*SCRIPTS.glob(f"test_{module}.py"), *SCRIPTS.glob(f"test_{module}_*.py")})


def mentions_tomllib(node: ast.AST, source: str) -> bool:
    return "tomllib" in (ast.get_source_segment(source, node) or "").lower()


def skip_sites(path: Path) -> set[str]:
    """Every place this test file skips, or leaves code unrun, for want of tomllib."""
    source = path.read_text()
    tree = ast.parse(source, str(path))
    sites: set[str] = set()

    def visit(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = ".".join([*scope, child.name])
                if any(mentions_tomllib(d, source) for d in child.decorator_list):
                    sites.add(name)
                visit(child, [*scope, child.name])
            elif isinstance(child, ast.If) and mentions_tomllib(child.test, source):
                sites.add(".".join(scope) or "<module>")
                visit(child, scope)
            elif (isinstance(child, ast.Expr) and isinstance(child.value, ast.Call)
                  and "skip_module_without_tomllib" in
                  (ast.get_source_segment(source, child.value.func) or "")):
                sites.add(".".join(scope) or "<module>")
            else:
                visit(child, scope)

    visit(tree, [])
    return sites


class SystemPythonTestsRunTests(unittest.TestCase):
    def test_each_root_is_still_an_explicit_system_python_site(self) -> None:
        for module, (where, text) in ROOTS.items():
            with self.subTest(module=module):
                self.assertTrue((SCRIPTS / f"{module}.py").is_file())
                self.assertIn(text, (ROOT / where).read_text(), where)

    def test_the_self_declared_set_is_pinned(self) -> None:
        self.assertEqual(self_declared(), EXPECTED_SELF_DECLARED)

    def test_class_one_tests_skip_only_for_listed_3_11_only_behaviour(self) -> None:
        found = {(path.name, site): module
                 for module in class_one() for path in test_files(module)
                 for site in skip_sites(path)}
        unlisted = sorted(f"{name}: {site} (tests {module})"
                          for (name, site), module in found.items()
                          if (name, site) not in ALLOWED)
        self.assertEqual(unlisted, [], "these tests of a module a 3.9 interpreter runs skip "
                         "without tomllib; make them run on 3.9, or list the 3.11-only "
                         "behaviour each guards in ALLOWED")
        stale = sorted(f"{name}: {site}" for name, site in ALLOWED if (name, site) not in found)
        self.assertEqual(stale, [], "ALLOWED names a skip that is gone, or a module no longer "
                         "in the set")
        for key, reason in ALLOWED.items():
            self.assertTrue(reason.startswith("3.11-only: ") and len(reason) > 20, key)

    def test_the_scan_sees_each_skip_form(self) -> None:
        sample = (
            "import testing_support\n"
            "testing_support.skip_module_without_tomllib()\n"
            "class A:\n"
            "    @testing_support.requires_tomllib\n"
            "    def test_x(self): pass\n"
            "    @unittest.skipUnless(HAVE_TOMLLIB, 'r')\n"
            "    def test_y(self): pass\n"
            "    def test_z(self):\n"
            "        if mod.tomllib is None:\n"
            "            self.skipTest('r')\n"
            "@unittest.skipIf(tomllib is None, 'r')\n"
            "class B: pass\n"
            "if testing_support.HAVE_TOMLLIB:\n"
            "    pass\n"
        )
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test_sample.py"
            path.write_text(sample)
            self.assertEqual(skip_sites(path),
                             {"<module>", "A.test_x", "A.test_y", "A.test_z", "B"})

    def test_a_bare_python3_module_is_not_class_one(self) -> None:
        # macos_fleet_lanes needs tomllib and runs under the lane PATH's python3.
        self.assertNotIn("macos_fleet_lanes", class_one())


if __name__ == "__main__":
    unittest.main()
