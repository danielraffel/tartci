#!/usr/bin/env python3
"""Pure contract tests for queue-tick support operations."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shipyard_queue_tick_support as support


class QueueTickSupportTests(unittest.TestCase):
    def invoke_json(
        self, function: object, value: object, **namespace: object
    ) -> tuple[int | None, str]:
        source = io.StringIO(json.dumps(value))
        output = io.StringIO()
        with mock.patch("sys.stdin", source), redirect_stdout(output):
            result = function(type("Args", (), namespace)())
        return result, output.getvalue().strip()

    def test_control_decoder_allows_additive_fields(self) -> None:
        _, control = self.invoke_json(
            support.command_control_flags,
            {
                "held": False,
                "authority_matches": True,
                "future": {"value": 1},
            },
        )
        self.assertEqual(control, "0")

    def test_control_decoder_requires_an_exact_boolean(self) -> None:
        with self.assertRaises(ValueError):
            self.invoke_json(support.command_control_flags, {"held": "false"})

    def test_merge_path_subcommands_are_gone(self) -> None:
        parser = support.build_parser()
        for retired in (
            "auth-mode",
            "app-token",
            "authority-read",
            "run-bounded",
            "mergeability",
            "reconcile-ok",
            "auto-merge-event",
            "github-origin",
        ):
            with self.subTest(retired=retired):
                with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
                    parser.parse_args([retired])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
