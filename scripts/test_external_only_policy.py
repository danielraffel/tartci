#!/usr/bin/env python3
"""External-only hosts report a build whose build or cache dir is on the internal disk.

m5studio, 2026-11-17: the internal Data volume sat at 98% while builds, ccache,
dependency sources and tool caches defaulted to $HOME. Each test below fails on
a tree without scripts/external_only_policy.py wired into `leases acquire`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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
REAL_CCACHE_DIR = eop.ccache_reported_dir


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

    def test_an_unreadable_internal_device_is_explicitly_unknown(self) -> None:
        def device(path: str) -> int:
            raise PermissionError(f"cannot read {path}")

        verdict = eop.judge([], EXT_ROOT, device=device)
        self.assertFalse(verdict["internal_device_known"])
        self.assertEqual(verdict["violations"], [])
        self.assertTrue(verdict["internal_device_errors"])


    def test_a_missing_darwin_probe_is_not_an_error_when_the_fallback_answers(self) -> None:
        def device(path: str) -> int:
            if path == "/System/Volumes/Data":
                raise FileNotFoundError(path)
            return INTERNAL

        with mock.patch.object(eop.sys, "platform", "linux"):
            verdict = eop.judge([], EXT_ROOT, device=device)
        self.assertTrue(verdict["internal_device_known"])
        self.assertEqual(verdict["internal_device_errors"], [])


class CollectTests(unittest.TestCase):
    def setUp(self) -> None:
        # Fall back to reading ccache.conf; the host's real ccache must not leak in.
        patcher = mock.patch.object(eop, "ccache_reported_dir", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ccache_own_answer_wins_over_file_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            real = Path(home) / "real"
            real.mkdir()
            with mock.patch.object(eop, "ccache_reported_dir", return_value=str(real)):
                self.assertEqual(eop.collect({}, home), [("ccache dir", str(real))])
            with mock.patch.object(eop, "ccache_reported_dir",
                                   return_value=str(Path(home) / "Library" / "Caches" / "ccache")):
                self.assertEqual(eop.collect({}, home), [])

    def test_env_build_and_cache_dirs_are_all_collected(self) -> None:
        env = {"CCACHE_DIR": "/a", "CPM_SOURCE_CACHE": "/b", "FETCHCONTENT_BASE_DIR": "/c",
               "PIP_CACHE_DIR": "/d", "UV_CACHE_DIR": "/e", "TARTCI_BUILD_DIR": "/f"}
        with tempfile.TemporaryDirectory() as home:
            got = dict(eop.collect(env, home, ["/g"], ["/h"]))
        self.assertEqual(set(got.values()), {"/a", "/b", "/c", "/d", "/e", "/f", "/g", "/h"})

    def test_ccache_conf_cache_dir_is_read_when_the_env_is_unset(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            cc = Path(home) / "cc"
            cc.mkdir()
            conf = Path(home) / "Library" / "Preferences" / "ccache"
            conf.mkdir(parents=True)
            (conf / "ccache.conf").write_text(f"max_size = 1G\ncache_dir = {cc} # note\n")
            got = eop.collect({}, home)
        self.assertEqual(got, [("ccache dir", str(cc))])

    def test_ccache_reported_dir_beats_the_user_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            conf = Path(home) / "Library" / "Preferences" / "ccache"
            conf.mkdir(parents=True)
            (conf / "ccache.conf").write_text("cache_dir = /Volumes/Example/cc\n")
            with mock.patch.object(eop, "ccache_reported_dir", return_value="/Users/x/dir-specific"):
                self.assertEqual(eop.collect({}, home), [("ccache dir", "/Users/x/dir-specific")])

    def test_a_configured_ccache_dir_is_judged_before_it_exists(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            conf = Path(home) / "Library" / "Preferences" / "ccache"
            conf.mkdir(parents=True)
            (conf / "ccache.conf").write_text("cache_dir = /Users/x/never-created\n")
            self.assertEqual(eop.collect({}, home), [("ccache dir", "/Users/x/never-created")])

    @unittest.skipUnless(shutil.which("ccache"), "needs a real ccache on PATH")
    def test_a_real_ccache_with_a_missing_default_dir_is_not_judged(self) -> None:
        # ccache -k cache_dir prints its default even when it does not exist.
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(eop, "ccache_reported_dir", REAL_CCACHE_DIR):
            env = {"PATH": os.environ["PATH"], "HOME": home}
            self.assertEqual(eop.collect(env, home), [])

    def test_ccache_configpath_wins_over_the_standard_files(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            conf = Path(home) / "custom.conf"
            custom = Path(home) / "custom"
            custom.mkdir()
            conf.write_text(f"cache_dir = {custom}\n")
            std = Path(home) / "Library" / "Preferences" / "ccache"
            std.mkdir(parents=True)
            (std / "ccache.conf").write_text("cache_dir = /Users/x/std\n")
            got = eop.collect({"CCACHE_CONFIGPATH": str(conf)}, home)
        self.assertEqual(got, [("ccache dir", str(custom))])

    def test_unset_pip_and_uv_defaults_count_when_they_exist(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(eop.collect({}, home), [])
            (Path(home) / "Library" / "Caches" / "pip").mkdir(parents=True)
            with mock.patch.object(eop.sys, "platform", "darwin"):
                (Path(home) / "Library" / "Caches" / "uv").mkdir(parents=True)
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
            self.assertEqual(eop.collect({}, home), [("ccache dir", str(default))])


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
            refusal = eop.check_acquire(args(), EXT_ROOT, mode="refuse", env={}, home=home,
                                        device=fake_device)
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

    def test_mode_is_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(host_profile.external_only_mode_setting(self.write(td, '[host]\nid = "x"\n')), "report")

    def test_absent_or_relative_means_internal_host(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(host_profile.external_root_setting(self.write(td, '[host]\nid = "x"\n')))
            self.assertIsNone(host_profile.external_root_setting(
                self.write(td, '[host]\nexternal_root = "Volumes/x"\n')))
            self.assertIsNone(host_profile.external_root_setting(str(Path(td) / "missing.toml")))


class AuditStatusTests(unittest.TestCase):
    def verdict(self, **kw):
        base = {"external_root": EXT_ROOT, "internal_device_known": True,
                "internal_device_errors": [], "judged": [], "violations": [], "unread": []}
        base.update(kw)
        return base

    def run_check(self, verdict):
        import contextlib, io
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(eop, "judge", return_value=verdict), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = eop._main(["check", "--external-root", EXT_ROOT])
        return code, out.getvalue()

    def test_a_clean_audit_is_ok(self) -> None:
        code, out = self.run_check(self.verdict())
        self.assertEqual(code, 0)
        self.assertIn("ok:", out)

    def test_an_unreadable_probe_is_not_reported_as_ok(self) -> None:
        code, out = self.run_check(self.verdict(unread=[{"label": "x", "path": "/p", "error": "EPERM"}]))
        self.assertEqual(code, 2)
        self.assertNotIn("ok:", out)

    def test_an_unknown_internal_device_is_not_reported_as_ok(self) -> None:
        code, out = self.run_check(self.verdict(internal_device_known=False))
        self.assertEqual(code, 2)
        self.assertNotIn("ok:", out)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "leases"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("TARTCI_", "CCACHE_", "CPM_", "FETCHCONTENT_", "PIP_", "UV_"))}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def acquire(self, root, *extra: str, cwd=None):
        profile = Path(self.tmp.name) / "fleet.toml"
        body = '[host]\nid = "t"\n' + (f'external_root = "{root}"\n' if root else "")
        profile.write_text(body)
        env = dict(self.env, TARTCI_FLEET_PROFILE=str(profile), HOME=self.tmp.name)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "acquire", "--cores", "1", "--id", "t1",
             "--store-dir", str(self.store), "--json", *extra],
            text=True, capture_output=True, env=env, timeout=60, cwd=cwd)

    def internal_dir(self) -> str:
        internal = os.path.realpath(self.tmp.name)
        if eop.internal_device(lambda p: os.stat(p).st_dev) != os.stat(internal).st_dev:
            self.skipTest("the temp dir is not on this host's boot volume")
        return internal

    def events(self) -> list:
        path = self.store / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def test_report_mode_grants_warns_and_journals_would_refuse(self) -> None:
        internal = self.internal_dir()
        proc = self.acquire("/Volumes/Example2", "--build-dir", internal)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("would refuse", proc.stderr)
        rows = [r for r in self.events() if r.get("event") == "external_only_policy"]
        self.assertEqual(len(rows), 1, self.events())
        self.assertEqual(rows[0]["outcome"], "would_refuse")
        self.assertEqual(rows[0]["paths"], [internal])

    def test_a_wait_loop_judges_once(self) -> None:
        internal = self.internal_dir()
        # Fill the host so the acquire is denied (75) and retried, then count warnings.
        proc = self.acquire("/Volumes/Example2", "--build-dir", internal, "--cores", "9999",
                            "--wait-secs", "5")
        self.assertEqual(proc.stderr.count("would refuse"), 1, proc.stderr)

    def test_a_deleted_working_directory_still_admits(self) -> None:
        gone = Path(self.tmp.name) / "gone"
        gone.mkdir()
        script = (f"import os,sys,subprocess; os.chdir({str(gone)!r}); os.rmdir({str(gone)!r}); "
                  f"sys.exit(subprocess.call({[sys.executable, str(SCRIPT), 'acquire', '--cores', '1', '--id', 't1', '--store-dir', str(self.store), '--json']!r}))")
        profile = Path(self.tmp.name) / "fleet.toml"
        profile.write_text('[host]\nid = "t"\nexternal_root = "/Volumes/Example2"\n')
        env = dict(self.env, TARTCI_FLEET_PROFILE=str(profile), HOME=self.tmp.name)
        proc = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_the_same_acquire_is_granted_on_an_internal_host(self) -> None:
        proc = self.acquire(None, "--build-dir", os.path.realpath(str(ROOT)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("would refuse", proc.stderr)


class SafeAndRefuseTests(unittest.TestCase):
    def setUp(self) -> None:
        pretend_paths_exist(self)

    def test_any_error_admits_with_one_warning(self) -> None:
        import contextlib, io
        err = io.StringIO()
        with mock.patch.object(eop.os, "getcwd", side_effect=FileNotFoundError("gone")), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(eop.check_acquire_safe(args(), EXT_ROOT, mode="refuse"))
        self.assertEqual(err.getvalue().count("not judged"), 1)

    def test_refuse_mode_still_refuses_in_code(self) -> None:
        a = args()
        a.build_dir = ["/Users/x/b"]
        with mock.patch.object(eop, "ccache_reported_dir", return_value=None):
            refusal = eop.check_acquire_safe(a, EXT_ROOT, mode="refuse", env={}, home="/nonexistent",
                                             device=fake_device)
        self.assertEqual(refusal["reason"], eop.REASON)

    def test_unknown_internal_device_warns_in_report_and_refuses_in_refuse_mode(self) -> None:
        import contextlib, io

        def unreadable(_path: str) -> int:
            raise PermissionError("EPERM")

        report_args = args()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(eop.check_acquire(report_args, EXT_ROOT, mode="report", env={},
                                                home="/nonexistent", device=unreadable))
        self.assertIn("could not prove the internal data volume", err.getvalue())
        self.assertTrue(report_args.external_policy_unread)

        refusal = eop.check_acquire(args(), EXT_ROOT, mode="refuse", env={},
                                    home="/nonexistent", device=unreadable)
        self.assertEqual(refusal["reason"], eop.REASON_UNREADABLE)

    def test_check_acquire_defaults_to_report(self) -> None:
        import contextlib, io
        a = args()
        a.build_dir = ["/Users/x/b"]
        with mock.patch.object(eop, "ccache_reported_dir", return_value=None), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(eop.check_acquire(a, EXT_ROOT, env={}, home="/nonexistent",
                                                device=fake_device))

    def test_an_unreadable_profile_means_policy_off_not_an_error(self) -> None:
        import contextlib, io
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "f.toml"
            path.write_bytes(b'[host]\nid = "\xff\xfe"\nexternal_root = "/Volumes/Example"\n')
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertIsNone(host_profile.external_root_setting(str(path)))
            self.assertIn("policy off", err.getvalue())

    def test_a_failing_journal_never_fails_the_acquire(self) -> None:
        import leases
        a = argparse.Namespace(kind="build", build_dir=[], cache_dir=[], owner="o", id="t1")
        with mock.patch.object(leases, "journal_lease_events", side_effect=TypeError("boom")), \
                mock.patch.object(leases.external_only_policy, "check_acquire_safe", return_value=None):
            a.external_would_refuse = [{"path": "/p", "label": "l"}]
            # Reaches the policy block only; the journal failure must not propagate out of it.
            cfg = {"external_root": EXT_ROOT, "external_only_mode": "report"}
            with mock.patch.object(leases, "capacity_config", return_value=cfg), \
                    mock.patch.object(leases, "locked_store", side_effect=StopIteration):
                with self.assertRaises(StopIteration):  # got past the journal block
                    leases.acquire(argparse.Namespace(
                        store_dir="/nonexistent", priority="build", pid=None, cores_requested=1,
                        id="t1", kind="build", owner="o", build_dir=[], cache_dir=[],
                        external_would_refuse=[{"path": "/p", "label": "l"}],
                        external_policy_checked=False))

    def test_unread_policy_evidence_is_journaled(self) -> None:
        import leases

        unread = [{"path": "/p", "label": "internal device probe", "error": "EPERM"}]
        args_for_acquire = argparse.Namespace(
            store_dir="/nonexistent", priority="build", pid=None, cores_requested=1,
            id="t1", kind="build", owner="o", build_dir=[], cache_dir=[],
            external_policy_checked=False)

        def mark_unread(args, *_args, **_kwargs):
            args.external_policy_unread = unread
            return None

        with mock.patch.object(leases.external_only_policy, "check_acquire_safe",
                               side_effect=mark_unread), \
                mock.patch.object(leases, "journal_lease_events") as journal, \
                mock.patch.object(leases, "capacity_config", return_value={
                    "external_root": EXT_ROOT, "external_only_mode": "report"}), \
                mock.patch.object(leases, "locked_store", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                leases.acquire(args_for_acquire)

        row = journal.call_args.args[1][0]
        self.assertEqual(row["outcome"], "unread")
        self.assertEqual(row["paths"], ["/p"])
        self.assertEqual(row["unread"], unread)

    def test_exports_json_is_machine_readable(self) -> None:
        env = {"PATH": os.environ.get("PATH", "")}
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "external_only_policy.py"), "exports",
             "--external-root", EXT_ROOT, "--json"],
            text=True, capture_output=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["UV_CACHE_DIR"], EXT_ROOT + "/caches/uv")
        self.assertNotIn("export ", proc.stdout)

    def test_refuse_is_not_reachable_from_the_profile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "f.toml"
            path.write_text('[host]\nexternal_root = "/Volumes/Example"\nexternal_only_mode = "refuse"\n')
            self.assertEqual(host_profile.external_only_mode_setting(str(path)), "report")


@unittest.skipUnless(HAVE_TOMLLIB, "reads the shipped profiles with tomllib")
class ShippedProfileTests(unittest.TestCase):
    def test_shipped_profiles_validate(self) -> None:
        for path in sorted((ROOT / "profiles").glob("*-macos-fleet.toml")):
            with self.subTest(profile=path.name):
                res = subprocess.run([sys.executable, "scripts/macos_fleet_lanes.py", "validate",
                                      str(path)], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, res.stderr)

    def test_m3_and_m5studio_opt_in_in_report_mode(self) -> None:
        # Without external_root the policy returns at once and no report row ever appears.
        expected = {"m3-macos-fleet.toml": "/Volumes/Workshop",
                    "m5studio-macos-fleet.toml": "/Volumes/Atelier"}
        for name, root in expected.items():
            path = str(ROOT / "profiles" / name)
            with self.subTest(profile=name):
                self.assertEqual(host_profile.external_root_setting(path), root)
                self.assertEqual(host_profile.external_only_mode_setting(path), "report")

    def test_refuse_mode_is_rejected_by_the_validator(self) -> None:
        source = (ROOT / "profiles" / "m5studio-macos-fleet.toml").read_text()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.toml"
            import re
            # The shipped profile already opts in; start from one that does not.
            source = re.sub(r'^external_(?:root|only_mode) = .*\n', '', source, flags=re.MULTILINE)
            path.write_text(re.sub(r'^tart_home = "[^"]+"$',
                                   'tart_home = "/Volumes/Example/VMs"', source,
                                   count=1, flags=re.MULTILINE).replace(
                    '[host]\n', '[host]\nexternal_root = "/Volumes/Example"\n'
                    'external_only_mode = "refuse"\n', 1))
            res = subprocess.run([sys.executable, "scripts/macos_fleet_lanes.py", "validate",
                                  str(path)], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("external_only_mode", res.stderr)

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
