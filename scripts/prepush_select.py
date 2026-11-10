#!/usr/bin/env python3
"""Pick the unit-test modules a change can affect, for scripts/prepush.sh.

Prints one module name per line (scripts/test_*.py, without the extension):

* a changed test module selects itself (this catches a stale assertion in the
  changed module's own tests);
* a changed scripts/<m>.py selects test_<m> when it exists, and every test
  module that imports <m>;
* any other changed file with a path (not a bare name such as `tartci`) selects
  the test modules that mention its repo-relative path.

Changed means: committed since the merge base with the base ref (a deleted file or
the old name of a rename counts), plus staged, unstaged and untracked files. Exits 2
when there is no merge base, rather than silently selecting only local edits. Runs under Python 3.9; no third-party imports.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SCRIPTS = "scripts"


def _git(*args):
    proc = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


class NoBase(Exception):
    """The base ref has no merge base with HEAD (not fetched?)."""


def changed_files(base):
    """Changed paths, including deleted ones and the old name of a rename."""
    merge_base = _git("merge-base", base, "HEAD").strip()
    if not merge_base:
        raise NoBase("no merge base with %s; run `git fetch origin` or set "
                     "TARTCI_PREPUSH_BASE" % base)
    names = set()
    names.update(_git("diff", "--name-only", "--no-renames", merge_base, "HEAD").splitlines())
    names.update(_git("diff", "--name-only", "--no-renames", "HEAD").splitlines())
    names.update(_git("ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(n for n in names if n)


def test_modules(root=ROOT):
    d = os.path.join(root, SCRIPTS)
    return sorted(f[:-3] for f in os.listdir(d) if f.startswith("test_") and f.endswith(".py"))


def _read(root, module):
    with open(os.path.join(root, SCRIPTS, module + ".py"), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def imports(text, name):
    return re.search(r"^\s*(?:import|from)\s+%s\b" % re.escape(name), text, re.M) is not None


def select(changed, root=ROOT):
    tests = test_modules(root)
    texts = {}

    def text(m):
        if m not in texts:
            texts[m] = _read(root, m)
        return texts[m]

    chosen = set()
    for path in changed:
        d, base = os.path.split(path)
        if d == SCRIPTS and base.endswith(".py"):
            name = base[:-3]
            if name.startswith("test_"):
                if name in tests:
                    chosen.add(name)
                continue
            if "test_" + name in tests:
                chosen.add("test_" + name)
            chosen.update(t for t in tests if imports(text(t), name))
        elif "/" in path or "." in base:
            chosen.update(t for t in tests if path in text(t))
    return sorted(chosen)


def main(argv):
    base = argv[1] if len(argv) > 1 else "origin/main"
    try:
        changed = changed_files(base)
    except NoBase as exc:
        print("prepush_select: %s" % exc, file=sys.stderr)
        return 2
    for m in select(changed):
        print(m)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
