#!/usr/bin/env python3
"""Hermetic installer tests for the Shipyard queue janitor."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("shipyard_queue_tick.sh")
SUPPORT = Path(__file__).with_name("shipyard_queue_tick_support.py")
INSTALLER = Path(__file__).with_name("install_shipyard_queue_tick.sh")


class QueueTickInstallerTests(unittest.TestCase):
    def test_reap_only_requires_explicit_app_wrapper(self) -> None:
        result = subprocess.run(
            [
                "/bin/bash",
                str(INSTALLER),
                "--repo-root",
                ".",
                "--mode",
                "reap",
            ],
            cwd=SCRIPT.parents[1],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("all modes require --gh-cli", result.stderr)

    def test_installer_deploys_and_verifies_launchd_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            repo = home / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/owner/repo.git",
                ],
                check=True,
            )
            fake_bin = home / "bin"
            fake_bin.mkdir()
            (fake_bin / "plutil").write_text(
                "#!/bin/sh\nexit 0\n", encoding="utf-8"
            )
            (fake_bin / "ghapp").write_text(
                "#!/bin/sh\nexit 0\n", encoding="utf-8"
            )
            (fake_bin / "launchctl").write_text(
                """#!/bin/sh
if [ "$1" = "print" ]; then
  printf '%s\\n' "$HOME/.config/shipyard/queue-tick.env"
  printf '%s\\n' "$HOME/.local/bin/tartci"
elif [ "$1" = "kickstart" ]; then
  mkdir -p "$HOME/Library/Logs"
  printf '{"status":"healthy"}\\n' > "$HOME/Library/Logs/shipyard-queue-tick.health.json"
fi
exit 0
""",
                encoding="utf-8",
            )
            for command in ("plutil", "ghapp", "launchctl"):
                (fake_bin / command).chmod(0o755)
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("SHIPYARD_", "TARTCI_"))
            }
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                }
            )
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER),
                    "--repo-root",
                    "repo",
                    "--mode",
                    "reap",
                    "--gh-cli",
                    "ghapp",
                    "--install",
                ],
                env=env,
                cwd=home,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            # Nothing is copied: the agent runs the installed generation.
            self.assertFalse((home / ".local/share/tartci/scripts").exists())
            config = home / ".config/shipyard/queue-tick.env"
            self.assertIn(
                f"SHIPYARD_QUEUE_REPO_ROOT={repo.resolve()}",
                config.read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                "SHIPYARD_QUEUE_AUTHORITY",
                config.read_text(encoding="utf-8"),
            )
            self.assertIn("fresh health verdict is healthy", result.stdout)
            with (
                home
                / "Library/LaunchAgents/"
                "com.danielraffel.shipyard.queue-tick.plist"
            ).open("rb") as source:
                plist = plistlib.load(source)
            self.assertEqual(plist["ProgramArguments"],
                             ["/bin/bash", f"{home}/.local/bin/tartci", "queue-tick"])
            environment = plist["EnvironmentVariables"]
            self.assertEqual(environment["SHIPYARD_TICK_APPLY"], "1")
            for retired in (
                "SHIPYARD_TICK_REAP_ONLY",
                "SHIPYARD_QUEUE_AUTHORITY",
                "SHIPYARD_TICK_MERGE_METHOD",
            ):
                self.assertNotIn(retired, environment)

    def test_merge_modes_are_retired(self) -> None:
        for args, message in (
            (["--mode", "live"], "--mode live is retired"),
            (["--authority"], "--authority is retired"),
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    ["/bin/bash", str(INSTALLER), *args],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)

    def test_failed_candidate_rolls_back_prior_bytes_and_loaded_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            repo = home / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/owner/repo.git",
                ],
                check=True,
            )
            install_dir = home / ".local/share/tartci/scripts"
            config_dir = home / ".config/shipyard"
            agents = home / "Library/LaunchAgents"
            logs = home / "Library/Logs"
            for path in (install_dir, config_dir, agents, logs):
                path.mkdir(parents=True)
            installed = install_dir / "shipyard_queue_tick.sh"
            installed_support = install_dir / "shipyard_queue_tick_support.py"
            config = config_dir / "queue-tick.env"
            plist = agents / "com.danielraffel.shipyard.queue-tick.plist"
            installed.write_bytes(b"prior-script")
            installed_support.write_bytes(b"prior-support")
            config.write_bytes(b"prior-config")
            plist.write_bytes(b"prior-plist")
            calls = home / "calls"
            fake_bin = home / "bin"
            fake_bin.mkdir()
            for command in ("ghapp", "plutil", "sleep"):
                (fake_bin / command).write_text(
                    "#!/bin/sh\nexit 0\n", encoding="utf-8"
                )
            (fake_bin / "launchctl").write_text(
                """#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
