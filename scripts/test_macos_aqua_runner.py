#!/usr/bin/env python3
"""Focused tests for the macOS console-Aqua JIT runner launcher."""

from __future__ import annotations

import os
import pathlib
import plistlib
import signal
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
GUEST = ROOT / "providers" / "tart-macos" / "guest-aqua-runner.sh"
SUPERVISOR = ROOT / "providers" / "tart-macos" / "runner.sh"


FAKE_LAUNCHCTL = r"""#!/usr/bin/env python3
import json
import os
import pathlib
import plistlib
import signal
import signal
import subprocess
import sys
import time

state_path = pathlib.Path(os.environ["FAKE_LAUNCHCTL_STATE"])
log_path = pathlib.Path(os.environ["FAKE_LAUNCHCTL_LOG"])
args = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(" ".join(args) + "\n")

def load():
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))

def save(value):
    state_path.write_text(json.dumps(value), encoding="utf-8")

if args[:1] == ["print"] and len(args) == 2:
    target = args[1]
    if target.startswith("gui/") and target.count("/") == 1:
        uid = target.split("/")[1]
        print(f'''{target} = {{
\ttype = login
\tsession = {os.environ.get("FAKE_AQUA_SESSION", "Aqua")}
\tsecurity context = {{
\t\tuid = {uid}
\t\tasid = {os.environ.get("FAKE_DOMAIN_ASID", "100123")}
\t}}
}}''')
        if os.environ.get("FAKE_AQUA_LARGE_DOMAIN") == "1":
            for index in range(20000):
                print(f"\tservice-{index} = idle")
        raise SystemExit(0)
    if target.startswith("pid/"):
        print(f'''{target} = {{
\tsecurity context = {{
\t\tuid = {os.environ["TARTCI_AQUA_EXPECTED_UID"]}
\t\tasid = {os.environ.get("FAKE_PROCESS_ASID", "100123")}
\t}}
}}''')
        if os.environ.get("FAKE_AQUA_LARGE_DOMAIN") == "1":
            for index in range(20000):
                print(f"\tprocess-property-{index} = idle")
        raise SystemExit(0)
    state = load()
    pid = state.get("pid")
    if pid:
        try:
            os.kill(pid, 0)
            raise SystemExit(0)
        except ProcessLookupError:
            pass
    raise SystemExit(1)

if args[:1] == ["bootstrap"] and len(args) == 3:
    save({"plist": args[2]})
    raise SystemExit(0)

if args[:2] == ["kickstart", "-k"] and len(args) == 3:
    state = load()
    with open(state["plist"], "rb") as stream:
        command = plistlib.load(stream)["ProgramArguments"]
    # launchd runs each job in its own process group and tears the whole group
    # down on bootout.  Model that, or a job's children outlive the test.
    proc = subprocess.Popen(command, env=os.environ.copy(), start_new_session=True)
    state["pid"] = proc.pid
    save(state)
    with open(os.environ["FAKE_LAUNCHCTL_PGIDS"], "a", encoding="utf-8") as stream:
        stream.write(f"{proc.pid}\n")
    raise SystemExit(0)

if args[:1] == ["bootout"] and len(args) == 2:
    state = load()
    pid = state.get("pid")
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    state_path.unlink(missing_ok=True)
    raise SystemExit(0)

raise SystemExit(2)
"""


# The fake runner blocks at most this long, so a teardown miss cannot leave a
# process running for days.
FAKE_RUNNER_MAX_SECS = 30


