#!/usr/bin/env python3
"""No value from pulp's secrets files reaches any output, log, state or event.

On 2026-10-04 m5studio's keychain.env, written without line breaks during a
keychain rotation, parsed as one PULP_SIGN_KEYCHAIN value that contained the
keychain password, and keychain-unlock printed it into its log as the
"keychain path". These tests feed malformed and well-formed files holding a
recognisable fake secret through every secrets-handling entry point and
assert no part of the secret appears anywhere they write.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fleet_doctor as fd  # noqa: E402
import fleet_self_update as su  # noqa: E402
import keychain_unlock  # noqa: E402
import secret_files  # noqa: E402
import signing_prompt_guard as guard  # noqa: E402

SECRET = "Zq9X!7wV%kP4mR2t"
NOTARY_SECRET = "Nt7!Qp2W%xK9vB4s"
# Every window of this many characters of either secret has an uppercase
# letter and a digit or punctuation, so no temp path or digest can hold one.
FRAGMENT = 6


def keychain_variants(kc: Path, p12: Path) -> dict[str, str]:
    return {
        # The 2026-10-04 shape: one line, inner quotes escaped.
        "incident": (f'PULP_SIGN_KEYCHAIN="\\"{kc}\\"PULP_SIGN_KEYCHAIN_PW=\\"{SECRET}\\"'
                     f'PULP_SIGN_P12="{p12}"\n'),
        "incident-no-close": (f'PULP_SIGN_KEYCHAIN="\\"{kc}\\"PULP_SIGN_KEYCHAIN_PW=\\"{SECRET}\\"'
                              f'PULP_SIGN_P12="{p12}\n'),
        "literal-newline-escapes": (f"PULP_SIGN_KEYCHAIN={kc}\\nPULP_SIGN_KEYCHAIN_PW={SECRET}"
                                    f"\\nPULP_SIGN_P12={p12}\n"),
        "unbalanced-quote": f'PULP_SIGN_KEYCHAIN="{kc}"\nPULP_SIGN_KEYCHAIN_PW="{SECRET}\n',
        "bare-secret-line": f'PULP_SIGN_KEYCHAIN="{kc}"\n{SECRET}\n',
        "two-words": f"PULP_SIGN_KEYCHAIN={kc} {SECRET}\nPULP_SIGN_KEYCHAIN_PW=x{SECRET}\n",
        "well-formed": f'PULP_SIGN_KEYCHAIN="{kc}"\nPULP_SIGN_KEYCHAIN_PW="{SECRET}"\n',
    }


class FakeSystem:
    """Every command fails and echoes both secrets, as a hostile tool would."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], **_: object) -> su.Result:
        self.calls.append(argv)
        return su.Result(1, "", f"error: {' '.join(argv)} {SECRET} {NOTARY_SECRET}")


