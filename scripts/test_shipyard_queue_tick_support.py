#!/usr/bin/env python3
"""Pure contract tests for queue-tick support operations."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shipyard_queue_tick_support as support

SUPPORT = Path(__file__).with_name("shipyard_queue_tick_support.py")


class QueueTickSupportTests(unittest.TestCase):
    def invoke_json(
        self, function: object, value: object, **namespace: object
    ) -> tuple[int | None, str]:
        source = io.StringIO(json.dumps(value))
        output = io.StringIO()
        with mock.patch("sys.stdin", source), redirect_stdout(output):
            result = function(type("Args", (), namespace)())
        return result, output.getvalue().strip()

    def test_versioned_decoders_allow_additive_fields(self) -> None:
        _, control = self.invoke_json(
            support.command_control_flags,
            {
                "held": False,
                "authority_matches": True,
                "future": {"value": 1},
            },
        )
        self.assertEqual(control, "0|1")

        _, mergeability = self.invoke_json(
            support.command_mergeability,
            {
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "isDraft": False,
                "future": "field",
            },
        )
        self.assertEqual(mergeability, "MERGEABLE|CLEAN|false")

        _, reconcile = self.invoke_json(
            support.command_reconcile_ok,
            {
                "schema_version": 1,
                "command": "ship-state:reconcile",
                "results": [
                    {
                        "pr": 42,
                        "ok": True,
                        "changes": [],
                        "future_result": 1,
                    }
                ],
                "future_envelope": True,
            },
            pr=42,
        )
        self.assertEqual(reconcile, "1")

        _, event = self.invoke_json(
            support.command_auto_merge_event,
            {
                "schema_version": 1,
                "command": "auto-merge",
                "event": "merged",
                "pr": 42,
                "future": {"field": True},
            },
            pr=42,
        )
        self.assertEqual(event, "merged")

    def test_decoders_still_require_version_and_core_typed_fields(self) -> None:
        malformed = (
            (
                support.command_control_flags,
                {"held": "false", "authority_matches": True},
                {},
            ),
            (
                support.command_mergeability,
                {
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "isDraft": "false",
                },
                {},
            ),
            (
                support.command_auto_merge_event,
                {
                    "schema_version": 2,
                    "command": "auto-merge",
                    "event": "merged",
                    "pr": 42,
                },
                {"pr": 42},
            ),
        )
        for function, payload, namespace in malformed:
            with self.subTest(function=function.__name__):
                with self.assertRaises(ValueError):
                    self.invoke_json(function, payload, **namespace)

    def test_origin_parser_is_shared_and_exact(self) -> None:
        self.assertEqual(
            support.parse_github_origin(
                "git@github.com:Generous-Corp/pulp.git"
            ),
            "Generous-Corp/pulp",
        )
        with self.assertRaises(ValueError):
            support.parse_github_origin(
                "https://evilgithub.com/Generous-Corp/pulp.git"
            )

    def test_ledger_update_is_atomic_and_typed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            args = type(
                "Args",
                (),
                {
                    "path": str(path),
                    "repo": "owner/repo",
                    "pr": "42",
                    "outcome": "not_found",
                },
            )()
            with redirect_stdout(io.StringIO()):
                support.command_ledger_update(args)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"owner/repo#42": 1},
            )

    def test_run_bounded_forwards_service_termination_to_child_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = root / "started"
            stopped = root / "stopped"
            process_group = root / "process-group"
            child = root / "child.py"
            child.write_text(
                """import os, pathlib, signal, subprocess, sys, time
started, stopped, process_group = map(pathlib.Path, sys.argv[1:])
def stop(_signum, _frame):
    stopped.touch()
    raise SystemExit(0)
for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(signum, stop)
subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
])
process_group.write_text(str(os.getpgrp()))
started.touch()
while True:
    time.sleep(1)
""",
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(SUPPORT),
                    "run-bounded",
                    "30",
                    sys.executable,
                    str(child),
                    str(started),
                    str(stopped),
                    str(process_group),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not started.exists():
                time.sleep(0.02)
            self.assertTrue(started.exists())
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=5), 128 + signal.SIGTERM)
            self.assertTrue(stopped.exists())
            group = int(process_group.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.fail("bounded child process group survived termination")

    def test_run_bounded_defers_signal_until_child_is_registered(self) -> None:
        class FakeProcess:
            pid = 4242

            def poll(self) -> None:
                return None

            def wait(self, timeout: int | None = None) -> int:
                return 0

        fake_process = FakeProcess()

        def spawn(*_args: object, **_kwargs: object) -> FakeProcess:
            os.kill(os.getpid(), signal.SIGTERM)
            return fake_process

        args = type(
            "Args",
            (),
            {"argv": ["shipyard", "runner", "steward"], "timeout": 30},
        )()
        with (
            mock.patch.object(support.subprocess, "Popen", side_effect=spawn),
            mock.patch.object(support.os, "killpg") as killpg,
            self.assertRaises(SystemExit) as raised,
        ):
            support.command_run_bounded(args)
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        killpg.assert_any_call(fake_process.pid, signal.SIGTERM)
        killpg.assert_any_call(fake_process.pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
