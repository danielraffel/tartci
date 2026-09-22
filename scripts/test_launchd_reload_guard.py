#!/usr/bin/env python3
"""`tartci launchd reload`: per-lane mid-job refusal and an honest --dry-run.

Every test runs against stubbed `launchctl` and `ps` on PATH and a temporary
LaunchAgents directory. Nothing here reaches the real launchd domain.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lane_busy

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "scripts" / "tartci_launchd_watchdog.py"
LANE = "com.danielraffel.tartci.tart-runner-macos-fleet.studio.pulp-gate"
SIBLING = "com.danielraffel.tartci.tart-runner-macos-fleet.studio.forge-gate"
MUTATIONS = ("bootout", "bootstrap", "kickstart", "disable", "enable")

LOADED = textwrap.dedent("""\
    gui/501/{label} = {{
    \tstate = running
    \tpid = {pid}
    \texit timeout = 30
    }}
    """)

PS_TREE = textwrap.dedent("""\
      PID  PPID COMMAND
      100     1 /bin/bash /Users/x/.local/bin/tartci serve macos --loop
      101   100 /bin/bash /Users/x/tartci/providers/tart-macos/runner.sh --loop
      102   101 /opt/homebrew/bin/tart run --no-graphics pulp-studio-1
      200     1 /bin/bash /Users/x/.local/bin/tartci serve macos --loop
      201   200 sleep 20
      300     1 /Users/x/actions-runner/bin/Runner.Listener run
      301   300 /Users/x/actions-runner/bin/Runner.Worker spawnclient 1 2
      400     1 /bin/bash /Users/x/tartci/providers/qemu-windows/runner.sh --loop
      401   400 /opt/homebrew/bin/qemu-system-aarch64 -M virt -accel hvf -drive file=w.qcow2
    """)


class FakeHost:
    """launchctl/ps stubs whose answers are files the test controls."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.agents = root / "LaunchAgents"
        self.agents.mkdir()
        self.log = root / "launchctl.log"
        self.prints = root / "prints"
        self.prints.mkdir()
        self.ps_out = root / "ps.out"
        self.ps_rc = root / "ps.rc"
        (self.bin / "launchctl").write_text(textwrap.dedent(f"""\
            #!/bin/sh
            printf '%s\\n' "$*" >> "{self.log}"
            if [ "$1" = print ]; then
              name=$(printf '%s' "$2" | tr '/' '_')
              if [ -f "{self.prints}/$name.out" ]; then
                cat "{self.prints}/$name.out"
                exit "$(cat "{self.prints}/$name.rc" 2>/dev/null || echo 0)"
              fi
              if [ -f "{self.prints}/$name.err" ]; then
                cat "{self.prints}/$name.err" >&2
                exit "$(cat "{self.prints}/$name.rc")"
              fi
              echo "Could not find service \\"$2\\" in domain" >&2
              exit 113
            fi
            if [ "$1" = bootout ]; then
              name=$(printf '%s' "$2" | tr '/' '_')
              rm -f "{self.prints}/$name.out" "{self.prints}/$name.rc"
            fi
            if [ "$1" = bootstrap ]; then
              label=$(basename "$3" .plist)
              name=$(printf '%s/%s' "$2" "$label" | tr '/' '_')
              printf 'state = waiting\\n\\texit timeout = 30\\n' > "{self.prints}/$name.out"
            fi
            exit 0
            """))
        (self.bin / "ps").write_text(textwrap.dedent(f"""\
            #!/bin/sh
            cat "{self.ps_out}" 2>/dev/null
            exit "$(cat "{self.ps_rc}" 2>/dev/null || echo 0)"
            """))
        for tool in self.bin.iterdir():
            tool.chmod(0o755)
        self.ps_out.write_text(PS_TREE)

    def plist(self, label: str) -> None:
        (self.agents / f"{label}.plist").write_text("<plist/>")

    def loaded(self, label: str, pid: int) -> None:
        name = f"gui/{os.getuid()}/{label}".replace("/", "_")
        (self.prints / f"{name}.out").write_text(LOADED.format(label=label, pid=pid))
        (self.prints / f"{name}.rc").write_text("0")

    def print_error(self, label: str, rc: int, message: str) -> None:
        name = f"gui/{os.getuid()}/{label}".replace("/", "_")
        (self.prints / f"{name}.err").write_text(message)
        (self.prints / f"{name}.rc").write_text(str(rc))

    def env(self) -> dict[str, str]:
        return {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                "TARTCI_POOL_UID": str(os.getuid())}

    def reload(self, label: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(WATCHDOG), "--reload", label,
             "--launch-agents-dir", str(self.agents), *extra],
            env=self.env(), text=True, capture_output=True, check=False)

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def mutated(self) -> list[str]:
        return [line for line in self.calls().splitlines()
                if line.split(" ", 1)[0] in MUTATIONS]


