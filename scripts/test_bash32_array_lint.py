#!/usr/bin/env python3
"""The bash 3.2 empty-array lint (scripts/bash32_array_lint.py)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bash32_array_lint as lint  # noqa: E402

# The line that stopped every lane on one host: `state_args` is empty unless a
# state path is set, and macOS /bin/bash 3.2 aborts on it under `set -u`.
UNGUARDED_629 = '    --path "$path" "${state_args[@]}" \\'
GUARDED_629 = '    --path "$path" ${state_args[@]+"${state_args[@]}"} \\'
# lifecycle.lib.sh's vm_lifecycle event before the guard: `pairs` is empty
# when every lifecycle span is, and the file is sourced into a `set -u` lane.
UNGUARDED_LIFECYCLE = ('  event vm_lifecycle "served=$served warm=$LC_WARM rc=$4 ${pairs[*]}" \\\n'
                       '    "served=$served" "warm=$LC_WARM" "rc=$4" "${pairs[@]}"')
LIFECYCLE = Path(__file__).resolve().parent.parent / "providers" / "tart-macos" / "lifecycle.lib.sh"


def names(text: str) -> list[str]:
    return [name for _, name in lint.findings_in(text)]


class Findings(unittest.TestCase):
    def test_the_storage_health_line_is_flagged(self) -> None:
        self.assertEqual(["state_args"], names(UNGUARDED_629))

    def test_the_guarded_form_passes(self) -> None:
        self.assertEqual([], names(GUARDED_629))
        self.assertEqual([], names('for x in ${xs+"${xs[@]}"}; do :; done'))

    def test_every_unsafe_form_is_flagged(self) -> None:
        for line in ('cmd "${args[@]}"', "cmd ${args[@]}", 'echo "${args[*]}"',
                     'echo "${args[@]:1}"', 'x=("${a[@]}" "${b[@]}")'):
            with self.subTest(line=line):
                self.assertTrue(names(line), line)
        self.assertEqual(["a", "b"], names('x=("${a[@]}" "${b[@]}")'))

    def test_a_guard_on_one_array_does_not_hide_another(self) -> None:
        self.assertEqual(["b"], names('cmd ${a[@]+"${a[@]}"} "${b[@]}"'))

    def test_safe_forms_pass(self) -> None:
        # Negative control: counts, defaults, positional parameters and
        # indexed elements are not empty-array expansions.
        for line in ('n=${#args[@]}', 'x="${args[@]:-}"', 'x="${args[@]-none}"',
                     'cmd "$@"', 'echo "${BASH_SOURCE[0]}"', 'echo "${args}"'):
            with self.subTest(line=line):
                self.assertEqual([], names(line), line)

    def test_comments_and_escapes(self) -> None:
        self.assertEqual([], names('# "${args[@]}" in prose'))
        self.assertEqual([], names('cmd "${args[@]}"  # bash32-lint: nonempty set two lines up'))
        # The escape needs a reason.
        self.assertEqual(["args"], names('cmd "${args[@]}"  # bash32-lint: nonempty'))

    def test_nounset_detection(self) -> None:
        for text in ("set -u\n", "set -euo pipefail\n", "  set -eu\n", "set -o nounset\n",
                     "set -e -u\n"):
            with self.subTest(text=text):
                self.assertTrue(lint.uses_nounset(text), text)
        for text in ("set -e\n", "set -eo pipefail\n", "# set -u\n", "echo set -u\n"):
            with self.subTest(text=text):
                self.assertFalse(lint.uses_nounset(text), text)


class Files(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_lint(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(Path(lint.__file__)), "--root", str(self.root)],
                              capture_output=True, text=True, check=False)

    def test_a_nounset_script_fails_and_names_the_line(self) -> None:
        self.write("providers/x/runner.sh", f"#!/usr/bin/env bash\nset -euo pipefail\n{UNGUARDED_629}\n")
        result = self.run_lint()
        self.assertEqual(1, result.returncode)
        self.assertIn("runner.sh:3:", result.stderr)

    def test_extensionless_bash_scripts_are_scanned(self) -> None:
        self.write("tartci", '#!/bin/bash\nset -u\ncmd "${a[@]}"\n')
        self.assertEqual(1, self.run_lint().returncode)

    def test_scripts_without_nounset_and_vendored_fixtures_pass(self) -> None:
        self.write("a.sh", '#!/bin/bash\nset -e\ncmd "${a[@]}"\n')
        self.write("tests/fixtures/other/b.sh", '#!/bin/bash\nset -u\ncmd "${a[@]}"\n')
        self.write("c.sh", f"#!/bin/bash\nset -u\n{GUARDED_629}\n")
        result = self.run_lint()
        self.assertEqual(0, result.returncode, result.stderr)

    def test_a_sourced_library_is_checked_without_its_own_nounset(self) -> None:
        self.write("providers/x/lifecycle.lib.sh",
                   f"#!/usr/bin/env bash\nemit(){{\n{UNGUARDED_LIFECYCLE}\n}}\n")
        result = self.run_lint()
        self.assertEqual(1, result.returncode)
        self.assertIn("lifecycle.lib.sh:3: ${pairs[@]}", result.stderr)
        self.assertIn("lifecycle.lib.sh:4: ${pairs[@]}", result.stderr)
        # The same text in a script that is not a library and sets no nounset passes.
        (self.root / "providers/x/lifecycle.lib.sh").rename(self.root / "providers/x/emit.sh")
        self.assertEqual(0, self.run_lint().returncode)

    def test_this_repository_is_clean(self) -> None:
        result = subprocess.run([sys.executable, str(Path(lint.__file__))],
                                capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)


@unittest.skipUnless(Path("/bin/bash").exists() and subprocess.run(
    ["/bin/bash", "-c", '[ "${BASH_VERSINFO[0]}" = 3 ]'], check=False).returncode == 0,
    "needs macOS /bin/bash 3.2")
class Bash32(unittest.TestCase):
    """What the lint stands for, on the interpreter the lanes run."""

    def run_bash(self, line: str) -> subprocess.CompletedProcess[str]:
        script = f'set -u\nstate_args=()\nprintf "[%s]" {line}\necho done\n'
        return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                              check=False)

    def emit_lifecycle(self, library: Path) -> subprocess.CompletedProcess[str]:
        """Source ``library`` into a `set -u` lane and emit a VM's lifecycle
        event when every span is empty."""
        script = (f'set -u\nsource "{library}"\ntartci_lifecycle_reset\n'
                  'tartci_lifecycle_pairs(){ :; }\n'
                  'event(){ printf "<%s>" "$@"; echo; }\n'
                  'tartci_lifecycle_emit 100.5 200.5 300.5 0\necho done\n')
        return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                              check=False)

    def test_a_lifecycle_event_with_no_spans_does_not_abort_the_lane(self) -> None:
        result = self.emit_lifecycle(LIFECYCLE)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("<vm_lifecycle><served=0 warm=0 rc=0 ><served=0><warm=0><rc=0>",
                      result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("done"))
        # Negative control: the line as it was aborts under bash 3.2.
        text = LIFECYCLE.read_text()
        guarded = text[text.index("  event vm_lifecycle"):].split("\n", 2)
        guarded = "\n".join(guarded[:2])
        self.assertEqual(1, text.count(guarded))
        with tempfile.TemporaryDirectory() as scratch:
            old = Path(scratch) / "lifecycle.lib.sh"
            old.write_text(text.replace(guarded, UNGUARDED_LIFECYCLE))
            aborted = self.emit_lifecycle(old)
        self.assertNotEqual(0, aborted.returncode)
        self.assertIn("pairs[*]: unbound variable", aborted.stderr)

    def test_the_unguarded_line_aborts_and_the_guarded_one_does_not(self) -> None:
        aborted = self.run_bash('"${state_args[@]}"')
        self.assertNotEqual(0, aborted.returncode)
        self.assertIn("unbound variable", aborted.stderr)
        self.assertEqual(0, self.run_bash('${state_args[@]+"${state_args[@]}"}').returncode)


if __name__ == "__main__":
    unittest.main()