case "$1" in
  print)
    printf '%s\\n' "$HOME/.config/shipyard/queue-tick.env"
    printf '%s\\n' "$HOME/.local/bin/tartci"
    exit 0
    ;;
  kickstart)
    printf '{"status":"starting"}\\n' > "$HOME/Library/Logs/shipyard-queue-tick.health.json"
    exit 0
    ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            for command in ("ghapp", "plutil", "sleep", "launchctl"):
                (fake_bin / command).chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "CALLS": str(calls),
                    "SHIPYARD_QUEUE_INSTALL_HEALTH_WAIT_SECS": "1",
                }
            )
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER),
                    "--repo-root",
                    str(repo),
                    "--mode",
                    "reap",
                    "--gh-cli",
                    "ghapp",
                    "--install",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("rolling back", result.stderr)
            self.assertEqual(installed.read_bytes(), b"prior-script")
            self.assertEqual(
                installed_support.read_bytes(), b"prior-support"
            )
            self.assertEqual(config.read_bytes(), b"prior-config")
            self.assertEqual(plist.read_bytes(), b"prior-plist")
            call_text = calls.read_text(encoding="utf-8")
            self.assertGreaterEqual(call_text.count("bootstrap"), 2)
            self.assertGreaterEqual(call_text.count("bootout"), 2)



# A launchd stand-in with state: loaded or not, a tick that runs for
# FAKE_TICK_SECS and then writes FAKE_TICK_HEALTH (or nothing), and bootstrap
# calls that fail FAKE_BOOTSTRAP_FAILS times with launchd's EIO first.
FAKE_LAUNCHD = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
state_path = os.path.join(os.environ["FAKE_STATE"], "launchd.json")
try:
    state = json.load(open(state_path))
except (OSError, ValueError):
    state = {"loaded": os.environ.get("FAKE_PRIOR_LOADED") == "1", "running_until": 0,
             "bootstrap_fails": int(os.environ.get("FAKE_BOOTSTRAP_FAILS", "0"))}
def save():
    json.dump(state, open(state_path, "w"))
