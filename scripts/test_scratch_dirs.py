#!/usr/bin/env python3
"""scratch_dirs: which temp-root scratch the reclaim pass removes, and why not.

Fixtures are real directories under a temp dir standing in for /private/tmp,
the per-user temp dir and Chrome's clone root; the process listings are
injected so every gate is exercised without depending on this host's state.
"""

from __future__ import annotations

import testing_support  # noqa: E402
import contextlib
import io
import os
import pathlib
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import disk_reclaim as dr  # noqa: E402
import pulp_reapers as pr  # noqa: E402
import scratch_dirs as sd  # noqa: E402

HOUR = 3600
GIB = 1024 ** 3


def backdate(path: pathlib.Path, hours: float) -> None:
    stamp = time.time() - hours * HOUR
    for directory, subdirs, files in os.walk(path, topdown=False):
        for name in [*subdirs, *files]:
            os.utime(os.path.join(directory, name), (stamp, stamp), follow_symlinks=False)
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def force_remove(path: pathlib.Path) -> None:
    if path.exists():
        sd.make_owned_tree_writable(path, os.geteuid())
        shutil.rmtree(path, ignore_errors=True)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(force_remove, self.base)
        self.roots = {key: self.base / key
                      for key in ("private_tmp", "user_tmp", "chrome_clone", "derived_data")}
        for root in self.roots.values():
            root.mkdir()

    def make(self, root: str, name: str, hours: float = 24, files: dict | None = None
             ) -> pathlib.Path:
        path = self.roots[root] / name
        path.mkdir()
        for relative, body in (files or {"payload": "x" * 4096}).items():
            target = path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        backdate(path, hours)
        return path

    def scan(self, *, fix: bool = True, idle_hours: float = 12, opened=(), texts=(),
             **kwargs):
        return sd.scan(self.roots, fix=fix, idle_hours=idle_hours,
                       opened=list(opened) or ["/dev/null"],
                       texts=list(texts) or ["/sbin/launchd"], **kwargs)


