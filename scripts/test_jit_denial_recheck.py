#!/usr/bin/env python3
"""Behavioral tests for the self-clearing JIT denial receipt.

A `repository_access` receipt (the runner-group precheck answered "deny") is
re-read on a bounded interval and removed only when the precheck now passes. A
`credential` receipt, or one written before the `cause=` field existed, never
auto-clears. The real receipt functions are sliced out of the macOS provider and
driven with a stub precheck script, so the tests run the provider's own text.

Run:  python3 scripts/test_jit_denial_recheck.py
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers/tart-macos/runner.sh"
FUNCS = (
    "jit_denial_file_for", "jit_denial_cause", "jit_denial_recheck",
    "jit_admission_denied", "record_jit_admission_denied",
    "clear_jit_admission_denied",
)


def function_text(source: str, name: str) -> str:
    m = re.search(rf"^{name}\(\)\{{\n.*?^\}}$", source, re.MULTILINE | re.DOTALL)
    if m is None:
        raise AssertionError(f"missing function {name}")
    return m.group(0)


def preamble(source: str) -> str:
    m = re.search(r"^JIT_DENIAL_RECHECK_SECS=.*?\n.*?\n", source, re.MULTILINE)
    assert m, "missing JIT_DENIAL_RECHECK_SECS"
    return m.group(0)


class JitDenialRecheck(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        (self.tmp / "scripts").mkdir()
        self.calls = self.tmp / "precheck-calls"
        (self.tmp / "scripts/runner_group_repository_access.py").write_text(
            "import os, sys\n"
            f"open({str(self.calls)!r}, 'a').write('x')\n"
            "rc = int(os.environ['STUB_RC'])\n"
            "print('{\"verdict\": \"allow\"}' if rc == 0 else '{\"verdict\": \"deny\"}')\n"
            "sys.exit(rc)\n"
        )
        src = RUNNER.read_text()
        self.lib = preamble(src) + "\n".join(function_text(src, f) for f in FUNCS)

    def drive(self, script: str, *, rc: int, now: int = 1000, secs: str = "900") -> subprocess.CompletedProcess:
        full = (
            "set -u\n"
            f"STATE_DIR={self.tmp}; REPO=o/r; JIT_GH_CLI=ghapp; TARTCI_ROOT={self.tmp}\n"
            f"TARTCI_JIT_DENIAL_RECHECK_SECS={secs}\n"
            f"STUB_RC={rc}; export STUB_RC\n"
            f"now_epoch(){{ echo {now}; }}\n"
            "event(){ echo \"EVENT $*\" >&2; }\nnote(){ echo \"NOTE $*\" >&2; }\n"
            + self.lib + "\n" + script
        )
        return subprocess.run(["bash", "-c", full], capture_output=True, text=True)

    def receipts(self) -> list[Path]:
        return [p for p in self.tmp.glob("jit-admission-denied.*") if p.suffix != ".recheck"]

    def n_calls(self) -> int:
        return len(self.calls.read_text()) if self.calls.exists() else 0

    def test_policy_receipt_clears_when_precheck_passes(self) -> None:
        r = self.drive('record_jit_admission_denied 8 L "d" repository_access; '
                       'jit_admission_denied 8 L && echo DENIED || echo CLEAR', rc=0)
        self.assertIn("CLEAR", r.stdout, r.stderr)
        self.assertEqual(self.receipts(), [])
        self.assertIn("EVENT jit_denial_cleared", r.stderr)
        self.assertIn("allow", r.stderr)  # evidence is logged

    def test_policy_receipt_stays_when_precheck_still_fails(self) -> None:
        r = self.drive('record_jit_admission_denied 8 L "d" repository_access; '
                       'jit_admission_denied 8 L && echo DENIED || echo CLEAR', rc=3)
        self.assertIn("DENIED", r.stdout, r.stderr)
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(self.n_calls(), 1)

    def test_credential_receipt_never_auto_clears(self) -> None:
        r = self.drive('record_jit_admission_denied 8 L "d" credential; '
                       'jit_admission_denied 8 L && echo DENIED || echo CLEAR', rc=0)
        self.assertIn("DENIED", r.stdout, r.stderr)
        self.assertEqual(self.n_calls(), 0)

    def test_default_cause_and_legacy_receipt_stay_manual(self) -> None:
        r = self.drive('record_jit_admission_denied 8 L "d"; '
                       'f="$(jit_denial_file_for 8 L)"; grep -x cause=credential "$f"; '
                       'grep -v "^cause=" "$f" > "$f.new"; mv "$f.new" "$f"; '
                       'jit_admission_denied 8 L && echo DENIED || echo CLEAR', rc=0)
        self.assertIn("DENIED", r.stdout, r.stderr)
        self.assertEqual(self.n_calls(), 0)

    def test_interval_is_respected(self) -> None:
        # Three polls inside one interval run the precheck once; the first
        # poll after the interval runs it again.
        script = (
            'record_jit_admission_denied 8 L "d" repository_access\n'
            'jit_admission_denied 8 L; jit_admission_denied 8 L; jit_admission_denied 8 L\n'
        )
        self.drive(script, rc=3)
        self.assertEqual(self.n_calls(), 1)
        self.drive('jit_admission_denied 8 L', rc=3, now=1000 + 899)
        self.assertEqual(self.n_calls(), 1)
        self.drive('jit_admission_denied 8 L', rc=3, now=1000 + 900)
        self.assertEqual(self.n_calls(), 2)

    def test_zero_interval_disables_recheck(self) -> None:
        r = self.drive('record_jit_admission_denied 8 L "d" repository_access; '
                       'jit_admission_denied 8 L && echo DENIED || echo CLEAR', rc=0, secs="0")
        self.assertIn("DENIED", r.stdout, r.stderr)
        self.assertEqual(self.n_calls(), 0)

    def test_provider_records_policy_cause_only_for_precheck_deny(self) -> None:
        src = RUNNER.read_text()
        self.assertIn('[ "$access_rc" -eq 3 ] && access_cause=repository_access', src)
        # The JIT-mint 401/403/404 path passes no cause, so it defaults to credential.
        mint = src[src.index("GitHub rejected JIT registration"):]
        self.assertNotIn("repository_access", mint[:300])


if __name__ == "__main__":
    unittest.main()
