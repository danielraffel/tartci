import os
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "agent-hygiene"

if shutil.which("zsh") is None:
    raise unittest.SkipTest("agent-hygiene is a macOS zsh entry point")


class AgentHygieneTests(unittest.TestCase):
    def run_script(self, *args, snapshot, transcripts, extra=None):
        env = os.environ.copy()
        env.update({
            "AH_PS_SNAPSHOT_FILE": str(snapshot),
            "AH_TRANSCRIPT_DIRS": str(transcripts),
            "AH_STALE_DAYS": "2",
            "AH_PROC_PCT": "1000",
            "AH_SWAP_PCT": "1000",
        })
        if extra:
            env.update(extra)
        return subprocess.run([str(SCRIPT), *args], text=True, capture_output=True, env=env)

    def run_tty(self, *args, env):
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.execve(str(SCRIPT), [str(SCRIPT), *args], env)
            except OSError:
                os._exit(127)
            os._exit(127)
        output = bytearray()
        sent = False
        deadline = time.monotonic() + 20
        status = None
        while status is None:
            if time.monotonic() >= deadline:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
                self.fail("PTY reap test timed out")
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(master, 4096)
                    if chunk:
                        output.extend(chunk)
                        if not sent and b"terminate these process groups" in output:
                            os.write(master, b"n\n")
                            sent = True
                except OSError:
                    pass
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited == 0:
                status = None
        while True:
            readable, _, _ = select.select([master], [], [], 0)
            if not readable:
                break
            try:
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                output.extend(chunk)
            except OSError:
                break
        os.close(master)
        return os.waitstatus_to_exitcode(status), output.decode(errors="replace")

    def _old_candidate_env(self, root: Path) -> dict[str, str]:
        snapshot = root / "ps"
        snapshot.write_text(
            "999991 1 00-00:01 10 /Users/test/.local/bin/claude\n"
        )
        transcripts = root / "transcripts"
        transcripts.mkdir()
        transcript = transcripts / "session-999991.jsonl"
        transcript.write_text("old")
        old = time.time() - 3 * 86400
        os.utime(transcript, (old, old))
        env = os.environ.copy()
        env.update(AH_PS_SNAPSHOT_FILE=str(snapshot), AH_TRANSCRIPT_DIRS=str(transcripts),
                   AH_STALE_DAYS="2", AH_PROC_PCT="1000", AH_SWAP_PCT="1000")
        return env

    def test_report_uses_transcript_activity_and_real_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot = root / "ps"
            snapshot.write_text("999991 1 00-00:01 10 /Users/test/.local/bin/claude\n")
            transcripts = root / "transcripts"
            transcripts.mkdir()
            transcript = transcripts / "session-999991.jsonl"
            transcript.write_text("recent")
            old = time.time() - 3 * 86400
            os.utime(transcript, (old, old))
            result = self.run_script("--report", snapshot=snapshot, transcripts=transcripts)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("999991 3d", result.stdout)

    def test_reap_refuses_agent_context_and_does_not_use_process_etime(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot = root / "ps"
            snapshot.write_text("999991 1 00-00:01 10 /Users/test/.local/bin/claude\n")
            transcripts = root / "transcripts"
            transcripts.mkdir()
            transcript = transcripts / "session-999991.jsonl"
            transcript.write_text("recent")
            old = time.time() - 3 * 86400
            os.utime(transcript, (old, old))
            result = self.run_script("--reap", "2", "--yes", snapshot=snapshot,
                                     transcripts=transcripts, extra={"CODEX_SANDBOX": "1"})
            self.assertEqual(result.returncode, 2)
            self.assertIn("human-only", result.stderr)
            self.assertIn("--yes is ignored", result.stderr) if "--yes is ignored" in result.stderr else None

    def test_non_tty_reap_refuses_without_agent_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = self._old_candidate_env(root)
            result = subprocess.run([str(SCRIPT), "--reap", "2"],
                                    text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("human-only", result.stderr)

    def test_live_subagent_child_is_protected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot = root / "ps"
            snapshot.write_text("999991 1 00-00:01 10 /Users/test/.local/bin/claude\n999992 999991 00-00:01 10 subagent --resume\n")
            transcripts = root / "transcripts"
            transcripts.mkdir()
            transcript = transcripts / "session-999991.jsonl"
            transcript.write_text("old")
            old = time.time() - 3 * 86400
            os.utime(transcript, (old, old))
            result = self.run_script("--reap", "2", snapshot=snapshot,
                                     transcripts=transcripts, extra={"CODEX_SANDBOX": "1"})
            self.assertEqual(result.returncode, 0)
            self.assertIn("sessions older than 2d: 0", result.stdout)

    def test_pty_reap_with_yes_still_asks_and_n_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            env = self._old_candidate_env(Path(td))
            code, output = self.run_tty("--reap", "2", "--yes", env=env)
            self.assertEqual(code, 0, output)
            self.assertIn("--yes is ignored", output)
            self.assertIn("terminate these process groups", output)
            self.assertNotIn("done; victims log", output)

    def test_pty_agent_context_refuses_before_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            env = self._old_candidate_env(Path(td))
            env["CLAUDECODE"] = "1"
            code, output = self.run_tty("--reap", "2", env=env)
            self.assertEqual(code, 2, output)
            self.assertIn("human-only", output)
            self.assertNotIn("terminate these process groups", output)


if __name__ == "__main__":
    unittest.main()
