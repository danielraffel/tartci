#!/usr/bin/env python3
"""PreToolUse guard: raw launchctl mutations of tartci lanes are blocked."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launchctl_guard as guard

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "claude-pretooluse-launchctl.sh"
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


class ClassifierTests(unittest.TestCase):
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

    def test_escape_hatch_allows_with_a_warning(self) -> None:
        for command in (f"TARTCI_ALLOW_RAW_LAUNCHCTL=1 launchctl kickstart -k gui/501/{LANE}",
                        f"env TARTCI_ALLOW_RAW_LAUNCHCTL=1 launchctl bootout gui/501/{LANE}"):
            code, message = guard.decide(command)
            self.assertEqual(code, 0)
            self.assertIn("TARTCI_ALLOW_RAW_LAUNCHCTL=1", message)
        # Only the exact value counts.
        self.assertEqual(guard.decide(
            f"TARTCI_ALLOW_RAW_LAUNCHCTL=0 launchctl kickstart -k gui/501/{LANE}")[0], 2)


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
