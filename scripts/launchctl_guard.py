#!/usr/bin/env python3
"""Block raw launchctl mutations of tartci lanes from an agent's shell.

Why this exists
---------------
On 2026-09-22 an agent ran a raw launchctl kickstart against a lane
supervisor and killed a healthy, just-recovered VM mid-job. No tartci code was
on that path, so no tartci guard could refuse it. The one choke point every
agent shell command passes through is the agent harness's PreToolUse hook;
this is the classifier that hook runs.

Raw launchctl is dangerous on a lane for two reasons:
  * kickstart (and KeepAlive) re-run launchd's CACHED job spec, never the
    plist on disk, so it cannot pick up a plist change; and kickstart -k,
    bootout, unload, remove, kill, stop and disable all kill the
    supervisor's process tree, including a VM that is running a job.
  * tartci launchd reload <label> does the full bootout+bootstrap that
    re-reads the plist AND refuses while that lane is mid-job; tartci pool
    drain lets running jobs finish. Those are the safe paths.

Contract (--hook): read a PreToolUse JSON object on stdin
({"tool_name": ..., "tool_input": {"command": ...}}). Exit 2 with the reason
on stderr when the command would run launchctl kickstart|bootout|unload|
remove|kill|disable|stop against a tartci runner/lane supervisor or an
actions.runner.* service, or against a target that cannot be resolved
statically (a variable, a glob, stdin via xargs). Here-document bodies are
data unless they feed a shell interpreter or ssh. Exit 0 otherwise, silently.
TARTCI_ALLOW_RAW_LAUNCHCTL=1 written in the command itself is the explicit,
auditable escape hatch (exit 0 with a warning). Malformed input exits 0 with
a note: a broken hook must not break the agent's shell.

The same hook refuses broad process kills: bare-name pkill/killall of an
agent shell or interpreter, shell-snapshot -f patterns, and kill -9 -1.
Use the tree's top PID or tartci launchd heal for targeted recovery.

This is a best-effort guard against agent mistakes, not a security boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

DANGEROUS_VERBS = frozenset({
    "kickstart", "bootout", "unload", "remove", "kill", "disable", "stop",
})
# Runner/lane supervisor families, the same set tartci_pool_runner_agents
# enumerates: fleet lanes (com.danielraffel.tartci.tart-runner-macos-fleet.
# <host>.<lane>[.slotN], rendered by lane_plist), legacy per-repo tart/qemu
# runners (com.danielraffel.pulp.tart-runner, ...-macos-gate-slot2,
# com.danielraffel.pulp.qemu-runner-windows, forge/vellum tart-runners), and
# persistent Actions services. tartci's non-runner agents (launchd-watchdog,
# reap, reclaim, the relay) are deliberately outside it: no job runs in them.
LANE_LABEL = re.compile(
    r"^(?:actions\.runner\.."
    r"|com\.danielraffel\.[a-z0-9-]+\.(?:tart|qemu)-runner(?:$|[-.]))"
)
ESCAPE = re.compile(r"(?:^|[\s;&|(])TARTCI_ALLOW_RAW_LAUNCHCTL=1(?=$|[\s;&|)])")
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
KEYWORDS = frozenset({"then", "do", "else", "elif", "if", "while", "until",
                      "!", "{", "}", "time"})
SEPARATOR_CHARS = set(";&|()<>\n")
DOMAIN_ONLY = re.compile(r"^(?:gui|user|login|system|pid|session)(?:/[^/]*)?/?$")
SERVICE_TARGET = re.compile(r"^(?:gui|user|login|system|pid|session)/[^/]+/(.+)$")
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
VAR_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
UNRESOLVED = "\x00unresolved\x00"
# Options that consume the following argument, per wrapper.
SUDO_ARG_OPTS = frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T"})
ENV_ARG_OPTS = frozenset({"-u", "-C", "-S", "-P"})
SSH_ARG_OPTS = frozenset({"-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i",
                          "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R",
                          "-S", "-W", "-w"})
XARGS_ARG_OPTS = frozenset({"-E", "-I", "-J", "-L", "-n", "-P", "-R", "-S", "-s", "-a", "-d"})
MAX_DEPTH = 8
BROAD_KILL_NAMES = frozenset({"bash", "sh", "zsh", "python", "python3", "node"})
WHOLE_USER_SELECTOR_OPTS = frozenset({"-u", "--user", "-U", "-G", "-g", "-t", "-P", "-s"})
SHELL_SNAPSHOT = re.compile(
    r"(?:snapshot[-_.]?(?:bash|sh|zsh)|(?:bash|sh|zsh)[-_.]?snapshot)", re.I
)
# pkill -f matches the complete command line.  Keep representative lines
# for the process families that the guard must protect; matching only the
# executable basename lets path and snapshot patterns evade the guard.
_HOME = str(Path.home())
try:
    _USER = pwd.getpwuid(os.getuid()).pw_name
except KeyError:
    _USER = os.environ.get("USER", "")
BROAD_KILL_COMMAND_LINES = tuple(line.format(home=_HOME, user=_USER) for line in (
    # Shapes captured from ps -axo command on the fleet hosts, with this
    # account's home and name filled in at import: these are runtime data,
    # so a home-anchored pkill -f pattern on <home> must match them here.
    "{home}/.local/share/tartci-generations/aad356ca6bef941b2b675d695a0231acf89e4833-dbf3e8dbd7183aef/providers/tart-macos/runner.sh --loop",
    "{home}/.local/libexec/TartCILauncher.app/Contents/MacOS/tartci-launcher --lane studio-pulp-gate",
    "/bin/zsh -c source {home}/.claude/shell-snapshots/snapshot-zsh-20261011.sh",
    "{home}/.claude/shell-snapshots/snapshot-zsh-20261011.sh",
    "/bin/zsh -lc tartci pool status",
    "/bin/bash -lc runner.sh --loop",
    "/bin/sh -c source {home}/.subrouter/codex/claude-proxy/25bf8e1a2393f1108d37029b/shell-snapshots/snapshot-zsh-20261011.sh",
    "{home}/.subrouter/codex/claude-proxy/25bf8e1a2393f1108d37029b/shell-snapshots/snapshot-zsh-20261011.sh",
    "/usr/bin/python3 -m tartci",
    "/usr/local/bin/python3.12 -m agent_cli",
    "/opt/homebrew/bin/node {home}/.local/bin/claude",
    "{home}/.local/bin/claude --dangerously-skip-permissions",
    "{home}/.local/bin/codex --full-auto",
    "/usr/local/bin/tartci-launcher --lane pulp-gate",
    "/usr/local/bin/shipyard-daemon-launcher --lane pulp-gate",
    "{home}/.local/bin/Runner.Listener --start",
    "/usr/bin/login -flp {user}",
    "sr codex resume 01a126fe-4702-7b62-9852-63e986e1379c",
    "sr claude proxy --resume 08fb81d8-38a3-4b20-8bcc-c34ba2a66ffb",
    "/usr/bin/login -flp {user} /bin/bash --noprofile --norc -c exec -l /bin/zsh",
    "{home}/.local/bin/studio-pulp-gate",
    "{home}/.local/bin/claude proxy --resume",
))
PROCESS_ROW = re.compile(r"^\s*(\d+)\s+(\d+)\s+(.*)$")
PROTECTED_PROCESS = re.compile(
    r"(?:tartci[-_]launcher|TartCILauncher|runner\.sh\s+--loop|"
    r"shipyard-daemon-launcher|"
    r"(?:^|\s)sr\s+(?:codex|claude)\b|(?:^|\s)(?:codex|claude)\b|"
    r"claude[- ]proxy|subrouter|shell-snapshots|(?:^|/)login\s+-flp\b|"
    r"(?:^|/)(?:bash|sh|zsh|dash|ksh)\b)",
    re.I,
)


def _matches(pattern: str, value: str, *, full: bool = False) -> bool:
    """Match a kill operand without letting malformed regex escape the hook."""
    try:
        return (re.fullmatch if full else re.search)(pattern, value, re.IGNORECASE) is not None
    except re.error:
        return True


def _matches_broad_name(pattern: str, name: str) -> bool:
    return _matches(pattern, name, full=True) or (
        name in {"python", "python3"}
        and _matches(r"python3(?:\.\d+)?", pattern, full=True)
    )


class Hit(NamedTuple):
    verb: str
    target: str
    why: str


class ProcessRow(NamedTuple):
    pid: int
    ppid: int
    command: str


def _process_table() -> list[ProcessRow] | None:
    """Read the host process table, or return None when it is unavailable."""
    snapshot = (os.environ.get("TARTCI_GUARD_PS_SNAPSHOT")
                if os.environ.get("TARTCI_GUARD_TESTING") == "1" else None)
    try:
        if snapshot:
            text = Path(snapshot).read_text(encoding="utf-8")
        else:
            text = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,command="],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    rows: list[ProcessRow] = []
    for line in text.splitlines():
        match = PROCESS_ROW.match(line)
        if match:
            rows.append(ProcessRow(int(match.group(1)), int(match.group(2)), match.group(3)))
    # A short receipt is indistinguishable from a truncated ps stream.  Fleet
    # hosts have substantially more than this; fail closed below the bound.
    return rows if len(rows) > 40 else None


def _protected_process_commands(rows: list[ProcessRow]) -> list[str]:
    by_pid = {row.pid: row for row in rows}
    protected = {row.pid for row in rows if PROTECTED_PROCESS.search(row.command)}
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row.pid not in protected and row.ppid in protected:
                protected.add(row.pid)
                changed = True
    return [by_pid[pid].command for pid in protected]


def _protected_process_names(rows: list[ProcessRow] | None) -> set[str] | None:
    if rows is None:
        return None
    names: set[str] = set()
    for command in _protected_process_commands(rows):
        words = command.split()
        if words:
            names.add(os.path.basename(words[0]))
        for word in words:
            base = os.path.basename(word)
            if re.search(r"(?:tartci[-_]launcher|runner|codex|claude|subrouter|"
                         r"login|bash|sh|zsh|shipyard-daemon|Runner\.Listener)",
                         base, re.I):
                names.add(base)
    return names


def _matches_live_protected(pattern: str, rows: list[ProcessRow] | None) -> bool | None:
    if rows is None:
        return None
    try:
        expression = re.compile(pattern, re.I)
    except re.error:
        return True
    return any(expression.search(command) for command in _protected_process_commands(rows))


def _matches_static_fallback(pattern: str) -> bool:
    return SHELL_SNAPSHOT.search(pattern) is not None or any(
        _matches(pattern, command_line) for command_line in BROAD_KILL_COMMAND_LINES
    )


RAW_KILL_VERB = re.compile(
    r"(?:^|[;&|]\s*|\b(?:do|then|else)\s+|[({]\s*)(?:"
    r"(?:sudo|env|nohup|timeout(?:\s+\S+)?|xargs)\s+)*(?:/[^\s/]+/)?"
    r"(?:kill|killall|pkill)\b|"
    r"\b(?:os\.kill|Process\.kill|signal\.SIG(?:KILL|TERM))\b|"
    r"\blaunchctl\s+(?:kill|bootout|remove)\b|"
    r"\bxargs\b[^;|\n]*\bkill\b|"
    r"\b(?:system|qx)\s*\([^)]*\bkill\b",
    re.I,
)
RAW_PID_SOURCE = re.compile(r"\b(?:pgrep|pidof|ps|lsof)\b|(?:^|\s)-[a-z]*[fx]\b")
STATIC_NAME_HINT = re.compile(r"(?:tartci|codex|claude|runner|listener|shell|zsh|bash|login|shipyard)", re.I)


def _has_raw_kill_verb(command: str) -> bool:
    # Non-kill commands may contain words such as kill as data.  Mask
    # only those command segments; a later segment can still contain a real
    # kill (for example: git status, then ssh ... kill ...).
    segments = re.split(r"([;&|\n]+)", command)
    for index in range(0, len(segments), 2):
        if _is_nonexecuting_data_segment(segments[index]):
            segments[index] = " "
    command = "".join(segments)
    # Remove grep-like command segments first: kill words there are data.
    parts = re.split(r"([;&|\n]+)", command)
    for index in range(0, len(parts), 2):
        search_exec = (re.match(r"^\s*(?:rg|ag)\b", parts[index], re.I)
                       and re.search(r"--(?:pre(?:-glob)?|pager)(?:=|\s)", parts[index], re.I))
        if (re.search(r"^\s*(?:grep|egrep|rg|ag|git\s+grep|echo)\b", parts[index], re.I)
                and not search_exec
                and not re.search(r"(?:^|\s)-O(?:\s|['\"]|$)|--open-files-in-pager(?:=|\s)",
                                  parts[index], re.I)):
            parts[index] = " "
    masked = "".join(parts)
    if RAW_KILL_VERB.search(masked) or re.search(r"\b(?:kill|pkill|killall)\b", masked, re.I):
        return True
    return False


def _is_nonexecuting_data_segment(segment: str) -> bool:
    """Return whether a command segment is on the read-only data allowlist."""
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        return False
    index = 0
    assignments: list[str] = []
    while index < len(words):
        word = words[index]
        if word in {"sudo", "nohup"}:
            index += 1
            continue
        if word == "env":
            index += 1
            while index < len(words) and "=" in words[index]:
                assignments.append(words[index])
                index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 1
            continue
        break
    if index >= len(words):
        return False
    command = os.path.basename(words[index]).lower()
    args = words[index + 1:]
    if command == "ls":
        return True
    if command == "git":
        lowered = [arg.lower() for arg in args]
        if any(name.split("=", 1)[0].upper().startswith("GIT_") for name in assignments):
            return False
        if "-c" in args or any(arg.lower().startswith("-c=") for arg in args):
            return False
        execution_options = ("--upload-pack", "--receive-pack", "-o",
                             "--open-files-in-pager", "--exec")
        if any(arg == option or arg.startswith(option + "=")
               or (option == "-o" and arg.startswith("-o") and arg != "-o")
               for arg in lowered for option in execution_options):
            return False
        subcommand_index = 0
        while subcommand_index < len(args):
            option = args[subcommand_index]
            option_lower = option.lower()
            if option == "-c" or option_lower.startswith("-c="):
                return False
            if option_lower in {"--git-dir", "--work-tree"}:
                subcommand_index += 2
                continue
            if option_lower.startswith("--git-dir=") or option_lower.startswith("--work-tree="):
                subcommand_index += 1
                continue
            if option == "-C":
                subcommand_index += 2
                continue
            break
        lowered = lowered[subcommand_index:]
        if lowered and lowered[0] in {"fetch", "ls-remote", "clone", "archive"} \
                and "-u" in lowered:
            return False
        readonly = {"status", "log", "show", "diff", "rev-parse", "ls-files",
                    "ls-remote", "fetch", "blame", "grep", "describe", "merge-base",
                    "cat-file"}
        if not lowered or lowered[0] not in readonly:
            if lowered[:2] != ["branch", "--list"] and lowered[:2] != ["remote", "-v"]:
                return False
        return True
    if command == "tar":
        lowered_args = [arg.lower() for arg in args]
        if any(arg in {"-i", "--to-command", "--use-compress-program", "--checkpoint-action",
                       "--info-script", "--new-volume-script", "--rsh-command", "--rmt-command"}
               or arg.startswith(("--to-command=", "--use-compress-program=", "--checkpoint-action=",
                                  "--info-script=", "--new-volume-script=", "--rsh-command=", "--rmt-command="))
               for arg in lowered_args):
            return False
        if "-F" in args:
            return False
        if any(arg.startswith("-") and not arg.startswith("--") and "i" in arg[1:]
               for arg in lowered_args):
            return False
        # Tar operands and filenames are data; only its command-executing
        # options keep the segment visible.  Listing forms are the explicit
        # read-only cases, while ordinary extraction remains safe when its
        # arguments merely contain the word kill.
        return True
    return False


def _raw_process_hits(command: str, rows: list[ProcessRow] | None) -> list[Hit]:
    """Apply the raw-text PID-source and kill co-occurrence rule."""
    if not _has_raw_kill_verb(command) or not RAW_PID_SOURCE.search(command):
        return []
    assignments = dict(re.findall(r"\b([A-Za-z_]\w*)=([^\s;&|]+)", command))
    quoted = re.findall(r"['\"]([^'\"]+)['\"]", command)
    flagged = re.findall(r"(?:^|\s)-[A-Za-z]*[fx]\s+([^\s;&|()]+)", command, re.I)
    flagged = [word.strip("(){}'\"") for word in flagged]
    operands: list[str] = []
    for match in re.finditer(r"\b(?:grep|egrep|pgrep|pkill|pidof)\b([^;|]*)", command, re.I):
        for word in re.findall(r"[^\s]+", match.group(1)):
            if not word.startswith("-"):
                operands.append(word.strip("(){}"))
    patterns = list(dict.fromkeys(quoted + flagged + operands))
    resolved: list[str] = []
    for pattern in patterns:
        variable = re.fullmatch(r"\$(?:\{)?([A-Za-z_]\w*)\}?", pattern)
        resolved.append(assignments.get(variable.group(1), "<unresolved>")
                        if variable else pattern)
    patterns = list(dict.fromkeys(resolved))
    if not patterns:
        return [Hit("process-source", "<unresolved>",
                    "a PID source and kill verb occur together")]
    hits: list[Hit] = []
    for pattern in patterns:
        if pattern == "<unresolved>":
            hits.append(Hit("process-source", pattern,
                            "the PID pattern is unresolved and cannot be shown safe"))
            continue
        live = _matches_live_protected(pattern, rows)
        if _matches_static_fallback(pattern) or live is True or live is None:
            hits.append(Hit("process-source", pattern,
                            "a PID source feeds a kill-capable command"))
    return hits


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    return list(lexer)


def _extract_substitutions(command: str) -> tuple[str, list[str]]:
    """Replace $(...) and ... with a placeholder, returning their bodies.

    Deliberately quote-blind: text that only looks like a substitution inside
    single quotes is classified too, which can only err toward blocking.
    """
    bodies: list[str] = []
    out: list[str] = []
    i = 0
    tick = chr(96)
    while i < len(command):
        if command.startswith("$(", i) and not command.startswith("$((", i):
            depth, j = 1, i + 2
            while j < len(command) and depth:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                j += 1
            bodies.append(command[i + 2:j - 1] if depth == 0 else command[i + 2:])
            out.append(UNRESOLVED)
            i = j
        elif command[i] == tick:
            j = command.find(tick, i + 1)
            end = len(command) if j < 0 else j
            bodies.append(command[i + 1:end])
            out.append(UNRESOLVED)
            i = end + 1
        else:
            out.append(command[i])
            i += 1
    return "".join(out), bodies


def _heredoc_feeds_shell(prefix: str) -> bool:
    """Whether the command owning a << reads its stdin as shell commands.

    PREFIX is the line text before the operator. The owner is the last simple
    command in it: bash, sh -s, sudo zsh, ssh host execute the body;
    cat, git commit -F -, tee treat it as data.
    """
    segment = re.split(r"\$\(|" + re.escape(chr(96)) + r"|[;&|()]", prefix)[-1]
    try:
        words = shlex.split(segment)
    except ValueError:
        words = segment.split()
    words = [word for word in words if not re.fullmatch(r"\d*[<>]&?\S*", word)]
    words, _ = _strip_wrappers(words, {})
    if not words:
        return False
    head = os.path.basename(words[0])
    if head in SHELLS:
        # With -c the script is the argument; the heredoc is only its stdin.
        return not any(re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", word) for word in words[1:])
    return head in ("ssh", "autossh")


HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(['\"]?)([A-Za-z0-9_.\-]+)\2")


def _split_heredocs(command: str) -> tuple[str, list[str]]:
    """Remove here-document bodies, returning (command, bodies run as shell).

    A heredoc body is data (a commit message, a file being written) unless the
    command it feeds is a shell interpreter or ssh, in which case it is
    returned for classification as commands.
    """
    lines = command.split("\n")
    kept: list[str] = []
    shell_bodies: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in HEREDOC.finditer(line):
            strip_tabs, word = match.group(1) == "-", match.group(3)
            body: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                index += 1
                if (candidate.lstrip("\t") if strip_tabs else candidate) == word:
                    break
                body.append(candidate)
            if _heredoc_feeds_shell(line[:match.start()]):
                shell_bodies.append("\n".join(body))
    return "\n".join(kept), shell_bodies


def _simple_commands(tokens: list[str]) -> list[list[str]]:
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= SEPARATOR_CHARS:
            commands.append([])
        else:
            commands[-1].append(token)
    return [command for command in commands if command]


def _expand(word: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        return variables.get(name, UNRESOLVED)
    return VAR_REF.sub(replace, word)


def _strip_wrappers(argv: list[str], variables: dict[str, str]) -> tuple[list[str], bool]:
    """Drop env/sudo/nohup/... prefixes. Returns (argv, stdin_supplies_args)."""
    from_stdin = False
    while argv:
        head = os.path.basename(argv[0])
        if ASSIGNMENT.match(argv[0]) or argv[0] in KEYWORDS:
            match = ASSIGNMENT.match(argv[0])
            if match:
                variables[match.group(1)] = _expand(match.group(2), variables)
            argv = argv[1:]
        elif head in ("env", "sudo", "xargs"):
            opts = {"env": ENV_ARG_OPTS, "sudo": SUDO_ARG_OPTS, "xargs": XARGS_ARG_OPTS}[head]
            from_stdin = from_stdin or head == "xargs"
            i = 1
            while i < len(argv):
                word = argv[i]
                if word == "--":
                    i += 1
                    break
                if word in opts:
                    i += 2
                elif word.startswith("-") or (head == "env" and ASSIGNMENT.match(word)):
                    i += 1
                else:
                    break
            argv = argv[i:]
        elif head in ("command", "exec", "nohup", "builtin", "caffeinate"):
            i = 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
            argv = argv[i:]
        elif head == "nice":
            i = 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 2 if argv[i] == "-n" else 1
            argv = argv[i:]
        elif head in ("timeout", "gtimeout"):
            i = 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 2 if argv[i] in ("-s", "-k", "--signal", "--kill-after") else 1
            argv = argv[i + 1:]  # the duration
        else:
            break
    return argv, from_stdin


def _label_of(target: str) -> str:
    if target.endswith(".plist"):
        return os.path.basename(target)[: -len(".plist")]
    match = SERVICE_TARGET.match(target)
    return match.group(1) if match else target


def _launchctl_hits(args: list[str], from_stdin: bool, variables: dict[str, str],
                    depth: int, process_rows: list[ProcessRow] | None) -> list[Hit]:
    words = [_expand(word, variables) for word in args]
    if not words:
        return []
    verb = words[0]
    if verb == "asuser" and len(words) > 2:
        return _classify_argv(words[2:], variables, depth + 1, process_rows)
    if verb not in DANGEROUS_VERBS:
        return []
    operands: list[str] = []
    skip = False
    for word in words[1:]:
        if skip:
            skip = False
            continue
        if verb == "unload" and word in ("-S", "-D"):
            skip = True
            continue
        if word.startswith("-") and not word.lstrip("-").isdigit():
            continue
        operands.append(word)
    if verb == "kill" and operands:
        operands = operands[1:]  # the signal
    hits: list[Hit] = []
    if verb == "bootout" and operands and DOMAIN_ONLY.match(operands[0]) \
            and UNRESOLVED not in operands[0]:
        if len(operands) == 1:
            return [Hit(verb, operands[0], "boots out every service in the domain, "
                        "including every lane")]
        operands = operands[1:]
    if not operands:
        why = ("its targets arrive on stdin (xargs), so they cannot be checked"
               if from_stdin else "no target could be read, so it cannot be checked")
        return [Hit(verb, "<stdin>" if from_stdin else "<none>", why)]
    for target in operands:
        label = _label_of(target)
        if UNRESOLVED in label or "$" in label or re.search(r"[*?\[]", label):
            hits.append(Hit(verb, target.replace(UNRESOLVED, "$(...)"),
                            "the target is computed at run time, so it cannot be "
                            "shown to be outside tartci's lanes"))
        elif LANE_LABEL.match(label):
            hits.append(Hit(verb, target, "it is a tartci-managed lane supervisor "
                            "or Actions runner service"))
    return hits


def _contains_kill_verb(command: str) -> bool:
    try:
        words = _tokens(command)
    except ValueError:
        words = command.split()
    for index, word in enumerate(words):
        base = os.path.basename(word)
        if base in {"kill", "killall", "pkill"}:
            return True
        if base == "launchctl" and index + 1 < len(words) and words[index + 1] in {
            "kill", "bootout", "stop", "remove", "disable"
        }:
            return True
    return False


def _classify_argv(argv: list[str], variables: dict[str, str], depth: int,
                   process_rows: list[ProcessRow] | None,
                   pid_source_kill: bool = False) -> list[Hit]:
    argv, from_stdin = _strip_wrappers(list(argv), variables)
    if not argv:
        return []
    head = os.path.basename(_expand(argv[0], variables))
    if head == "launchctl":
        return _launchctl_hits(argv[1:], from_stdin, variables, depth, process_rows)
    if head in ("pkill", "killall", "pgrep", "pidof"):
        words = [_expand(word, variables) for word in argv[1:]]
        operands: list[str] = []
        patterns: list[str] = []
        selector_arg = False
        has_user_selector = False
        skip = False
        for word in words:
            if skip:
                # -f consumes a pattern; selector options consume a value.
                if selector_arg:
                    selector_arg = False
                else:
                    patterns.append(word)
                skip = False
            elif word == "-f":
                skip = True
            elif word in WHOLE_USER_SELECTOR_OPTS:
                has_user_selector = True
                selector_arg = True
                skip = True
            elif not word.startswith("-"):
                operands.append(word)
        protected_names = _protected_process_names(process_rows)
        hits: list[Hit] = [
            Hit(head, target,
                "bare-name kill would terminate every matching agent shell/process")
            for target in operands
            if (head not in ("pgrep", "pidof") or pid_source_kill)
            and (any(_matches_broad_name(target, name) for name in BROAD_KILL_NAMES)
                 or (head in ("pkill", "killall") and
                     ((len(target) > 1 and _matches_static_fallback(target))
                      or protected_names is None
                      or any(_matches(target, name) for name in protected_names))))
        ]
        # A standalone pgrep is observational.  A surrounding xargs/command
        # substitution is classified separately by the kill command it feeds.
        source_patterns = patterns if head != "pidof" else operands
        for target in (() if head in ("pgrep", "pidof") and not pid_source_kill
                       else source_patterns):
            live_match = _matches_live_protected(target, process_rows)
            static_match = _matches_static_fallback(target)
            if live_match is None:
                matched = True
            else:
                matched = static_match or live_match
            if matched:
                hits.append(Hit(head, target,
                                "-f matches a protected process or agent session"))
        if has_user_selector and not operands:
            hits.append(Hit(head, "-u", "whole-user selector has no process operand"))
        return hits
    if head == "kill":
        words = [_expand(word, variables) for word in argv[1:]]
        signal_index = 0
        if words[:1] in (["-s"], ["--signal"]):
            signal_index = 2
        elif words and words[0] == "-1" and len(words) > 1:
            signal_index = 1
        elif words and words[0].startswith("-") and words[0] != "-1":
            signal_index = 1
        targets = words[signal_index:]
        broadcast = "-1" in targets or (words == ["-1"])
        if broadcast:
            signal = words[0] if words and words[0] != "-1" else "default"
            return [Hit("kill", f"{signal} -1",
                        "broadcast signal can terminate every agent and supervisor")]
        return []
    if head == "agent-hygiene":
        if "--reap" in argv[1:]:
            return [Hit(head, "--reap",
                        "age-based agent reaping is human-only and unsafe from an agent")]
        return []
    if head == "export":
        for word in argv[1:]:
            match = ASSIGNMENT.match(word)
            if match:
                variables[match.group(1)] = _expand(match.group(2), variables)
        return []
    if head in SHELLS:
        for index, word in enumerate(argv[1:], start=1):
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", word) and index + 1 < len(argv):
                return classify(argv[index + 1], depth + 1, dict(variables), process_rows)
        return []
    if head == "eval":
        return classify(" ".join(argv[1:]), depth + 1, variables, process_rows)
    if head in ("ssh", "autossh"):
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if argv[i] in SSH_ARG_OPTS else 1
        remote = argv[i + 1:]
        return classify(" ".join(remote), depth + 1, {}, process_rows) if remote else []
    return []


_PROCESS_ROWS_UNSET = object()


def classify(command: str, depth: int = 0,
             variables: dict[str, str] | None = None,
             process_rows: list[ProcessRow] | None | object = _PROCESS_ROWS_UNSET,
             pid_source_kill: bool = False) -> list[Hit]:
    """Every dangerous launchctl invocation COMMAND would run."""
    if depth > MAX_DEPTH:
        return [Hit("?", command[:80], "nesting too deep to classify")]
    variables = {} if variables is None else variables
    if process_rows is _PROCESS_ROWS_UNSET:
        process_rows = _process_table()
    hits: list[Hit] = []
    command, shell_bodies = _split_heredocs(command)
    hits.extend(_raw_process_hits(command, process_rows))
    pid_source_kill = pid_source_kill or _contains_kill_verb(command)
    for body in shell_bodies:
        hits.extend(classify(body, depth + 1, dict(variables), process_rows,
                             pid_source_kill=pid_source_kill))
    flattened, bodies = _extract_substitutions(command)
    for body in bodies:
        hits.extend(classify(body, depth + 1, dict(variables), process_rows,
                             pid_source_kill=pid_source_kill))
    try:
        tokens = _tokens(flattened)
    except ValueError:
        # Unbalanced quotes: fall back to whitespace words so a launchctl
        # mutation is still seen.
        tokens = flattened.split()
    for argv in _simple_commands(tokens):
        hits.extend(_classify_argv(argv, variables, depth, process_rows,
                                   pid_source_kill=pid_source_kill))
    return hits


def block_message(command: str, hits: list[Hit]) -> str:
    broad = any(hit.verb in {"pkill", "killall", "pgrep", "kill", "agent-hygiene", "process-source"} for hit in hits)
    heading = ("BLOCKED by tartci launchd guard: broad process kill."
               if broad else "BLOCKED by tartci launchd guard: raw launchctl against a CI lane.")
    lines = [heading, ""]
    for hit in hits:
        lines.append(f"  {hit.verb} {hit.target}: {hit.why}")
    lines += [
        "",
        "Why: kickstart re-runs launchd's CACHED job spec (never the plist on",
        "disk), and kickstart -k / bootout / unload / remove / kill / stop /",
        "disable kill the supervisor's process tree, including a VM that is",
        "running a job. On 2026-09-22 a raw kickstart of a lane supervisor",
        "killed a healthy, just-recovered VM mid-job.",
        "",
        "Safe paths:",
        "  tartci launchd reload <label>   full bootout+bootstrap that re-reads",
        "                                  the plist; refuses while the lane is",
        "                                  mid-job (--dry-run shows the plan)",
        "  tartci pool drain               stop taking work; running jobs finish",
    ]
    if broad:
        lines += [
            "  Targeted recovery: kill the tree top by its PID, or run",
            "  tartci launchd heal             bounded launchd recovery with a receipt",
        ]
    lines += [""]
    if broad:
        lines.append("Broad process kills have no escape hatch; target the tree by PID or run tartci launchd heal.")
    else:
        lines += [
            "If you really mean it, put TARTCI_ALLOW_RAW_LAUNCHCTL=1 in the command",
            "itself (e.g. TARTCI_ALLOW_RAW_LAUNCHCTL=1 launchctl ...).",
        ]
    return "\n".join(lines)


def decide(command: str) -> tuple[int, str]:
    hits = classify(command)
    if not hits:
        return 0, ""
    broad = any(hit.verb in {"pkill", "killall", "pgrep", "kill", "agent-hygiene", "process-source"} for hit in hits)
    if ESCAPE.search(command) and not broad:
        return 0, ("tartci launchd guard: TARTCI_ALLOW_RAW_LAUNCHCTL=1 given; allowing "
                   + "; ".join(f"launchctl {h.verb} {h.target}" for h in hits))
    return 2, block_message(command, hits)


def _command_from_hook(raw: str) -> str | None:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("hook payload is not a JSON object")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if isinstance(command, list) and all(isinstance(word, str) for word in command):
        return shlex.join(command)
    return command if isinstance(command, str) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tartci launchd guard",
        description="Classify a shell command for raw launchctl lane mutations.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--hook", action="store_true",
                        help="read a PreToolUse JSON payload on stdin")
    source.add_argument("--command", help="classify this command string")
    args = parser.parse_args(argv)
    if args.hook:
        try:
            command = _command_from_hook(sys.stdin.read())
        except (ValueError, UnicodeDecodeError) as exc:
            print(f"tartci launchd guard: ignoring malformed hook input ({exc})",
                  file=sys.stderr)
            return 0
        if command is None:
            return 0
    else:
        command = args.command
    code, message = decide(command)
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
