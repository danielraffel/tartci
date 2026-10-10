#!/usr/bin/env python3
"""tartci fleet-macos self-update, every branch against a fake System."""

from __future__ import annotations

import testing_support  # noqa: E402
testing_support.skip_module_without_tomllib()
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from unittest import mock
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_self_update as su

ROOT = Path(__file__).resolve().parents[1]
INSTALLED = "1" * 40
T_OLD = "a" * 40      # first-parent commit older than the soak
T_NEW = "b" * 40      # newer, still soaking
NOW = 1_800_000_000.0
FINGERPRINT = "D1:0A:18:4D:5A:20:7E:AA:92:69:55:44:7D:C2:7E:2A:D9:65:DF:B8"
IDENTITY = FINGERPRINT.replace(":", "")
PUBLISHED = {"schema": "tartci.advertised-labels/v1", "registrations": [
    {"host_id": "m1"}, {"host_id": "studio"}, {"host_id": "m5"}],
    "hosts": [{"host_id": "m1", "ssh": "m1"}, {"host_id": "studio", "ssh": "m3"},
              {"host_id": "m5", "ssh": "m5"}]}
FAKE_GH = "fakegh-cli"


def ok(out: str = "", err: str = "") -> su.Result:
    return su.Result(0, out, err)


class FakeSystem(su.System):
    """Scripted command results; records every call. sleep() advances time."""

    def __init__(self, home: Path, *, sealed: bool = False) -> None:
        self.home, self.sealed = home, sealed
        self.clock = NOW
        self.calls: list[tuple[list[str], str | None]] = []
        self.pool_state = "on"
        self.offplan = [0]             # successive `pool off --plan` exit codes
        self.install_rcs = [0]         # successive `install --apply` exit codes
        self.floor = {"allowed": True, "reason": "", "findings": []}
        # Keyed by SSH target (what the peer resolves to), not host_id.
        self.peers = {"m1": {"state": "on", "participating": True},
                      "m5": {"state": "on", "participating": True},
                      "m3": {"state": "on", "participating": True}}
        self.peer_status_errors: dict[str, str] = {}
        self.peer_markers: dict[str, dict] = {}
        self.peer_waiting: dict[str, dict] = {}   # a peer's waiting.json ticket
        self.peer_clock: dict[str, float] = {}
        self.peer_pool_since: dict[str, float] = {}   # mtime of a peer's pool-state file
        self.published = json.loads(json.dumps(PUBLISHED))
        self.checks: dict[str, list] = {}
        self.signing_rc = 0
        self.locked = False          # dedicated keychain locked in this session
        self.unlocked = False
        self.unlock_rc = 0
        self.rollback_install_rc = 0
        self.broken_target = False     # the new generation fails verification
        self.broken_previous = False   # ...and so does the restored one
        self.on_rc = 0
        self.off_rc = 0                # `pool off` refusing before it stops anything
        self.on_sleep = None           # called with the seconds of every sleep()
        self.hook = None               # called with argv before dispatch (signal tests)
        self.clone_ok = True
        self.critical: list[list[str]] = []
        self.snapshot_verify_rc = 0
        self.procs: dict[int, str] = {}     # other live processes: pid -> start
        self.on_rcs: list[int] = []         # successive pool on exit codes, then on_rc
        self.on_at: float | None = None
        self.heartbeat_after = 0            # seconds after pool on until heartbeats
        self.status_problems: list = []
        # pool on refused at verify-installed, before any mutation (the
        # OS-drift receipt): the pool keeps whatever state it was in.
        self.refuse_keeps_state = False

        self.log_lines = f"{T_NEW} {int(NOW - 60)}\n{T_OLD} {int(NOW - 7200)}\n"
        self.ancestor = True
        self.relay = {"ok": True, "probe": "relay authenticated"}
        self.status_after_on = {"state": "on", "participating": True,
                                "fleet": {"managed": True, "fleet_ready": True,
                                          "serving": {"blocked": False}}}
        self.guard_rcs = (2, 0)
        self.settling_reads = 0        # status reads that still miss a first heartbeat
        self.settling_code = "heartbeat_missing"
        self.bundle_commit = None      # what the built bundle claims (default: target)
        self.codesign_verify_rc = 0
        self.agents_check_rc = 0
        self.agents_rc = 0
        self.agents_raises = False
        self.vm_verify_rc = 0
        self.vm_verify_calls: list[list[str]] = []
        self.installed_after = None    # commit the host executes after install
        self.checked_out = None
        self.writer_domain_exec = True   # the installed shipyard has the subcommand
        self.audit_holds_domain = False  # a Sandbox E2E audit holds it exclusive
        self.fenced: list[list[str]] = []

    def now(self) -> float:
        return self.clock

    def run_critical(self, argv, *, cwd=None, env=None, timeout=900, record=None):
        self.critical.append(list(argv))
        return self.run(argv, cwd=cwd, env=env, timeout=timeout)

    def process_start(self, pid):
        if pid in self.procs:
            return self.procs[pid]
        return "self-start" if pid == os.getpid() else None

    def group_alive(self, pgid):
        return pgid in self.procs

    def sleep(self, seconds: float) -> None:
        self.clock += seconds
        if self.on_sleep:
            self.on_sleep(seconds)

    def mutations(self) -> list[str]:
        out = []
        for argv, _ in self.calls:
            joined = " ".join(argv)
            for word in ("pool drain", "pool off", "pool on", "--apply"):
                if word in joined and "--plan" not in joined and "status" not in joined:
                    out.append(joined)
                    break
        return out

    def run(self, argv, *, cwd=None, env=None, timeout=900):  # noqa: C901
        self.calls.append((list(argv), cwd))
        a = list(argv)
        joined = " ".join(a)
        if self.hook:
            self.hook(a)
        if a[0] == FAKE_GH:
            sha = a[2].split("/commits/")[1].split("/")[0]
            runs = self.checks.get(sha, [{"name": "lint", "status": "completed",
                                          "conclusion": "success"}])
            return ok(json.dumps({"check_runs": runs}))
        if a[0] == "/usr/bin/ditto":
            shutil.copytree(a[-2], a[-1], symlinks=True)
            return ok()
        if a[0] == "security" and a[1] == "unlock-keychain":
            self.unlocked = self.unlock_rc == 0
            return su.Result(self.unlock_rc, "", "" if self.unlock_rc == 0 else "bad password")
        if a[0] == "codesign" and "--timestamp" in a:
            # The live failure: the dedicated keychain is locked in this
            # session until something unlocks it here.
            rc = self.signing_rc if (self.unlocked or not self.locked) else 1
            return su.Result(rc, "", "" if rc == 0 else "errSecInternalComponent")
        if a[0] == "git":
            if "clone" in a:
                if not self.clone_ok:
                    Path(a[-1]).mkdir(parents=True)  # a half clone
                    return su.Result(128, "", "early EOF")
                (Path(a[-1]) / ".git").mkdir(parents=True)
                return ok()
            if "rev-list" in a:
                return ok(f"{T_NEW}\n{T_OLD}\n{INSTALLED}\n")
            if "rev-parse" in a:
                ref = a[-1].split("^")[0]
                return ok((ref if su.SHA.fullmatch(ref) else T_NEW) + "\n")
            if "cat-file" in a:
                return ok()
            if "merge-base" in a:
                return su.Result(0 if self.ancestor else 1)
            if "log" in a:
                # Honour `base..main` the way git does: commits newer than base.
                base = next((x.split("..")[0] for x in a if ".." in x), None)
                lines = self.log_lines.splitlines(keepends=True)
                cut = next((i for i, line in enumerate(lines) if base and line.startswith(base)),
                           len(lines))
                return ok("".join(lines[:cut]))
            if "show" in a:
                return ok(json.dumps(self.published))
            if "remote" in a:
                return ok(su.REPO_URL + "\n")
            if "checkout" in a:
                self.checked_out = a[-1]
            return ok()
        if a[0] == "ssh":
            # The target is the first argument that is neither an option nor
            # an option's value, wherever the options end.
            i = 1
            while a[i].startswith("-"):
                i += 2 if a[i] in ("-o", "-i", "-p") else 1
            peer = a[i]
            if "pool status" in a[-1]:
                if peer in self.peer_status_errors:
                    return su.Result(255, "", self.peer_status_errors[peer])
                value = self.peers.get(peer)
                return ok(json.dumps(value)) if value else su.Result(255, "", "ssh: unreachable")
            marker = self.peer_markers.get(peer)
            clock = int(self.peer_clock.get(peer, self.clock))
            ticket = self.peer_waiting.get(peer)
            pool_since = self.peer_pool_since.get(peer)
            return ok(f"{clock}\n" + (json.dumps(marker) if marker else "") + "\n"
                      + su.WAITING_SEPARATOR + "\n" + (json.dumps(ticket) if ticket else "")
                      + ("\n" + su.POOL_SINCE_SEPARATOR + "\n" + str(int(pool_since))
                         if pool_since is not None else ""))
        if a[:2] == ["python3", "scripts/capacity_floor.py"]:
            return su.Result(0 if self.floor.get("allowed") else 3, json.dumps(self.floor))
        if a[:2] == ["python3", "scripts/network_profile.py"]:
            return su.Result(0 if self.relay.get("ok") else 6, json.dumps(self.relay))
        if a[:2] == ["python3", "-c"]:
            return ok("f" * 64 + "\n")
        if a[:2] == ["python3", "scripts/macos_launcher_identity.py"]:
            return su.Result(self.snapshot_verify_rc, "{}", "" if self.snapshot_verify_rc == 0
                             else "launcher sha256 does not match approval")
        if a[:2] == ["python3", "scripts/macos_fleet_lanes.py"]:
            return self._render(a)
        if a[:3] == ["python3", "scripts/support_agents.py", "check-templates"]:
            return su.Result(self.agents_check_rc, "4 declared agents render" if
                             self.agents_check_rc == 0 else "reclaim: missing template")
        if a[0] == "codesign" and "--extract-certificates" in joined:
            return ok()
        if a[0] == "codesign" and "--verify" in a:
            return su.Result(self.codesign_verify_rc, "", "" if self.codesign_verify_rc == 0 else "invalid signature")
        if a[0] == "openssl":
            return ok(f"SHA1 Fingerprint={FINGERPRINT}\n")
        if a[0] == "security":
            return ok(f'  1) {IDENTITY} "Developer ID Application: X (95CX6P84C4)"\n')
        if a[:2] == ["bash", "scripts/build_macos_launcher.sh"]:
            return self._build(a)
        if a[0] == str(self.home / ".local" / "bin" / "shipyard"):
            if a[1:] == ["writer-domain-exec", "--help"]:
                return ok() if self.writer_domain_exec else su.Result(2, "", "unrecognized subcommand")
            if a[1] == "writer-domain-exec":
                if self.audit_holds_domain:
                    return su.Result(75, "", "sandbox_writer_domain_overlap: exclusive sandbox "
                                     "audit owns ~/Library/Application Support/shipyard")
                child = a[a.index("--") + 1:]
                self.fenced.append(child)
                return self.run(child, cwd=cwd, env=env, timeout=timeout)
        if a[0] == "./tartci":
            return self._tartci(a[1:])
        if a[0] == str(self.home / ".local" / "bin" / "tartci"):
            return self._shim(a[1:])
        raise AssertionError(f"unexpected command: {joined}")

    # ── fakes for tartci subcommands ────────────────────────────────────
    def _tartci(self, args):
        if args[:2] == ["support-manifest", "write"]:
            manifest = self.home / ".local/share/tartci/update-checkout/.tartci-support-manifest.json"
            (manifest.parent / "scripts").mkdir(parents=True, exist_ok=True)
            (manifest.parent / "scripts" / "x.py").write_text("x\n")
            manifest.write_text(json.dumps({"members": [{"path": "scripts/x.py"}],
                                            "source_commit": T_OLD}))
            return ok()
        if args[:2] == ["fleet-macos", "validate"]:
            return ok("valid")
        if args[:2] == ["fleet-macos", "install"]:
            if "--apply" not in args:
                return ok("dry run")
            if self.checked_out == INSTALLED:  # a rollback reinstall
                if self.rollback_install_rc == 0:
                    self._set_installed(INSTALLED)
                return su.Result(self.rollback_install_rc, "", "rollback install failed")
            rc = self.install_rcs.pop(0) if len(self.install_rcs) > 1 else self.install_rcs[0]
            if rc == 0:
                self._set_installed(self.installed_after or self.checked_out)
            return su.Result(rc, "", "" if rc == 0 else "agents still unloading")
        if args[:2] == ["pool", "undrain"]:
            if self.pool_state != "draining":
                return su.Result(5, "", "not draining")
            self.pool_state = "on"
            self.on_at = self.clock
            return ok("undrained")
        if args[:2] == ["pool", "drain"]:
            self.pool_state = "draining"
            return ok("draining")
        if args[:3] == ["pool", "off", "--plan"]:
            rc = self.offplan.pop(0) if len(self.offplan) > 1 else self.offplan[0]
            return su.Result(rc, "plan")
        if args[:2] == ["pool", "off"]:
            if self.off_rc:
                return su.Result(self.off_rc, "", "refusing pool off: capacity for required label "
                                 "'pulp-build-merge-group' could not be determined: "
                                 "runner_census_timeout")
            self.pool_state = "off"
            return ok("off")
        raise AssertionError(f"unexpected ./tartci {args}")

    def running(self):
        if self.sealed:
            meta = self.home / "libexec/TartCILauncher.app/Contents/Resources/bundle.json"
            return json.loads(meta.read_text())["source_commit"]
        return json.loads((self.home / ".config/tartci/macos-fleet-install.json").read_text())[
            "support"]["source_commit"]

    def _shim(self, args):
        if args[:2] == ["pool", "on"]:
            rc = self.on_rcs.pop(0) if self.on_rcs else self.on_rc
            if rc == 0:
                self.pool_state = "on"
                self.on_at = self.clock
            elif not self.refuse_keeps_state:
                self.pool_state = "off"  # pool on's own rollback closes admission
            return su.Result(rc, "on", "" if rc == 0 else
                             "fleet-macos: loaded persistent LaunchAgent x is not running")
        if args[:2] == ["pool", "status"]:
            if self.pool_state != "on":
                return ok(json.dumps({"state": self.pool_state, "participating": False,
                                      "fleet": {"managed": True, "fleet_ready": False,
                                                "problems": self.status_problems}}))
            if self.on_at is not None and self.clock - self.on_at < self.heartbeat_after:
                return ok(json.dumps({"state": "on", "participating": True,
                                      "fleet": {"managed": True, "fleet_ready": False,
                                                "problems": [{"code": "heartbeat_missing"}]}}))
            if self.status_problems:
                return ok(json.dumps({"state": "on", "participating": True,
                                      "fleet": {"managed": True, "fleet_ready": False,
                                                "problems": self.status_problems}}))
            if (self.broken_target and self.running() != INSTALLED) or \
                    (self.broken_previous and self.running() == INSTALLED):
                return ok(json.dumps({"state": "on", "participating": True,
                                      "fleet": {"managed": True, "fleet_ready": False,
                                                "problems": ["broken"]}}))
            if self.settling_reads > 0:
                self.settling_reads -= 1
                return ok(json.dumps({"state": "on", "participating": True,
                                      "fleet": {"managed": True, "fleet_ready": False,
                                                "problems": [{"code": self.settling_code,
                                                              "label": "lane"}]}}))
            return ok(json.dumps(self.status_after_on))
        if args[:2] == ["launchd", "guard"]:
            return su.Result(self.guard_rcs[0] if "kickstart" in args[-1] else self.guard_rcs[1])
        if args[:3] == ["fleet-macos", "support-agents", "auto"]:
            if self.agents_raises:
                raise OSError("shim vanished")
            return su.Result(self.agents_rc, "support agents: ok" if self.agents_rc == 0
                             else "support agents: drift")
        if args[:2] == ["vm-dhcp", "verify"]:
            self.vm_verify_calls.append(list(args))
            return su.Result(self.vm_verify_rc, '{"action": "verifying"}' if self.vm_verify_rc == 0
                             else "breaker unreadable")
        raise AssertionError(f"unexpected shim {args}")

    def _set_installed(self, commit):
        if self.sealed:
            meta = self.home / "libexec/TartCILauncher.app/Contents/Resources/bundle.json"
            meta.write_text(json.dumps({"source_commit": commit}))
        else:
            receipt = self.home / ".config/tartci/macos-fleet-install.json"
            receipt.write_text(json.dumps({"support": {"source_commit": commit}}))

    def _render(self, a):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/macos_fleet_lanes.py"), *a[2:]],
                              capture_output=True, text=True)
        return su.Result(proc.returncode, proc.stdout, proc.stderr)

    def _build(self, a):
        out = Path(a[a.index("--output") + 1])
        approval = Path(a[a.index("--approval-output") + 1])
        profile = Path(a[a.index("--profile") + 1])
        rendered = out.parent / "render"
        subprocess.run([sys.executable, str(ROOT / "scripts/macos_fleet_lanes.py"), "render",
                        str(profile), "--output", str(rendered)], check=True, capture_output=True)
        lanes = {}
        for plist in rendered.glob("*.plist"):
            env = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]
            lanes[env["TARTCI_QUEUE_LANE_ID"]] = {"environment": dict(sorted(env.items()))}
        res = out / "Contents" / "Resources"
        res.mkdir(parents=True)
        (res / "lanes.json").write_text(json.dumps({"schema": 1, "lanes": lanes}))
        (res / "bundle.json").write_text(json.dumps({"source_commit": self.bundle_commit or self.checked_out}))
        approval.write_text("c" * 64 + "\n")
        return su.Result(0, "", "rm: cannot remove staging: Permission denied")


