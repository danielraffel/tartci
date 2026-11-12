#!/usr/bin/env python3
"""The disk reclaimer must be installed by setup, not left to a manual step.

Three hosts ran without it because installing it was documented rather than
wired: m5 reached 14 GiB free with 488 build dirs and refused 276 leases. These
tests pin the two properties that keep that from recurring — `setup` reaches the
installer, and the installer is safe to call on every setup.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_reclaim_agent.sh"
LABEL = "com.danielraffel.tartci.reclaim"


def run(argv, env=None):
    return subprocess.run(argv, cwd=ROOT, text=True, capture_output=True,
                          env={**os.environ, **(env or {})}, check=False)


class SetupWiresTheReclaimer(unittest.TestCase):
    def test_setup_invokes_the_installer(self):
        # The gap was never "no installer" — it was that nothing called one. A
        # grep is the whole assertion: if setup stops calling it, a new machine
        # silently ships without the janitor again.
        body = (ROOT / "tartci").read_text()
        start = body.index("cmd_setup()")
        end = body.index("cmd_bench()", start)
        self.assertIn("install_reclaim_agent.sh", body[start:end],
                      "cmd_setup must install the disk reclaimer")

    def test_doctor_reports_both_janitors(self):
        body = (ROOT / "tartci").read_text()
        start = body.index("cmd_doctor()")
        end = body.index("cmd_setup()", start)
        section = body[start:end]
        self.assertIn("janitors:", section)
        self.assertIn("com.danielraffel.tartci.${_j%%:*}", section)


def launchctl_double(directory: Path) -> tuple[Path, Path]:
    """A launchctl stand-in that records every call and holds no real domain.

    `print` succeeds while something is "loaded" and reports the plist path it
    was bootstrapped from (the file `loaded` holds it), like launchd does.
    """
    calls = directory / "launchctl.calls"
    double = directory / "launchctl-double"
    loaded = directory / "loaded"
    double.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{calls}'\n"
        f"case \"$1\" in print) [ -f '{loaded}' ] && printf '\\tpath = %s\\n' \"$(cat '{loaded}')\" ;; "
        f"bootstrap) printf '%s' \"$3\" > '{loaded}' ;; bootout) rm -f '{loaded}' ;; "
        "esac\n")
    double.chmod(0o755)
    return double, calls


def as_real(double: Path) -> dict:
    """Env that makes the guard judge `double` as if it were /bin/launchctl."""
    return {"TARTCI_LAUNCHCTL_BIN": str(double), "TARTCI_LAUNCHD_GUARD_TREAT_AS_REAL": "1"}


class InstallerIsSafeToRepeat(unittest.TestCase):
    def test_plan_mode_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "agents"
            double, _ = launchctl_double(Path(td))
            res = run([str(INSTALLER), "--plan"], {"TARTCI_AGENTS_DIR": str(agents),
                                                   "TARTCI_LAUNCHCTL_BIN": str(double)})
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertFalse((agents / f"{LABEL}.plist").exists(),
                             "--plan must not write the agent")
            self.assertIn("plan:", res.stdout)

    def test_renders_a_valid_plist_naming_the_reclaimer(self):
        # Render through the same path the installer uses, so a template that
        # stops invoking `tartci reclaim` fails here rather than on a host.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "rendered.plist"
            res = run(["python3", "scripts/render_launchd_template.py",
                       f"launchd/{LABEL}.plist.template", "--set", f"HOME={td}"])
            self.assertEqual(res.returncode, 0, res.stderr)
            out.write_text(res.stdout)
            spec = plistlib.loads(out.read_bytes())
            self.assertEqual(spec["Label"], LABEL)
            self.assertIn("reclaim", spec["ProgramArguments"])
            self.assertIn("--fix", spec["ProgramArguments"])
            # Space-triggered, not calendar-triggered: the whole point is to act
            # when the volume is filling, and an hourly pass is what makes the
            # pressure thresholds meaningful.
            self.assertGreater(int(spec["StartInterval"]), 0)
            env = spec.get("EnvironmentVariables") or {}
            self.assertIn("TARTCI_RECLAIM_PRESSURE_FREE_GB", env)
            self.assertIn("TARTCI_RECLAIM_FAIL_BELOW_GB", env)
            # A pass walks terabytes on the volumes gate VMs build on; it must
            # yield CPU and I/O to them, without being confined to the
            # efficiency cores (Background), which stalls it under load.
            self.assertEqual(spec.get("ProcessType"), "Standard")
            self.assertGreaterEqual(spec.get("Nice", 0), 5)
            self.assertIs(spec.get("LowPriorityIO"), True)

    def test_rejects_an_unknown_argument(self):
        res = run([str(INSTALLER), "--wat"])
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)


class NeverTouchesTheRealDomainFromATempHome(unittest.TestCase):
    """The leak: a temp-HOME install replaced the host's real reclaim agent.

    `tartci setup` run by tests/test_tart_channel.sh with HOME=$tmp/home
    reached this installer, which bootstrapped $tmp/home's plist into the one
    real gui/<uid> domain after booting the real job out. The real launchctl is
    now reachable only from the account's own home and LaunchAgents dir.
    """

    # Every refusal below runs against a recording double that the guard
    # judges as the real launchctl. If the guard is ever broken, the install
    # lands in the double (and the test fails on its call log) instead of in
    # this machine's real gui/<uid> domain.

    def test_temp_home_is_refused_before_any_launchctl_call_or_write(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            double, calls = launchctl_double(Path(td))
            res = run([str(INSTALLER), "--install"], {"HOME": str(home), **as_real(double)})
            self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
            self.assertIn("is not this account's home", res.stderr)
            self.assertFalse((home / "Library" / "LaunchAgents" / f"{LABEL}.plist").exists())
            self.assertFalse(calls.exists(), "launchctl was called despite the refusal")

    def test_the_default_launchctl_is_the_real_one(self):
        # The refusal tests above rely on the guard treating the default as
        # real; pin that the default IS /bin/launchctl.
        body = INSTALLER.read_text()
        self.assertIn('LAUNCHCTL="${TARTCI_LAUNCHCTL_BIN:-/bin/launchctl}"', body)

    def test_real_home_but_foreign_agents_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            double, calls = launchctl_double(Path(td))
            res = run([str(INSTALLER), "--install"],
                      {"TARTCI_AGENTS_DIR": str(Path(td) / "agents"), **as_real(double)})
            self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
            self.assertIn("is not under", res.stderr)
            self.assertFalse(calls.exists())

    def test_a_shadowing_registration_is_booted_out_and_replaced(self):
        # m3 after the leak: the label was loaded, from a temp plist. The old
        # installer saw "loaded" + "plist current" and did nothing.
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            double, calls = launchctl_double(Path(td))
            env = {"HOME": str(home), "TARTCI_LAUNCHCTL_BIN": str(double)}
            self.assertEqual(run([str(INSTALLER), "--install"], env).returncode, 0)
            leaked = "/private/var/folders/x/T/tmp.leak/home/Library/LaunchAgents/r.plist"
            (Path(td) / "loaded").write_text(leaked)
            calls.unlink()
            res = run([str(INSTALLER), "--install"], env)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("leaked registration", res.stdout)
            target = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            log = calls.read_text()
            self.assertIn(f"bootout gui/{os.getuid()}/{LABEL}", log)
            self.assertIn(f"bootstrap gui/{os.getuid()} {target}", log)
            self.assertEqual((Path(td) / "loaded").read_text(), str(target))
            # Control: run again, now held from the target, and nothing changes.
            calls.unlink()
            again = run([str(INSTALLER), "--install"], env)
            self.assertIn("already installed and loaded", again.stdout)
            self.assertNotIn("bootstrap", calls.read_text())

    def test_a_test_double_installs_into_the_temp_home(self):
        # Control: the guard refuses the real domain, not installation. With a
        # double the same temp-HOME install succeeds and bootstraps ITS plist.
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            double, calls = launchctl_double(Path(td))
            res = run([str(INSTALLER), "--install"],
                      {"HOME": str(home), "TARTCI_LAUNCHCTL_BIN": str(double)})
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            plist = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            self.assertTrue(plist.is_file())
            self.assertIn(f"bootstrap gui/{os.getuid()} {plist}", calls.read_text())

    def test_self_update_installer_carries_the_same_guard(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            double, calls = launchctl_double(Path(td))
            res = run([str(ROOT / "scripts" / "install_self_update_agent.sh"), "--install"],
                      {"HOME": str(home), "TARTCI_SELF_UPDATE_SKIP_PEERS": "1",
                       **as_real(double)})
            self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
            self.assertIn("is not this account's home", res.stderr)
            self.assertFalse(calls.exists())

    def test_shell_suites_that_run_setup_cannot_reach_the_real_domain(self):
        # tests/test_tart_channel.sh runs `tartci setup` with a temp HOME. It
        # must now see the installer refuse, not silently register.
        body = (ROOT / "tests" / "test_tart_channel.sh").read_text()
        self.assertIn("launchd guard: HOME=", body)


if __name__ == "__main__":
    unittest.main()
