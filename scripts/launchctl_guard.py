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
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
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
WHOLE_USER_SELECTOR_OPTS = frozenset({"-u", "--user", "-U", "-G", "-g", "-t"})
SHELL_SNAPSHOT = re.compile(
    r"(?:snapshot[-_.]?(?:bash|sh|zsh)|(?:bash|sh|zsh)[-_.]?snapshot)", re.I
)
# pkill -f matches the complete command line.  Keep representative lines
# for the process families that the guard must protect; matching only the
# executable basename lets path and snapshot patterns evade the guard.
BROAD_KILL_COMMAND_LINES = (
    # Shapes captured from ps -axo command on the fleet hosts.
    "/Users/danielraffel/.local/share/tartci-generations/aad356ca6bef941b2b675d695a0231acf89e4833-dbf3e8dbd7183aef/providers/tart-macos/runner.sh --loop",
    "/Users/danielraffel/.local/libexec/TartCILauncher.app/Contents/MacOS/TartCILauncher --lane pulp-gate",
    "/bin/zsh -c source /Users/danielraffel/.claude/shell-snapshots/snapshot-zsh-20261011.sh",
    "/Users/danielraffel/.claude/shell-snapshots/snapshot-zsh-20261011.sh",
    "/bin/zsh -lc tartci pool status",
    "/bin/bash -lc runner.sh --loop",
    "/bin/sh -c source /Users/danielraffel/.subrouter/codex/claude-proxy/25bf8e1a2393f1108d37029b/shell-snapshots/snapshot-zsh-20261011.sh",
    "/Users/danielraffel/.subrouter/codex/claude-proxy/25bf8e1a2393f1108d37029b/shell-snapshots/snapshot-zsh-20261011.sh",
    "/usr/bin/python3 -m tartci",
    "/usr/local/bin/python3.12 -m agent_cli",
    "/opt/homebrew/bin/node /Users/danielraffel/.local/bin/claude",
    "/Users/danielraffel/.local/bin/claude --dangerously-skip-permissions",
    "/Users/danielraffel/.local/bin/codex --full-auto",
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
                    depth: int) -> list[Hit]:
    words = [_expand(word, variables) for word in args]
    if not words:
        return []
    verb = words[0]
    if verb == "asuser" and len(words) > 2:
        return _classify_argv(words[2:], variables, depth + 1)
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


def _classify_argv(argv: list[str], variables: dict[str, str], depth: int) -> list[Hit]:
    argv, from_stdin = _strip_wrappers(list(argv), variables)
    if not argv:
        return []
    head = os.path.basename(_expand(argv[0], variables))
    if head == "launchctl":
        return _launchctl_hits(argv[1:], from_stdin, variables, depth)
    if head in ("pkill", "killall"):
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
        hits: list[Hit] = [
            Hit(head, target,
                "bare-name kill would terminate every matching agent shell/process")
            for target in operands
            if any(_matches_broad_name(target, name) for name in BROAD_KILL_NAMES)
        ]
        hits.extend(
            Hit(head, target,
                "-f matches a shell snapshot and can kill every agent session")
            for target in patterns
            if SHELL_SNAPSHOT.search(target) or any(
                _matches(target, command_line)
                for command_line in BROAD_KILL_COMMAND_LINES
            )
        )
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
                return classify(argv[index + 1], depth + 1, dict(variables))
        return []
    if head == "eval":
        return classify(" ".join(argv[1:]), depth + 1, variables)
    if head in ("ssh", "autossh"):
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if argv[i] in SSH_ARG_OPTS else 1
        remote = argv[i + 1:]
        return classify(" ".join(remote), depth + 1, {}) if remote else []
    return []


def classify(command: str, depth: int = 0,
             variables: dict[str, str] | None = None) -> list[Hit]:
    """Every dangerous launchctl invocation COMMAND would run."""
    if depth > MAX_DEPTH:
        return [Hit("?", command[:80], "nesting too deep to classify")]
    variables = {} if variables is None else variables
    hits: list[Hit] = []
    command, shell_bodies = _split_heredocs(command)
    for body in shell_bodies:
        hits.extend(classify(body, depth + 1, dict(variables)))
    flattened, bodies = _extract_substitutions(command)
    for body in bodies:
        hits.extend(classify(body, depth + 1, dict(variables)))
    try:
        tokens = _tokens(flattened)
    except ValueError:
        # Unbalanced quotes: fall back to whitespace words so a launchctl
        # mutation is still seen.
        tokens = flattened.split()
    for argv in _simple_commands(tokens):
        hits.extend(_classify_argv(argv, variables, depth))
    return hits


def block_message(command: str, hits: list[Hit]) -> str:
    broad = any(hit.verb in {"pkill", "killall", "kill", "agent-hygiene"} for hit in hits)
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
    broad = any(hit.verb in {"pkill", "killall", "kill", "agent-hygiene"} for hit in hits)
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