def make_home(td: Path, *, sealed: bool = False, relay: bool = False) -> Path:
    home = td / "home"
    config = home / ".config" / "tartci"
    config.mkdir(parents=True)
    profile = (ROOT / "profiles" / ("m3-macos-fleet.toml" if sealed else "m1-macos-fleet.toml")).read_text()
    checkout = home / ".local/share/tartci/update-checkout"
    (checkout / ".git").mkdir(parents=True)
    shutil.copytree(ROOT / "profiles", checkout / "profiles")
    if sealed:
        live = home / "libexec" / "TartCILauncher.app"
        (live / "Contents" / "Resources").mkdir(parents=True)
        (live / "Contents/Resources/bundle.json").write_text(json.dumps({"source_commit": INSTALLED}))
        pin = config / "m3-launcher-approved.sha256"
        pin.write_text("0" * 64 + "\n")
        os.chmod(pin, 0o600)
        # The profile keeps its validated real paths (load() pins them); the
        # tests redirect the helper through su.launch_helper instead.
    else:
        (config / "macos-fleet-install.json").write_text(
            json.dumps({"support": {"source_commit": INSTALLED}}))
    (config / "macos-fleet-profile.toml").write_text(profile)
    if relay:
        (config / "network-profile.toml").write_text("[http_connect_relay]\nenabled = true\n")
    return home


class Base(unittest.TestCase):
    sealed = False
    relay = False

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.td.name), sealed=self.sealed, relay=self.relay)
        os.environ["TARTCI_HOME"] = str(self.home / ".tartci")
        os.environ["TARTCI_GH_CLI"] = FAKE_GH
        # No [peers]: targets come from the published supply.
        self.cfg = su.Config(home=self.home, poll_seconds=45, wait_seconds=600)
        self.sys = FakeSystem(self.home, sealed=self.sealed)
        # Starvation opens and closes a GitHub issue through host_off; record,
        # never call ghapp.
        import host_off
        self.issues: list[tuple[str, str]] = []
        for name, fake in (("_open_issue", lambda t, b: self.issues.append(("open", t)) or (0, "77")),
                           ("_close_issue", lambda n: self.issues.append(("close", n)) or (0, "closed"))):
            original = getattr(host_off, name)
            setattr(host_off, name, fake)
            self.addCleanup(setattr, host_off, name, original)
        if self.sealed:
            helper = {"path": str(self.home / "libexec" / "TartCILauncher.app"),
                      "approval_sha256_path": str(self.home / ".config/tartci/m3-launcher-approved.sha256")}
            original = su.launch_helper
            su.launch_helper = lambda cfg: helper
            self.addCleanup(setattr, su, "launch_helper", original)

    def tearDown(self) -> None:
        os.environ.pop("TARTCI_HOME", None)
        os.environ.pop("TARTCI_GH_CLI", None)
        for current, dirs, files in os.walk(self.td.name):
            for name in dirs:
                os.chmod(os.path.join(current, name), 0o755)
        self.td.cleanup()

    def apply(self) -> int:
        return su.plan_or_apply(self.cfg, self.sys, apply=True, target_ref="origin/main")

    def plan(self) -> int:
        return su.plan_or_apply(self.cfg, self.sys, apply=False, target_ref="origin/main")

    def last(self) -> dict:
        return json.loads((self.cfg.state_dir / "last.json").read_text())

    def assertUpdated(self, rc: int) -> None:
        """Took its turn and updated. EXIT_DEFERRED is also 0, so rc alone cannot say."""
        self.assertEqual(rc, su.EXIT_OK)
        self.assertEqual(self.last()["status"], "succeeded", self.last())

    def assertDeferred(self, rc: int) -> None:
        """Deferred to a peer: exit 0, nothing mutated, a place kept in the queue."""
        self.assertEqual(rc, su.EXIT_DEFERRED)
        ticket = json.loads((self.cfg.state_dir / "waiting.json").read_text())
        self.assertTrue(ticket.get("reason"), ticket)


class SkewTests(Base):
    def test_target_is_newest_first_parent_commit_past_the_soak(self) -> None:
        skew = su.measure_skew(self.cfg, self.sys, INSTALLED, NOW)
        self.assertEqual((skew["state"], skew["behind"], skew["target"]), ("behind", 2, T_OLD))
        log = next(a for a, _ in self.sys.calls if a[:1] == ["git"] and "log" in a)
        self.assertIn("--first-parent", log)

    def test_all_soaking_does_nothing(self) -> None:
        self.sys.log_lines = f"{T_NEW} {int(NOW - 60)}\n"
        self.assertEqual(self.apply(), su.EXIT_NOTHING)
        self.assertEqual(self.sys.mutations(), [])
        self.assertEqual(json.loads((self.cfg.state_dir / "skew.json").read_text())["state"], "soaking")

    def test_current_does_nothing(self) -> None:
        self.sys.log_lines = ""
        self.assertEqual(self.apply(), su.EXIT_NOTHING)
        self.assertEqual(self.sys.mutations(), [])

    def test_unknown_and_diverged_fail_closed(self) -> None:
        self.sys.ancestor = False
        self.assertEqual(self.apply(), su.EXIT_UNKNOWN)
        self.assertEqual(self.sys.mutations(), [])
        (self.home / ".config/tartci/macos-fleet-install.json").write_text("{}")
        self.assertEqual(self.apply(), su.EXIT_UNKNOWN)

    def test_profile_without_install_receipt_is_not_managed(self) -> None:
        (self.home / ".config/tartci/macos-fleet-install.json").unlink()
        self.assertEqual(self.plan(), su.EXIT_UNKNOWN)
        skew = json.loads((self.cfg.state_dir / "skew.json").read_text())
        self.assertEqual(skew["state"], "not_applicable")
        self.assertIsNone(su.summary(self.home)["problem"])
        self.assertIn("skew n/a", su.render_skew(skew))
        # Control: a receipt without a commit is a managed host, and unknown.
        (self.home / ".config/tartci/macos-fleet-install.json").write_text("{}")
        self.assertEqual(self.plan(), su.EXIT_UNKNOWN)
        self.assertIn("unknown", su.summary(self.home)["problem"])

    def test_stale_flag_and_render(self) -> None:
        self.sys.log_lines = f"{T_OLD} {int(NOW - 3 * 86400)}\n"
        skew = su.measure_skew(self.cfg, self.sys, INSTALLED, NOW)
        self.assertTrue(skew["stale"])
        self.assertIn("1 commits behind main", su.render_skew(skew))
        self.assertIn("STALE", su.render_skew(skew))
        self.assertIn("UNKNOWN", su.render_skew(None))


class SkewAfterApplyTests(Base):
    """A verified apply records skew for the generation it installed.

    m3, 2026-09-28: skew.json was measured at 05:30:43Z, the update to the
    newest commit was verified at 05:39Z, and status went on reading "1 commits
    behind main" because skew.json is otherwise written only before the run.
    """

    def skew(self) -> dict:
        return json.loads((self.cfg.state_dir / "skew.json").read_text())

    def test_a_verified_apply_rewrites_skew_for_the_installed_target(self) -> None:
        self.assertUpdated(self.apply())
        skew = self.skew()
        self.assertEqual((skew["installed"], skew["recorded_by"]), (T_OLD, "verified_apply"))
        # Only the still-soaking commit is ahead now, not the one just installed.
        self.assertEqual(skew["behind"], 1)
        steps = [step["step"] for step in json.loads(
            Path(self.last()["receipt"]).read_text())["steps"]]
        self.assertIn("skew", steps)

    def test_the_record_does_not_requery_checks(self) -> None:
        # Verification already proved this generation runs; the record must not
        # depend on check runs, which can be slow, rate-limited or red later.
        real, kwargs_seen = su.measure_skew, []

        def spy(*args, **kwargs):
            kwargs_seen.append(kwargs)
            return real(*args, **kwargs)

        with mock.patch.object(su, "measure_skew", side_effect=spy):
            self.assertUpdated(self.apply())
        self.assertEqual(kwargs_seen[-1].get("verify_checks"), False)

    def test_a_failed_skew_record_never_fails_the_update(self) -> None:
        real, calls = su.measure_skew, []

        def second_call_fails(*args, **kwargs):
            calls.append(args)
            if len(calls) > 1:
                raise OSError("disk")
            return real(*args, **kwargs)

        with mock.patch.object(su, "measure_skew", side_effect=second_call_fails):
            self.assertUpdated(self.apply())
        self.assertEqual(len(calls), 2)


class OrchestratorGenerationTests(Base):
    """Which code ran an update is recorded beside what it installed.

    The installed generation orchestrates; the target's code runs only for
    support-manifest, validate, install and the template check. So a change to
    orchestration takes effect one update late, and the receipt must show it.
    """

    def test_a_receipt_names_the_orchestrating_generation_beside_the_target(self) -> None:
        with mock.patch.object(su, "orchestrator_generation", return_value=INSTALLED):
            self.assertUpdated(self.apply())
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        self.assertEqual((receipt["orchestrator_generation"], receipt["target"]),
                         (INSTALLED, T_OLD))
        self.assertNotEqual(receipt["orchestrator_generation"], receipt["target"])

    def test_the_generation_is_read_from_the_running_code_path(self) -> None:
        gen = Path(self.td.name) / ".local/share/tartci-generations" / \
            f"{'c' * 40}-4757141035587a83" / "scripts"
        gen.mkdir(parents=True)
        self.assertEqual(su.orchestrator_generation(gen / "fleet_self_update.py"), "c" * 40)
        self.assertIsNone(su.orchestrator_generation(Path(self.td.name) / "checkout/x.py"))


