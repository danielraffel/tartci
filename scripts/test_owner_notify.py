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


if __name__ == "__main__":
    unittest.main()