class Selection(Fixture):
    def test_each_known_pattern_is_removed_when_idle(self):
        made = [
            self.make("private_tmp", "shipyard-validation-aB3dE9"),
            self.make("user_tmp", "tmpab12_xyz", files={"older-clean-clone/a.txt": "a"}),
            self.make("user_tmp", "pulp-version-bump-proof-68enueb0"),
            self.make("user_tmp", "pulp-generated-bump-test-k2j3h4g5"),
            self.make("user_tmp", "pulp-authority-cold-22020-112970984099375-1"),
            self.make("user_tmp", "pulp-fetch-install-18785-2"),
            self.make("private_tmp", "pulp-fetch-src-18785-1"),
            self.make("user_tmp", "shipyard-test-codex-Q1w2E3"),
            self.make("chrome_clone", "code_sign_clone.BVfQrS"),
        ]
        report = self.scan()
        self.assertEqual(report["removed"], len(made), report)
        for path in made:
            self.assertFalse(path.exists(), path)
        self.assertGreater(report["removed_bytes"], 0)
        self.assertEqual(report["by_pattern"]["chrome-code-sign-clone"]["removed"], 1)

    def test_any_generated_pulp_temp_dir_and_chrome_temp_is_removed_when_idle(self):
        # Shapes seen in m3's temp dir: node/C mkdtemp, Python mkdtemp, a C++
        # <pid>-<tick>-<n> counter, BSD mktemp -t, and Chrome's own scratch.
        made = [self.make("user_tmp", name) for name in (
            "pulp-materialized-atlas-YhX9tp",
            "pulp-generated-bump-base-w8gw7p9n",
            "pulp-swiftui-module-836053866706375",
            "pulp-mcp-audio-probe-0633e21d0d58cd2a87cb8d5162a4f6bd",
            "pulp-swiftui-gate-b3-widgets-123456789",
            "pulp-arch-bad-19447-112967930892166-0",
            "pulp-gates-script-inputs.CdrLdO",
            "com.google.Chrome.Air0Z3",
            "com.google.Chrome.chrome_chrome_url_fetcher_.3Ir9yE",
        )]
        report = self.scan()
        self.assertEqual(report["removed"], len(made), report)
        for path in made:
            self.assertFalse(path.exists(), path)

    def test_fixed_name_pulp_dirs_are_never_touched(self):
        # A tool reuses these on purpose; pulp-control-<uid> is a live
        # broker's directory. An all-lowercase suffix is indistinguishable
        # from a word, so it is left too.
        kept = [self.make("user_tmp", name) for name in (
            "pulp-audio-doctor", "pulp-control-501", "pulp-locks-501", "pulp-cli-bake-1",
            "pulp-child-process-working-dir", "pulp-materialized-atlas-ilwvao",
        )]
        kept.append(self.make("private_tmp", "pulp-materialized-atlas-YhX9tp"))
        report = self.scan()
        self.assertEqual(report["removed"], 0, report)
        for path in kept:
            self.assertTrue(path.exists(), path)

    def test_derived_data_needs_two_weeks_idle_even_under_pressure(self):
        fresh = self.make("derived_data", "cmux-begnpxtmcrbvxrcrqeplcypfldvl", hours=13 * 24)
        stale = self.make("derived_data", "cmux-subrouter-goal-copy", hours=15 * 24)
        report = self.scan(idle_hours=sd.PRESSURE_IDLE_HOURS)
        self.assertTrue(fresh.exists())
        self.assertFalse(stale.exists())
        self.assertEqual(report["by_pattern"]["xcode-derived-data"]["removed"], 1, report)

    def test_derived_data_an_open_build_keeps_its_folder(self):
        stale = self.make("derived_data", "Pulp-agcvtsjdjopthyetczvivkmaozcb", hours=30 * 24)
        self.scan(opened=[str(stale / "payload")])
        self.assertTrue(stale.exists())

    def test_anything_not_named_is_never_touched(self):
        kept = [
            self.make("private_tmp", "shipyard-validation"),          # no suffix
            self.make("private_tmp", "my-notes"),
            self.make("user_tmp", "tmpab12_xyz", files={"older-clean-clone/a": "a",
                                                         "something-else": "b"}),
            self.make("user_tmp", "tmpzz12_xyz", files={"report.json": "{}"}),
            self.make("user_tmp", "pulp-authority-cold"),             # no id
            self.make("chrome_clone", "Google Chrome.app.bundle"),
            # A pattern only applies under its own root.
            self.make("user_tmp", "shipyard-validation-aB3dE9"),
            self.make("private_tmp", "code_sign_clone.BVfQrS"),
        ]
        report = self.scan()
        self.assertEqual(report["removed"], 0, report)
        for path in kept:
            self.assertTrue(path.exists(), path)

    def test_a_symlink_named_like_scratch_is_not_followed(self):
        target = self.base / "precious"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        link = self.roots["private_tmp"] / "shipyard-validation-aB3dE9"
        link.symlink_to(target)
        report = self.scan()
        self.assertEqual(report["candidates"], 0)
        self.assertTrue((target / "keep.txt").exists())