class ReserveCheckTests(Base):
    def test_the_target_is_validated_with_the_gate_reserve_ratchet(self) -> None:
        self.assertUpdated(self.apply())
        validates = [a for a, _ in self.sys.calls if a[:3] == ["./tartci", "fleet-macos", "validate"]]
        self.assertTrue(validates)
        self.assertTrue(all("--check-reserve" in a for a in validates), validates)


class HappyPathTests(Base):
    def test_apply_runs_the_procedure_in_order_and_verifies(self) -> None:
        self.assertEqual(self.apply(), su.EXIT_OK, self.sys.calls[-5:])
        joined = [" ".join(a) for a, _ in self.sys.calls]
        def first(pred):
            return next(i for i, c in enumerate(joined) if pred(c))
        order = [first(lambda c, n=n: n in c) for n in (
            "support-manifest write", "fleet-macos validate", "fleet-macos install", "ssh",
            "capacity_floor.py", "pool drain", "pool off --plan")]
        order.append(first(lambda c: c.startswith("./tartci pool off") and "--plan" not in c))
        order += [first(lambda c, n=n: n in c) for n in ("--apply", "pool on")]
        order.append(first(lambda c: "pool status" in c and not c.startswith("ssh")))
        self.assertEqual(order, sorted(order))
        self.assertEqual(self.last()["status"], "succeeded")
        self.assertFalse((self.cfg.state_dir / "active.json").exists())
        # pool on and status go through the INSTALLED shim from $HOME.
        shim_calls = [(a, cwd) for a, cwd in self.sys.calls if a[0].endswith(".local/bin/tartci")]
        self.assertTrue(shim_calls and all(cwd == str(self.home) for _, cwd in shim_calls))
        # drain etc. run from the managed checkout with the App CLI.
        drain = next(cwd for a, cwd in self.sys.calls if a[:3] == ["./tartci", "pool", "drain"])
        self.assertEqual(drain, str(self.cfg.checkout))

    def test_plan_is_read_only(self) -> None:
        self.assertEqual(self.plan(), su.EXIT_OK)
        self.assertEqual(self.sys.mutations(), [])
        self.assertFalse((self.cfg.state_dir / "last.json").exists())


class OneAtATimeTests(Base):
    def test_peer_authentication_failure_is_named_separately(self) -> None:
        self.sys.peer_status_errors["m5"] = "Permission denied (publickey)."
        peer = su.read_peer(self.cfg, self.sys, "m5", "m5")
        self.assertFalse(peer["readable"])
        self.assertIn("SSH authentication failed", peer["evidence"])
        self.assertNotIn("unreachable", peer["evidence"])

    def test_peer_draining_refuses_before_any_mutation(self) -> None:
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertDeferred(self.apply())
        self.assertEqual(self.sys.mutations(), [])

    def test_peer_updating_or_unreachable_or_unmapped_refuses(self) -> None:
        # This fake host is m1, so m5 and studio are its peers.
        self.sys.peer_markers["m5"] = {"host_id": "m5", "target": T_OLD, "ts": NOW - 60}
        self.assertDeferred(self.apply())
        self.sys.peer_markers.clear()
        del self.sys.peers["m5"]  # unreachable
        self.assertDeferred(self.apply())
        self.assertEqual(self.sys.mutations(), [])

    def test_stale_peer_marker_is_ignored(self) -> None:
        self.sys.peer_markers["m5"] = {"host_id": "m5", "target": T_OLD, "ts": NOW - 4 * 3600}
        self.assertUpdated(self.apply())

    def test_marker_age_uses_the_peer_clock(self) -> None:
        # The peer wrote its marker a minute ago on ITS clock, which runs 5h
        # behind ours; comparing against our clock would call it stale.
        self.sys.peer_clock["m5"] = NOW - 5 * 3600
        self.sys.peer_markers["m5"] = {"host_id": "m5", "target": T_OLD, "ts": NOW - 5 * 3600 - 60}
        self.assertDeferred(self.apply())
        self.assertEqual(self.sys.mutations(), [])

    def test_stagger_is_stable_and_bounded(self) -> None:
        self.assertEqual(su.stagger_seconds("m5"), su.stagger_seconds("m5"))
        self.assertTrue(0 <= su.stagger_seconds("studio") < 600)


class FloorTests(Base):
    def test_idle_by_design_last_server_passes_the_flag_and_logs_the_rule(self) -> None:
        self.sys.floor = {"allowed": False, "reason": "last_serving_host", "findings": [
            {"label": "pulp-release-tagged", "verdict": "last_serving_host"}]}
        self.assertUpdated(self.apply())
        drain = next(a for a, _ in self.sys.calls if a[:3] == ["./tartci", "pool", "drain"])
        self.assertIn("--allow-last-serving-host", drain)
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        self.assertTrue(any("idle-by-design" in s["detail"] for s in receipt["steps"]))

    def test_other_last_server_and_unknown_refuse(self) -> None:
        self.sys.floor = {"allowed": False, "reason": "last_serving_host", "findings": [
            {"label": "pulp-build-pr-head", "verdict": "last_serving_host"}]}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.sys.floor = {"allowed": False, "reason": "capacity_unknown", "findings": []}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])


def healthy_peer(**fleet_overrides) -> dict:
    fleet = {"managed": True, "fleet_ready": True, "problems": [],
             "expected_supervisors": 5, "verified_running_supervisors": 5,
             "serving": {"blocked": False, "blocked_lanes": [], "unmeasurable_lanes": []},
             "config": {"supply": {"state": "match"}}}
    fleet.update(fleet_overrides)
    return {"state": "on", "participating": True, "fleet": fleet}


class OnDemandSupplyTests(Base):
    """A JIT fleet registers runners only while they hold jobs, so being the
    only host with a registered runner is the idle norm. A label another host
    publishes and can mint on demand is still served."""

    LABEL = "pulp-build-pr-head"

    def setUp(self) -> None:
        super().setUp()
        self.sys.floor = {"allowed": False, "reason": "last_serving_host", "findings": [
            {"repo": "Generous-Corp/pulp", "label": self.LABEL, "verdict": "last_serving_host"}]}
        self.sys.published["registrations"] = [
            {"host_id": "m1", "repo": "Generous-Corp/pulp", "labels": ["pulp-build", self.LABEL]},
            {"host_id": "m5", "repo": "Generous-Corp/pulp", "labels": ["pulp-build", self.LABEL]},
            {"host_id": "studio", "repo": "Generous-Corp/pulp", "labels": ["pulp-build"]},
        ]
        self.sys.peers["m5"] = healthy_peer()
        self.sys.peers["m3"] = healthy_peer()

    def test_healthy_peer_publishing_the_label_lets_the_update_proceed(self) -> None:
        self.assertUpdated(self.apply())
        drain = next(a for a, _ in self.sys.calls if a[:3] == ["./tartci", "pool", "drain"])
        self.assertIn("--allow-last-serving-host", drain)
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        floor = next(s for s in receipt["steps"] if s["step"] == "capacity-floor")
        self.assertIn("on-demand supply", floor["detail"])
        self.assertIn(f"{self.LABEL} by m5", floor["detail"])

    def assertRefusedWithoutMutation(self, fragment: str) -> None:
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])
        newest = max((self.cfg.state_dir / "attempts").glob("*.json"),
                     key=lambda path: path.stat().st_mtime_ns)
        self.assertIn(fragment, json.loads(newest.read_text()).get("error") or "")

    def test_no_other_host_publishes_the_label(self) -> None:
        self.sys.published["registrations"] = [
            r for r in self.sys.published["registrations"] if r["host_id"] != "m5"]
        self.assertRefusedWithoutMutation("no other host publishes")

    def test_peer_publishing_it_for_another_repo_does_not_count(self) -> None:
        self.sys.published["registrations"][1]["repo"] = "Generous-Corp/forge"
        self.assertRefusedWithoutMutation("no other host publishes")

    def test_peer_not_fleet_ready(self) -> None:
        self.sys.peers["m5"] = healthy_peer(fleet_ready=False)
        self.assertRefusedWithoutMutation("fleet not ready")

    def test_peer_missing_a_supervisor(self) -> None:
        self.sys.peers["m5"] = healthy_peer(verified_running_supervisors=4)
        self.assertRefusedWithoutMutation("supervisors 4/5")

    def test_peer_serving_blocked(self) -> None:
        self.sys.peers["m5"] = healthy_peer(serving={"blocked": True})
        self.assertRefusedWithoutMutation("serving blocked")

    def test_peer_supply_mismatch(self) -> None:
        self.sys.peers["m5"] = healthy_peer(config={"supply": {"state": "mismatch"}})
        self.assertRefusedWithoutMutation("does not match the published supply")

    def test_peer_with_problems(self) -> None:
        self.sys.peers["m5"] = healthy_peer(problems=["lane pulp-gate heartbeat_missing"])
        self.assertRefusedWithoutMutation("fleet problems")

    def test_peer_status_without_fleet_section_fails_closed(self) -> None:
        # An older tartci, or a status the probe cannot read, is not capability.
        self.sys.peers["m5"] = {"state": "on", "participating": True}
        self.assertRefusedWithoutMutation("no fleet section")

    def test_capacity_unknown_still_refuses(self) -> None:
        self.sys.floor = {"allowed": False, "reason": "capacity_unknown", "findings": []}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])

    def test_every_blocked_label_needs_its_own_server(self) -> None:
        self.sys.floor["findings"].append(
            {"repo": "Generous-Corp/pulp", "label": "pulp-build-merge-group",
             "verdict": "last_serving_host"})
        self.assertRefusedWithoutMutation("pulp-build-merge-group")
        self.sys.published["registrations"][1]["labels"].append("pulp-build-merge-group")
        self.assertUpdated(self.apply())

    def test_plan_reports_the_same_decision(self) -> None:
        self.assertEqual(self.plan(), su.EXIT_OK)
        self.sys.peers["m5"] = healthy_peer(fleet_ready=False)
        self.assertEqual(self.plan(), su.EXIT_REFUSED)


class PeerTurnReplayTests(Base):
    """The 2026-10-09 update turn, replayed from the hosts' own records.

    This fake host is m1; its peers are studio (m5studio, ssh m3) and m5.
    Epoch values are the real ones, shifted so the last observation lands on
    NOW.
    """

    OBSERVED = 1791529322             # date +%s when the tickets were read
    SHIFT = NOW - OBSERVED

    def at(self, epoch: float) -> float:
        return epoch + self.SHIFT

    def test_a_peer_draining_for_its_own_update_reads_as_the_turn_holder(self) -> None:
        # m5 at 06:05:54Z read m5studio, which announced at 05:55:18Z and then
        # drained. Read pool state first, it said only "draining".
        self.sys.peers["m3"] = {"state": "draining", "participating": False}
        self.sys.peer_markers["m3"] = {"host_id": "studio",
                                       "target": "1c32310463f2f5ebabed386c4417eb1653539934",
                                       "ts": NOW - 636}
        self.sys.peer_pool_since["m3"] = NOW - 600
        peer = su.read_peer(self.cfg, self.sys, "studio", "m3")
        self.assertTrue(peer["busy"])
        self.assertEqual(peer["evidence"], "peer studio is self-updating to 1c32310463f2 (10 min in)")
        self.assertEqual(peer["active_age"], 636)
        self.assertNotIn("draining", peer["evidence"])
        # The readability fix itself: the turn holder, its target and its age.
        self.assertIn("self-updating to 1c32310463f2", peer["evidence"])
        self.assertIn("(10 min in)", peer["evidence"])

    def test_the_recorded_queue_lets_exactly_one_host_go(self) -> None:
        # waiting.json on m1 and m5studio at 07:02Z, after m5 finished its
        # update and came back on. Both still carried "peer m5 is draining",
        # a reason up to one interval old; the next read must not reuse it.
        su._write_json(self.cfg.state_dir / "waiting.json", {
            "host_id": "m1", "issue": None,
            "reason": "another fleet host is not serving normally: peer m5 is draining "
                      "(participating=False)",
            "since": self.at(1791526495.20782), "starved_evented": None,
            "target": "d4158f38974c181e94efd7eb43c2767c084f533b",
            "ts": self.at(1791528338.536526)})
        studio_ticket = {
            "host_id": "m5studio", "issue": None,
            "reason": "another fleet host is not serving normally: peer m5 is draining "
                      "(participating=False)",
            "since": self.at(1791528023.668664), "starved_evented": None,
            "target": "d4158f38974c181e94efd7eb43c2767c084f533b",
            "ts": self.at(1791528023.668664)}
        self.sys.peer_waiting["m3"] = studio_ticket
        self.sys.peer_pool_since["m5"] = self.at(1791528936)   # m5 back on at 06:55:36Z
        self.sys.peer_pool_since["m3"] = self.at(1791526209)
        # m1 holds the older ticket, so m1 goes...
        self.assertUpdated(self.apply())
        # ...and m5studio, reading m1's ticket, yields to it.
        ahead = su.queue_ahead("studio", studio_ticket["since"],
                               {"m1": self.at(1791526495.20782)})
        self.assertEqual(len(ahead), 1)
        self.assertTrue(ahead[0].startswith("m1 "), ahead)

    def test_a_markerless_drain_older_than_the_bound_no_longer_holds_the_turn(self) -> None:
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.sys.peer_pool_since["m5"] = NOW - su.PEER_DRAIN_STALE_SECONDS - 60
        peer = su.read_peer(self.cfg, self.sys, "m5", "m5")
        self.assertFalse(peer["busy"])
        self.assertTrue(peer["off"])
        self.assertIn("draining for 3.0 h with no update marker", peer["evidence"])
        self.assertUpdated(self.apply())
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        peers_step = next(step for step in receipt["steps"] if step["step"] == "peers")
        self.assertIn("counted as serving nothing by the capacity floor: m5",
                      peers_step["detail"])

    def test_a_recent_markerless_drain_still_holds_the_turn(self) -> None:
        # Control, same instrument: only the drain's age changed.
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.sys.peer_pool_since["m5"] = NOW - 1800
        self.assertDeferred(self.apply())
        self.assertEqual(self.sys.mutations(), [])
        self.assertIn("peer m5 is draining (participating=False), pool state unchanged for 30 min",
                      su.waiting_ticket(self.cfg)["reason"])

    def test_a_drain_of_unknown_age_holds_the_turn(self) -> None:
        # No pool-state time (an old peer, or stat failed): fail closed.
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertDeferred(self.apply())
        self.assertEqual(self.sys.mutations(), [])

    def test_an_update_that_died_after_draining_stops_holding_the_turn(self) -> None:
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.sys.peer_markers["m5"] = {"host_id": "m5", "target": T_OLD, "ts": NOW - 4 * 3600}
        self.sys.peer_pool_since["m5"] = NOW - 4 * 3600 + 60
        self.assertUpdated(self.apply())

    def test_an_expired_marker_on_a_recent_drain_still_holds_the_turn(self) -> None:
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.sys.peer_markers["m5"] = {"host_id": "m5", "target": T_OLD, "ts": NOW - 4 * 3600}
        self.sys.peer_pool_since["m5"] = NOW - 3600
        self.assertDeferred(self.apply())

    def test_an_opted_out_peer_that_is_on_still_holds_the_turn(self) -> None:
        # The drain bound is for draining only, never for a host on but not
        # participating, however long ago its pool state changed.
        self.sys.peers["m5"] = {"state": "on", "participating": False}
        self.sys.peer_pool_since["m5"] = NOW - 30 * 86400
        self.assertDeferred(self.apply())


