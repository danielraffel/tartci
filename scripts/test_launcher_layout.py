#!/usr/bin/env python3
"""launcher_layout: which launcher a sealed-launcher host installs, v1 or v2."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launcher_layout as ll

HERE = Path(__file__).resolve().parent
V1 = {"launch_helper": {"schema": 1}}
V2 = {"launch_helper": {"schema": 2, "layout": "v2"}}


class LauncherLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.home = Path(self.td.name)
        (self.home / ".config/tartci").mkdir(parents=True)
        self.opt_in = ll.opt_in_path(self.home)

    def tearDown(self) -> None:
        self.td.cleanup()

    def opt(self, mode: int = 0o600) -> None:
        self.opt_in.write_text("")
        os.chmod(self.opt_in, mode)

    def test_without_the_opt_in_a_host_stays_v1(self) -> None:
        self.assertEqual("v1", ll.decide(self.home, None))
        self.assertEqual("v1", ll.decide(self.home, V1))

    def test_the_opt_in_moves_a_v1_host_to_v2(self) -> None:
        self.opt()
        self.assertEqual("v2", ll.decide(self.home, V1))
        self.assertEqual("v2", ll.decide(self.home, None))
        self.assertEqual("v2", ll.decide(self.home, V2))

    def test_an_untrusted_opt_in_is_refused_loudly(self) -> None:
        for mode in (0o620, 0o602, 0o666):
            with self.subTest(mode=oct(mode)):
                self.opt(mode)
                with self.assertRaisesRegex(ll.LayoutError, "group- or world-writable"):
                    ll.decide(self.home, V1)
        self.opt_in.unlink()
        self.opt_in.mkdir()
        with self.assertRaisesRegex(ll.LayoutError, "regular file"):
            ll.decide(self.home, V1)
        self.opt_in.rmdir()
        target = self.home / "elsewhere"
        target.write_text("")
        os.chmod(target, 0o600)
        self.opt_in.symlink_to(target)
        with self.assertRaisesRegex(ll.LayoutError, "regular file"):
            ll.decide(self.home, V1)

    def test_an_opt_in_owned_by_another_user_is_refused(self) -> None:
        self.opt()
        real = os.getuid
        try:
            ll.os.getuid = lambda: real() + 1
            with self.assertRaisesRegex(ll.LayoutError, "not owned by this user"):
                ll.opted_in(self.home)
        finally:
            ll.os.getuid = real

    def test_a_v2_host_that_lost_its_opt_in_refuses_and_names_the_downgrade(self) -> None:
        with self.assertRaises(ll.LayoutError) as caught:
            ll.decide(self.home, V2)
        message = str(caught.exception)
        self.assertIn("never flips back silently", message)
        self.assertIn("--launcher-layout v1", message)
        self.assertIn("Removable Volumes", message)
        # The explicit downgrade is the way back.
        self.assertEqual("v1", ll.decide(self.home, V2, "v1"))

    def test_a_lost_receipt_does_not_read_as_nothing_installed(self) -> None:
        pointer = self.home / ".config/tartci/launcher-generation.json"
        pointer.write_text("{}")
        for receipt in (None, {"support": {}}):
            with self.subTest(receipt=receipt):
                self.assertEqual("v2", ll.installed(receipt, self.home))
                with self.assertRaises(ll.LayoutError) as caught:
                    ll.decide(self.home, receipt)
                self.assertIn("install receipt is missing but the launcher v2 pointer",
                              str(caught.exception))
                self.assertIn("--launcher-layout v1", str(caught.exception))
        self.assertEqual("v1", ll.decide(self.home, None, "v1"), "the explicit downgrade")
        self.opt()
        self.assertEqual("v2", ll.decide(self.home, None))
        # A recorded v1 receipt is not overruled by a leftover pointer.
        self.opt_in.unlink()
        self.assertEqual("v1", ll.decide(self.home, V1))

    def test_a_v2_app_on_disk_is_evidence_too(self) -> None:
        app = self.home / "TartCILauncher.app"
        (app / "Contents/Resources").mkdir(parents=True)
        self.assertIsNone(ll.installed(None, self.home, app), "control: no evidence")
        self.assertEqual("v1", ll.decide(self.home, None, app=app))
        (app / "Contents/Resources/policy.json").write_text(json.dumps({"schema": 2}))
        self.assertEqual("v2", ll.installed(None, self.home, app))
        with self.assertRaisesRegex(ll.LayoutError, "a launcher v2 app at"):
            ll.decide(self.home, None, app=app)
        (app / "Contents/Resources/policy.json").write_text(json.dumps({"schema": 1}))
        self.assertEqual("v1", ll.decide(self.home, None, app=app))
        status = ll.status(self.home, None, app)
        self.assertIsNone(status["layout"])

    def test_explicit_v2_needs_the_opt_in(self) -> None:
        with self.assertRaisesRegex(ll.LayoutError, "needs the per-host opt-in"):
            ll.decide(self.home, V1, "v2")
        self.opt()
        self.assertEqual("v2", ll.decide(self.home, V1, "v2"))
        with self.assertRaisesRegex(ll.LayoutError, "v1 or v2"):
            ll.decide(self.home, V1, "v3")

    def test_status_reports_the_layout_and_the_opt_in(self) -> None:
        self.assertEqual({"layout": None, "opt_in": "absent", "opt_in_path": str(self.opt_in),
                          "problem": None}, ll.status(self.home, None))
        self.assertEqual("v1", ll.status(self.home, V1)["layout"])
        self.opt()
        value = ll.status(self.home, V2)
        self.assertEqual(("v2", "present", None), (value["layout"], value["opt_in"], value["problem"]))
        self.opt(0o666)
        self.assertEqual("refused", ll.status(self.home, V1)["opt_in"])
        self.opt_in.unlink()
        self.assertIn("--launcher-layout v1", ll.status(self.home, V2)["problem"])
        self.assertIn("launcher problem", ll.status_text(ll.status(self.home, V2)))
        self.assertIn("layout none", ll.status_text(ll.status(self.home, None)))

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(HERE / "launcher_layout.py"),
                               "--home", str(self.home), *args],
                              capture_output=True, text=True, check=False)

    def test_the_command_reads_the_receipt_and_refuses_an_unreadable_one(self) -> None:
        receipt = self.home / "receipt.json"
        receipt.write_text(json.dumps(V2))
        result = self.cli("--receipt", str(receipt))
        self.assertEqual(2, result.returncode)
        self.assertIn("--launcher-layout v1", result.stderr)
        self.assertEqual("v2\n", self.cli("--receipt", str(receipt), "--installed").stdout)
        self.assertEqual("v1\n", self.cli("--receipt", str(receipt), "--explicit", "v1").stdout)
        self.assertEqual("v1\n", self.cli("--receipt", str(self.home / "absent.json")).stdout)
        receipt.write_text("{not json")
        result = self.cli("--receipt", str(receipt))
        self.assertEqual(2, result.returncode)
        self.assertIn("unreadable", result.stderr)
        status = json.loads(self.cli("--status").stdout)
        self.assertEqual("absent", status["opt_in"])

    def v2_app(self) -> Path:
        app = self.home / "libexec/TartCILauncher.app"
        (app / "Contents/Resources").mkdir(parents=True)
        (app / "Contents/Resources/policy.json").write_text(json.dumps({"schema": 2}))
        return app

    def test_the_profile_names_the_helper_app_with_or_without_tomllib(self) -> None:
        profile = self.home / ll.PROFILE
        self.assertIsNone(ll.profile_app(profile), "control: no profile")
        profile.write_text('[host]\nid = "m1"\n# a comment\n\n[launch_helper]\n'
                           'layout = "v2"\npath = "/Users/me/libexec/TartCILauncher.app"\n'
                           '\n[lanes]\npath = "/not/this/one"\n')
        expected = Path("/Users/me/libexec/TartCILauncher.app")
        self.assertEqual(expected, ll.profile_app(profile))
        import builtins  # noqa: PLC0415
        real_import = builtins.__import__

        def without_tomllib(name, *args, **kwargs):
            if name == "tomllib":
                raise ImportError("no tomllib on Python 3.9")
            return real_import(name, *args, **kwargs)
        from unittest import mock  # noqa: PLC0415
        with mock.patch.object(builtins, "__import__", without_tomllib):
            self.assertEqual(expected, ll.profile_app(profile))
        self.assertEqual("/a/b.app", ll._profile_helper_path("[launch_helper]\npath = '/a/b.app'\n"))
        self.assertIsNone(ll._profile_helper_path('[lanes]\npath = "/x"\n'))
        profile.write_text('[launch_helper]\nlayout = "v2"\n')
        self.assertIsNone(ll.profile_app(profile))

    def test_the_profile_app_wins_over_the_receipts(self) -> None:
        app = self.v2_app()
        (self.home / ll.PROFILE).write_text(f'[launch_helper]\npath = "{app}"\n')
        receipt = {"launch_helper": {"layout": "v1", "path": "/elsewhere/TartCILauncher.app"}}
        self.assertEqual(app, ll.helper_app(self.home, receipt))
        (self.home / ll.PROFILE).unlink()
        self.assertEqual(Path("/elsewhere/TartCILauncher.app"), ll.helper_app(self.home, receipt))
        self.assertIsNone(ll.helper_app(self.home, None))

    def test_status_command_finds_a_receiptless_v2_app_through_the_profile(self) -> None:
        app = self.v2_app()
        absent = self.home / "no-receipt.json"
        self.assertIsNone(json.loads(self.cli("--status", "--receipt", str(absent)).stdout)["layout"],
                          "control: no profile names the app")
        (self.home / ll.PROFILE).write_text(f'[launch_helper]\npath = "{app}"\n')
        status = json.loads(self.cli("--status", "--receipt", str(absent)).stdout)
        self.assertEqual("v2", status["layout"])
        self.assertIn(f"a launcher v2 app at {app}", status["problem"])
        text = self.cli("--status-text", "--receipt", str(absent)).stdout
        self.assertIn("layout v2", text)

    def test_pool_status_passes_this_home_to_both_layout_reads(self) -> None:
        # The app now comes from the profile under --home, so both pool status
        # reads must name the home they report on.
        source = (HERE.parent / "tartci").read_text()
        self.assertIn('local launcher_args=(--home "$HOME")', source)
        for mode in ("--status ", "--status-text "):
            calls = [line for line in source.splitlines()
                     if "scripts/launcher_layout.py" in line and mode in line]
            self.assertEqual(1, len(calls), mode)
            self.assertIn('"${launcher_args[@]}"', calls[0])


if __name__ == "__main__":
    unittest.main()