class LaneBusyProbeTests(unittest.TestCase):
    def _probe(self, host: FakeHost, *labels: str) -> list[lane_busy.LaneBusy]:
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{host.bin}:{old}"
        try:
            return lane_busy.probe(list(labels))
        finally:
            os.environ["PATH"] = old

    def test_tart_run_descendant_is_busy_and_idle_sibling_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            host.loaded(LANE, 100)
            host.loaded(SIBLING, 200)
            busy, idle = self._probe(host, LANE, SIBLING)
            self.assertEqual(busy.state, lane_busy.BUSY)
            self.assertEqual((busy.worker_pid, busy.worker_kind), (102, "tart run"))
            # Control: same instrument, same tree, a lane with no VM is idle.
            self.assertEqual(idle.state, lane_busy.IDLE)

    def test_runner_worker_descendant_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            label = "actions.runner.Generous-Corp-pulp.preamble"
            host.loaded(label, 300)
            (row,) = self._probe(host, label)
            self.assertEqual((row.state, row.worker_kind), (lane_busy.BUSY, "Runner.Worker"))

    def test_qemu_windows_vm_descendant_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            label = "com.danielraffel.pulp.qemu-runner-windows"
            host.loaded(label, 400)
            host.loaded(SIBLING, 200)
            busy, idle = self._probe(host, label, SIBLING)
            self.assertEqual((busy.state, busy.worker_kind, busy.worker_pid),
                             (lane_busy.BUSY, "qemu-system", 401))
            self.assertEqual(idle.state, lane_busy.IDLE)
            table = {1: (0, "sup"), 2: (1, "qemu-img convert a b"), 3: (1, "grep qemu-system")}
            self.assertIsNone(lane_busy.find_worker(1, table))

    def test_absent_is_absent_and_other_print_errors_are_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            host.print_error(SIBLING, 5, "Input/output error")
            absent, unknown = self._probe(host, LANE, SIBLING)
            self.assertEqual(absent.state, lane_busy.ABSENT)
            self.assertEqual(unknown.state, lane_busy.UNKNOWN)

    def test_unreadable_process_table_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            host.loaded(LANE, 200)
            host.ps_rc.write_text("1")
            (row,) = self._probe(host, LANE)
            self.assertEqual(row.state, lane_busy.UNKNOWN)

    def test_word_boundaries(self) -> None:
        table = {10: (1, "sup"), 11: (10, "/usr/bin/start runner"),
                 12: (10, "grep tart running"), 13: (10, "vim Runner.Worker.cs")}
        self.assertIsNone(lane_busy.find_worker(10, table))
        table[14] = (13, "tart run vm")
        self.assertEqual(lane_busy.find_worker(10, table)[0], 14)