class UpdateQueueTests(Base):
    """One host at a time, in the order hosts started waiting."""

    def ticket(self, **fields) -> None:
        value = {"host_id": "m1", "target": T_OLD, "since": NOW - 3600, "ts": NOW - 1800,
                 "reason": "peer m5 is draining"}
        value.update(fields)
        su._write_json(self.cfg.state_dir / "waiting.json", value)

    def test_a_peer_that_has_waited_longer_goes_first(self) -> None:
        # m3 on 2026-09-30: refused at every attempt while a peer took each turn.
        self.ticket(since=NOW - 1800)
        self.sys.peer_waiting["m5"] = {"host_id": "m5", "since": NOW - 3 * 3600, "ts": NOW - 60}
        self.assertDeferred(self.apply())
        self.assertEqual(self.sys.mutations(), [])
        self.assertIn("yielding the update turn", su.waiting_ticket(self.cfg)["reason"])

    def test_this_host_goes_first_when_it_has_waited_longer(self) -> None:
        # Control, same instrument: only the order of the two waits changed.
        self.ticket(since=NOW - 3 * 3600)
        self.sys.peer_waiting["m5"] = {"host_id": "m5", "since": NOW - 1800, "ts": NOW - 60}
        self.assertUpdated(self.apply())
        self.assertIsNone(su.waiting_ticket(self.cfg), "a host that had its turn leaves the queue")

    def slow_checkout(self, seconds: float) -> None:
        """The attempt spends `seconds` before it surveys its peers."""
        def hook(argv):
            if argv[:1] == ["git"] and "checkout" in argv:
                self.sys.clock += seconds
        self.sys.hook = hook

    def test_the_earliest_ticket_goes_whatever_its_survey_delay(self) -> None:
        # 2026-10-02: m3 held the earliest ticket, surveyed after a 10 min
        # checkout, and yielded to m5 while m5 yielded back. No host updated.
        mine, theirs = NOW - 12000, NOW - 12000 + 420
        self.ticket(since=mine)
        self.sys.peer_waiting["m5"] = {"host_id": "m5", "since": theirs, "ts": NOW - 60}
        self.slow_checkout(3600)
        self.assertUpdated(self.apply())
        # The same two tickets seen from m5: it yields, so exactly one goes.
        self.assertEqual(su.queue_ahead("m1", mine, {"m5": theirs}), [])
        self.assertEqual(len(su.queue_ahead("m5", theirs, {"m1": mine})), 1)

    def test_a_slow_checkout_still_yields_to_an_earlier_ticket(self) -> None:
        # Control, same instrument: m5 now joined the queue 7 min BEFORE this host.
        self.ticket(since=NOW - 12000)
        self.sys.peer_waiting["m5"] = {"host_id": "m5", "since": NOW - 12000 - 420,
                                       "ts": NOW - 60}
        self.slow_checkout(600)
        self.assertDeferred(self.apply())
        self.assertIn("m5", su.waiting_ticket(self.cfg)["reason"])

    def test_equal_tickets_go_to_the_lower_host_name(self) -> None:
        since = NOW - 3600
        self.assertEqual(su.queue_ahead("m1", since, {"m5": since}), [])
        self.assertEqual(len(su.queue_ahead("m5", since, {"m1": since})), 1)
        self.ticket(since=since)
        self.sys.peer_waiting["m5"] = {"host_id": "m5", "since": since, "ts": NOW - 60}
        self.assertUpdated(self.apply())

    def test_an_off_front_host_does_not_hold_the_turn(self) -> None:
        # m5 joined first but is off: it cannot take a turn, so the next
        # ticket goes, subject to the capacity floor.
        self.ticket(since=NOW - 3600)
        self.sys.peer_waiting["m5"] = {"host_id": "m5", "since": NOW - 9 * 3600, "ts": NOW - 60}
        self.sys.peers["m5"] = {"state": "off", "participating": False}
        self.assertUpdated(self.apply())

    def test_a_ticket_its_host_stopped_refreshing_is_ignored(self) -> None:
        self.sys.peer_waiting["m5"] = {"host_id": "m5", "since": NOW - 9 * 3600,
                                       "ts": NOW - su.WAITING_TICKET_TTL - 60}
        self.assertUpdated(self.apply())

    def test_the_place_in_the_queue_survives_a_new_target(self) -> None:
        self.ticket(since=NOW - 2 * 3600, target="c" * 40)
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertDeferred(self.apply())
        self.assertEqual(su.waiting_ticket(self.cfg)["since"], NOW - 2 * 3600)

    def test_deferral_past_the_bound_is_starvation_and_loud_once(self) -> None:
        self.ticket(since=NOW - su.STARVED_AFTER_SECONDS - 60)
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        events = (self.cfg.state_dir / "events.jsonl").read_text()
        self.assertEqual(events.count("self_update_starved"), 1)
        self.assertTrue(any("STARVED" in line for line in su.status_lines(self.cfg.state_dir)))
        self.assertIn("STARVED", su.summary(self.home)["problem"])

    def test_starvation_opens_one_issue_and_the_next_turn_closes_it(self) -> None:
        # 2026-10-01: three hosts starved for 7-9 h and wrote events nobody read.
        self.ticket(since=NOW - su.STARVED_AFTER_SECONDS - 60)
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual([kind for kind, _ in self.issues], ["open"])
        self.assertIn("self-update starved", self.issues[0][1])
        self.assertEqual(su.waiting_ticket(self.cfg)["issue"], "77")
        self.sys.peers["m5"] = {"state": "on", "participating": True}
        self.assertUpdated(self.apply())
        self.assertEqual(self.issues[-1], ("close", "77"))

    def test_a_short_deferral_is_quiet(self) -> None:
        # Control for the bound: the same deferral an hour in is not a problem.
        self.ticket(since=NOW - 3600)
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertDeferred(self.apply())
        self.assertFalse((self.cfg.state_dir / "events.jsonl").exists()
                         and "self_update_starved" in (self.cfg.state_dir / "events.jsonl").read_text())
        self.assertIsNone(su.summary(self.home)["problem"])
        self.assertEqual(self.issues, [])


class AnnounceOrderTests(Base):
    """Between two self-updating hosts the earlier announcement proceeds."""

    def peer_announces(self, offset: float) -> None:
        # m5 writes its marker while this host (m1) settles after announcing;
        # `offset` is when, relative to this host's announcement.
        def hook(seconds: float) -> None:
            if seconds == su.ANNOUNCE_SETTLE_SECONDS:
                announced = self.sys.clock - seconds
                self.sys.peer_markers["m5"] = {"host_id": "m5", "target": T_OLD,
                                               "ts": announced + offset}
        self.sys.on_sleep = hook

    def test_a_higher_id_that_announced_first_wins(self) -> None:
        # The lower id used to proceed whenever it saw a self-updating peer,
        # even one that had announced a minute earlier and already moved on.
        self.peer_announces(-60)
        self.assertDeferred(self.apply())
        self.assertFalse(any("pool drain" in m for m in self.sys.mutations()))
        self.assertIn("announced first", su.waiting_ticket(self.cfg)["reason"])

    def test_this_host_proceeds_when_it_announced_first(self) -> None:
        self.peer_announces(su.ANNOUNCE_TIE_SECONDS + 5)
        self.assertUpdated(self.apply())

    def test_a_tie_goes_to_the_lower_id(self) -> None:
        self.peer_announces(2)
        self.assertUpdated(self.apply())  # m1 < m5


class PoolOffRefusalTests(Base):
    def test_a_pool_off_refusal_restores_the_host_and_does_not_spend_the_attempt(self) -> None:
        # m3, 2026-09-30 21:12Z: the census timed out inside `pool off`; the run
        # recorded "failed", which spent the 6 h attempt and counted to the halt.
        self.sys.off_rc = 11
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.pool_state, "on")
        receipts = [json.loads(path.read_text())
                    for path in (self.cfg.state_dir / "attempts").glob("*.json")]
        self.assertEqual([r["status"] for r in receipts], ["refused"])
        self.assertIn("pool off refused (exit 11)", receipts[0]["error"])
        self.assertIsNone(su.halt_reason(self.cfg.state_dir))
        self.sys.off_rc = 0
        self.assertUpdated(self.apply())

    def test_a_capacity_refusal_while_waiting_idle_is_a_refusal(self) -> None:
        # m1 on 2026-10-03: drained, waited, then `pool off --plan` exited 11
        # and the attempt was recorded as failed.
        self.sys.offplan = [12, 11]
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.pool_state, "on")
        receipts = [json.loads(path.read_text())
                    for path in (self.cfg.state_dir / "attempts").glob("*.json")]
        self.assertEqual([r["status"] for r in receipts], ["refused"])
        self.assertIn("nothing stopped", receipts[0]["error"])
        self.assertIsNone(su.halt_reason(self.cfg.state_dir))

    def test_an_unknown_plan_exit_while_waiting_idle_still_fails(self) -> None:
        # Control: only the documented refusal codes are refusals.
        self.sys.offplan = [12, 9]
        self.assertEqual(self.apply(), su.EXIT_FAILED)

    def test_lanes_launchd_still_holds_are_a_refusal_too(self) -> None:
        # m5studio, 2026-10-01 10:51Z: "launchd still holds ... forge-gate
        # after 40s" (exit 13) was recorded as a failed update and spent 6 h.
        self.sys.off_rc = 13
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.pool_state, "on")
        self.assertIsNone(su.halt_reason(self.cfg.state_dir))
        self.sys.off_rc = 0
        self.assertUpdated(self.apply())

    def test_any_other_pool_off_failure_is_still_a_failure(self) -> None:
        self.sys.off_rc = 1
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.last()["status"], "failed")


class OffPeerTests(Base):
    """A peer that is OFF and not updating is left to the capacity floor.

    m5studio was off for a store move from 19:04Z to 06:14Z on 2026-10-01/02,
    and every other host refused on it: 11.3 h of fleet-wide starvation.
    """

    OFF = {"state": "off", "participating": False}
    LABEL = OnDemandSupplyTests.LABEL

    def setUp(self) -> None:
        super().setUp()
        # The on-demand fixture: the label is this host's to serve, and only
        # peers that publish it and are fully healthy can cover it.
        self.sys.floor = {"allowed": False, "reason": "last_serving_host", "findings": [
            {"repo": "Generous-Corp/pulp", "label": self.LABEL, "verdict": "last_serving_host"}]}
        self.sys.published["registrations"] = [
            {"host_id": "m1", "repo": "Generous-Corp/pulp", "labels": ["pulp-build", self.LABEL]},
            {"host_id": "m5", "repo": "Generous-Corp/pulp", "labels": ["pulp-build", self.LABEL]},
            {"host_id": "studio", "repo": "Generous-Corp/pulp", "labels": ["pulp-build"]},
        ]
        self.sys.peers["m3"] = healthy_peer()

    def test_an_off_peer_with_the_floor_satisfied_proceeds(self) -> None:
        # studio also publishes the label and is healthy, so draining this
        # host leaves it served even with m5 counted as absent.
        self.sys.published["registrations"][2]["labels"].append(self.LABEL)
        self.sys.peers["m5"] = self.OFF
        self.assertUpdated(self.apply())
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        peers = next(step for step in receipt["steps"] if step["step"] == "peers")
        self.assertIn("off and not updating", peers["detail"])
        self.assertIn("m5", peers["detail"])

    def test_an_off_peer_with_the_floor_broken_refuses(self) -> None:
        # Only m5 publishes the label: with m5 off, draining this host would
        # take it to zero.
        self.sys.peers["m5"] = self.OFF
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])

    def test_an_off_peer_that_is_mid_update_still_refuses(self) -> None:
        self.sys.published["registrations"][2]["labels"].append(self.LABEL)
        self.sys.peers["m5"] = self.OFF
        self.sys.peer_markers["m5"] = {"host_id": "m5", "target": T_OLD, "ts": NOW - 60}
        self.assertDeferred(self.apply())
        self.assertEqual(self.sys.mutations(), [])

    def test_a_draining_peer_still_refuses(self) -> None:
        self.sys.published["registrations"][2]["labels"].append(self.LABEL)
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertDeferred(self.apply())
        self.assertEqual(self.sys.mutations(), [])

    def test_an_off_peer_holds_no_place_in_the_queue(self) -> None:
        self.sys.published["registrations"][2]["labels"].append(self.LABEL)
        self.sys.peers["m5"] = self.OFF
        self.sys.peer_waiting["m5"] = {"host_id": "m5", "since": NOW - 9 * 3600, "ts": NOW - 60}
        self.assertUpdated(self.apply())


