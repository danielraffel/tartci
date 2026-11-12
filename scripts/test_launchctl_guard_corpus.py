#!/usr/bin/env python3
"""Regression corpus for every reviewed broad-kill command shape."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launchctl_guard as guard
from test_launchctl_guard import PS_FIXTURE


# Copied from the reviewer-owned probe5.py/probe6.py shape lists.
MUST_REFUSE = tuple(dict.fromkeys(
    ["ps aux | grep tartci-launcher | awk '{print $2}' | xargs kill -9", "kill $(ps -axo pid=,command= | grep tartci-launcher | awk '{print $1}')", 'PAT=tartci-launcher; kill $(pgrep -f $PAT)', 'kill $(pgrep -f $UNSET_PATTERN)', 'kill $(pgrep -f "${NAME}")', "pkill -f 'claude proxy --resume'", "kill $(pgrep -f 'sr codex')", 'kill -9 $(pgrep -f tartci-launcher)', 'kill -9 `pgrep -f codex`', 'pgrep -f codex | sudo xargs kill', 'pgrep -f codex | while read p; do kill $p; done', "for p in $(pgrep -f 'sr codex'); do kill $p; done", 'ssh m3 \'kill $(pgrep -f "sr codex")\'', "ssh m3 'kill $(pgrep -f tartci-launcher)'", "ssh m3 'for p in $(pgrep -f codex); do kill $p; done'", 'ssh m3 "kill \\$(pgrep -f \'sr codex\')"', 'bash -c "kill \\$(pgrep -f \'sr codex\')"', 'eval "kill \\$(pgrep -f \'sr codex\')"', "pgrep -f 'sr codex' | xargs -I{} sh -c 'kill {}'", "pgrep -f 'sr codex' | perl -ne 'kill 9, $_'", "pgrep -f 'sr codex' | python3 -c 'import os,sys; [os.kill(int(p),9) for p in sys.stdin]'", 'pgrep -f \'sr codex\' | awk \'{system("kill " $1)}\'', 'PAT=\'sr codex\'; kill $(pgrep -f "$PAT")', "ps -axo pid=,command= | grep 'sr codex' | awk '{print $1}' | xargs kill", 'kill $(lsof -t -c codex)', "P=$(pgrep -f 'sr codex'); kill $P", "launchctl kill SIGKILL pid/$(pgrep -f 'sr codex')", "pkill -f 'sr codex resume'", "pkill -f 'login -flp danielraffel'", "pkill -f 'MacOS/tartci-launcher --lane'", 'pkill -f studio-pulp-gate', 'pkill -f tartci-launcher', 'pkill -f claude-proxy', 'pkill -f /bin/sh', "/bin/kill $(pgrep -f 'sr codex')", 'ssh m3 \'/bin/kill $(pgrep -f "sr codex")\'', "ssh m3 'pgrep -f codex | while read p; do kill $p; done'", "ssh m3 'if pgrep -f tartci-launcher >/dev/null; then kill $(pgrep -f tartci-launcher); fi'", "ssh m3 'timeout 5 kill $(pgrep -f tartci-launcher)'", 'ssh m3 \'env kill -9 $(pgrep -f "sr codex")\'', 'timeout 5 kill $(pgrep -f tartci-launcher)', "nohup kill $(pgrep -f 'sr codex')", 'ssh m3 \'ps aux | grep tartci-launcher | awk "{print \\$2}" | xargs kill\'', "TARTCI_ALLOW_RAW_LAUNCHCTL=1 kill $(pgrep -f 'sr codex')", "TARTCI_ALLOW_RAW_LAUNCHCTL=1 ssh m3 'kill $(pgrep -f tartci-launcher)'"] + [
        "ssh m3 '/usr/bin/kill $(pgrep -f \"sr codex\")'",
        "ssh m3 'nice -n 5 kill $(pgrep -f tartci-launcher)'",
        "ssh m3 'exec kill -9 $(pgrep -f \"sr codex\")'",
        "ssh m3 'command kill $(pgrep -f tartci-launcher)'",
        "pkill codex", "killall codex", "pkill tartci-launcher", "killall -m 'tartci.*'",
        "pkill -x sr", "ssh m3 'killall -m \"tartci.*\"'", "ssh m3 pkill codex",
        "kill -9 $(pgrep -x codex)", "kill $(pgrep codex)",
        "pkill launcher", "pkill odex", "pkill laude", "pkill daemon-launcher",
        "ssh m3 'pkill launcher'", "pkill -P 1", "ssh m3 'pkill -P 1'",
        "pkill -g 1", "pkill -s 1",
        "git status && ssh m3 'kill $(pgrep -f \"sr codex\")'",
        "git fetch -q; ssh m3 'kill $(pgrep -f tartci-launcher)'",
        "ls >/dev/null; bash -c \"kill \\$(pgrep -f 'sr codex')\"",
        "tar --version >/dev/null && pgrep -f 'sr codex' | perl -ne 'kill 9, $_'",
        "git log -1 && pgrep -f 'sr codex' | python3 -c 'import os,sys; [os.kill(int(p),9) for p in sys.stdin]'",
        "git status; eval \"kill \\$(pgrep -f 'sr codex')\"",
        "pkill -9 launcher", "pkill -9 -P 1", "pkill -i CODEX", "killall -m '.*'",
        "git rebase -x \"pkill -f tartci-launcher\" HEAD~1",
        "git rebase --exec 'kill $(pgrep -f \"sr codex\")' HEAD~2",
        "git bisect run sh -c 'pkill -f tartci-launcher'",
        "git -c core.pager='sh -c \"kill \\$(pgrep -f tartci-launcher)\"' log",
        "tar -cf /dev/null --to-command='kill $(pgrep -f tartci-launcher)' x",
        "git -c core.sshCommand='ssh m3 pkill -f tartci-launcher' fetch",
        "env GIT_SSH_COMMAND='ssh m3 pkill -f tartci-launcher' git fetch",
        "git submodule foreach 'pkill -f tartci-launcher'",
        "git filter-branch --tree-filter 'pkill -f tartci-launcher'",
        "git config alias.z '!pkill -f tartci-launcher'",
        "git z pkill -f tartci-launcher",
        "tar -xI 'pkill -f tartci-launcher'",
        "tar -I 'pkill -f tartci-launcher' -tf archive.tar",
        "tar --use-compress-program='pkill -f tartci-launcher' -tf archive.tar",
        "tar --checkpoint-action=exec='pkill -f tartci-launcher' -tf archive.tar",
        "git --upload-pack='pkill -f tartci-launcher' fetch",
        "git --receive-pack='pkill -f tartci-launcher' push",
        "git -O 'pkill -f tartci-launcher' log",
        "git --open-files-in-pager='pkill -f tartci-launcher' log",
        "git --exec='pkill -f tartci-launcher' log",
        "git grep -O'sh -c \"kill \\$(pgrep -f tartci-launcher)\"' main",
        "git grep --open-files-in-pager='kill $(pgrep -f \"sr codex\")' x",
        "git grep -O'kill $(pgrep -f tartci-launcher)' x",
        "git -C /tmp/x grep -O'kill $(pgrep -f tartci-launcher)' x",
        "git fetch --upload-pack='sh -c \"kill \\$(pgrep -f tartci-launcher)\"' .",
        "git ls-remote --upload-pack='kill $(pgrep -f \"sr codex\")' .",
        "git ls-remote -u 'kill $(pgrep -f \"sr codex\")' .",
        "tar --info-script='kill $(pgrep -f tartci-launcher)' -tf archive.tar",
        "tar --new-volume-script='kill $(pgrep -f tartci-launcher)' -tf archive.tar",
        "tar --rsh-command='kill $(pgrep -f tartci-launcher)' -tf archive.tar",
        "tar -F 'kill $(pgrep -f tartci-launcher)' -tf archive.tar",
        "rg --pre 'sh -c \"kill \\$(pgrep -f tartci-launcher)\"' pattern .",
        "rg --pre='kill $(pgrep -f \"sr codex\")' x .",
        "ag --pager='kill $(pgrep -f tartci-launcher)' pattern",
    ]
))
MUST_ALLOW = tuple(['pgrep -f codex', "pgrep -fl 'sr codex'", "pgrep -f 'sr codex' | wc -l", 'pgrep -f codex && echo found', 'ps aux | grep codex', "ps -axo pid,ppid,command | grep 'sr codex'", 'tail -f ~/Library/Logs/tartci/tartci-self-update.log', 'ps aux | grep -i kill', 'ps aux | grep pkill', "ps aux | grep 'kill'", 'ps aux | grep "pkill -f"', "grep -rn 'pkill -f' scripts/", "rg -n 'kill' scripts/launchctl_guard.py", 'rg -n "os.kill" scripts/', "git grep -n 'kill -9' -- scripts", 'git log --oneline | grep -i kill', "grep -n 'xargs kill' scripts/*.py", 'python3 -m unittest -f scripts.test_launchctl_guard', 'pytest -x scripts/test_kill_guard.py', 'lsof -t -i :8080', 'launchctl list | grep tartci', "echo 'do not kill the lane' && tartci pool status -f", "ls -lf /bin/kill", "tar -xf kill-switch.tar", "git commit -F msg.txt -m 'guard: refuse broad kill'", "sed -n '/kill/p' run.log", "cat docs/kill.md", "man kill", "which pkill", "gh pr view 487 --json body -q .body | less", "env FOO=bar ls -lf /bin/kill", "sudo git commit -F msg.txt -m 'guard: refuse broad kill'", "nohup tar -xf kill-switch.tar", "tar -tf archive.tar", "tar -tzf archive.tar", "sudo tar -tf archive.tar", "git -C /tmp/x log", "git -C /tmp/x log --oneline -S 'pkill -f'",])

def _rows(path: Path) -> list[guard.ProcessRow]:
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = guard.PROCESS_ROW.match(line)
        if match:
            result.append(guard.ProcessRow(int(match.group(1)), int(match.group(2)), match.group(3)))
    return result


FULL_ROWS = _rows(PS_FIXTURE)
PS_M5 = Path(__file__).resolve().parent / "fixtures" / "launchctl_guard_ps_m5.txt"
M5_ROWS = _rows(PS_M5)
PARTIAL_ROWS = M5_ROWS[:100]
TABLE_STATES = {
    "m3": FULL_ROWS,
    "m5": M5_ROWS,
    "partial-100": PARTIAL_ROWS,
    "no-table": None,
    "slow-ps": None,
}


class ReviewedCommandCorpus(unittest.TestCase):
    def test_every_must_refuse_shape_across_every_table_state(self) -> None:
        for state, rows in TABLE_STATES.items():
            for command in MUST_REFUSE:
                with self.subTest(state=state, command=command):
                    with mock.patch.object(guard, "_process_table", return_value=rows):
                        self.assertEqual(guard.decide(command)[0], 2)

    def test_every_must_allow_shape_across_every_table_state(self) -> None:
        for state, rows in TABLE_STATES.items():
            for command in MUST_ALLOW:
                with self.subTest(state=state, command=command):
                    with mock.patch.object(guard, "_process_table", return_value=rows):
                        self.assertEqual(guard.decide(command), (0, ""))

    def test_slow_ps_receipt_is_explicitly_represented(self) -> None:
        with mock.patch.object(guard.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("ps", 5)):
            with mock.patch.dict("os.environ", {"TARTCI_GUARD_TESTING": "1"}, clear=False):
                self.assertEqual(guard._process_table(), None)


if __name__ == "__main__":
    unittest.main()
