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


if __name__ == "__main__":
    unittest.main(verbosity=2)