class MidJobWaitTests(Base):
    def test_waits_while_mid_job_then_proceeds(self) -> None:
        self.sys.offplan = [12, 12, 0]
        self.assertUpdated(self.apply())
        self.assertEqual(self.sys.clock - NOW, 2 * 45 + su.ANNOUNCE_SETTLE_SECONDS)

    def test_timeout_restores_the_host_to_on(self) -> None:
        self.sys.offplan = [12]
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.sys.pool_state, "on")
        self.assertIn("still mid-job", self.last()["error"])
        joined = [" ".join(a) for a, _ in self.sys.calls]
        self.assertFalse(any(c.startswith("./tartci pool off") and "--plan" not in c for c in joined))
        self.assertFalse(any("--apply" in c for c in joined))


class ShipyardWriterLeaseTests(Base):
    """The install writes ~/.local/bin only under `shipyard writer-domain-exec`."""

    def shipyard(self) -> None:
        path = self.home / ".local" / "bin" / "shipyard"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")

    def installs(self) -> list[str]:
        return [" ".join(a) for a, _ in self.sys.calls
                if "--apply" in a and "install" in a and a[0] == "./tartci"]

    def test_an_audit_holding_the_domain_defers_and_writes_nothing(self) -> None:
        # m3 on 2026-10-02: the install wrote ~/.local/bin/tartci mid-audit
        # and the Sandbox E2E audit failed.
        self.shipyard()
        self.sys.audit_holds_domain = True
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.installs(), [])
        self.assertEqual(self.sys.pool_state, "on")
        receipts = [json.loads(path.read_text())
                    for path in (self.cfg.state_dir / "attempts").glob("*.json")]
        self.assertEqual([r["status"] for r in receipts], ["refused"])
        self.assertIn("sandbox_writer_domain_overlap", receipts[0]["error"])
        self.assertIsNone(su.halt_reason(self.cfg.state_dir))

    def test_a_free_domain_installs_under_the_lease(self) -> None:
        # Control, same instrument: shipyard present, no audit.
        self.shipyard()
        self.assertUpdated(self.apply())
        self.assertEqual(len(self.sys.fenced), 1)
        self.assertEqual(self.sys.fenced[0][0], "./tartci")
        self.assertIn("--apply", self.sys.fenced[0])
        wrapped = next(a for a, _ in self.sys.calls if "writer-domain-exec" in a and "--" in a)
        self.assertEqual(wrapped[wrapped.index("--path") + 1], str(self.home / ".local/bin/tartci"))

    def test_without_shipyard_the_install_is_unfenced(self) -> None:
        self.assertUpdated(self.apply())
        self.assertEqual(self.sys.fenced, [])
        self.assertEqual(len(self.installs()), 1)

    def test_a_shipyard_without_the_subcommand_is_treated_as_absent(self) -> None:
        self.shipyard()
        self.sys.writer_domain_exec = False
        self.assertUpdated(self.apply())
        self.assertEqual(self.sys.fenced, [])
        self.assertFalse(any("writer-domain-exec" in a and "--" in a for a, _ in self.sys.calls))


class InstallFailureTests(Base):
    def test_transient_install_failure_is_retried(self) -> None:
        self.sys.install_rcs = [1, 1, 0]
        self.assertUpdated(self.apply())

    def test_persistent_install_failure_restores_on_and_reports(self) -> None:
        self.sys.install_rcs = [1]
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.sys.pool_state, "on")
        self.assertEqual(self.last()["status"], "failed")
        lines = su.status_lines(self.cfg.state_dir)
        self.assertTrue(any("LAST ATTEMPT FAILED" in line for line in lines))
        self.assertIn("FAILED", su.summary(self.home)["problem"])

    def test_rate_limit_one_attempt_per_target(self) -> None:
        self.sys.install_rcs = [1]
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        calls_before = len(self.sys.mutations())
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(len(self.sys.mutations()), calls_before)
        self.sys.clock += 7 * 3600
        self.sys.install_rcs = [0]
        self.assertUpdated(self.apply())

    def test_refusal_does_not_spend_the_attempt(self) -> None:
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertDeferred(self.apply())
        self.sys.peers["m5"] = {"state": "on", "participating": True}
        self.assertUpdated(self.apply())


class VerificationTests(Base):
    def test_each_verification_check_can_fail(self) -> None:
        receipt = su.Receipt(self.cfg, self.sys, T_OLD, "apply")
        self.sys._set_installed(T_OLD)
        cases = {
            "not ready": lambda s: s.status_after_on["fleet"].update(fleet_ready=False),
            "serving BLOCKED": lambda s: s.status_after_on["fleet"].update(serving={"blocked": True}),
            "runs 2222": lambda s: s._set_installed("2" * 40),
            "guard": lambda s: setattr(s, "guard_rcs", (0, 0)),
        }
        su.verify(self.cfg, self.sys, T_OLD, receipt)  # control: healthy passes
        for needle, mutate in cases.items():
            with self.subTest(needle=needle):
                self.tearDown()
                self.setUp()
                self.sys._set_installed(T_OLD)
                mutate(self.sys)
                with self.assertRaises(su.Failed) as caught:
                    su.verify(self.cfg, self.sys, T_OLD, receipt)
                self.assertIn(needle, str(caught.exception))


class HostConditionVerifyTests(Base):
    """A host condition the install neither causes nor fixes never fails it.

    m5 is set to sleep on AC. Once that became a readiness problem, every
    update of m5 failed verification and rolled back (07:29Z and 13:53Z on
    2026-10-02), each time after a drain.
    """

    SLEEPS = {"code": "host_idle_sleep_enabled", "detail": "sleeps after 1 min idle on AC"}

    def test_a_host_that_sleeps_on_ac_still_updates(self) -> None:
        self.sys.status_problems = [self.SLEEPS]
        self.assertUpdated(self.apply())

    def test_a_full_disk_still_updates(self) -> None:
        self.sys.status_problems = [{"code": "disk_pressure", "label": "/Users/x"}]
        self.assertUpdated(self.apply())

    def test_a_real_problem_beside_a_host_condition_still_fails(self) -> None:
        # Control: only the host conditions are set aside.
        self.sys.status_problems = [self.SLEEPS, {"code": "loaded_receipt_mismatch",
                                                  "label": "lane"}]
        self.assertNotEqual(self.apply(), su.EXIT_OK)
        self.assertNotEqual(self.last()["status"], "succeeded")
        self.assertIn("loaded_receipt_mismatch", self.last()["error"])


class RelayTests(Base):
    relay = True

    def test_relay_reconciled_after_install(self) -> None:
        self.assertUpdated(self.apply())
        joined = [" ".join(a) for a, _ in self.sys.calls]
        relay = next(i for i, c in enumerate(joined) if "network_profile.py reconcile" in c)
        install = next(i for i, c in enumerate(joined) if "--apply" in c)
        self.assertGreater(relay, install)

    def test_relay_failure_fails_and_restores(self) -> None:
        self.sys.relay = {"ok": False, "reason": "probe failed"}
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertIn("relay", self.last()["error"])
        self.assertEqual(self.sys.pool_state, "on")


class SigningKeychainTests(Base):
    """The agent unlocks pulp's dedicated keychain in its own session.

    `pulp ship doctor` over SSH unlocked it for that SSH session only. The
    launchd agent kept failing the probe with errSecInternalComponent, and
    m5studio refused 17 times in a row (about 9 h) with nothing on any status
    surface.
    """

    sealed = True

    def secrets(self) -> None:
        path = self.home / ".config/pulp/secrets/keychain.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('PULP_SIGN_KEYCHAIN="$HOME/Library/Keychains/pulp-signing.keychain-db"\n'
                        "PULP_SIGN_KEYCHAIN_PW=s3cret\n")

    def calls(self, word: str) -> list[list[str]]:
        return [a for a, _ in self.sys.calls if a[0] == word]

    def test_a_keychain_locked_in_this_session_is_unlocked_before_the_probe(self) -> None:
        self.secrets()
        self.sys.locked = True
        self.assertUpdated(self.apply())
        unlock = self.calls("security")
        self.assertEqual(unlock[0][:3], ["security", "unlock-keychain", "-p"])
        self.assertEqual(unlock[0][-1],
                         str(self.home / "Library/Keychains/pulp-signing.keychain-db"))
        order = [("probe" if a[0] == "codesign" and "--timestamp" in a else a[0])
                 for a, _ in self.sys.calls if a[0] in ("security", "codesign")]
        self.assertLess(order.index("security"), order.index("probe"))

    def test_a_locked_keychain_without_secrets_is_refused_and_loud(self) -> None:
        # Control: with nothing to unlock from, the probe fails as before, and
        # the run of refusals is now on the status surfaces.
        self.sys.locked = True
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.sys.clock += 1800
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.calls("security"), [])
        line = su.signing_blocked(self.cfg.state_dir)
        self.assertIn("SIGNING KEYCHAIN LOCKED: 2 refusal(s) in a row", line)
        self.assertIn("pulp ship doctor", line)
        self.assertIn(line, su.status_lines(self.cfg.state_dir))
        self.assertIn("SIGNING KEYCHAIN LOCKED", su.summary(self.home)["problem"])
        # Unlockable again: the next success ends the run and the line goes.
        self.secrets()
        self.assertUpdated(self.apply())
        self.assertIsNone(su.signing_blocked(self.cfg.state_dir))

    def test_a_failed_unlock_is_recorded_and_the_probe_decides(self) -> None:
        self.secrets()
        self.sys.unlock_rc = 51
        # Another keychain in the search list signs (the doctor's unattended
        # sibling): the update proceeds, and the receipt says the unlock failed.
        self.assertUpdated(self.apply())
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        step = next(s for s in receipt["steps"] if s["step"] == "signing-keychain")
        self.assertIn("could not be unlocked", step["detail"])

    def test_a_failed_unlock_with_nothing_that_signs_refuses_before_the_host_is_touched(self) -> None:
        self.secrets()
        self.sys.locked = True
        self.sys.unlock_rc = 51
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])
        self.assertIn("SIGNING KEYCHAIN LOCKED", su.signing_blocked(self.cfg.state_dir))


