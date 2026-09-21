#!/usr/bin/env python3
"""Tests for tartci host resource profile derivation."""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import host_profile


HOST_PROFILE_PATH = Path(host_profile.__file__).resolve()


class HostProfileRoleTests(unittest.TestCase):
    def test_dedicated_builder_budget(self) -> None:
        profile = host_profile.build_profile(
            role="dedicated-builder",
            cores=28,
            model="Mac15,14",
        )
        self.assertEqual(profile["role"], "dedicated-builder")
        self.assertEqual(profile["headroom_cores"], 2)
        self.assertEqual(profile["lease_capacity_cores"], 26)
        self.assertEqual(profile["pulp_build_jobs"], 12)
        self.assertEqual(profile["reserved_gate_cores"], 14)
        self.assertEqual(profile["qos"], "normal")

    def test_dev_overflow_budget(self) -> None:
        profile = host_profile.build_profile(
            role="dev-overflow",
            cores=18,
            model="Mac17,7",
        )
        self.assertEqual(profile["headroom_cores"], 4)
        self.assertEqual(profile["lease_capacity_cores"], 14)
        self.assertEqual(profile["pulp_build_jobs"], 6)
        self.assertEqual(profile["reserved_gate_cores"], 8)
        self.assertEqual(profile["qos"], "background")

    def test_dev_overflow_vm_pool_fits_non_gate_budget(self) -> None:
        # A dev-overflow VM lane runs at non-gate priority, so it can only ever
        # acquire a lease if vm_pool_cores fits the non-gate budget
        # (lease_capacity - reserved_gate_cores). If it does not, the VM is
        # permanently capacity_exceeded while the idle macOS gate holds its
        # reservation, which starves the required-gate Linux preamble
        # fleet-wide. Assert the fit at the real Mac Studio size (28 cores),
        # the host that surfaced the deadlock.
        profile = host_profile.build_profile(role="dev-overflow", cores=28)
        non_gate = profile["lease_capacity_cores"] - profile["reserved_gate_cores"]
        self.assertEqual(non_gate, 6)
        self.assertEqual(profile["vm_pool_cores"], 6)
        self.assertLessEqual(profile["vm_pool_cores"], non_gate)

    def test_light_budget_is_clamped_to_small_hosts(self) -> None:
        profile = host_profile.build_profile(role="light", cores=4, model="portable")
        self.assertEqual(profile["headroom_cores"], 3)
        self.assertEqual(profile["lease_capacity_cores"], 1)
        self.assertEqual(profile["pulp_build_jobs"], 1)
        self.assertEqual(profile["reserved_gate_cores"], 0)

    def test_role_file_wins_over_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            role_file = Path(td) / "role"
            role_file.write_text("light\n", encoding="utf-8")
            role, source = host_profile.resolve_role(
                role_file=str(role_file),
                cores=28,
                model="Mac15,14",
            )
        self.assertEqual(role, "light")
        self.assertTrue(source.startswith("file:"))

    def test_default_never_promotes_to_dedicated_builder(self) -> None:
        role, source = host_profile.resolve_role(
            cores=28,
            model="Mac15,14",
            role_file="/tmp/tartci-missing-role-for-test",
        )
        self.assertEqual(source, "default")
        self.assertEqual(role, "dev-overflow")

    def test_shell_exports_include_pulp_build_jobs(self) -> None:
        profile = host_profile.build_profile(role="dev-overflow", cores=18)
        text = host_profile.shell_exports(profile)
        self.assertIn("PULP_BUILD_JOBS=6", text)
        self.assertIn("TARTCI_GATE_RESERVED_CORES=8", text)

    def test_json_shape_is_stable(self) -> None:
        profile = host_profile.build_profile(role="light", cores=10, memory_mb=16384)
        encoded = json.loads(json.dumps(profile))
        self.assertEqual(encoded["schema"], 2)
        self.assertIn("no mitigation yet", encoded["notes"])
        # Memory axis: 16 GiB - 6 GiB headroom - 4 GiB link reserve = 6 GiB budget.
        self.assertEqual(encoded["mem_mb"], 16384)
        self.assertEqual(encoded["lease_capacity_mem_mb"], 6144)
        self.assertEqual(encoded["pulp_build_mem_budget_mb"], 6144)

    def test_memory_reserve_mirrors_the_core_reserve(self) -> None:
        """reserved_gate_mem_mb holds the gate's share on the memory axis too.

        Pinned to the live m3 shape: 28 cores / 96 GiB dedicated-builder, whose
        core reserve is 14 of a 26-core budget. The memory reserve must be the
        same share of the 80 GiB memory budget, or the gate is protected on one
        axis and exposed on the other.
        """
        profile = host_profile.build_profile(
            role="dedicated-builder", cores=28, memory_mb=98304
        )
        self.assertEqual(profile["lease_capacity_cores"], 26)
        self.assertEqual(profile["reserved_gate_cores"], 14)
        self.assertEqual(profile["lease_capacity_mem_mb"], 81920)
        self.assertEqual(profile["reserved_gate_mem_mb"], 81920 * 14 // 26)
        self.assertEqual(
            profile["non_gate_capacity_mem_mb"],
            81920 - profile["reserved_gate_mem_mb"],
        )

    def test_memory_reserve_never_starves_the_non_gate_class(self) -> None:
        """Even a tiny budget leaves non-gate work one compile job's worth."""
        profile = host_profile.build_profile(
            role="dedicated-builder", cores=28, memory_mb=17408
        )
        # 17 GiB - 8 GiB headroom - 8 GiB link reserve = 1 GiB budget, which is
        # under one compile job — the reserve must collapse rather than zero the
        # non-gate class out.
        self.assertEqual(profile["lease_capacity_mem_mb"], 1536)
        self.assertEqual(profile["reserved_gate_mem_mb"], 0)
        self.assertEqual(profile["non_gate_capacity_mem_mb"], 1536)

    def test_memory_reserve_is_zero_when_the_axis_is_off(self) -> None:
        profile = host_profile.build_profile(role="light", cores=10, memory_mb=0)
        self.assertEqual(profile["reserved_gate_mem_mb"], 0)
        self.assertEqual(profile["non_gate_capacity_mem_mb"], 0)

    def test_memory_axis_off_when_ram_unknown(self) -> None:
        profile = host_profile.build_profile(role="light", cores=10, memory_mb=0)
        self.assertEqual(profile["lease_capacity_mem_mb"], 0)
        self.assertEqual(profile["pulp_build_mem_budget_mb"], 0)


class HostProfileEnvironmentTests(unittest.TestCase):
    def test_environment_role_wins_over_default(self) -> None:
        old = os.environ.get("TARTCI_ROLE")
        os.environ["TARTCI_ROLE"] = "light"
        try:
            role, source = host_profile.resolve_role(
                cores=18,
                model="Mac17,7",
                role_file="/tmp/tartci-missing-role-for-test",
            )
        finally:
            if old is None:
                os.environ.pop("TARTCI_ROLE", None)
            else:
                os.environ["TARTCI_ROLE"] = old
        self.assertEqual((role, source), ("light", "environment"))


class HostProfileMinimalPathTests(unittest.TestCase):
    """Detection must not depend on the caller's PATH.

    A launchd agent inherits a minimal PATH that routinely omits /usr/sbin,
    where `sysctl` lives. Every system-binary call here resolves absolutely, so
    host detection still works under that PATH instead of raising
    FileNotFoundError and taking the whole lease governor down with it.
    """

    #: A launchd-agent PATH with no /usr/sbin and no /sbin.
    MINIMAL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

    def _clean_env(self) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("TARTCI_")
        }
        env["PATH"] = self.MINIMAL_PATH
        return env

    @contextlib.contextmanager
    def _minimal_path(self):
        saved = {key: os.environ[key] for key in list(os.environ) if key.startswith("TARTCI_")}
        old_path = os.environ.get("PATH")
        for key in saved:
            os.environ.pop(key, None)
        os.environ["PATH"] = self.MINIMAL_PATH
        try:
            yield
        finally:
            os.environ.update(saved)
            if old_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old_path

    def test_detect_cores_survives_path_without_usr_sbin(self) -> None:
        with self._minimal_path():
            self.assertGreater(host_profile.detect_cores(), 0)

    def test_detect_memory_survives_path_without_usr_sbin(self) -> None:
        with self._minimal_path():
            self.assertGreater(host_profile.detect_memory_mb(), 0)

    def test_detect_model_survives_path_without_usr_sbin(self) -> None:
        with self._minimal_path():
            self.assertTrue(host_profile.detect_model())

    def test_cli_emits_valid_json_under_launchd_path(self) -> None:
        """End-to-end: the exact failure that denied every lease (rc=2)."""
        script = Path(host_profile.__file__).resolve()
        proc = subprocess.run(
            [sys.executable, str(script), "--json"],
            env=self._clean_env(),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=f"host-profile exited {proc.returncode} under a launchd PATH; stderr:\n{proc.stderr}",
        )
        profile = json.loads(proc.stdout)
        self.assertGreater(profile["ncpu"], 0)
        self.assertGreater(profile["mem_mb"], 0)
        self.assertGreater(profile["lease_capacity_cores"], 0)
        self.assertGreater(profile["pulp_build_mem_budget_mb"], 0)


