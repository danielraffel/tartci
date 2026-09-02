#!/usr/bin/env python3
"""Tests for the host-level pool opt-out helpers (providers/common/pool.lib.sh)."""

from __future__ import annotations

import os
import json
import plistlib
import shlex
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


class TransitionLockHandoffTests(unittest.TestCase):
    def test_live_listener_handoff_releases_global_transition_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "transition.lock"
            proc = _bash(
                f"source {LIB}; "
                "tartci_pool_lock_acquire; "
                "sleep 30 & listener=$!; "
                "tartci_pool_lock_handoff_to_listener \"$listener\"; handoff=$?; "
                "kill -0 \"$listener\" && alive=yes || alive=no; "
                "tartci_pool_lock_acquire; reacquire=$?; tartci_pool_lock_release; "
                "kill \"$listener\"; wait \"$listener\" 2>/dev/null || true; "
                f"[ -d {lock} ] && held=yes || held=no; "
                "echo handoff=$handoff alive=$alive reacquire=$reacquire held=$held",
                {"TARTCI_POOL_TRANSITION_LOCK": str(lock)},
            )
            self.assertEqual(
                proc.stdout.strip(),
                "handoff=0 alive=yes reacquire=0 held=no",
                proc.stderr,
            )

    def test_dead_listener_keeps_transition_lock_owned_for_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "transition.lock"
            proc = _bash(
                f"source {LIB}; "
                "tartci_pool_lock_acquire; "
                "(exit 0) & listener=$!; wait \"$listener\"; "
                "tartci_pool_lock_handoff_to_listener \"$listener\"; rc=$?; "
                f"[ -d {lock} ] && held=yes || held=no; "
                "tartci_pool_lock_release; echo rc=$rc held=$held",
                {"TARTCI_POOL_TRANSITION_LOCK": str(lock)},
            )
            self.assertEqual(proc.stdout.strip(), "rc=1 held=yes", proc.stderr)

    def test_listener_cannot_handoff_an_unowned_transition_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "transition.lock"
            lock.mkdir()
            (lock / "pid").write_text("999999\n")
            proc = _bash(
                f"source {LIB}; "
                "sleep 30 & listener=$!; "
                "tartci_pool_lock_handoff_to_listener \"$listener\"; rc=$?; "
                "kill \"$listener\"; wait \"$listener\" 2>/dev/null || true; "
                f"[ -d {lock} ] && held=yes || held=no; "
                "echo rc=$rc held=$held",
                {"TARTCI_POOL_TRANSITION_LOCK": str(lock)},
            )
            self.assertEqual(proc.stdout.strip(), "rc=1 held=yes", proc.stderr)


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
            "com.danielraffel.pulp.tart-runner-macos-gate-slot2.plist",
            "com.danielraffel.pulp.qemu-runner-windows.plist",
            "com.danielraffel.forge.tart-runner-macos.plist",
            "com.danielraffel.vellum.tart-runner-macos.plist",
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
                    "com.danielraffel.forge.tart-runner-macos",
                    "com.danielraffel.pulp.qemu-runner-windows",
                    "com.danielraffel.pulp.tart-runner",
                    "com.danielraffel.pulp.tart-runner-linux",
                    "com.danielraffel.pulp.tart-runner-macos-gate-slot2",
                    "com.danielraffel.tartci.tart-runner-macos-fleet.m1.forge-gate",
                    "com.danielraffel.vellum.tart-runner-macos",
                ],
            )

    def test_missing_dir_is_empty_not_error(self) -> None:
        proc = _bash(f"source {LIB}; tartci_pool_runner_agents /no/such/dir; echo rc=$?")
        self.assertIn("rc=0", proc.stdout)
        self.assertEqual([l for l in proc.stdout.splitlines() if l != "rc=0"], [])

