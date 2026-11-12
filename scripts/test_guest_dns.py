#!/usr/bin/env python3
"""Tests for the opt-in guest resolvers ([guest_network] dns_servers)."""

from __future__ import annotations

import testing_support  # noqa: E402
testing_support.skip_module_without_tomllib()
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_profile  # noqa: E402
import macos_fleet_lanes as fleet  # noqa: E402

LIB = ROOT / "providers/tart-macos/guest-dns.lib.sh"
RUNNER = ROOT / "providers/tart-macos/runner.sh"
PROFILES = ROOT / "profiles"


class NormalizeTests(unittest.TestCase):
    def test_accepts_distinct_routable_addresses(self) -> None:
        self.assertEqual(host_profile.normalize_guest_dns_servers(["1.1.1.1", " 8.8.8.8 "]),
                         ["1.1.1.1", "8.8.8.8"])
        self.assertEqual(host_profile.normalize_guest_dns_servers(["2606:4700:4700::1111"]),
                         ["2606:4700:4700::1111"])

    def test_rejects_unusable_values(self) -> None:
        for bad in (None, "1.1.1.1", [], ["1.1.1.1"] * 2, ["1.1.1.1", 8],
                    ["127.0.0.1"], ["0.0.0.0"], ["224.0.0.1"], ["fe80::1"],
                    ["not-an-ip"], ["1.1.1.1; rm -rf /"],
                    ["1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9"]):
            with self.subTest(bad=bad):
                self.assertIsNone(host_profile.normalize_guest_dns_servers(bad))


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "profile.toml"
        env = {k: v for k, v in os.environ.items() if k != "TARTCI_GUEST_DNS_SERVERS"}
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def settings(self) -> dict:
        return host_profile.guest_network_settings(str(self.path))

    def test_absent_profile_or_table_is_off(self) -> None:
        self.assertEqual(self.settings(), {"guest_dns_servers": "", "guest_dns_source": "default"})
        self.path.write_text("schema = 1\n[leases]\nrank_vm_waiters = true\n")
        self.assertEqual(self.settings()["guest_dns_servers"], "")

    def test_table_is_read(self) -> None:
        self.path.write_text('schema = 1\n[guest_network]\ndns_servers = ["1.1.1.1", "8.8.8.8"]\n')
        got = self.settings()
        self.assertEqual(got["guest_dns_servers"], "1.1.1.1 8.8.8.8")
        self.assertEqual(got["guest_dns_source"], f"file:{self.path}")

    def test_malformed_table_reads_as_off_and_says_so(self) -> None:
        self.path.write_text('schema = 1\n[guest_network]\ndns_servers = ["127.0.0.1"]\n')
        got = self.settings()
        self.assertEqual(got["guest_dns_servers"], "")
        self.assertTrue(got["guest_dns_source"].startswith("invalid:"))
        self.path.write_text("schema = [\n")  # not TOML at all
        self.assertEqual(self.settings()["guest_dns_servers"], "")

    def test_environment_overrides_the_file(self) -> None:
        self.path.write_text('schema = 1\n[guest_network]\ndns_servers = ["1.1.1.1"]\n')
        with mock.patch.dict(os.environ, {"TARTCI_GUEST_DNS_SERVERS": "off"}):
            self.assertEqual(self.settings()["guest_dns_servers"], "")
        with mock.patch.dict(os.environ, {"TARTCI_GUEST_DNS_SERVERS": "9.9.9.9,8.8.8.8"}):
            self.assertEqual(self.settings(),
                             {"guest_dns_servers": "9.9.9.9 8.8.8.8",
                              "guest_dns_source": "environment"})
        with mock.patch.dict(os.environ, {"TARTCI_GUEST_DNS_SERVERS": "garbage"}):
            self.assertEqual(self.settings()["guest_dns_servers"], "1.1.1.1")

    def test_build_profile_carries_the_key(self) -> None:
        self.path.write_text('schema = 1\n[guest_network]\ndns_servers = ["1.1.1.1"]\n')
        profile = host_profile.build_profile(role="light", cores=8, model="t", memory_mb=16384,
                                             fleet_profile=str(self.path))
        self.assertEqual(profile["guest_dns_servers"], "1.1.1.1")


class FleetProfileTests(unittest.TestCase):
    def test_canary_is_m1_only(self) -> None:
        for path in sorted(PROFILES.glob("*-macos-fleet.toml")):
            data = fleet.load(path)
            with self.subTest(profile=path.name):
                if path.name == "m1-macos-fleet.toml":
                    self.assertEqual(data["guest_network"]["dns_servers"], ["1.1.1.1", "8.8.8.8"])
                else:
                    self.assertNotIn("guest_network", data)

    def test_table_is_policed(self) -> None:
        body = (PROFILES / "m1-macos-fleet.toml").read_text()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m1.toml"
            for bad, pattern in (
                    ('dns_servers = ["1.1.1.1"]\nextra = 1', "unknown guest_network"),
                    ('dns_servers = ["127.0.0.1"]', "routable IP"),
                    ('dns_servers = "1.1.1.1"', "routable IP"),
                    ("dns_servers = []", "routable IP")):
                mutated = re.sub(r"(?m)^dns_servers = .*$", lambda _m: bad, body, count=1)
                self.assertNotEqual(mutated, body, "control: the dns_servers line was rewritten")
                path.write_text(mutated)
                with self.assertRaisesRegex(ValueError, pattern):
                    fleet.load(path)