class SealedTests(Base):
    sealed = True

    def pin(self) -> Path:
        return self.home / ".config/tartci/m3-launcher-approved.sha256"

    def test_reseal_extracts_identity_pins_and_installs_the_new_bundle(self) -> None:
        self.assertEqual(self.apply(), su.EXIT_OK, self.last())
        build = next(a for a, _ in self.sys.calls if a[:2] == ["bash", "scripts/build_macos_launcher.sh"])
        self.assertEqual(build[build.index("--identity") + 1], IDENTITY)
        install = next(a for a, _ in self.sys.calls if "--apply" in a)
        self.assertIn("--launch-helper-source", install)
        self.assertEqual(self.pin().read_text(), "c" * 64 + "\n")
        self.assertEqual(oct(self.pin().stat().st_mode & 0o777), "0o600")
        snapshot = Path(json.loads(Path(self.last()["receipt"]).read_text())["snapshot"])
        self.assertEqual((snapshot / "approved.sha256").read_text(), "0" * 64 + "\n")
        self.assertEqual(json.loads((snapshot / "TartCILauncher.app/Contents/Resources/bundle.json")
                                    .read_text())["source_commit"], INSTALLED)
        self.assertTrue((snapshot / "profile.toml").is_file())
        # The checkout is writable again after the immutable build.
        self.assertTrue(os.access(self.cfg.checkout, os.W_OK))

    def test_install_failure_restores_the_pin(self) -> None:
        self.sys.install_rcs = [1]
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.pin().read_text(), "0" * 64 + "\n")
        self.assertEqual(self.sys.pool_state, "on")

    def test_bad_bundle_refuses_before_drain(self) -> None:
        for mutate in (lambda s: setattr(s, "bundle_commit", "9" * 40),
                       lambda s: setattr(s, "codesign_verify_rc", 1),
                       lambda s: setattr(s, "agents_check_rc", 3)):
            with self.subTest():
                self.tearDown()
                self.setUp()
                mutate(self.sys)
                self.assertEqual(self.apply(), su.EXIT_REFUSED)
                self.assertEqual(self.sys.mutations(), [])
                self.assertEqual(self.pin().read_text(), "0" * 64 + "\n")

    def test_support_agents_run_after_verify_and_never_fail_the_update(self) -> None:
        for mutate in (lambda s: None,
                       lambda s: setattr(s, "agents_rc", 4),
                       lambda s: setattr(s, "agents_raises", True)):
            with self.subTest():
                self.tearDown()
                self.setUp()
                mutate(self.sys)
                self.assertUpdated(self.apply())
                steps = json.loads(Path(self.last()["receipt"]).read_text())["steps"]
                names = [step["step"] for step in steps]
                self.assertIn("support-agents", names)
                self.assertLess(names.index("verify"), names.index("support-agents"))
                agents = next(step for step in steps if step["step"] == "support-agents")
                self.assertEqual(agents["ok"], self.sys.agents_rc == 0 and not self.sys.agents_raises)

    def test_same_sealed_target_can_be_planned_and_applied_again(self) -> None:
        # A previous run's build is left read-only by build_macos_launcher.sh.
        leftover = self.cfg.state_dir / "builds" / T_OLD / "TartCILauncher.app" / "Contents"
        (leftover / "Resources" / "support").mkdir(parents=True)
        (leftover / "Resources" / "support" / "x").write_text("x")
        os.chmod(leftover / "Resources" / "support" / "x", 0o444)
        for d in (leftover / "Resources" / "support", leftover / "Resources", leftover):
            os.chmod(d, 0o555)
        self.assertEqual(self.plan(), su.EXIT_OK)
        self.assertEqual(self.plan(), su.EXIT_OK)
        self.assertEqual(self.apply(), su.EXIT_OK, self.last())

    def test_build_happens_only_after_the_gates(self) -> None:
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertDeferred(self.apply())
        self.assertFalse(any(a[:2] == ["bash", "scripts/build_macos_launcher.sh"]
                             for a, _ in self.sys.calls))
        receipts = list((self.cfg.state_dir / "attempts").glob("*.json"))
        self.assertTrue(all(json.loads(p.read_text())["status"] != "running" for p in receipts))

    def test_snapshot_that_does_not_verify_refuses_before_drain(self) -> None:
        self.sys.snapshot_verify_rc = 1
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])
        self.assertEqual(self.pin().read_text(), "0" * 64 + "\n")

    def test_failed_rollback_repins_to_the_live_bundle(self) -> None:
        self.sys.broken_target = True
        self.sys.rollback_install_rc = 1
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertIn("ROLLBACK FAILED", self.last()["error"])
        self.assertEqual(self.sys.running(), T_OLD)            # new bundle still live
        self.assertEqual(self.pin().read_text(), "c" * 64 + "\n")  # pin matches it

    def test_plan_extracts_identity_read_only(self) -> None:
        self.assertEqual(self.plan(), su.EXIT_OK)
        self.assertEqual(self.sys.mutations(), [])
        self.assertFalse(any(a[:2] == ["bash", "scripts/build_macos_launcher.sh"]
                             for a, _ in self.sys.calls))
        self.assertEqual(self.pin().read_text(), "0" * 64 + "\n")


class SurfaceTests(Base):
    def test_summary_problem_for_stale_unknown_and_failed(self) -> None:
        self.assertIsNone(su.summary(self.home)["problem"])
        su._write_json(self.cfg.state_dir / "skew.json", {"state": "behind", "stale": True,
                                                           "behind": 4, "oldest_undeployed": "T"})
        self.assertIn("4 commits behind", su.summary(self.home)["problem"])
        su._write_json(self.cfg.state_dir / "skew.json", {"state": "unknown", "reason": "x"})
        self.assertIn("unknown", su.summary(self.home)["problem"])

    def test_doctor_and_config_verdicts_show_skew(self) -> None:
        import fleet_doctor
        import macos_fleet_lanes as fleet
        su._write_json(self.cfg.state_dir / "skew.json", {
            "state": "behind", "behind": 3, "oldest_undeployed": "2026-09-23T06:51:41Z",
            "stale": False, "measured_at": su._iso(time.time())})
        self.assertEqual(fleet_doctor.check_self_update(su.summary(self.home)).code,
                         "self_update_current")
        self.assertEqual(fleet_doctor.check_self_update(None).code, "self_update_unmeasured")
        su._write_json(self.cfg.state_dir / "last.json", {"status": "failed", "target": T_OLD,
                                                          "error": "boom", "at": "t"})
        self.assertEqual(fleet_doctor.check_self_update(su.summary(self.home)).code,
                         "self_update_problem")
        text = fleet.render_config_verdicts(fleet.config_verdicts(
            self.home / "absent.toml", ROOT))
        self.assertIn("tartci: 3 commits behind main (oldest undeployed: 2026-09-23T06:51:41Z)", text)
        self.assertIn("LAST ATTEMPT FAILED", text)

    def test_watchdog_warns_on_self_update_problem(self) -> None:
        import tartci_launchd_watchdog as wd
        clean = {"profile_drift": {"state": "in_sync"}, "supply": {"state": "match"}}
        self.assertIn("self_update=", wd.config_problem(
            {**clean, "self_update": {"problem": "last self-update FAILED"}}))
        self.assertIsNone(wd.config_problem({**clean, "self_update": {"problem": None}}))


class VmDhcpVerifyTests(Base):
    """After the new generation serves, one probe proves the VM network."""

    def test_an_update_asks_the_breaker_to_verify(self):
        self.assertUpdated(self.apply())
        self.assertEqual(self.sys.vm_verify_calls, [["vm-dhcp", "verify", "--reason", "self_update"]])
        steps = {s["step"]: s for s in json.loads(Path(self.last()["receipt"]).read_text())["steps"]}
        self.assertTrue(steps["vm-dhcp-verify"]["ok"])

    def test_a_failed_verify_never_fails_the_update(self):
        self.sys.vm_verify_rc = 1
        self.assertUpdated(self.apply())
        steps = {s["step"]: s for s in json.loads(Path(self.last()["receipt"]).read_text())["steps"]}
        self.assertFalse(steps["vm-dhcp-verify"]["ok"])

    def test_a_refused_update_never_verifies(self):
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.apply()
        self.assertEqual(self.sys.vm_verify_calls, [])


class PausedByStallTests(Base):
    """A self-update the launchd guard holds for a timer stall says so itself."""

    STARTED = NOW - 4 * 86400

    SELF = "com.danielraffel.tartci.self-update"

    def guard(self, *, paused=(SELF,), active=True, age=60, directory=None):
        directory = directory or self.cfg.state_dir.parent / "launchd-interval-guard"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "status.json").write_text(json.dumps({
            "ts": time.time() - age, "agents_checked": 18, "errors": [], "kicked": [],
            "stalled": [{"label": self.SELF, "interval": 1800}],
            "paused": [{"label": label, "interval": 1800} for label in paused],
            "episode": {"active": active, "labels": [self.SELF], "started_ts": self.STARTED}}))

    def stale_skew(self):
        su._write_json(self.cfg.state_dir / "skew.json", {
            "state": "behind", "behind": 44, "stale": True, "installed": INSTALLED,
            "target": T_OLD, "oldest_undeployed": "2026-10-05T05:21:21Z",
            "measured_at": su._iso(time.time())})

    def test_a_paused_self_update_says_why_everywhere(self):
        # m3 from 2026-10-05: behind and stale, no attempt since, the guard
        # holding it. Its status and doctor read like a failed update.
        import fleet_doctor
        self.stale_skew()
        self.guard()
        since = su._iso(self.STARTED)
        lines = su.status_lines(self.cfg.state_dir)
        self.assertTrue(any(l.startswith(f"self-update PAUSED by the launchd timer stall since {since}")
                            for l in lines), lines)
        summary = su.summary(self.home)
        self.assertIn("a reboot resumes it", summary["paused"])
        finding = fleet_doctor.check_self_update(summary)
        self.assertEqual((finding.state, finding.code), (fleet_doctor.PROBLEM, "self_update_paused"))
        self.assertIn("self_update_paused", fleet_doctor.load_reasons())

    def test_the_guard_dir_override_is_honoured(self):
        other = self.home / "guard-elsewhere"
        self.guard(directory=other)
        with mock.patch.dict(os.environ, {"TARTCI_INTERVAL_GUARD_DIR": str(other)}):
            self.assertIsNotNone(su.paused_line(self.cfg.state_dir))
        self.assertIsNone(su.paused_line(self.cfg.state_dir))

    def test_not_paused_unless_the_guard_holds_self_update_in_a_live_stall(self):
        import fleet_doctor
        self.stale_skew()
        for name, kwargs in (("another agent paused, not self-update",
                              {"paused": ("com.danielraffel.tartci.launchd-watchdog",)}),
                             ("nothing paused", {"paused": ()}),
                             ("no active episode", {"active": False}),
                             ("stale guard receipt", {"age": 10 * 3600})):
            with self.subTest(case=name):
                self.guard(**kwargs)
                self.assertIsNone(su.paused_line(self.cfg.state_dir))
                self.assertEqual(fleet_doctor.check_self_update(su.summary(self.home)).code,
                                 "self_update_problem")

    def test_no_guard_receipt_is_not_paused(self):
        self.assertIsNone(su.paused_line(self.cfg.state_dir))


class AgentTemplateTests(unittest.TestCase):
    def test_template_renders_and_installer_only_plans_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # No per-host settings file is needed any more.
            env = {**os.environ, "TARTCI_AGENTS_DIR": str(Path(td) / "agents"),
                   "TARTCI_SELF_UPDATE_SKIP_PEERS": "1"}
            proc = subprocess.run(["bash", str(ROOT / "scripts/install_self_update_agent.sh")],
                                  env=env, text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("plan only", proc.stdout)
            self.assertFalse((Path(td) / "agents").exists())
        rendered = subprocess.run(
            [sys.executable, str(ROOT / "scripts/render_launchd_template.py"),
             str(ROOT / "launchd/com.danielraffel.tartci.self-update.plist.template"),
             "--set", "HOME=/Users/x"], capture_output=True, check=True)
        value = plistlib.loads(rendered.stdout)
        self.assertEqual(value["StartInterval"], 1800)
        self.assertGreaterEqual(value["ExitTimeOut"], 60)
        self.assertEqual(value["ProgramArguments"][-4:],
                         ["fleet-macos", "self-update", "--apply", "--scheduled"])

    def test_watchdog_never_interrupts_the_agent(self) -> None:
        import tartci_launchd_watchdog as wd
        self.assertIn("com.danielraffel.tartci.self-update", wd.UNINTERRUPTIBLE_AGENTS)
        self.assertIn(3, wd.APPLICATION_EXIT_CODES["com.danielraffel.tartci.self-update"])


class VerifySettleTests(Base):
    def test_a_first_heartbeat_still_pending_is_waited_for(self) -> None:
        self.sys.settling_reads = 3
        start = self.sys.clock
        self.assertUpdated(self.apply())
        self.assertEqual(self.last()["status"], "succeeded")
        self.assertNotEqual(self.sys.running(), INSTALLED)
        self.assertGreaterEqual(self.sys.clock - start, 3 * su.VERIFY_SETTLE_POLL_SECONDS)

    def test_a_heartbeat_that_never_arrives_still_rolls_back(self) -> None:
        self.sys.settling_reads = 10_000
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertIn("heartbeat_missing", self.last()["error"])

    def test_a_non_settling_problem_is_not_retried(self) -> None:
        self.sys.settling_reads = 1
        self.sys.settling_code = "loaded_receipt_mismatch"
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        reads = [a for a, _ in self.sys.calls if a[-3:] == ["pool", "status", "--json"]]
        self.assertIn("loaded_receipt_mismatch", self.last()["error"])
        self.assertLessEqual(len(reads), 3)


class SystemRunTests(unittest.TestCase):
    def test_bare_python3_runs_under_this_interpreter(self) -> None:
        # A PATH whose python3 is not this interpreter, as under ssh/launchd.
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "python3"
            shim.write_text("#!/bin/sh\necho path-python3\n")
            shim.chmod(0o755)
            result = su.System().run(["python3", "-c", "import sys; print(sys.executable)"],
                                     env={"PATH": f"{tmp}:/usr/bin:/bin"})
        self.assertEqual(result.rc, 0, result.err)
        self.assertEqual(result.out.strip(), sys.executable)


class CensusEnvTests(unittest.TestCase):
    def test_tartci_calls_resolve_this_interpreter_even_from_a_minimal_path(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            os.environ.pop("TARTCI_PYTHON", None)
            env = su.census_env()
        self.assertEqual(env["TARTCI_PYTHON"], sys.executable)
        proc = subprocess.run(["/bin/sh", "-c", "python3 -c 'import sys; print(sys.executable)'"],
                              env={**os.environ, "PATH": "/usr/bin:/bin", **env},
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(os.path.realpath(proc.stdout.strip()), os.path.realpath(sys.executable))


class RollbackTests(Base):
    def test_verify_failure_rolls_back_to_the_previous_generation(self) -> None:
        self.sys.broken_target = True
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.sys.running(), INSTALLED)
        self.assertEqual(self.sys.pool_state, "on")
        last = self.last()
        self.assertEqual(last["status"], "rolled_back")
        self.assertIn(f"rolled back to {INSTALLED[:12]} and verified", last["error"])
        # The rollback reinstalled the snapshot profile from the previous commit.
        rollback_install = [a for a, _ in self.sys.calls if "--apply" in a and
                            any(str(x).endswith("profile.toml") and "rollback" in str(x) for x in a)]
        self.assertEqual(len(rollback_install), 1)
        self.assertIn("ROLLED BACK", "\n".join(su.status_lines(self.cfg.state_dir)))

    def test_rollback_failure_is_loud_and_leaves_the_host_on(self) -> None:
        self.sys.broken_target = True
        self.sys.rollback_install_rc = 1
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        last = self.last()
        self.assertEqual(last["status"], "failed")
        self.assertIn("ROLLBACK FAILED", last["error"])
        self.assertIn("host is on", last["error"])
        self.assertEqual(self.sys.pool_state, "on")

    def test_rollback_verifies_the_previous_generation(self) -> None:
        self.sys.broken_target = True
        self.sys.broken_previous = True
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        last = self.last()
        self.assertEqual(last["status"], "failed")
        self.assertIn("ROLLBACK FAILED", last["error"])
        self.assertIn(f"verification of {INSTALLED[:12]}", last["error"])

    def test_pool_on_failure_is_recorded_as_host_off(self) -> None:
        self.sys.install_rcs = [1]
        self.sys.on_rc = 1
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertIn("HOST LEFT OFF", self.last()["error"])

    def test_install_failure_keeps_the_previous_generation_without_reinstall(self) -> None:
        self.sys.install_rcs = [1]
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.sys.running(), INSTALLED)
        self.assertIn(f"previous generation {INSTALLED[:12]}", self.last()["error"])
        self.assertFalse(any("rollback" in str(a) for a, _ in self.sys.calls))


class RelayRollbackTests(Base):
    relay = True

    def test_relay_failure_after_install_rolls_back(self) -> None:
        self.sys.relay = {"ok": False, "reason": "probe failed"}
        calls = {"n": 0}

        def relay_ok_on_rollback(argv):
            if argv[:2] == ["python3", "scripts/network_profile.py"]:
                calls["n"] += 1
        self.sys.hook = relay_ok_on_rollback
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.sys.running(), INSTALLED)
        self.assertEqual(self.last()["status"], "rolled_back")
        self.assertIn("relay", self.last()["error"])