class RunnerAgentLoadedTests(unittest.TestCase):
    def test_toml_selector_uses_capable_unversioned_python3(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bindir = Path(td) / "bin"
            bindir.mkdir()
            fake = bindir / "python3"
            fake.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = -c ]; then exit 0; fi\n"
                "printf '%s\\n' \"$1\"\n"
            )
            fake.chmod(0o755)
            for versioned in ("python3.11", "python3.12"):
                unavailable = bindir / versioned
                unavailable.write_text("#!/bin/sh\nexit 1\n")
                unavailable.chmod(0o755)
            proc = _bash(
                f"source {shlex.quote(str(ROOT / 'tartci'))} >/dev/null; "
                "tartci_toml_python selected-marker",
                # Shadow host-provided versioned interpreters so this fixture
                # proves the unversioned-python3 branch rather than depending
                # on the runner image.
                {"PATH": f"{bindir}:/bin:/usr/bin", "TARTCI_PYTHON": ""},
            )
            self.assertEqual(proc.stdout.strip(), "selected-marker", proc.stderr)

    def test_toml_helpers_use_supported_python_selection(self) -> None:
        source = (ROOT / "tartci").read_text()
        self.assertIn("tartci_toml_python()", source)
        self.assertIn("TARTCI_PYTHON", source)
        self.assertIn("/opt/homebrew/bin/python3.11", source)
        self.assertNotIn(
            'exec python3 "$HERE/scripts/macos_fleet_lanes.py"', source
        )

    def _fake_launchctl(self, td: str) -> Path:
        bindir = Path(td) / "bin"
        bindir.mkdir()
        launchctl = bindir / "launchctl"
        launchctl.write_text(
            "#!/usr/bin/env python3\n"
            "import signal\n"
            "import sys\n"
            "signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
            "label = 'com.example.loaded'\n"
            "if sys.argv[1] == 'print':\n"
            "    raise SystemExit(0 if sys.argv[2] == f'gui/501/{label}' else 3)\n"
            "if sys.argv[1] == 'list':\n"
            "    print(f'42\\t0\\t{label}')\n"
            "    for index in range(100000):\n"
            "        print(f'{index}\\t0\\tcom.example.tail.{index}')\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(2)\n"
        )
        launchctl.chmod(0o755)
        return bindir

    def test_loaded_probe_uses_exact_launchd_domain_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bindir = self._fake_launchctl(td)
            proc = _bash(
                f"source {LIB}; "
                "tartci_pool_agent_loaded com.example.loaded; echo loaded=$?; "
                "tartci_pool_agent_loaded com.example.absent; echo absent=$?",
                {
                    "PATH": f"{bindir}:{os.environ['PATH']}",
                    "TARTCI_POOL_UID": "501",
                },
            )
            self.assertEqual(proc.stdout.splitlines(), ["loaded=0", "absent=3"], proc.stderr)

    def test_loaded_probe_avoids_early_match_pipefail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bindir = self._fake_launchctl(td)
            proc = _bash(
                f"source {LIB}; set -o pipefail; "
                "launchctl list | grep -qF com.example.loaded; legacy=$?; "
                "tartci_pool_agent_loaded com.example.loaded; current=$?; "
                "echo legacy=$legacy current=$current",
                {
                    "PATH": f"{bindir}:{os.environ['PATH']}",
                    "TARTCI_POOL_UID": "501",
                },
            )
            fields = dict(field.split("=") for field in proc.stdout.strip().split())
            self.assertNotEqual(fields["legacy"], "0", proc.stderr)
            self.assertEqual(fields["current"], "0", proc.stderr)

    def test_pool_status_uses_loaded_probe_for_both_output_modes(self) -> None:
        source = (ROOT / "tartci").read_text()
        status = source.index("    status)", source.index("cmd_pool()"))
        end = source.index("\n      ;;", status)
        body = source[status:end]
        self.assertEqual(body.count('tartci_pool_agent_loaded "$label"'), 2)
        self.assertNotIn("launchctl list", body)


