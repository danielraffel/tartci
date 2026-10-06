#!/usr/bin/env python3
"""Selftests for the tart-linux lint lane that run without a GitHub job or a VM.

The live isolation proofs (acceptance plan sections C and D) run on a prototype
host and their output is recorded in the pull request; these cover what can be
proved from code alone: the egress allowlist derivation, the profile-derived
VM size, the lane's refusals, and the guest-probe verdict.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import egress_allowlist
import host_profile

ROOT = Path(__file__).resolve().parents[1]
LANE_LIB = ROOT / "providers" / "tart-linux" / "lint-lane.lib.sh"
RUNNER = ROOT / "providers" / "tart-linux" / "runner.sh"
FIXTURE = ROOT / "providers" / "tart-linux" / "fixtures" / "lint-hostile-job.sh"
MANIFEST = ROOT / "manifests" / "pulp.lint-linux.toml"

META = {
    "web": ["140.82.112.0/20", "2a0a:a440::/29"],
    "api": ["140.82.112.0/20", "143.55.64.0/20"],
    "git": ["140.82.112.0/20"],
    "actions": ["20.85.130.105/32", "4.148.0.0/16"],
}


class EgressAllowlistTests(unittest.TestCase):
    def test_derives_ipv4_only_collapsed_and_hashed(self) -> None:
        record = egress_allowlist.build_record(META, 1_000)
        self.assertEqual(record["keys"], ["web", "api", "git", "actions"])
        self.assertTrue(all(ipaddress.ip_network(c).version == 4 for c in record["cidrs"]))
        self.assertEqual(record["cidr_count"], 4)  # 140.82.112.0/20 deduplicated
        self.assertEqual(record["name"], "GitHub's published self-hosted-runner egress set")
        self.assertRegex(record["sha256"], r"^[0-9a-f]{64}$")

    def test_refuses_a_rule_that_would_void_the_block(self) -> None:
        for bad in ("0.0.0.0/0", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8"):
            with self.subTest(bad=bad):
                meta = dict(META, actions=[bad])
                with self.assertRaises(ValueError):
                    egress_allowlist.derive(meta)

    def test_documented_hostnames_add_host_resolved_slash_32s(self) -> None:
        answers = iter([["13.107.42.16"], ["13.107.42.16", "13.107.43.16"], ["13.107.42.16"]])
        resolved = egress_allowlist.resolve_hostnames(
            names=("pipelines.actions.githubusercontent.com",), rounds=3, resolver=lambda _: next(answers))
        self.assertEqual(resolved["pipelines.actions.githubusercontent.com"], ["13.107.42.16", "13.107.43.16"])
        record = egress_allowlist.build_record(META, 1_000, resolved)
        self.assertIn("13.107.42.16/32", record["cidrs"])
        self.assertIn("13.107.43.16/32", record["cidrs"])
        self.assertEqual(record["hostnames"], resolved)
        self.assertIn("documented runner hostnames", record["source"])

    def test_documented_hostnames_are_the_runner_set(self) -> None:
        names = set(egress_allowlist.RUNNER_HOSTNAMES)
        for required in ("github.com", "api.github.com", "pipelines.actions.githubusercontent.com",
                         "broker.actions.githubusercontent.com", "results-receiver.actions.githubusercontent.com"):
            self.assertIn(required, names)
        # Package indexes are not GitHub's runner egress and stay out.
        self.assertFalse({"pypi.org", "files.pythonhosted.org"} & names)

    def test_an_unresolvable_documented_hostname_refuses(self) -> None:
        def fail(_):
            raise OSError("nxdomain")
        with self.assertRaises(ValueError):
            egress_allowlist.resolve_hostnames(names=("github.com",), rounds=2, resolver=fail)

    def test_a_hostname_resolving_into_the_lan_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            egress_allowlist.derive(META, {"github.com": ["192.168.86.43"]})

    def test_refuses_a_missing_key(self) -> None:
        meta = {k: v for k, v in META.items() if k != "actions"}
        with self.assertRaises(ValueError):
            egress_allowlist.derive(meta)

    def test_tampered_cache_is_not_an_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c.json"
            record = egress_allowlist.build_record(META, time.time())
            egress_allowlist.save(cache, record)
            self.assertIsNotNone(egress_allowlist.load(cache))
            record["cidrs"].append("1.1.1.0/24")
            cache.write_text(json.dumps(record))
            self.assertIsNone(egress_allowlist.load(cache))

    def test_check_flags_withdrawn_ranges_past_the_lead_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c.json"
            fresh = Path(tmp) / "meta.json"
            old = egress_allowlist.build_record(META, time.time() - 100 * 3600)
            egress_allowlist.save(cache, old)
            hosts = Path(tmp) / "hosts.json"
            hosts.write_text("{}")
            fresh.write_text(json.dumps(dict(META, actions=["20.85.130.105/32"])))
            argv = ["check", "--cache", str(cache), "--fresh-meta", str(fresh), "--fresh-hostnames", str(hosts)]
            rc = egress_allowlist.main(argv)
            self.assertEqual(rc, 1)
            recent = egress_allowlist.build_record(META, time.time())
            egress_allowlist.save(cache, recent)
            rc = egress_allowlist.main(argv)
            self.assertEqual(rc, 0, "a withdrawal inside the lead window is not drift yet")


class LintVmSizeTests(unittest.TestCase):
    def test_size_and_slots_follow_the_non_gate_share(self) -> None:
        self.assertEqual(host_profile.lint_vm_settings(3, 27648),
                         {"lint_vm_cores": 1, "lint_vm_mem_mb": 2048, "lint_vm_slots": 2})
        self.assertEqual(host_profile.lint_vm_settings(1, 4096)["lint_vm_mem_mb"], 1024)
        self.assertEqual(host_profile.lint_vm_settings(1, 4096)["lint_vm_slots"], 1)
        self.assertEqual(host_profile.lint_vm_settings(0, 27648)["lint_vm_slots"], 0)
        self.assertEqual(host_profile.lint_vm_settings(4, 512)["lint_vm_slots"], 0)

    def test_profile_exports_the_lint_keys(self) -> None:
        profile = json.loads(subprocess.check_output(
            ["python3", str(ROOT / "scripts" / "host_profile.py"), "--json"], text=True))
        exports = host_profile.shell_exports(profile)
        for key in ("TARTCI_LINT_VM_CORES", "TARTCI_LINT_VM_MEM_MB", "TARTCI_LINT_VM_SLOTS"):
            self.assertIn(key + "=", exports)

    def test_no_typed_size_in_the_lane(self) -> None:
        """The lane takes its size from the profile; a literal would be a second source."""
        lib = LANE_LIB.read_text()
        runner = RUNNER.read_text()
        self.assertNotRegex(lib, r"(?m)^[^#]*TARTCI_(LINUX|LINT)_VM_(CORES|MEM_MB)=\d")
        self.assertNotRegex(lib, r"--(cpu|memory)[ =]\d")
        self.assertIn('tartci_vm_lease_cores "$LANE_PROVIDER"', runner)
        self.assertIn('tartci_vm_lease_mem_mb "$LANE_PROVIDER"', runner)
        vm_lease = (ROOT / "providers" / "common" / "vm-lease.lib.sh").read_text()
        block = vm_lease[vm_lease.index("    tart-linux-lint)"):]
        self.assertIn('key="lint_vm_cores"', block.split(";;")[0])


def lane_shell(script: str, **env: str) -> subprocess.CompletedProcess[str]:
    full = {
        "PATH": os.environ["PATH"], "HOME": env.pop("HOME", os.environ["HOME"]),
        "TARTCI_ROOT": str(ROOT), "TARTCI_LINUX_LANE": "lint", **env,
    }
    prelude = 'die(){ echo "DIE: $*"; exit 3; }; note(){ :; }; source "$TARTCI_ROOT/providers/tart-linux/lint-lane.lib.sh"; '
    return subprocess.run(["/bin/bash", "-c", prelude + script], capture_output=True, text=True, env=full)


class LaneRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        key = self.home / ".config" / "tartci" / "keys" / "lint-vm_ed25519"
        key.parent.mkdir(parents=True)
        key.write_text("k")
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh" / "id_ed25519").write_text("personal")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def configure(self, **env: str) -> subprocess.CompletedProcess[str]:
        env.setdefault("TARTCI_LINUX_GOLDEN", "pulp-lint-linux:2026-10-06")
        return lane_shell('tartci_lint_lane_configure && echo "LABELS=$LABELS KEY=$SSH_KEY_PRIV PREFIX=$TARTCI_LINT_NAME_PREFIX"',
                          HOME=str(self.home), **env)

    def test_defaults(self) -> None:
        out = self.configure().stdout
        self.assertIn("LABELS=self-hosted,Linux,ARM64,pulp-lint-linux-arm64", out)
        self.assertIn("lint-vm_ed25519", out)
        self.assertIn("PREFIX=pulp-lint-ephemeral-", out)

    def test_refuses_build_labels(self) -> None:
        for labels in ("self-hosted,Linux,ARM64,pulp-build-linux", "self-hosted,Linux,pulp-trusted-build"):
            with self.subTest(labels=labels):
                self.assertIn("refuses build labels", self.configure(TARTCI_RUNNER_LABELS=labels).stdout)

    def test_refuses_a_personal_key(self) -> None:
        out = self.configure(TARTCI_VM_SSH_KEY=str(self.home / ".ssh" / "id_ed25519")).stdout
        self.assertIn("refuses a personal SSH key", out)

    def test_refuses_an_undelimited_prefix(self) -> None:
        self.assertIn("must end in - or _", self.configure(TARTCI_RUNNER_NAME_PREFIX="pulp-lint").stdout)

    def test_refuses_without_a_golden(self) -> None:
        self.assertIn("needs TARTCI_LINUX_GOLDEN", self.configure(TARTCI_LINUX_GOLDEN="").stdout)

    def test_vm_name_is_namespaced(self) -> None:
        out = lane_shell('TARTCI_LINT_NAME_PREFIX=pulp-lint-ephemeral-; tartci_lint_vm_name 7').stdout
        self.assertRegex(out, r"^pulp-lint-ephemeral-[a-z0-9-]+-\d+-7$")

    def test_runner_refuses_to_boot_without_softnet_root(self) -> None:
        runner = RUNNER.read_text()
        gate = runner.index("tartci_lint_softnet_ready")
        self.assertLess(gate, runner.index("run_one(){"))
        self.assertIn("refusing to boot an untrusted guest on default NAT", runner)


class LaunchSourceTests(unittest.TestCase):
    """The hand-sed launchd template must not be able to start the lint lane."""

    TEMPLATE = ROOT / "launchd" / "com.danielraffel.pulp.tart-runner-linux.plist.template"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        key = self.home / ".config" / "tartci" / "keys" / "lint-vm_ed25519"
        key.parent.mkdir(parents=True)
        key.write_text("k")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def rendered_template_env(self) -> tuple[str, dict]:
        """Render the template the way its own header says, then add the lint switch."""
        import plistlib
        text = self.TEMPLATE.read_text()
        start = text.index("<?xml") if "<?xml" in text else text.index("<!DOCTYPE")
        body = text[start:].replace("$HOME", str(self.home)).replace(
            "$TARTCI_HOST_LABEL", "pulp-host-test").replace("$TART_HOME", str(self.home / "VMs"))
        plist = plistlib.loads(body.encode())
        env = dict(plist["EnvironmentVariables"])
        env["TARTCI_LINUX_LANE"] = "lint"
        env["TARTCI_LINUX_GOLDEN"] = "pulp-lint-linux:2026-10-06"
        return plist["Label"], env

    def configure_as_launchd(self, label: str, env: dict) -> subprocess.CompletedProcess[str]:
        return lane_shell("tartci_lint_lane_configure && echo CONFIGURED",
                          HOME=str(self.home), XPC_SERVICE_NAME=label,
                          **{k: v for k, v in env.items() if k not in ("PATH", "HOME")})

    def test_hand_template_with_the_lint_switch_refuses_to_start(self) -> None:
        label, env = self.rendered_template_env()
        out = self.configure_as_launchd(label, env).stdout
        self.assertNotIn("CONFIGURED", out)
        self.assertIn("refuses the hand-rendered tart-runner-linux template", out)

    def test_any_launchd_job_without_a_render_receipt_refuses(self) -> None:
        _, env = self.rendered_template_env()
        out = self.configure_as_launchd("com.example.renamed-copy", env).stdout
        self.assertNotIn("CONFIGURED", out)
        self.assertIn("needs a fleet-macos render receipt", out)

    def test_a_fleet_render_receipt_admits_the_launchd_job(self) -> None:
        _, env = self.rendered_template_env()
        receipt = self.home / "render.json"
        receipt.write_text(json.dumps({"renderer": "fleet-macos", "lane_kind": "tart-linux",
                                       "lane_profile": "lint"}))
        env["TARTCI_LANE_RENDER_RECEIPT"] = str(receipt)
        out = self.configure_as_launchd("com.danielraffel.tartci.lane.lint", env).stdout
        self.assertIn("CONFIGURED", out)

    def test_an_interactive_one_shot_is_allowed(self) -> None:
        for xpc in ("", "0"):
            with self.subTest(xpc=xpc):
                out = lane_shell("tartci_lint_lane_configure && echo CONFIGURED", HOME=str(self.home),
                                 XPC_SERVICE_NAME=xpc, TARTCI_LINUX_GOLDEN="pulp-lint-linux:x").stdout
                self.assertIn("CONFIGURED", out)


class GuestProbeVerdictTests(unittest.TestCase):
    GOOD = ("boot_id=x\nipv6_stack=absent\nipv6_global_addrs=0\nbake_stamp=ok\nhost_shares=0\n"
            "host_share_devices=0\ncredential_files=0\ntoken_strings=0")

    def verdict(self, probe: str) -> bool:
        return lane_shell('tartci_lint_probe_ok "$P"', P=probe).returncode == 0

    def test_good_probe_passes(self) -> None:
        self.assertTrue(self.verdict(self.GOOD))

    def test_each_property_is_required(self) -> None:
        for key, bad in (("ipv6_stack", "present"), ("bake_stamp", "bad"), ("ipv6_global_addrs", "1"),
                         ("host_shares", "1"),
                         ("host_share_devices", "1"),
                         ("credential_files", "2"), ("token_strings", "1")):
            with self.subTest(key=key):
                probe = re.sub(rf"(?m)^{key}=.*$", f"{key}={bad}", self.GOOD)
                self.assertFalse(self.verdict(probe))
        self.assertFalse(self.verdict(""), "an unreadable guest is a refusal")

    def test_probe_runs_before_any_jit_mint(self) -> None:
        runner = RUNNER.read_text()
        self.assertLess(runner.index("tartci_lint_guest_probe"), runner.index("generate-jitconfig"))
        self.assertLess(runner.index('tartci_lint_write_receipt "$logdir/receipt.json"'),
                        runner.index("generate-jitconfig"))

    def test_lint_lane_shares_no_host_directory(self) -> None:
        runner = RUNNER.read_text()
        lint_branch = runner[runner.index("if tartci_lint_lane_enabled; then\n    # Untrusted guest"):
                             runner.index("  else\n    tartci_prepare_disk_root")]
        self.assertNotIn("--dir", lint_branch)
        self.assertIn("--net-softnet-block=0.0.0.0/0", LANE_LIB.read_text())


class FixtureContractTests(unittest.TestCase):
    def test_fixture_probes_every_section_c_target(self) -> None:
        fixture = FIXTURE.read_text()
        for needle in ("api.github.com:443", "192.168.86.43:22", "100.100.100.100:53", "1.1.1.1:443",
                       "example.com:443", "udp:8.8.8.8:53", "ALLOWED(residual: DNS)", "ipv6:",
                       "iptables -F", "probe_set root", "PERSIST", "CRED", "ESCAPE", "fork-bomb"):
            self.assertIn(needle, fixture)

    def test_golden_manifest_is_pinned(self) -> None:
        import tomllib
        m = tomllib.loads(MANIFEST.read_text())
        self.assertIn("@sha256:", m["base"])
        self.assertRegex(m["runner"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(m["guest"]["ipv6"])
        self.assertFalse(m["guest"]["ssh_key"].startswith("~/.ssh/"))
        bake = (ROOT / "providers" / "tart-linux" / "provision-lint.sh").read_text()
        self.assertIn("ipv6.disable=1", bake)
        # The stamp is the last write before templating.
        self.assertLess(bake.index("/etc/tartci/bake-stamp"), bake.index('note "shut down and template"'))
        self.assertLess(bake.index("grep -qw ipv6.disable=1 /proc/cmdline"), bake.index("/etc/tartci/bake-stamp"))

    def test_token_scan_is_bounded_by_the_bake_stamp_with_every_pattern(self) -> None:
        lib = LANE_LIB.read_text()
        self.assertIn('-newer "$stamp"', lib)
        for pattern in ("gh[pousr]_", "github_pat_", "AKIA[0-9A-Z]{16}", "PRIVATE KEY"):
            self.assertIn(pattern, lib)
        probe = lib[lib.index("TARTCI_LINT_GUEST_PROBE='"):lib.index("tartci_lint_guest_probe(){")]
        # The probe body is one single-quoted string: an apostrophe inside it ends it early.
        self.assertEqual(probe.count("'"), 2, "an apostrophe inside the probe body breaks the quoting")


if __name__ == "__main__":
    unittest.main()
