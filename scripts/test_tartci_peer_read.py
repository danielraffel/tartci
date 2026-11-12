#!/usr/bin/env python3
"""tartci-peer-read: exactly today's peer reads run; everything else exits 77."""

from __future__ import annotations

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

import capacity_floor  # noqa: E402
import fleet_self_update as su  # noqa: E402
import job_claim  # noqa: E402
import tartci_peer_read as pr  # noqa: E402

SCRIPT = Path(pr.__file__)


class Recorder:
    """A System whose every ssh call is recorded and answered with nothing."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def run(self, argv, **_kw):
        self.argv.append(list(argv))
        if argv[-1] == pr.POOL_STATUS:
            return su.Result(0, json.dumps({"state": "on", "participating": True}), "")
        return su.Result(0, "1700000000\n", "")

    def now(self) -> float:
        return 1700000000.0


class ClientsSendOnlyAllowedCommands(unittest.TestCase):
    """The allowlist is what the clients in this tree actually send."""

    def setUp(self) -> None:
        self.allowed = pr.allowed_commands()

    def test_fleet_self_update_peer_probe(self) -> None:
        rec = Recorder()
        su.read_peer(mock.Mock(), rec, "m5", "m5")
        sent = [argv[-1] for argv in rec.argv]
        self.assertEqual([self.allowed["pool-status"]], sent[:1])
        self.assertIn(self.allowed["self-update-marker"], sent)
        for command in sent:
            self.assertEqual(command, pr.resolve(command, self.allowed))

    def test_fleet_self_update_canary_read(self) -> None:
        rec = Recorder()
        with mock.patch.object(su, "self_host_id", return_value="m1"), \
                mock.patch.object(su, "published_peers", return_value={"m5": "m5"}):
            su.read_canary_verified(mock.Mock(), rec, "m5")
        self.assertEqual([self.allowed["fleet-source-verified"]], [a[-1] for a in rec.argv])

    def test_job_claim_and_capacity_floor(self) -> None:
        self.assertEqual(self.allowed["job-claim-status"], job_claim.PEER_STATUS_COMMAND)
        self.assertEqual(self.allowed["pool-status"], capacity_floor.PEER_STATUS_COMMAND)


class Dispatch(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.home / ".local/bin").mkdir(parents=True)
        fake = self.home / ".local/bin/tartci"
        fake.write_text('#!/bin/sh\nprintf \'{"argv": "%s"}\\n\' "$*"\n')
        fake.chmod(0o755)
        self.tartci_home = self.tmp / "tartci"
        state = self.tartci_home / "state/self-update"
        state.mkdir(parents=True)
        (state / "active.json").write_text(json.dumps({"target": "abc"}))
        (state / su.FLEET_SOURCE_VERIFIED).write_text(json.dumps({"commit": "c" * 40}))
        self.pwned = self.tmp / "pwned"

    def env(self, command: str | None) -> dict[str, str]:
        env = {"HOME": str(self.home), "TARTCI_HOME": str(self.tartci_home),
               "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "SSH_CONNECTION": "100.64.0.9 50000 100.64.0.1 22"}
        if command is not None:
            env["SSH_ORIGINAL_COMMAND"] = command
        return env

    def dispatch(self, command: str | None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT)], env=self.env(command),
                              capture_output=True, text=True, timeout=60, cwd=self.tmp)

    def direct(self, command: str) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/sh", "-c", command], env=self.env(None),
                              capture_output=True, text=True, timeout=60, cwd=self.tmp)

    def audit_lines(self) -> list[str]:
        log = self.tartci_home / "state/peer-read/audit.log"
        return log.read_text().splitlines() if log.exists() else []

    # Negative: anything but an allowed read is refused, unrun, and audited once.
    def test_everything_else_is_refused_with_one_audit_line(self) -> None:
        allowed = pr.allowed_commands()
        hostile = [
            None, "", "id", f"touch {self.pwned}", "bash", "sh -i",
            "pool-status; touch " + str(self.pwned),
            allowed["pool-status"] + f"; touch {self.pwned}",
            allowed["pool-status"] + "\n",
            " " + allowed["job-claim-status"],
            allowed["self-update-marker"][:-1],
            "cd ~ && ~/.local/bin/tartci pool drain",
            "POOL-STATUS",
        ]
        for count, command in enumerate(hostile, start=1):
            with self.subTest(command=command):
                proc = self.dispatch(command)
                self.assertEqual(pr.EX_REFUSED, proc.returncode)
                self.assertEqual("", proc.stdout)
                self.assertEqual(1, len(proc.stderr.splitlines()), proc.stderr)
                self.assertIn("tartci-peer-read refused from=100.64.0.9", proc.stderr)
                self.assertEqual(count, len(self.audit_lines()))
                self.assertFalse(self.pwned.exists())

    def test_an_unloadable_allowlist_refuses(self) -> None:
        with mock.patch.object(pr, "allowed_commands", side_effect=ImportError("x")), \
                mock.patch.object(pr.os, "execv") as execv, \
                mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.tartci_home)}), \
                mock.patch("sys.stderr"):
            rc = pr.main({"SSH_ORIGINAL_COMMAND": "pool-status"})
        self.assertEqual(pr.EX_REFUSED, rc)
        execv.assert_not_called()
        self.assertIn("reason=allowlist_unavailable", self.audit_lines()[0])

    # Positive: each verb and each legacy string prints what the command prints.
    def test_each_verb_and_legacy_string_matches_the_direct_command(self) -> None:
        for verb, command in pr.allowed_commands().items():
            expected = self.direct(command)
            self.assertEqual(0, expected.returncode, (verb, expected.stderr))
            for asked in (verb, command):
                with self.subTest(verb=verb, legacy=asked == command):
                    proc = self.dispatch(asked)
                    self.assertEqual(0, proc.returncode, proc.stderr)
                    if verb == "pool-status" or verb == "job-claim-status":
                        self.assertEqual(expected.stdout, proc.stdout)
                        self.assertTrue(json.loads(proc.stdout)["argv"])
                    else:
                        # Both print `date +%s` first; the rest is byte-identical.
                        self.assertEqual(expected.stdout.split("\n", 1)[1],
                                         proc.stdout.split("\n", 1)[1])
        self.assertEqual([], self.audit_lines(), "an allowed read is not a refusal")

    def test_the_marker_read_still_parses_as_the_client_expects(self) -> None:
        proc = self.dispatch("self-update-marker")
        first, _, rest = proc.stdout.partition("\n")
        self.assertTrue(first.strip().isdigit())
        self.assertIn('"target": "abc"', rest.partition(su.WAITING_SEPARATOR)[0])


if __name__ == "__main__":
    unittest.main()
