#!/usr/bin/env python3
"""Who changed the pool or a lane (scripts/manual_change_log.py)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manual_change_log as mcl  # noqa: E402


class Tests(unittest.TestCase):
    def test_the_parent_chain_is_walked_to_launchd_and_bounded(self) -> None:
        table = {900: (800, "tartci pool off --force"), 800: (700, "-bash"),
                 700: (1, "sshd: daniel@ttys004")}
        chain = mcl.parents(900, ps=lambda pid: table.get(pid))
        self.assertEqual([c["command"] for c in chain],
                         ["tartci pool off --force", "-bash", "sshd: daniel@ttys004"])
        loop = mcl.parents(5, ps=lambda pid: (pid + 1, "x"))
        self.assertEqual(len(loop), mcl.MAX_PARENTS)

    def test_the_caller_is_named(self) -> None:
        self.assertEqual(mcl.caller({"SSH_CONNECTION": "192.0.2.9 1 192.0.2.5 22"})["via"], "ssh")
        self.assertEqual(mcl.caller({"SSH_CONNECTION": "192.0.2.9 1 192.0.2.5 22"})["ssh_client"],
                         "192.0.2.9")
        self.assertEqual(mcl.caller({"TARTCI_LAUNCHD": "1"})["via"], "launchd")
        launchd = mcl.caller({"XPC_SERVICE_NAME": "com.danielraffel.tartci.self-update",
                              "SSH_CONNECTION": "192.0.2.9 1 192.0.2.5 22"})
        self.assertEqual((launchd["via"], launchd["launchd_job"]),
                         ("launchd", "com.danielraffel.tartci.self-update"))
        self.assertEqual(mcl.caller({})["via"], "terminal")

    def test_a_row_is_appended_and_an_unwritable_log_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "logs" / "manual-changes.log"
            with mock.patch.dict(os.environ, {"TARTCI_MANUAL_CHANGE_LOG": str(log)}):
                mcl.record("pool off", "refused", ["--plan"], env={}, start=1)
                mcl.record("pool on", "requested", [], env={}, start=1)
            rows = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertEqual([r["action"] for r in rows], ["pool off", "pool on"])
            (Path(td) / "ro").mkdir(mode=0o500)
            with mock.patch.dict(os.environ, {"TARTCI_MANUAL_CHANGE_LOG":
                                              str(Path(td) / "ro" / "x" / "log")}):
                mcl.record("pool off", "x", [], env={}, start=1)


class Redaction(unittest.TestCase):
    # Each secret is a 24-character token found nowhere else, so its absence
    # from the log can only mean it was redacted (test_leak_needles).
    PLANTED = {
        "GH_TOKEN=ghp_Qm7ZxV2kLp9WtR4nYc8HbJ3s gh api repos/x": "ghp_Qm7ZxV2kLp9WtR4nYc8HbJ3s",
        'curl -H "Authorization: token gho_Ht5NwQ8zLd2VkP7rXm4CyB9e" https://api.github.com':
            "gho_Ht5NwQ8zLd2VkP7rXm4CyB9e",
        'curl -H "Authorization: Bearer Br3XkW8mQz5LtN2vPc7YhJ4d" u': "Br3XkW8mQz5LtN2vPc7YhJ4d",
        "curl -H 'Authorization: Basic Ba9QwE4rTz7YuI2oPl5KjH8g' u": "Ba9QwE4rTz7YuI2oPl5KjH8g",
        "env MY_SECRET=Sc6VbN1mQw8ErT3yUi9OpA4s run": "Sc6VbN1mQw8ErT3yUi9OpA4s",
        "tool --password Pw2LkJ7hGf4DsA9zXc6VbN3m": "Pw2LkJ7hGf4DsA9zXc6VbN3m",
        "tool --api-key=Ak5QzW8xEc3RvT6bYn1UmI4o": "Ak5QzW8xEc3RvT6bYn1UmI4o",
        "echo github_pat_11Gp4HnJ8kLm2QwE7rTy5UiO": "github_pat_11Gp4HnJ8kLm2QwE7rTy5UiO",
        "echo ghs_Gs9TyU3iOp6AsD1fGh8JkL4z": "ghs_Gs9TyU3iOp6AsD1fGh8JkL4z",
        "git clone https://user:Ur7PqW2eRt9YuI4oPa6SdF3g@example.com/x.git":
            "Ur7PqW2eRt9YuI4oPa6SdF3g",
        "git clone https://oauth2:glpat-Gl4KjH9gFd2SaZ7xCv5BnM8q@gitlab.com/x.git":
            "glpat-Gl4KjH9gFd2SaZ7xCv5BnM8q",
        "security unlock-keychain -p Kc8MnB3vCx6ZaS1dFg4HjK7l /Users/x/signing.keychain-db":
            "Kc8MnB3vCx6ZaS1dFg4HjK7l",
        "curl -u admin:Cu2WqE5rTy8UiO3pAs6DfG9h https://api.example.com":
            "Cu2WqE5rTy8UiO3pAs6DfG9h",
        "AWS_SECRET_ACCESS_KEY=Aw6ZxC9vBn2MqW5eRt8YuI1o aws s3 ls": "Aw6ZxC9vBn2MqW5eRt8YuI1o",
        "env SHIPYARD_GITHUB_APP_PRIVATE_KEY=Gk3RtY7uIo1PaS5dFg9HjK2l shipyard pr":
            "Gk3RtY7uIo1PaS5dFg9HjK2l",
    }

    def test_planted_secrets_never_reach_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "manual-changes.log"
            # One record per planted command, each as the caller's parent: the
            # chain is capped at MAX_PARENTS, so one long chain would drop some.
            with mock.patch.dict(os.environ, {"TARTCI_MANUAL_CHANGE_LOG": str(log)}):
                for command in self.PLANTED:
                    mcl.record("pool off", "forced", ["--force"], env={}, start=900,
                               ps=lambda p, c=command: (1, c) if p == 900 else None)
                mcl.record("pool off", "forced", ["--force", "GH_TOKEN=ghp_Ar4GsX9cVb2NmQ7wEr5TyU8i"],
                           env={}, start=1)
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(rows), len(self.PLANTED) + 1)
            self.assertTrue(all(r["parents"] for r in rows[:-1]))
            text = log.read_text()
            for command, secret in self.PLANTED.items():
                with self.subTest(command=command[:30]):
                    self.assertNotIn(secret, text)
            self.assertNotIn("ghp_Ar4GsX9cVb2NmQ7wEr5TyU8i", text)
            self.assertIn("[redacted]", text)
            # Control: an ordinary command line is kept whole.
            for plain in ("tartci pool off --force --plan", "ssh-keyscan -T 5 m5-lan",
                          "git push --no-verify origin main",
                          "ssh -o StrictHostKeyChecking=accept-new m5",
                          "codesign --keychain /Users/x/pulp.keychain-db -s ID a.app",
                          "SHIPYARD_GITHUB_APP_PRIVATE_KEY_PATH=/x/key.pem shipyard pr",
                          "sort -u names.txt", "git clone https://github.com/o/r.git"):
                self.assertEqual(mcl.redact(plain), plain)

    def test_the_log_is_owner_only_and_an_existing_one_is_tightened(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "manual-changes.log"
            with mock.patch.dict(os.environ, {"TARTCI_MANUAL_CHANGE_LOG": str(log)}):
                mcl.record("pool on", "requested", [], env={}, start=1)
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
                log.chmod(0o644)
                mcl.record("pool on", "requested", [], env={}, start=1)
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(log.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
