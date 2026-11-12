#!/usr/bin/env python3
"""`tartci pool off` refuses a mid-job lane; `--plan` shows a transition and writes nothing.

A fake host: temporary HOME + LaunchAgents, a fleet receipt, and stubbed
`launchctl`, `ps`, `scutil`, `nohup` on PATH. Nothing reaches real launchd.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANE = "com.danielraffel.tartci.tart-runner-macos-fleet.studio.pulp-gate"
IDLE_LANE = "com.danielraffel.tartci.tart-runner-macos-fleet.studio.forge-gate"
UNOWNED = "actions.runner.danielraffel-Shipyard.Shipyard-studio-02"
MUTATIONS = ("bootout", "bootstrap", "disable", "enable", "kickstart")

PS_TREE = textwrap.dedent("""\
      PID  PPID COMMAND
      100     1 /bin/bash /Users/x/.local/bin/tartci serve macos --loop
      101   100 /opt/homebrew/bin/tart run --no-graphics pulp-studio-1
      200     1 /bin/bash /Users/x/.local/bin/tartci serve macos --loop
      201   200 sleep 20
    """)


class FakePoolHost:
    def __init__(self, root: Path, *, busy: bool = True) -> None:
        self.root = root
        home = root / "home"
        self.agents = home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)
        for label in (LANE, IDLE_LANE, UNOWNED):
            with (self.agents / f"{label}.plist").open("wb") as handle:
                plistlib.dump({"Label": label, "ProgramArguments": ["/bin/true"]}, handle)
        config = home / ".config" / "tartci"
        config.mkdir(parents=True)
        (config / "macos-fleet-install.json").write_text(json.dumps({
            "schema": 3,
            "plists": {f"{LANE}.plist": {"sha256": "x"}, f"{IDLE_LANE}.plist": {"sha256": "x"}},
            "persistent_plists": {},
        }))
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "launchctl.log"
        self.prints = root / "prints"
        self.prints.mkdir()
        self.ps_rc = root / "ps.rc"
        (root / "ps.out").write_text(PS_TREE)
        stubs = {
            "scutil": "#!/bin/sh\nprintf 'test-host\\n'\n",
            "nohup": f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"{root}/nohup.log\"\n",
            "ps": textwrap.dedent(f"""\
                #!/bin/sh
                cat "{root}/ps.out"
                exit "$(cat "{self.ps_rc}" 2>/dev/null || echo 0)"
                """),
            "launchctl": textwrap.dedent(f"""\
                #!/bin/sh
                printf '%s\\n' "$*" >> "{self.log}"
                # Like launchd, a booted-out service stops being printable
                # (immediately here; see PoolOffWaitsForLaunchdTests for
                # the real, delayed release).
                if [ "$1" = bootout ]; then
                  base="{self.prints}/$(printf '%s' "$2" | tr '/' '_')"
                  if [ -f "$base.linger" ]; then touch "$base.exiting"; else rm -f "$base.rc"; fi
                fi
                if [ "$1" = print ]; then
                  base="{self.prints}/$(printf '%s' "$2" | tr '/' '_')"
                  if [ -f "$base.exiting" ]; then
                    left="$(cat "$base.linger")"
                    if [ "$left" -le 0 ]; then rm -f "$base.rc" "$base.exiting"
                    else echo $((left - 1)) > "$base.linger"; fi
                  fi
                  name=$(printf '%s' "$2" | tr '/' '_')
                  if [ -f "{self.prints}/$name.rc" ]; then
                    cat "{self.prints}/$name.out" 2>/dev/null
                    cat "{self.prints}/$name.err" >&2 2>/dev/null
                    exit "$(cat "{self.prints}/$name.rc")"
                  fi
                  echo "Could not find service \\"$2\\" in domain" >&2
                  exit 113
                fi
                exit 0
                """),
        }
        for name, body in stubs.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.loaded(IDLE_LANE, 200)
        self.loaded(LANE, 100 if busy else 200)
        self.participation = root / "participation"
        self.state = root / "state"
        self.participation.write_text("1\n")
        self.state.write_text("on\n")
        self.lock = root / "lock"
        self.env = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "TARTCI_POOL_STATE_FILE": str(self.state),
            "TARTCI_POOL_PARTICIPATION_FILE": str(self.participation),
            "TARTCI_POOL_TRANSITION_LOCK": str(self.lock),
            "TARTCI_POOL_PERSISTENT_HOLD_FILE": str(root / "hold"),
            "TARTCI_POOL_UID": str(os.getuid()),
            "TARTCI_MANUAL_CHANGE_LOG": str(root / "manual-changes.log"),
            "SSH_CONNECTION": "192.0.2.9 52344 192.0.2.5 22",
        }

    def _name(self, label: str) -> str:
        return f"gui/{os.getuid()}/{label}".replace("/", "_")

    def loaded(self, label: str, pid: int) -> None:
        (self.prints / f"{self._name(label)}.out").write_text(
            f"state = running\n\tpid = {pid}\n\texit timeout = 30\n")
        (self.prints / f"{self._name(label)}.rc").write_text("0")

    def linger(self, label: str, prints: int) -> None:
        """After bootout, keep `label` printable for `prints` more calls.

        This is launchd's real behaviour: bootout returns once the stop is
        SENT, and the service stays loaded while its TERM handler runs.
        """
        (self.prints / f"{self._name(label)}.linger").write_text(str(prints))

    def print_error(self, label: str) -> None:
        (self.prints / f"{self._name(label)}.out").unlink(missing_ok=True)
        (self.prints / f"{self._name(label)}.err").write_text("Input/output error\n")
        (self.prints / f"{self._name(label)}.rc").write_text("5")

    def pool(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ROOT / "tartci"), "pool", *args], cwd=ROOT,
                              env=self.env, text=True, capture_output=True, check=False)

    def mutations(self) -> list[str]:
        calls = self.log.read_text() if self.log.exists() else ""
        return [line for line in calls.splitlines() if line.split(" ", 1)[0] in MUTATIONS]

    def records(self) -> tuple[str, str]:
        return self.participation.read_text(), self.state.read_text()


class PoolOffWaitsForLaunchdTests(unittest.TestCase):
    """`pool off` returns only once launchd has let go of every lane.

    The ordering bug: self-update runs `pool off` and then the fleet installer,
    whose first check is `launchctl print` on every target lane. bootout had
    returned, but a lane still in its TERM cleanup was still loaded, so every
    host's first install was refused ("...forge-gate" on m5 and m1,
    "...vellum-gate" on m3) and only the retry applied.
    """

    def installer_check(self, host: FakePoolHost, label: str) -> subprocess.CompletedProcess[str]:
        # Exactly what install_macos_fleet.sh's assert_agent_unloaded asks.
        return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                              env=host.env, text=True, capture_output=True, check=False)

    def test_off_waits_out_a_lane_still_running_its_term_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td), busy=False)
            host.linger(IDLE_LANE, 6)
            proc = host.pool("off")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            check = self.installer_check(host, IDLE_LANE)
            self.assertNotEqual(check.returncode, 0, "installer would see the lane loaded")
            self.assertIn("Could not find service", check.stderr)
            prints = [line for line in host.log.read_text().splitlines()
                      if line == f"print gui/{os.getuid()}/{IDLE_LANE}"]
            # The wait really polled through the linger, not past it.
            self.assertGreaterEqual(len(prints), 7)
            self.assertEqual(host.records(), ("0\n", "off\n"))

    def test_a_lane_that_never_lets_go_fails_off_and_keeps_admission_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td), busy=False)
            host.linger(IDLE_LANE, 10 ** 6)
            host.env["TARTCI_POOL_UNLOAD_WAIT_SECS"] = "1"
            proc = host.pool("off")
            self.assertEqual(proc.returncode, 13, proc.stdout + proc.stderr)
            self.assertIn(IDLE_LANE, proc.stderr)
            self.assertNotIn(LANE + "\n", proc.stderr.replace(IDLE_LANE, ""))
            self.assertEqual(host.records(), ("0\n", "off\n"))
            # And the installer's refusal still stands for it: never install
            # while a lane is genuinely loaded.
            self.assertEqual(self.installer_check(host, IDLE_LANE).returncode, 0)
            body = (ROOT / "scripts" / "install_macos_fleet.sh").read_text()
            self.assertIn('assert_agent_unloaded "$label" target || exit 3', body)


class PoolOffMidJobTests(unittest.TestCase):
    def test_off_refuses_a_mid_job_lane_before_writing_anything(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td))
            proc = host.pool("off")
            self.assertEqual(proc.returncode, 12, proc.stdout + proc.stderr)
            self.assertIn("mid-job", proc.stderr)
            self.assertIn(LANE, proc.stderr)
            self.assertIn("tart run", proc.stderr)
            self.assertIn("tartci pool drain", proc.stderr)
            self.assertIn("--plan", proc.stderr)
            self.assertIn("--now", proc.stderr)
            self.assertEqual(host.records(), ("1\n", "on\n"))
            self.assertEqual(host.mutations(), [])

    def test_off_proceeds_when_every_owned_lane_is_idle(self) -> None:
        # Positive control: the same host with the VM gone stops immediately,
        # so the refusal above is the mid-job probe and not a broken fixture.
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td), busy=False)
            proc = host.pool("off")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"bootout gui/{os.getuid()}/{LANE}", host.mutations())
            self.assertEqual(host.records(), ("0\n", "off\n"))
            # The unowned runner is never probed into a refusal or stopped.
            self.assertNotIn(f"bootout gui/{os.getuid()}/{UNOWNED}", host.mutations())

    def test_now_is_the_explicit_emergency_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td))
            proc = host.pool("off", "--now")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"bootout gui/{os.getuid()}/{LANE}", host.mutations())
            self.assertEqual(host.records(), ("0\n", "off\n"))

    def test_unknown_busy_state_refuses_and_now_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td), busy=False)
            host.print_error(IDLE_LANE)
            proc = host.pool("off")
            self.assertEqual(proc.returncode, 12, proc.stdout + proc.stderr)
            self.assertIn("unknown", proc.stderr)
            self.assertEqual(host.records(), ("1\n", "on\n"))
            self.assertEqual(host.mutations(), [])
            self.assertEqual(host.pool("off", "--now").returncode, 0)

    def test_unreadable_process_table_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td), busy=False)
            host.ps_rc.write_text("1")
            proc = host.pool("off")
            self.assertEqual(proc.returncode, 12, proc.stdout + proc.stderr)
            self.assertEqual(host.mutations(), [])

    def test_mid_job_check_precedes_the_participation_write(self) -> None:
        lines = (ROOT / "tartci").read_text().splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("cmd_pool() {"))
        branch = next(i for i in range(start, len(lines))
                      if 'if [ "$sub" = off ]; then' in lines[i])
        check = next(i for i in range(branch, len(lines))
                     if "tartci_pool_mid_job" in lines[i])
        write = next(i for i in range(branch, len(lines))
                     if "tartci_pool_write_participation 0" in lines[i])
        self.assertLess(check, write)


class ManualChangeLogTests(unittest.TestCase):
    """Every pool change names its caller (m5, 2026-10-16 05:19Z: an off/on
    over ssh killed two jobs and nothing recorded who ran it)."""

    def rows(self, host: FakePoolHost) -> list[dict]:
        path = Path(host.env["TARTCI_MANUAL_CHANGE_LOG"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_refused_while_busy_is_logged_with_the_ssh_caller(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td))
            self.assertEqual(host.pool("off").returncode, 12)
            (row,) = self.rows(host)
            self.assertEqual((row["action"], row["via"], row["ssh_client"]),
                             ("pool off", "ssh", "192.0.2.9"))
            self.assertTrue(row["outcome"].startswith("refused"))
            self.assertTrue(row["parents"], "the parent command lines are recorded")
            self.assertEqual(host.mutations(), [])

    def test_allowed_when_idle_and_logged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td), busy=False)
            self.assertEqual(host.pool("off").returncode, 0)
            (row,) = self.rows(host)
            self.assertEqual(row["outcome"], "proceeding: every owned lane idle")

    def test_force_is_allowed_while_busy_and_logged_as_forced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td))
            proc = host.pool("off", "--force")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"bootout gui/{os.getuid()}/{LANE}", host.mutations())
            (row,) = self.rows(host)
            self.assertEqual(row["outcome"], "forced: mid-job check skipped")
            self.assertIn("--force", row["args"])


class PoolPlanTests(unittest.TestCase):
    def assert_nothing_written(self, host: FakePoolHost) -> None:
        self.assertEqual(host.records(), ("1\n", "on\n"))
        self.assertFalse(host.lock.exists(), "plan took the transition lock")
        self.assertEqual(host.mutations(), [])
        self.assertFalse((host.root / "nohup.log").exists(), "plan launched a watcher")

    def test_off_plan_names_what_it_would_kill_and_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td))
            proc = host.pool("off", "--plan")
            self.assertEqual(proc.returncode, 12, proc.stdout + proc.stderr)
            out = proc.stdout
            self.assertIn("nothing will be changed", out)
            self.assertIn("state: on -> off; participation: 1 -> 0", out)
            self.assertIn(LANE, out)
            self.assertIn(IDLE_LANE, out)
            self.assertIn(f"{LANE}: busy, would be KILLED", out)
            self.assertIn("not touched by pool off", out)
            self.assertIn(UNOWNED, out)
            self.assertIn("capacity floor: ok", out)
            self.assertIn("would REFUSE", out)
            self.assert_nothing_written(host)
            # It really probed: launchd was asked about the owned lanes.
            self.assertIn(f"print gui/{os.getuid()}/{LANE}", host.log.read_text())

    def test_off_now_plan_would_proceed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td))
            proc = host.pool("off", "--plan", "--now")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("would proceed", proc.stdout)
            self.assert_nothing_written(host)

    def test_drain_plan_lets_mid_job_lanes_finish(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td))
            proc = host.pool("drain", "--plan")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("state: on -> draining", proc.stdout)
            self.assertIn(f"{LANE}: busy, would finish its job", proc.stdout)
            self.assertIn("would proceed", proc.stdout)
            self.assert_nothing_written(host)

    def test_on_plan_lists_activation_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td))
            # Unmanaged host: activation is every runner agent on disk.
            (host.root / "home" / ".config" / "tartci" / "macos-fleet-install.json").unlink()
            for plist in host.agents.glob("com.danielraffel.tartci.*.plist"):
                plist.rename(host.agents / plist.name.replace(
                    "tartci.tart-runner-macos-fleet.studio", "pulp.tart-runner-macos"))
            host.participation.write_text("0\n")
            host.state.write_text("off\n")
            proc = host.pool("on", "--plan")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("state: off -> on; participation: 0 -> 1", proc.stdout)
            self.assertIn("not checked by --plan", proc.stdout)
            self.assertIn("launch-helper probe (exit 9)", proc.stdout)
            self.assertIn(UNOWNED, proc.stdout)
            self.assertEqual(host.records(), ("0\n", "off\n"))
            self.assertFalse(host.lock.exists())
            self.assertEqual(host.mutations(), [])

    def test_on_plan_refuses_an_invalid_receipt_like_on_would(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td))
            proc = host.pool("on", "--plan")
            self.assertEqual(proc.returncode, 7, proc.stdout + proc.stderr)
            self.assertIn("REFUSE", proc.stdout)
            self.assert_nothing_written(host)

    def test_plan_is_rejected_for_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td))
            self.assertEqual(host.pool("status", "--plan").returncode, 2)

    def test_usage_documents_plan_and_now(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = FakePoolHost(Path(td)).pool("--help")
            self.assertIn("--plan", proc.stdout)
            self.assertIn("--now", proc.stdout)



class PoolDrainLockTests(unittest.TestCase):
    """`pool drain` closes admission, then takes the transition lock that a
    supervisor holds while it mints a JIT runner. A mint in flight must not
    turn the drain into a host that is "on" with admission closed."""

    def hold_lock(self, host: FakePoolHost) -> None:
        host.lock.mkdir()
        (host.lock / "pid").write_text("99999\n")

    def test_drain_waits_for_an_in_flight_mint_to_release_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td), busy=False)
            self.hold_lock(host)
            releaser = subprocess.Popen(
                ["/bin/sh", "-c", f"sleep 12; rm -rf '{host.lock}'"])
            try:
                proc = host.pool("drain")
            finally:
                releaser.wait(timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(host.records(), ("0\n", "draining\n"))

    def test_drain_that_never_gets_the_lock_restores_participation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td), busy=False)
            host.env["TARTCI_POOL_DRAIN_LOCK_WAIT_SECS"] = "1"
            self.hold_lock(host)
            proc = host.pool("drain")
            self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
            self.assertIn("pool transition busy", proc.stderr)
            self.assertEqual(host.records(), ("1\n", "on\n"))
            self.assertEqual(host.mutations(), [])

    def test_restore_keeps_a_host_that_was_already_opted_out(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakePoolHost(Path(td), busy=False)
            host.env["TARTCI_POOL_DRAIN_LOCK_WAIT_SECS"] = "1"
            host.participation.write_text("0\n")
            self.hold_lock(host)
            self.assertEqual(host.pool("drain").returncode, 4)
            self.assertEqual(host.records(), ("0\n", "on\n"))


if __name__ == "__main__":
    unittest.main()