class ReloadGuardTests(unittest.TestCase):
    def test_reload_refuses_a_mid_job_lane_and_names_the_way_out(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            host.plist(LANE)
            host.loaded(LANE, 100)
            proc = host.reload(LANE)
            self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
            self.assertIn(f"lane {LANE} is mid-job", proc.stderr)
            self.assertIn("tart run", proc.stderr)
            self.assertIn("pid 102", proc.stderr)
            self.assertIn("tartci pool drain", proc.stderr)
            self.assertIn("--allow-mid-job", proc.stderr)
            self.assertEqual(host.mutated(), [])

    def test_idle_lane_reloads_while_a_sibling_lane_builds(self) -> None:
        # The positive control for the refusal above, and the reason the probe
        # is per label: a host-wide VM check would refuse this reload.
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            host.plist(SIBLING)
            host.loaded(SIBLING, 200)
            host.loaded(LANE, 100)
            proc = host.reload(SIBLING)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"bootout gui/{os.getuid()}/{SIBLING}", host.calls())
            self.assertIn("bootstrap", host.calls())

    def test_allow_mid_job_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            host.plist(LANE)
            host.loaded(LANE, 100)
            proc = host.reload(LANE, "--allow-mid-job")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"bootout gui/{os.getuid()}/{LANE}", host.calls())

    def test_unknown_busy_state_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            host.plist(LANE)
            host.loaded(LANE, 200)
            host.ps_rc.write_text("1")
            proc = host.reload(LANE)
            self.assertEqual(proc.returncode, 3)
            self.assertIn("unknown", proc.stderr)
            self.assertEqual(host.mutated(), [])
            host.print_error(SIBLING, 5, "Input/output error")
            host.plist(SIBLING)
            proc = host.reload(SIBLING)
            self.assertEqual(proc.returncode, 3)
            self.assertEqual(host.mutated(), [])


class ReloadDryRunTests(unittest.TestCase):
    def test_dry_run_prints_the_plan_and_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            host.plist(SIBLING)
            host.loaded(SIBLING, 200)
            proc = host.reload(SIBLING, "--dry-run")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"would bootout gui/{os.getuid()}/{SIBLING}", proc.stdout)
            self.assertIn(f"would bootstrap gui/{os.getuid()} ", proc.stdout)
            self.assertIn(f"would kickstart -k gui/{os.getuid()}/{SIBLING}", proc.stdout)
            self.assertEqual(host.mutated(), [])
            # It checked something: the preconditions consulted launchd.
            self.assertIn(f"print gui/{os.getuid()}/{SIBLING}", host.calls())

    def test_dry_run_of_an_absent_lane_plans_bootstrap_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            host.plist(LANE)
            proc = host.reload(LANE, "--dry-run")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("would bootout", proc.stdout)
            self.assertIn("would bootstrap", proc.stdout)

    def test_dry_run_reports_every_refusal_with_a_distinct_exit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            host.plist(LANE)
            host.loaded(LANE, 100)
            busy = host.reload(LANE, "--dry-run")
            self.assertEqual(busy.returncode, 3)
            self.assertIn("REFUSE: lane", busy.stderr)
            missing = host.reload("com.danielraffel.tartci.nope", "--dry-run")
            self.assertEqual(missing.returncode, 3)
            self.assertIn("REFUSE: no plist", missing.stderr)
            # A loaded service with no finite ExitTimeOut refuses the same way.
            host.plist(SIBLING)
            name = f"gui/{os.getuid()}/{SIBLING}".replace("/", "_")
            (host.prints / f"{name}.out").write_text("state = waiting\n")
            (host.prints / f"{name}.rc").write_text("0")
            no_timeout = host.reload(SIBLING, "--dry-run")
            self.assertEqual(no_timeout.returncode, 3)
            self.assertIn("ExitTimeOut", no_timeout.stderr)
            self.assertEqual(host.mutated(), [])

    def test_dispatcher_passes_reload_flags_through(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host = FakeHost(Path(td))
            home = Path(td) / "home"
            (home / "Library").mkdir(parents=True)
            (home / "Library" / "LaunchAgents").symlink_to(host.agents)
            host.plist(LANE)
            host.loaded(LANE, 100)
            proc = subprocess.run(
                [str(ROOT / "tartci"), "launchd", "reload", LANE, "--dry-run"],
                env={**host.env(), "HOME": str(home)},
                text=True, capture_output=True, check=False)
            self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
            self.assertIn("mid-job", proc.stderr)


if __name__ == "__main__":
    unittest.main()