class SealedRollbackTests(Base):
    sealed = True

    def pin(self) -> Path:
        return self.home / ".config/tartci/m3-launcher-approved.sha256"

    def test_sealed_rollback_restores_bundle_and_pin_together(self) -> None:
        self.sys.broken_target = True
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.last()["status"], "rolled_back")
        self.assertEqual(self.sys.running(), INSTALLED)
        self.assertEqual(self.pin().read_text(), "0" * 64 + "\n")
        rollback = [a for a, _ in self.sys.calls if "--apply" in a][-1]
        source = rollback[rollback.index("--launch-helper-source") + 1]
        self.assertIn("rollback", source)

    def test_signing_probe_failure_refuses_before_drain(self) -> None:
        self.sys.signing_rc = 1
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])
        self.assertFalse(any(a[:2] == ["bash", "scripts/build_macos_launcher.sh"]
                             for a, _ in self.sys.calls))


class TerminationTests(Base):
    def test_sigterm_mid_run_puts_the_host_back_and_finishes_the_receipt(self) -> None:
        import signal

        def kill_during_wait(argv):
            if argv[:4] == ["./tartci", "pool", "off", "--plan"]:
                self.sys.hook = None
                os.kill(os.getpid(), signal.SIGTERM)
        self.sys.hook = kill_during_wait
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.sys.pool_state, "on")
        self.assertIn("terminated", self.last()["error"])
        self.assertFalse((self.cfg.state_dir / "active.json").exists())
        self.assertIsNot(signal.getsignal(signal.SIGTERM), su._raise_terminated)


class RecordTests(Base):
    def test_refusal_does_not_erase_the_failure_record(self) -> None:
        self.sys.install_rcs = [1]
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.apply(), su.EXIT_REFUSED)  # rate-limited
        self.assertEqual(self.last()["status"], "failed")
        self.assertIn("FAILED", su.summary(self.home)["problem"])

    def test_consecutive_failures_halt_until_cleared(self) -> None:
        self.sys.install_rcs = [1]
        for _ in range(su.MAX_CONSECUTIVE_FAILURES):
            self.assertEqual(self.apply(), su.EXIT_FAILED)
            self.sys.clock += 7 * 3600
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertIn("HALTED", su.summary(self.home)["problem"])
        self.sys.clock += 1
        su._write_json(self.cfg.state_dir / "halt-cleared.json", {"at": su._iso(self.sys.clock)})
        self.sys.clock += 1
        self.sys.install_rcs = [0]
        self.assertUpdated(self.apply())

    def test_old_refusal_receipts_are_pruned(self) -> None:
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertDeferred(self.apply())
        self.assertEqual(len(list((self.cfg.state_dir / "attempts").glob("*.json"))), 1)
        self.sys.clock += 8 * 86400
        self.sys.peers["m5"] = {"state": "on", "participating": True}
        self.sys.log_lines = ""
        self.apply()
        self.assertEqual(list((self.cfg.state_dir / "attempts").glob("*.json")), [])


class ChecksAndTargetTests(Base):
    def test_red_newest_soaked_commit_falls_back_to_an_older_green_one(self) -> None:
        older = "c" * 40
        self.sys.log_lines = (f"{T_NEW} {int(NOW - 60)}\n{T_OLD} {int(NOW - 7200)}\n"
                              f"{older} {int(NOW - 9000)}\n")
        self.sys.checks[T_OLD] = [{"name": "lint", "status": "completed", "conclusion": "failure"}]
        skew = su.measure_skew(self.cfg, self.sys, INSTALLED, NOW)
        self.assertEqual(skew["target"], older)
        self.assertIn("lint=failure", skew["skipped"][0])

    def test_no_green_soaked_commit_does_nothing(self) -> None:
        self.sys.checks[T_OLD] = []
        self.assertEqual(self.apply(), su.EXIT_NOTHING)
        self.assertEqual(self.sys.mutations(), [])
        skew = json.loads((self.cfg.state_dir / "skew.json").read_text())
        self.assertEqual(skew["state"], "unverified")

    def test_explicit_target_must_be_on_the_first_parent_chain(self) -> None:
        skew = su.measure_skew(self.cfg, self.sys, INSTALLED, NOW, "d" * 40)
        self.assertEqual(skew["state"], "unknown")
        self.assertIn("first-parent", skew["reason"])
        ok_skew = su.measure_skew(self.cfg, self.sys, INSTALLED, NOW, T_OLD)
        self.assertEqual(ok_skew["target"], T_OLD)


class ScaleTests(Base):
    def test_a_fourth_host_needs_no_per_host_edits(self) -> None:
        self.sys.published["registrations"].append({"host_id": "x9"})
        self.sys.published["hosts"].append({"host_id": "x9", "ssh": None})
        self.sys.peers["tartci-x9"] = {"state": "on", "participating": True}
        peers = su.published_peers(self.cfg, self.sys)
        self.assertEqual(peers["x9"], "tartci-x9")
        self.assertEqual(peers["studio"], "m3")
        self.assertUpdated(self.apply())
        self.assertTrue(any(a[:1] == ["ssh"] and "tartci-x9" in a for a, _ in self.sys.calls))
        # [peers] is an override only.
        self.cfg.peers = {"x9": "x9.lan"}
        self.assertEqual(su.published_peers(self.cfg, self.sys)["x9"], "x9.lan")

    def test_published_supply_carries_each_profiles_ssh_alias(self) -> None:
        import macos_fleet_lanes as fleet
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copytree(ROOT / "profiles", root / "profiles")
            text = (ROOT / "profiles/m1-macos-fleet.toml").read_text()
            text = text.replace('name = "m1-macos-fleet"', 'name = "x9-macos-fleet"')
            text = text.replace('id = "m1"', 'id = "x9"', 1).replace('ssh = "m1"\n', "")
            (root / "profiles/x9-macos-fleet.toml").write_text(text)
            hosts = {row["host_id"]: row["ssh"] for row in fleet.published_snapshot(root)["hosts"]}
            self.assertEqual(hosts, {"m1": "m1", "studio": "m3", "m5": "m5", "m5studio": "m5s", "x9": None})
            (root / "profiles/x9-macos-fleet.toml").write_text(
                text.replace('id = "x9"', 'id = "x9"\nssh = "bad alias!"', 1))
            with self.assertRaises(ValueError):
                fleet.load(root / "profiles/x9-macos-fleet.toml")


class AtomicCloneTests(Base):
    def test_a_failed_clone_leaves_no_checkout(self) -> None:
        shutil.rmtree(self.cfg.checkout)
        self.sys.clone_ok = False
        with self.assertRaises(su.Refused):
            su.refresh_checkout(self.cfg, self.sys)
        self.assertFalse(self.cfg.checkout.exists())
        self.assertEqual(list(self.cfg.checkout.parent.glob(".update-checkout.*")), [])
        self.sys.clone_ok = True
        su.refresh_checkout(self.cfg, self.sys)
        self.assertTrue((self.cfg.checkout / ".git").is_dir())


class InterruptedRecoveryTests(Base):
    def test_second_sigterm_during_rollback_still_finishes_and_counts(self) -> None:
        self.sys.broken_target = True

        def terminate_in_rollback(argv):
            if argv[:2] == ["./tartci", "support-manifest"] and self.sys.checked_out == INSTALLED:
                self.sys.hook = None
                raise su.Terminated("second SIGTERM")
        self.sys.hook = terminate_in_rollback
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        last = self.last()
        self.assertEqual(last["status"], "failed")
        self.assertIn("ROLLBACK FAILED", last["error"])
        self.assertIn("Terminated", last["error"])
        self.assertEqual(self.sys.pool_state, "on")
        receipts = [json.loads(p.read_text()) for p in (self.cfg.state_dir / "attempts").glob("*.json")]
        self.assertTrue(receipts and all(r["status"] != "running" for r in receipts))

    def test_terminated_after_install_names_the_verify_command(self) -> None:
        def terminate_at_pool_on(argv):
            if argv[-2:] == ["pool", "on"]:
                self.sys.hook = None
                raise su.Terminated("SIGTERM")
        self.sys.hook = terminate_at_pool_on
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertIn("self-update --verify", self.last()["error"])

    def test_install_runs_as_a_critical_section(self) -> None:
        self.assertUpdated(self.apply())
        self.assertEqual(len(self.sys.critical), 1)
        self.assertIn("--apply", self.sys.critical[0])

    def test_install_timeout_is_not_retried(self) -> None:
        self.sys.install_rcs = [su.EXIT_TIMED_OUT, 0]
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(len(self.sys.critical), 1)
        self.assertIn("timed out", self.last()["error"])

    def test_successful_runs_prune_old_snapshots(self) -> None:
        rollback = self.cfg.state_dir / "rollback"
        for i in range(su.KEEP_SNAPSHOTS + 3):
            (rollback / f"2026010{i}T000000Z-old").mkdir(parents=True)
            os.utime(rollback / f"2026010{i}T000000Z-old", (i, i))
        self.assertUpdated(self.apply())
        self.assertEqual(len(list(rollback.iterdir())), su.KEEP_SNAPSHOTS)


class CriticalSectionTests(unittest.TestCase):
    """The real System.run_critical, with real child processes."""

    def test_sigterm_is_deferred_until_the_child_exits(self) -> None:
        import signal
        with tempfile.TemporaryDirectory() as td:
            done = Path(td) / "done"
            script = f"trap 'echo trapped >> {td}/trap' TERM; sleep 1; echo ok > {done}"
            old = signal.signal(signal.SIGTERM, su._raise_terminated)
            try:
                import threading
                threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
                with self.assertRaises(su.Terminated):
                    su.System().run_critical(["bash", "-c", script], timeout=30)
            finally:
                signal.signal(signal.SIGTERM, old)
            self.assertEqual(done.read_text(), "ok\n")          # child finished
            self.assertFalse((Path(td) / "trap").exists())      # and was never signalled

    def test_timeout_terms_the_group_and_waits_for_its_trap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = (f"trap 'echo restored > {td}/trap; exit 3' TERM; "
                      "while :; do sleep 0.1; done")
            original = su.INSTALL_TERM_GRACE
            su.INSTALL_TERM_GRACE = 10
            try:
                result = su.System().run_critical(["bash", "-c", script], timeout=0.5)
            finally:
                su.INSTALL_TERM_GRACE = original
            self.assertEqual(result.rc, su.EXIT_TIMED_OUT)
            self.assertEqual((Path(td) / "trap").read_text(), "restored\n")
            self.assertIn("timed out", result.err)

    def test_clear_dir_removes_a_read_only_build(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td) / "b" / "c"
            tree.mkdir(parents=True)
            (tree / "f").write_text("x")
            os.chmod(tree / "f", 0o444)
            os.chmod(tree, 0o555)
            os.chmod(tree.parent, 0o555)
            su.clear_dir(Path(td) / "b")
            self.assertFalse((Path(td) / "b").exists())