class Gates(Fixture):
    def test_recent_scratch_stays(self):
        path = self.make("private_tmp", "shipyard-validation-aB3dE9", hours=2)
        report = self.scan()
        self.assertTrue(path.exists())
        self.assertEqual(report["kept"], {"recent": 1})

    def test_a_recent_file_deep_inside_keeps_the_whole_tree(self):
        path = self.make("private_tmp", "shipyard-validation-aB3dE9",
                         files={"tmpq/older-clean-clone/.git/index": "i"})
        os.utime(path / "tmpq" / "older-clean-clone" / ".git", None)
        report = self.scan()
        self.assertTrue(path.exists())
        self.assertEqual(report["kept"], {"recent": 1})

    def test_an_open_file_inside_keeps_it(self):
        path = self.make("private_tmp", "shipyard-validation-aB3dE9")
        # lsof reports the /tmp spelling; the root is spelled /private/tmp.
        opened = [str(path / "payload")]
        report = self.scan(opened=opened)
        self.assertTrue(path.exists())
        self.assertEqual(report["kept"], {"open_files": 1})

    def test_a_process_environment_naming_it_keeps_it(self):
        path = self.make("private_tmp", "shipyard-validation-aB3dE9")
        texts = [f"ctest --output-on-failure TMPDIR={path} HOME=/Users/x"]
        report = self.scan(texts=texts)
        self.assertTrue(path.exists())
        self.assertEqual(report["kept"], {"named_by_process": 1})

    def test_tmp_and_var_spellings_are_the_same_path(self):
        names = sd.spellings(pathlib.Path("/private/tmp/shipyard-validation-aB3dE9"))
        self.assertIn("/tmp/shipyard-validation-aB3dE9", names)
        names = sd.spellings(pathlib.Path("/var/folders/x/T/pulp-fetch-install-1-2"))
        self.assertIn("/private/var/folders/x/T/pulp-fetch-install-1-2", names)

    def test_an_unreadable_process_table_removes_nothing(self):
        path = self.make("private_tmp", "shipyard-validation-aB3dE9")
        for opened, texts in ((None, ["x"]), (["/dev/null"], None)):
            report = sd.scan(self.roots, fix=True, idle_hours=12, opened=opened, texts=texts)
            self.assertTrue(path.exists())
            self.assertIn("error", report)
            self.assertEqual(report["kept"], {"process_scan_unavailable": 1})

    def test_another_users_scratch_is_never_touched(self):
        path = self.make("private_tmp", "shipyard-validation-aB3dE9")
        report = self.scan(uid=os.geteuid() + 1)
        self.assertTrue(path.exists())
        self.assertEqual(report["kept"], {"other_owner": 1})

    def test_a_tree_touched_between_scan_and_removal_stays(self):
        path = self.make("private_tmp", "shipyard-validation-aB3dE9")
        real = sd.newest_mtime
        calls = []

        def touched(target, *args, **kwargs):
            calls.append(target)
            if len(calls) == 2:  # the recheck immediately before removal
                os.utime(path / "payload", None)
            return real(target, *args, **kwargs)

        with mock.patch.object(sd, "newest_mtime", side_effect=touched):
            report = self.scan()
        self.assertTrue(path.exists())
        self.assertEqual(report["kept"], {"touched_during_pass": 1})

    def test_a_walk_that_cannot_see_everything_keeps_the_entry(self):
        path = self.make("user_tmp", "pulp-authority-cold-1-2-1",
                         files={f"f{i}": "x" for i in range(5)})
        self.assertIsNone(sd.newest_mtime(path, max_entries=3))
        with mock.patch.object(sd.newest_mtime, "__defaults__", (sd.WALK_MAXDEPTH, 3)):
            report = self.scan()
        self.assertTrue(path.exists())
        self.assertEqual(report["kept"], {"unmeasurable": 1})

    def test_dry_run_counts_but_removes_nothing(self):
        path = self.make("private_tmp", "shipyard-validation-aB3dE9")
        report = self.scan(fix=False)
        self.assertTrue(path.exists())
        self.assertEqual(report["removed"], 1)
        self.assertEqual(report["mode"], "dry-run")


class ReadOnlyTrees(Fixture):
    def test_a_read_only_installed_pack_is_removed(self):
        """The exact leak: pulp-fetch-install-*/<sha>/ui.js locked dr-x/r--."""
        path = self.make("private_tmp", "shipyard-validation-aB3dE9",
                         files={"pulp-fetch-install-18785-2/2ae42d8a/ui.js": "export {}"})
        pack = path / "pulp-fetch-install-18785-2" / "2ae42d8a"
        os.chmod(pack / "ui.js", stat.S_IRUSR)
        os.chmod(pack, stat.S_IRUSR | stat.S_IXUSR)
        sealed = path / "sealed"
        sealed.mkdir()
        (sealed / "inner").mkdir()
        os.chmod(sealed, 0)
        # A plain rmtree is what every earlier cleanup ran, and it fails here.
        with self.assertRaises(OSError):
            shutil.rmtree(pack)
        self.assertIsNone(sd.remove_tree(path, os.geteuid()))
        self.assertFalse(path.exists())


class Settings(unittest.TestCase):
    def profile(self, body: str) -> pathlib.Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        handle.write(body)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return pathlib.Path(handle.name)

    def test_off_unless_the_profile_opts_in(self):
        report = sd.run(fix=True, profile=self.profile("[reclaim]\ntmp_checkouts = true\n"))
        self.assertFalse(report["enabled"])

    @testing_support.requires_tomllib
    def test_idle_hours_are_bounded(self):
        for value in ("1", "721", "\"12\"", "12.5"):
            report = sd.run(fix=True, profile=self.profile(
                f"[reclaim]\nscratch_dirs = true\nscratch_idle_hours = {value}\n"))
            self.assertFalse(report["enabled"], value)
            self.assertIn("scratch_idle_hours", report["reason"])

    def test_the_whole_table_validates_with_scratch_keys(self):
        table = {"pulp_worktree_builds": False, "scratch_dirs": True, "scratch_idle_hours": 12}
        self.assertEqual(pr.validate_table(table), [])

    @testing_support.requires_tomllib
    def test_pressure_selects_the_shorter_gate(self):
        base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(force_remove, base)
        roots = {"private_tmp": base}
        path = base / "shipyard-validation-aB3dE9"
        path.mkdir()
        backdate(path, 6)
        profile = self.profile("[reclaim]\nscratch_dirs = true\n")
        listings = {"opened": ["/dev/null"], "texts": ["/sbin/launchd"]}
        real_scan = sd.scan
        with mock.patch.object(sd, "scan",
                               side_effect=lambda *a, **k: real_scan(*a, **{**k, **listings})):
            calm = sd.run(fix=True, profile=profile, roots=roots)
            self.assertTrue(path.exists())
            self.assertEqual(calm["idle_hours"], 12)
            pressed = sd.run(fix=True, profile=profile, roots=roots, pressure=True)
        self.assertEqual(pressed["idle_hours"], sd.PRESSURE_IDLE_HOURS)
        self.assertFalse(path.exists())