STUBS = {
    "route": 'echo "   interface: en0"',
    "networksetup": textwrap.dedent("""\
        echo "networksetup $*" >> "$STUB_LOG"
        case "$1" in
          -listnetworkserviceorder)
            printf '%s\\n' '(1) tart-version-2.36.0' '(Hardware Port: tart-version-2.36.0, Device: tart-version-2.36.0)' ''
            printf '%s\\n' '(2) Ethernet' '(Hardware Port: Ethernet, Device: en0)' ;;
          -getdnsservers) printf '%s\\n' ${STUB_READBACK:-$(cat "$STUB_STATE" 2>/dev/null)} ;;
          -setdnsservers) shift 2; printf '%s ' "$@" > "$STUB_STATE" ;;
        esac"""),
    "sudo": 'shift; "$@"',
    "killall": "exit 0",
    "sleep": "exit 0",
    "dscacheutil": '[ -n "${STUB_RESOLVE_FAIL:-}" ] || echo "ip_address: 140.82.112.3"',
}


class GuestScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for name, body in STUBS.items():
            (bindir / name).write_text("#!/bin/bash\n" + body + "\n")
            (bindir / name).chmod(0o755)
        self.log = self.tmp / "log"
        self.env = {"PATH": f"{bindir}:/usr/bin:/bin", "STUB_LOG": str(self.log),
                    "STUB_STATE": str(self.tmp / "state")}
        self.script = subprocess.run(["bash", "-c", f'source "{LIB}"; guest_dns_guest_script'],
                                     capture_output=True, text=True, check=True).stdout

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_guest(self, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-s", "--", "github.com", "1.1.1.1", "8.8.8.8"],
                              input=self.script, capture_output=True, text=True,
                              env={**self.env, **env}, check=False)

    def sets(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if "-setdnsservers" in line]

    def test_applies_to_the_default_route_service(self) -> None:
        proc = self.run_guest()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.sets(), ["networksetup -setdnsservers Ethernet 1.1.1.1 8.8.8.8"])

    def test_unresolvable_probe_restores_dhcp(self) -> None:
        proc = self.run_guest(STUB_RESOLVE_FAIL="1")
        self.assertEqual(proc.returncode, 5)
        self.assertEqual(self.sets()[-1], "networksetup -setdnsservers Ethernet Empty")

    def test_read_back_mismatch_restores_dhcp(self) -> None:
        proc = self.run_guest(STUB_READBACK="192.168.64.1")
        self.assertEqual(proc.returncode, 4)
        self.assertEqual(self.sets()[-1], "networksetup -setdnsservers Ethernet Empty")

    def test_no_matching_service_changes_nothing(self) -> None:
        bindir = Path(self.env["PATH"].split(":")[0])
        (bindir / "route").write_text('#!/bin/bash\necho "   interface: en9"\n')
        proc = self.run_guest()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.sets(), [])


class HostWrapperTests(unittest.TestCase):
    """Drive tartci_apply_guest_dns with a stub ssh that runs the guest script locally."""

    def apply(self, servers: str, ssh_rc: int = 0) -> tuple[subprocess.CompletedProcess, str, str]:
        with tempfile.TemporaryDirectory() as td:
            events, ssh_log = Path(td) / "events", Path(td) / "ssh"
            prog = textwrap.dedent(f"""\
                source "{LIB}"
                tartci_profile_value(){{ [ "$1" = guest_dns_servers ] && printf '%s' "{servers}"; }}
                event(){{ printf '%s %s\\n' "$1" "$2" >> "{events}"; }}
                note(){{ :; }}
                ssh(){{ cat >/dev/null; printf '%s\\n' "$*" >> "{ssh_log}"; return {ssh_rc}; }}
                SSH_OPTS=(-o BatchMode=yes); SSH_KEY_PRIV=/k; VM_USER=admin
                tartci_apply_guest_dns 192.168.64.9
                """)
            proc = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, check=False)
            read = lambda p: p.read_text() if p.exists() else ""  # noqa: E731
            return proc, read(events), read(ssh_log)

    def test_off_is_a_no_op(self) -> None:
        proc, events, ssh_log = self.apply("")
        self.assertEqual((proc.returncode, events, ssh_log), (0, "", ""))

    def test_applied(self) -> None:
        proc, events, ssh_log = self.apply("1.1.1.1 8.8.8.8")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("admin@192.168.64.9 bash -s -- github.com 1.1.1.1 8.8.8.8", ssh_log)
        self.assertEqual(events.strip(), "guest_dns result=applied servers=1.1.1.1,8.8.8.8")

    def test_guest_failure_is_fail_open(self) -> None:
        proc, events, _ = self.apply("1.1.1.1", ssh_rc=5)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(events.strip(), "guest_dns result=kept_dhcp rc=5 servers=1.1.1.1")

    def test_unsafe_words_never_reach_ssh(self) -> None:
        proc, events, ssh_log = self.apply("1.1.1.1 $(id)")
        self.assertEqual((proc.returncode, ssh_log), (0, ""))
        self.assertEqual(events.strip(), "guest_dns result=skipped reason=bad_servers")


class RunnerWiringTests(unittest.TestCase):
    def test_applied_after_boot_and_before_the_runner_exists(self) -> None:
        body = RUNNER.read_text()
        self.assertIn('source "$TARTCI_ROOT/providers/tart-macos/guest-dns.lib.sh"', body)
        apply_at = body.index('tartci_apply_guest_dns "$ip"')
        self.assertLess(body.index('ip="$CURRENT_IP"'), apply_at)
        self.assertLess(apply_at, body.index('if ! ensure_runner_version "$ip"; then'))


if __name__ == "__main__":
    unittest.main()
