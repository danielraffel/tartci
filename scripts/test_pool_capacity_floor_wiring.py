#!/usr/bin/env python3
"""The pool capacity floor is wired ahead of the mutation it guards.

A guard that runs after admission is already closed protects nothing, so the
ordering inside `cmd_pool` is asserted directly, and the preflight helper is
extracted and executed against a stubbed interpreter so its exit-code contract
is exercised without touching a live pool.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DISPATCHER = REPO_ROOT / "tartci"


def extract_function(name: str) -> str:
    lines = DISPATCHER.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


class WiringOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lines = DISPATCHER.read_text(encoding="utf-8").splitlines()

    def index_of(self, needle: str, *, after: int = 0) -> int:
        for offset, line in enumerate(self.lines[after:], start=after):
            if needle in line:
                return offset
        raise AssertionError(f"not found in tartci: {needle}")

    def test_drain_is_guarded_before_admission_closes(self) -> None:
        drain = self.index_of("    drain)")
        guard = self.index_of("tartci_pool_capacity_floor_preflight drain", after=drain)
        mutation = self.index_of("tartci_pool_write_participation 0", after=drain)

        self.assertLess(guard, mutation)

    def test_off_is_guarded_before_admission_closes(self) -> None:
        branch = self.index_of('if [ "$sub" = off ]; then')
        guard = self.index_of("tartci_pool_capacity_floor_preflight off", after=branch)
        mutation = self.index_of("tartci_pool_write_participation 0", after=branch)

        self.assertLess(guard, mutation)

    def test_the_override_flag_is_documented_in_usage(self) -> None:
        usage = self.lines[self.index_of("local pool_usage=")]

        self.assertIn("--allow-last-serving-host", usage)


class PreflightContractTests(unittest.TestCase):
    """Every refusal path returns a nonzero code and names a cause."""

    def run_preflight(self, stub_exit: int, stub_output: str, *, allow: str = "0", action: str = "drain"):
        body = extract_function("tartci_pool_capacity_floor_preflight")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "harness.sh"
            script.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -uo pipefail",
                        'HERE="$1"; shift',
                        "tartci_toml_python() {",
                        '  printf \'%s\\n\' "$*" >"$STUB_ARGS"',
                        '  printf \'%s\' "$STUB_OUTPUT"',
                        '  return "$STUB_EXIT"',
                        "}",
                        body,
                        'tartci_pool_capacity_floor_preflight "$1" "$2"',
                        'echo "rc=$?"',
                    ]
                ),
                encoding="utf-8",
            )
            args_file = Path(tmp) / "args"
            env = os.environ.copy()
            env.update(
                {
                    "STUB_EXIT": str(stub_exit),
                    "STUB_OUTPUT": stub_output,
                    "STUB_ARGS": str(args_file),
                }
            )
            proc = subprocess.run(
                ["bash", str(script), str(REPO_ROOT), action, allow],
                capture_output=True, text=True, env=env, timeout=30,
            )
            forwarded = args_file.read_text(encoding="utf-8") if args_file.exists() else ""
            return proc, forwarded

    def test_an_allowed_capacity_check_proceeds(self) -> None:
        proc, forwarded = self.run_preflight(0, "pool drain keeps every required label served")

        self.assertIn("rc=0", proc.stdout)
        self.assertIn("keeps every required label served", proc.stdout)
        self.assertIn("--action drain", forwarded)
        self.assertNotIn("--allow-last-serving-host", forwarded)

    def test_a_last_serving_host_refusal_stops_the_mutation(self) -> None:
        proc, _ = self.run_preflight(3, "refusing pool drain: no host other than studio serves")

        self.assertIn("rc=11", proc.stdout)
        self.assertIn("refusing pool drain", proc.stderr)

    def test_an_indeterminate_capacity_answer_stops_the_mutation(self) -> None:
        proc, _ = self.run_preflight(4, "refusing pool drain: capacity ... could not be determined")

        self.assertIn("rc=11", proc.stdout)
        self.assertIn("could not be determined", proc.stderr)

    def test_a_missing_toml_interpreter_stops_the_mutation(self) -> None:
        proc, _ = self.run_preflight(127, "")

        self.assertIn("rc=11", proc.stdout)
        self.assertIn("TARTCI_PYTHON", proc.stderr)

    def test_a_silent_failure_still_names_a_cause(self) -> None:
        proc, _ = self.run_preflight(9, "")

        self.assertIn("rc=11", proc.stdout)
        self.assertIn("without a typed cause", proc.stderr)

    def test_the_override_is_forwarded_to_the_guard(self) -> None:
        proc, forwarded = self.run_preflight(0, "proceeding under override", allow="1")

        self.assertIn("rc=0", proc.stdout)
        self.assertIn("--allow-last-serving-host", forwarded)

    def test_the_action_is_forwarded_to_the_guard(self) -> None:
        _, forwarded = self.run_preflight(0, "ok", action="off")

        self.assertIn("--action off", forwarded)


if __name__ == "__main__":
    unittest.main()
