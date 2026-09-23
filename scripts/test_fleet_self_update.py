#!/usr/bin/env python3
"""tartci fleet-macos self-update, every branch against a fake System."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
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
        self.peer_markers: dict[str, dict] = {}
        self.peer_clock: dict[str, float] = {}
        self.published = json.loads(json.dumps(PUBLISHED))
        self.checks: dict[str, list] = {}
        self.signing_rc = 0
        self.rollback_install_rc = 0
        self.broken_target = False     # the new generation fails verification
        self.broken_previous = False   # ...and so does the restored one
        self.on_rc = 0
        self.hook = None               # called with argv before dispatch (signal tests)
        self.clone_ok = True
        self.critical: list[list[str]] = []
        self.snapshot_verify_rc = 0
        self.log_lines = f"{T_NEW} {int(NOW - 60)}\n{T_OLD} {int(NOW - 7200)}\n"
        self.ancestor = True
        self.relay = {"ok": True, "probe": "relay authenticated"}
        self.status_after_on = {"state": "on", "participating": True,
                                "fleet": {"managed": True, "fleet_ready": True,
                                          "serving": {"blocked": False}}}
        self.guard_rcs = (2, 0)
        self.bundle_commit = None      # what the built bundle claims (default: target)
        self.codesign_verify_rc = 0
        self.installed_after = None    # commit the host executes after install
        self.checked_out = None

    def now(self) -> float:
        return self.clock

    def run_critical(self, argv, *, cwd=None, env=None, timeout=900):
        self.critical.append(list(argv))
        return self.run(argv, cwd=cwd, env=env, timeout=timeout)

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

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
        if a[0] == "codesign" and "--timestamp" in a:
            return su.Result(self.signing_rc, "", "" if self.signing_rc == 0 else "errSecInternalComponent")
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
                return ok(self.log_lines)
            if "show" in a:
                return ok(json.dumps(self.published))
            if "remote" in a:
                return ok(su.REPO_URL + "\n")
            if "checkout" in a:
                self.checked_out = a[-1]
            return ok()
        if a[0] == "ssh":
            peer = a[5]
            if "pool status" in a[-1]:
                value = self.peers.get(peer)
                return ok(json.dumps(value)) if value else su.Result(255, "", "ssh: unreachable")
            marker = self.peer_markers.get(peer)
            clock = int(self.peer_clock.get(peer, self.clock))
            return ok(f"{clock}\n" + (json.dumps(marker) if marker else ""))
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
        if args[:2] == ["pool", "drain"]:
            self.pool_state = "draining"
            return ok("draining")
        if args[:3] == ["pool", "off", "--plan"]:
            rc = self.offplan.pop(0) if len(self.offplan) > 1 else self.offplan[0]
            return su.Result(rc, "plan")
        if args[:2] == ["pool", "off"]:
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
            if self.on_rc == 0:
                self.pool_state = "on"
            return su.Result(self.on_rc, "on", "" if self.on_rc == 0 else "pool on refused")
        if args[:2] == ["pool", "status"]:
            if (self.broken_target and self.running() != INSTALLED) or \
                    (self.broken_previous and self.running() == INSTALLED):
                return ok(json.dumps({"state": "on", "participating": True,
                                      "fleet": {"managed": True, "fleet_ready": False,
                                                "problems": ["broken"]}}))
            return ok(json.dumps(self.status_after_on))
        if args[:2] == ["launchd", "guard"]:
            return su.Result(self.guard_rcs[0] if "kickstart" in args[-1] else self.guard_rcs[1])
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

    def test_stale_flag_and_render(self) -> None:
        self.sys.log_lines = f"{T_OLD} {int(NOW - 3 * 86400)}\n"
        skew = su.measure_skew(self.cfg, self.sys, INSTALLED, NOW)
        self.assertTrue(skew["stale"])
        self.assertIn("1 commits behind main", su.render_skew(skew))
        self.assertIn("STALE", su.render_skew(skew))
        self.assertIn("UNKNOWN", su.render_skew(None))


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
    def test_peer_draining_refuses_before_any_mutation(self) -> None:
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])

    def test_peer_updating_or_unreachable_or_unmapped_refuses(self) -> None:
        # This fake host is m1, so m5 and studio are its peers.
        self.sys.peer_markers["m5"] = {"host_id": "m5", "target": T_OLD, "ts": NOW - 60}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.sys.peer_markers.clear()
        del self.sys.peers["m5"]  # unreachable
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])

    def test_stale_peer_marker_is_ignored(self) -> None:
        self.sys.peer_markers["m5"] = {"host_id": "m5", "target": T_OLD, "ts": NOW - 4 * 3600}
        self.assertEqual(self.apply(), su.EXIT_OK)

    def test_marker_age_uses_the_peer_clock(self) -> None:
        # The peer wrote its marker a minute ago on ITS clock, which runs 5h
        # behind ours; comparing against our clock would call it stale.
        self.sys.peer_clock["m5"] = NOW - 5 * 3600
        self.sys.peer_markers["m5"] = {"host_id": "m5", "target": T_OLD, "ts": NOW - 5 * 3600 - 60}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])

    def test_stagger_is_stable_and_bounded(self) -> None:
        self.assertEqual(su.stagger_seconds("m5"), su.stagger_seconds("m5"))
        self.assertTrue(0 <= su.stagger_seconds("studio") < 600)


class FloorTests(Base):
    def test_idle_by_design_last_server_passes_the_flag_and_logs_the_rule(self) -> None:
        self.sys.floor = {"allowed": False, "reason": "last_serving_host", "findings": [
            {"label": "pulp-release-tagged", "verdict": "last_serving_host"}]}
        self.assertEqual(self.apply(), su.EXIT_OK)
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


class MidJobWaitTests(Base):
    def test_waits_while_mid_job_then_proceeds(self) -> None:
        self.sys.offplan = [12, 12, 0]
        self.assertEqual(self.apply(), su.EXIT_OK)
        self.assertEqual(self.sys.clock - NOW, 2 * 45)

    def test_timeout_restores_the_host_to_on(self) -> None:
        self.sys.offplan = [12]
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.sys.pool_state, "on")
        self.assertIn("still mid-job", self.last()["error"])
        joined = [" ".join(a) for a, _ in self.sys.calls]
        self.assertFalse(any(c.startswith("./tartci pool off") and "--plan" not in c for c in joined))
        self.assertFalse(any("--apply" in c for c in joined))


class InstallFailureTests(Base):
    def test_transient_install_failure_is_retried(self) -> None:
        self.sys.install_rcs = [1, 1, 0]
        self.assertEqual(self.apply(), su.EXIT_OK)

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
        self.assertEqual(self.apply(), su.EXIT_OK)

    def test_refusal_does_not_spend_the_attempt(self) -> None:
        self.sys.peers["m5"] = {"state": "off", "participating": False}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.sys.peers["m5"] = {"state": "on", "participating": True}
        self.assertEqual(self.apply(), su.EXIT_OK)


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


class RelayTests(Base):
    relay = True

    def test_relay_reconciled_after_install(self) -> None:
        self.assertEqual(self.apply(), su.EXIT_OK)
        joined = [" ".join(a) for a, _ in self.sys.calls]
        relay = next(i for i, c in enumerate(joined) if "network_profile.py reconcile" in c)
        install = next(i for i, c in enumerate(joined) if "--apply" in c)
        self.assertGreater(relay, install)

    def test_relay_failure_fails_and_restores(self) -> None:
        self.sys.relay = {"ok": False, "reason": "probe failed"}
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertIn("relay", self.last()["error"])
        self.assertEqual(self.sys.pool_state, "on")


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
                       lambda s: setattr(s, "codesign_verify_rc", 1)):
            with self.subTest():
                self.tearDown()
                self.setUp()
                mutate(self.sys)
                self.assertEqual(self.apply(), su.EXIT_REFUSED)
                self.assertEqual(self.sys.mutations(), [])
                self.assertEqual(self.pin().read_text(), "0" * 64 + "\n")

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
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
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
            "stale": False, "measured_at": "now"})
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
        self.assertEqual(self.apply(), su.EXIT_OK)

    def test_old_refusal_receipts_are_pruned(self) -> None:
        self.sys.peers["m5"] = {"state": "off", "participating": False}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
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
        self.assertEqual(self.apply(), su.EXIT_OK)
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
            self.assertEqual(hosts, {"m1": "m1", "studio": "m3", "m5": "m5", "x9": None})
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
        self.assertEqual(self.apply(), su.EXIT_OK)
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
        self.assertEqual(self.apply(), su.EXIT_OK)
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


if __name__ == "__main__":
    unittest.main()
