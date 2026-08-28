#!/usr/bin/env python3
"""Static safety checks for the M1-only scoped JIT GitHub wrapper."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "tartci-m1-stackbench-jit-gh"


class M1StackbenchJitWrapperTests(unittest.TestCase):
    def test_wrapper_keeps_token_file_backed_and_scoped(self) -> None:
        body = WRAPPER.read_text(encoding="utf-8")
        self.assertIn('token_file="$HOME/.config/pulp/secrets/ghcr-stackbench-token"', body)
        self.assertIn("stat -f '%Lp'", body)
        self.assertIn('[ "$mode" = "600" ]', body)
        self.assertIn('exec env GH_TOKEN="$token" /opt/homebrew/bin/gh "$@"', body)
        self.assertNotIn("exec ghapp", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
