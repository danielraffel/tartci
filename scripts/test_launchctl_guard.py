#!/usr/bin/env python3
"""PreToolUse guard: raw launchctl mutations of tartci lanes are blocked."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launchctl_guard as guard

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "claude-pretooluse-launchctl.sh"
PS_FIXTURE = ROOT / "scripts" / "fixtures" / "launchctl_guard_ps_m3.txt"
LANE = "com.danielraffel.tartci.tart-runner-macos-fleet.studio.pulp-gate"
RUNNER = "actions.runner.Generous-Corp-pulp.pulp-preamble-m5"
LEGACY = "com.danielraffel.pulp.tart-runner-macos-release"

BLOCKED = [
    f"launchctl kickstart -k gui/501/{LANE}",
    f"launchctl kickstart gui/501/{LANE}",
    f"/bin/launchctl bootout gui/$(id -u)/{LANE}",
    f"launchctl bootout user/501/{RUNNER}",
    f"launchctl bootout {LANE}",
    f"launchctl bootout gui/501 $HOME/Library/LaunchAgents/{LANE}.plist",
    f"launchctl unload -w ~/Library/LaunchAgents/{LANE}.plist",
    f"launchctl remove {LANE}",
    f"launchctl kill SIGTERM gui/501/{LANE}",
    f"launchctl disable gui/501/{LEGACY}",
    f"launchctl stop {RUNNER}",
    f"echo ok && launchctl kickstart -k gui/501/{LANE}",
    f"true; launchctl kickstart -k gui/501/{LANE}",
    f"true || launchctl kickstart -k gui/501/{LANE}",
    f"echo x | launchctl kickstart -k gui/501/{LANE}",
    f"echo ok\nlaunchctl kickstart -k gui/501/{LANE}",
    f"bash -c 'launchctl kickstart -k gui/501/{LANE}'",
    f"sh -c \"launchctl bootout gui/501/{LANE}\"",
    f"zsh -lc 'launchctl bootout gui/501/{LANE}'",
    f"env X=1 launchctl kickstart -k gui/501/{LANE}",
    f"sudo launchctl kickstart -k gui/501/{LANE}",
    f"sudo -u daniel env -i launchctl kickstart -k gui/501/{LANE}",
    f"nohup launchctl kickstart -k gui/501/{LANE} &",
    f"X=1 launchctl kickstart -k gui/501/{LANE}",
    f"L={LANE}; launchctl kickstart -k gui/501/$L",
    f"export L={LANE}; launchctl bootout gui/501/${{L}}",
    f"echo $(launchctl bootout gui/501/{LANE})",
    f"ssh m5 'launchctl kickstart -k gui/501/{LANE}'",
    f"launchctl asuser 501 launchctl stop {LANE}",
    f"eval \"launchctl bootout gui/501/{LANE}\"",
    # Targets that cannot be shown to be outside tartci's lanes.
    "for l in a b; do launchctl bootout gui/501/$l; done",
    "launchctl list | awk '{print $3}' | xargs -n1 launchctl remove",
    "launchctl bootout gui/501/com.danielraffel.tartci.*",
    "launchctl bootout gui/501",
]

ALLOWED = [
    f"launchctl print gui/501/{LANE}",
    "launchctl list",
    f"launchctl print-disabled gui/501",
    f"launchctl bootstrap gui/501 ~/Library/LaunchAgents/{LANE}.plist",
    f"launchctl enable gui/501/{LANE}",
    "launchctl kickstart -k gui/501/com.apple.Finder",
    "launchctl bootout gui/501/homebrew.mxcl.postgresql",
    "echo launchctl kickstart -k foo",
    f"git commit -m 'launchctl kickstart gui/501/{LANE}'",
    f"tartci launchd reload {LANE}",
    "grep -rn launchctl scripts/",
    "ls -la",
]

BROAD_KILLS = [
    "pkill -9 -x bash",
    "pkill sh",
    "pkill -f bash",
    "pkill '^bash$'",
    "pkill -x 'bash|sh'",
    "pkill -x Python",
    "killall python3",
    "killall -u $USER",
    "pkill -u $USER",
    "pkill -f snapshot-zsh",
    "pkill -f zsh-snapshot",
    "pkill -f SNAPSHOT-ZSH",
    "pkill -f /bin/zsh",
    "pkill -f bin/bash",
    "pkill -f 'zsh.*snapshot'",
    "pkill -f 'runner.sh --loop'",
    "pkill -f TartCILauncher",
    "pkill -f 'zsh -c source'",
    "pkill -f shell-snapshots",
    "pkill -f 'zsh -lc'",
    "pkill -f claude-proxy",
    "pkill -f subrouter",
    "pkill -f /bin/sh",
    "pkill python3.12",
    "kill -9 -1",
    "kill -TERM -1",
    "kill -s KILL -1",
    "kill -1",
    "kill -s TERM -1",
    "kill -- -1",
    "kill -9 -- -1",
    "pkill -U 1234",
    "pkill -G 1234",
    "pkill -g 1234",
    "pkill -t ttys001",
    "agent-hygiene --reap 5 --yes",
]


class ClassifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._old_ps_snapshot = os.environ.get("TARTCI_GUARD_PS_SNAPSHOT")
        cls._old_testing = os.environ.get("TARTCI_GUARD_TESTING")
        os.environ["TARTCI_GUARD_TESTING"] = "1"
        os.environ["TARTCI_GUARD_PS_SNAPSHOT"] = str(PS_FIXTURE)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._old_ps_snapshot is None:
            os.environ.pop("TARTCI_GUARD_PS_SNAPSHOT", None)
        else:
            os.environ["TARTCI_GUARD_PS_SNAPSHOT"] = cls._old_ps_snapshot
        if cls._old_testing is None:
            os.environ.pop("TARTCI_GUARD_TESTING", None)
        else:
            os.environ["TARTCI_GUARD_TESTING"] = cls._old_testing

    def test_proxy_snapshot_and_sh_process_shapes_are_refused(self) -> None:
        # These are command-line shapes captured from ``ps -axo command`` on
        # the fleet.  ``pkill -f`` matches the complete command line, so each
        # pattern must be checked against these real paths rather than only a
        # process basename.
        for command in ("pkill -f claude-proxy", "pkill -f subrouter", "pkill -f /bin/sh"):
            with self.subTest(command=command):
                code, message = guard.decide(command)
                self.assertEqual(code, 2, command)
                self.assertIn("tartci launchd heal", message)

    def test_every_raw_lane_mutation_is_blocked(self) -> None:
        for command in BLOCKED:
            with self.subTest(command=command):
                code, message = guard.decide(command)
                self.assertEqual(code, 2, command)
                self.assertIn("tartci launchd reload", message)
                self.assertIn("tartci pool drain", message)
                self.assertIn("cached", message.lower())

    def test_unrelated_and_read_only_commands_pass_silently(self) -> None:
        # Positive control above: the same classifier blocks the lane forms, so
        # these passes are not an instrument that never fires.
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertEqual(guard.decide(command), (0, ""))

    def test_same_verb_on_a_foreign_service_is_the_control(self) -> None:
        self.assertEqual(guard.decide("launchctl kickstart -k gui/501/com.example.web")[0], 0)
        self.assertEqual(guard.decide(f"launchctl kickstart -k gui/501/{LANE}")[0], 2)

    def test_broad_process_kills_are_refused_and_targeted_recovery_is_named(self) -> None:
        # The static list is only the fail-closed path when ps is unavailable.
        old = os.environ["TARTCI_GUARD_PS_SNAPSHOT"]
        os.environ["TARTCI_GUARD_PS_SNAPSHOT"] = str(ROOT / "missing-ps-snapshot")
        try:
            for command in BROAD_KILLS:
                with self.subTest(command=command):
                    code, message = guard.decide(command)
                    self.assertEqual(code, 2, command)
                    self.assertIn("tartci launchd heal", message)
                    self.assertIn("tree top", message)
        finally:
            os.environ["TARTCI_GUARD_PS_SNAPSHOT"] = old

    def test_home_anchored_kills_are_refused_without_a_process_table(self) -> None:
        # The fail-closed list carries this account's real home, so a pattern
        # anchored on it still matches when ps is unavailable.
        home = str(Path.home())
        old = os.environ["TARTCI_GUARD_PS_SNAPSHOT"]
        os.environ["TARTCI_GUARD_PS_SNAPSHOT"] = str(ROOT / "missing-ps-snapshot")
        try:
            for command in (f"pkill -f '^{home}/.local/bin'",
                            f"pkill -f '{home}/.subrouter'",
                            f"pkill -f '^{home}/.local/share/tartci-generations'",
                            f"pkill -f '{home}/.claude/shell-snapshots'"):
                with self.subTest(command=command):
                    self.assertEqual(2, guard.decide(command)[0], command)
            self.assertTrue(guard._matches_static_fallback(f"^{home}/.local/bin"))
            # Control: a home-anchored pattern naming nothing protected passes.
            self.assertFalse(guard._matches_static_fallback(f"^{home}/harmless-worker$"))
        finally:
            os.environ["TARTCI_GUARD_PS_SNAPSHOT"] = old

    def test_live_process_table_refuses_real_agent_and_lane_shapes(self) -> None:
        patterns = (
            "pkill -f tartci-launcher",
            "pkill -f resume",
            "pkill -f 'codex resume'",
            "pkill -f 'sr codex'",
            "pkill -f 'claude proxy'",
            "pkill -f 'login -flp'",
            f"pkill -f '^{Path.home()}/.subrouter/codex/claude-proxy'",
            "pgrep -f 'sr codex' | xargs kill",
        )
        for command in patterns:
            with self.subTest(command=command):
                code, message = guard.decide(command)
                self.assertEqual(code, 2, command)
                self.assertIn("tartci launchd heal", message)

    def test_live_process_table_allows_pattern_matching_no_protected_process(self) -> None:
        self.assertEqual(guard.decide("pkill -f '^/Users/example/harmless-worker$'"), (0, ""))

    def test_read_only_pgrep_is_allowed(self) -> None:
        self.assertEqual(guard.decide("pgrep -f 'sr codex'"), (0, ""))
        self.assertEqual(guard.decide("pidof tartci-launcher"), (0, ""))

    def test_pid_source_patterns_are_checked_when_any_kill_is_present(self) -> None:
        commands = (
            "kill $(pgrep -f 'sr codex')",
            "kill -9 $(pgrep -f tartci-launcher)",
            "kill `pgrep -f 'sr codex'`",
            "pgrep -f tartci-launcher | sudo xargs kill",
            "while read pid; do kill $pid; done < <(pgrep -f 'sr codex')",
            "for p in $(pgrep -f tartci-launcher); do kill $p; done",
            "kill $(pidof tartci-launcher)",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(guard.decide(command)[0], 2)

    def test_raw_wrappers_and_interpreters_are_checked(self) -> None:
        commands = (
            "ssh m3 \"kill $(pgrep -f 'sr codex')\"",
            "ssh m3 'kill $(pgrep -f tartci-launcher)'",
            "bash -c \"kill $(pgrep -f 'sr codex')\"",
            "eval \"kill $(pgrep -f tartci-launcher)\"",
            "pgrep -f 'sr codex' | xargs sh -c 'kill $0'",
            "perl -e 'kill 9, grep(/sr codex/, qx(pgrep -f sr codex))'",
            "python3 -c \"import os; os.kill(int(next(iter([]))), 9) # pgrep -f sr codex\"",
            "awk 'system(\"kill $(pgrep -f tartci-launcher)\")'",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(guard.decide(command)[0], 2)

    def test_unquoted_pid_source_operands_and_assignments_are_checked(self) -> None:
        commands = (
            "ps aux | grep tartci-launcher | awk '{print $2}' | xargs kill -9",
            "kill $(ps aux | grep tartci-launcher | awk '{print $2}')",
            "PAT=tartci-launcher; kill $(pgrep -f $PAT)",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(guard.decide(command)[0], 2)
        code, message = guard.decide("kill $(pgrep -f $UNKNOWN)")
        self.assertEqual(code, 2)
        self.assertIn("<unresolved>", message)

    def test_read_only_grep_for_kill_words_is_allowed(self) -> None:
        for command in ("grep -i kill notes.txt | less", "grep pkill README.md | cat"):
            with self.subTest(command=command):
                self.assertEqual(guard.decide(command), (0, ""))

    def test_raw_read_only_wrappers_remain_allowed(self) -> None:
        for command in ("ssh m3 \"pgrep -f 'sr codex'\"",
                        "bash -c \"pgrep -f tartci-launcher\"",
                        "eval \"pidof tartci-launcher\""):
            with self.subTest(command=command):
                self.assertEqual(guard.decide(command), (0, ""))

    def test_descendant_of_protected_lane_is_refused(self) -> None:
        rows = "\n".join([
            "1 0 /sbin/launchd",
            "10 1 /Users/example/bin/tartci-launcher --lane pulp-gate",
            "11 10 /usr/bin/helper --safe-child",
        ] + [f"{100 + i} 1 /usr/bin/idle-{i}" for i in range(41)])
        with mock.patch.object(guard, "_process_table", return_value=[
            guard.ProcessRow(int(a), int(b), c)
            for a, b, c in (line.split(" ", 2) for line in rows.splitlines())
        ]):
            self.assertEqual(guard.decide("pkill -f safe-child")[0], 2)

    def test_static_protection_is_added_to_live_table(self) -> None:
        rows = [guard.ProcessRow(i, 1, f"/usr/bin/idle-{i}") for i in range(1, 42)]
        self.assertTrue(guard.classify("pkill -f tartci-launcher", process_rows=rows))

    def test_partial_table_fails_closed(self) -> None:
        partial = "\n".join(f"{i} 1 /usr/bin/idle-{i}" for i in range(1, 41))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial-ps.txt"
            path.write_text(partial + "\n", encoding="utf-8")
            old = os.environ["TARTCI_GUARD_PS_SNAPSHOT"]
            os.environ["TARTCI_GUARD_PS_SNAPSHOT"] = str(path)
            try:
                self.assertEqual(guard.decide("pkill -f harmless-worker")[0], 2)
            finally:
                os.environ["TARTCI_GUARD_PS_SNAPSHOT"] = old

    def test_slow_ps_fails_closed(self) -> None:
        with mock.patch.object(guard.subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", 5)):
            old = os.environ.pop("TARTCI_GUARD_PS_SNAPSHOT", None)
            try:
                self.assertEqual(guard.decide("pkill -f harmless-worker")[0], 2)
            finally:
                if old is not None:
                    os.environ["TARTCI_GUARD_PS_SNAPSHOT"] = old

    def test_benign_kills_are_the_control(self) -> None:
        for command in ("pkill -f worker-job", "killall fseventsd", "kill -TERM 1234",
                        "kill -1 1234",
                        "pkill -u $USER worker-job", "pkill -U 1234 worker-job",
                        "pkill -G 1234 worker-job", "pkill -g 1234 worker-job",
                        "pkill -t ttys001 worker-job"):
            with self.subTest(command=command):
                self.assertEqual(guard.decide(command), (0, ""))

    def test_malformed_regex_fails_closed_without_traceback(self) -> None:
        code, message = guard.decide("pkill '['")
        self.assertEqual(code, 2)
        self.assertIn("broad process kill", message)

    def test_launchctl_escape_does_not_authorize_a_broad_kill(self) -> None:
        code, message = guard.decide("TARTCI_ALLOW_RAW_LAUNCHCTL=1 pkill -x zsh")
        self.assertEqual(code, 2)
        self.assertIn("broad process kill", message)

    def test_escape_hatch_allows_with_a_warning(self) -> None:
        for command in (f"TARTCI_ALLOW_RAW_LAUNCHCTL=1 launchctl kickstart -k gui/501/{LANE}",
                        f"env TARTCI_ALLOW_RAW_LAUNCHCTL=1 launchctl bootout gui/501/{LANE}"):
            code, message = guard.decide(command)
            self.assertEqual(code, 0)
            self.assertIn("TARTCI_ALLOW_RAW_LAUNCHCTL=1", message)
        # Only the exact value counts.
        self.assertEqual(guard.decide(
            f"TARTCI_ALLOW_RAW_LAUNCHCTL=0 launchctl kickstart -k gui/501/{LANE}")[0], 2)
        code, message = guard.decide(
            "TARTCI_ALLOW_RAW_LAUNCHCTL=1 kill $(pgrep -f tartci-launcher)")
        self.assertEqual(code, 2)
        self.assertIn("broad process kill", message)


class HeredocTests(unittest.TestCase):
    """A heredoc body is data unless it feeds a shell interpreter or ssh."""

    DATA = [
        # A commit message that documents the incident.
        "git commit -m \"$(cat <<'EOF'\nfix: stop agents raw-kicking lanes\n\n"
        f"launchctl kickstart -k gui/501/{LANE} killed a VM.\nEOF\n)\"",
        # A runbook being written to a file.
        f"cat > /tmp/runbook.md <<'EOF'\nNever run:\n  launchctl bootout gui/501/{LANE}\nEOF",
        # A commit message on stdin with a bare verb.
        "git commit -F - <<X\nwhy launchctl bootout is dangerous:\nlaunchctl bootout\nX",
        # <<- strips tabs from the terminator.
        f"cat <<-EOF > notes\n\tlaunchctl stop {RUNNER}\n\tEOF",
    ]
    SHELL = [
        f"bash <<'EOF'\nlaunchctl bootout gui/501/{LANE}\nEOF",
        f"sh -s <<EOF\necho hi\nlaunchctl kickstart -k gui/501/{LANE}\nEOF",
        f"sudo zsh <<'EOF'\nlaunchctl bootout gui/501/{LANE}\nEOF",
        f"ssh m5 <<'EOF'\nlaunchctl bootout gui/501/{LANE}\nEOF",
        f"cat <<'EOF' > x\nlaunchctl bootout gui/501/{LANE}\nEOF\n"
        f"launchctl kickstart -k gui/501/{LANE}",
    ]

    def test_heredoc_bodies_are_data(self) -> None:
        for command in self.DATA:
            with self.subTest(command=command):
                self.assertEqual(guard.decide(command), (0, ""))

    def test_heredoc_into_a_shell_is_classified(self) -> None:
        # Positive control for the data cases: the same bodies fed to an
        # interpreter, or a real command after the heredoc, still block.
        for command in self.SHELL:
            with self.subTest(command=command):
                self.assertEqual(guard.decide(command)[0], 2)


class LaneFamilyTests(unittest.TestCase):
    def test_tartci_non_runner_agents_are_not_lanes(self) -> None:
        for label in ("com.danielraffel.tartci.launchd-watchdog",
                      "com.danielraffel.tartci.reap",
                      "com.danielraffel.tartci.reclaim",
                      "com.danielraffel.tartci.http-connect-ssh-relay"):
            with self.subTest(label=label):
                self.assertEqual(guard.decide(f"launchctl kickstart -k gui/501/{label}"), (0, ""))

    def test_every_runner_family_is_a_lane(self) -> None:
        for label in (LANE, f"{LANE}.slot2", RUNNER, LEGACY,
                      "com.danielraffel.pulp.tart-runner",
                      "com.danielraffel.pulp.tart-runner-macos-gate-slot2",
                      "com.danielraffel.pulp.qemu-runner-windows",
                      "com.danielraffel.forge.tart-runner-macos",
                      "com.danielraffel.vellum.tart-runner-macos"):
            with self.subTest(label=label):
                self.assertEqual(guard.decide(f"launchctl kickstart -k gui/501/{label}")[0], 2)


class HookEntryTests(unittest.TestCase):
    def _hook(self, payload: str, via: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(via, input=payload, text=True, capture_output=True,
                              check=False, cwd=tempfile.gettempdir())

    def _payload(self, command: str) -> str:
        return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})

    def test_shim_and_dispatcher_block_and_allow(self) -> None:
        for via in ([str(SHIM)], [str(ROOT / "tartci"), "launchd", "guard", "--hook"]):
            blocked = self._hook(self._payload(f"launchctl kickstart -k gui/501/{LANE}"), via)
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            self.assertIn("BLOCKED", blocked.stderr)
            allowed = self._hook(self._payload("launchctl list"), via)
            self.assertEqual((allowed.returncode, allowed.stdout, allowed.stderr), (0, "", ""))

    def test_claude_and_codex_hook_formats_refuse_broad_kills(self) -> None:
        payloads = [
            self._payload("pkill -9 -x bash"),
            json.dumps({"tool_name": "Bash", "tool_input": {
                "command": ["kill", "-9", "-1"]}}),
        ]
        for via in ([str(SHIM)], [str(ROOT / "tartci"), "launchd", "guard", "--hook"]):
            for payload in payloads:
                with self.subTest(via=via, payload=payload):
                    blocked = self._hook(payload, via)
                    self.assertEqual(blocked.returncode, 2, blocked.stderr)
                    self.assertIn("broad process kill", blocked.stderr)

    def test_shim_resolves_tartci_through_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / "hook.sh"
            link.symlink_to(SHIM)
            proc = self._hook(self._payload(f"launchctl bootout gui/501/{LANE}"), [str(link)])
            self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_malformed_input_never_breaks_the_shell(self) -> None:
        for payload in ("not json", "[]", "", '{"tool_input": 5}'):
            proc = self._hook(payload, [str(SHIM)])
            self.assertEqual(proc.returncode, 0, payload)
        proc = self._hook("not json", [str(SHIM)])
        self.assertIn("malformed", proc.stderr)

    def test_non_shell_tools_pass_and_argv_commands_are_classified(self) -> None:
        edit = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "/x"}})
        self.assertEqual(self._hook(edit, [str(SHIM)]).returncode, 0)
        argv = json.dumps({"tool_name": "shell", "tool_input": {
            "command": ["launchctl", "bootout", f"gui/501/{LANE}"]}})
        self.assertEqual(self._hook(argv, [str(SHIM)]).returncode, 2)


class HooksPrintTests(unittest.TestCase):
    def test_hooks_print_names_the_shim_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {**os.environ, "HOME": td}
            proc = subprocess.run([str(ROOT / "tartci"), "hooks", "print"],
                                  env=env, text=True, capture_output=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.count(str(SHIM)), 2)
            self.assertIn('"PreToolUse"', proc.stdout)
            self.assertIn('"matcher": "Bash"', proc.stdout)
            self.assertEqual(list(Path(td).iterdir()), [])
            blocks = [block for block in proc.stdout.split("\n\n")]
            parsed = [json.loads("\n".join(line for line in block.splitlines()
                                           if not line.startswith("#")))
                      for block in blocks if block.strip()]
            self.assertEqual(len(parsed), 2)


if __name__ == "__main__":
    unittest.main()
