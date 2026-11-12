"""Leak checks must not be satisfiable by chance.

A test that proves a secret never reaches a log or state file does it with
`assertNotIn(needle, text)`. The text usually holds random temp paths, so a
weak needle turns the check into a coin toss: a two-letter fragment such as
"pa" failed whenever mkdtemp produced a name like /tmp/tmpa1ibggvt. These
checks run over every test module so that shape cannot come back:

* a negative check never searches free text (file contents, process output,
  rendered data) for a string literal shorter than four characters;
* it never searches for a fragment of a secret (an index, slice or split of
  a secret-named value); search for the whole secret instead;
* every whitespace-separated word of a secret-named string constant is at
  least five characters and contains something a temp path or hex digest
  cannot (an uppercase letter or punctuation other than `_.-/`).

A deliberate exception carries `# needle-ok: <reason>` on the same line.
"""
from __future__ import annotations

import ast
import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SECRET_NAME = re.compile(r"(secret|password|passphrase|token|(^|_)pw$)", re.IGNORECASE)
PATH_LIKE_WORD = re.compile(r"^[a-z0-9_./-]*$")
MIN_LITERAL = 4
MIN_SECRET_WORD = 5
WAIVER = "needle-ok:"


def _names(node: ast.AST) -> list[str]:
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)] + [
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)]


def _is_secret_fragment(node: ast.AST) -> bool:
    """An index, slice or method call (e.g. .split()[0]) applied to a
    secret-named value."""
    if isinstance(node, (ast.Subscript, ast.Call)):
        return any(SECRET_NAME.search(name) for name in _names(node))
    return False


# Haystacks that are free text: file contents, process output, rendered or
# decoded data. A short needle against a dict or set of ids is an exact
# membership test and is fine.
TEXT_SOURCES = {"read_text", "read", "stdout", "stderr", "output", "decode",
                "dumps", "getvalue", "text"}


def _is_text(node: ast.AST) -> bool:
    if isinstance(node, (ast.JoinedStr, ast.BinOp)):
        return True
    return any(name in TEXT_SOURCES for name in _names(node))


def _negative_needles(tree: ast.AST):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "assertNotIn" and len(node.args) >= 2):
            yield node.args[0], node.args[1], node.lineno
        elif (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], ast.NotIn)):
            yield node.left, node.comparators[0], node.lineno


def _secret_constants(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper() \
                        and SECRET_NAME.search(target.id):
                    yield target.id, node.value.value, node.lineno


def violations(source: str, filename: str = "<test>") -> list[str]:
    lines = source.splitlines()
    waived = lambda lineno: WAIVER in lines[lineno - 1]  # noqa: E731
    tree = ast.parse(source, filename)
    found = []
    for needle, haystack, lineno in _negative_needles(tree):
        if waived(lineno):
            continue
        if isinstance(needle, ast.Constant) and isinstance(needle.value, str) \
                and len(needle.value) < MIN_LITERAL and _is_text(haystack):
            found.append(f"{filename}:{lineno}: negative check searches for "
                         f"{needle.value!r}, too short to be absent by design")
        elif _is_secret_fragment(needle):
            found.append(f"{filename}:{lineno}: negative check searches for a "
                         "fragment of a secret; search for the whole secret")
    for name, value, lineno in _secret_constants(tree):
        if waived(lineno):
            continue
        for word in value.split():
            if len(word) < MIN_SECRET_WORD or PATH_LIKE_WORD.match(word):
                found.append(f"{filename}:{lineno}: {name} word {word!r} could "
                             "appear in a temp path or digest by chance")
    return found


class CheckerCatchesWeakNeedles(unittest.TestCase):
    def test_short_literal_in_text_is_rejected(self) -> None:
        self.assertTrue(violations('self.assertNotIn("pa", path.read_text())\n'))
        self.assertTrue(violations('self.assertNotIn("0", proc.stdout)\n'))

    def test_short_key_in_a_mapping_is_allowed(self) -> None:
        self.assertEqual(violations('self.assertNotIn("103", remembered)\n'), [])

    def test_secret_fragment_is_rejected(self) -> None:
        self.assertTrue(violations("self.assertNotIn(SECRET.split()[0], text)\n"))
        self.assertTrue(violations("assert PASSWORD[:3] not in text\n"))

    def test_weak_secret_constant_is_rejected(self) -> None:
        self.assertTrue(violations("SECRET = 'pa ss\"w0rd'\n"))
        self.assertTrue(violations("TOKEN = 'abcdef123456'\n"))

    def test_strong_needles_pass(self) -> None:
        self.assertEqual(violations(
            "SECRET = 'Pw-Q7 ss\"w\\\\rd'\n"
            "self.assertNotIn(SECRET, text)\n"
            'self.assertNotIn("top-secret-jit", out)\n'
            'self.assertNotIn("<key>TARTCI_X</key>", body)\n'
            "self.assertNotIn(key, env)\n"), [])

    def test_waiver_is_honoured(self) -> None:
        self.assertEqual(violations(
            'self.assertNotIn("x", p.read_text())  # needle-ok: marker\n'), [])


class EveryTestModuleIsClean(unittest.TestCase):
    def test_no_leak_check_can_pass_or_fail_by_chance(self) -> None:
        files = sorted(HERE.glob("test_*.py"))
        self.assertGreater(len(files), 20)  # the scan really saw the suite
        found = [v for path in files
                 for v in violations(path.read_text(encoding="utf-8"), path.name)]
        self.assertEqual(found, [], "\n".join(found))


if __name__ == "__main__":
    unittest.main()