def processes_tagged(tag: str, pgids: frozenset = frozenset()) -> list[tuple[int, int, str]]:
    """Return (pid, pgid, command) for live processes belonging to a test.

    A process belongs when its argv or environment holds tag, or it shares a
    process group with one that does, or its group is listed in pgids.  The
    group rules catch children such as `/bin/bash ./run.sh` whose argv never
    names the tag and whose environment `ps e` cannot show (platform binaries on macOS).
    """
    output = subprocess.run(
        ["/bin/ps", "axwwe", "-o", "pid=,pgid=,stat=,command="],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    rows = []
    for line in output.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 4:
            continue
        pid, pgid, state = int(fields[0]), int(fields[1]), fields[2]
        if pid == os.getpid() or state.startswith("Z"):
            continue
        rows.append((pid, pgid, fields[3]))
    own_pgid = os.getpgid(0)
    groups = set(pgids) | {pgid for _, pgid, command in rows if tag in command}
    groups.discard(own_pgid)
    return [row for row in rows if tag in row[2] or row[1] in groups]


def reap_tagged(tag: str, pgids: frozenset = frozenset(), timeout: float = 5.0) -> list[tuple[int, int, str]]:
    """SIGKILL every process group belonging to a test; return what was alive."""
    found = processes_tagged(tag, pgids)
    own_pgid = os.getpgid(0)
    for pid, pgid, _ in found:
        try:
            if pgid != own_pgid:
                os.killpg(pgid, signal.SIGKILL)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while processes_tagged(tag, pgids) and time.monotonic() < deadline:
        time.sleep(0.05)
    return found


class AquaRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)
        self.expect_reaped = False
        # Outside the tmp dir so the record survives temp.cleanup.
        pgid_fd, pgid_name = tempfile.mkstemp(prefix="tartci-aqua-pgids-")
        os.close(pgid_fd)
        self.pgid_log = pathlib.Path(pgid_name)
        self.addCleanup(self.pgid_log.unlink, missing_ok=True)
        # Registered after temp.cleanup, so it runs first: the tmp path is the
        # tag that identifies everything this test spawned.
        self.addCleanup(self.reap_and_check)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.launchctl = self.bin / "launchctl"
        self.launchctl.write_text(FAKE_LAUNCHCTL, encoding="utf-8")
        self.launchctl.chmod(self.launchctl.stat().st_mode | stat.S_IXUSR)
        self.fake_stat = self.bin / "stat"
        self.fake_stat.write_text(
            f"#!/bin/sh\nprintf '%s\\n' {subprocess.check_output(['/usr/bin/id', '-un'], text=True).strip()!r}\n",
            encoding="utf-8",
        )
        self.fake_stat.chmod(0o700)
        self.fake_pgrep = self.bin / "pgrep"
        self.fake_pgrep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fake_pgrep.chmod(0o700)
        self.fake_plutil = self.bin / "plutil"
        self.fake_plutil.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import pathlib
                import plistlib
                import sys

                if len(sys.argv) != 3 or sys.argv[1] != "-lint":
                    raise SystemExit(64)
                with open(sys.argv[2], "rb") as stream:
                    plistlib.load(stream)
                pathlib.Path(os.environ["FAKE_PLUTIL_LOG"]).write_text(
                    " ".join(sys.argv[1:]), encoding="utf-8"
                )
                """
            ),
            encoding="utf-8",
        )
        self.fake_plutil.chmod(0o700)
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "FAKE_LAUNCHCTL_STATE": str(self.home / "launchctl-state.json"),
                "FAKE_LAUNCHCTL_LOG": str(self.home / "launchctl.log"),
                "FAKE_LAUNCHCTL_PGIDS": str(self.pgid_log),
                "TARTCI_GUEST_LAUNCHCTL": str(self.launchctl),
                "TARTCI_GUEST_STAT": str(self.fake_stat),
                "TARTCI_GUEST_PGREP": str(self.fake_pgrep),
                "TARTCI_GUEST_PLUTIL": str(self.fake_plutil),
                "FAKE_PLUTIL_LOG": str(self.home / "plutil.log"),
                "TARTCI_AQUA_EXPECTED_UID": str(os.getuid()),
                "TARTCI_AQUA_WAIT_SECS": "0",
                "TARTCI_AQUA_READY_SECS": "5",
                "FAKE_DOMAIN_ASID": "100123",
                "FAKE_PROCESS_ASID": "100123",
            }
        )

    def job_pgids(self) -> frozenset:
        try:
            text = self.pgid_log.read_text(encoding="utf-8")
        except FileNotFoundError:
            return frozenset()
        return frozenset(int(line) for line in text.split())

    def owned_processes(self) -> list[tuple[int, int, str]]:
        return processes_tagged(str(self.home), self.job_pgids())

    def reap_and_check(self) -> None:
        pgids = self.job_pgids()
        leaked = reap_tagged(str(self.home), pgids)
        survivors = processes_tagged(str(self.home), pgids)
        if survivors:
            self.fail(f"processes survived SIGKILL teardown: {survivors}")
        if leaked and not self.expect_reaped:
            self.fail(f"test leaked processes (now reaped): {leaked}")

    def wait_until_untagged(self, timeout: float = 10.0) -> list[tuple[int, int, str]]:
        deadline = time.monotonic() + timeout
        while True:
            alive = self.owned_processes()
            if not alive or time.monotonic() >= deadline:
                return alive
            time.sleep(0.1)

    def invoke(self, command: str, label: str = "com.tartci.test") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(GUEST), command, label],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def install_fake_runner(self, marker: pathlib.Path, *, block: bool = False) -> None:
        runner = self.home / "actions-runner"
        runner.mkdir()
        script = runner / "run.sh"
        script.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/bash
                set -euo pipefail
                [ "${{ACTIONS_RUNNER_INPUT_JITCONFIG:-}}" = 'top-secret-jit' ]
                case "$(ps -p $$ -o command=)" in *top-secret-jit*) exit 90 ;; esac
                printf 'Running job: Aqua test\\n'
                printf success > {str(marker)!r}
                {f"for _ in $(seq {FAKE_RUNNER_MAX_SECS}); do sleep 1; done" if block else ":"}
                """
            ),
            encoding="utf-8",
        )
        script.chmod(0o700)

    def put_jit(self, label: str = "com.tartci.test") -> pathlib.Path:
        path = self.home / ".tartci" / "aqua-runner" / label / "jit.cfg"
        path.parent.mkdir(parents=True)
        path.write_text("top-secret-jit", encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_preflight_launchagent_matches_console_aqua_asid(self) -> None:
        result = self.invoke("preflight")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preflight-ok", result.stderr)
        calls = (self.home / "launchctl.log").read_text(encoding="utf-8")
        self.assertIn(f"bootstrap gui/{os.getuid()}", calls)
        self.assertIn(f"kickstart -k gui/{os.getuid()}/com.tartci.test", calls)
        plutil = (self.home / "plutil.log").read_text(encoding="utf-8")
        self.assertIn("-lint", plutil)

    def test_preflight_accepts_large_launchctl_domain_without_pipefail_false_negative(self) -> None:
        self.env["FAKE_AQUA_LARGE_DOMAIN"] = "1"
        result = self.invoke("preflight")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preflight-ok", result.stderr)

    def test_plutil_rejection_returns_78_and_cleans_preflight(self) -> None:
        self.fake_plutil.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        result = self.invoke("preflight")
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn("plist failed validation", result.stderr)
        self.assertNotIn("unbound variable", result.stderr)
        self.assertFalse((self.home / "launchctl-state.json").exists())
        self.assertFalse(
            (self.home / ".tartci/aqua-runner/com.tartci.test.preflight").exists()
        )

    def test_preflight_fails_closed_without_aqua_session_and_never_bootstraps(self) -> None:
        self.env["FAKE_AQUA_SESSION"] = "Background"
        result = self.invoke("preflight")
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn("healthy console Aqua session", result.stderr)
        self.assertIn("last=non-aqua-domain", result.stderr)
        self.assertNotIn("unbound variable", result.stderr)
        self.assertFalse(
            (self.home / ".tartci/aqua-runner/com.tartci.test.preflight").exists()
        )
        calls = (self.home / "launchctl.log").read_text(encoding="utf-8")
        self.assertNotIn("bootstrap", calls)

    def test_probe_rejects_mutated_process_asid(self) -> None:
        self.env["FAKE_PROCESS_ASID"] = "100999"
        result = self.invoke("preflight")
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn("Aqua probe ASID mismatch", result.stderr)

    def test_live_launch_uses_file_input_and_removes_secret(self) -> None:
        marker = self.home / "runner-ran"
        self.install_fake_runner(marker)
        jit = self.put_jit()
        result = self.invoke("run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())
        self.assertFalse(jit.exists())
        self.assertIn("aqua-runner-shell", result.stdout)
        self.assertIn("Running job: Aqua test", result.stdout)
        self.assertNotIn("top-secret-jit", result.stdout + result.stderr)

    def test_live_guard_blocks_runner_after_asid_mutation(self) -> None:
        marker = self.home / "runner-ran"
        self.install_fake_runner(marker)
        jit = self.put_jit()
        self.env["FAKE_PROCESS_ASID"] = "100999"
        result = self.invoke("run")
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(jit.exists())
        self.assertIn("failed its live ASID guard", result.stderr)
        self.assertNotIn("unbound variable", result.stderr)
        self.assertFalse((self.home / "launchctl-state.json").exists())

    def test_disconnect_signal_boots_out_launchagent_and_scrubs_jit(self) -> None:
        marker = self.home / "runner-ran"
        self.install_fake_runner(marker, block=True)
        jit = self.put_jit()
        process = subprocess.Popen(
            [str(GUEST), "run", "com.tartci.test"],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.1)
        self.assertTrue(marker.exists(), "mock runner never reached its live state")
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, stdout + stderr)
        self.assertFalse(jit.exists())
        self.assertFalse((self.home / "launchctl-state.json").exists())
        calls = (self.home / "launchctl.log").read_text(encoding="utf-8")
        self.assertIn(f"bootout gui/{os.getuid()}/com.tartci.test", calls)

    def test_disconnect_leaves_no_runner_process_behind(self) -> None:
        # The runner-child shell defers its TERM trap while ./run.sh runs in
        # the foreground, so only a process-group teardown stops a blocked run.
        marker = self.home / "runner-ran"
        self.install_fake_runner(marker, block=True)
        self.put_jit()
        process = subprocess.Popen(
            [str(GUEST), "run", "com.tartci.test"],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.1)
        self.assertTrue(marker.exists(), "mock runner never reached its live state")
        self.assertTrue(
            any("run.sh" in command and "runner-child" not in command for _, _, command in self.owned_processes()),
            "instrument control: the live runner-child must be visible to the scan",
        )
        process.terminate()
        process.communicate(timeout=10)
        self.assertEqual(self.wait_until_untagged(), [])

    def test_teardown_reaps_a_detached_process_group(self) -> None:
        sleeper = self.home / "leaky.sh"
        sleeper.write_text("#!/bin/bash\nsleep 300 &\nwait\n", encoding="utf-8")
        sleeper.chmod(0o700)
        subprocess.Popen(
            [str(sleeper)],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while True:
            owned = self.owned_processes()
            if any("sleep 300" in command for _, _, command in owned):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        self.assertTrue(any("sleep 300" in command for _, _, command in owned), owned)
        group = {pgid for _, pgid, _ in owned}
        self.expect_reaped = True
        self.doCleanups()
        self.assertEqual(processes_tagged(str(sleeper), frozenset(group)), [])

    def test_supervisor_mints_only_after_preflight_and_never_places_jit_on_command_line(self) -> None:
        supervisor = SUPERVISOR.read_text(encoding="utf-8")
        run_one = supervisor[supervisor.index("run_one(){") :]
        preflight = run_one.index('install_and_preflight_aqua_runner "$ip" "$vm"')
        mint = run_one.index("generate-jitconfig")
        self.assertLess(preflight, mint)
        self.assertNotIn("./run.sh --jitconfig", supervisor)
        self.assertNotIn("printf '%s' '$jit'", supervisor)
        discard = supervisor[supervisor.index("discard_current_vm(){") : supervisor.index("cleanup(){")]
        self.assertLess(discard.index("stop_current_aqua_runner"), discard.index("terminate_current_guardian"))
        guardian = supervisor[supervisor.index("terminate_current_guardian(){") : supervisor.index("discard_current_vm(){")]
        self.assertLess(guardian.index('kill -9 "$pid"'), guardian.index('wait "$pid"'))
        self.assertIn('! kill -0 "$pid"', guardian)
        stream = supervisor[supervisor.index("run_runner_until_done(){") : supervisor.index("install_and_preflight_aqua_runner(){")]
        self.assertLess(stream.index('CURRENT_AQUA_LABEL="$aqua_label"'), stream.index("printf '%s' \"$jit\" | ssh"))
        self.assertIn("failed to stream JIT config", stream)
        self.assertIn("stop_current_aqua_runner", stream)
        guest = GUEST.read_text(encoding="utf-8")
        self.assertIn('jit_config="$(cat "$jit_file")"', guest)
        self.assertIn('export ACTIONS_RUNNER_INPUT_JITCONFIG="$jit_config"', guest)
        self.assertIn('actual_asid" != "$console_asid', guest)
        self.assertIn('bootout "$label"', guest)


if __name__ == "__main__":
    unittest.main()
