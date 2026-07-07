#!/usr/bin/env python3
"""Tests for the optional, read-only Orchard block in tartci status."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import status  # noqa: E402


class _Env:
    def __init__(self, **kw: str | None) -> None:
        self._kw = kw
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> "_Env":
        for k, v in self._kw.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc: object) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class OrchardStatusTests(unittest.TestCase):
    def test_unconfigured_when_url_unset(self) -> None:
        with _Env(TARTCI_ORCHARD_URL=None):
            self.assertEqual(status.orchard_status(), {"configured": False})

    def test_configured_but_unreachable_never_raises(self) -> None:
        # A closed port → configured, reachable False, an error string, no throw.
        with _Env(TARTCI_ORCHARD_URL="https://127.0.0.1:6199"):
            result = status.orchard_status()
        self.assertTrue(result["configured"])
        self.assertFalse(result["reachable"])
        self.assertIn("error", result)

    def test_status_json_always_carries_an_orchard_block(self) -> None:
        # main() must always include the orchard key (fail-safe), even with no
        # Orchard installed. Capture the emitted JSON.
        import io
        import json
        from contextlib import redirect_stdout

        with _Env(TARTCI_ORCHARD_URL=None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = status.main(["--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertIn("orchard", data)
        self.assertFalse(data["orchard"]["configured"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
