#!/usr/bin/env python3
"""Tests for the host-level pool opt-out helpers (providers/common/pool.lib.sh)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers" / "common" / "pool.lib.sh"


def _bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


class ParticipationTests(unittest.TestCase):
    def test_absent_file_defaults_to_participating(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "nope"
            proc = _bash(f"source {LIB}; tartci_pool_read_participation {f}")
            self.assertEqual(proc.stdout.strip(), "1", proc.stderr)

    def test_explicit_zero_is_opted_out(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "p"
            f.write_text("0\n")
            proc = _bash(f"source {LIB}; tartci_pool_read_participation {f}")
            self.assertEqual(proc.stdout.strip(), "0", proc.stderr)

    def test_garbage_defaults_to_participating(self) -> None:
        # A corrupt/unexpected value must never silently pull a host out.
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "p"
            f.write_text("banana\n")
            proc = _bash(f"source {LIB}; tartci_pool_read_participation {f}")
            self.assertEqual(proc.stdout.strip(), "1", proc.stderr)

    def test_write_then_read_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "sub" / "p"  # exercises mkdir -p
            proc = _bash(
                f"source {LIB}; tartci_pool_write_participation 0 {f}; "
                f"tartci_pool_read_participation {f}"
            )
            self.assertEqual(proc.stdout.strip(), "0", proc.stderr)
            self.assertEqual(Path(f).read_text().strip(), "0")


class AdmissionStateTests(unittest.TestCase):
    def test_state_survives_new_shell_and_on_reopens_admission(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "pool-state"
            participation = Path(td) / "participation"
            env = {
                "TARTCI_POOL_STATE_FILE": str(state),
                "TARTCI_POOL_PARTICIPATION_FILE": str(participation),
            }
            first = _bash(
                f"source {LIB}; tartci_pool_write_state draining; "
                "tartci_pool_write_participation 0; tartci_pool_read_state",
                env,
            )
            self.assertEqual(first.stdout.strip(), "draining", first.stderr)
            rejoined = _bash(
                f"source {LIB}; tartci_pool_read_state; "
                "tartci_pool_admission_open; echo open=$?",
                env,
            )
            self.assertEqual(rejoined.stdout.splitlines(), ["draining", "open=1"])
            reopened = _bash(
                f"source {LIB}; tartci_pool_write_state on; "
                "tartci_pool_write_participation 1; tartci_pool_admission_open; "
                "echo open=$?",
                env,
            )
            self.assertEqual(reopened.stdout.strip(), "open=0", reopened.stderr)

    def test_legacy_participation_zero_infers_off(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            participation = Path(td) / "participation"
            participation.write_text("0\n")
            proc = _bash(
                f"source {LIB}; tartci_pool_read_state",
                {
                    "TARTCI_POOL_STATE_FILE": str(Path(td) / "absent"),
                    "TARTCI_POOL_PARTICIPATION_FILE": str(participation),
                },
            )
            self.assertEqual(proc.stdout.strip(), "off", proc.stderr)

    def test_default_state_is_on_so_missing_state_cannot_wedge_host(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = _bash(
                f"source {LIB}; tartci_pool_read_state; "
                "tartci_pool_admission_open; echo open=$?",
                {
                    "TARTCI_POOL_STATE_FILE": str(Path(td) / "absent-state"),
                    "TARTCI_POOL_PARTICIPATION_FILE": str(Path(td) / "absent-part"),
                },
            )
            self.assertEqual(proc.stdout.splitlines(), ["on", "open=0"])

    def test_participation_zero_closes_provider_admission_before_state_flip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            participation = Path(td) / "participation"
            state.write_text("on\n")
            participation.write_text("0\n")
            proc = _bash(
                f"source {LIB}; tartci_pool_admission_open; echo open=$?",
                {
                    "TARTCI_POOL_STATE_FILE": str(state),
                    "TARTCI_POOL_PARTICIPATION_FILE": str(participation),
                },
            )
            self.assertEqual(proc.stdout.strip(), "open=1", proc.stderr)


class RunnerAgentEnumerationTests(unittest.TestCase):
    def _fixture_dir(self, td: str) -> Path:
        d = Path(td) / "LaunchAgents"
        d.mkdir()
        # active runner agents (should match) — incl. the bare `tart-runner`
        # (the macOS GATE lane), which MUST be covered or `pool off` leaves the
        # gate serving.
        for name in (
            "com.danielraffel.pulp.tart-runner.plist",
            "com.danielraffel.pulp.tart-runner-linux.plist",
            "com.danielraffel.pulp.qemu-runner-windows.plist",
            "com.danielraffel.tartci.tart-runner-macos-fleet.m1.forge-gate.plist",
            "actions.runner.danielraffel-pulp.pulp-preamble-m5.plist",
        ):
            (d / name).write_text("<plist/>")
        # non-runner or inactive (should NOT match)
        for name in (
            "com.danielraffel.pulp.queue-saturation.plist",  # not a runner
            "com.danielraffel.pulp.tart-runner-linux.plist.pre-engage.bak",  # .bak
            "com.danielraffel.pulp.tart-runner-macos.plist.disabled-20260611",  # disabled
            "com.apple.something.plist",  # unrelated
        ):
            (d / name).write_text("<plist/>")
        return d

    def test_enumerates_only_active_runner_agents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = self._fixture_dir(td)
            proc = _bash(f"source {LIB}; tartci_pool_runner_agents {d} | sort")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            got = sorted(l for l in proc.stdout.splitlines() if l.strip())
            self.assertEqual(
                got,
                [
                    "actions.runner.danielraffel-pulp.pulp-preamble-m5",
                    "com.danielraffel.pulp.qemu-runner-windows",
                    "com.danielraffel.pulp.tart-runner",
                    "com.danielraffel.pulp.tart-runner-linux",
                    "com.danielraffel.tartci.tart-runner-macos-fleet.m1.forge-gate",
                ],
            )

    def test_missing_dir_is_empty_not_error(self) -> None:
        proc = _bash(f"source {LIB}; tartci_pool_runner_agents /no/such/dir; echo rc=$?")
        self.assertIn("rc=0", proc.stdout)
        self.assertEqual([l for l in proc.stdout.splitlines() if l != "rc=0"], [])


class PersistentRunnerDrainTests(unittest.TestCase):
    def _fake_path(self, td: str) -> tuple[Path, Path]:
        bindir = Path(td) / "bin"
        bindir.mkdir()
        log = Path(td) / "launchctl.log"
        (bindir / "launchctl").write_text(
            "#!/bin/sh\n"
            'echo "$*" >> "$FAKE_LAUNCHCTL_LOG"\n'
            '[ "$1" = print ] && exit 1\n'
            "exit 0\n"
        )
        for path in bindir.iterdir():
            path.chmod(0o755)
        return bindir, log

    def test_active_worker_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "agents"
            agents.mkdir()
            (agents / "actions.runner.owner.repo.preamble.plist").write_text("<plist/>")
            bindir, log = self._fake_path(td)
            state = Path(td) / "state"
            state.write_text("draining\n")
            hold = Path(td) / "hold"
            hold.write_text("held-idle\n")
            proc = _bash(
                f"source {LIB}; "
                "tartci_pool_agent_pid() { echo 42; }; "
                "tartci_pool_pid_tree_has_worker() { return 0; }; "
                f"tartci_pool_quiesce_persistent_agents {agents}; echo rc=$?",
                {
                    "PATH": f"{bindir}:{os.environ['PATH']}",
                    "FAKE_LAUNCHCTL_LOG": str(log),
                    "TARTCI_POOL_STATE_FILE": str(state),
                    "TARTCI_POOL_PERSISTENT_HOLD_FILE": str(hold),
                    "TARTCI_POOL_TRANSITION_LOCK": str(Path(td) / "lock"),
                },
            )
            self.assertEqual(proc.stdout.strip(), "rc=1", proc.stderr)
            self.assertNotIn("bootout", log.read_text() if log.exists() else "")

    def test_idle_persistent_runner_is_booted_out(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "agents"
            agents.mkdir()
            label = "actions.runner.owner.repo.preamble"
            (agents / f"{label}.plist").write_text("<plist/>")
            bindir, log = self._fake_path(td)
            state = Path(td) / "state"
            state.write_text("draining\n")
            hold = Path(td) / "hold"
            hold.write_text("held-idle\n")
            proc = _bash(
                f"source {LIB}; "
                "tartci_pool_agent_pid() { echo 42; }; "
                "tartci_pool_pid_tree_has_worker() { return 1; }; "
                f"tartci_pool_quiesce_persistent_agents {agents}; echo rc=$?",
                {
                    "PATH": f"{bindir}:{os.environ['PATH']}",
                    "FAKE_LAUNCHCTL_LOG": str(log),
                    "TARTCI_POOL_STATE_FILE": str(state),
                    "TARTCI_POOL_PERSISTENT_HOLD_FILE": str(hold),
                    "TARTCI_POOL_TRANSITION_LOCK": str(Path(td) / "lock"),
                },
            )
            self.assertEqual(proc.stdout.strip(), "rc=0", proc.stderr)
            self.assertIn(f"bootout gui/", log.read_text())
            self.assertIn(label, log.read_text())

    def test_idle_runner_quiesces_while_sibling_worker_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "agents"
            agents.mkdir()
            busy = "actions.runner.owner.repo.busy"
            idle = "actions.runner.owner.repo.idle"
            for label in (busy, idle):
                (agents / f"{label}.plist").write_text("<plist/>")
            bindir, log = self._fake_path(td)
            state = Path(td) / "state"
            state.write_text("draining\n")
            hold = Path(td) / "hold"
            hold.write_text("held-idle\n")
            proc = _bash(
                f"source {LIB}; "
                f'tartci_pool_agent_pid() {{ case "$1" in *busy) echo 41;; *) echo 42;; esac; }}; '
                'tartci_pool_pid_tree_has_worker() { [ "$1" = 41 ]; }; '
                f"tartci_pool_quiesce_persistent_agents {agents}; echo rc=$?",
                {
                    "PATH": f"{bindir}:{os.environ['PATH']}",
                    "FAKE_LAUNCHCTL_LOG": str(log),
                    "TARTCI_POOL_STATE_FILE": str(state),
                    "TARTCI_POOL_PERSISTENT_HOLD_FILE": str(hold),
                    "TARTCI_POOL_TRANSITION_LOCK": str(Path(td) / "lock"),
                },
            )
            self.assertEqual(proc.stdout.strip(), "rc=1", proc.stderr)
            calls = log.read_text()
            self.assertIn(idle, calls)
            self.assertNotIn(f"bootout gui/{os.getuid()}/{busy}", calls)

    def test_non_draining_state_supersedes_watcher_before_bootout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "agents"
            agents.mkdir()
            label = "actions.runner.owner.repo.preamble"
            (agents / f"{label}.plist").write_text("<plist/>")
            bindir, log = self._fake_path(td)
            state = Path(td) / "state"
            state.write_text("on\n")
            hold = Path(td) / "hold"
            hold.write_text("held-idle\n")
            proc = _bash(
                f"source {LIB}; "
                "tartci_pool_agent_pid() { echo 42; }; "
                "tartci_pool_pid_tree_has_worker() { return 1; }; "
                f"tartci_pool_quiesce_persistent_agents {agents}; echo rc=$?",
                {
                    "PATH": f"{bindir}:{os.environ['PATH']}",
                    "FAKE_LAUNCHCTL_LOG": str(log),
                    "TARTCI_POOL_PERSISTENT_HOLD_FILE": str(hold),
                    "TARTCI_POOL_TRANSITION_LOCK": str(Path(td) / "lock"),
                    "TARTCI_POOL_STATE_FILE": str(state),
                },
            )
            self.assertEqual(proc.stdout.strip(), "rc=1", proc.stderr)
            self.assertNotIn("bootout", log.read_text() if log.exists() else "")

    def test_failed_bootout_keeps_watcher_pending_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "agents"
            agents.mkdir()
            label = "actions.runner.owner.repo.preamble"
            (agents / f"{label}.plist").write_text("<plist/>")
            state = Path(td) / "state"
            state.write_text("draining\n")
            hold = Path(td) / "hold"
            hold.write_text("held-idle\n")
            calls = Path(td) / "calls"
            proc = _bash(
                f"source {LIB}; "
                "tartci_pool_agent_pid() { echo 42; }; "
                "tartci_pool_pid_tree_has_worker() { return 1; }; "
                f'launchctl() {{ echo "$*" >> {calls}; return 0; }}; '
                f"tartci_pool_quiesce_persistent_agents {agents}; echo rc=$?",
                {
                    "TARTCI_POOL_STATE_FILE": str(state),
                    "TARTCI_POOL_PERSISTENT_HOLD_FILE": str(hold),
                    "TARTCI_POOL_TRANSITION_LOCK": str(Path(td) / "lock"),
                },
            )
            self.assertEqual(proc.stdout.strip(), "rc=1", proc.stderr)
            self.assertIn("bootout", calls.read_text())

    def test_persistent_runner_requires_authoritative_hold_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "agents"
            agents.mkdir()
            label = "actions.runner.owner.repo.preamble"
            (agents / f"{label}.plist").write_text("<plist/>")
            bindir, log = self._fake_path(td)
            proc = _bash(
                f"source {LIB}; tartci_pool_quiesce_persistent_agents {agents}; echo rc=$?",
                {
                    "PATH": f"{bindir}:{os.environ['PATH']}",
                    "FAKE_LAUNCHCTL_LOG": str(log),
                    "TARTCI_POOL_PERSISTENT_HOLD_FILE": str(Path(td) / "absent"),
                    "TARTCI_POOL_TRANSITION_LOCK": str(Path(td) / "lock"),
                },
            )
            self.assertEqual(proc.stdout.strip(), "rc=1", proc.stderr)
            self.assertNotIn("bootout", log.read_text() if log.exists() else "")

    def test_provider_only_host_needs_no_hold_or_watcher_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "agents"
            agents.mkdir()
            proc = _bash(
                f"source {LIB}; tartci_pool_quiesce_persistent_agents {agents}; echo rc=$?",
                {
                    "TARTCI_POOL_PERSISTENT_HOLD_FILE": str(Path(td) / "absent"),
                    "TARTCI_POOL_TRANSITION_LOCK": str(Path(td) / "lock"),
                },
            )
            self.assertEqual(proc.stdout.strip(), "rc=0", proc.stderr)


class ProviderAdmissionContractTests(unittest.TestCase):
    def test_every_provider_gates_immediately_before_jit_mint(self) -> None:
        for relative in (
            "providers/tart-macos/runner.sh",
            "providers/tart-linux/runner.sh",
            "providers/qemu-windows/runner.sh",
        ):
            with self.subTest(provider=relative):
                source = (ROOT / relative).read_text()
                # The last occurrence is the executable POST (headers may name it).
                mint = source.rindex("generate-jitconfig")
                gate = source.rfind("tartci_pool_admission_open", 0, mint)
                self.assertGreater(gate, mint - 1200, f"late/missing admission gate in {relative}")
                lock = source.rfind("tartci_pool_lock_acquire", 0, gate)
                release = source.find("tartci_pool_lock_release", mint)
                self.assertGreater(lock, mint - 1600, f"missing mint lock in {relative}")
                self.assertGreater(release, mint, f"mint lock not released in {relative}")

    def test_drain_closes_shared_participation_before_state_transition(self) -> None:
        source = (ROOT / "tartci").read_text()
        drain = source.index("    drain)")
        watcher = source.index("    _drain-watch)", drain)
        body = source[drain:watcher]
        self.assertLess(
            body.index("tartci_pool_write_participation 0"),
            body.index("tartci_pool_write_state draining"),
        )

    def test_pool_on_invalidates_old_persistent_hold_receipt(self) -> None:
        source = (ROOT / "tartci").read_text()
        on_off = source.index("    on|off)")
        drain = source.index("    drain)", on_off)
        body = source[on_off:drain]
        self.assertIn('rm -f "$TARTCI_POOL_PERSISTENT_HOLD_FILE"', body)

    def test_emergency_off_bypasses_cooperative_transition_lock(self) -> None:
        source = (ROOT / "tartci").read_text()
        on_off = source.index("    on|off)")
        drain = source.index("    drain)", on_off)
        body = source[on_off:drain]
        off = body.index('if [ "$sub" = off ]')
        acquire = body.index("tartci_pool_lock_acquire")
        self.assertGreater(acquire, off)
        self.assertIn("Emergency stop deliberately bypasses", body)

    def test_orphan_lock_repair_is_fail_closed(self) -> None:
        source = (ROOT / "tartci").read_text()
        repair = source.index("    repair-lock)")
        status = source.index("    status)", repair)
        body = source[repair:status]
        self.assertIn('tartci_pool_read_participation)" = 0', body)
        self.assertIn('tartci_pool_read_state)" != on', body)
        self.assertIn('kill -0 "$owner"', body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