class NoSecretAnywhere(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.home / ".tartci"),
                                           "HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for key in ("PULP_SECRETS_DIR", "PULP_SIGN_KEYCHAIN", "PULP_SIGN_KEYCHAIN_PW"):
            os.environ.pop(key, None)
        kcdir = self.home / "Library" / "Keychains"
        kcdir.mkdir(parents=True)
        self.kc = kcdir / "pulp-signing-rotated-20261004.keychain-db"
        self.kc.write_text("")
        self.secrets = self.home / ".config" / "pulp" / "secrets"
        self.secrets.mkdir(parents=True)
        (self.secrets / "notary.env").write_text(
            f"PULP_NOTARY_KEY_ID=ABC123\nPULP_NOTARY_PASSWORD={NOTARY_SECRET}\n")
        plist = self.home / "Library" / "LaunchAgents" / f"{keychain_unlock.LABEL}.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("<plist/>")

    def assertClean(self, label: str, text: object) -> None:
        text = text if isinstance(text, str) else json.dumps(text, default=str)
        for secret in (SECRET, NOTARY_SECRET):
            for i in range(len(secret) - FRAGMENT + 1):
                window = secret[i:i + FRAGMENT]
                self.assertNotIn(window, text, f"{label}: a fragment of a secret leaked")  # needle-ok: windows of a mixed-case punctuated secret

    def echo(self, script_or_argv: object) -> tuple[int, str]:
        return 1, f"security: failed {script_or_argv} {SECRET} {NOTARY_SECRET}"

    def outputs(self) -> list[tuple[str, object]]:
        """Drive every entry point once; return everything each one wrote."""
        seen: list[tuple[str, object]] = []
        value = keychain_unlock.run(self.home, self.echo)
        seen.append(("keychain_unlock.run", value))
        state = keychain_unlock.state_path(self.home)
        seen.append(("keychain-unlock state file", state.read_text()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(keychain_unlock, "_security_interactive", self.echo):
            keychain_unlock.main([])
        seen.append(("keychain-unlock log line", out.getvalue()))
        seen.append(("keychain.unlock last()", keychain_unlock.last(self.home)))

        status = guard.status(self.home, self.echo)
        seen.append(("signing_prompt_guard.status", status))
        seen.append(("signing_prompt_guard.describe", guard.describe(status)))
        for args in ([], ["--json"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(guard, "_run", self.echo):
                guard.main(args)
            seen.append((f"signing_prompt_guard.main {args}", out.getvalue()))
        with mock.patch.object(guard, "_run", self.echo):
            finding = fd.check_signing_prompts(None, self.home)
        seen.append(("fleet_doctor finding", [finding.detail, finding.facts]))
        with mock.patch.object(guard, "status",
                               side_effect=RuntimeError(f"boom {SECRET} {NOTARY_SECRET}")):
            finding = fd.check_signing_prompts(None, self.home)
        seen.append(("fleet_doctor exception finding", [finding.detail, finding.facts]))

        sys_ = FakeSystem()
        seen.append(("unlock_signing_keychain receipt", su.unlock_signing_keychain(sys_, self.home)))
        seen.append(("signing_secrets_problem", su.signing_secrets_problem(self.home)))
        seen.append(("signing_keychain", su.signing_keychain(self.home)))
        seen.append(("keychain_args", su.keychain_args(self.home)))
        with self.assertRaises(su.Refused) as refused:
            su.signing_probe(sys_, "Developer ID", self.home, su.keychain_args(self.home))
        seen.append(("signing_probe refusal", str(refused.exception)))
        return seen

    def test_every_malformed_and_well_formed_file_leaks_nothing(self) -> None:
        variants = keychain_variants(self.kc, self.secrets / "pulp-signing.p12")
        for name, text in variants.items():
            with self.subTest(variant=name):
                (self.secrets / "keychain.env").write_text(text)
                for label, output in self.outputs():
                    self.assertClean(f"{name}: {label}", output)

    def test_the_incident_file_is_reported_by_key_name(self) -> None:
        (self.secrets / "keychain.env").write_text(
            keychain_variants(self.kc, self.secrets / "p12")["incident-no-close"])
        self.assertIsNone(su.signing_keychain(self.home))
        problem = su.signing_secrets_problem(self.home)
        self.assertIn("PULP_SIGN_KEYCHAIN", problem)
        value = keychain_unlock.run(self.home, self.echo)
        self.assertEqual(value["state"], "failed")
        self.assertIn("keychain.env is malformed", value["detail"])
        self.assertEqual(guard.status(self.home, self.echo)["state"], "risk")

    def test_a_well_formed_file_still_unlocks(self) -> None:
        # Control: the secret IS read and used; it just never reaches output.
        (self.secrets / "keychain.env").write_text(
            keychain_variants(self.kc, self.secrets / "p12")["well-formed"])
        self.assertEqual(su.signing_keychain(self.home), str(self.kc))
        self.assertEqual(su.signing_secrets(self.home)["PULP_SIGN_KEYCHAIN_PW"], SECRET)
        scripts = []
        value = keychain_unlock.run(self.home, lambda s: (scripts.append(s), (0, "no-timeout"))[1])
        self.assertEqual(value["state"], "ok")
        self.assertIn(SECRET, scripts[0])
        self.assertIsNone(su.signing_secrets_problem(self.home))

    def test_the_assertion_sees_a_leak(self) -> None:
        # Control for assertClean: a fragment of the secret is caught.
        with self.assertRaises(AssertionError):
            self.assertClean("control", "tail ..." + SECRET[3:12])

    def test_redact_removes_values_and_shell_quoted_forms(self) -> None:
        (self.secrets / "keychain.env").write_text(f"PULP_SIGN_KEYCHAIN_PW='{SECRET}'\n")
        self.assertEqual(secret_files.redact(f"a {SECRET} b {NOTARY_SECRET}", self.home),
                         "a <redacted> b <redacted>")
        self.assertEqual(secret_files.redact("ABC123 stays", self.home), "ABC123 stays")


# Modules that read secrets files. Each must never format raw file content
# into a message, and every line it prints must go through redact().
GUARDED = ("secret_files.py", "keychain_unlock.py", "signing_prompt_guard.py")
GUARDED_FUNCTIONS = {"fleet_self_update.py": ("signing_secrets", "signing_secrets_problem",
                                              "signing_keychain", "unlock_signing_keychain",
                                              "signing_probe")}
# Names that hold raw secrets-file content or a secret value.
RAW_NAMES = {"text", "raw", "lines", "line", "stripped", "content", "password", "secret",
             "secrets", "values", "words", "_raw"}
RAW_CALLS = {"read_text", "read_bytes", "readlines", "readline", "signing_secrets", "load",
             "parse"}
# A security(1) command script is the one place a password is formatted, and
# it goes to standard input, never to output.
STDIN_SINKS = {"interactive", "_quote"}
# Whatever is formatted inside these calls is redacted before it goes anywhere.
REDACTORS = {"redact", "_scrub"}


def raw_content_in(node: ast.AST, exempt: set[int]) -> str | None:
    for sub in ast.walk(node):
        if id(sub) in exempt:
            continue
        if isinstance(sub, ast.Name) and sub.id in RAW_NAMES:
            return sub.id
        if isinstance(sub, ast.Attribute) and sub.attr in RAW_NAMES:
            return sub.attr
        if isinstance(sub, ast.Call):
            func = sub.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in RAW_CALLS:
                return f"{name}()"
    return None


def _name(func: ast.AST) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def formatting_violations(tree: ast.AST) -> list[str]:
    exempt = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and _name(node.func) in STDIN_SINKS | REDACTORS) or \
                (isinstance(node, ast.FunctionDef) and node.name in STDIN_SINKS):
            exempt.update(id(sub) for sub in ast.walk(node) if sub is not node)
    found = []
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        pieces: list[ast.AST] = []
        if isinstance(node, ast.JoinedStr):
            pieces = [v.value for v in node.values if isinstance(v, ast.FormattedValue)]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)) and \
                isinstance(node.left, (ast.Constant, ast.JoinedStr)):
            pieces = [node.right]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                node.func.attr in ("format", "join") and isinstance(node.func.value, ast.Constant):
            pieces = [*node.args, *[k.value for k in node.keywords]]
        for piece in pieces:
            culprit = raw_content_in(piece, exempt)
            if culprit:
                found.append(f"line {node.lineno}: formats {culprit}")
    return found


def unredacted_prints(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
            arg = node.args[0] if node.args else None
            safe = (isinstance(arg, ast.Call) and getattr(arg.func, "attr", "") == "redact") or \
                isinstance(arg, ast.Constant)
            if not safe:
                found.append(f"line {node.lineno}: print without secret_files.redact")
    return found


class GuardTests(unittest.TestCase):
    def trees(self) -> list[tuple[str, ast.AST]]:
        out = []
        for name in GUARDED:
            out.append((name, ast.parse((HERE / name).read_text())))
        for name, functions in GUARDED_FUNCTIONS.items():
            module = ast.parse((HERE / name).read_text())
            defs = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
            for function in functions:
                self.assertIn(function, defs, f"{name}: {function} moved; update the guard")
                out.append((f"{name}:{function}", defs[function]))
        return out

    def test_no_secrets_module_formats_raw_file_content(self) -> None:
        problems = [f"{name} {v}" for name, tree in self.trees()
                    for v in formatting_violations(tree)]
        self.assertEqual(problems, [])

    def test_every_print_in_keychain_unlock_is_redacted(self) -> None:
        tree = ast.parse((HERE / "keychain_unlock.py").read_text())
        self.assertEqual(unredacted_prints(tree), [])

    def test_the_guard_catches_the_incident_pattern(self) -> None:
        # Control: the exact shapes that leaked, or would have, are flagged.
        for source in ('print(f"{stamp}: {keychain}: {password}")',
                       'detail = f"bad line: {line}"',
                       'msg = "bad: " + text',
                       'msg = "bad: {}".format(values)',
                       'msg = f"{su.signing_secrets(home)}"',
                       'raise Refused(f"cannot parse {path.read_text()}")'):
            tree = ast.parse(source)
            self.assertTrue(formatting_violations(tree) or unredacted_prints(tree), source)
        self.assertEqual(formatting_violations(ast.parse('x = f"{secret_files.redact(text)}"')), [])


if __name__ == "__main__":
    unittest.main()
