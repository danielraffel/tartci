#!/usr/bin/env python3
"""Declared support agents converge, and nothing undeclared is ever touched.

Pins: the registry renders each agent byte-identically to its install script;
plan writes nothing but its receipt; apply acts only with the profile switch on,
renders everything before changing anything, leaves a matching agent alone,
kickstarts only where the installer does, and removes only a declaration
dropped from a present table; an absent table manages and removes nothing;
undeclared tartci and tmp. agents are reported, never touched; the registry and
OTHER_OWNERS account for exactly the templates in launchd/.

Run:  python3 scripts/test_support_agents.py
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_doctor  # noqa: E402
import support_agents as sa  # noqa: E402

FOUR = ["reclaim", "artifact-cache-refresh", "keychain-unlock", "schedule-backstop"]
THREE = FOUR[:3]


def table(declared: List[str], bootstrap: bool = False, backstop: str = "off") -> str:
    names = ", ".join(f'"{n}"' for n in declared)
    return (f'schema = 1\nschedule_backstop = "{backstop}"\n[host]\nid = "t"\n'
            f'[support_agents]\ndeclared = [{names}]\nbootstrap = {str(bootstrap).lower()}\n')


class FakeSystem(sa.System):
    """Real renders; launchctl recorded and simulated; guard always passes."""

    def __init__(self, home: Path) -> None:
        super().__init__(home, "launchctl-fake")
        self.loaded: Dict[str, str] = {}
        self.calls: List[Tuple[str, ...]] = []
        self.fail_render: set = set()
        self.guard_ok = True
        self.bootstrap_rc = 0

    @property
    def agents_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    def render(self, agent, profile):
        if agent.label in self.fail_render:
            return None, "template broken"
        return super().render(agent, profile)

    def launchctl_run(self, *args):
        self.calls.append(args)
        if args[0] == "bootstrap":
            if self.bootstrap_rc == 0:
                spec = plistlib.loads(Path(args[2]).read_bytes())
                self.loaded[spec["Label"]] = args[2]
            return self.bootstrap_rc, "" if self.bootstrap_rc == 0 else "Bootstrap failed: 5"
        if args[0] == "bootout":
            self.loaded.pop(args[1].split("/", 2)[2], None)
        return 0, ""

    def loaded_path(self, label):
        return self.loaded.get(label)

    def domain_guard(self, target):
        return (True, "") if self.guard_ok else (False, "HOME is not this account's home")

    def mutations(self) -> List[Tuple[str, ...]]:
        return [c for c in self.calls if c[0] != "print"]


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.home = self.root / "home"
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        self.profile = self.root / "profile.toml"
        self.state = self.root / "state"
        self.sys = FakeSystem(self.home)

    def tearDown(self) -> None:
        self.td.cleanup()

    def write_profile(self, text: str) -> None:
        self.profile.write_text(text)

    def run_pass(self, mode: str = "auto") -> int:
        return sa.Converger(self.sys, self.profile, self.state).run(mode)

    def receipt(self) -> dict:
        return json.loads((self.state / "last.json").read_text())

    def plist(self, name: str) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{sa.REGISTRY[name].label}.plist"

    def install_rendered(self, name: str, profile: Optional[dict] = None, load: bool = True) -> bytes:
        data, _ = sa.load_profile(self.profile)
        rendered, err = sa.System.render(self.sys, sa.REGISTRY[name], profile or data)
        self.assertIsNotNone(rendered, err)
        self.plist(name).write_bytes(rendered)
        if load:
            self.sys.loaded[sa.REGISTRY[name].label] = str(self.plist(name))
        return rendered


@unittest.skipIf(sa.tomllib is None, "needs tomllib")
class HostMaintenanceAgents(Case):
    """The watchdog and the reaper are declared like every other support agent.

    They were rendered by hand from launchd/README.md, so m5studio was brought
    up serving gate VMs with no watchdog: no heal pass, no skew or tool
    freshness refresh, and nothing reported it.
    """

    def test_m5studio_without_a_watchdog_reads_missing(self):
        self.write_profile(table(["launchd-watchdog", "reap"]).replace(
            '[host]\nid = "t"\n', '[host]\nid = "m5studio"\ntart_home = "/Volumes/Atelier/VMs"\n'))
        self.install_rendered("reap")
        self.assertEqual(self.run_pass("plan"), sa.EXIT_OK)
        agents = self.receipt()["agents"]
        self.assertEqual(agents["launchd-watchdog"]["state"], "missing")
        self.assertEqual(agents["reap"]["state"], "match_bytes")
        self.assertEqual(self.sys.mutations(), [])

    def test_each_render_carries_its_own_hosts_tart_home(self):
        for host, store in (("m3", "/Volumes/Workshop/VMs"), ("m5studio", "/Volumes/Atelier/VMs")):
            profile = {"host": {"id": host, "tart_home": store}}
            for name in ("launchd-watchdog", "reap"):
                rendered, err = sa.System.render(self.sys, sa.REGISTRY[name], profile)
                self.assertIsNotNone(rendered, err)
                env = plistlib.loads(rendered)["EnvironmentVariables"]
                self.assertEqual(env.get("TART_HOME"), store, (host, name))

    def test_every_shipped_profile_declares_them(self):
        for path in sorted((ROOT / "profiles").glob("*-macos-fleet.toml")):
            data, _ = sa.load_profile(path)
            declared = (data.get("support_agents") or {}).get("declared") or []
            with self.subTest(profile=path.name):
                self.assertIn("launchd-watchdog", declared)
                self.assertIn("reap", declared)

    def test_they_are_no_longer_owned_by_hand(self):
        for label in ("com.danielraffel.tartci.launchd-watchdog", "com.danielraffel.tartci.reap"):
            self.assertNotIn(label, sa.OTHER_OWNERS)


@unittest.skipIf(sa.tomllib is None, "needs tomllib")
class BootstrapOnly(Case):
    """`bootstrap = ["reap"]` installs the VM janitor and nothing else.

    On 2026-10-09 every profile declared reap and only m3 had it: with
    bootstrap = false the self-update pass only planned. Turning bootstrap on
    for the whole table would also rewrite or install every other agent.
    """

    DECLARED = ["reclaim", "artifact-cache-refresh", "keychain-unlock",
                "launchd-watchdog", "reap"]

    def setUp(self) -> None:
        super().setUp()
        self.write_profile(table(self.DECLARED).replace(
            "bootstrap = false", 'bootstrap = ["reap"]').replace(
            '[host]\nid = "t"\n', '[host]\nid = "m1"\ntart_home = "/Users/x/VMs"\n'))

    def test_only_the_janitor_is_installed(self):
        self.assertEqual(self.run_pass(), sa.EXIT_OK, self.receipt())
        self.assertEqual(self.sys.loaded[sa.REGISTRY["reap"].label], str(self.plist("reap")))
        for name in self.DECLARED[:-1]:
            self.assertFalse(self.plist(name).exists(), name)
        import fleet_doctor
        self.assertEqual(fleet_doctor.check_vm_janitor(sa.status(self.state)).code,
                         "vm_janitor_loaded", "the doctor reads the pass that installed it")
        written = {c[2] for c in self.sys.mutations() if c[0] == "bootstrap"}
        self.assertEqual(written, {str(self.plist("reap"))})
        self.assertEqual(self.receipt()["bootstrap_only"], ["reap"])

    def test_the_others_stay_reported_as_pending(self):
        self.run_pass()
        status = sa.status(self.state)
        self.assertEqual(status["state"], "drift")
        self.assertNotIn("reap", status["changes"])
        self.assertIn("launchd-watchdog", status["changes"])

    def test_a_list_never_drops_an_agent(self):
        self.run_pass()
        self.write_profile(table(["reap"]).replace("bootstrap = false", 'bootstrap = ["reap"]'))
        self.install_rendered("reclaim")
        self.run_pass()
        self.assertTrue(self.plist("reclaim").exists())
        reclaim = sa.REGISTRY["reclaim"].label
        self.assertFalse([c for c in self.sys.mutations()
                          if c[0] == "bootout" and c[1].endswith(reclaim)])

    def test_false_still_writes_nothing(self):
        # Control, same instrument: only the switch changed.
        self.write_profile(table(self.DECLARED))
        self.assertEqual(self.run_pass(), sa.EXIT_OK)
        self.assertEqual(self.sys.mutations(), [])
        self.assertFalse(self.plist("reap").exists())

    def test_every_shipped_profile_bootstraps_the_janitor_only(self):
        for path in sorted((ROOT / "profiles").glob("*-macos-fleet.toml")):
            data, _ = sa.load_profile(path)
            with self.subTest(profile=path.name):
                self.assertEqual(data["support_agents"]["bootstrap"], ["reap"])
                self.assertEqual(sa.validate(data), [])


@unittest.skipIf(sa.tomllib is None, "needs tomllib")
class Plan(Case):
    def test_missing_matching_and_differing_are_named_and_nothing_is_written(self):
        self.write_profile(table(THREE))
        self.install_rendered("reclaim")
        spec = plistlib.loads(self.install_rendered("keychain-unlock"))
        self.plist("keychain-unlock").write_bytes(plistlib.dumps(spec, sort_keys=True))
        spec["StartInterval"] = 1
        before = plistlib.dumps(spec)
        self.plist("keychain-unlock").write_bytes(before)
        self.assertEqual(self.run_pass("plan"), sa.EXIT_OK)
        agents = self.receipt()["agents"]
        self.assertEqual(agents["reclaim"]["state"], "match_bytes")
        self.assertEqual(agents["artifact-cache-refresh"]["state"], "missing")
        self.assertEqual(agents["keychain-unlock"]["state"], "differs")
        self.assertEqual([d["path"] for d in agents["keychain-unlock"]["diff"]], ["/StartInterval"])
        self.assertEqual(self.sys.mutations(), [])
        self.assertFalse(self.plist("artifact-cache-refresh").exists())
        self.assertEqual(self.plist("keychain-unlock").read_bytes(), before)

    def test_key_order_alone_is_a_plist_match(self):
        self.write_profile(table(["reclaim"]))
        spec = plistlib.loads(self.install_rendered("reclaim"))
        self.plist("reclaim").write_bytes(plistlib.dumps(spec, sort_keys=True))
        self.run_pass("plan")
        self.assertEqual(self.receipt()["agents"]["reclaim"]["state"], "match_plist")

    def test_switch_off_never_writes_even_with_differences(self):
        self.write_profile(table(THREE, bootstrap=False))
        for mode in ("auto", "apply"):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_pass(mode), sa.EXIT_OK)
                self.assertEqual(self.receipt()["mode"], "plan")
                self.assertEqual(self.sys.mutations(), [])
                self.assertFalse(any(self.plist(n).exists() for n in THREE))

    def test_the_backstop_environment_follows_its_mode(self):
        self.write_profile(table(FOUR, backstop="live"))
        self.run_pass("plan")
        data, _ = sa.load_profile(self.profile)
        rendered, _ = sa.System.render(self.sys, sa.REGISTRY["schedule-backstop"], data)
        env = plistlib.loads(rendered)["EnvironmentVariables"]
        self.assertEqual(env["TARTCI_BACKSTOP_APPLY"], "1")
        self.write_profile(table(FOUR, backstop="dry-run"))
        data, _ = sa.load_profile(self.profile)
        rendered, _ = sa.System.render(self.sys, sa.REGISTRY["schedule-backstop"], data)
        self.assertEqual(plistlib.loads(rendered)["EnvironmentVariables"]["TARTCI_BACKSTOP_APPLY"], "0")


@unittest.skipIf(sa.tomllib is None, "needs tomllib")
class Apply(Case):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile(table(THREE, bootstrap=True))

    def test_missing_is_rendered_bootstrapped_and_kickstarted_only_where_the_installer_does(self):
        self.assertEqual(self.run_pass(), sa.EXIT_OK, self.receipt())
        for name in THREE:
            self.assertTrue(self.plist(name).is_file())
            self.assertEqual(self.sys.loaded[sa.REGISTRY[name].label], str(self.plist(name)))
        kicked = {c[1].split("/", 2)[2] for c in self.sys.calls if c[0] == "kickstart"}
        self.assertEqual(kicked, {sa.REGISTRY["reclaim"].label, sa.REGISTRY["keychain-unlock"].label})
        self.assertEqual(oct(self.plist("reclaim").stat().st_mode & 0o777), "0o644")

    def test_a_matching_loaded_agent_is_left_alone(self):
        for name in THREE:
            self.install_rendered(name)
        spec = plistlib.loads(self.plist("reclaim").read_bytes())
        self.plist("reclaim").write_bytes(plistlib.dumps(spec, sort_keys=True))
        self.assertEqual(self.run_pass(), sa.EXIT_OK)
        self.assertEqual(self.sys.mutations(), [])
        self.assertEqual(sa.status(self.state)["state"], "ok")

    def test_matching_but_not_loaded_is_bootstrapped_without_a_write(self):
        for name in THREE:
            self.install_rendered(name)
        self.sys.loaded.pop(sa.REGISTRY["reclaim"].label)
        before = self.plist("reclaim").stat().st_mtime_ns
        self.run_pass()
        self.assertEqual([c[0] for c in self.sys.mutations()], ["bootstrap", "kickstart"])
        self.assertEqual(self.plist("reclaim").stat().st_mtime_ns, before)

    def test_a_leaked_registration_is_booted_out_first(self):
        for name in THREE:
            self.install_rendered(name)
        self.sys.loaded[sa.REGISTRY["reclaim"].label] = "/tmp/elsewhere.plist"
        self.run_pass()
        self.assertEqual([c[0] for c in self.sys.mutations()], ["bootout", "bootstrap", "kickstart"])

    def test_a_differing_loaded_agent_is_rewritten_and_reloaded(self):
        for name in THREE:
            self.install_rendered(name)
        spec = plistlib.loads(self.plist("artifact-cache-refresh").read_bytes())
        spec["StartInterval"] = 1
        self.plist("artifact-cache-refresh").write_bytes(plistlib.dumps(spec))
        self.run_pass()
        self.assertEqual([c[0] for c in self.sys.mutations()], ["bootout", "bootstrap"])
        self.assertNotEqual(plistlib.loads(self.plist("artifact-cache-refresh").read_bytes())
                            ["StartInterval"], 1)

    def test_one_failing_render_changes_nothing_in_the_pass(self):
        self.sys.fail_render.add(sa.REGISTRY["keychain-unlock"].label)
        self.assertEqual(self.run_pass(), sa.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])
        self.assertFalse(any(self.plist(n).exists() for n in THREE))
        self.assertIn("render failed", self.receipt()["refused"])

    def test_a_failed_bootstrap_is_reported_and_not_kickstarted(self):
        self.sys.bootstrap_rc = 5
        self.assertEqual(self.run_pass(), sa.EXIT_FAILED)
        self.assertTrue(all("bootstrap failed" in f for f in self.receipt()["failures"]),
                        self.receipt()["failures"])
        self.assertFalse([c for c in self.sys.calls if c[0] == "kickstart"])
        self.assertEqual(sa.status(self.state)["state"], "drift")

    def test_the_domain_guard_refuses_before_any_launchctl_call(self):
        self.sys.guard_ok = False
        self.assertEqual(self.run_pass(), sa.EXIT_FAILED)
        self.assertEqual(self.sys.mutations(), [])
        self.assertFalse(any(self.plist(n).exists() for n in THREE))


@unittest.skipIf(sa.tomllib is None, "needs tomllib")
class Drop(Case):
    def converge(self, declared: List[str], bootstrap: bool = True) -> None:
        self.write_profile(table(declared, bootstrap=bootstrap))
        self.assertEqual(self.run_pass(), sa.EXIT_OK, self.receipt())

    def test_a_dropped_declaration_is_booted_out_and_moved_aside_with_its_log_kept(self):
        self.converge(THREE)
        log = self.home / "Library" / "Logs" / "tartci" / "keychain-unlock.log"
        log.parent.mkdir(parents=True)
        log.write_text("history\n")
        self.sys.calls.clear()
        self.converge(["reclaim", "artifact-cache-refresh"])
        dropped = self.receipt()["dropped"]
        self.assertEqual([d["name"] for d in dropped], ["keychain-unlock"])
        self.assertFalse(self.plist("keychain-unlock").exists())
        self.assertTrue(Path(dropped[0]["moved_to"]).is_file())
        self.assertNotIn(sa.REGISTRY["keychain-unlock"].label, self.sys.loaded)
        self.assertEqual(log.read_text(), "history\n")

    def test_an_absent_table_removes_nothing(self):
        self.converge(THREE)
        self.sys.calls.clear()
        self.write_profile('schema = 1\n[host]\nid = "t"\n')
        self.assertEqual(self.run_pass(), sa.EXIT_OK)
        self.assertEqual(self.sys.mutations(), [])
        self.assertTrue(all(self.plist(n).exists() for n in THREE))

    def test_no_previous_receipt_removes_nothing(self):
        for name in THREE:
            self.write_profile(table(THREE))
            self.install_rendered(name)
        self.converge(["reclaim"])
        self.assertEqual(self.receipt()["dropped"], [])
        self.assertTrue(self.plist("keychain-unlock").exists())

    def test_a_receipt_from_an_absent_table_is_not_a_declaration(self):
        self.write_profile('schema = 1\n[host]\nid = "t"\n')
        self.run_pass()
        self.assertFalse(self.receipt()["table_present"])
        self.assertIsNone(self.receipt()["table_sha256"])
        for name in THREE:
            self.write_profile(table(THREE))
            self.install_rendered(name)
        self.converge(["reclaim"])
        self.assertEqual(self.receipt()["dropped"], [])

    def test_a_receipt_without_a_present_table_digest_is_never_a_declaration(self):
        # A receipt whose table was absent, or that carries no digest, cannot
        # testify that anything was declared, whatever its declared list says.
        for forged in ({"table_present": False, "table_sha256": None},
                       {"table_present": True, "table_sha256": None}):
            with self.subTest(forged=forged):
                self.converge(THREE)
                receipt = dict(self.receipt(), **forged)
                (self.state / "last.json").write_text(json.dumps(receipt))
                self.converge(["reclaim"])
                self.assertEqual(self.receipt()["dropped"], [])
                self.assertTrue(self.plist("keychain-unlock").exists())

    def test_an_older_snapshot_receipt_drops_only_what_the_new_table_removed(self):
        self.converge(THREE)
        older = self.receipt()["table_sha256"]
        self.converge(list(reversed(THREE)))       # a different present table, same agents
        self.assertNotEqual(self.receipt()["table_sha256"], older)
        self.assertEqual(self.receipt()["dropped"], [])
        self.converge(["artifact-cache-refresh", "reclaim"])
        self.assertEqual([d["name"] for d in self.receipt()["dropped"]], ["keychain-unlock"])

    def test_the_switch_off_drops_nothing(self):
        self.converge(THREE)
        self.sys.calls.clear()
        self.converge(["reclaim"], bootstrap=False)
        self.assertEqual(self.receipt()["dropped"], [])
        self.assertEqual(self.sys.mutations(), [])


@unittest.skipIf(sa.tomllib is None, "needs tomllib")
class Undeclared(Case):
    def test_reported_never_touched(self):
        agents = self.home / "Library" / "LaunchAgents"
        for label in ("com.danielraffel.tmp.interval-kick", "com.danielraffel.tartci.mystery"):
            (agents / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label}))
        ignored = {
            "com.danielraffel.tartci.tart-runner-macos-fleet.t.pulp-gate": "lane",
            "com.danielraffel.tartci.self-update": "other owner",
            "com.pulp.host-vitals": "outside the prefixes",
        }
        for label in ignored:
            (agents / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label}))
        (agents / "com.danielraffel.tartci.reap.plist.bak-2026").write_bytes(
            plistlib.dumps({"Label": "com.danielraffel.tartci.reap"}))
        self.write_profile(table(["reclaim"], bootstrap=True))
        self.run_pass()
        found = sorted(u["label"] for u in self.receipt()["undeclared"])
        self.assertEqual(found, ["com.danielraffel.tartci.mystery",
                                 "com.danielraffel.tmp.interval-kick"])
        self.assertTrue((agents / "com.danielraffel.tmp.interval-kick.plist").exists())
        self.assertFalse(any("interval-kick" in " ".join(c) or "mystery" in " ".join(c)
                             for c in self.sys.calls))


class Registry(unittest.TestCase):
    def test_every_tartci_template_is_a_registry_entry_or_named_owner(self):
        labels = sorted(p.name[:-len(".plist.template")]
                        for p in (ROOT / "launchd").glob("*.plist.template"))
        self.assertTrue(labels, "control: no templates found")
        registered = {a.label for a in sa.REGISTRY.values()}
        for label in labels:
            if label.startswith("com.danielraffel.tartci."):
                with self.subTest(label=label):
                    self.assertEqual(int(label in registered) + int(label in sa.OTHER_OWNERS), 1,
                                     f"{label} must be exactly one of a registry entry or "
                                     "an OTHER_OWNERS entry")

    def test_nothing_of_ours_ships_under_tmp(self):
        labels = [p.name for p in (ROOT / "launchd").glob("*.plist.template")]
        self.assertTrue(labels)
        self.assertFalse([l for l in labels if l.startswith("com.danielraffel.tmp.")])

    def test_every_registry_entry_has_its_template_and_installer(self):
        for name, agent in sa.REGISTRY.items():
            with self.subTest(name=name):
                self.assertTrue(agent.template.is_file())
                if agent.installer is not None:
                    self.assertTrue((ROOT / agent.installer).is_file())
                run_at_load = plistlib.loads(
                    __import__("re").sub(rb"<!--.*?-->", b"", agent.template.read_bytes(),
                                         flags=__import__("re").DOTALL)).get("RunAtLoad")
                self.assertEqual(agent.kickstart, bool(run_at_load),
                                 "kickstart must follow the template's RunAtLoad, as the installer does")

    def test_other_owners_name_a_path_that_exists(self):
        for label, owner in sa.OTHER_OWNERS.items():
            with self.subTest(label=label):
                self.assertTrue((ROOT / owner.split(" ")[0]).exists(), owner)


@unittest.skipIf(sa.tomllib is None, "needs tomllib")
class RenderParity(unittest.TestCase):
    """The registry renders each agent byte-identically to its install script."""

    def test_the_canary_renders_from_its_template_and_is_never_kickstarted(self):
        agent = sa.REGISTRY["reuse-canary"]
        self.assertIsNone(agent.installer)
        self.assertFalse(agent.kickstart)
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            rendered, err = sa.System(home).render(agent, {})
            self.assertIsNotNone(rendered, err)
            spec = plistlib.loads(rendered)
            self.assertEqual(spec["Label"], "com.danielraffel.tartci.reuse-canary")
            self.assertEqual(spec["ProgramArguments"],
                             ["/bin/bash", f"{home}/.local/bin/tartci", "reuse-canary", "run"])
            self.assertIs(spec["RunAtLoad"], False)

    def test_byte_identical_to_each_installer(self):
        with_installer = {n: a for n, a in sa.REGISTRY.items() if a.installer}
        self.assertEqual(len(with_installer), 4, "control: the four installers")
        for name, agent in with_installer.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                home = root / "home"
                agents = home / "Library" / "LaunchAgents"
                agents.mkdir(parents=True)
                calls = root / "calls"
                double = root / "launchctl"
                loaded = root / "loaded"
                double.write_text(
                    f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{calls}'\n"
                    f"case \"$1\" in print) [ -f '{loaded}' ] || exit 1; "
                    f"printf '\\tpath = %s\\n' \"$(cat '{loaded}')\" ;; "
                    f"bootstrap) printf '%s' \"$3\" > '{loaded}' ;; esac\nexit 0\n")
                double.chmod(0o755)
                profile = root / "profile.toml"
                profile.write_text(table(FOUR, backstop="live"))
                env = dict(os.environ, HOME=str(home), TARTCI_AGENTS_DIR=str(agents),
                           TARTCI_LAUNCHCTL_BIN=str(double), TARTCI_FLEET_PROFILE=str(profile))
                res = subprocess.run(["bash", str(ROOT / agent.installer), "--install"], cwd=ROOT,
                                     env=env, capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                installed = (agents / f"{agent.label}.plist").read_bytes()
                data, _ = sa.load_profile(profile)
                rendered, err = sa.System(home).render(agent, data)
                self.assertEqual(installed, rendered, err)


@unittest.skipIf(sa.tomllib is None, "needs tomllib")
class Validate(unittest.TestCase):
    def problems(self, text: str) -> List[str]:
        import tomllib
        return sa.validate(tomllib.loads(text))

    def test_rules(self):
        self.assertEqual(self.problems(table(THREE)), [])
        self.assertEqual(self.problems(table(FOUR, backstop="live")), [])
        self.assertEqual(self.problems('schema = 1\n'), [])
        self.assertTrue(self.problems(table(["reclaim", "nope"])))
        self.assertTrue(self.problems(table(["reclaim", "reclaim"])))
        self.assertTrue(self.problems(table(THREE).replace("bootstrap = false", 'bootstrap = "no"')))
        self.assertEqual(self.problems(table(THREE).replace(
            "bootstrap = false", 'bootstrap = ["reclaim"]')), [])
        for bad in ('[]', '["reap"]', '["reclaim", "reclaim"]', '[1]'):
            self.assertTrue(self.problems(table(THREE).replace(
                "bootstrap = false", f"bootstrap = {bad}")), bad)
        self.assertTrue(self.problems(table(THREE).replace("bootstrap = false", "extra = 1")))
        self.assertTrue(self.problems(table(FOUR)), "backstop declared while off")
        self.assertTrue(self.problems(table(THREE, backstop="live")), "backstop live, undeclared")
        canary = '[reuse_canary]\nenabled = true\nrepo = "/r"\nworktrees_root = "/w"\n'
        self.assertEqual(self.problems(table(THREE + ["reuse-canary"]) + canary), [])
        self.assertTrue(self.problems(table(THREE) + canary), "canary enabled, undeclared")
        self.assertTrue(self.problems(table(THREE + ["reuse-canary"])), "canary declared, not enabled")

    def test_repo_profiles_declare_and_validate(self):
        profiles = sorted((ROOT / "profiles").glob("*-macos-fleet.toml"))
        self.assertTrue(profiles, "control: no fleet profiles")
        for path in profiles:
            with self.subTest(path=path.name):
                data, why = sa.load_profile(path)
                self.assertIsNotNone(data, why)
                # Whole-table bootstrap stays off until approved; the VM
                # janitor alone is converged everywhere.
                self.assertEqual(data[sa.TABLE]["bootstrap"], ["reap"],
                                 "only the VM janitor is bootstrapped")
                self.assertTrue(set(THREE) <= set(data[sa.TABLE]["declared"]))
                canary = (data.get("reuse_canary") or {}).get("enabled") is True
                self.assertEqual(canary, "reuse-canary" in data[sa.TABLE]["declared"])
                self.assertEqual(canary, path.name in ("m3-macos-fleet.toml", "m1-macos-fleet.toml"))
                res = subprocess.run([sys.executable, "scripts/macos_fleet_lanes.py", "validate",
                                      str(path)], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, res.stderr)

    def test_the_fleet_validator_rejects_a_bad_table(self):
        source = (ROOT / "profiles" / "m5-macos-fleet.toml").read_text()
        good = 'declared = ["reclaim", "artifact-cache-refresh", "keychain-unlock", "launchd-watchdog", "reap"]'
        self.assertIn(good, source)
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.toml"
            bad.write_text(source.replace(good, 'declared = ["reclaim", "nope"]'))
            res = subprocess.run([sys.executable, "scripts/macos_fleet_lanes.py", "validate",
                                  str(bad)], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("support_agents.declared has unknown agents", res.stderr)

    def test_an_unreadable_profile_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "p.toml"
            path.write_text(table(["nope"]))
            home = Path(td) / "home"
            (home / "Library" / "LaunchAgents").mkdir(parents=True)
            code = sa.Converger(FakeSystem(home), path, Path(td) / "state").run("auto")
            self.assertEqual(code, sa.EXIT_REFUSED)
            self.assertEqual(sa.status(Path(td) / "state")["state"], "unreadable")


@unittest.skipIf(sa.tomllib is None, "needs tomllib")
class CheckTemplates(Case):
    def test_renders_or_refuses(self):
        self.write_profile(table(THREE))
        self.assertTrue(sa.check_templates(self.profile, self.sys)[0])
        self.sys.fail_render.add(sa.REGISTRY["reclaim"].label)
        ok, why = sa.check_templates(self.profile, self.sys)
        self.assertFalse(ok)
        self.assertIn("reclaim", why)


class Doctor(unittest.TestCase):
    def test_codes(self):
        reasons = fleet_doctor.load_reasons()
        cases = {
            "ok": ("ok", "support_agents_ok"),
            "pending": ("unknown", "support_agents_pending"),
            "drift": ("problem", "support_agents_drift"),
            "never": ("unknown", "support_agents_never"),
            "unreadable": ("unknown", "support_agents_unreadable"),
        }
        for state, (verdict, code) in cases.items():
            with self.subTest(state=state):
                found, extra = fleet_doctor.check_support_agents({"state": state})
                self.assertEqual((found.state, found.code), (verdict, code))
                for f in (found, extra):
                    self.assertIn(f.code, fleet_doctor.CODES)
                    self.assertIn(f.code, reasons)
        found, extra = fleet_doctor.check_support_agents(
            {"state": "ok", "undeclared": [{"label": "com.danielraffel.tmp.interval-kick"}]})
        self.assertEqual((extra.state, extra.code), ("problem", "undeclared_fleet_agent"))
        self.assertIn("interval-kick", extra.detail)

    def test_collect_reports_both(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            (home / ".config" / "tartci").mkdir(parents=True)
            (home / "Library" / "LaunchAgents").mkdir(parents=True)
            rows = fleet_doctor.collect(home=home, skip_census=True,
                                        probe=lambda root: {"error": "stub"})
            checks = {row.check: row.code for row in rows}
            self.assertEqual(checks.get("support_agents"), "support_agents_never")
            self.assertIn("undeclared_fleet_agent", checks)


class Wiring(unittest.TestCase):
    def test_the_verb_reaches_the_module(self):
        body = (ROOT / "tartci").read_text()
        self.assertIn('scripts/support_agents.py" "$@"', body)


if __name__ == "__main__":
    unittest.main()
