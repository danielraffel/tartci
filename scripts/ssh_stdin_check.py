#!/usr/bin/env python3
"""Every ssh client invocation must say what its stdin is.

An ssh client forwards its stdin to the remote command. Called from inside a
`while read ... done <<< "$list"` loop, it drains the rest of the list, and the
loop ends early with no error. That is #371: `gate_supply.py decide` read
peers over ssh from inside the supervisor's class loop, and every class after
the first with young demand went unobserved.

shellcheck catches the direct form (SC2095, warning severity, run by
`scripts/lint.sh`), but not this one: the ssh was inside a Python helper the
loop called, and shellcheck cannot see through a process or a function
boundary. So this check does not ask whether a call sits in a loop. It asks
every ssh invocation to state its stdin, wherever it is:

  shell   `ssh -n`, an input redirect on the same command (`</dev/null`,
          `<<EOF`, `<<<`, `< file`), or ssh as the right side of a pipe.
          `ssh -G` (print config, no connection) is exempt.
  Python  a list or tuple whose first element is the ssh client must contain
          "-n". A literal is checked rather than the subprocess call, because
          the argv is often built far from the call. The first element is the
          ssh client when it is the string "ssh" or a path ending in "/ssh";
          a name or attribute called like one (`ssh`, `args.ssh`,
          `self.ssh_bin`, `ssh_path`, `remote_ssh`); or a name, parameter or
          argparse option given such a string as its value or default.

A command that genuinely forwards its caller's stdin (a wrapper whose callers
pipe a script into it) carries the comment `ssh-stdin: <reason>`, on its line
or the line above, and is skipped.

Usage: ssh_stdin_check.py [ROOT]   prints one line per finding; exit 1 if any.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterator, List, Tuple

EXEMPT = "ssh-stdin:"
SHELL_START = re.compile(r"^#!.*\b(bash|sh|zsh)\b")
# Characters after which a word is in command position.
COMMAND_PREFIX = set(" \t;&|(!{`")


def repo_files(root: Path) -> List[Path]:
    out = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True,
                         text=True, check=True).stdout.split()
    return [root / name for name in out]


def is_test(path: Path) -> bool:
    return path.name.startswith("test_") or "tests" in path.parts


def is_shell(path: Path) -> bool:
    if path.suffix == ".sh":
        return True
    if path.suffix:
        return False
    try:
        with path.open("r", errors="replace") as handle:
            return bool(SHELL_START.match(handle.readline()))
    except OSError:
        return False


def logical_lines(text: str) -> Iterator[Tuple[int, str]]:
    """(first line number, text) with backslash continuations joined."""
    start, parts = None, []
    for number, line in enumerate(text.splitlines(), 1):
        if start is None:
            start = number
        if line.rstrip().endswith("\\"):
            parts.append(line.rstrip()[:-1])
            continue
        parts.append(line)
        yield start, " ".join(parts)
        start, parts = None, []
    if parts:
        yield start or 1, " ".join(parts)


def command_positions(line: str) -> Iterator[int]:
    """Offsets of `ssh` used as a command word, outside quoted literal text.

    Tracks single and double quotes, and treats `$(`...`)` inside double
    quotes as code again, so `"$(ssh host cmd)"` counts and `"... ssh ..."`
    in a message does not. A `#` in command context starts a comment.
    """
    quote, depth, i = "", [], 0
    while i < len(line):
        ch = line[i]
        if quote == "'":
            if ch == "'":
                quote = ""
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = ""
            elif line.startswith("$(", i):
                depth.append('"')
                quote = ""
                i += 1
        else:
            if ch == "#" and (i == 0 or line[i - 1] in " \t;"):
                return
            if ch == "\\":
                i += 1
            elif ch in "'\"":
                quote = ch
            elif line.startswith("$(", i):
                depth.append("")
                i += 1
            elif ch == ")" and depth:
                quote = depth.pop()
            elif line.startswith("ssh", i) and (i == 0 or line[i - 1] in COMMAND_PREFIX) \
                    and i + 3 < len(line) and line[i + 3] in " \t":
                yield i
        i += 1


def command_text(line: str, start: int) -> str:
    """The command that starts at `start`, up to its first unquoted separator.

    Quote-aware, so a `&&` or `|` inside the remote command string does not
    end it, and a redirect after that string still belongs to it.
    """
    quote, depth, i = "", 0, start
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = ""
        elif ch == "\\":
            i += 1
        elif ch in "'\"":
            quote = ch
        elif line.startswith("$(", i):
            depth += 1
            i += 1
        elif ch == ")":
            if depth == 0:
                return line[start:i]
            depth -= 1
        elif depth == 0 and (ch in ";&|`" or line.startswith("&&", i)):
            return line[start:i]
        i += 1
    return line[start:]


def shell_findings(path: Path, text: str) -> Iterator[str]:
    physical = text.splitlines()
    for number, line in logical_lines(text):
        if EXEMPT in line or line.lstrip().startswith("#"):
            continue
        if number >= 2 and EXEMPT in physical[number - 2]:
            continue
        for at in command_positions(line):
            before, after = line[:at].rstrip(), line[at + 3:]
            command = command_text(line, at)[3:]
            tokens = command.split()
            if before.endswith("|") and not before.endswith("||"):
                continue  # stdin is the pipe
            if "-n" in tokens or "-G" in tokens or re.search(r"(^|\s)\d?<", command):
                continue
            if re.match(r"\s*(>|>>|2>|&>|$)", after) or after.lstrip().startswith(")"):
                continue  # `command -v ssh` and similar: not an invocation
            yield (f"{path}:{number}: ssh without -n or a stdin redirect; it will drain the "
                   f"caller's stdin (a `while read` loop ends early)")


# A name or attribute that by its name holds the ssh client.
SSH_NAME = re.compile(r"(?i)^(ssh|ssh_?(bin|binary|path|cmd|command|exe|executable|program|"
                      r"client)|\w+_ssh)$")


def _is_ssh_value(node: ast.AST | None) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and (node.value == "ssh" or node.value.endswith("/ssh")))


def _ssh_bound_names(tree: ast.AST) -> set:
    """Names, parameters and argparse options whose value or default is the ssh client."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _is_ssh_value(node.value):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(target.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            positional = args.posonlyargs + args.args
            for arg, default in zip(positional[len(positional) - len(args.defaults):],
                                    args.defaults):
                if _is_ssh_value(default):
                    names.add(arg.arg)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                if _is_ssh_value(default):
                    names.add(arg.arg)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "add_argument"
              and any(k.arg == "default" and _is_ssh_value(k.value) for k in node.keywords)):
            dest = next((k.value.value for k in node.keywords if k.arg == "dest"
                         and isinstance(k.value, ast.Constant)), None)
            flags = [a.value for a in node.args
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if dest is None and flags:
                longs = [f for f in flags if f.startswith("--")] or flags
                dest = longs[0].lstrip("-").replace("-", "_")
            if dest:
                names.add(dest)
    return names


def _first_is_ssh(node: ast.AST, bound: set = frozenset()) -> bool:
    if _is_ssh_value(node):
        return True
    if isinstance(node, ast.Name):
        return bool(SSH_NAME.match(node.id)) or node.id in bound
    if isinstance(node, ast.Attribute):
        return bool(SSH_NAME.match(node.attr)) or node.attr in bound
    return False


def python_findings(path: Path, text: str) -> Iterator[str]:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return
    lines = text.splitlines()
    bound = _ssh_bound_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
            continue
        if not _first_is_ssh(node.elts[0], bound):
            continue
        # An argv carries options; a bare tuple of program names does not.
        if not any(isinstance(e, ast.Constant) and isinstance(e.value, str)
                   and e.value.startswith("-") for e in node.elts):
            continue
        if any(isinstance(e, ast.Constant) and e.value in ("-n", "-G") for e in node.elts):
            continue
        here = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        above = lines[node.lineno - 2] if node.lineno >= 2 else ""
        if EXEMPT in here or (EXEMPT in above and above.lstrip().startswith("#")):
            continue
        yield (f"{path}:{node.lineno}: ssh argv without \"-n\"; a subprocess inherits the "
               f"caller's stdin and ssh forwards it (a `while read` loop ends early)")


def findings(root: Path) -> List[str]:
    found: List[str] = []
    for path in repo_files(root):
        if is_test(path) or not path.is_file():
            continue
        rel = path.relative_to(root)
        if path.suffix == ".py":
            found += python_findings(rel, path.read_text(errors="replace"))
        elif is_shell(path):
            found += shell_findings(rel, path.read_text(errors="replace"))
    return found


def main(argv: List[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1]
    found = findings(root)
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