class InterruptedRunTests(Base):
    """A self-update SIGKILLed by launchd (ExitTimeOut) is found and recovered."""

    def _killed_receipt(self, *, announced=True, pid=424242, start="Mon Sep 21 00:00:00 2026"):
        path = self.cfg.state_dir / "attempts" / "20260101T000000Z-aaaaaaaaaaaa.json"
        steps = [{"step": "checkout"}] + ([{"step": "announce"}, {"step": "install"}] if announced else [])
        su._write_json(path, {"schema": su.SCHEMA, "mode": "apply", "status": "running",
                              "target": T_OLD, "previous": INSTALLED, "started_at": "2026-01-01T00:00:00Z",
                              "pid": pid, "pid_start": start, "steps": steps,
                              "approval": None, "pin_path": None})
        su._write_json(self.cfg.state_dir / "active.json",
                       {"host_id": "m1", "target": T_OLD, "ts": NOW, "pid": pid, "pid_start": start})
        return path

    def test_killed_run_is_recovered_counted_and_unblocks_peers(self) -> None:
        path = self._killed_receipt()
        self.sys.pool_state = "off"
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        value = json.loads(path.read_text())
        self.assertEqual(value["status"], "failed")
        self.assertIn("interrupted", value["error"])
        self.assertIn("self-update --verify", value["error"])
        self.assertEqual(self.last()["status"], "failed")
        self.assertEqual(self.sys.pool_state, "on")
        self.assertFalse((self.cfg.state_dir / "active.json").exists())
        self.assertEqual(self.sys.mutations(), [f"{self.home}/.local/bin/tartci pool on"])
        # It counts toward the halt like any failure.
        self.assertIn("FAILED", su.summary(self.home)["problem"])

    def test_killed_before_any_change_is_not_a_failure(self) -> None:
        path = self._killed_receipt(announced=False)
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(json.loads(path.read_text())["status"], "refused")
        self.assertEqual(self.sys.mutations(), [])
        self.assertFalse((self.cfg.state_dir / "last.json").exists())

    def test_live_run_or_live_installer_refuses(self) -> None:
        self._killed_receipt(pid=424242, start="S")
        self.sys.procs[424242] = "S"
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])
        self.sys.procs.clear()
        (self.cfg.state_dir / "attempts").mkdir(exist_ok=True)
        for p in (self.cfg.state_dir / "attempts").glob("*.json"):
            p.unlink()
        su._write_json(self.cfg.state_dir / "installer.json", {"pgid": 777, "start": "T"})
        self.sys.procs[777] = "T"
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.plan(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])
        # Control: once the installer has exited (or its pid was reused), runs proceed.
        self.sys.procs[777] = "a different process"
        self.assertUpdated(self.apply())

    def test_plan_reports_but_does_not_recover(self) -> None:
        path = self._killed_receipt()
        self.assertEqual(self.plan(), su.EXIT_OK)
        self.assertEqual(json.loads(path.read_text())["status"], "running")
        self.assertEqual(self.sys.mutations(), [])

    def test_receipt_is_on_disk_while_running(self) -> None:
        seen = []

        def peek(argv):
            if argv[:3] == ["./tartci", "pool", "drain"]:
                seen.extend(json.loads(p.read_text())["status"]
                            for p in (self.cfg.state_dir / "attempts").glob("*.json"))
        self.sys.hook = peek
        self.assertUpdated(self.apply())
        self.assertIn("running", seen)


class DeferralCapTests(unittest.TestCase):
    def test_sigterm_deferral_is_capped_below_exit_timeout(self) -> None:
        import signal
        import threading
        self.assertLess(su.SIGTERM_DEFER_CAP + su.SIGTERM_TERM_WAIT, 120)
        with tempfile.TemporaryDirectory() as td:
            record = Path(td) / "installer.json"
            script = f"trap 'echo restored > {td}/trap; exit 3' TERM; while :; do sleep 0.1; done"
            caps = (su.SIGTERM_DEFER_CAP, su.SIGTERM_TERM_WAIT)
            su.SIGTERM_DEFER_CAP, su.SIGTERM_TERM_WAIT = 1, 5
            old = signal.signal(signal.SIGTERM, su._raise_terminated)
            started = time.monotonic()
            try:
                threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
                with self.assertRaises(su.Terminated):
                    su.System().run_critical(["bash", "-c", script], timeout=60, record=record)
            finally:
                signal.signal(signal.SIGTERM, old)
                su.SIGTERM_DEFER_CAP, su.SIGTERM_TERM_WAIT = caps
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual((Path(td) / "trap").read_text(), "restored\n")  # trap ran
            self.assertFalse(record.exists())  # it exited, so nothing is left to guard

    def test_installer_that_ignores_term_is_recorded(self) -> None:
        import signal
        import threading
        with tempfile.TemporaryDirectory() as td:
            record = Path(td) / "installer.json"
            caps = (su.SIGTERM_DEFER_CAP, su.SIGTERM_TERM_WAIT)
            su.SIGTERM_DEFER_CAP, su.SIGTERM_TERM_WAIT = 1, 1
            old = signal.signal(signal.SIGTERM, su._raise_terminated)
            try:
                threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
                with self.assertRaises(su.Terminated) as caught:
                    su.System().run_critical(["bash", "-c", "trap '' TERM; sleep 4"],
                                             timeout=60, record=record)
            finally:
                signal.signal(signal.SIGTERM, old)
                su.SIGTERM_DEFER_CAP, su.SIGTERM_TERM_WAIT = caps
            self.assertIn("STILL RUNNING", str(caught.exception))
            value = json.loads(record.read_text())
            self.assertTrue(su.System().group_alive(value["pgid"]))
            os.killpg(value["pgid"], signal.SIGKILL)

    def test_group_already_gone_at_timeout_is_not_an_error(self) -> None:
        original = os.killpg

        def vanished(pgid, sig):
            raise ProcessLookupError
        os.killpg = vanished
        try:
            result = su.System().run_critical(["bash", "-c", "sleep 1.5"], timeout=0.3)
        finally:
            os.killpg = original
        self.assertEqual(result.rc, su.EXIT_TIMED_OUT)


class IncidentTests(Base):
    """m5, 2026-09-25: verify read readiness once, before the first heartbeat."""

    def test_heartbeats_after_pool_on_are_waited_for(self) -> None:
        self.sys.heartbeat_after = 105   # measured on m5
        self.assertEqual(self.apply(), su.EXIT_OK, self.last())
        self.assertEqual(self.last()["status"], "succeeded")
        self.assertEqual(self.sys.running(), T_OLD)
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        self.assertTrue(any(s["step"] == "verify" for s in receipt["steps"]))
        self.assertGreaterEqual(self.sys.clock - self.sys.on_at, 105)

    def test_never_ready_times_out_rolls_back_and_ends_on(self) -> None:
        self.sys.broken_target = True
        self.sys.heartbeat_after = 105   # the previous generation also needs time
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.last()["status"], "rolled_back")
        self.assertIn("verification of", self.last()["error"])
        self.assertEqual(self.sys.running(), INSTALLED)
        self.assertEqual(self.sys.pool_state, "on")

    def test_refused_pool_on_after_rollback_reinstalls_and_ends_on(self) -> None:
        # The incident's second half: pool on of the restored generation raced
        # its persistent runner and refused three times, closing admission.
        self.sys.broken_target = True
        # new gen on; rollback pool on x3 refused; recovery pool on x3 refused
        self.sys.on_rcs = [0, 10, 10, 7, 10, 10, 7]
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.sys.pool_state, "on")
        self.assertFalse(self.last()["host_off"])
        self.assertIn("host is on", self.last()["error"])
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        self.assertTrue(any(s["step"] == "reinstall-for-pool-on" for s in receipt["steps"]))

    def test_rollback_failed_verify_still_ends_on(self) -> None:
        self.sys.broken_target = True
        self.sys.broken_previous = True
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertIn("ROLLBACK FAILED", self.last()["error"])
        self.assertEqual(self.sys.pool_state, "on")
        self.assertFalse(self.last()["host_off"])

    def test_host_off_only_when_pool_on_is_impossible_and_loud(self) -> None:
        self.sys.broken_target = True
        self.sys.on_rcs = [0]
        self.sys.on_rc = 7
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertTrue(self.last()["host_off"])
        self.assertIn("LEFT THIS HOST OFF", su.summary(self.home)["problem"])
        self.assertTrue(any("WAS LEFT OFF" in line for line in su.status_lines(self.cfg.state_dir)))

    def test_a_host_left_off_is_put_back_before_the_same_target_guard(self) -> None:
        # m3, 2026-09-29: the update and its rollback both left the host OFF,
        # and the 6 h same-target guard then refused every scheduled run
        # before it looked at the pool.
        self.sys.broken_target = True
        self.sys.on_rcs = [0]
        self.sys.on_rc = 7
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertTrue(self.last()["host_off"])
        self.assertEqual(self.sys.pool_state, "off")
        self.sys.on_rc = 0          # the volume came back
        self.sys.clock += 600
        before = len(self.sys.calls)
        self.assertEqual(self.apply(), su.EXIT_REFUSED)   # the guard still holds the target
        run = [" ".join(a) for a, _ in self.sys.calls[before:]]
        self.assertTrue(any(c.endswith("pool on") for c in run), run)
        self.assertFalse(any("pool drain" in c or "--apply" in c for c in run), run)
        self.assertEqual(self.sys.pool_state, "on")
        self.assertFalse(self.last()["host_off"])
        self.assertNotIn("LEFT THIS HOST", su.summary(self.home)["problem"] or "")
        events = (self.cfg.state_dir / "events.jsonl").read_text()
        self.assertIn("host_off_recovered", events)

    def test_a_healthy_host_is_not_touched_by_recovery(self) -> None:
        # Control: no host_off record, so no extra `pool on` before the update.
        self.assertUpdated(self.apply())
        ons = [a for a, _ in self.sys.calls if a[-2:] == ["pool", "on"]]
        self.assertEqual(len(ons), 1)


class OsInterpreterRefreshTests(Base):
    def test_current_host_with_os_changed_interpreter_reinstalls_same_generation(self) -> None:
        self.sys.log_lines = ""   # current with main
        self.sys.status_problems = [{"code": "interpreter_changed_by_os_update",
                                     "detail": "macOS build 25A1 -> 26A1"}]
        self.sys.pool_state = "on"

        def refreshed(argv):
            if "--apply" in argv:
                self.sys.status_problems = []   # the reinstall rewrites the receipt
        self.sys.hook = refreshed
        self.assertEqual(self.apply(), su.EXIT_OK, self.last())
        self.assertTrue(any("--apply" in a for a, _ in self.sys.calls))
        self.assertEqual(self.last()["target"], INSTALLED)

    def test_current_host_without_the_problem_does_nothing(self) -> None:
        self.sys.log_lines = ""
        self.sys.status_problems = [{"code": "receipt_mismatch", "detail": "x"}]
        self.assertEqual(self.apply(), su.EXIT_NOTHING)
        self.assertEqual(self.sys.mutations(), [])


class RecoveryBranchTests(Base):
    """_recover when nothing new was installed (the common pre-install failure)."""

    def test_pre_install_failure_with_refused_pool_on_undrains_when_never_idle(self) -> None:
        # OS-drift host: drain, lanes never idle (timeout), pool on refuses at
        # verify-installed before mutation, so the pool stays draining.
        self.sys.offplan = [12]
        self.sys.on_rc = 7
        self.sys.refuse_keeps_state = True
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        last = self.last()
        self.assertEqual(self.sys.pool_state, "on")
        self.assertEqual(last["pool_state"], "undrained")
        self.assertFalse(last["host_off"])
        self.assertTrue(any(a[:3] == ["./tartci", "pool", "undrain"] for a, _ in self.sys.calls))
        self.assertFalse(any("--apply" in a for a, _ in self.sys.calls))

    def test_pre_install_failure_with_refused_pool_on_reinstalls_previous(self) -> None:
        # Install fails 4x (the installer restores itself: previous still
        # runs) and pool on refuses once: reinstall previous, then pool on.
        self.sys.install_rcs = [1]
        self.sys.on_rcs = [7, 7, 7]
        self.sys.rollback_install_rc = 0
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.sys.pool_state, "on")
        self.assertEqual(self.last()["pool_state"], "on")
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        self.assertTrue(any(s["step"] == "reinstall-for-pool-on" and s["ok"]
                            for s in receipt["steps"]))

    def test_left_draining_is_reported_as_draining_not_off(self) -> None:
        self.sys.offplan = [12]
        self.sys.on_rc = 7
        self.sys.refuse_keeps_state = True

        original = self.sys._tartci

        def fake_tartci(args):
            if args[:2] == ["pool", "undrain"]:
                return su.Result(1, "", "undrain unavailable")
            return original(args)
        self.sys._tartci = fake_tartci
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        last = self.last()
        self.assertEqual(last["pool_state"], "draining")
        self.assertTrue(last["host_off"])
        self.assertIn("LEFT DRAINING", last["error"])
        self.assertIn("LEFT THIS HOST DRAINING", su.summary(self.home)["problem"])

    def test_terminated_run_never_reinstalls(self) -> None:
        self.sys.install_rcs = [1]
        self.sys.on_rc = 7

        def terminate_after_install(argv):
            if "--apply" in argv:
                self.sys.hook = None
                raise su.Terminated("SIGTERM")
        self.sys.hook = terminate_after_install
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(sum("--apply" in a for a, _ in self.sys.calls), 1)
        self.assertIn("terminated", self.last()["error"])

    def test_reinstalling_a_failed_target_says_so(self) -> None:
        self.sys.broken_target = True
        self.sys.rollback_install_rc = 1   # rollback fails: target still runs
        self.sys.on_rcs = [0, 7, 7, 7]     # target on; then pool on refuses
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        self.assertTrue(any(s["step"] == "reinstall-for-pool-on"
                            and "FAILED verification" in s["detail"] for s in receipt["steps"]))


if __name__ == "__main__":
    unittest.main()