class PoolCommandHelpTests(unittest.TestCase):
    def test_managed_plist_without_receipt_is_broken_and_require_ready_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            agents = home / "Library/LaunchAgents"
            agents.mkdir(parents=True)
            state = home / ".config/tartci"
            state.mkdir(parents=True)
            (state / "pool-state").write_text("on\n")
            (state / "native-build-participation").write_text("1\n")
            label = "com.danielraffel.tartci.tart-runner-macos-fleet.test.gate"
            with (agents / f"{label}.plist").open("wb") as handle:
                plistlib.dump({"Label": label, "ProgramArguments": ["/bin/true"]}, handle)
            env = {**os.environ, "HOME": str(home)}
            observed = subprocess.run(
                [str(ROOT / "tartci"), "pool", "status", "--json"],
                text=True, capture_output=True, check=False, env=env,
            )
            self.assertEqual(observed.returncode, 0, observed.stderr)
            body = json.loads(observed.stdout)
            self.assertTrue(body["fleet"]["managed"])
            self.assertFalse(body["fleet"]["fleet_ready"])
            self.assertEqual(
                body["fleet"]["problems"][0]["code"], "receipt_mismatch"
            )
            required = subprocess.run(
                [str(ROOT / "tartci"), "pool", "status", "--json", "--require-ready"],
                text=True, capture_output=True, check=False, env=env,
            )
            self.assertEqual(required.returncode, 8, required.stderr)

    def _run_pool(
        self, root: Path, *args: str
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        home = root / "home"
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "actions.runner.owner.repo.preamble.plist").write_text("plist\n")

        state = root / "state"
        participation = root / "participation"
        state.write_text("on\n")
        participation.write_text("1\n")

        fake_bin = root / "bin"
        fake_bin.mkdir()
        launchctl_log = root / "launchctl.log"
        nohup_log = root / "nohup.log"
        for name, body in {
            "scutil": "#!/bin/sh\nprintf 'test-host\\n'\n",
            "launchctl": "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$FAKE_LAUNCHCTL_LOG\"\n",
            "nohup": "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$FAKE_NOHUP_LOG\"\n",
        }.items():
            script = fake_bin / name
            script.write_text(body)
            script.chmod(0o755)

        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "PATH": f"{fake_bin}:{env['PATH']}",
                "FAKE_LAUNCHCTL_LOG": str(launchctl_log),
                "FAKE_NOHUP_LOG": str(nohup_log),
                "TARTCI_POOL_STATE_FILE": str(state),
                "TARTCI_POOL_PARTICIPATION_FILE": str(participation),
                "TARTCI_POOL_TRANSITION_LOCK": str(root / "lock"),
                "TARTCI_POOL_PERSISTENT_HOLD_FILE": str(
                    root / "persistent-runner-admission-hold"
                ),
            }
        )
        proc = subprocess.run(
            [str(ROOT / "tartci"), "pool", *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        return proc, launchctl_log, nohup_log

    def test_help_is_read_only_for_every_pool_subcommand(self) -> None:
        for subcommand in (None, "on", "drain", "off", "status", "repair-lock"):
            with self.subTest(
                subcommand=subcommand
            ), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                args = ("--help",) if subcommand is None else (subcommand, "--help")
                proc, launchctl_log, nohup_log = self._run_pool(root, *args)

                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage: tartci pool", proc.stdout)
                self.assertEqual((root / "participation").read_text(), "1\n")
                self.assertEqual((root / "state").read_text(), "on\n")
                self.assertFalse(launchctl_log.exists(), f"launchctl called for {subcommand}")
                self.assertFalse(nohup_log.exists(), f"watcher launched for {subcommand}")

    def test_pool_on_preserves_network_profile_failure_cause(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = root / "home" / ".config" / "tartci" / "network-profile.toml"
            profile.parent.mkdir(parents=True)
            profile.write_text("schema_version = 2\n", encoding="utf-8")
            proc, launchctl_log, _ = self._run_pool(root, "on")
            self.assertEqual(proc.returncode, 6, proc.stderr)
            self.assertIn("network profile requires schema_version = 1", proc.stderr)
            self.assertNotIn("opt-in network profile did not converge", proc.stderr)
            self.assertFalse(launchctl_log.exists(), "failed admission must not load a controller")


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
    def test_installer_rederives_persistent_authority_from_locked_profile(self) -> None:
        source = (ROOT / "scripts/install_macos_fleet.sh").read_text()
        locked = source.index('locked_config="$stage_dir/profile.toml"')
        snapshot_parse = source.index(
            'fleet.persistent_plist_records(data, Path(sys.argv[3]))', locked
        )
        persistent_preflight = source.index(
            'assert_agent_unloaded "$label" "declared persistent"',
            snapshot_parse,
        )
        self.assertLess(locked, snapshot_parse)
        self.assertLess(snapshot_parse, persistent_preflight)

    def test_pool_on_kickstarts_bootstrapped_services_before_admission(self) -> None:
        source = (ROOT / "tartci").read_text()
        start = source.index("    on|off)", source.index("cmd_pool()"))
        end = source.index("    repair-lock)", start)
        body = source[start:end]
        bootstrap = body.index('launchctl bootstrap "gui/$(id -u)"')
        kickstart = body.index('launchctl kickstart "$launchd_target"', bootstrap)
        state_on = body.index("tartci_pool_write_state on", kickstart)
        self.assertLess(bootstrap, kickstart)
        self.assertLess(kickstart, state_on)
        self.assertNotIn('kickstart -k "$launchd_target"', body)

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

    def test_every_provider_hands_transition_to_live_listener_before_waiting(self) -> None:
        cases = {
            "providers/tart-linux/runner.sh": ("CURRENT_RUNNER_PID=$!", "tartci_monitor_runner_assignment"),
            "providers/qemu-windows/runner.sh": ("runner_pid=$!", "runner_start=\"$(now_epoch)\""),
        }
        for relative, (spawn, wait_boundary) in cases.items():
            with self.subTest(provider=relative):
                source = (ROOT / relative).read_text()
                mint = source.rindex("generate-jitconfig")
                spawn_at = source.index(spawn, mint)
                handoff = source.index("tartci_pool_lock_handoff_to_listener", spawn_at)
                wait_at = source.index(wait_boundary, handoff)
                self.assertLess(spawn_at, handoff)
                self.assertLess(handoff, wait_at)

        mac = (ROOT / "providers/tart-macos/runner.sh").read_text()
        run_listener = mac[
            mac.index("run_runner_until_done(){") : mac.index("install_and_preflight_aqua_runner(){")
        ]
        spawn_at = run_listener.index("ssh_pid=$!")
        handoff = run_listener.index("tartci_pool_lock_handoff_to_listener", spawn_at)
        wait_at = run_listener.index("start=\"$(date +%s)\"", handoff)
        self.assertLess(spawn_at, handoff)
        self.assertLess(handoff, wait_at)

        windows = (ROOT / "providers/qemu-windows/runner.sh").read_text()
        linux = (ROOT / "providers/tart-linux/runner.sh").read_text()
        self.assertNotIn("assigned=1\n      tartci_pool_lock_release", mac)
        self.assertNotIn("runner_assigned=1\n      tartci_pool_lock_release", windows)
        self.assertNotIn("mark_runner_assigned(){ tartci_pool_lock_release", linux)

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