class BootVolumeWatch(unittest.TestCase):
    """disk_reclaim judges the boot data volume on its own floor."""

    def setUp(self) -> None:
        self.base = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.code = self.base / "Code"
        self.code.mkdir()
        self.vms = self.base / "VMs"
        self.vms.mkdir()
        self.boot = self.base / "Data"
        self.boot.mkdir()
        self.state = self.base / "state"
        environ = {"TARTCI_FLEET_PROFILE": str(self.base / "absent.toml")}
        patcher = mock.patch.dict(os.environ, environ)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_pass(self, free: dict[str, float], devices: dict[str, int], *extra: str):
        def fake_free(path):
            return int(free[str(pathlib.Path(path))] * GIB)

        def fake_device(path):
            return devices[str(pathlib.Path(path))]

        with mock.patch.object(dr, "free_bytes", side_effect=fake_free), \
                mock.patch.object(dr, "device_id", side_effect=fake_device), \
                mock.patch.object(dr, "active_command_lines", return_value=""):
            receipt: dict = {}
            args = dr.build_parser().parse_args([
                "--roots", str(self.code), "--lease-path", str(self.vms),
                "--boot-volume", str(self.boot), "--fail-below-gb", "60",
                "--state-dir", str(self.state), "--json", *extra])
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = dr._run(args, receipt)
        return code, receipt["report"]["boot_volume"]

    def test_a_low_boot_volume_off_the_tart_store_fails_with_its_own_code(self):
        """m3 on 2026-10-01: Tart store on Workshop healthy, boot disk at 2 GiB."""
        code, boot = self.run_pass(
            {str(self.code): 600, str(self.vms): 600, str(self.boot): 2},
            {str(self.code): 2, str(self.vms): 2, str(self.boot): 1})
        self.assertEqual(code, 5)
        self.assertEqual(boot["judged_by"], "own_floor")
        self.assertTrue(boot["below_floor"])

    def test_a_healthy_boot_volume_passes(self):
        code, boot = self.run_pass(
            {str(self.code): 600, str(self.vms): 600, str(self.boot): 80},
            {str(self.code): 2, str(self.vms): 2, str(self.boot): 1})
        self.assertEqual(code, 0)
        self.assertFalse(boot["below_floor"])

    def test_the_tart_store_floor_still_wins_when_both_are_low(self):
        code, _ = self.run_pass(
            {str(self.code): 10, str(self.vms): 10, str(self.boot): 2},
            {str(self.code): 2, str(self.vms): 2, str(self.boot): 1})
        self.assertEqual(code, 3)

    def test_a_boot_volume_holding_the_tart_store_is_judged_once(self):
        """m1/m5: VMs on the boot disk, so the lease floor already covers it."""
        code, boot = self.run_pass(
            {str(self.code): 40, str(self.vms): 40, str(self.boot): 40},
            {str(self.code): 1, str(self.vms): 1, str(self.boot): 1})
        self.assertEqual(code, 3)
        self.assertEqual(boot["judged_by"], "lease_floor")
        self.assertFalse(boot["below_floor"])

    def test_a_zero_boot_floor_disables_the_watch(self):
        code, boot = self.run_pass(
            {str(self.code): 600, str(self.vms): 600, str(self.boot): 2},
            {str(self.code): 2, str(self.vms): 2, str(self.boot): 1},
            "--boot-floor-gb", "0")
        self.assertEqual(code, 0)
        self.assertEqual(boot["judged_by"], "disabled")


if __name__ == "__main__":
    unittest.main()
