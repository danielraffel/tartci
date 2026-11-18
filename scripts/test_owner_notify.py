#!/usr/bin/env python3
"""The owner channel: at most once per key, and never from a test run (owner_notify.py)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import owner_notify  # noqa: E402


class OwnerNotifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "notify.json"
        self.sent: list = []

    def ok(self, text):
        self.sent.append(text)
        return True, "HTTP 204"

    def test_sends_once_per_key_and_again_for_a_new_key(self) -> None:
        self.assertTrue(owner_notify.notify_once(self.state, "a", "x", sender=self.ok)["sent"])
        self.assertEqual(owner_notify.notify_once(self.state, "a", "x", sender=self.ok)["skipped"],
                         "already sent for this episode")
        owner_notify.notify_once(self.state, "b", "y", sender=self.ok)
        self.assertEqual(self.sent, ["x", "y"])

    def test_a_failed_send_is_retried(self) -> None:
        owner_notify.notify_once(self.state, "a", "x", sender=lambda t: (False, "post failed"))
        self.assertNotIn("sent_at", json.loads(self.state.read_text()))
        self.assertTrue(owner_notify.notify_once(self.state, "a", "x", sender=self.ok)["sent"])

    def test_an_unwritable_receipt_never_sends(self) -> None:
        # A send whose receipt cannot be saved would repeat on every pass.
        state = Path(self.tmp.name) / "missing-dir" / "sub" / "notify.json"
        with mock.patch.object(owner_notify, "_test_run", return_value=True), \
                mock.patch("host_off._write_json", side_effect=OSError("read-only")):
            out = owner_notify.notify_once(state, "a", "x", sender=self.ok)
        self.assertEqual(self.sent, [])
        self.assertIn("receipt not writable", out["skipped"])

    def test_the_switch_turns_it_off(self) -> None:
        with mock.patch.dict(os.environ, {"TARTCI_OWNER_NOTIFY": "0"}):
            out = owner_notify.notify_once(self.state, "a", "x", sender=self.ok)
        self.assertEqual(self.sent, [])
        self.assertEqual(out["skipped"], "TARTCI_OWNER_NOTIFY=0")

    def test_a_non_utf8_webhook_file_is_a_failed_send_not_an_error(self) -> None:
        hook = Path(self.tmp.name) / "hook"
        hook.write_bytes(b"\xff\xfe\x00bad")
        with mock.patch.dict(os.environ, {"TARTCI_OWNER_WEBHOOK_FILE": str(hook)}), \
                mock.patch.object(owner_notify, "_test_run", return_value=False), \
                mock.patch("urllib.request.urlopen") as opened:
            ok, detail = owner_notify.post("hello")
        opened.assert_not_called()
        self.assertFalse(ok)
        self.assertIn("no readable owner webhook", detail)

    def test_the_real_sender_never_posts_from_a_test_run(self) -> None:
        hook = Path(self.tmp.name) / "hook"
        hook.write_text("https://example.invalid/webhook\n")
        with mock.patch.dict(os.environ, {"TARTCI_OWNER_WEBHOOK_FILE": str(hook)}), \
                mock.patch("urllib.request.urlopen") as opened:
            ok, detail = owner_notify.post("hello")
        self.assertFalse(ok)
        self.assertIn("never sent from a test run", detail)
        opened.assert_not_called()


ROOT = Path(__file__).resolve().parents[1]


class NoVolumesTests(unittest.TestCase):
    """During the 2026-11-18 m5studio wedge every launchd job that opened the
    Tart store volume hung. The owner message is the way out of that wedge,
    so nothing on its path may resolve under /Volumes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        (self.home / ".config/pulp/secrets/fleet").mkdir(parents=True)
        (self.home / ".config/pulp/secrets/fleet/discord-machine-room.webhook").write_text(
            "https://example.invalid/webhook\n")

    def test_the_watchdog_that_sends_runs_and_logs_off_volumes(self) -> None:
        import plistlib
        import render_launchd_template as render
        template = ROOT / "launchd" / "com.danielraffel.tartci.launchd-watchdog.plist.template"
        import re
        source = re.sub(rb"<!--.*?-->", b"", template.read_bytes(), flags=re.DOTALL)
        plist = render.replace_strings(plistlib.loads(source),
                                       {"TART_HOME": "/Volumes/Example/VMs",
                                        "HOME": "/Users/example"})
        env = plist["EnvironmentVariables"]
        paths = [*plist["ProgramArguments"][:2], plist["WorkingDirectory"],
                 plist["StandardOutPath"], plist["StandardErrorPath"], env["HOME"],
                 env["TARTCI_HOME"], *env["PATH"].split(":")]
        for path in paths:
            self.assertFalse(path.startswith("/Volumes"), path)
        # The store path is only the store: the owner path never reads it.
        self.assertEqual(env["TART_HOME"], "/Volumes/Example/VMs")

    def test_default_webhook_and_receipt_resolve_under_home(self) -> None:
        import tcc_prompt
        env = {"HOME": str(self.home)}
        with mock.patch.dict(os.environ, env), \
                mock.patch.dict(os.environ, {"TARTCI_OWNER_WEBHOOK_FILE": ""}), \
                mock.patch.dict(os.environ, {"TARTCI_HOME": ""}):
            os.environ.pop("TARTCI_OWNER_WEBHOOK_FILE")
            os.environ.pop("TARTCI_HOME")
            webhook = owner_notify.webhook_file()
            receipt = tcc_prompt._home() / "state" / tcc_prompt.NOTIFY_STATE_NAME
        for path in (webhook, receipt):
            self.assertTrue(str(path).startswith(str(self.home)), path)

    def test_a_send_opens_nothing_under_volumes(self) -> None:
        opened: list = []
        recording = [True]

        def hook(event, args):
            if recording[0] and event == "open" and args and isinstance(args[0], (str, bytes)):
                opened.append(os.fsdecode(args[0]))
        sys.addaudithook(hook)
        self.addCleanup(recording.__setitem__, 0, False)

        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        state = self.home / ".tartci" / "state" / "tcc-prompt-notify.json"
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                mock.patch.object(owner_notify, "_test_run", return_value=False), \
                mock.patch("urllib.request.urlopen", return_value=Response()):
            os.environ.pop("TARTCI_OWNER_WEBHOOK_FILE", None)
            out = owner_notify.notify_once(state, "k", "Click Allow on m5studio")
        recording[0] = False
        self.assertTrue(out["sent"], out)
        self.assertTrue(any(p.endswith("discord-machine-room.webhook") for p in opened), opened)
        # The modules themselves load from the code checkout, which in
        # production is ~/.local/share/tartci-generations (the template test
        # above); a developer checkout may sit on /Volumes.
        code = str(Path(__file__).resolve().parent)
        self.assertEqual([p for p in opened if os.path.realpath(p).startswith("/Volumes")
                          and not os.path.realpath(p).startswith(code)], [])


if __name__ == "__main__":
    unittest.main()
