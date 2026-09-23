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
    {"host_id": "m1"}, {"host_id": "studio"}, {"host_id": "m5"}]}


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
        self.peers = {"m1": {"state": "on", "participating": True},
                      "m5": {"state": "on", "participating": True},
                      "studio": {"state": "on", "participating": True}}
        self.peer_markers: dict[str, dict] = {}
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
        if a[0] == "git":
            if "rev-parse" in a:
                return ok(T_NEW + "\n")
            if "cat-file" in a:
                return ok()
            if "merge-base" in a:
                return su.Result(0 if self.ancestor else 1)
            if "log" in a:
                return ok(self.log_lines)
            if "show" in a:
                return ok(json.dumps(PUBLISHED))
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
            return ok(json.dumps(marker) if marker else "")
        if a[:2] == ["python3", "scripts/capacity_floor.py"]:
            return su.Result(0 if self.floor.get("allowed") else 3, json.dumps(self.floor))
        if a[:2] == ["python3", "scripts/network_profile.py"]:
            return su.Result(0 if self.relay.get("ok") else 6, json.dumps(self.relay))
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

    def _shim(self, args):
        if args[:2] == ["pool", "on"]:
            self.pool_state = "on"
            return ok("on")
        if args[:2] == ["pool", "status"]:
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
        self.cfg = su.Config(home=self.home, peers={"m1": "m1", "m5": "m5", "studio": "studio"},
                             poll_seconds=45, wait_seconds=600)
        self.sys = FakeSystem(self.home, sealed=self.sealed)
        if self.sealed:
            helper = {"path": str(self.home / "libexec" / "TartCILauncher.app"),
                      "approval_sha256_path": str(self.home / ".config/tartci/m3-launcher-approved.sha256")}
            original = su.launch_helper
            su.launch_helper = lambda cfg: helper
            self.addCleanup(setattr, su, "launch_helper", original)

    def tearDown(self) -> None:
        os.environ.pop("TARTCI_HOME", None)
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
        self.assertIn("--first-parent", " ".join(self.sys.calls[-1][0]))

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
        del self.sys.peers["m5"]
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.sys.peers["m5"] = {"state": "on", "participating": True}
        del self.cfg.peers["m5"]
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])

    def test_stale_peer_marker_is_ignored(self) -> None:
        self.sys.peer_markers["m5"] = {"host_id": "m5", "target": T_OLD, "ts": NOW - 4 * 3600}
        self.assertEqual(self.apply(), su.EXIT_OK)

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
    def test_each_verification_failure_is_reported(self) -> None:
        cases = {
            "not ready": lambda s: s.status_after_on["fleet"].update(fleet_ready=False),
            "serving BLOCKED": lambda s: s.status_after_on["fleet"].update(serving={"blocked": True}),
            "runs 2222": lambda s: setattr(s, "installed_after", "2" * 40),
            "guard": lambda s: setattr(s, "guard_rcs", (0, 0)),
        }
        for needle, mutate in cases.items():
            with self.subTest(needle=needle):
                self.tearDown()
                self.setUp()
                mutate(self.sys)
                self.assertEqual(self.apply(), su.EXIT_FAILED)
                self.assertIn(needle, self.last()["error"])
                self.assertEqual(self.sys.pool_state, "on")


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
        backups = list(self.pin().parent.glob("m3-launcher-approved.sha256.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "0" * 64 + "\n")
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
            settings = Path(td) / "self-update.toml"
            settings.write_text('[peers]\nm1 = "m1"\n')
            env = {**os.environ, "TARTCI_AGENTS_DIR": str(Path(td) / "agents"),
                   "TARTCI_SELF_UPDATE_SETTINGS": str(settings)}
            proc = subprocess.run(["bash", str(ROOT / "scripts/install_self_update_agent.sh")],
                                  env=env, text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("plan only", proc.stdout)
            self.assertFalse((Path(td) / "agents").exists())
            settings.unlink()
            refused = subprocess.run(["bash", str(ROOT / "scripts/install_self_update_agent.sh")],
                                     env=env, text=True, capture_output=True)
            self.assertEqual(refused.returncode, 3)
        rendered = subprocess.run(
            [sys.executable, str(ROOT / "scripts/render_launchd_template.py"),
             str(ROOT / "launchd/com.danielraffel.tartci.self-update.plist.template"),
             "--set", "HOME=/Users/x"], capture_output=True, check=True)
        value = plistlib.loads(rendered.stdout)
        self.assertEqual(value["StartInterval"], 1800)
        self.assertEqual(value["ProgramArguments"][-4:],
                         ["fleet-macos", "self-update", "--apply", "--scheduled"])

    def test_watchdog_never_interrupts_the_agent(self) -> None:
        import tartci_launchd_watchdog as wd
        self.assertIn("com.danielraffel.tartci.self-update", wd.UNINTERRUPTIBLE_AGENTS)
        self.assertIn(3, wd.APPLICATION_EXIT_CODES["com.danielraffel.tartci.self-update"])


if __name__ == "__main__":
    unittest.main()
