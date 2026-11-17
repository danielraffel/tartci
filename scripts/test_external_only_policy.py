#!/usr/bin/env python3
"""External-only hosts refuse a build whose build or cache dir is on the internal disk.

m5studio, 2026-11-17: the internal Data volume sat at 98% while builds, ccache,
dependency sources and tool caches defaulted to $HOME. Each test below fails on
a tree without scripts/external_only_policy.py wired into `leases acquire`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import external_only_policy as eop  # noqa: E402
import host_profile  # noqa: E402

try:
    import tomllib  # noqa: F401
    HAVE_TOMLLIB = True
except ModuleNotFoundError:
    HAVE_TOMLLIB = False

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "leases.py"
INTERNAL, EXTERNAL = 1, 2
REAL_NEAREST = eop.nearest_existing


def pretend_paths_exist(case: unittest.TestCase) -> None:
    """The fake external volume is not mounted here, so let its paths resolve as written."""
    patcher = mock.patch.object(eop, "nearest_existing", side_effect=os.path.realpath)
    patcher.start()
    case.addCleanup(patcher.stop)
EXT_ROOT = "/Volumes/Example"


def fake_device(path: str) -> int:
    """Everything under /Volumes/Example is the external volume; the rest is internal."""
    return EXTERNAL if (path + "/").startswith(EXT_ROOT + "/") else INTERNAL


def args(**kw) -> argparse.Namespace:
    return argparse.Namespace(kind=kw.pop("kind", "build"), build_dir=[], cache_dir=[], **kw)


class JudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        pretend_paths_exist(self)

    def test_an_internal_path_is_a_violation_and_an_external_one_is_not(self) -> None:
        verdict = eop.judge([("CCACHE_DIR", "/Users/x/.cache/ccache"),
                             ("PIP_CACHE_DIR", EXT_ROOT + "/caches/pip")],
                            EXT_ROOT, device=fake_device)
        self.assertEqual([v["label"] for v in verdict["violations"]], ["CCACHE_DIR"])

    def test_a_symlink_to_the_external_volume_passes_and_one_back_to_internal_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "real"
            real.mkdir()
            link = Path(td) / "link"
            link.symlink_to(real)
            # Devices follow the resolved target, as os.stat does.
            def device(path: str) -> int:
                return EXTERNAL if path.startswith(os.path.realpath(str(real))) else INTERNAL
            ok = eop.judge([("--cache-dir", str(link / "sub"))], EXT_ROOT, device=device)
            self.assertEqual(ok["violations"], [])
            bad = eop.judge([("--cache-dir", str(Path(td) / "other"))], EXT_ROOT, device=device)
            self.assertEqual(len(bad["violations"]), 1)

    def test_a_missing_path_is_judged_by_its_nearest_existing_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            resolved = REAL_NEAREST(str(Path(td) / "a" / "b" / "c"))
            self.assertEqual(resolved, os.path.realpath(td))

    def test_an_unmounted_external_volume_is_refused_not_written_to_the_boot_disk(self) -> None:
        # /Volumes/Example2 is absent, so its nearest existing ancestor is on the boot volume.
        def device(path: str) -> int:
            return INTERNAL
        with mock.patch.object(eop, "nearest_existing", REAL_NEAREST):
            verdict = eop.judge([("CCACHE_DIR", "/Volumes/Example2/caches/ccache")],
                                "/Volumes/Example2", device=device)
        self.assertEqual(len(verdict["violations"]), 1)
        self.assertNotIn("Example2", verdict["violations"][0]["resolved"])

    def test_an_unreadable_path_is_reported_not_refused(self) -> None:
        def device(path: str) -> int:
            if path.startswith("/Users"):
                raise PermissionError("EPERM")
            return INTERNAL
        verdict = eop.judge([("CCACHE_DIR", "/Users/x/c")], EXT_ROOT, device=device)
        self.assertEqual(verdict["violations"], [])
        self.assertEqual(len(verdict["unread"]), 1)


class CollectTests(unittest.TestCase):
    def setUp(self) -> None:
        # Fall back to reading ccache.conf; the host's real ccache must not leak in.
        patcher = mock.patch.object(eop, "ccache_reported_dir", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ccache_own_answer_wins_over_file_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(eop, "ccache_reported_dir", return_value="/Users/x/real"):
            self.assertEqual(eop.collect({}, home), [("ccache.conf cache_dir", "/Users/x/real")])

    def test_env_build_and_cache_dirs_are_all_collected(self) -> None:
        env = {"CCACHE_DIR": "/a", "CPM_SOURCE_CACHE": "/b", "FETCHCONTENT_BASE_DIR": "/c",
               "PIP_CACHE_DIR": "/d", "UV_CACHE_DIR": "/e", "TARTCI_BUILD_DIR": "/f"}
        with tempfile.TemporaryDirectory() as home:
            got = dict(eop.collect(env, home, ["/g"], ["/h"]))
        self.assertEqual(set(got.values()), {"/a", "/b", "/c", "/d", "/e", "/f", "/g", "/h"})

    def test_ccache_conf_cache_dir_is_read_when_the_env_is_unset(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            conf = Path(home) / "Library" / "Preferences" / "ccache"
            conf.mkdir(parents=True)
            (conf / "ccache.conf").write_text("max_size = 1G\ncache_dir = /Users/x/cc # note\n")
            got = eop.collect({}, home)
        self.assertEqual(got, [("ccache.conf cache_dir", "/Users/x/cc")])

    def test_ccache_configpath_wins_over_the_standard_files(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            conf = Path(home) / "custom.conf"
            conf.write_text("cache_dir = /Users/x/custom\n")
            std = Path(home) / "Library" / "Preferences" / "ccache"
            std.mkdir(parents=True)
            (std / "ccache.conf").write_text("cache_dir = /Users/x/std\n")
            got = eop.collect({"CCACHE_CONFIGPATH": str(conf)}, home)
        self.assertEqual(got, [("ccache.conf cache_dir", "/Users/x/custom")])

    def test_unset_pip_and_uv_defaults_count_when_they_exist(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(eop.collect({}, home), [])
            (Path(home) / "Library" / "Caches" / "pip").mkdir(parents=True)
            (Path(home) / ".cache" / "uv").mkdir(parents=True)
            got = dict(eop.collect({}, home))
            self.assertEqual(set(got), {"PIP_CACHE_DIR default dir", "UV_CACHE_DIR default dir"})
            self.assertEqual(eop.collect({"PIP_CACHE_DIR": "/p", "UV_CACHE_DIR": "/u"}, home),
                             [("PIP_CACHE_DIR", "/p"), ("UV_CACHE_DIR", "/u")])

    def test_the_working_directory_stands_in_for_an_undeclared_build_dir(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(eop.collect({}, home, cwd="/Users/x/w"), [("working directory", "/Users/x/w")])
            self.assertEqual(eop.collect({}, home, ["/b"], cwd="/Users/x/w"), [("--build-dir", "/b")])
            self.assertEqual(eop.collect({"TARTCI_BUILD_DIR": "/b"}, home, cwd="/Users/x/w"),
                             [("TARTCI_BUILD_DIR", "/b")])

    def test_ccache_default_dir_counts_only_when_it_exists(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(eop.collect({}, home), [])
            default = Path(home) / "Library" / "Caches" / "ccache"
            default.mkdir(parents=True)
            self.assertEqual(eop.collect({}, home), [("ccache default dir", str(default))])


class AcquireCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        pretend_paths_exist(self)
        patcher = mock.patch.object(eop, "ccache_reported_dir", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, a, root=EXT_ROOT, env=None, mode="refuse"):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(eop.os, "getcwd", return_value=EXT_ROOT + "/Code/w"):
            return eop.check_acquire(a, root, mode=mode, env=env or {}, home=home,
                                     device=fake_device)

    def test_a_host_without_external_root_is_unchanged(self) -> None:
        self.assertIsNone(self.check(args(), root=None, env={"CCACHE_DIR": "/Users/x/c"}))

    def test_an_internal_ccache_dir_is_refused_with_path_and_fix(self) -> None:
        refusal = self.check(args(), env={"CCACHE_DIR": "/Users/x/.cache/ccache"})
        self.assertEqual(refusal["reason"], eop.REASON)
        self.assertIn("/Users/x/.cache/ccache", refusal["message"])
        self.assertIn(f"export CCACHE_DIR={EXT_ROOT}/caches/ccache", refusal["message"])

    def test_report_mode_admits_and_warns(self) -> None:
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = self.check(args(), env={"CCACHE_DIR": "/Users/x/c"}, mode="report")
        self.assertIsNone(result)
        self.assertIn("would refuse", err.getvalue())
        self.assertIn("/Users/x/c", err.getvalue())

    def test_an_internal_build_dir_is_refused(self) -> None:
        a = args()
        a.build_dir = ["/Users/x/Code/proj/build"]
        refusal = self.check(a)
        self.assertEqual(refusal["violations"][0]["label"], "--build-dir")
        self.assertIn(EXT_ROOT, refusal["message"])

    def test_an_internal_working_directory_is_refused_without_a_declared_build_dir(self) -> None:
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(eop.os, "getcwd", return_value="/Users/x/Code/p"):
            refusal = eop.check_acquire(args(), EXT_ROOT, env={}, home=home, device=fake_device)
        self.assertEqual(refusal["violations"][0]["label"], "working directory")

    def test_external_dirs_are_admitted(self) -> None:
        a = args()
        a.build_dir = [EXT_ROOT + "/Code/w/build"]
        self.assertIsNone(self.check(a, env={"CCACHE_DIR": EXT_ROOT + "/caches/ccache"}))

    def test_a_vm_lease_is_not_judged_here(self) -> None:
        self.assertIsNone(self.check(args(kind="tart-macos-vm"), env={"CCACHE_DIR": "/Users/x/c"}))


class ProfileSettingTests(unittest.TestCase):
    def write(self, td: str, body: str) -> str:
        path = Path(td) / "fleet.toml"
        path.write_text(body)
        return str(path)

    def test_external_root_is_read_from_the_host_table(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self.write(td, f'[host]\nid = "x"\nexternal_root = "{EXT_ROOT}"\n')
            self.assertEqual(host_profile.external_root_setting(path), EXT_ROOT)
            self.assertEqual(host_profile.build_profile(fleet_profile=path)["external_root"], EXT_ROOT)

    def test_mode_defaults_to_report_and_reads_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(host_profile.external_only_mode_setting(self.write(td, '[host]\nid = "x"\n')), "report")
            self.assertEqual(host_profile.external_only_mode_setting(
                self.write(td, '[host]\nexternal_only_mode = "refuse"\n')), "refuse")
            self.assertEqual(host_profile.external_only_mode_setting(
                self.write(td, '[host]\nexternal_only_mode = "bogus"\n')), "report")

    def test_absent_or_relative_means_internal_host(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(host_profile.external_root_setting(self.write(td, '[host]\nid = "x"\n')))
            self.assertIsNone(host_profile.external_root_setting(
                self.write(td, '[host]\nexternal_root = "Volumes/x"\n')))
            self.assertIsNone(host_profile.external_root_setting(str(Path(td) / "missing.toml")))


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "leases"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("TARTCI_", "CCACHE_", "CPM_", "FETCHCONTENT_", "PIP_", "UV_"))}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def acquire(self, root, *extra: str, mode="refuse"):
        profile = Path(self.tmp.name) / "fleet.toml"
        body = '[host]\nid = "t"\n' + (
            f'external_root = "{root}"\nexternal_only_mode = "{mode}"\n' if root else "")
        profile.write_text(body)
        env = dict(self.env, TARTCI_FLEET_PROFILE=str(profile), HOME=self.tmp.name)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "acquire", "--cores", "1", "--id", "t1",
             "--store-dir", str(self.store), "--json", *extra],
            text=True, capture_output=True, env=env, timeout=60)

    def test_refused_before_a_lease_is_taken_and_never_retried(self) -> None:
        internal = os.path.realpath(self.tmp.name)
        if eop.internal_device(lambda p: os.stat(p).st_dev) != os.stat(internal).st_dev:
            self.skipTest("the temp dir is not on this host's boot volume")
        proc = self.acquire("/Volumes/Example2", "--build-dir", internal,
                            "--wait-secs", "30")
        self.assertEqual(proc.returncode, eop.EXIT_REFUSED, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["reason"], eop.REASON)
        self.assertIn(internal, proc.stderr)
        self.assertIn("fix:", proc.stderr)
        self.assertFalse(list(self.store.glob("*.json")) if self.store.exists() else [])

    def test_report_mode_grants_and_warns(self) -> None:
        internal = os.path.realpath(self.tmp.name)
        proc = self.acquire("/Volumes/Example2", "--build-dir", internal, mode="report")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("would refuse", proc.stderr)

    def test_the_same_acquire_is_granted_on_an_internal_host(self) -> None:
        proc = self.acquire(None, "--build-dir", os.path.realpath(str(ROOT)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


@unittest.skipUnless(HAVE_TOMLLIB, "reads the shipped profiles with tomllib")
class ShippedProfileTests(unittest.TestCase):
    def test_hosts_with_an_external_tart_store_are_external_only_and_validate(self) -> None:
        import tomllib
        seen = 0
        for path in sorted((ROOT / "profiles").glob("*-macos-fleet.toml")):
            host = tomllib.loads(path.read_text())["host"]
            external = host["tart_home"].startswith("/Volumes/")
            with self.subTest(profile=path.name):
                root = host.get("external_root")
                if external:
                    seen += 1
                    self.assertTrue(root and (host["tart_home"] + "/").startswith(root + "/"), root)
                else:
                    self.assertIsNone(root)
                res = subprocess.run([sys.executable, "scripts/macos_fleet_lanes.py", "validate",
                                      str(path)], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, res.stderr)
                if external:
                    self.assertIn(host.get("external_only_mode"), ("report", "refuse"))
        self.assertGreaterEqual(seen, 2)

    def test_a_root_that_does_not_hold_the_tart_store_is_rejected(self) -> None:
        source = (ROOT / "profiles" / "m5-macos-fleet.toml").read_text()
        import re
        match = re.search(r'^tart_home = "([^"]+)"$', source, re.M)
        base = source.replace(match.group(0), 'tart_home = "/Volumes/Example/VMs"')
        for root in ("/Users/example", "/Volumes/Data", "/Volumes/Example/", "/Volumes/Example/../x"):
            with self.subTest(root=root), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "bad.toml"
                path.write_text(base.replace('[host]\n', f'[host]\nexternal_root = "{root}"\n', 1))
                res = subprocess.run([sys.executable, "scripts/macos_fleet_lanes.py", "validate",
                                      str(path)], cwd=ROOT, capture_output=True, text=True)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("external_root", res.stderr)


if __name__ == "__main__":
    unittest.main()
