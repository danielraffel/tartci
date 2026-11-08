#!/usr/bin/env python3
"""Behavioral tests for the self-clearing JIT denial receipt.

A `repository_access` receipt (the runner-group precheck answered "deny") is
re-read on a bounded interval and removed only when the precheck now passes. A
`credential` receipt, or one written before the `cause=` field existed, never
auto-clears, unless its recorded source error is a GitHub rate limit (a
transient condition that older providers misfiled as a credential refusal).
A rate limit at the JIT boundary writes no receipt at all; it backs off. The real receipt functions are sliced out of the macOS provider and
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
PROOF_LIB = ROOT / "providers/tart-macos/boundary-proof.lib.sh"
FUNCS = (
    "jit_denial_file_for", "jit_denial_cause", "jit_denial_source",
    "jit_denial_recheck_cause", "jit_denial_recheck",
    "jit_admission_denied", "record_jit_admission_denied",
    "clear_jit_admission_denied", "record_github_rate_limited",
    "github_rate_limit_backoff_active", "clear_github_rate_limited",
)
LIB_FUNCS = (
    "tartci_github_rate_limited", "tartci_github_auth_rejected",
    "tartci_repository_access_denied", "tartci_repository_access_reason",
)

# The error m5's vellum-gate lane latched as `cause=credential` on 2026-11-05:
# a primary rate limit, which GitHub reports as HTTP 403.
PRIMARY_RATE_LIMIT = """runner repository access error: GitHub API failed for repos/o/r: {
\t"message": "API rate limit exceeded for installation ID 147677577. If you reach out to GitHub Support for help, please include the request ID X",
\t"documentation_url": "https://docs.github.com/en/rest/using-the-rest-api/getting-started-with-the-rest-api#rate-limiting",
\t"status": "403"
}
gh: API rate limit exceeded for installation ID 147677577. (HTTP 403)
"""
SECONDARY_RATE_LIMIT = "gh: You have exceeded a secondary rate limit. Please wait a few minutes before you try again. (HTTP 403)\n"
TOO_MANY = "gh: HTTP 429: Too Many Requests\nRetry-After: 600\n"
CREDENTIAL_403 = "gh: Resource not accessible by integration (HTTP 403)\n"
CREDENTIAL_401 = "gh: Bad credentials (HTTP 401)\n"


def function_text(source: str, name: str) -> str:
    m = re.search(rf"^{name}\(\)\{{\n.*?^\}}$", source, re.MULTILINE | re.DOTALL)
    if m is None:
        raise AssertionError(f"missing function {name}")
    return m.group(0)


def preamble(source: str) -> str:
    m = re.search(r"^JIT_DENIAL_RECHECK_SECS=.*?\n.*?\n", source, re.MULTILINE)
    assert m, "missing JIT_DENIAL_RECHECK_SECS"
    r = re.search(r"^GITHUB_RATE_LIMIT_FILE=.*?\n(?:.*\n){4}", source, re.MULTILINE)
    assert r, "missing GITHUB_RATE_LIMIT_* settings"
    return m.group(0) + r.group(0)


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
        lib = PROOF_LIB.read_text()
        self.lib = (
            "\n".join(function_text(lib, f) for f in LIB_FUNCS) + "\n"
            + preamble(src) + "\n".join(function_text(src, f) for f in FUNCS)
        )

    def error_file(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.write_text(text)
        return path

    def drive(self, script: str, *, rc: int = 0, now: int = 1000, secs: str = "900",
              env: str = "") -> subprocess.CompletedProcess:
        full = (
            "set -u\n"
            f"STATE_DIR={self.tmp}; REPO=o/r; JIT_GH_CLI=ghapp; TARTCI_ROOT={self.tmp}\n"
            f"TARTCI_JIT_DENIAL_RECHECK_SECS={secs}\n{env}\n"
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


    # -- rate limits are transient ------------------------------------------

    def classify(self, text: str, rc: int = 1) -> str:
        err = self.error_file("err", text)
        r = self.drive(
            f'tartci_repository_access_denied {rc} {err} && echo DENIED || echo NOT_DENIED; '
            f'tartci_github_rate_limited {err} && echo RATE || echo NO_RATE; '
            f'tartci_repository_access_reason {err}; echo')
        return r.stdout

    def test_primary_rate_limit_403_is_not_a_denial(self) -> None:
        out = self.classify(PRIMARY_RATE_LIMIT)
        self.assertIn("NOT_DENIED", out)
        self.assertIn("RATE", out)
        self.assertIn("reason=rate_limited", out)

    def test_secondary_and_429_rate_limits_are_not_denials(self) -> None:
        for text in (SECONDARY_RATE_LIMIT, TOO_MANY):
            out = self.classify(text)
            self.assertIn("NOT_DENIED", out, text)
            self.assertIn("RATE", out, text)

    def test_control_real_credential_refusals_still_deny(self) -> None:
        for text in (CREDENTIAL_403, CREDENTIAL_401):
            out = self.classify(text)
            self.assertIn("DENIED", out.split()[0], text)
            self.assertIn("NO_RATE", out, text)
        # The precheck's own policy verdict denies whatever stderr says.
        self.assertTrue(self.classify(PRIMARY_RATE_LIMIT, rc=3).startswith("DENIED"))

    def legacy_receipt(self, source: Path) -> str:
        return (f'record_jit_admission_denied 8 L '
                f'"repository access denied before JIT registration; inspect {source}" credential; ')

    def test_legacy_credential_receipt_from_rate_limit_clears(self) -> None:
        src = self.error_file("vm.repository-access-error", PRIMARY_RATE_LIMIT)
        r = self.drive(self.legacy_receipt(src)
                       + 'jit_admission_denied 8 L && echo DENIED || echo CLEAR', rc=0)
        self.assertIn("CLEAR", r.stdout, r.stderr)
        self.assertEqual(self.receipts(), [])
        self.assertIn("cause=credential_rate_limited", r.stderr)

    def test_legacy_rate_limit_receipt_stays_while_precheck_fails(self) -> None:
        src = self.error_file("vm.repository-access-error", PRIMARY_RATE_LIMIT)
        r = self.drive(self.legacy_receipt(src)
                       + 'jit_admission_denied 8 L && echo DENIED || echo CLEAR', rc=1)
        self.assertIn("DENIED", r.stdout, r.stderr)
        self.assertEqual(self.n_calls(), 1)

    def test_control_legacy_receipt_from_real_credential_error_stays_manual(self) -> None:
        for text in (CREDENTIAL_403, CREDENTIAL_401):
            src = self.error_file("vm.repository-access-error", text)
            r = self.drive(self.legacy_receipt(src)
                           + 'jit_admission_denied 8 L && echo DENIED || echo CLEAR', rc=0)
            self.assertIn("DENIED", r.stdout, r.stderr)
        self.assertEqual(self.n_calls(), 0)

    def test_control_legacy_receipt_with_missing_source_stays_manual(self) -> None:
        r = self.drive(self.legacy_receipt(self.tmp / "gone")
                       + 'jit_admission_denied 8 L && echo DENIED || echo CLEAR', rc=0)
        self.assertIn("DENIED", r.stdout, r.stderr)
        self.assertEqual(self.n_calls(), 0)

    def test_mint_receipt_source_is_parsed_through_trailing_text(self) -> None:
        src = self.error_file("vm.jit-error", SECONDARY_RATE_LIMIT)
        r = self.drive(f'record_jit_admission_denied 8 L "GitHub rejected JIT registration; '
                       f'inspect {src} and change the explicit JIT auth route before retrying"; '
                       'jit_admission_denied 8 L && echo DENIED || echo CLEAR', rc=0)
        self.assertIn("CLEAR", r.stdout, r.stderr)

    def rate_state(self) -> dict[str, str]:
        text = (self.tmp / "github-rate-limited").read_text()
        return dict(line.split("=", 1) for line in text.splitlines())

    def test_rate_limit_backoff_doubles_caps_and_clears(self) -> None:
        err = self.error_file("err", PRIMARY_RATE_LIMIT)
        env = ("TARTCI_GITHUB_RATE_LIMIT_BACKOFF_SECS=100\n"
               "TARTCI_GITHUB_RATE_LIMIT_BACKOFF_MAX_SECS=350")
        seen = []
        for _ in range(4):
            self.drive(f"record_github_rate_limited {err}", env=env)
            seen.append(int(self.rate_state()["backoff_secs"]))
        self.assertEqual(seen, [100, 200, 350, 350])
        self.assertEqual(self.rate_state()["until"], str(1000 + 350))
        r = self.drive("github_rate_limit_backoff_active && echo ACTIVE || echo IDLE", now=1349, env=env)
        self.assertIn("ACTIVE", r.stdout)
        r = self.drive("github_rate_limit_backoff_active && echo ACTIVE || echo IDLE", now=1350, env=env)
        self.assertIn("IDLE", r.stdout)
        self.drive("clear_github_rate_limited", env=env)
        self.assertFalse((self.tmp / "github-rate-limited").exists())
        self.drive(f"record_github_rate_limited {err}", env=env)
        self.assertEqual(self.rate_state()["backoff_secs"], "100")

    def test_rate_limit_backoff_honours_retry_after(self) -> None:
        err = self.error_file("err", TOO_MANY)
        self.drive(f"record_github_rate_limited {err}")
        self.assertEqual(self.rate_state()["backoff_secs"], "600")

    def test_successful_access_keeps_mint_backoff_until_mint_succeeds(self) -> None:
        """Run the provider's mint block through repeated access/mint attempts.

        Access proofs succeed, the first three JIT mints hit a rate limit, and
        the fourth mint succeeds.  The production block must leave the shared
        backoff untouched across those successful access proofs, then clear it
        only after the successful mint.
        """
        fake = self.tmp / "fake-gh"
        count = self.tmp / "mint-count"
        fake.write_text(
            "#!/bin/sh\n"
            f"n=$(cat {count} 2>/dev/null || echo 0); n=$((n + 1)); echo $n >{count}\n"
            "case $n in 1|2|3) echo 'API rate limit exceeded (HTTP 403)' >&2; exit 1;; esac\n"
            "echo encoded-jit\n"
        )
        fake.chmod(0o755)
        src = RUNNER.read_text()
        start = src.index("  event mint_jit")
        end = src.index('  note "[$i] vm', start)
        mint_block = src[start:end]
        script = f'''\
mint_once() {{
{mint_block}
}}
access_success() {{ ! tartci_repository_access_denied 0 /dev/null; }}
i=1; vm=vm-1; ip=127.0.0.1; selected_labels=macOS; selected_tier=1
selected_group_id=8; selected_runner_api_root=https://example.test
label_args=(); ASSIGNMENT_MODE=legacy; CURRENT_REGISTERED_RUNNER=""
ASSIGNMENT_V2_IDLE_RETARGET_SECS=0; IDLE_TIMEOUT=900; JOB_TIMEOUT=1
tartci_lifecycle_mark() {{ :; }}
tartci_pool_lock_release() {{ :; }}
clear_jit_admission_denied() {{ :; }}
cleanup() {{ :; }}
for expected in 120 240 480; do
  access_success || exit 70
  mint_once || :
  actual="$(sed -n 's/^backoff_secs=//p' "$GITHUB_RATE_LIMIT_FILE")"
  [ "$actual" = "$expected" ] || exit 71
  echo "BACKOFF=$actual"
done
access_success || exit 70
mint_once
if [ -e "$GITHUB_RATE_LIMIT_FILE" ]; then echo STALE; else echo RESET; fi
'''
        r = self.drive(script, env=f"JIT_GH_CLI={fake}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["BACKOFF=120", "BACKOFF=240", "BACKOFF=480", "RESET"], r.stderr)

    def test_provider_never_latches_a_rate_limit(self) -> None:
        src = RUNNER.read_text()
        access = src[src.index('if tartci_repository_access_denied "$access_rc" "$access_error"; then'):]
        access = access[:access.index("discard_current_vm")]
        self.assertIn('elif tartci_github_rate_limited "$access_error"; then\n'
                      '      record_github_rate_limited', access)
        mint = src[src.index('"$JIT_GH_CLI" api -X POST "$selected_runner_api_root/generate-jitconfig"'):]
        mint = mint[:mint.index("JIT config mint failed")]
        self.assertLess(mint.index("tartci_github_rate_limited"),
                        mint.index("record_jit_admission_denied"))
        self.assertIn("elif tartci_github_auth_rejected", mint)
        self.assertNotIn("grep -Eq 'HTTP (401|403|404)", mint)
        self.assertIn("if github_rate_limit_backoff_active; then", src)
        access_success = src.index('  rm -f "$access_error"\n')
        mint_start = src.index('  local jit_error=', access_success)
        mint_success = src.index('  rm -f "$jit_error"\n  clear_github_rate_limited', mint_start)
        self.assertNotIn("clear_github_rate_limited", src[access_success:mint_success])

if __name__ == "__main__":
    unittest.main()