with open(os.path.join(os.environ["FAKE_STATE"], "calls"), "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
home = os.environ["HOME"]
command = sys.argv[1]
if command == "print":
    if not state["loaded"]:
        print('Could not find service', file=sys.stderr)
        sys.exit(113)
    running = time.time() < state["running_until"]
    print("\tstate = " + ("running" if running else "not running"))
    print(home + "/.config/shipyard/queue-tick.env")
    print(home + "/.local/bin/tartci")
elif command == "bootout":
    state["loaded"] = False
elif command == "bootstrap":
    if state["bootstrap_fails"] > 0:
        state["bootstrap_fails"] -= 1
        save()
        print("Bootstrap failed: 5: Input/output error", file=sys.stderr)
        sys.exit(5)
    state["loaded"] = True
elif command == "kickstart":
    seconds = float(os.environ.get("FAKE_TICK_SECS", "0"))
    state["running_until"] = time.time() + seconds
    health = os.environ.get("FAKE_TICK_HEALTH", "healthy")
    if health != "none":
        path = home + "/Library/Logs/shipyard-queue-tick.health.json"
        writer = ("import json, os, sys, time; time.sleep(float(sys.argv[3])); "
                  "os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True); "
                  "json.dump({'status': sys.argv[2]}, open(sys.argv[1], 'w'))")
        subprocess.Popen([sys.executable, "-c", writer, path, health, str(seconds)],
                         start_new_session=True)
save()
"""


class TickCompletionAndRollbackTests(unittest.TestCase):
    """The install waits for the tick itself, and a rollback never leaves the
    tick silently unloaded (m3, 2026-10-01: the health wait expired during a
    ~4 minute tick, and the rollback's bootstrap raced its bootout)."""

    def run_install(self, **fake: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
        directory = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        home = Path(directory)
        fake_bin = home / "bin"
        fake_bin.mkdir()
        for command in ("ghapp", "plutil"):
            (fake_bin / command).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "launchctl").write_text(FAKE_LAUNCHD, encoding="utf-8")
        for command in ("ghapp", "plutil", "launchctl"):
            (fake_bin / command).chmod(0o755)
        plist = home / "Library/LaunchAgents/com.danielraffel.shipyard.queue-tick.plist"
        plist.parent.mkdir(parents=True)
        plist.write_bytes(b"prior-plist")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SHIPYARD_", "TARTCI_"))}
        env.update({"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "FAKE_STATE": str(home), **fake})
        result = subprocess.run(
            ["/bin/bash", str(INSTALLER), "--mode", "reap", "--gh-cli", "ghapp", "--install"],
            env=env, cwd=home, text=True, capture_output=True, check=False, timeout=120)
        state = json.loads((home / "launchd.json").read_text())
        return result, home, Path(str(state["loaded"]))

    def test_a_tick_longer_than_the_wait_still_installs(self) -> None:
        # The tick runs 4 s against a 2 s wait. A fixed wait called this a
        # failed install; waiting for the running tick to finish does not.
        result, _, loaded = self.run_install(
            FAKE_TICK_SECS="4", SHIPYARD_QUEUE_INSTALL_HEALTH_WAIT_SECS="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fresh health verdict is healthy", result.stdout)
        self.assertEqual(str(loaded), "True")

    def test_a_tick_that_ends_without_a_verdict_fails_without_waiting_out_the_cap(self) -> None:
        # Control: the wait follows the tick, so a tick that exits silently is
        # a failure after the short idle wait, not after TICK_MAX_SECS.
        result, _, _ = self.run_install(
            FAKE_TICK_SECS="1", FAKE_TICK_HEALTH="none", FAKE_PRIOR_LOADED="1",
            SHIPYARD_QUEUE_INSTALL_HEALTH_WAIT_SECS="2")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("did not publish a fresh healthy verdict", result.stderr)

    def test_a_rollback_whose_bootstrap_races_the_bootout_still_reloads(self) -> None:
        # The new tick is unhealthy, so the install rolls back. The first two
        # bootstraps fail with EIO; the prior tick must still end up loaded.
        result, _, loaded = self.run_install(
            FAKE_TICK_SECS="0", FAKE_TICK_HEALTH="unhealthy", FAKE_PRIOR_LOADED="1",
            FAKE_BOOTSTRAP_FAILS="2")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("rolled back: the prior", result.stderr)
        self.assertEqual(str(loaded), "True")

    def test_a_rollback_kickstarts_the_prior_tick_after_reloading_it(self) -> None:
        # launchd can defer a RunAtLoad launch indefinitely on a busy host, so
        # a reloaded prior tick that is only bootstrapped may never run.
        result, home, loaded = self.run_install(
            FAKE_TICK_SECS="0", FAKE_TICK_HEALTH="unhealthy", FAKE_PRIOR_LOADED="1")
        self.assertIn("rolled back: the prior", result.stderr)
        self.assertEqual(str(loaded), "True")
        calls = (home / "calls").read_text().splitlines()
        last_bootstrap = max(i for i, call in enumerate(calls) if call.startswith("bootstrap"))
        self.assertTrue(
            any(call.startswith("kickstart") for call in calls[last_bootstrap + 1:]), calls)

    def test_a_rollback_that_cannot_reload_fails_loudly(self) -> None:
        result, home, loaded = self.run_install(
            FAKE_TICK_SECS="0", FAKE_TICK_HEALTH="unhealthy", FAKE_PRIOR_LOADED="1",
            FAKE_BOOTSTRAP_FAILS="99")
        self.assertEqual(str(loaded), "False")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("ROLLBACK FAILED", result.stderr)
        self.assertIn("NOT LOADED", result.stderr)
        health = json.loads((home / "Library/Logs/shipyard-queue-tick.health.json").read_text())
        self.assertEqual(health["status"], "unhealthy")
        self.assertIn("agent_unloaded", health["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