class HostDeliveryReportTests(unittest.TestCase):
    """How code reaches a lane, read off the live plist and nothing else.

    Two agents concluded "deploy to m3" on a host where the deploy command is
    a silent no-op, because nothing on the host said which delivery mechanism
    it uses. The report has to answer that from the plist, never from a
    hostname, and it has to distinguish "current" from "cannot tell".
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.agents = self.tmp / "agents"
        self.agents.mkdir()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        (self.repo / "a").write_text("one")
        self._git("add", "a")
        self._git("commit", "-qm", "one")
        self.old_commit = self._git("rev-parse", "HEAD")
        (self.repo / "a").write_text("two")
        self._git("commit", "-qam", "two")
        self.head = self._git("rev-parse", "HEAD")

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            text=True, capture_output=True, check=True,
        )
        return proc.stdout.strip()

    def _plist(self, label: str, arguments: list[str]) -> None:
        (self.agents / f"{label}.plist").write_bytes(
            plistlib.dumps({"Label": label, "ProgramArguments": arguments})
        )

    def _generation_lane(self, label: str, commit: str) -> Path:
        gen = self.tmp / "generations" / f"{commit}-deadbeefdeadbeef"
        gen.mkdir(parents=True)
        (gen / ".tartci-support-manifest.json").write_text(json.dumps({
            "schema": 2,
            "repository": "https://github.com/danielraffel/tartci.git",
            "source_commit": commit,
            "members": [],
        }))
        launch = (
            self.tmp / ".local/share/tartci-generations"
            / f"{commit}-deadbeefdeadbeef"
        )
        launch.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(gen), str(launch))
        self._plist(label, [
            "/bin/bash", str(launch / ".tartci-launch"), "serve", "macos", "--loop",
        ])
        return launch

    def _sealed_lane(self, label: str, commit: str, *, marker: bool = True) -> Path:
        app = self.tmp / "libexec" / "TartCILauncher.app"
        (app / "Contents/MacOS").mkdir(parents=True, exist_ok=True)
        (app / "Contents/Resources").mkdir(parents=True, exist_ok=True)
        if marker:
            (app / "Contents/Resources/bundle.json").write_text(json.dumps({
                "schema": 1,
                "source_commit": commit,
                "support_manifest_sha256": "a" * 64,
                "profile_policy_sha256": "b" * 64,
                "tart_home": "/Volumes/Workshop/VMs",
            }))
            (app / "Contents/Resources/lanes.json").write_text(json.dumps({
                "schema": 1, "lanes": {"studio-pulp-gate": {"environment": {}}},
            }))
        self._plist(label, [
            str(app / "Contents/MacOS/tartci-launcher"), "--lane", "studio-pulp-gate",
        ])
        return app

    def _report(self) -> dict:
        return host_profile.build_delivery_report(
            agents=self.agents, repo_root=self.repo
        )

    # -- the two mechanisms --------------------------------------------------

    def test_a_generation_lane_is_named_and_is_deployable(self) -> None:
        label = host_profile.FLEET_LABEL_PREFIX + "anyhost.pulp-gate"
        self._generation_lane(label, self.head)
        lane = self._report()["lanes"][0]
        self.assertEqual(lane["delivery"], "generation")
        self.assertEqual(lane["in_force"]["source_commit"], self.head)
        self.assertTrue(lane["accepts_generation_install"])
        self.assertFalse(lane["staleness"]["stale"])

    def test_a_sealed_lane_is_named_and_is_not_deployable(self) -> None:
        """The finding that cost the time: on this shape the install command
        stages a generation the launcher never execs."""
        label = host_profile.FLEET_LABEL_PREFIX + "anyhost.pulp-gate"
        self._sealed_lane(label, self.head)
        lane = self._report()["lanes"][0]
        self.assertEqual(lane["delivery"], "sealed-bundle")
        self.assertEqual(lane["in_force"]["source_commit"], self.head)
        self.assertFalse(lane["accepts_generation_install"])
        self.assertIn("never execs", lane["how_to_update"])

    def test_the_mechanism_is_derived_from_the_plist_not_the_hostname(self) -> None:
        """Same host, same report run, both mechanisms present. A hostname
        cannot produce this answer; the ProgramArguments can."""
        self._generation_lane(
            host_profile.FLEET_LABEL_PREFIX + "samehost.gen-lane", self.head
        )
        self._sealed_lane(
            host_profile.FLEET_LABEL_PREFIX + "samehost.sealed-lane", self.head
        )
        by_label = {
            lane["label"].rsplit(".", 1)[-1]: lane["delivery"]
            for lane in self._report()["lanes"]
        }
        self.assertEqual(
            by_label, {"gen-lane": "generation", "sealed-lane": "sealed-bundle"}
        )

    # -- staleness -----------------------------------------------------------

    def test_an_older_commit_reports_how_far_behind(self) -> None:
        self._generation_lane(
            host_profile.FLEET_LABEL_PREFIX + "anyhost.pulp-gate", self.old_commit
        )
        stale = self._report()["lanes"][0]["staleness"]
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["behind_by"], 1)

    def test_a_commit_absent_from_the_checkout_is_stale_with_no_distance(self) -> None:
        """Counting commits against a ref this checkout has never seen would
        report 0, which renders identically to up-to-date."""
        self._sealed_lane(
            host_profile.FLEET_LABEL_PREFIX + "anyhost.pulp-gate", "f" * 40
        )
        stale = self._report()["lanes"][0]["staleness"]
        self.assertTrue(stale["stale"])
        self.assertIsNone(stale["behind_by"])
        self.assertIn("not present", stale["detail"])

    def test_an_unreadable_marker_is_unknown_and_never_current(self) -> None:
        """A missing version marker is ignorance. Reporting it as current is
        the failure mode this whole report exists to end."""
        self._sealed_lane(
            host_profile.FLEET_LABEL_PREFIX + "anyhost.pulp-gate",
            self.head, marker=False,
        )
        lane = self._report()["lanes"][0]
        self.assertIsNone(lane["in_force"]["source_commit"])
        self.assertIsNone(lane["staleness"]["stale"])
        self.assertIn("unreadable sealed marker", lane["in_force"]["detail"])

    # -- the zero case gets a control ---------------------------------------

    def test_no_fleet_lanes_is_distinguishable_from_an_unreadable_directory(self) -> None:
        self._plist("com.example.unrelated", ["/bin/true"])
        report = self._report()
        self.assertEqual(report["lanes"], [])
        self.assertEqual(report["fleet_plists_seen"], 0)
        self.assertEqual(report["plists_seen"], 1)
        self.assertIn("no fleet lanes on this host",
                      host_profile.delivery_report_text(report))

    def test_an_empty_agents_directory_reports_blindness(self) -> None:
        report = self._report()
        self.assertEqual(report["plists_seen"], 0)
        self.assertIn("BLIND", host_profile.delivery_report_text(report))

    def test_the_cli_emits_the_report_as_json(self) -> None:
        self._generation_lane(
            host_profile.FLEET_LABEL_PREFIX + "anyhost.pulp-gate", self.head
        )
        env = dict(os.environ, TARTCI_AGENTS_DIR=str(self.agents))
        proc = subprocess.run(
            [sys.executable, str(HOST_PROFILE_PATH), "--delivery", "--json"],
            text=True, capture_output=True, check=False, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(payload["lanes"][0]["delivery"], "generation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
