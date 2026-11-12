#!/usr/bin/env python3
"""The keychain-unlock agent unlocks only the dedicated keychain, never exposes
its password, and never reads settings from a locked keychain."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import keychain_unlock  # noqa: E402
import tartci_launchd_watchdog as wd  # noqa: E402

SECRET = 'Pw-Q7 ss"w\\\\rd'


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.home / ".tartci")})
        env.start()
        self.addCleanup(env.stop)
        kc = self.home / "Library" / "Keychains"
        kc.mkdir(parents=True)
        self.legacy = kc / "pulp-signing.keychain-db"
        self.sibling = kc / "pulp-signing-unattended.keychain-db"
        self.legacy.write_text("")
        self.sibling.write_text("")
        secrets = self.home / ".config" / "pulp" / "secrets"
        secrets.mkdir(parents=True)
        (secrets / "keychain.env").write_text(
            f"PULP_SIGN_KEYCHAIN={self.legacy}\nPULP_SIGN_KEYCHAIN_PW='{SECRET}'\n")
        self.scripts: list[str] = []
        self.unlock_rc = 0
        self.info = "no-timeout"

    def interactive(self, script: str) -> tuple[int, str]:
        self.scripts.append(script)
        if script.startswith("unlock-keychain"):
            return self.unlock_rc, ("" if self.unlock_rc == 0 else
                                    f"security: SecKeychainUnlock {self.sibling}: The user name or "
                                    f"passphrase you entered is not correct. ({SECRET})")
        return 0, f'Keychain "{self.sibling}" {self.info}'

    def run_(self) -> dict:
        return keychain_unlock.run(self.home, self.interactive)


class UnlockTests(Fixture):
    def test_unlocks_the_dedicated_keychain_on_stdin_and_clears_auto_lock(self) -> None:
        value = self.run_()
        self.assertEqual((value["state"], value["keychain"]), ("ok", str(self.sibling)))
        unlock, settings = self.scripts
        self.assertIn(f'"{self.sibling}"', unlock)
        self.assertIn(keychain_unlock._quote(SECRET), unlock)
        self.assertIn("set-keychain-settings", settings)
        self.assertEqual(keychain_unlock.last(self.home)["state"], "ok")
        state = keychain_unlock.state_path(self.home).read_text()
        for word in SECRET.split():
            self.assertNotIn(word, state)

    def test_the_password_is_never_an_argument(self) -> None:
        seen = {}

        def fake_run(argv, **kw):
            seen.setdefault("argv", []).append(argv)
            seen.setdefault("input", []).append(kw.get("input"))
            return subprocess.CompletedProcess(argv, 0, "no-timeout", "")
        with mock.patch.object(keychain_unlock.subprocess, "run", side_effect=fake_run):
            self.assertEqual(keychain_unlock.run(self.home)["state"], "ok")
        self.assertTrue(all(argv == ["/usr/bin/security", "-i"] for argv in seen["argv"]))
        self.assertIn(keychain_unlock._quote(SECRET), seen["input"][0])

    def test_a_wrong_password_fails_redacted_and_never_reads_settings(self) -> None:
        self.unlock_rc = 51
        value = self.run_()
        self.assertEqual(value["state"], "failed")
        self.assertEqual(len(self.scripts), 1)  # no show-keychain-info on a locked keychain
        self.assertNotIn(SECRET, value["detail"])
        self.assertIn("<redacted>", value["detail"])

    def test_a_keychain_that_still_relocks_is_reported(self) -> None:
        self.info = "lock-on-sleep timeout=300s"
        self.assertEqual(self.run_()["state"], "failed")

    def test_no_keychain_env_is_a_no_op(self) -> None:
        (self.home / ".config" / "pulp" / "secrets" / "keychain.env").unlink()
        self.assertEqual(self.run_()["state"], "not_applicable")
        self.assertEqual(self.scripts, [])

    def test_quoting_round_trips_through_security_tokenizer_rules(self) -> None:
        self.assertEqual(keychain_unlock._quote('a"b\\c'), '"a\\"b\\\\c"')


class AgentShapeTests(unittest.TestCase):
    def test_the_template_runs_at_login_and_every_15_minutes_without_a_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run(
                [sys.executable, str(HERE / "render_launchd_template.py"),
                 str(HERE.parent / "launchd" / f"{keychain_unlock.LABEL}.plist.template"),
                 "--set", f"HOME={tmp}"], capture_output=True, check=True)
            plist = plistlib.loads(out.stdout)
        self.assertEqual(plist["Label"], keychain_unlock.LABEL)
        self.assertIs(plist["RunAtLoad"], True)
        self.assertEqual(plist["StartInterval"], keychain_unlock.INTERVAL_SECS)
        self.assertEqual(plist["ProgramArguments"][-1], "keychain-unlock")
        self.assertNotIn("KEYCHAIN_PW", plistlib.dumps(plist).decode())

    def test_the_watchdog_reinstalls_it_only_where_keychain_env_exists(self) -> None:
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "keychain unlock agent: installed and loaded", "")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(wd.keychain_unlock_agent_pass(tmp, fake_run))
            self.assertEqual(calls, [])
            secrets = Path(tmp) / ".config" / "pulp" / "secrets"
            secrets.mkdir(parents=True)
            (secrets / "keychain.env").write_text("")
            line = wd.keychain_unlock_agent_pass(tmp, fake_run)
        self.assertIn("(re)installed", line)
        self.assertTrue(calls[0][-2].endswith("install_keychain_unlock_agent.sh"))

    def test_setup_and_the_heal_pass_install_it(self) -> None:
        tartci = (HERE.parent / "tartci").read_text()
        self.assertIn('"$HERE/scripts/install_keychain_unlock_agent.sh" --install', tartci)
        self.assertIn("keychain-unlock) shift; cmd_keychain_unlock", tartci)
        source = (HERE / "tartci_launchd_watchdog.py").read_text()
        self.assertIn("unlock_line = keychain_unlock_agent_pass()", source[source.index("def main("):])


if __name__ == "__main__":
    unittest.main()
