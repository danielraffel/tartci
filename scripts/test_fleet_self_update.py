#!/usr/bin/env python3
"""tartci fleet-macos self-update, every branch against a fake System."""

from __future__ import annotations

import testing_support  # noqa: E402
testing_support.skip_module_without_tomllib()
import contextlib
import io
import json
import os
import plistlib
import re
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
import tart_pin
import fleet_source

ROOT = Path(__file__).resolve().parents[1]
INSTALLED = "1" * 40
TART_SHA, SOFTNET_SHA = "a" * 64, "b" * 64
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
        self.store_result = {"ms": 4.0}  # the pre-drain write into the Tart store
        self.store_writes: list[str] = []
        # None: healthy, checked a minute before the fake clock (as idle
        # lanes keep it); a test sets a tuple to pin the record.
        self.store_health_result = None
        # Tart: installed, what the tap offers, how `brew upgrade` ends, and
        # whether a clone of the golden boots.
        self.tart: str | None = "2.40.1"
        self.softnet: str | None = "0.24.0"
        self.tap = {"tart": ("2.40.1", TART_SHA), "softnet": ("0.24.0", SOFTNET_SHA)}
        self.tap_ok = True
        self.brew_upgrade_rc = 0
        self.brew_upgrade_installs: str | None = None  # default: the tap version
        self.tart_running = False                     # a `tart run` process exists
        self.boot_ok = True                           # a booted clone reports an IP
        self.boot_ip = "192.168.64.5\n"               # what `tart ip` prints when it boots
        self.tart_stop_rc = self.tart_delete_rc = 0
        self.peer_tart: dict[str, dict] = {}          # ssh target -> its tart-verified.json
        self.peer_tart_clock: float | None = None     # the canary's clock, if not ours
        self.peer_tart_rc = 0
        # Installed kegs per formula, like the Cellar. `brew link` links the
        # HIGHEST installed version (Homebrew 7 latest_kegs), whatever opt says.
        self.kegs: dict[str, set[str]] = {"tart": set(), "softnet": set()}
        self.uninstall_rc = 0
        # sha256 of the archive `brew --cache` names, per formula.
        self.archive_sha = {"tart": TART_SHA, "softnet": SOFTNET_SHA}
        self.tart_delete_rcs: list[int] = []        # successive delete results, then tart_delete_rc
        self.offplan = [0]             # successive `pool off --plan` exit codes
        self.offplan_text = "plan"     # its stdout
        self.install_rcs = [0]         # successive `install --apply` exit codes
        self.dry_run_rc = 0
        self.preflight_rc = 0
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
        self.peer_paused: set[str] = set()   # peers whose self-update an operator paused
        self.peer_halted: set[str] = set()   # peers whose self-update halted on failures
        self.published = json.loads(json.dumps(PUBLISHED))
        self.checks: dict[str, list] = {}
        self.workflow_runs: dict[str, list] = {}   # sha -> actions/runs entries
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
        self.remote_url = su.REPO_URL + "\n"
        self.api_repository = su.DEFAULT_REPOSITORY
        self.unreadable_repositories: set[str] = set()
        # repository -> results to return, one per read, before answering normally
        self.repository_failures: dict[str, list] = {}
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
        self.off_main: set[str] = set()   # commits `merge-base --is-ancestor <c> origin/main` rejects
        self.built_team: str | None = None  # a built launcher's TeamIdentifier, if not the live one's
        self.cat_file_rc = 0
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
        self.launchd_print = None
        self.target_orchestrator_rc = 0
        self.target_orchestrator_calls: list[list[str]] = []
        self.v2_generation: Path | None = None   # launcher v2: the pointer's generation
        self.live_team = "95CX6P84C4"            # the live app's TeamIdentifier
        self.keychain_identities = [IDENTITY]    # Developer ID identities (or (id, team))
        self.live_identity = None                # the live app's certificate (None: IDENTITY)
        self.built_identity = None               # what the last launcher build signed with

    def now(self) -> float:
        return self.clock

    def run_critical(self, argv, *, cwd=None, env=None, timeout=900, record=None):
        self.critical.append(list(argv))
        return self.run(argv, cwd=cwd, env=env, timeout=timeout)

    def store_write(self, directory, timeout=10.0):
        self.store_writes.append(directory)
        if isinstance(self.store_result, list):  # successive results
            return dict(self.store_result.pop(0) if len(self.store_result) > 1
                        else self.store_result[0])
        return dict(self.store_result)

    def store_health(self, directory):
        if self.store_health_result is None:
            return "healthy", "directory_metadata_ok", su._iso(self.clock - 60)
        return self.store_health_result

    def set_tart(self, version: str) -> None:
        """Install and link this Tart keg; earlier kegs stay in the Cellar."""
        self.tart = version
        self._link("tart", version)

    def set_softnet(self, version: str) -> None:
        self.softnet = version
        self._link("softnet", version)

    def _link(self, name: str, version: str, keep: bool = False) -> None:
        assert tart_pin.OPT != Path("/opt/homebrew/opt"), "tart_pin.OPT is not patched"
        self.kegs[name].add(version)
        if tart_pin.OPT.is_dir():
            link = tart_pin.OPT / name
            link.unlink(missing_ok=True)
            link.symlink_to(f"../Cellar/{name}/{version}")

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
        self.env_calls = getattr(self, "env_calls", [])
        self.env_calls.append(dict(env or {}))
        a = list(argv)
        joined = " ".join(a)
        if self.hook:
            self.hook(a)
        if a[:2] == ["launchctl", "print"]:
            return self.launchd_print or su.Result(1, "", "not configured")
        if a[0] in (tart_pin.TART, tart_pin.BREW, "/bin/ln"):
            # A test must never reach the real /opt/homebrew/opt.
            assert tart_pin.OPT != Path("/opt/homebrew/opt"), "tart_pin.OPT is not patched"
        if a[0] == tart_pin.TART and a[1:] == ["--version"]:
            return ok(self.tart + "\n") if self.tart else su.Result(127, "", "not installed")
        if a[0] == tart_pin.TART and a[1] == "clone":
            return ok()
        if a[0] == tart_pin.TART and a[1] == "stop":
            return su.Result(self.tart_stop_rc, "", "" if not self.tart_stop_rc else "stop failed")
        if a[0] == tart_pin.TART and a[1] == "delete":
            rc = self.tart_delete_rcs.pop(0) if self.tart_delete_rcs else self.tart_delete_rc
            return su.Result(rc, "", "" if not rc else "busy")
        if a[:2] == [tart_pin.BREW, "--cache"]:
            return ok(f"/fake/Caches/Homebrew/downloads/{a[-1].rsplit('/', 1)[-1]}.tar.gz\n")
        if a[:3] == ["/usr/bin/shasum", "-a", "256"]:
            name = a[-1].rsplit("/", 1)[-1].split(".tar.gz")[0]
            return ok(f"{self.archive_sha[name]}  {a[-1]}\n")
        if a[0] == tart_pin.TART and a[1] == "ip":
            return ok(self.boot_ip) if self.boot_ok else su.Result(1, "", "no IP")
        if a[0] == "/bin/sh" and "--no-graphics" in joined:
            return ok()
        if a[:3] == [tart_pin.BREW, "info", "--json=v2"]:
            if not self.tap_ok:
                return su.Result(1, "", "Error: No available formula")
            return ok(json.dumps({"casks": [], "formulae": [
                {"full_name": f"openai/tools/{name}", "versions": {"stable": version},
                 "urls": {"stable": {"checksum": sha}}}
                for name, (version, sha) in self.tap.items()]}))
        if a[:2] == [tart_pin.BREW, "upgrade"]:
            if self.brew_upgrade_rc == 0:
                self.set_tart(self.brew_upgrade_installs or self.tap["tart"][0])
                self.set_softnet(self.tap["softnet"][0])
            return su.Result(self.brew_upgrade_rc, "", "" if self.brew_upgrade_rc == 0
                             else "Error: Failed to download resource")
        if a[:2] == [tart_pin.BREW, "uninstall"]:
            # Resolves the keg through opt (default_kegs) and removes only it.
            name = a[-1].rsplit("/", 1)[-1]
            if self.uninstall_rc:
                return su.Result(self.uninstall_rc, "", "Error: uninstall failed")
            keg = os.readlink(tart_pin.OPT / name).rsplit("/", 1)[-1]
            self.kegs[name].discard(keg)
            (tart_pin.OPT / name).unlink()
            setattr(self, name, None)
            return ok()
        if a[:2] == [tart_pin.BREW, "link"]:
            name = a[-1].rsplit("/", 1)[-1]
            if not self.kegs[name]:
                return su.Result(1, "", f"Error: No such keg: {name}")
            latest = max(self.kegs[name], key=tart_pin.vkey)
            self._link(name, latest, keep=True)
            setattr(self, name, latest)
            return ok()
        if a[:2] == [tart_pin.BREW, "unlink"] or a[0] == "/bin/ln":
            return ok()
        if a[:2] == ["pgrep", "-f"] and a[2] == tart_pin.TART_RUN_PATTERN:
            return su.Result(0, "4242 tart run m5-gate\n") if self.tart_running else su.Result(1)
        if a[0] == "ssh" and a[-1] == su.TART_VERIFIED_SCRIPT:
            if self.peer_tart_rc:
                return su.Result(self.peer_tart_rc, "", "ssh: connect timed out")
            value = self.peer_tart.get(a[-2])
            clock = self.clock if self.peer_tart_clock is None else self.peer_tart_clock
            return ok(f"{int(clock)}\n" + (json.dumps(value) if value else ""))
        if a[0] == FAKE_GH:
            repository = a[2].removeprefix("repos/").split("/commits/")[0]
            if self.repository_failures.get(repository):
                return self.repository_failures[repository].pop(0)
            if repository in self.unreadable_repositories:
                return su.Result(1, "", "not found")
            if "/actions/runs?head_sha=" in a[2]:
                sha = a[2].split("head_sha=")[1].split("&")[0]
                if sha not in self.workflow_runs:
                    return su.Result(1, "", "not recorded")
                return ok(json.dumps({"workflow_runs": self.workflow_runs[sha]}))
            if "check-runs" in a[2]:
                sha = a[2].split("/commits/")[1].split("/")[0]
                runs = self.checks.get(sha, [{"name": "lint", "status": "completed",
                                              "conclusion": "success"}])
                return ok(json.dumps({"check_runs": runs}))
            if "/commits/" not in a[2]:
                return ok(json.dumps({"full_name": self.api_repository}))
            return ok(json.dumps({"sha": a[2].split("/")[-1]}))
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
                return su.Result(self.cat_file_rc, "", "missing commit" if self.cat_file_rc else "")
            if "merge-base" in a:
                if a[-1] == "origin/main":
                    return su.Result(1 if a[-2] in self.off_main else 0)
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
                return ok(self.remote_url)
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
                         if pool_since is not None else "")
                      + ("\n" + su.PAUSED_SEPARATOR + "\n"
                         + ("paused\n" if peer in self.peer_paused else "")
                         + ("halted\n" if peer in self.peer_halted else "")
                         if peer in self.peer_paused | self.peer_halted else ""))
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
            return self._render(a, env)
        if a[:3] == ["python3", "scripts/support_agents.py", "check-templates"]:
            return su.Result(self.agents_check_rc, "4 declared agents render" if
                             self.agents_check_rc == 0 else "reclaim: missing template")
        if a[0] == "codesign" and "--extract-certificates" in joined:
            return ok()
        if a[0] == "codesign" and "-d" in a and "--verbose=2" in a:
            team = (self.built_team if self.built_team and "/builds/" in a[-1]
                    else self.live_team)
            return su.Result(0, "", f"Identifier=x\nTeamIdentifier={team}\n")
        if a[0] == "codesign" and "--verify" in a:
            return su.Result(self.codesign_verify_rc, "", "" if self.codesign_verify_rc == 0 else "invalid signature")
        if a[0] == "openssl":
            live = self.live_identity or IDENTITY
            return ok("SHA1 Fingerprint=" + ":".join(live[i:i + 2] for i in range(0, 40, 2)) + "\n")
        if a[0] == "security":
            rows = [(item, "95CX6P84C4") if isinstance(item, str) else item
                    for item in self.keychain_identities]
            return ok("".join(f'  {n}) {identity} "Developer ID Application: X ({team})"\n'
                              for n, (identity, team) in enumerate(rows, 1)))
        if a[:2] == ["bash", "scripts/build_macos_launcher.sh"]:
            return self._build(a, env)
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
        if args[:2] == ["fleet-macos", "self-update"]:
            self.target_orchestrator_calls.append(list(args))
            return su.Result(self.target_orchestrator_rc, "target orchestrator", ""
                             if self.target_orchestrator_rc == 0 else "target refused")
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
            if "--apply" in args and self.v2_generation is not None:
                # The installer records the layout it wrote in the receipt.
                layout = ("v1" if "--launcher-layout" in args
                          and args[args.index("--launcher-layout") + 1] == "v1"
                          else "v2" if "--launch-set-identity" in args else None)
                receipt = self.home / ".config/tartci/macos-fleet-install.json"
                if layout:
                    value = json.loads(receipt.read_text())
                    value["launch_helper"] = {"schema": 2, "layout": "v2"} if layout == "v2" \
                        else {"schema": 1}
                    receipt.write_text(json.dumps(value))
            if "--apply" not in args:
                if "--preflight" in args:
                    return su.Result(self.preflight_rc, "preflight ok" if self.preflight_rc == 0 else "preflight rejected",
                                     "" if self.preflight_rc == 0 else "installer preflight failed")
                return su.Result(self.dry_run_rc, "dry run" if self.dry_run_rc == 0 else "preflight rejected",
                                 "" if self.dry_run_rc == 0 else "installer preflight failed")
            if self.checked_out == INSTALLED:  # a rollback reinstall
                if self.rollback_install_rc == 0:
                    self._set_installed(INSTALLED)
                return su.Result(self.rollback_install_rc, "", "rollback install failed")
            rc = self.install_rcs.pop(0) if len(self.install_rcs) > 1 else self.install_rcs[0]
            if rc == 0:
                self._set_installed(self.installed_after or self.checked_out)
                if "--launch-helper-source" in args and self.built_identity:
                    # The installed app is the one just built, signed by it.
                    self.live_identity = self.built_identity
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
            text = (self.offplan_text.pop(0) if isinstance(self.offplan_text, list)
                    and len(self.offplan_text) > 1 else
                    self.offplan_text[0] if isinstance(self.offplan_text, list)
                    else self.offplan_text)
            return su.Result(rc, text)
        if args[:2] == ["pool", "off"]:
            if self.off_rc:
                return su.Result(self.off_rc, "", "refusing pool off: capacity for required label "
                                 "'pulp-build-merge-group' could not be determined: "
                                 "runner_census_timeout")
            self.pool_state = "off"
            return ok("off")
        raise AssertionError(f"unexpected ./tartci {args}")

    def running(self):
        if self.v2_generation is not None:
            return json.loads((self.v2_generation / ".tartci-support-manifest.json")
                              .read_text())["source_commit"]
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
            return su.Result(rc, "on", "" if rc == 0 else getattr(self, "on_text", None) or
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
        if self.v2_generation is not None:
            (self.v2_generation / ".tartci-support-manifest.json").write_text(
                json.dumps({"source_commit": commit}))
            return
        if self.sealed:
            meta = self.home / "libexec/TartCILauncher.app/Contents/Resources/bundle.json"
            meta.write_text(json.dumps({"source_commit": commit}))
        else:
            receipt = self.home / ".config/tartci/macos-fleet-install.json"
            receipt.write_text(json.dumps({"support": {"source_commit": commit}}))

    def _render(self, a, env=None):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/macos_fleet_lanes.py"), *a[2:]],
                              capture_output=True, text=True, env={**os.environ, **(env or {})})
        return su.Result(proc.returncode, proc.stdout, proc.stderr)

    def _build(self, a, env=None):
        self.built_identity = a[a.index("--identity") + 1]
        out = Path(a[a.index("--output") + 1])
        approval = Path(a[a.index("--approval-output") + 1])
        profile = Path(a[a.index("--profile") + 1])
        rendered = out.parent / "render"
        render_env = {**os.environ, **(env or {})}
        render_env["TARTCI_RENDER_PYTHON"] = a[a.index("--python") + 1]
        subprocess.run([sys.executable, str(ROOT / "scripts/macos_fleet_lanes.py"), "render",
                        str(profile), "--output", str(rendered)], check=True,
                       capture_output=True, env=render_env)
        lanes = {}
        for plist in rendered.glob("*.plist"):
            env = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]
            expected = os.path.realpath(render_env["TARTCI_RENDER_PYTHON"])
            assert env["TARTCI_PYTHON"] == expected
            assert env["SHIPYARD_GHAPP_PYTHON_BINARY"] == expected
            lanes[env["TARTCI_QUEUE_LANE_ID"]] = {"environment": dict(sorted(env.items()))}
        res = out / "Contents" / "Resources"
        res.mkdir(parents=True)
        if "--layout" in a and a[a.index("--layout") + 1] == "v2":
            (res / "inputs.json").write_text(json.dumps({"schema": 1, "sha256": "f" * 64}))
            approval.write_text("d" * 64 + "\n")
            return su.Result(0, "", "")
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
    # Sealed-launcher tests about other gates treat the launcher change as
    # acknowledged; LauncherConsentTests exercises the consent gate itself.
    consent_gate = False

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
        self.bodies: list[str] = []
        for name, fake in (("_open_issue", lambda t, b: self.issues.append(("open", t))
                            or self.bodies.append(b) or (0, "77")),
                           ("_close_issue", lambda n: self.issues.append(("close", n)) or (0, "closed")),
                           ("_patch_issue_body", lambda n, b: self.issues.append(("patch", n))
                            or self.bodies.append(b) or (0, n))):
            original = getattr(host_off, name)
            setattr(host_off, name, fake)
            self.addCleanup(setattr, host_off, name, original)
        if self.sealed:
            helper = {"path": str(self.home / "libexec" / "TartCILauncher.app"),
                      "approval_sha256_path": str(self.home / ".config/tartci/m3-launcher-approved.sha256")}
            original = su.launch_helper
            su.launch_helper = lambda cfg: helper
            self.addCleanup(setattr, su, "launch_helper", original)
            if not self.consent_gate:
                gate = mock.patch.object(su, "launcher_change_refusal", return_value=None)
                gate.start()
                self.addCleanup(gate.stop)

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

    def test_scheduled_pause_refuses_real_apply_before_any_mutation(self) -> None:
        su._write_json(self.cfg.state_dir / "scheduled-pause.json",
                       {"at": su._iso(self.sys.now()), "reason": "bad rollout"})
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])
        self.assertIn("self-update PAUSED", "\n".join(su.status_lines(self.cfg.state_dir)))


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


class TargetOrchestratorHandoffTests(Base):
    sealed = True

    def test_handoff_executes_a_real_dispatcher_child(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            checkout = home / ".local/share/tartci/update-checkout"
            checkout.mkdir(parents=True)
            dispatcher = checkout / "tartci"
            dispatcher.write_text("#!/bin/sh\nprintf 'HANDOFF_EXEC:%s\\n' \"$1\"\nexit 23\n")
            dispatcher.chmod(0o755)
            script = ("import fleet_self_update as su; from pathlib import Path; "
                      f"cfg=su.Config(Path({str(home)!r})); "
                      f"su.orchestrator_generation=lambda: {INSTALLED!r}; "
                      "su.launch_helper=lambda cfg: {'path': 'sealed'}; "
                      f"raise SystemExit(su.handoff_target_orchestrator(cfg, {T_OLD!r}, "
                      "apply=True, scheduled=False, refresh=False) or 0)")
            env = {**os.environ, "PYTHONPATH": str(ROOT / "scripts")}
            result = subprocess.run([sys.executable, "-c", script], env=env,
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 23)
        self.assertIn("HANDOFF_EXEC:fleet-macos", result.stdout)

    def test_missing_dispatcher_is_a_refused_receipt(self) -> None:
        with mock.patch.object(su, "orchestrator_generation", return_value=INSTALLED):
            rc = self.apply()
        self.assertEqual(rc, su.EXIT_REFUSED)
        receipt = json.loads(next((self.cfg.state_dir / "attempts").glob("*.json")).read_text())
        self.assertEqual(receipt["status"], "refused")
        self.assertIn("target dispatcher unavailable", receipt["error"])
        self.assertEqual(self.sys.mutations(), [])

    def test_non_executable_dispatcher_is_a_refused_receipt(self) -> None:
        dispatcher = self.cfg.checkout / "tartci"
        dispatcher.write_text("#!/bin/sh\nexit 0\n")
        dispatcher.chmod(0o644)
        with mock.patch.object(su, "orchestrator_generation", return_value=INSTALLED):
            rc = self.apply()
        self.assertEqual(rc, su.EXIT_REFUSED)
        receipt = json.loads(next((self.cfg.state_dir / "attempts").glob("*.json")).read_text())
        self.assertEqual(receipt["status"], "refused")
        self.assertIn("PermissionError", receipt["error"])
        self.assertEqual(self.sys.mutations(), [])

    def test_an_installed_orchestrator_hands_off_to_the_target_checkout(self) -> None:
        calls = []
        with mock.patch.object(su, "orchestrator_generation", return_value=INSTALLED), \
                mock.patch.object(su.os, "execve", side_effect=lambda *args: calls.append(args)):
            self.assertTrue(su.handoff_target_orchestrator(
                self.cfg, T_OLD, apply=True, scheduled=True, refresh=False))
        self.assertEqual(calls[0][0], str(self.cfg.checkout / "tartci"))
        self.assertEqual(calls[0][1], [
            str(self.cfg.checkout / "tartci"), "fleet-macos", "self-update", "--apply",
            "--target", T_OLD, "--home", str(self.home), "--scheduled"])
        self.assertEqual(calls[0][2]["TARTCI_SELF_UPDATE_TARGET_ORCHESTRATOR"], "1")

    def test_handoff_is_disabled_for_plan_unsealed_refresh_or_marker(self) -> None:
        with mock.patch.object(su, "orchestrator_generation", return_value=INSTALLED), \
                mock.patch.object(su.os, "execve") as execve:
            self.assertFalse(su.handoff_target_orchestrator(
                self.cfg, T_OLD, apply=False, scheduled=False, refresh=False))
            self.assertFalse(su.handoff_target_orchestrator(
                self.cfg, T_OLD, apply=True, scheduled=False, refresh=True))
            with mock.patch.dict(os.environ, {"TARTCI_SELF_UPDATE_TARGET_ORCHESTRATOR": "1"}):
                self.assertFalse(su.handoff_target_orchestrator(
                    self.cfg, T_OLD, apply=True, scheduled=False, refresh=False))
        execve.assert_not_called()

    def test_apply_calls_handoff_before_the_installed_builder(self) -> None:
        with mock.patch.object(su, "handoff_target_orchestrator", return_value=True) as handoff:
            self.assertEqual(self.apply(), su.EXIT_OK)
        handoff.assert_called_once_with(self.cfg, T_OLD, apply=True, scheduled=False, refresh=False)


class HandoffNegativeSelectionTests(Base):
    def test_unsealed_hosts_never_handoff(self) -> None:
        with mock.patch.object(su, "orchestrator_generation", return_value=INSTALLED), \
                mock.patch.object(su.os, "execve") as execve:
            self.assertFalse(su.handoff_target_orchestrator(
                self.cfg, T_OLD, apply=True, scheduled=False, refresh=False))
        execve.assert_not_called()

    def test_soaking_target_never_reaches_handoff(self) -> None:
        self.sys.log_lines = f"{T_NEW} {int(NOW - 60)}\n"
        with mock.patch.object(su, "handoff_target_orchestrator") as handoff:
            self.assertEqual(su.plan_or_apply(
                self.cfg, self.sys, apply=True, target_ref=T_NEW, explicit=True),
                su.EXIT_REFUSED)
        handoff.assert_not_called()

    def test_unverified_target_never_reaches_handoff(self) -> None:
        self.sys.log_lines = f"{T_OLD} {int(NOW - 7200)}\n"
        self.sys.checks[T_OLD] = [{"name": "lint", "status": "completed",
                                   "conclusion": "failure"}]
        with mock.patch.object(su, "handoff_target_orchestrator") as handoff:
            self.assertEqual(su.plan_or_apply(
                self.cfg, self.sys, apply=True, target_ref=T_OLD, explicit=True),
                su.EXIT_REFUSED)
        handoff.assert_not_called()

    def test_non_first_parent_target_never_reaches_handoff(self) -> None:
        self.sys.ancestor = False
        with mock.patch.object(su, "handoff_target_orchestrator") as handoff:
            self.assertEqual(su.plan_or_apply(
                self.cfg, self.sys, apply=True, target_ref=T_OLD, explicit=True),
                su.EXIT_UNKNOWN)
        handoff.assert_not_called()


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
        # Peer observation intentionally leads the procedure: a later gate
        # may refuse before queue handling, but must not skip the unreadable
        # streak that breaks a dead-peer deadlock.
        order = [first(lambda c: c.startswith("ssh "))]
        order += [first(lambda c, n=n: n in c) for n in (
            "support-manifest write", "fleet-macos validate", "fleet-macos install",
            "capacity_floor.py", "pool drain", "pool off --plan")]
        order.append(first(lambda c: c.startswith("./tartci pool off") and "--plan" not in c))
        order += [first(lambda c, n=n: n in c) for n in ("--apply", "pool on")]
        order.append(first(lambda c: "pool status" in c and not c.startswith("ssh")))
        self.assertEqual(order, sorted(order))
        self.assertEqual(self.last()["status"], "succeeded")
        self.assertFalse((self.cfg.state_dir / "active.json").exists())
        # No launch helper, no launcher change: the store is never written.
        self.assertEqual([], self.sys.store_writes)
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
        # Control, same instrument: only the drain's age changed. Inside the
        # grace a self-update's marker may have been written between the reads.
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.sys.peer_pool_since["m5"] = NOW - 600
        self.assertDeferred(self.apply())
        self.assertEqual(self.sys.mutations(), [])
        self.assertIn("peer m5 is draining (participating=False), pool state unchanged for 10 min",
                      su.waiting_ticket(self.cfg)["reason"])

    def test_an_operator_drain_past_the_grace_no_longer_holds_the_turn(self) -> None:
        # No update marker at all: a self-update writes its marker before it
        # drains, so this is an operator's drain (a containment drain).
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.sys.peer_pool_since["m5"] = NOW - su.PEER_DRAIN_GRACE_SECONDS - 60
        peer = su.read_peer(self.cfg, self.sys, "m5", "m5")
        self.assertEqual((False, True), (peer["busy"], peer["off"]))
        self.assertIn("an operator drain: no update marker for more than 15 min",
                      peer["evidence"])
        self.assertUpdated(self.apply())

    def test_a_paused_peer_that_is_draining_no_longer_holds_the_turn(self) -> None:
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.sys.peer_pool_since["m5"] = NOW - 60       # inside the grace
        self.sys.peer_paused.add("m5")
        peer = su.read_peer(self.cfg, self.sys, "m5", "m5")
        self.assertEqual((False, True), (peer["busy"], peer["off"]))
        self.assertIn("its self-update is paused, so no update is in this drain",
                      peer["evidence"])
        self.assertUpdated(self.apply())

    def test_a_halted_peer_that_is_draining_no_longer_holds_the_turn(self) -> None:
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.sys.peer_pool_since["m5"] = NOW - 60
        self.sys.peer_halted.add("m5")
        peer = su.read_peer(self.cfg, self.sys, "m5", "m5")
        self.assertEqual((False, True), (peer["busy"], peer["off"]))
        self.assertIn("its self-update is halted, so no update is in this drain",
                      peer["evidence"])

    def test_the_evidence_names_the_rule_that_released_the_peer(self) -> None:
        # The deferred host's log must say WHICH rule let it go, so an operator
        # reading it knows whether to look for a pause, a halt or a drain.
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        cases = (({"m5"}, set(), NOW - 60, "self-update is paused,"),
                 (set(), {"m5"}, NOW - 60, "self-update is halted,"),
                 ({"m5"}, {"m5"}, NOW - 60, "self-update is halted and paused,"),
                 (set(), set(), NOW - su.PEER_DRAIN_GRACE_SECONDS - 60,
                  "an operator drain: no update marker for more than 15 min"))
        for paused, halted, since, rule in cases:
            with self.subTest(rule=rule):
                self.sys.peer_paused = {"m5"} if paused else set()
                self.sys.peer_halted = set(halted)
                self.sys.peer_pool_since["m5"] = since
                evidence = su.read_peer(self.cfg, self.sys, "m5", "m5")["evidence"]
                self.assertIn(rule, evidence)
                self.assertIn("left to the capacity floor", evidence)

    def test_the_peer_command_reports_a_pause_and_the_halt_rule_like_halt_reason(self) -> None:
        # The real marker command, run in a shell against a fixture state dir:
        # its halted verdict must agree with halt_reason on the same receipts.
        import subprocess  # noqa: PLC0415
        state = self.home / "peer" / ".tartci" / "state" / "self-update"
        (state / "attempts").mkdir(parents=True)
        env = {"HOME": str(self.home / "peer"), "TARTCI_HOME": str(self.home / "peer/.tartci"),
               "PATH": "/usr/bin:/bin"}

        def words() -> set[str]:
            out = subprocess.run(["/bin/bash", "-c", su._PEER_MARKER], env=env,
                                 capture_output=True, text=True, check=True).stdout
            return set(out.partition(su.PAUSED_SEPARATOR)[2].split())
        self.assertEqual(set(), words())
        for n, status in enumerate(("failed", "rolled_back", "failed")):
            (state / "attempts" / f"2027010{n}T000000Z.json").write_text(json.dumps(
                {"mode": "apply", "status": status, "started_at": f"2027-01-0{n + 1}T00:00:00Z"}))
        self.assertTrue(su.halt_reason(state))
        self.assertEqual({"halted"}, words())
        (state / "halt-cleared.json").write_text(json.dumps({"at": "2027-01-02T12:00:00Z"}))
        self.assertIsNone(su.halt_reason(state))
        self.assertEqual(set(), words())
        (state / "scheduled-pause.json").write_text(json.dumps({"at": "x", "reason": "held"}))
        self.assertEqual({"paused"}, words())

    def test_a_peer_mid_update_still_holds_the_turn_even_paused(self) -> None:
        # Control: a live update marker wins over the drain's age and the pause.
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.sys.peer_markers["m5"] = {"host_id": "m5", "target": T_NEW, "ts": NOW - 1800}
        self.sys.peer_pool_since["m5"] = NOW - 2 * 3600
        self.sys.peer_paused.add("m5")
        self.assertDeferred(self.apply())
        self.assertEqual(self.sys.mutations(), [])
        self.assertIn("peer m5 is self-updating", su.waiting_ticket(self.cfg)["reason"])

    def test_the_capacity_floor_still_gates_a_turn_past_an_operator_drain(self) -> None:
        # Control: m5 no longer holds the turn, but this host is the last
        # server of a required label, so it still may not drain.
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.sys.peer_pool_since["m5"] = NOW - su.PEER_DRAIN_GRACE_SECONDS - 60
        self.sys.floor = {"allowed": False, "reason": "last_serving_host", "findings": [
            {"label": "pulp-build-pr-head", "verdict": "last_serving_host"}]}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])
        receipts = sorted((self.cfg.state_dir / "attempts").glob("*.json"))
        receipt = json.loads(receipts[-1].read_text())
        self.assertIn("last server of pulp-build-pr-head", json.dumps(receipt))
        self.assertIn("counted as serving nothing by the capacity floor: m5", json.dumps(receipt))

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

    def gate_queue(self, *, drop: tuple = (), fail: bool = False) -> list[str]:
        """Serve the gate-demand read from the answers captured from
        Generous-Corp/pulp at 2026-10-13 16:48 UTC
        (scripts/fixtures/gate_job_demand/), with every time moved so that
        capture time is the test clock. `drop` removes runs from both run
        listings; `fail` makes every read fail. Returns the paths read."""
        fixtures = Path(__file__).resolve().parent / "fixtures" / "gate_job_demand"
        fleet = self.cfg.checkout / "fleet"
        fleet.mkdir(parents=True, exist_ok=True)
        shutil.copy(fixtures / "advertised-labels.json", fleet / "advertised-labels.json")
        shift = NOW - su.gate_job_demand.epoch("2026-10-13T16:48:10Z")

        def moved(value):
            if isinstance(value, dict):
                return {key: (su._iso(su.gate_job_demand.epoch(item) + shift)
                              if key.endswith("_at") and isinstance(item, str) and item
                              else moved(item)) for key, item in value.items()}
            if isinstance(value, list):
                return [moved(item) for item in value]
            return value

        paths: list[str] = []

        def fetch(path: str) -> dict:
            paths.append(path)
            if fail:
                raise RuntimeError("gh: Server Error (HTTP 502)")
            if "/actions/runs?status=" in path:
                if not path.startswith("repos/Generous-Corp/pulp/"):
                    return {"total_count": 0, "workflow_runs": []}
                status = path.split("status=", 1)[1].split("&", 1)[0]
                answer = json.loads((fixtures / f"pulp-runs-{status}.json").read_text())
                answer["workflow_runs"] = [run for run in answer["workflow_runs"]
                                           if run["id"] not in drop]
                answer["total_count"] = len(answer["workflow_runs"])
                return moved(answer)
            run = path.split("/actions/runs/", 1)[1].split("/", 1)[0]
            return moved(json.loads((fixtures / f"pulp-jobs-{run}.json").read_text()))

        original = su._demand_fetch
        su._demand_fetch = lambda: fetch
        self.addCleanup(setattr, su, "_demand_fetch", original)
        return paths

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
        self.gate_queue()
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
        self.gate_queue()
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual([kind for kind, _ in self.issues], ["open"])
        self.assertIn("self-update starved", self.issues[0][1])
        self.assertEqual(su.waiting_ticket(self.cfg)["issue"], "77")
        self.sys.peers["m5"] = {"state": "on", "participating": True}
        self.assertUpdated(self.apply())
        self.assertEqual(self.issues[-1], ("close", "77"))

    def test_with_gate_demand_starvation_trips_an_hour_past_the_dark_peer_bound(self) -> None:
        # Real apply entry point. A queued macOS job in a running Build and
        # Test run is demand; the bound is one hour past the 3 h exclusion.
        self.assertEqual(su.STARVED_AFTER_SECONDS, su.PEER_UNREACHABLE_EXCLUDE_SECONDS + 3600)
        self.ticket(since=NOW - su.STARVED_AFTER_SECONDS - 60)
        self.gate_queue()
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        events = (self.cfg.state_dir / "events.jsonl").read_text()
        self.assertIn("self_update_starved", events)
        self.assertIn("115420876725", events)
        self.assertEqual([kind for kind, _ in self.issues], ["open"])

    def test_a_dark_peer_does_not_trip_starvation_before_it_is_excluded(self) -> None:
        # Past the old one-hour bound and past the 3 h exclusion, but inside
        # the hour after it: quiet, with demand queued.
        self.ticket(since=NOW - su.PEER_UNREACHABLE_EXCLUDE_SECONDS - 1800)
        self.gate_queue()
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertDeferred(self.apply())
        self.assertEqual(self.issues, [])

    def test_the_zombie_run_alone_is_not_demand(self) -> None:
        # The 2026-08-19 merge_group run is still queued, with no jobs. With
        # only it (and runs whose macOS jobs are running), the bound stays 6 h.
        paths = self.gate_queue(drop=(38433042640,))
        self.ticket(since=NOW - su.STARVED_AFTER_SECONDS - 60)
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertDeferred(self.apply())
        self.assertEqual(self.issues, [])
        self.assertFalse(any("/runs/32218602754/" in path for path in paths), paths)
        self.assertEqual(su.waiting_ticket(self.cfg)["demand"], [])

    def test_a_failed_demand_read_falls_back_to_six_hours_and_says_so(self) -> None:
        self.gate_queue(fail=True)
        self.ticket(since=NOW - su.STARVED_AFTER_SECONDS - 60)
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertDeferred(self.apply())
        self.assertIn("gate demand unread", out.getvalue())
        self.assertIn("HTTP 502", su.waiting_ticket(self.cfg)["demand_error"])
        self.assertEqual(self.issues, [])
        # Past the fallback it still trips, and the event carries the failure.
        self.ticket(since=NOW - su.STARVED_FALLBACK_SECONDS - 60)
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        events = (self.cfg.state_dir / "events.jsonl").read_text()
        self.assertIn("HTTP 502", events)
        # The issue says which bound applied and why, not "gate jobs queued".
        self.assertIn("The bound without demand is 6 h", self.bodies[-1])
        self.assertIn("(the demand read failed)", self.bodies[-1])
        self.assertNotIn("while gate jobs it serves were queued", self.bodies[-1])

    def test_the_starvation_issue_says_what_why_how_to_fix_and_verify(self) -> None:
        self.ticket(since=NOW - su.STARVED_AFTER_SECONDS - 60)
        self.gate_queue()
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        body = self.bodies[0]
        for needle in ("**What happened.** m1 has had no tartci self-update turn",
                       "Queued gate job 115420876725 (Generous-Corp/pulp Build and Test: macos)",
                       "**Likely cause and fix.**", "is not serving normally",
                       "`tartci pool status`", "Safe to auto-apply: no",
                       "**Verify.**", "`tartci fleet-macos self-update --plan`"):
            self.assertIn(needle, body)
        self.assertIn("while gate jobs it serves were queued", body)
        marker = su.tartci_incident.parse(body)
        self.assertEqual(marker["host"], "m1")
        self.assertEqual(marker["class"], "self_update_starved")
        self.assertEqual(marker["symptom_at"], su._iso(NOW - su.STARVED_AFTER_SECONDS - 60))
        self.assertEqual(marker["detected_at"], su._iso(NOW))
        self.assertIsNone(marker["fixed_at"])

    def test_the_turn_records_fixed_at_on_the_marker_then_closes(self) -> None:
        self.ticket(since=NOW - su.STARVED_AFTER_SECONDS - 60)
        self.gate_queue()
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.sys.peers["m5"] = {"state": "on", "participating": True}
        self.assertUpdated(self.apply())
        self.assertEqual([kind for kind, _ in self.issues], ["open", "patch", "close"])
        patched = self.bodies[-1]
        self.assertEqual(patched.count("tartci-incident"), 1)
        fixed = su.tartci_incident.parse(patched)["fixed_at"]
        self.assertIsNotNone(fixed)
        self.assertGreaterEqual(su.gate_job_demand.epoch(fixed), NOW)

    def test_a_failed_patch_keeps_the_fix_time_and_retries_before_closing(self) -> None:
        # A transient API error on the PATCH must not close the issue with a
        # marker still saying fixed_at null: nothing would ever correct it.
        import host_off
        self.ticket(since=NOW - su.STARVED_AFTER_SECONDS - 60)
        self.gate_queue()
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.sys.peers["m5"] = {"state": "on", "participating": True}
        failing = lambda n, b: self.issues.append(("patch-failed", n)) or (1, "HTTP 502")
        with mock.patch.object(host_off, "_patch_issue_body", failing):
            self.assertUpdated(self.apply())
        self.assertEqual([kind for kind, _ in self.issues], ["open", "patch-failed"])
        pending = json.loads((self.cfg.state_dir / su.PENDING_FIX_NAME).read_text())["fixes"]
        self.assertEqual([fix["issue"] for fix in pending], ["77"])
        fixed_at = pending[0]["fixed_at"]
        self.assertIn("HTTP 502", pending[0]["error"])
        # The next run PATCHes the recorded fix time, then closes.
        self.apply()
        self.assertEqual([kind for kind, _ in self.issues], ["open", "patch-failed", "patch", "close"])
        self.assertEqual(su.tartci_incident.parse(self.bodies[-1])["fixed_at"], fixed_at)
        self.assertFalse((self.cfg.state_dir / su.PENDING_FIX_NAME).exists())

    def test_a_failed_close_is_retried_without_patching_again(self) -> None:
        import host_off
        self.ticket(since=NOW - su.STARVED_AFTER_SECONDS - 60)
        self.gate_queue()
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.sys.peers["m5"] = {"state": "on", "participating": True}
        failing = lambda n: self.issues.append(("close-failed", n)) or (1, "HTTP 502")
        with mock.patch.object(host_off, "_close_issue", failing):
            self.assertUpdated(self.apply())
        self.assertTrue((self.cfg.state_dir / su.PENDING_FIX_NAME).exists())
        self.assertTrue(su.finish_pending_fixes(self.cfg))
        self.assertEqual([kind for kind, _ in self.issues], ["open", "patch", "close-failed", "close"])

    def test_a_deferred_run_still_retries_a_pending_fix(self) -> None:
        # A run that defers never clears a ticket, so the retry has to happen
        # at entry: the fix recorded by an earlier run still lands.
        body = su.tartci_incident.fixed(
            "starved\n\n" + su.tartci_incident.marker("m1", "self_update_starved",
                                                        su._iso(NOW - 9000), su._iso(NOW - 3600)),
            su._iso(NOW - 600))
        su._write_json(self.cfg.state_dir / su.PENDING_FIX_NAME, {"fixes": [
            {"issue": "41", "issue_body": body, "fixed_at": su._iso(NOW - 600), "patched": False}]})
        self.ticket(since=NOW - 1800)
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertDeferred(self.apply())
        self.assertEqual(self.issues, [("patch", "41"), ("close", "41")])
        self.assertEqual(self.bodies[-1], body)
        self.assertFalse((self.cfg.state_dir / su.PENDING_FIX_NAME).exists())

    def pending_fix(self, issue: str = "41") -> None:
        body = su.tartci_incident.fixed(
            "starved\n\n" + su.tartci_incident.marker("m1", "self_update_starved",
                                                        su._iso(NOW - 9000), su._iso(NOW - 3600)),
            su._iso(NOW - 600))
        su._write_json(self.cfg.state_dir / su.PENDING_FIX_NAME, {"fixes": [
            {"issue": issue, "issue_body": body, "fixed_at": su._iso(NOW - 600), "patched": False}]})

    def test_a_deleted_issue_is_dropped_and_logged_once_not_retried_forever(self) -> None:
        import host_off
        answers = {
            "patch 404": ("_patch_issue_body", lambda n, b: (1, "gh: Not Found (HTTP 404)")),
            "close 410": ("_close_issue",
                          lambda n: (1, '{"message":"This issue was deleted","status":"410"}')),
        }
        for name, (helper, answer) in answers.items():
            with self.subTest(name=name):
                self.pending_fix()
                events = self.cfg.state_dir / "events.jsonl"
                events.unlink(missing_ok=True)
                with mock.patch.object(host_off, helper, answer), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertTrue(su.finish_pending_fixes(self.cfg))
                self.assertFalse((self.cfg.state_dir / su.PENDING_FIX_NAME).exists())
                self.assertIn("#41 is gone", out.getvalue())
                self.assertEqual(events.read_text().count("self_update_issue_gone"), 1)
                # Nothing is left to retry: the next pass makes no calls.
                before = len(self.issues)
                self.assertTrue(su.finish_pending_fixes(self.cfg))
                self.assertEqual(len(self.issues), before)
                self.assertEqual(events.read_text().count("self_update_issue_gone"), 1)

    def test_a_transient_error_is_still_retried(self) -> None:
        # Control for the terminal case, same instrument: a 502 stays pending.
        import host_off
        self.pending_fix()
        with mock.patch.object(host_off, "_patch_issue_body", lambda n, b: (1, "gh: Server Error (HTTP 502)")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(su.finish_pending_fixes(self.cfg))
        self.assertTrue((self.cfg.state_dir / su.PENDING_FIX_NAME).exists())
        self.assertFalse((self.cfg.state_dir / "events.jsonl").exists())

    def fresh_demand(self, observed: float) -> None:
        su._write_json(self.cfg.state_dir / "gate-queue.json", {
            "observed_at": su._iso(observed), "host_id": "m1", "errors": [],
            "jobs": [{"id": 1, "hosts": ["m1"], "repo": "Generous-Corp/pulp",
                      "workflow": "Build and Test", "name": "macos",
                      "created_at": su._iso(NOW - 600)}]})

    def test_a_stale_demand_snapshot_is_no_demand(self) -> None:
        self.fresh_demand(NOW - su.GATE_DEMAND_FRESH_SECONDS - 60)
        jobs, errors = su.gate_demand(self.cfg, "m1", NOW)
        self.assertEqual(jobs, [])
        self.assertTrue(any("stale" in error for error in errors), errors)
        # Control, same instrument: the same snapshot, fresh, is demand.
        self.fresh_demand(NOW - 60)
        self.assertEqual([job["id"] for job in su.gate_demand(self.cfg, "m1", NOW)[0]], [1])

    def test_defer_on_a_stale_snapshot_keeps_the_six_hour_bound(self) -> None:
        # A refresh that wrote nothing leaves an old snapshot behind; it is
        # not demand, so 4 h in the queue is still only a deferral.
        self.fresh_demand(NOW - su.GATE_DEMAND_FRESH_SECONDS - 60)
        self.ticket(since=NOW - su.STARVED_AFTER_SECONDS - 60)
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        with mock.patch.object(su, "refresh_gate_demand", lambda *args: {}):
            self.assertDeferred(self.apply())
            self.assertEqual(self.issues, [])
            # Control: a fresh snapshot of the same job trips the 4 h bound.
            self.fresh_demand(NOW - 60)
            self.assertEqual(self.apply(), su.EXIT_REFUSED)

    def test_waiting_line_uses_the_demand_bound(self) -> None:
        # 4.5 h in the queue: starved when gate jobs this host serves are
        # queued, still only deferred without them.
        self.ticket(since=NOW - 4.5 * 3600)
        self.fresh_demand(NOW - 60)
        self.assertIn("STARVED", su.waiting_line(self.cfg.state_dir, NOW))
        (self.cfg.state_dir / "gate-queue.json").unlink()
        self.assertIn("deferred", su.waiting_line(self.cfg.state_dir, NOW))

    def test_a_short_deferral_is_quiet(self) -> None:
        # Control for the bound: a deferral just under it, with demand queued,
        # is not a problem.
        self.gate_queue()
        self.ticket(since=NOW - su.STARVED_AFTER_SECONDS + 60)
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertDeferred(self.apply())
        self.assertFalse((self.cfg.state_dir / "events.jsonl").exists()
                         and "self_update_starved" in (self.cfg.state_dir / "events.jsonl").read_text())
        self.assertIsNone(su.summary(self.home)["problem"])
        self.assertEqual(self.issues, [])


class RefusedTurnTests(Base):
    sealed = True

    def test_a_pre_drain_refusal_releases_an_old_queue_ticket(self) -> None:
        su._write_json(self.cfg.state_dir / "waiting.json", {
            "host_id": "m1", "target": T_OLD, "since": NOW - 3 * 3600,
            "ts": NOW - 60, "reason": "previous refusal",
        })
        self.sys.codesign_verify_rc = 1
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertIsNone(su.waiting_ticket(self.cfg))

    def test_capacity_floor_refusal_keeps_the_ticket(self) -> None:
        self.ticket = {"host_id": "m1", "target": T_OLD, "since": NOW - 3600,
                       "ts": NOW - 60, "reason": "capacity"}
        su._write_json(self.cfg.state_dir / "waiting.json", self.ticket)
        self.sys.floor = {"allowed": False, "reason": "capacity_unknown", "findings": []}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(su.waiting_ticket(self.cfg)["since"], self.ticket["since"])

    def test_recent_attempt_refusal_keeps_the_ticket(self) -> None:
        ticket = {"host_id": "m1", "target": T_OLD, "since": NOW - 3600,
                  "ts": NOW - 60, "reason": "recent attempt"}
        su._write_json(self.cfg.state_dir / "waiting.json", ticket)
        attempts = self.cfg.state_dir / "attempts"
        su._write_json(attempts / "recent.json", {
            "mode": "apply", "target": T_OLD, "status": "failed",
            "started_at": su._iso(NOW - 60),
        })
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(su.waiting_ticket(self.cfg)["since"], ticket["since"])


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


FORGE = ("com.danielraffel.tartci.tart-runner-macos-fleet.m1.forge-gate: busy, would be KILLED "
         "(supervisor pid 87749 owns tart run pid 11310)")


def plan_text(*rows: str) -> str:
    """`pool off --plan` output as the real command prints it (m1, 2026-10-16)."""
    return "\n".join([
        "pool off --plan on m1: nothing will be changed",
        "  state: draining -> off; participation: 0 -> 0",
        "  capacity floor: ok (pool off keeps every required label served)",
        "  mid-job lanes:", *[f"    {row}" for row in rows],
        "verdict: pool off would REFUSE (an owned lane is mid-job or unknown; use pool drain, "
        "or --now)"])


class DrainWaitVisibilityTests(Base):
    def steps(self) -> list[dict]:
        return json.loads(Path(self.last()["receipt"]).read_text())["steps"]

    def test_the_wait_names_the_lane_once_per_change(self) -> None:
        other = "com.danielraffel.tartci.tart-runner-macos-fleet.m1.pulp-gate: busy, x"
        self.sys.offplan = [12, 12, 12, 0]
        self.sys.offplan_text = [plan_text(FORGE), plan_text(FORGE), plan_text(FORGE, other),
                                 "plan"]
        self.assertUpdated(self.apply())
        waits = [s["detail"] for s in self.steps() if s["step"] == "wait-idle"]
        self.assertEqual(waits, [f"waiting on {FORGE}", f"waiting on {FORGE}; {other}",
                                 "no owned lane mid-job"])

    def test_a_long_unchanged_wait_still_writes_a_step_every_five_minutes(self) -> None:
        # m1, 2026-10-22: steps at 08:00 then 08:55, nothing between.
        self.sys.offplan = [12] * 12 + [0]
        self.sys.offplan_text = [plan_text(FORGE)] * 12 + ["plan"]
        self.assertUpdated(self.apply())
        waits = [s["detail"] for s in self.steps() if s["step"] == "wait-idle"]
        self.assertEqual(waits[0], f"waiting on {FORGE}")
        self.assertEqual(waits[1], f"still waiting on {FORGE}, 5 min")
        self.assertEqual(waits[-1], "no owned lane mid-job")
        self.assertEqual(len(waits), 3)

    def test_a_drain_timeout_opens_one_issue_naming_the_lane_and_a_clean_drain_closes_it(
            self) -> None:
        self.sys.offplan = [12]
        self.sys.offplan_text = plan_text(FORGE)
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertIn("m1.forge-gate", self.last()["error"])
        opened = [t for kind, t in self.issues if kind == "open"]
        self.assertEqual(len(opened), 1)
        self.assertIn("self-update drain timed out after 10 min waiting on a lane", opened[0])
        self.assertIn("supervisor pid 87749 owns tart run pid 11310", self.bodies[0])
        self.assertIn("a manual VM never blocks the drain", self.bodies[0])
        import tartci_incident  # noqa: PLC0415
        self.assertEqual(tartci_incident.parse(self.bodies[0])["class"],
                         "self_update_drain_timeout")
        self.assertNotIn(("close", "77"), self.issues)
        # The next attempt drains cleanly: the issue closes.
        self.sys.offplan = [0]
        self.sys.offplan_text = "plan"
        self.sys.clock += 3600
        self.apply()
        self.assertIn(("close", "77"), self.issues)

    def test_the_issue_closes_when_a_later_drain_completes(self) -> None:
        self.sys.offplan = [12]
        self.sys.offplan_text = plan_text(FORGE)
        su.drain_timeout_alert(self.cfg, "m1", [FORGE], 600, NOW)
        self.assertEqual([k for k, _ in self.issues], ["open"])
        su.drain_timeout_alert(self.cfg, "m1", [FORGE], 600, NOW + 3600)
        self.assertEqual([k for k, _ in self.issues], ["open"])   # same episode
        su.drain_timeout_alert(self.cfg, "m1", None, 30, NOW + 7200)
        self.assertEqual(self.issues[-1], ("close", "77"))

    def test_mid_job_rows_are_read_from_the_plan(self) -> None:
        self.assertEqual(su.mid_job_lanes(plan_text(FORGE)), [FORGE])
        self.assertEqual(su.mid_job_lanes("pool off --plan: nothing\nverdict: ok"), [])


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

    def test_source_move_has_a_separate_rate_limit_from_public_attempt(self) -> None:
        """A source move must not wait six hours after a public install."""
        attempts = self.cfg.state_dir / "attempts"
        su._write_json(attempts / "public.json", {
            "mode": "apply", "target": INSTALLED, "status": "succeeded",
            "started_at": su._iso(NOW - 60), "fleet_source": None,
        })
        private = {"repository": "ORG/private-fleet-authority", "commit": "c" * 40}
        self.assertIsNone(su.recent_attempt(self.cfg, INSTALLED, NOW, private))
        su._write_json(attempts / "private.json", {
            "mode": "apply", "target": INSTALLED, "status": "succeeded",
            "started_at": su._iso(NOW - 60), "fleet_source": private,
        })
        self.assertIsNotNone(su.recent_attempt(self.cfg, INSTALLED, NOW, private))
        newer_private = {"repository": private["repository"], "commit": "d" * 40}
        self.assertIsNotNone(su.recent_attempt(self.cfg, INSTALLED, NOW, newer_private))

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

    def test_build_launcher_uses_rendered_pin_from_environment(self) -> None:
        pinned = self.home / "pinned-python"
        pinned.symlink_to(sys.executable)
        with mock.patch.dict(os.environ, {"TARTCI_RENDER_PYTHON": str(pinned)}):
            self.assertEqual(self.apply(), su.EXIT_OK, self.last())
        build_index = next(i for i, (args, _) in enumerate(self.sys.calls)
                           if args[:2] == ["bash", "scripts/build_macos_launcher.sh"])
        build_env = self.sys.env_calls[build_index]
        self.assertEqual(build_env["TARTCI_RENDER_PYTHON"], os.path.realpath(pinned))
        build = self.sys.calls[build_index][0]
        self.assertEqual(build[build.index("--python") + 1], os.path.realpath(pinned))

    def test_install_failure_restores_the_pin(self) -> None:
        self.sys.install_rcs = [1]
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.pin().read_text(), "0" * 64 + "\n")
        self.assertEqual(self.sys.pool_state, "on")

    def test_installer_preflight_failure_refuses_before_drain(self) -> None:
        self.sys.preflight_rc = 1
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.pool_state, "on")
        self.assertEqual(self.sys.mutations(), [])
        receipt_path = max((self.cfg.state_dir / "attempts").glob("*.json"), key=lambda p: p.stat().st_mtime)
        receipt = json.loads(receipt_path.read_text())
        self.assertIn("installer preflight refused before drain", receipt["error"])
        self.assertFalse(any("--apply" in args for args, _ in self.sys.calls
                             if args[:2] == ["./tartci", "fleet-macos"]))

    def test_pin_dependent_preflight_refusal_restores_after_drain(self) -> None:
        self.sys.dry_run_rc = 1
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.pool_state, "on")
        self.assertEqual(self.pin().read_text(), "0" * 64 + "\n")
        receipt_path = max((self.cfg.state_dir / "attempts").glob("*.json"), key=lambda p: p.stat().st_mtime)
        receipt = json.loads(receipt_path.read_text())
        names = [step["step"] for step in receipt["steps"]]
        self.assertIn("drain", names)
        self.assertIn("pool-on", names)
        self.assertLess(names.index("drain"), names.index("pool-on"))
        self.assertIn("install-dry-run", names)
        self.assertIn("refused after drain", receipt["error"])
        self.assertNotEqual(receipt["status"], "failed")

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

    def test_private_source_environment_can_be_rendered_into_agent(self) -> None:
        rendered = subprocess.run(
            [sys.executable, str(ROOT / "scripts/render_launchd_template.py"),
             str(ROOT / "launchd/com.danielraffel.tartci.self-update.plist.template"),
             "--set", "HOME=/Users/x",
             "--environment", "TARTCI_FLEET_SOURCE_ROOT=/private/fleet",
             "--environment", "TARTCI_FLEET_SOURCE_COMMIT=" + "a" * 40,
             "--environment", "TARTCI_FLEET_SOURCE_REPOSITORY=ORG/private-fleet-authority",
             "--environment", "TARTCI_ADVERTISED_LABELS=/private/fleet/fleet/advertised-labels.json",
             "--environment", "TARTCI_JOB_CLAIM_SUPPLY=/private/fleet/fleet/advertised-labels.json"],
            capture_output=True, check=True)
        environment = plistlib.loads(rendered.stdout)["EnvironmentVariables"]
        self.assertEqual(environment["TARTCI_FLEET_SOURCE_ROOT"], "/private/fleet")
        self.assertEqual(environment["TARTCI_FLEET_SOURCE_COMMIT"], "a" * 40)
        self.assertEqual(environment["TARTCI_FLEET_SOURCE_REPOSITORY"], "ORG/private-fleet-authority")

    def test_watchdog_never_interrupts_the_agent(self) -> None:
        import tartci_launchd_watchdog as wd
        self.assertIn("com.danielraffel.tartci.self-update", wd.UNINTERRUPTIBLE_AGENTS)
        self.assertIn(3, wd.APPLICATION_EXIT_CODES["com.danielraffel.tartci.self-update"])


class ProgressLineTests(Base):
    """--status and pool status say which step a running update is in."""

    def running(self, steps: list[tuple[str, str, str]], pid: int | None = None) -> None:
        su._write_json(self.cfg.state_dir / "active.json",
                       {"host_id": "m1", "pid": pid or os.getpid(), "target": T_NEW})
        su._write_json(self.cfg.state_dir / "attempts" / "20270101T000000Z-x.json",
                       {"mode": "apply", "status": "running", "target": T_NEW,
                        "started_at": "2027-01-01T00:00:00Z",
                        "steps": [{"at": at, "step": name, "ok": True, "detail": detail}
                                  for at, name, detail in steps]})

    def test_a_drain_waiting_on_a_job_says_so_and_what_is_left(self) -> None:
        self.running([("2027-01-01T00:00:10Z", "snapshot", "snap"),
                      ("2027-01-01T00:00:20Z", "drain", "keeps every label served"),
                      ("2027-01-01T00:01:00Z", "wait-idle", "waiting on lane: busy"),
                      ("2027-01-01T00:30:00Z", "wait-idle", "waiting on lane: busy")])
        now = su._ts("2027-01-01T01:01:00Z")
        self.assertEqual(
            f"self-update: IN PROGRESS to {T_NEW[:12]} (started 2027-01-01T00:00:00Z, "
            "61 min): wait-idle for 60 min: waiting on lane: busy; "
            "then off, install, pool-on, verify",
            su.progress_line(self.cfg.state_dir, now))

    def test_the_line_reaches_status(self) -> None:
        self.running([("2027-01-01T00:00:20Z", "install", "")])
        self.assertTrue(any(line.startswith("self-update: IN PROGRESS")
                            for line in su.status_lines(self.cfg.state_dir)))

    def test_a_dead_update_is_reported_stale(self) -> None:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.running([("2027-01-01T00:00:20Z", "drain", "")], pid=dead.pid)
        self.assertIn("STALE active.json", su.progress_line(self.cfg.state_dir))

    def test_no_update_running_adds_nothing(self) -> None:
        self.assertIsNone(su.progress_line(self.cfg.state_dir))
        self.assertFalse(any("IN PROGRESS" in line
                             for line in su.status_lines(self.cfg.state_dir)))
class HandoffSettlesTests(unittest.TestCase):
    def test_a_handoff_in_progress_settles_instead_of_failing_verify(self) -> None:
        # The previous supervisor finishing its job before it hands the lane
        # over must be re-read, not rolled back.
        self.assertIn("orphaned_supervisor", su.SETTLING_PROBLEM_CODES)
        self.assertIn("supervisor_handoff_pending", su.SETTLING_PROBLEM_CODES)


class VerifySettleTests(Base):
    def test_heartbeat_timeout_rollback_has_distinct_outcome(self) -> None:
        self.assertEqual(
            su._rollback_terminal_message(
                "verification of aaaaaaaaaaaa: fleet not ready: "
                "[{'code': 'heartbeat_missing', 'label': 'lane'}]",
                "b" * 40,
            ),
            "rollback installed; verification timed out; rolled back to bbbbbbbbbbbb and verified",
        )

    def test_rollback_wording_requires_rollback_steps_to_succeed(self) -> None:
        self.assertNotIn(
            "rollback installed and pool on",
            su._rollback_terminal_message(
                "verification of aaaaaaaaaaaa: fleet not ready: heartbeat_missing",
                "b" * 40),
        )

    def test_heartbeat_missing_with_crashed_launchd_lane_includes_last_log_line(self) -> None:
        log = self.home / "lane.log"
        log.write_text("booting\nlaunchd: runner crashed\n")
        self.sys.launchd_print = su.Result(
            0,
            f"last exit code = 75\nstderr path = {log}\n",
        )
        details = su._heartbeat_failure_diagnostics(self.sys, ["lane"])
        self.assertEqual(details, ["lane lane last exit 75; last log line: launchd: runner crashed"])

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
    def test_census_env_preserves_private_source_binding(self) -> None:
        values = {
            "TARTCI_FLEET_SOURCE_ROOT": "/private/fleet",
            "TARTCI_FLEET_SOURCE_COMMIT": "a" * 40,
            "TARTCI_FLEET_SOURCE_REPOSITORY": "ORG/private-fleet-authority",
            "TARTCI_ADVERTISED_LABELS": "/private/fleet/fleet/advertised-labels.json",
            "TARTCI_JOB_CLAIM_SUPPLY": "/private/fleet/fleet/advertised-labels.json",
        }
        with mock.patch.dict(os.environ, values, clear=False):
            env = su.census_env()
        for name, value in values.items():
            self.assertEqual(env[name], value)

    def test_tartci_calls_resolve_this_interpreter_even_from_a_minimal_path(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            os.environ.pop("TARTCI_PYTHON", None)
            env = su.census_env()
        self.assertEqual(env["TARTCI_PYTHON"], sys.executable)
        self.assertEqual(env["HOME"], os.environ.get("HOME") or os.path.expanduser("~"))
        proc = subprocess.run(["/bin/sh", "-c", "python3 -c 'import sys; print(sys.executable)'"],
                              env={**os.environ, "PATH": "/usr/bin:/bin", **env},
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(os.path.realpath(proc.stdout.strip()), os.path.realpath(sys.executable))

    def test_census_env_preserves_home_for_production_shaped_ghapp(self) -> None:
        # The real ghapp wrapper uses HOME for its cache and refuses to run
        # without it. Exercise the exact reduced census environment rather
        # than a stub that accepts any environment.
        with tempfile.TemporaryDirectory() as tmp:
            ghapp = Path(tmp) / "ghapp"
            ghapp.write_text(
                "#!/bin/sh\n"
                "[ -n \"${HOME:-}\" ] || { echo 'ghapp requires HOME' >&2; exit 78; }\n"
                "printf '%s\\n' authenticated\n"
            )
            ghapp.chmod(0o755)
            env = su.census_env("danielraffel/tartci")
            env["PATH"] = f"{tmp}:{env['PATH']}"
            ok_run = subprocess.run([str(ghapp), "api", "repos/danielraffel/tartci"],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(ok_run.returncode, 0, ok_run.stderr)
            self.assertEqual(ok_run.stdout.strip(), "authenticated")

            mutant_env = env.copy()
            mutant_env.pop("HOME", None)
            denied = subprocess.run([str(ghapp), "api", "repos/danielraffel/tartci"],
                                    env=mutant_env, capture_output=True, text=True)
            self.assertEqual(denied.returncode, 78)
            self.assertIn("requires HOME", denied.stderr)


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

    def test_recovery_pool_on_reuses_receipted_serving_set(self) -> None:
        self.sys.install_rcs = [1]
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        pool_on = [a for a, _ in self.sys.calls if "pool" in a and "on" in a]
        self.assertTrue(any("--restore-receipt" in a for a in pool_on), pool_on)

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


class StoreWriteChildTests(unittest.TestCase):
    """System.store_write runs the write in a child it can abandon: a write
    stuck in a wedged volume must never hold the self-update process."""

    def test_the_write_is_made_by_a_child_not_this_process(self) -> None:
        with tempfile.TemporaryDirectory() as store:
            child = ("import json, os, sys, time; t = time.monotonic(); "
                     "open(os.path.join(sys.argv[1], 'pid'), 'w').write(str(os.getpid())); "
                     "print(json.dumps({'ms': 1.0}))")
            with mock.patch.object(su, "STORE_WRITE_CHILD", child):
                self.assertEqual({"ms": 1.0}, su.System().store_write(store))
            self.assertNotEqual(str(os.getpid()), Path(store, "pid").read_text())

    def test_a_write_that_never_returns_is_abandoned_on_time(self) -> None:
        children = []
        real_popen = su.subprocess.Popen

        def popen(*args, **kwargs):
            children.append(real_popen(*args, **kwargs))
            return children[-1]
        with tempfile.TemporaryDirectory() as store:
            with mock.patch.object(su, "STORE_WRITE_CHILD", "import time; time.sleep(60)"), \
                    mock.patch.object(su.subprocess, "Popen", popen):
                started = time.monotonic()
                result = su.System().store_write(store, timeout=0.5)
            self.assertEqual({"timed_out": True, "timeout": 0.5}, result)
            self.assertLess(time.monotonic() - started, 5)
        # The child's whole process group is gone: nothing left sleeping.
        with self.assertRaises(ProcessLookupError):
            os.killpg(children[0].pid, 0)

    def test_a_child_that_cannot_start_is_an_error_not_an_exception(self) -> None:
        with tempfile.TemporaryDirectory() as store:
            with mock.patch.object(su, "child_python", side_effect=RuntimeError("no python")):
                self.assertIn("could not start", su.System().store_write(store)["error"])
            with mock.patch.object(su.subprocess, "Popen", side_effect=OSError("no fork")):
                self.assertIn("could not start", su.System().store_write(store)["error"])

    def test_the_child_runs_isolated(self) -> None:
        argvs = []
        real_popen = su.subprocess.Popen

        def popen(argv, *args, **kwargs):
            argvs.append(argv)
            return real_popen(argv, *args, **kwargs)
        with tempfile.TemporaryDirectory() as store, \
                mock.patch.object(su.subprocess, "Popen", popen):
            su.System().store_write(store)
        self.assertEqual("-I", argvs[0][1])

    def test_a_real_write_reports_milliseconds_and_leaves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as store:
            result = su.System().store_write(store)
            self.assertIn("ms", result)
            self.assertEqual([], os.listdir(store))


class StoreHealthRecordTests(unittest.TestCase):
    """System.store_health reads the storage-health gate's real record."""

    def setUp(self) -> None:
        import storage_health
        self.sh = storage_health
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.store = self.tmp / "store"
        self.store.mkdir()
        patch = mock.patch.object(Path, "home", classmethod(lambda cls: self.home))
        patch.start()
        self.addCleanup(patch.stop)
        self.state = self.sh.default_state(self.store)
        self.assertTrue(str(self.state).startswith(str(self.home)))

    def record(self, status: str = "healthy") -> None:
        self.sh.save_state(self.state, {
            "schema": 1, "status": status, "reason": "directory_metadata_ok",
            "path": str(self.store), "failure_streak": 0, "healthy_streak": 3,
            "checked_at": "2026-10-22T15:04:23Z"})

    def test_a_fresh_record_is_healthy_with_its_check_time(self) -> None:
        self.record()
        self.assertEqual(("healthy", "directory_metadata_ok", "2026-10-22T15:04:23Z"),
                         su.System().store_health(str(self.store)))

    def test_a_quarantine_marker_wins_over_a_healthy_record(self) -> None:
        self.record()
        Path(str(self.state) + ".quarantine").write_text("")
        self.assertEqual("quarantined", su.System().store_health(str(self.store))[0])

    def test_no_record_is_unknown(self) -> None:
        status, reason, _ = su.System().store_health(str(self.store))
        self.assertEqual("unknown", status)
        self.assertIn("no storage-health record", reason)

    def test_a_corrupt_record_is_unknown(self) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text("{not json")
        self.assertEqual("unknown", su.System().store_health(str(self.store))[0])


class LauncherProbeStopTests(Base):
    """After installing a NEW sealed launcher, a failed volume probe stops the
    update with the host OFF instead of rolling back: a rollback replaces the
    launcher again, which can reset its Removable Volumes consent again."""
    sealed = True
    PROBE = ("fleet readiness: launch helper volume probe timed out after 60s (probe wait "
             "elapsed 60.1s; its write to the store did not finish): helper blocked in open()")

    def installs(self) -> list[list[str]]:
        return [a for a, _ in self.sys.calls if "--apply" in a and "install" in a]

    def test_a_failed_probe_after_a_new_launcher_stops_without_rollback(self) -> None:
        self.sys.on_rc = 1
        self.sys.on_text = self.PROBE
        self.assertEqual(su.EXIT_FAILED, self.apply())
        last = self.last()
        self.assertEqual("failed", last["status"])
        self.assertIn("STOPPED and NOT rolled back", last["error"])
        self.assertIn("Grant Removable Volumes to TartCI Launcher", last["error"])
        self.assertEqual(1, len(self.installs()), "no rollback install")
        self.assertFalse(any(step["step"].startswith("rollback")
                             for step in json.loads(Path(last["receipt"]).read_text())["steps"]))
        self.assertEqual(T_OLD, self.sys.running(), "the new generation stays installed")
        self.assertEqual("c" * 64 + "\n", self.pin().read_text(), "pin matches the live bundle")
        self.assertIs(True, json.loads(Path(last["receipt"]).read_text())["host_off"])
        self.assertIs(True, last["host_off"])
        opened = [title for kind, title in self.issues if kind == "open"]
        self.assertIn("[tartci] studio is OFF: grant Removable Volumes to TartCI Launcher, "
                      "then pool on", opened)
        body = next(b for b in self.bodies if "Files and Folders" in b)
        self.assertIn("tartci pool on", body)
        self.assertIn("## Safe to auto-apply\nNo.", body)

    def test_a_slow_volume_stops_without_asking_for_the_grant(self) -> None:
        self.sys.on_rc = 1
        self.sys.on_text = ("fleet readiness: launch helper volume probe timed out after 60s "
                            "(probe wait elapsed 60.1s; its write to the store did not finish): "
                            "volume I/O slow; same-dir write from parent took 4200 ms. "
                            "Not an access denial")
        self.assertEqual(su.EXIT_FAILED, self.apply())
        last = self.last()
        self.assertIn("STOPPED and NOT rolled back", last["error"])
        self.assertIn("The Tart store volume was slow", last["error"])
        self.assertNotIn("Grant Removable Volumes", last["error"])
        self.assertIs(True, last["host_off"])
        self.assertEqual(1, len(self.installs()), "no rollback install")
        opened = [title for kind, title in self.issues if kind == "open"]
        self.assertIn("[tartci] studio is OFF: its Tart store volume was too slow for the "
                      "launcher probe", opened)
        body = next(b for b in self.bodies if "launcher_probe_slow_volume" in b)
        self.assertNotIn("Removable Volumes to TartCI Launcher", body)
        self.assertNotIn("Files and Folders", body)
        self.assertIn("tartci pool on", body)

    def test_a_slow_store_refuses_a_launcher_change_before_drain(self) -> None:
        # m5studio, 2026-10-18: the probe failed after the install and the
        # host stayed OFF for days. Checked first, nothing changes.
        self.sys.store_result = {"ms": 4200.0}
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertEqual([], self.installs())
        self.assertEqual("on", self.sys.pool_state)
        self.assertTrue(self.sys.store_writes, "the store was checked")
        receipt = self.latest_receipt()
        self.assertIn("Tart store volume slow before a launcher change", receipt["error"])
        self.assertIn("4200 ms", receipt["error"])

    def test_a_slow_store_on_apply_releases_the_queue_ticket(self) -> None:
        # HostFault, not a plain refusal: the turn goes to the next host.
        (self.cfg.state_dir).mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "waiting.json").write_text(json.dumps({"since": su.time.time() - 60}))
        self.sys.store_result = {"ms": 4200.0}
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertFalse((self.cfg.state_dir / "waiting.json").exists())

    def test_a_plan_never_writes_to_the_store(self) -> None:
        self.plan()
        self.assertEqual([], self.sys.store_writes)
        steps = [s for s in self.latest_receipt()["steps"] if s["step"] == "store-volume"]
        self.assertTrue(steps and "a plan writes nothing" in steps[-1]["detail"], steps)

    def test_an_unhealthy_store_refuses_without_a_write(self) -> None:
        for status in ("quarantined", "degraded", "unknown"):
            self.sys.store_writes.clear()
            self.sys.store_health_result = (status, "probe_timeout", su._iso(NOW - 60))
            self.assertEqual(su.EXIT_REFUSED, self.apply(), status)
            self.assertEqual([], self.sys.store_writes, status)
            self.assertEqual([], self.sys.mutations(), status)
            self.assertIn(f"storage health for", self.latest_receipt()["error"])
            self.assertIn(status, self.latest_receipt()["error"])

    def record_aged(self, minutes: float) -> None:
        self.sys.store_health_result = ("healthy", "directory_metadata_ok",
                                        su._iso(self.sys.clock - minutes * 60))

    def a_store_write_timed_out_last_attempt(self) -> None:
        self.sys.store_result = [{"timed_out": True, "timeout": 10.0}, {"ms": 4.0}]
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.sys.store_writes.clear()
        self.sys.clock += 1800

    def test_a_stale_healthy_record_on_a_busy_host_lets_the_write_decide(self) -> None:
        # Lanes mid-job do not rewrite the record; that is not a refusal.
        (self.cfg.state_dir).mkdir(parents=True, exist_ok=True)
        self.record_aged(180)
        self.apply()
        self.assertTrue(self.sys.store_writes)
        self.assertNotIn("stale", str(self.latest_receipt().get("error")))
        self.assertNotEqual("refused", self.latest_receipt()["status"])

    def test_a_stale_record_after_a_timed_out_write_refuses_without_a_write(self) -> None:
        self.a_store_write_timed_out_last_attempt()
        self.record_aged(180)
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.store_writes)
        self.assertEqual([], self.sys.mutations())
        error = self.latest_receipt()["error"]
        self.assertIn("is stale: its healthy record is 180 min old", error)
        self.assertIn("the last store write timed out", error)

    def test_a_wedged_store_is_written_once_however_many_attempts_follow(self) -> None:
        # Re-review: the stale refusal ran no write and reset the guard, so a
        # hanging write was retried every other attempt (writes 1,1,2,2,3),
        # each stranding a child in an uninterruptible wait.
        self.sys.store_result = {"timed_out": True, "timeout": 10.0}
        for attempt in range(3):
            self.record_aged(180)
            self.assertEqual(su.EXIT_REFUSED, self.apply(), attempt)
            self.sys.clock += 1800
        self.assertEqual(1, len(self.sys.store_writes))

    def test_a_deferral_between_attempts_does_not_reset_the_guard(self) -> None:
        self.sys.store_result = {"timed_out": True, "timeout": 10.0}
        self.record_aged(180)
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.sys.clock += 1800
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.assertEqual(su.EXIT_DEFERRED, self.apply())
        self.assertTrue(self.latest_receipt().get("deferred"))
        self.sys.peers["m5"] = {"state": "on", "participating": True}
        self.sys.clock += 1800
        self.record_aged(180)
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual(1, len(self.sys.store_writes))
        self.assertIn("the last store write timed out", self.latest_receipt()["error"])

    def test_the_stale_bound_is_ten_minutes(self) -> None:
        self.a_store_write_timed_out_last_attempt()
        self.record_aged(11)
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.store_writes, "11 min is stale")
        self.record_aged(9)
        self.apply()
        self.assertTrue(self.sys.store_writes, "9 min is fresh: the write decides")

    def test_a_store_that_turns_slow_during_the_drain_puts_the_pool_back(self) -> None:
        self.sys.store_result = [{"ms": 4.0}, {"ms": 4200.0}]
        self.apply()
        self.assertEqual(2, len(self.sys.store_writes), "checked before and after the drain")
        self.assertEqual([], self.installs(), "nothing installed")
        self.assertEqual("on", self.sys.pool_state)
        self.assertIn("slow after the drain", self.latest_receipt()["error"])
        self.assertEqual("refused", self.latest_receipt()["status"])

    def test_a_store_write_that_hangs_also_refuses_before_drain(self) -> None:
        self.sys.store_result = {"timed_out": True, "timeout": 10.0}
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertIn("did not finish within 10s", self.latest_receipt()["error"])

    def latest_receipt(self) -> dict:
        receipts = sorted((self.cfg.state_dir / "attempts").glob("*.json"))
        return json.loads(receipts[-1].read_text())

    def test_any_other_pool_on_failure_still_rolls_back(self) -> None:
        # pool on fails every attempt after the install, then serves the rollback.
        self.sys.on_rcs = [1] * su.Run.POOL_ON_ATTEMPTS
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertEqual("rolled_back", self.last()["status"])
        self.assertEqual(INSTALLED, self.sys.running())

    def test_a_completed_update_closes_the_issue(self) -> None:
        su._write_json(self.cfg.state_dir / "launcher-probe-alert.json",
                       {"since": "2026-10-16T13:00:00Z", "evented": True, "issue": "91"})
        self.assertUpdated(self.apply())
        self.assertIn(("close", "91"), self.issues)
        self.assertFalse((self.cfg.state_dir / "launcher-probe-alert.json").exists())

    def pin(self) -> Path:
        return self.home / ".config/tartci/m3-launcher-approved.sha256"


class UnsealedProbeTests(Base):
    def test_without_a_new_launcher_the_probe_text_does_not_stop_a_rollback(self) -> None:
        self.sys.on_rcs = [1] * su.Run.POOL_ON_ATTEMPTS
        self.sys.on_text = LauncherProbeStopTests.PROBE
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertEqual("rolled_back", self.last()["status"])


class LauncherConsentTests(Base):
    """A sealed launcher is not replaced on an external-volume host until an
    operator has acknowledged the change: replacing TartCILauncher.app can
    reset its Removable Volumes consent, and then no lane starts."""
    sealed = True
    consent_gate = True

    def ack(self) -> Path:
        return self.home / ".config/tartci/launcher-change-ack"

    def setUp(self) -> None:
        super().setUp()
        # The owner's override ([launcher] auto_apply = false): every change
        # waits for the ack. LauncherAutoApplyTests covers the default.
        self.cfg.launcher_auto_apply = False
        self.ack().write_text(T_OLD + "\n")

    def latest(self) -> dict:
        attempts = sorted((self.cfg.state_dir / "attempts").glob("*.json"))
        return json.loads(attempts[-1].read_text())

    def test_an_unacknowledged_change_refuses_and_alerts_before_any_drain(self) -> None:
        self.ack().unlink()
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertFalse(any(a[:2] == ["bash", "scripts/build_macos_launcher.sh"]
                             for a, _ in self.sys.calls), "no signing build for a refused run")
        self.assertFalse(any("codesign" in a[0] for a, _ in self.sys.calls))
        self.assertEqual("refused", self.latest()["status"])
        self.assertIn("launcher change needs consent", self.latest()["error"])
        self.assertIn(f"echo {T_OLD} > {self.ack()}", self.latest()["error"])
        self.assertEqual([("open", f"[tartci] studio: grant Removable Volumes to TartCI Launcher "
                                   f"before self-update installs {T_OLD[:12]}")], self.issues)
        body = self.bodies[0]
        self.assertIn(f"echo {T_OLD} > {self.ack()}", body)
        self.assertIn("Files and Folders", body)
        self.assertIn("## Safe to auto-apply\nNo.", body)
        # A refusal does not spend the attempt: once acknowledged it updates.
        self.ack().write_text(T_OLD + "\n")
        self.assertUpdated(self.apply())
        self.assertIn(("close", "77"), self.issues)

    def test_an_acknowledgement_for_another_commit_still_refuses(self) -> None:
        self.ack().write_text(T_NEW + "\n")
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations())

    def test_a_plan_reports_the_refusal_and_opens_nothing(self) -> None:
        self.ack().unlink()
        self.assertEqual(su.EXIT_REFUSED, self.plan())
        self.assertEqual([], self.issues)
        self.assertEqual([], self.sys.mutations())

    def test_no_change_to_the_launcher_needs_no_consent(self) -> None:
        self.ack().unlink()
        self.assertIsNone(su.launcher_change_refusal(self.cfg, INSTALLED, INSTALLED, False))
        self.assertIsNone(su.launcher_change_refusal(self.cfg, T_OLD, INSTALLED, True))
        self.assertIsNotNone(su.launcher_change_refusal(self.cfg, T_OLD, INSTALLED, False))

    def test_an_acknowledged_change_whose_probe_fails_stops_without_rollback(self) -> None:
        # Both guards in one run: the gate lets the acknowledged change in,
        # and the failed volume probe then stops it instead of rolling back.
        self.sys.on_rc = 1
        self.sys.on_text = LauncherProbeStopTests.PROBE
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertIn("STOPPED and NOT rolled back", self.last()["error"])
        installs = [a for a, _ in self.sys.calls if "--apply" in a and "install" in a]
        self.assertEqual(1, len(installs))
        self.assertEqual(T_OLD, self.sys.running())

    def test_a_host_without_a_sealed_launcher_is_not_gated(self) -> None:
        self.ack().unlink()
        with mock.patch.object(su, "launch_helper", return_value=None):
            self.assertIsNone(su.launcher_change_refusal(self.cfg, T_OLD, INSTALLED, False))


# danielraffel/agent-skills .fleet/fleet-liveness.sh alerts on the first
# `tartci pool status` line this matches and posts it as the remedy.
LIVENESS_SELF_UPDATE_FAIL = re.compile(
    r"^self-update.*(HALTED|STARVED|WAS LEFT|LAUNCHER CONSENT LOST|"
    r"LAST ATTEMPT (FAILED|ROLLED BACK))")


class LauncherAutoApplyTests(Base):
    """Plan item 12: a launcher change applies without launcher-change-ack
    when the target is on tartci main and the new launcher keeps the
    installed Team ID; a lost Removable Volumes grant rolls back, holds the
    target and alerts with the remedy."""
    sealed = True
    consent_gate = True

    def ack(self) -> Path:
        return self.home / ".config/tartci/launcher-change-ack"

    def latest(self) -> dict:
        attempts = sorted((self.cfg.state_dir / "attempts").glob("*.json"))
        return json.loads(attempts[-1].read_text())

    def builds(self) -> list[list[str]]:
        return [a for a, _ in self.sys.calls if a[:2] == ["bash", "scripts/build_macos_launcher.sh"]]

    def installs(self) -> list[list[str]]:
        return [a for a, _ in self.sys.calls if "--apply" in a and "install" in a]

    def hold(self) -> dict | None:
        return su._read_json(self.cfg.state_dir / su.LAUNCHER_AUTO_APPLY_NAME)

    def fail_probe_until_rollback(self) -> None:
        self.sys.on_rc = 1
        self.sys.on_text = LauncherProbeStopTests.PROBE

        def recover(argv):
            if "--apply" in argv and "rollback" in " ".join(argv):
                self.sys.on_rc = 0
        self.sys.hook = recover

    def test_on_main_with_the_same_team_it_applies_and_records_the_ack(self) -> None:
        self.assertFalse(self.ack().exists())
        self.assertUpdated(self.apply())
        ack = self.latest()["launcher_ack"]
        self.assertEqual("automatic", ack["mode"])
        self.assertEqual(T_OLD, ack["target"])
        self.assertEqual(T_OLD, ack["key"])
        self.assertEqual(self.sys.live_team, ack["team_id"])
        self.assertEqual(self.sys.live_team, ack["installed_team_id"])
        self.assertIn("on tartci main", ack["reason"])
        self.assertIn("--launch-helper-source", self.installs()[0])
        self.assertFalse(self.ack().exists(), "no ack file is written")
        self.assertEqual([], [i for i in self.issues if i[0] == "open"])
        self.assertIsNone(self.hold())

    def test_an_off_main_target_waits_for_the_ack(self) -> None:
        self.sys.off_main = {T_OLD}
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertEqual([], self.builds())
        self.assertIn("launcher change needs consent", self.latest()["error"])
        self.assertIn(f"echo {T_OLD} > {self.ack()}", self.latest()["error"])
        self.assertIn("not on tartci origin/main", self.latest()["launcher_ack"]["auto_refused"])
        # Control: the same run on main applies.
        self.sys.off_main = set()
        self.assertUpdated(self.apply())

    def test_a_built_launcher_for_another_team_is_refused_before_drain_and_held(self) -> None:
        self.sys.built_team = "ZZZZZZZZZZ"
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations(), "refused before any drain")
        error = self.latest()["error"]
        self.assertIn(f"is for team ZZZZZZZZZZ, the installed one for {self.sys.live_team}", error)
        self.assertIn(f"acknowledge with: echo '{T_OLD}' > {self.ack()}", error)
        self.assertEqual("team_mismatch", self.hold()["outcome"])
        # Held: the next turn neither auto-acknowledges nor builds again.
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual(1, len(self.builds()))
        self.assertIn("held until the target changes",
                      self.latest()["launcher_ack"]["auto_refused"])
        # The printed ack is the owner's way through.
        self.ack().write_text(T_OLD + "\n")
        self.assertUpdated(self.apply())

    def test_the_owner_override_requires_the_ack_again(self) -> None:
        settings = self.home / ".config/tartci/self-update.toml"
        settings.write_text("[launcher]\nauto_apply = false\n")
        self.cfg = su.load_config(self.home)
        self.cfg.poll_seconds, self.cfg.wait_seconds = 45, 600
        self.assertFalse(self.cfg.launcher_auto_apply)
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertIn("auto_apply = false", self.latest()["launcher_ack"]["auto_refused"])
        settings.write_text("[launcher]\nauto_apply = \"no\"\n")
        with self.assertRaisesRegex(su.ConfigError, "auto_apply = true\\|false"):
            su.load_config(self.home)

    def test_a_lost_grant_rolls_back_holds_the_target_and_alerts(self) -> None:
        self.fail_probe_until_rollback()
        self.assertEqual(su.EXIT_FAILED, self.apply())
        last = self.last()
        self.assertEqual("rolled_back", last["status"], last)
        self.assertTrue(last["error"].startswith("launcher consent lost"), last["error"])
        self.assertIn("grant Removable Volumes to TartCI Launcher", last["error"])
        self.assertEqual(INSTALLED, self.sys.running())
        self.assertEqual(2, len(self.installs()), "forward install, then the rollback")
        receipt = json.loads(Path(last["receipt"]).read_text())
        self.assertIs(True, receipt["launcher_consent_lost"])
        self.assertEqual("probe_failed_rolled_back", receipt["launcher_ack"]["outcome"])
        hold = self.hold()
        self.assertEqual((T_OLD, "probe_failed"), (hold["target"], hold["outcome"]))
        self.assertEqual(f"echo '{T_OLD}' > {self.ack()}", hold["acknowledge"])
        opened = [title for kind, title in self.issues if kind == "open"]
        self.assertIn(f"[tartci] studio: launcher auto-apply rolled back; grant Removable "
                      f"Volumes to TartCI Launcher for {T_OLD[:12]}", opened)
        # The liveness check's first matching status line carries the remedy
        # and the exact command.
        lines = su.status_lines(self.cfg.state_dir, self.sys)
        alert = next(line for line in lines if LIVENESS_SELF_UPDATE_FAIL.search(line))
        self.assertIn("LAUNCHER CONSENT LOST", alert)
        self.assertIn("System Settings > Privacy & Security > Files and Folders", alert)
        self.assertIn(f"echo '{T_OLD}' > {self.ack()}", alert)
        # No loop: past the attempt window the same target waits for the ack
        # (main has not moved; a newer target would be tried once).
        self.sys.log_lines = f"{T_OLD} {int(NOW - 7200)}\n"
        self.sys.clock += self.cfg.rate_hours * 3600 + 60
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual(2, len(self.installs()))
        self.assertIn("held until the target changes",
                      self.latest()["launcher_ack"]["auto_refused"])
        self.assertTrue(any(LIVENESS_SELF_UPDATE_FAIL.search(line)
                            for line in su.status_lines(self.cfg.state_dir, self.sys)))

    def test_a_new_target_after_a_hold_is_tried_automatically(self) -> None:
        su.hold_launcher_auto_apply(self.cfg, self.sys, "c" * 40, "probe_failed", "x", "c" * 40)
        self.assertUpdated(self.apply())
        self.assertEqual("automatic", self.latest()["launcher_ack"]["mode"])
        self.assertIsNone(self.hold(), "a completed update clears the hold")

    def test_a_slow_volume_after_an_auto_ack_still_stops_without_rollback(self) -> None:
        # A slow store is not a lost grant, and a rollback would not help.
        self.sys.on_rc = 1
        self.sys.on_text = ("fleet readiness: launch helper volume probe timed out after 60s: "
                            "volume I/O slow; same-dir write from parent took 4200 ms")
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertIn("STOPPED and NOT rolled back", self.last()["error"])
        self.assertEqual(1, len(self.installs()))
        self.assertIsNone(self.hold())

    def test_a_failed_rollback_still_names_the_lost_grant(self) -> None:
        self.sys.on_rc = 1
        self.sys.on_text = LauncherProbeStopTests.PROBE
        self.assertEqual(su.EXIT_FAILED, self.apply())
        last = self.last()
        self.assertEqual("failed", last["status"])
        self.assertTrue(last["error"].startswith("launcher consent lost"), last["error"])
        self.assertIn("ROLLBACK FAILED", last["error"])

    def test_a_plan_reports_the_automatic_ack_and_changes_nothing(self) -> None:
        self.assertEqual(su.EXIT_OK, self.plan())
        self.assertEqual([], self.sys.mutations())
        self.assertEqual("automatic", self.latest()["launcher_ack"]["mode"])


class ChildPythonCallSiteTests(unittest.TestCase):
    """self-update starts every Python child with python_child.child_python():
    under launcher v2 sys.executable is the bare pin, not the lane's shim."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.shim = Path(td.name) / "python3"
        self.shim.write_text("#!/bin/sh\nexec true\n")
        self.shim.chmod(0o755)
        os.environ["TARTCI_PYTHON"] = str(self.shim)
        su._PYTHON_SHIM_DIR = None
        self.addCleanup(setattr, su, "_PYTHON_SHIM_DIR", None)

    def test_system_run_the_path_shim_and_census_use_the_child_interpreter(self) -> None:
        seen = []
        with mock.patch.object(su.subprocess, "run",
                               side_effect=lambda argv, **kw: seen.append(argv) or
                               subprocess.CompletedProcess(argv, 0, "", "")):
            su.System().run(["python3", "scripts/x.py"])
        self.assertEqual(str(self.shim), seen[0][0])
        directory = su.python_shim_dir()
        self.assertEqual(str(self.shim), os.readlink(os.path.join(directory, "python3")))
        self.assertEqual(str(self.shim), su.census_env()["TARTCI_PYTHON"])

    def test_v1_keeps_the_interpreter_it_ran_before(self) -> None:
        os.environ.pop("TARTCI_PYTHON")
        seen = []
        with mock.patch.object(su.subprocess, "run",
                               side_effect=lambda argv, **kw: seen.append(argv) or
                               subprocess.CompletedProcess(argv, 0, "", "")):
            su.System().run(["python3", "scripts/x.py"])
        self.assertEqual(sys.executable, seen[0][0])
        self.assertEqual(sys.executable, os.readlink(os.path.join(su.python_shim_dir(), "python3")))
        self.assertEqual(sys.executable, su.census_env()["TARTCI_PYTHON"])


class SealedRollbackTests(Base):
    sealed = True

    def pin(self) -> Path:
        return self.home / ".config/tartci/m3-launcher-approved.sha256"

    def test_rollback_pool_on_failure_cannot_claim_installed_and_on(self) -> None:
        self.sys.broken_target = True
        self.sys.on_rcs = [0, 1, 1, 1, 1, 1]
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertIn("ROLLBACK FAILED", self.last()["error"])
        self.assertNotIn("rollback installed and pool on", self.last()["error"])

    def test_sealed_rollback_restores_bundle_and_pin_together(self) -> None:
        self.sys.broken_target = True
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.last()["status"], "rolled_back")
        self.assertEqual(self.sys.running(), INSTALLED)
        self.assertEqual(self.pin().read_text(), "0" * 64 + "\n")
        rollback = [a for a, _ in self.sys.calls if "--apply" in a][-1]
        source = rollback[rollback.index("--launch-helper-source") + 1]
        self.assertIn("rollback", source)

    def test_rollback_reinstalls_with_the_previous_interpreter_and_pin(self) -> None:
        # m5s, 2026-10-15: the update switched the lanes to a new interpreter
        # and its pin; the rollback rendered the old launcher against the new
        # pin, refused, and left the host off.
        import plistlib  # noqa: PLC0415
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        lane = agents / "com.danielraffel.tartci.tart-runner-macos-fleet.m1.pulp-gate.plist"
        lane.write_bytes(plistlib.dumps({"Label": lane.stem, "EnvironmentVariables": {
            "TARTCI_PYTHON": "/old/python3.15"}}))
        pin = self.home / ".config" / "tartci" / "python.pin"
        pin.write_text("/old/python3.15\n")

        seen = {"installs": 0}

        def switch_on_install(argv):
            if "--apply" in argv and "install" in argv:
                seen["installs"] += 1
                if seen["installs"] == 1:  # the new generation's install writes its pin
                    pin.write_text("/new/python3.12\n")
        self.sys.hook = switch_on_install
        self.sys.broken_target = True
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        self.assertEqual(self.last()["status"], "rolled_back")
        installs = [(a, e) for (a, _), e in zip(self.sys.calls, self.sys.env_calls)
                    if "--apply" in a and "install" in a]
        self.assertGreaterEqual(len(installs), 2)
        rollback_argv, rollback_env = installs[-1]
        self.assertIn("rollback", " ".join(rollback_argv))
        self.assertEqual(rollback_env.get("TARTCI_PYTHON"), "/old/python3.15")
        # It must outrank the snapshot profile's host.python in the installer.
        self.assertEqual(rollback_env.get("TARTCI_ROLLBACK_PYTHON"), "/old/python3.15")
        self.assertNotIn("TARTCI_ROLLBACK_PYTHON", installs[0][1] or {})
        self.assertEqual(pin.read_text().strip(), "/old/python3.15")

    def test_rollback_pins_what_the_lanes_ran_when_the_old_pin_disagrees(self) -> None:
        # The installer honours TARTCI_ROLLBACK_PYTHON only when it equals the
        # pin or a lane's, so the pin must name the interpreter reinstalled.
        import plistlib  # noqa: PLC0415
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        lane = agents / "com.danielraffel.tartci.tart-runner-macos-fleet.m1.pulp-gate.plist"
        lane.write_bytes(plistlib.dumps({"Label": lane.stem, "EnvironmentVariables": {
            "TARTCI_PYTHON": "/old/lane-python3.15"}}))
        pin = self.home / ".config" / "tartci" / "python.pin"
        pin.write_text("/old/pin-python3.15\n")

        def new_pin_on_first_install(argv):
            if "--apply" in argv and "install" in argv and pin.read_text().startswith("/old/pin"):
                pin.write_text("/new/python3.12\n")
        self.sys.hook = new_pin_on_first_install
        self.sys.broken_target = True
        self.assertEqual(self.apply(), su.EXIT_FAILED)
        installs = [e for (a, _), e in zip(self.sys.calls, self.sys.env_calls)
                    if "--apply" in a and "install" in a]
        self.assertEqual(installs[-1].get("TARTCI_ROLLBACK_PYTHON"), "/old/lane-python3.15")
        self.assertEqual(pin.read_text().strip(), "/old/lane-python3.15")

    def test_a_running_update_shows_in_status_with_its_step(self) -> None:
        # m1, 2026-10-22: a 20 min drain wait was invisible in --status.
        receipt = self.cfg.state_dir / "attempts" / "20261022T074900Z-afd9b8171459.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({
            "mode": "apply", "status": "running", "target": "afd9b817" * 5,
            "started_at": "2026-10-22T07:49:00Z", "pid": 4242, "pid_start": "Thu 07:49",
            "steps": [{"at": "2026-10-22T07:50:00Z", "step": "drain", "ok": True,
                       "detail": "waiting for 2 VMs"}]}))
        self.sys.procs[4242] = "Thu 07:49"
        line = su.running_line(self.cfg.state_dir, self.sys)
        self.assertIn("IN PROGRESS to afd9b817afd9", line)
        self.assertIn("pid 4242", line)
        self.assertIn("last step drain at 2026-10-22T07:50:00Z: waiting for 2 VMs", line)
        self.assertIn(line, su.status_lines(self.cfg.state_dir, self.sys))
        del self.sys.procs[4242]
        self.assertIn("INTERRUPTED", su.running_line(self.cfg.state_dir, self.sys))

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
    def test_explicit_target_recovers_an_off_history_install(self) -> None:
        self.sys.cat_file_rc = 1
        scheduled = su.measure_skew(self.cfg, self.sys, "d" * 40, NOW)
        self.assertEqual(scheduled["state"], "unknown")
        self.assertIn("not in the tartci history", scheduled["reason"])
        explicit = su.measure_skew(self.cfg, self.sys, "d" * 40, NOW, T_OLD, explicit=True)
        self.assertEqual((explicit["state"], explicit["behind"], explicit["target"],
                          explicit["selected_target"]), ("behind", None, T_OLD, T_OLD))
        self.assertIn("not in the tartci history", explicit["reason"])
        self.assertIsNone(explicit["oldest_undeployed"])
        self.assertNotIn("stale", explicit)
        self.assertIn("skew UNKNOWN", su.render_skew(explicit))

    def test_off_history_explicit_target_still_soaking_is_refused(self) -> None:
        self.sys.cat_file_rc = 1
        self.sys.log_lines = f"{T_NEW} {int(NOW - 60)}\n{T_OLD} {int(NOW - 9000)}\n"
        skew = su.measure_skew(self.cfg, self.sys, "d" * 40, NOW, T_NEW, explicit=True)
        self.assertEqual(skew["state"], "soaking")
        self.assertIsNone(skew["behind"])
        self.assertEqual(su.plan_or_apply(self.cfg, self.sys, apply=True,
                                          target_ref=T_NEW, explicit=True), su.EXIT_REFUSED)

    def test_off_history_explicit_red_target_is_refused(self) -> None:
        self.sys.cat_file_rc = 1
        self.sys.log_lines = f"{T_NEW} {int(NOW - 9000)}\n{T_OLD} {int(NOW - 10000)}\n"
        self.sys.checks[T_NEW] = [{"name": "lint", "status": "completed",
                                   "conclusion": "failure"}]
        skew = su.measure_skew(self.cfg, self.sys, "d" * 40, NOW, T_NEW, explicit=True)
        self.assertEqual(skew["state"], "unverified")
        self.assertIsNone(skew["behind"])
        self.assertEqual(su.plan_or_apply(self.cfg, self.sys, apply=True,
                                          target_ref=T_NEW, explicit=True), su.EXIT_REFUSED)

    def test_off_history_explicit_off_chain_target_is_refused(self) -> None:
        self.sys.cat_file_rc = 1
        skew = su.measure_skew(self.cfg, self.sys, "d" * 40, NOW, "d" * 40, explicit=True)
        self.assertEqual(skew["state"], "unknown")
        self.assertIn("first-parent", skew["reason"])

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

    def test_explicit_main_tip_soaking_is_refused_without_install(self) -> None:
        self.sys.log_lines = f"{T_NEW} {int(NOW - 60)}\n{T_OLD} {int(NOW - 9000)}\n"
        skew = su.measure_skew(self.cfg, self.sys, INSTALLED, NOW, T_NEW, explicit=True)
        self.assertEqual(skew["state"], "soaking")
        self.assertIn("soaking until", skew["selection_reason"])
        self.assertEqual(su.plan_or_apply(self.cfg, self.sys, apply=True, target_ref=T_NEW, explicit=True), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])

    def test_explicit_main_tip_soaked_but_red_is_refused_without_install(self) -> None:
        self.sys.log_lines = f"{T_NEW} {int(NOW - 7200)}\n{T_OLD} {int(NOW - 9000)}\n"
        self.sys.checks[T_NEW] = [{"name": "lint", "status": "completed", "conclusion": "failure"}]
        skew = su.measure_skew(self.cfg, self.sys, INSTALLED, NOW, T_NEW, explicit=True)
        self.assertEqual(skew["state"], "unverified")
        self.assertIn("not green: ", skew["selection_reason"])
        self.assertEqual(su.plan_or_apply(self.cfg, self.sys, apply=True, target_ref=T_NEW, explicit=True), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])

    def test_a_pull_request_judgement_on_mains_tip_is_not_its_check(self) -> None:
        # tartci 0b651d9b, 2026-10-22: a pull-request comment ran the
        # review-verdict workflow on main's tip, and its in-progress check run
        # held m5s's self-update as "not green: review-verdict=in_progress".
        lint = {"name": "lint", "status": "completed", "conclusion": "success",
                "check_suite": {"id": 1}}
        verdict = {"name": "review-verdict", "status": "in_progress", "conclusion": None,
                   "check_suite": {"id": 7}}
        self.sys.checks[T_OLD] = [lint, verdict]
        self.sys.workflow_runs[T_OLD] = [
            {"event": "push", "check_suite_id": 1},
            {"event": "issue_comment", "check_suite_id": 7},
        ]
        self.assertEqual(su.checks_green(self.cfg, self.sys, T_OLD), (True, "all checks green"))
        # The same run from a push is the commit's own check, and holds it.
        self.sys.workflow_runs[T_OLD][1]["event"] = "push"
        self.assertEqual(su.checks_green(self.cfg, self.sys, T_OLD),
                         (False, "not green: review-verdict=in_progress"))
        # When the workflow runs cannot be read, every check run counts.
        del self.sys.workflow_runs[T_OLD]
        self.assertFalse(su.checks_green(self.cfg, self.sys, T_OLD)[0])
        # Only pull-request judgements on a commit is not a green commit.
        self.sys.checks[T_OLD] = [verdict]
        self.sys.workflow_runs[T_OLD] = [{"event": "issue_comment", "check_suite_id": 7}]
        self.assertEqual(su.checks_green(self.cfg, self.sys, T_OLD),
                         (False, "no check runs recorded"))

    def test_default_path_falls_back_to_soaked_green_and_records_skipped(self) -> None:
        self.sys.log_lines = f"{T_NEW} {int(NOW - 7200)}\n{T_OLD} {int(NOW - 9000)}\n"
        self.sys.checks[T_NEW] = [{"name": "lint", "status": "completed", "conclusion": "failure"}]
        skew = su.measure_skew(self.cfg, self.sys, INSTALLED, NOW)
        self.assertEqual(skew["selected_target"], T_OLD)
        self.assertTrue(skew["skipped"])

    def test_explicit_selection_is_printed_and_recorded(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(su.plan_or_apply(self.cfg, self.sys, apply=False,
                                              target_ref=T_OLD, explicit=True), su.EXIT_OK)
        self.assertIn(f"selected {T_OLD} (requested {T_OLD}; same)", output.getvalue())
        self.assertIn(f"repository={su.DEFAULT_REPOSITORY}", output.getvalue())
        receipts = list((self.cfg.state_dir / "attempts").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual({key: receipt[key] for key in
                          ("requested_target", "selected_target", "explicit", "soak_until")},
                         {"requested_target": T_OLD, "selected_target": T_OLD,
                          "explicit": True, "soak_until": None})

    def test_main_explicit_target_is_refused_without_install(self) -> None:
        self.sys.log_lines = f"{T_NEW} {int(NOW - 60)}\n{T_OLD} {int(NOW - 9000)}\n"
        with mock.patch.object(su, "load_config", return_value=self.cfg), \
                mock.patch.object(su, "System", return_value=self.sys):
            result = su.main(["--home", str(self.home), "--target", T_NEW, "--plan"])
        self.assertEqual(result, su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])

    def test_main_without_target_keeps_default_green_fallback(self) -> None:
        self.sys.log_lines = f"{T_NEW} {int(NOW - 7200)}\n{T_OLD} {int(NOW - 9000)}\n"
        self.sys.checks[T_NEW] = [{"name": "lint", "status": "completed", "conclusion": "failure"}]
        with mock.patch.object(su, "load_config", return_value=self.cfg), \
                mock.patch.object(su, "System", return_value=self.sys):
            result = su.main(["--home", str(self.home), "--plan"])
        self.assertEqual(result, su.EXIT_OK)
        receipts = list((self.cfg.state_dir / "attempts").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())["target"], T_OLD)

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
            original = (ROOT / "profiles/m1-macos-fleet.toml").read_text()
            # This test exercises profile-to-publication alias derivation for
            # every profile.  Keep its fixture published even when production
            # temporarily withdraws a host from the fleet.
            (root / "profiles/m1-macos-fleet.toml").write_text(
                original.replace("published = false", "published = true", 1))
            text = original.replace("published = false", "published = true", 1)
            text = text.replace('name = "m1-macos-fleet"', 'name = "x9-macos-fleet"')
            text = text.replace('id = "m1"', 'id = "x9"', 1).replace('ssh = "m1"\n', "")
            (root / "profiles/x9-macos-fleet.toml").write_text(text)
            hosts = {row["host_id"]: row["ssh"] for row in fleet.published_snapshot(root)["hosts"]}
            self.assertEqual(hosts, {"m1": "m1", "studio": "m3", "m5": "m5", "m5studio": "m5s", "x9": None})
            (root / "profiles/x9-macos-fleet.toml").write_text(
                text.replace('id = "x9"', 'id = "x9"\nssh = "bad alias!"', 1))
            with self.assertRaises(ValueError):
                fleet.load(root / "profiles/x9-macos-fleet.toml")



class PeerSshDestinationTests(Base):
    """A published hosts[].ssh is not bound by profiles_sha256: a value ssh
    would read as an option is refused at every peer SSH site, the peer reads
    as unreadable (fail closed), and every ssh argv ends options with "--"."""

    HOSTILE = "-oProxyCommand=touch /tmp/x"

    def publish_hostile_studio(self) -> None:
        for row in self.sys.published["hosts"]:
            if row["host_id"] == "studio":
                row["ssh"] = self.HOSTILE

    def ssh_calls(self) -> list[list[str]]:
        return [a for a, _ in self.sys.calls if a[:1] == ["ssh"]]

    def assertNoHostileSsh(self) -> None:
        self.assertFalse([a for a in self.ssh_calls() if any("ProxyCommand" in x for x in a)])

    def test_peer_ssh_refuses_options_and_ends_options_before_the_destination(self) -> None:
        for bad in (self.HOSTILE, "-p22", "", None, 7, "a b", "m3;id"):
            with self.subTest(bad=bad):
                self.assertIsNone(su.peer_ssh(bad))
        for good in ("m3", "tartci-x9", "admin@m5s.lan"):
            with self.subTest(good=good):
                argv = su.peer_ssh(good)
                self.assertEqual(argv[-2:], ["--", good])

    def test_published_peers_keeps_a_refused_host_so_it_still_counts(self) -> None:
        self.publish_hostile_studio()
        peers = su.published_peers(self.cfg, self.sys)
        self.assertEqual(peers["studio"], self.HOSTILE)
        self.assertEqual(peers["m5"], "m5")

    def test_read_peer_refuses_a_hostile_destination_and_fails_closed(self) -> None:
        peer = su.read_peer(self.cfg, self.sys, "studio", self.HOSTILE)
        self.assertTrue(peer["busy"])
        self.assertFalse(peer["readable"])
        self.assertIn("ssh destination", peer["evidence"])
        self.assertIn("refused", peer["evidence"])
        self.assertEqual(self.ssh_calls(), [])

    def test_read_peer_still_reads_a_normal_alias_after_double_dash(self) -> None:
        peer = su.read_peer(self.cfg, self.sys, "studio", "m3")
        self.assertTrue(peer["readable"])
        self.assertTrue(self.ssh_calls())
        for argv in self.ssh_calls():
            self.assertEqual(argv[argv.index("--") + 1], "m3")

    def test_a_hostile_published_peer_never_lets_an_update_proceed(self) -> None:
        self.publish_hostile_studio()
        self.assertNotEqual(self.last_status_after_apply(), "succeeded")
        self.assertNoHostileSsh()
        for argv in self.ssh_calls():
            self.assertIn("--", argv)

    def last_status_after_apply(self) -> str | None:
        self.apply()
        path = self.cfg.state_dir / "last.json"
        return json.loads(path.read_text()).get("status") if path.exists() else None

    def test_a_normal_published_alias_still_updates(self) -> None:
        self.assertUpdated(self.apply())
        self.assertTrue(self.ssh_calls())
        for argv in self.ssh_calls():
            self.assertIn("--", argv)

    def test_read_canary_verified_refuses_a_hostile_destination(self) -> None:
        with mock.patch.object(su, "published_peers", return_value={"studio": self.HOSTILE}), \
                mock.patch.object(self.sys, "run", side_effect=AssertionError("ssh ran")), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertIsNone(su.read_canary_verified(self.cfg, self.sys, "studio"))
        self.assertIn("refused", err.getvalue())

    def test_read_canary_verified_reads_a_normal_alias_after_double_dash(self) -> None:
        record = {"repository": "ORG/fleet", "commit": "a" * 40, "verified_at": 1000}
        seen: list[list[str]] = []

        def run(argv, **kw):
            seen.append(argv)
            return su.Result(0, "1600\n" + json.dumps(record) + "\n")
        with mock.patch.object(su, "published_peers", return_value={"studio": "m3"}), \
                mock.patch.object(self.sys, "run", side_effect=run):
            self.assertEqual(su.read_canary_verified(self.cfg, self.sys, "studio")["age"], 600.0)
        self.assertEqual(seen[0][seen[0].index("--") + 1], "m3")

    def test_read_tart_verified_refuses_a_hostile_destination(self) -> None:
        with mock.patch.object(su, "published_peers", return_value={"studio": self.HOSTILE}), \
                mock.patch.object(self.sys, "run", side_effect=AssertionError("ssh ran")):
            value = su.read_tart_verified(self.cfg, self.sys, "studio")
        self.assertIn("refused", value["unreachable"])

    def test_read_tart_verified_reads_a_normal_alias_after_double_dash(self) -> None:
        self.sys.peer_tart["m3"] = {"version": "2.40.1", "sha256": TART_SHA, "verified_at": NOW}
        value = su.read_tart_verified(self.cfg, self.sys, "studio")
        self.assertNotIn("unreachable", value or {})
        argv = [a for a in self.ssh_calls() if a[-1] == su.TART_VERIFIED_SCRIPT][0]
        self.assertEqual(argv[argv.index("--") + 1], "m3")

    def coverage(self, target: str) -> tuple[dict, list]:
        supply = {"registrations": [{"host_id": "studio", "labels": ["mac"]}], "hosts": []}
        with mock.patch.object(su, "published_supply", return_value=supply), \
                mock.patch.object(su, "published_peers", return_value={"studio": target}), \
                mock.patch.object(su, "peer_mints_on_demand", return_value=None):
            return su.on_demand_supply(self.cfg, self.sys, "m1", [("mac", None)])

    def test_coverage_probe_refuses_a_hostile_destination(self) -> None:
        covered, missing = self.coverage(self.HOSTILE)
        self.assertEqual(covered, {})
        self.assertIn("refused", missing[0])
        self.assertEqual(self.ssh_calls(), [])

    def test_coverage_probe_reads_a_normal_alias_after_double_dash(self) -> None:
        covered, missing = self.coverage("m3")
        self.assertEqual((covered, missing), ({"mac": ["studio"]}, []))
        argv = self.ssh_calls()[0]
        self.assertEqual(argv[argv.index("--") + 1], "m3")


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
        self.assertEqual([kind for kind, _ in self.issues], ["open"])
        self.assertIn("left OFF", self.issues[0][1])
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


class RepositoryIdentityTests(Base):
    def test_repository_resolution_refusal_is_receipted_at_the_entry_point(self):
        self.sys.unreadable_repositories.update(
            {su.LEGACY_REPOSITORY, su.DEFAULT_REPOSITORY})
        with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": FAKE_GH}, clear=True), \
                mock.patch.object(su, "load_config", return_value=self.cfg), \
                mock.patch.object(su, "System", return_value=self.sys):
            result = su.main(["--home", str(self.home), "--apply"])
        self.assertEqual(result, su.EXIT_REFUSED)
        receipts = list((self.cfg.state_dir / "attempts").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt["status"], "refused")
        self.assertEqual(receipt["target"], INSTALLED)
        self.assertIn("no accepted TartCI repository is readable", receipt["error"])
        self.assertEqual(receipt["steps"][-1]["step"], "refused")
        self.assertEqual(json.loads((self.cfg.state_dir / "skew.json").read_text())["state"],
                         "unknown")

    HUNG = "TimeoutExpired: Command '['ghapp', 'api', 'repos/x']' timed out after 60 seconds"

    def reads(self) -> list[str]:
        return [a[2] for a, _ in self.sys.calls if a and a[0] == FAKE_GH]

    def test_a_read_that_timed_out_is_retried_once_and_then_succeeds(self):
        # m1, 2026-10-16: one hung read refused the whole self-update.
        self.sys.api_repository = su.LEGACY_REPOSITORY
        self.sys.repository_failures[su.LEGACY_REPOSITORY] = [su.Result(127, "", self.HUNG)]
        before = self.sys.clock
        with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": FAKE_GH}, clear=True):
            self.assertEqual(su.configured_repository(self.cfg, self.sys), su.LEGACY_REPOSITORY)
        self.assertEqual(self.reads(), [f"repos/{su.LEGACY_REPOSITORY}"] * 2)
        self.assertEqual(self.sys.clock - before, su.REPOSITORY_READ_RETRY_SECONDS)

    def test_reads_that_keep_hanging_refuse_naming_each_failure(self):
        for repository in (su.LEGACY_REPOSITORY, su.DEFAULT_REPOSITORY):
            self.sys.repository_failures[repository] = [su.Result(127, "", self.HUNG)] * 2
        with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": FAKE_GH}, clear=True):
            with self.assertRaises(su.Refused) as caught:
                su.configured_repository(self.cfg, self.sys)
        message = str(caught.exception)
        self.assertIn("no accepted TartCI repository is readable", message)
        for repository in (su.LEGACY_REPOSITORY, su.DEFAULT_REPOSITORY):
            self.assertIn(f"{repository}: exit 127: TimeoutExpired", message)
        self.assertEqual(message.count("(after one retry)"), 2)
        self.assertEqual(len(self.reads()), 4)

    def test_gh_network_errors_are_retried_once(self):
        for text in ("error connecting to api.github.com",
                     "Get \"https://api.github.com/repos/x\": dial tcp: lookup api.github.com: "
                     "no such host"):
            with self.subTest(text=text):
                self.sys.calls.clear()
                self.sys.api_repository = su.LEGACY_REPOSITORY
                self.sys.repository_failures[su.LEGACY_REPOSITORY] = [su.Result(1, "", text)]
                before = self.sys.clock
                with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": FAKE_GH}, clear=True):
                    self.assertEqual(su.LEGACY_REPOSITORY,
                                     su.configured_repository(self.cfg, self.sys))
                self.assertEqual([f"repos/{su.LEGACY_REPOSITORY}"] * 2, self.reads())
                self.assertEqual(su.REPOSITORY_READ_RETRY_SECONDS, self.sys.clock - before)

    def test_a_successful_read_is_never_retried_whatever_its_json_says(self):
        # rc 0 with the wrong full_name is an answer; "network_count" in the
        # JSON must not look like a transport error.
        answer = su.Result(0, json.dumps({"full_name": "example/other", "network_count": 3,
                                          "description": "timed out connection reset"}), "")
        for repository in (su.LEGACY_REPOSITORY, su.DEFAULT_REPOSITORY):
            self.sys.repository_failures[repository] = [answer]
        before = self.sys.clock
        with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": FAKE_GH}, clear=True):
            with self.assertRaises(su.Refused) as caught:
                su.configured_repository(self.cfg, self.sys)
        self.assertEqual(2, len(self.reads()))
        self.assertEqual(before, self.sys.clock)
        self.assertIn("answered example/other", str(caught.exception))

    def test_a_definite_answer_is_not_retried(self):
        # Control: a 404 is an answer, not a transport failure.
        self.sys.unreadable_repositories.update({su.LEGACY_REPOSITORY, su.DEFAULT_REPOSITORY})
        before = self.sys.clock
        with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": FAKE_GH}, clear=True):
            with self.assertRaises(su.Refused) as caught:
                su.configured_repository(self.cfg, self.sys)
        self.assertEqual(len(self.reads()), 2)
        self.assertEqual(self.sys.clock, before)
        self.assertIn(f"{su.LEGACY_REPOSITORY}: exit 1: not found", str(caught.exception))

    def test_pretransfer_installer_dispatch_carries_readable_legacy_slug(self):
        self.sys.api_repository = su.LEGACY_REPOSITORY
        self.sys.unreadable_repositories.add(su.DEFAULT_REPOSITORY)
        with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": FAKE_GH}, clear=True):
            repository = su.configured_repository(self.cfg, self.sys)
            run = object.__new__(su.Run)
            run.cfg, run.sys, run.install_args, run.repository = (
                self.cfg, self.sys, ["fleet-macos", "install", "profile"], repository)
            run.receipt = mock.Mock()
            run._writer_fence = lambda: []
            su.Run._install(run, run.install_args)
        self.assertEqual(su.LEGACY_REPOSITORY, repository)
        self.assertEqual(su.LEGACY_REPOSITORY, self.sys.env_calls[-1]["GH_REPO"])

    def test_default_resolution_uses_first_readable_accepted_slug(self):
        self.sys.unreadable_repositories.add(su.LEGACY_REPOSITORY)
        self.sys.api_repository = su.DEFAULT_REPOSITORY
        with mock.patch.dict(os.environ, {"TARTCI_GH_CLI": FAKE_GH}, clear=True):
            self.assertEqual(su.DEFAULT_REPOSITORY,
                             su.canonical_repository(self.cfg, self.sys))

    def test_api_accepts_legacy_and_new_full_name(self):
        for repository in (su.LEGACY_REPOSITORY, su.DEFAULT_REPOSITORY):
            self.sys.api_repository = repository
            with mock.patch.dict(os.environ, {"GH_REPO": repository}):
                self.assertEqual(su.canonical_repository(self.cfg, self.sys), repository)

    def test_api_rejects_third_repository(self):
        self.sys.api_repository = "example/other-tartci"
        with self.assertRaises(su.Refused):
            su.canonical_repository(self.cfg, self.sys)

    def test_refresh_rejects_third_remote(self):
        self.sys.remote_url = "https://example.invalid/other-tartci.git\n"
        with self.assertRaises(su.Refused):
            su.refresh_checkout(self.cfg, self.sys)


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

    def test_os_interpreter_refresh_does_not_bypass_code_soak(self) -> None:
        self.sys.log_lines = f"{T_NEW} {int(NOW - 60)}\n"
        self.sys.status_problems = [{"code": "interpreter_changed_by_os_update",
                                     "detail": "macOS build changed"}]
        self.assertEqual(self.apply(), su.EXIT_NOTHING)
        self.assertEqual(self.sys.mutations(), [])


class TartUpgradeTests(Base):
    """fleet/tart.toml pins Tart; a host that opts in moves to it while its pool
    is off, the canary first, proven by booting a clone of the golden. On a
    failure the new kegs are removed so the previous ones are linked again,
    the pin is not retried here, and a rollback that does not bring the
    previous Tart back leaves the host OFF."""

    PIN = ('[tart]\nversion = "2.40.1"\nsha256 = "' + TART_SHA + '"\n'
           '[softnet]\nversion = "0.24.0"\nsha256 = "' + SOFTNET_SHA + '"\n')
    SOAK = 4 * 3600

    def setUp(self) -> None:
        super().setUp()
        # The commit this turn installs (checkout) and the installed generation
        # (refresh_reason reads it) carry the same pin unless a test moves one.
        self.installed_root = self.home / "installed-generation"
        for root in (self.cfg.checkout, self.installed_root):
            (root / "fleet").mkdir(parents=True, exist_ok=True)
            (root / "fleet" / "tart.toml").write_text(self.PIN)
        opt = self.home / "opt"
        opt.mkdir()
        for name, value in (("OPT", opt), ("REPO_ROOT", self.installed_root)):
            patcher = mock.patch.object(tart_pin, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sys.set_tart("2.36.0")
        self.sys.set_softnet("0.23.0")
        self.cfg.tart_upgrade, self.cfg.tart_canary = True, "m1"   # this host is m1

    def receipt(self) -> dict:
        return json.loads(Path(self.last()["receipt"]).read_text())

    def steps(self, name: str) -> list[dict]:
        return [step for step in self.receipt()["steps"] if step["step"] == name]

    def index_of(self, predicate) -> int:
        return next(i for i, (argv, _) in enumerate(self.sys.calls) if predicate(argv))

    def calls(self, *prefix: str) -> list[list[str]]:
        return [a for a, _ in self.sys.calls if a[:len(prefix)] == list(prefix)]

    def upgrades(self) -> list[list[str]]:
        return self.calls(tart_pin.BREW, "upgrade")

    def verified(self) -> dict | None:
        return su._read_json(self.cfg.state_dir / tart_pin.VERIFIED)

    def failed(self) -> dict | None:
        return su._read_json(self.cfg.state_dir / tart_pin.FAILED)

    def gate(self) -> dict | None:
        return su._read_json(self.cfg.state_dir / tart_pin.GATE)

    def decide(self):
        return su.tart_decision(self.cfg, self.sys)

    # ── off by default, canary first ────────────────────────────────────

    def test_off_by_default(self) -> None:
        self.cfg.tart_upgrade = False
        self.assertUpdated(self.apply())
        self.assertEqual([], self.upgrades())
        self.assertEqual("2.36.0", self.sys.tart)
        self.assertEqual([], self.steps("tart-upgrade"))
        self.assertIs(False, self.gate()["due"])

    def test_the_canary_upgrades_while_off_boots_a_clone_and_publishes(self) -> None:
        self.assertUpdated(self.apply())
        self.assertEqual(("2.40.1", "0.24.0"), (self.sys.tart, self.sys.softnet))
        self.assertIn("Tart 2.36.0 -> 2.40.1, Softnet 0.24.0; a clone of pulp-build-runner:latest "
                      "booted", self.steps("tart-upgrade")[0]["detail"])
        upgrade = self.index_of(lambda a: a[:2] == [tart_pin.BREW, "upgrade"])
        off = self.index_of(lambda a: a[:3] == ["./tartci", "pool", "off"] and "--plan" not in a)
        install = self.index_of(lambda a: "install" in a and "--apply" in a)
        boot = self.index_of(lambda a: a[:2] == [tart_pin.TART, "ip"])
        self.assertLess(off, upgrade)
        self.assertLess(upgrade, boot)
        self.assertLess(boot, install)
        env = self.sys.env_calls[upgrade]
        self.assertEqual("1", env["HOMEBREW_NO_AUTO_UPDATE"])
        self.assertEqual("1", env["HOMEBREW_NO_INSTALL_CLEANUP"])
        self.assertEqual({"version": "2.40.1", "sha256": TART_SHA},
                         {k: self.verified()[k] for k in ("version", "sha256")})
        published = [s["step"] for s in self.receipt()["steps"]]
        self.assertLess(published.index("verify"), published.index("tart-verified"))
        self.assertEqual([], list((self.cfg.state_dir.parent / "scratch-vms").glob("*.json")),
                         "the clone's scratch record goes once it is deleted")

    def test_the_boot_check_runs_with_the_store_s_tart_home_and_stops_the_clone(self) -> None:
        self.assertUpdated(self.apply())
        profile_home = su.tart_boot_target(self.cfg)[1]
        for verb in ("clone", "ip", "stop", "delete"):
            index = self.index_of(lambda a, v=verb: a[:2] == [tart_pin.TART, v])
            self.assertEqual(profile_home, self.sys.env_calls[index]["TART_HOME"], verb)

    def test_another_host_waits_for_the_canary(self) -> None:
        self.cfg.tart_canary = "studio"
        self.assertUpdated(self.apply())
        self.assertEqual([], self.upgrades())
        self.assertIn("waiting for canary studio to boot and serve Tart 2.40.1",
                      self.steps("tart-upgrade")[0]["detail"])
        self.assertIsNone(self.verified())
        self.assertIs(False, self.gate()["due"], "waiting for the canary is not a problem")

    def test_an_unreachable_canary_is_named_as_such(self) -> None:
        self.cfg.tart_canary = "studio"
        self.sys.peer_tart_rc = 255
        self.assertIn("waiting for canary studio: unreachable or unknown (ssh m3 exit 255)",
                      self.decide().refusal)

    def test_a_canary_record_for_another_version_or_sha_is_not_adoption(self) -> None:
        self.cfg.tart_canary = "studio"
        for record in ({"version": "2.39.0", "sha256": TART_SHA, "verified_at": NOW - 9 * 3600},
                       {"version": "2.40.1", "sha256": "c" * 64, "verified_at": NOW - 9 * 3600},
                       {"version": "2.40.1", "verified_at": NOW - 9 * 3600}):
            with self.subTest(record=record):
                self.sys.peer_tart["m3"] = record
                self.assertIn("waiting for canary studio to boot", self.decide().refusal)

    def test_adoption_waits_exactly_the_soak_on_the_canary_s_clock(self) -> None:
        self.cfg.tart_canary = "studio"
        # The canary's clock is 2 h ahead: its record is aged on its clock.
        self.sys.peer_tart_clock = NOW + 7200
        record = {"version": "2.40.1", "sha256": TART_SHA}
        self.sys.peer_tart["m3"] = dict(record, verified_at=NOW + 7200 - (self.SOAK - 1))
        self.assertIn("soaking for 240 min", self.decide().refusal)
        self.sys.peer_tart["m3"] = dict(record, verified_at=NOW + 7200 - self.SOAK)
        self.assertIsNone(self.decide().refusal)

    def test_another_host_adopts_after_the_soak(self) -> None:
        self.cfg.tart_canary = "studio"
        self.sys.peer_tart["m3"] = {"version": "2.40.1", "sha256": TART_SHA,
                                    "verified_at": NOW - 5 * 3600}
        self.assertUpdated(self.apply())
        self.assertEqual("2.40.1", self.sys.tart)
        self.assertIsNone(self.verified(), "only the canary publishes")

    def test_a_non_canary_already_on_the_pin_does_nothing(self) -> None:
        self.cfg.tart_canary = "studio"
        self.sys.set_tart("2.40.1")
        self.assertIsNone(self.decide())

    def test_a_newer_tart_than_the_pin_is_never_touched_and_never_drains(self) -> None:
        self.sys.set_tart("2.41.1")
        self.assertIn("Tart 2.41.1 is newer than the pin 2.40.1", self.decide().refusal)
        self.sys.log_lines = ""
        self.assertEqual(su.EXIT_NOTHING, self.apply())
        self.assertEqual([], self.sys.mutations())

    # ── the tap ─────────────────────────────────────────────────────────

    def test_a_tap_that_does_not_offer_exactly_the_pin_is_refused(self) -> None:
        for tap in ({"tart": ("2.41.1", TART_SHA), "softnet": ("0.24.0", SOFTNET_SHA)},
                    {"tart": ("2.40.1", "c" * 64), "softnet": ("0.24.0", SOFTNET_SHA)},
                    {"tart": ("2.40.1", TART_SHA), "softnet": ("0.25.0", SOFTNET_SHA)}):
            with self.subTest(tap=tap):
                (self.cfg.state_dir / tart_pin.TAP_CACHE).unlink(missing_ok=True)
                self.sys.tap = tap
                self.assertIn("the tap offers", self.decide().refusal)
        self.assertUpdated(self.apply())
        self.assertEqual([], self.upgrades())

    def test_the_tap_cache_is_per_pin_and_never_from_the_future(self) -> None:
        self.assertIsNone(self.decide().refusal)
        infos = lambda: len(self.calls(tart_pin.BREW, "info", "--json=v2"))  # noqa: E731
        self.assertIsNone(self.decide().refusal)
        self.assertEqual(1, infos(), "cached for an hour")
        cache = su._read_json(self.cfg.state_dir / tart_pin.TAP_CACHE)
        su._write_json(self.cfg.state_dir / tart_pin.TAP_CACHE, dict(cache, at=NOW + 600))
        self.decide()
        self.assertEqual(2, infos(), "a cache dated in the future is not trusted")
        (self.cfg.checkout / "fleet" / "tart.toml").write_text(
            self.PIN.replace("2.40.1", "2.40.2"))
        self.sys.tap["tart"] = ("2.40.2", TART_SHA)
        self.assertIsNone(self.decide().refusal, "a moved pin never reads the old answer")
        self.assertEqual(3, infos())

    # ── failure, rollback and memory ────────────────────────────────────

    def test_a_clone_that_does_not_boot_restores_the_previous_kegs(self) -> None:
        self.sys.boot_ok = False
        self.assertUpdated(self.apply())
        self.assertEqual(("2.36.0", "0.23.0"), (self.sys.tart, self.sys.softnet))
        self.assertEqual("../Cellar/tart/2.36.0", os.readlink(tart_pin.OPT / "tart"))
        self.assertEqual("../Cellar/softnet/0.23.0", os.readlink(tart_pin.OPT / "softnet"))
        self.assertEqual({"2.36.0"}, self.sys.kegs["tart"], "only the new keg was removed")
        uninstalls = self.calls(tart_pin.BREW, "uninstall")
        self.assertEqual(["openai/tools/tart", "openai/tools/softnet"], [a[-1] for a in uninstalls])
        self.assertIn("--ignore-dependencies", uninstalls[0])
        failed = self.steps("tart-upgrade")[0]
        self.assertFalse(failed["ok"])
        self.assertIn("did not report an IP", failed["detail"])
        rollback = self.steps("tart-rollback")[0]
        self.assertTrue(rollback["ok"], rollback)
        self.assertIn("restored tart ../Cellar/tart/2.36.0, softnet ../Cellar/softnet/0.23.0; "
                      "Tart 2.36.0 answers", rollback["detail"])
        self.assertEqual("on", self.sys.pool_state)
        self.assertIsNone(self.verified())

    def test_an_empty_ip_is_not_a_boot(self) -> None:
        self.sys.boot_ip = "\n"
        self.assertUpdated(self.apply())
        self.assertIn("did not report an IP", self.steps("tart-upgrade")[0]["detail"])
        self.assertEqual("2.36.0", self.sys.tart)

    def test_an_upgrade_that_lands_another_version_is_rolled_back(self) -> None:
        self.sys.brew_upgrade_installs = "2.38.0"
        self.assertUpdated(self.apply())
        self.assertIn("brew installed Tart 2.38.0, not 2.40.1", self.steps("tart-upgrade")[0]["detail"])
        self.assertEqual("2.36.0", self.sys.tart)
        self.assertTrue(self.steps("tart-rollback")[0]["ok"])

    def test_a_softnet_left_on_another_version_is_rolled_back(self) -> None:
        self.sys.tap["softnet"] = ("0.24.0", SOFTNET_SHA)
        original = self.sys.set_softnet
        self.sys.set_softnet = lambda version: original("0.23.5")
        self.assertUpdated(self.apply())
        self.assertIn("Softnet links ../Cellar/softnet/0.23.5, not 0.24.0",
                      self.steps("tart-upgrade")[0]["detail"])
        self.assertEqual("2.36.0", self.sys.tart)

    def test_a_failed_brew_upgrade_never_stops_the_update(self) -> None:
        self.sys.brew_upgrade_rc = 1
        self.assertUpdated(self.apply())
        self.assertEqual("2.36.0", self.sys.tart)
        self.assertIn("brew upgrade exit 1", self.steps("tart-upgrade")[0]["detail"])
        self.assertIn("nothing was relinked; Tart 2.36.0 answers",
                      self.steps("tart-rollback")[0]["detail"])
        self.assertEqual([], self.calls(tart_pin.BREW, "uninstall"),
                         "the previous keg is never uninstalled")

    def test_a_rollback_that_fails_leaves_the_host_off_and_counts(self) -> None:
        self.sys.boot_ok = False
        self.sys.uninstall_rc = 1
        self.assertEqual(su.EXIT_FAILED, self.apply())
        last = self.last()
        self.assertEqual("failed", last["status"])
        self.assertIs(True, last["host_off"])
        self.assertIn("Tart rollback failed", last["error"])
        self.assertNotEqual("on", self.sys.pool_state)
        self.assertFalse(self.steps("tart-rollback")[0]["ok"])
        self.assertFalse(any("install" in a and "--apply" in a for a, _ in self.sys.calls),
                         "nothing is installed after a broken Tart")

    def test_a_rollback_whose_tart_does_not_answer_leaves_the_host_off(self) -> None:
        self.sys.boot_ok = False
        original = self.sys.run

        def silent_after_rollback(argv, **kw):
            if argv[:2] == [tart_pin.TART, "--version"] and self.calls(tart_pin.BREW, "link"):
                return su.Result(127, "", "dyld: Library not loaded")
            return original(argv, **kw)
        self.sys.run = silent_after_rollback
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertIn("Tart is NOT runnable now, expected 2.36.0", self.last()["error"])
        self.assertIs(True, self.last()["host_off"])

    def test_a_failed_pin_is_not_retried_until_the_pin_moves_or_is_cleared(self) -> None:
        self.sys.boot_ok = False
        self.assertUpdated(self.apply())
        self.assertEqual({"version": "2.40.1", "sha256": TART_SHA},
                         {k: self.failed()[k] for k in ("version", "sha256")})
        self.sys.boot_ok = True
        self.assertIn("failed here at", self.decide().refusal)
        self.sys.log_lines = ""
        self.assertEqual(su.EXIT_NOTHING, self.apply(), "no drain every 6 h for a failed pin")
        self.assertIs(True, self.gate()["due"], "a stuck pin past its gate stays a problem")
        (self.cfg.checkout / "fleet" / "tart.toml").write_text(
            self.PIN.replace(TART_SHA, "d" * 64))
        self.sys.tap["tart"] = ("2.40.1", "d" * 64)
        self.assertIsNone(self.decide().refusal, "a moved pin is tried")
        (self.cfg.checkout / "fleet" / "tart.toml").write_text(self.PIN)
        self.sys.tap["tart"] = ("2.40.1", TART_SHA)
        (self.cfg.state_dir / tart_pin.FAILED).unlink()
        self.assertIsNone(self.decide().refusal, "an operator clears it by removing the record")

    def pool_ons(self) -> list[int]:
        return [i for i, (a, _) in enumerate(self.sys.calls)
                if a[-2:] == ["pool", "on"] or a[1:3] == ["pool", "on"]
                or (a[-3:-1] == ["pool", "on"])]

    def test_sigterm_mid_upgrade_holds_the_host_off_without_a_slow_rollback(self) -> None:
        import signal

        def term_during_boot(argv):
            if argv[:2] == [tart_pin.TART, "ip"]:
                self.sys.hook = None
                os.kill(os.getpid(), signal.SIGTERM)
        self.sys.hook = term_during_boot
        before = len(self.sys.calls)
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertIs(True, self.last()["host_off"])
        self.assertIn("interrupted mid Tart upgrade, not rolled back", self.last()["error"])
        self.assertEqual([], self.calls(tart_pin.BREW, "uninstall"), "no brew under a SIGTERM")
        on_after = [i for i in self.pool_ons() if i >= before
                    and self.index_of(lambda a: a[:2] == [tart_pin.TART, "ip"]) < i]
        self.assertEqual([], on_after, "no pool on after the SIGTERM")
        self.assertTrue((self.cfg.state_dir / tart_pin.HOLD).is_file())
        self.assertFalse((self.cfg.state_dir / tart_pin.PENDING).exists(),
                         "the hold takes over from the pending record")

    def held_off(self) -> None:
        """A TartBroken stop: the rollback's uninstall fails."""
        self.sys.boot_ok = False
        self.sys.uninstall_rc = 1
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertIs(True, self.last()["host_off"])
        self.sys.pool_state = "off"

    def test_a_held_host_is_not_turned_on_by_the_next_self_update(self) -> None:
        self.held_off()
        ons = len(self.pool_ons())
        outcome = su.recover_left_off(self.cfg, self.sys)
        self.assertIs(False, outcome["attempted"])
        self.assertIn("held OFF on purpose", outcome["reason"])
        self.assertEqual(ons, len(self.pool_ons()))
        self.assertEqual("off", self.sys.pool_state)

    def test_a_held_host_is_not_turned_on_by_the_watchdog_and_keeps_alerting(self) -> None:
        import host_off
        self.held_off()
        calls: list[str] = []
        pool = host_off.pool_state_file(self.home)
        pool.parent.mkdir(parents=True, exist_ok=True)
        pool.write_text("off\n")
        os.utime(pool, (NOW - 3600, NOW - 3600))
        outcome = host_off.recover(self.cfg.state_dir, pool, lambda: calls.append("on") or (0, ""),
                                   now=NOW + 600, who="launchd-watchdog")
        self.assertEqual([], calls)
        self.assertIs(False, outcome["attempted"])
        current = host_off.status(self.cfg.state_dir, pool, NOW + 600)
        self.assertIs(True, current["unexpected"], "it still alerts")
        self.assertIn("HELD OFF on purpose", current["detail"])
        import tartci_launchd_watchdog as watchdog
        with mock.patch.object(watchdog, "_pool_on", lambda: calls.append("on") or (0, "")), \
                mock.patch.object(host_off, "state_dir", lambda home=None: self.cfg.state_dir), \
                mock.patch.object(host_off, "pool_state_file", lambda home=None: pool), \
                mock.patch.object(host_off, "alert", lambda *a, **k: None):
            line = watchdog.host_off_pass(now=NOW + 900)
        self.assertEqual([], calls, "host_off_pass never turns a held host on")
        self.assertIn("WARN", line)

    def test_a_held_host_refuses_every_update_until_tart_is_fixed(self) -> None:
        self.held_off()
        self.sys.boot_ok, self.sys.uninstall_rc = True, 0
        self.sys.clock += (self.cfg.rate_hours + 1) * 3600   # past the same-target guard
        installs = len([a for a, _ in self.sys.calls if "install" in a and "--apply" in a])
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual("off", self.sys.pool_state)
        self.assertEqual(installs, len([a for a, _ in self.sys.calls
                                        if "install" in a and "--apply" in a]))
        self.assertIs(True, self.last()["host_off"], "nothing cleared the OFF record")
        receipts = sorted((self.cfg.state_dir / "attempts").glob("*.json"))
        error = json.loads(receipts[-1].read_text())["error"]
        self.assertIn("host HELD OFF for Tart", error)
        self.assertIn("brew uninstall --ignore-dependencies openai/tools/tart", error)

    def test_a_hold_from_an_earlier_episode_is_ignored_and_removed(self) -> None:
        import host_off
        self.held_off()
        # Months later an unrelated update leaves the host OFF: a new episode.
        last = self.last()
        su._write_json(self.cfg.state_dir / "last.json",
                       dict(last, at="2027-06-01T00:00:00Z", error="pool on failed"))
        self.assertIsNone(host_off.held(self.cfg.state_dir))
        calls: list[str] = []
        pool = host_off.pool_state_file(self.home)
        pool.parent.mkdir(parents=True, exist_ok=True)
        pool.write_text("off\n")
        os.utime(pool, (NOW - 3600, NOW - 3600))
        outcome = host_off.recover(self.cfg.state_dir, pool, lambda: calls.append("on") or (0, ""),
                                   now=su._ts("2027-06-01T00:10:00Z"),
                                   who="launchd-watchdog")
        self.assertEqual(["on"], calls, "the unrelated OFF is recovered")
        self.assertTrue(outcome["attempted"])
        self.assertIsNone(su.tart_hold_refusal(self.cfg, self.sys))
        self.assertFalse((self.cfg.state_dir / tart_pin.HOLD).exists())

    def test_a_hold_is_cleared_once_the_pool_is_on_with_a_working_tart(self) -> None:
        self.held_off()
        self.assertIsNotNone(su.tart_hold_refusal(self.cfg, self.sys))
        # The operator relinked 2.36.0 by hand and turned the pool on.
        self.sys.kegs["tart"].discard("2.40.1")
        self.sys.set_tart("2.36.0")
        self.sys.pool_state = "on"
        self.assertIsNone(su.tart_hold_refusal(self.cfg, self.sys))
        self.assertFalse((self.cfg.state_dir / tart_pin.HOLD).exists())

    def test_a_hold_stays_while_the_pool_is_off_whatever_tart_answers(self) -> None:
        self.held_off()
        self.sys.kegs["tart"].discard("2.40.1")
        self.sys.set_tart("2.36.0")          # Tart fixed by hand, pool not on yet
        self.assertEqual("off", self.sys.pool_state)
        self.assertIsNotNone(su.tart_hold_refusal(self.cfg, self.sys))
        self.assertTrue((self.cfg.state_dir / tart_pin.HOLD).exists())

    def test_a_hold_stays_while_the_pool_is_on_with_the_failed_tart(self) -> None:
        self.held_off()
        self.sys.pool_state = "on"           # someone forced the pool on
        self.assertEqual("2.40.1", self.sys.tart)
        self.assertIsNotNone(su.tart_hold_refusal(self.cfg, self.sys))
        self.assertTrue((self.cfg.state_dir / tart_pin.HOLD).exists())

    def test_a_run_killed_mid_tart_upgrade_is_held_off_by_the_next_run(self) -> None:
        def kill_during_boot(argv):
            if argv[:2] == [tart_pin.TART, "ip"]:
                raise SystemExit("SIGKILL stand-in")
        self.sys.hook = kill_during_boot
        with self.assertRaises(SystemExit):
            self.apply()
        self.sys.hook = None
        self.assertTrue((self.cfg.state_dir / tart_pin.PENDING).is_file())
        receipts = sorted((self.cfg.state_dir / "attempts").glob("*.json"))
        value = json.loads(receipts[-1].read_text())
        value.update(status="running", pid=999999)
        receipts[-1].write_text(json.dumps(value))
        ons = len(self.pool_ons())
        su.recover_interrupted(self.cfg, self.sys, [receipts[-1]])
        self.assertEqual(ons, len(self.pool_ons()), "no pool on for a killed Tart upgrade")
        self.assertIs(True, self.last()["host_off"])
        self.assertIn("HOST HELD OFF: killed mid Tart upgrade", self.last()["error"])
        self.assertTrue((self.cfg.state_dir / tart_pin.HOLD).is_file())
        self.assertFalse((self.cfg.state_dir / tart_pin.PENDING).exists())

    def test_the_recovery_text_removes_the_failed_keg_before_linking(self) -> None:
        self.held_off()
        error = self.last()["error"]
        self.assertLess(error.index("brew uninstall --ignore-dependencies openai/tools/tart"),
                        error.index("brew link --overwrite openai/tools/tart"))
        self.assertIn("tart-broken.json", error)

    def test_brew_never_autoremoves_in_a_rollback(self) -> None:
        self.sys.boot_ok = False
        self.assertUpdated(self.apply())
        index = self.index_of(lambda a: a[:2] == [tart_pin.BREW, "uninstall"])
        self.assertEqual("1", self.sys.env_calls[index]["HOMEBREW_NO_AUTOREMOVE"])

    def test_a_link_that_lands_on_another_keg_is_a_failed_rollback(self) -> None:
        # A stray newer keg in the Cellar: `brew link` picks it, not 2.36.0.
        self.sys.kegs["tart"].add("2.39.0")
        self.sys.boot_ok = False
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertIn("tart links ../Cellar/tart/2.39.0 after the rollback, not "
                      "../Cellar/tart/2.36.0", self.last()["error"])

    def test_a_failed_canary_boot_check_is_remembered(self) -> None:
        self.sys.log_lines = ""
        self.sys.set_tart("2.40.1")
        self.sys.boot_ok = False
        self.assertEqual(su.EXIT_OK, self.apply(), self.last())
        self.assertFalse(self.steps("tart-upgrade")[0]["ok"])
        self.assertEqual("2.40.1", self.failed()["version"])
        self.assertIsNone(self.verified())

    def test_the_canary_s_own_record_must_match_the_pin_s_sha(self) -> None:
        self.sys.set_tart("2.40.1")
        su._write_json(self.cfg.state_dir / tart_pin.VERIFIED,
                       {"version": "2.40.1", "sha256": "c" * 64, "verified_at": NOW - 99999})
        self.assertTrue(self.decide().prove_only)
        su._write_json(self.cfg.state_dir / tart_pin.VERIFIED,
                       {"version": "2.40.1", "sha256": TART_SHA, "verified_at": NOW - 99999})
        self.assertIsNone(self.decide())

    def test_a_host_without_a_golden_refuses_before_the_drain(self) -> None:
        self.sys.log_lines = ""
        with mock.patch.object(su, "tart_boot_target", return_value=None):
            self.assertIn("names no golden", self.decide().refusal)
            self.assertEqual(su.EXIT_NOTHING, self.apply())
        self.assertEqual([], self.sys.mutations())

    def test_a_vm_left_after_pool_off_defers_the_pin_instead_of_draining_again(self) -> None:
        self.sys.tart_running = True
        self.assertUpdated(self.apply())
        self.assertIn("deferred for 24 h", self.steps("tart-upgrade")[0]["detail"])
        self.sys.tart_running = False
        self.assertIn("deferred until", self.decide().refusal)
        self.sys.log_lines = ""
        self.assertEqual(su.EXIT_NOTHING, self.apply())
        self.sys.clock += tart_pin.DEFER_SECONDS
        (self.cfg.state_dir / tart_pin.TAP_CACHE).unlink(missing_ok=True)
        self.assertIsNone(self.decide().refusal, "tried again after the deferral")

    def test_refresh_reads_the_installed_generation_s_pin(self) -> None:
        self.sys.log_lines = ""
        (self.cfg.checkout / "fleet" / "tart.toml").write_text(self.PIN.replace("2.40.1", "9.9.9"))
        self.assertIn("upgrade Tart 2.36.0 -> 2.40.1", str(su.refresh_reason(self.cfg, self.sys)))

    def test_a_running_tart_vm_is_never_replaced(self) -> None:
        self.sys.tart_running = True
        self.assertUpdated(self.apply())
        self.assertEqual([], self.upgrades())
        self.assertIn("a Tart VM process was still running after pool off",
                      self.steps("tart-upgrade")[0]["detail"])

    def test_the_vm_process_pattern_matches_tart_itself_only(self) -> None:
        import re
        pattern = re.compile(tart_pin.TART_RUN_PATTERN)
        for line in ("/opt/homebrew/Cellar/tart/2.38.0/tart.app/Contents/MacOS/tart run --no-graphics x",
                     "tart run x"):
            self.assertTrue(pattern.search(line), line)
        for line in ("bash -c echo tart run", "/usr/bin/grep tart run", "vim notes-on-tart run"):
            self.assertFalse(pattern.search(line), line)

    def test_a_clone_that_cannot_be_deleted_fails_the_tart_step(self) -> None:
        # A good boot whose clone survives three stop/delete tries must not
        # end in a quiet pool on beside it: it is a failed Tart step.
        self.sys.tart_delete_rc = 1
        self.assertUpdated(self.apply())
        cleanup = self.steps("tart-check-cleanup")[0]
        self.assertFalse(cleanup["ok"])
        self.assertIn("still there after 3 tries; the watchdog deletes it after", cleanup["detail"])
        self.assertEqual(3, len(self.calls(tart_pin.TART, "delete")))
        failed = self.steps("tart-upgrade")[0]
        self.assertFalse(failed["ok"])
        self.assertIn("could not be removed", failed["detail"])
        self.assertEqual("2.36.0", self.sys.tart, "rolled back")
        self.assertEqual("2.40.1", self.failed()["version"])
        self.assertIsNone(self.verified())
        import scratch_vm_ttl
        names = [r["name"] for r in scratch_vm_ttl.records()]
        self.assertEqual([f"{tart_pin.CHECK_PREFIX}{os.getpid()}"], names)
        self.assertTrue(self.calls(tart_pin.TART, "stop"), "the clone is stopped")

    def test_a_cleanup_that_succeeds_on_a_retry_is_fine(self) -> None:
        self.sys.tart_delete_rcs = [1]
        self.assertUpdated(self.apply())
        self.assertEqual(2, len(self.calls(tart_pin.TART, "delete")))
        self.assertEqual([], self.steps("tart-check-cleanup"))
        self.assertTrue(self.steps("tart-upgrade")[0]["ok"])
        self.assertEqual("2.40.1", self.sys.tart)

    # ── the pinned bytes ────────────────────────────────────────────────

    def test_the_downloaded_archives_are_checked_against_the_pin(self) -> None:
        self.assertUpdated(self.apply())
        hashed = [a[-1] for a in self.calls("/usr/bin/shasum", "-a", "256")]
        self.assertEqual(2, len(hashed), "Tart and the Softnet that moved with it")
        self.assertTrue(self.steps("tart-upgrade")[0]["ok"])

    def test_a_re_cut_archive_under_the_pinned_version_is_rolled_back(self) -> None:
        self.sys.archive_sha["tart"] = "e" * 64
        self.assertUpdated(self.apply())
        detail = self.steps("tart-upgrade")[0]["detail"]
        self.assertIn("the downloaded openai/tools/tart archive", detail)
        self.assertIn("has sha256 eeeeeeeeeeee, the pin is aaaaaaaaaaaa", detail)
        self.assertEqual("2.36.0", self.sys.tart)
        self.assertTrue(self.steps("tart-rollback")[0]["ok"])
        self.assertEqual([], self.calls(tart_pin.TART, "clone"), "no boot of unpinned bytes")

    def test_a_re_cut_softnet_archive_is_rolled_back(self) -> None:
        self.sys.archive_sha["softnet"] = "f" * 64
        self.assertUpdated(self.apply())
        self.assertIn("openai/tools/softnet archive", self.steps("tart-upgrade")[0]["detail"])
        self.assertEqual(("2.36.0", "0.23.0"), (self.sys.tart, self.sys.softnet))

    def test_a_stale_cached_tap_is_reread_before_the_upgrade(self) -> None:
        self.assertIsNone(self.decide().refusal)       # caches the matching answer
        self.sys.tap["tart"] = ("2.40.1", "c" * 64)    # the tap re-cut the release since
        self.assertIsNone(self.decide().refusal, "a plan may use the cached answer")
        self.assertUpdated(self.apply())
        self.assertEqual([], self.upgrades())
        self.assertIn("the tap offers tart 2.40.1 (cccccccccccc)",
                      self.steps("tart-upgrade")[0]["detail"])

    def test_a_stale_cached_tap_never_starts_a_tart_only_drain(self) -> None:
        self.assertIsNone(self.decide().refusal)
        self.sys.tap["tart"] = ("2.40.1", "c" * 64)
        self.sys.log_lines = ""
        self.assertEqual(su.EXIT_NOTHING, self.apply())
        self.assertEqual([], self.sys.mutations())

    # ── turns of their own ──────────────────────────────────────────────

    def test_a_current_tartci_takes_a_drained_turn_for_the_pinned_tart(self) -> None:
        self.sys.log_lines = ""   # TartCI current with main
        self.assertEqual(su.EXIT_OK, self.apply(), self.last())
        self.assertEqual(INSTALLED, self.last()["target"])
        self.assertIn("upgrade Tart 2.36.0 -> 2.40.1", str(self.receipt().get("refresh")))
        self.assertEqual("2.40.1", self.sys.tart)
        self.assertEqual("on", self.sys.pool_state)

    def test_a_host_waiting_for_the_canary_does_not_drain_for_tart(self) -> None:
        self.sys.log_lines = ""
        self.cfg.tart_canary = "studio"
        self.assertEqual(su.EXIT_NOTHING, self.apply())
        self.assertEqual([], self.sys.mutations())

    def test_an_unreadable_tap_never_starts_a_drain(self) -> None:
        self.sys.log_lines = ""
        self.sys.tap_ok = False
        self.assertEqual(su.EXIT_NOTHING, self.apply())
        self.assertEqual([], self.sys.mutations())

    def test_a_canary_already_on_the_pin_boot_checks_it_once_and_publishes(self) -> None:
        self.sys.log_lines = ""
        self.sys.set_tart("2.40.1")
        self.assertEqual(su.EXIT_OK, self.apply(), self.last())
        self.assertEqual([], self.upgrades())
        self.assertIn("canary boot check of Tart 2.40.1: booted",
                      self.steps("tart-upgrade")[0]["detail"])
        self.assertEqual("2.40.1", self.verified()["version"])
        self.assertEqual(su.EXIT_NOTHING, self.apply(), "proven once, then nothing to do")

    def test_the_plan_names_the_tart_upgrade(self) -> None:
        self.assertEqual(su.EXIT_OK, self.plan())
        attempts = sorted((self.cfg.state_dir / "attempts").glob("*.json"))
        plan = json.loads(attempts[-1].read_text())
        self.assertIn("upgrade Tart 2.36.0 -> 2.40.1 (boot-checked)", plan["steps"][-1]["detail"])
        self.assertEqual([], self.upgrades())

    def test_tart_settings_are_validated(self) -> None:
        settings = self.home / "self-update.toml"
        settings.write_text('[tart]\nupgrade = "yes"\n')
        with self.assertRaises(su.ConfigError):
            su.load_config(self.home, settings)
        settings.write_text('[tart]\nupgrade = true\ncanary = "studio"\nsoak_seconds = 60\n')
        cfg = su.load_config(self.home, settings)
        self.assertEqual((True, "studio", 60), (cfg.tart_upgrade, cfg.tart_canary,
                                                cfg.tart_soak_seconds))
        self.assertFalse(su.load_config(self.home, self.home / "absent.toml").tart_upgrade)

    def test_status_says_when_tart_is_behind_its_pin(self) -> None:
        state = self.cfg.state_dir.parent / "tool-freshness" / "state.json"
        su._write_json(state, {"tools": {"tart": {"state": "behind", "installed": "2.36.0",
                                                  "latest_tag": "2.40.1", "stale": True}}})
        self.assertIn("self-update: Tart 2.36.0 is behind its pin 2.40.1 (fleet/tart.toml); with "
                      "[tart] upgrade = true a drained self-update moves it (STALE)",
                      su.status_lines(self.cfg.state_dir))
        su._write_json(state, {"tools": {"tart": {"state": "current"}}})
        self.assertFalse(any("Tart" in line for line in su.status_lines(self.cfg.state_dir)))


class PrivateSourceSystem(FakeSystem):
    """FakeSystem plus GitHub API reads of one private fleet repository."""

    def __init__(self, home: Path, repository) -> None:
        super().__init__(home)
        self.private = repository

    def run(self, argv, *, cwd=None, env=None, timeout=900):
        if argv[:2] == [FAKE_GH, "api"] and argv[2].startswith(f"repos/{self.private.repository}/"):
            self.calls.append((list(argv), cwd))
            return su.Result(*self.private(argv[2]))
        return super().run(argv, cwd=cwd, env=env, timeout=timeout)

    def _set_installed(self, commit):
        # The installer records the selected source in its receipt.
        super()._set_installed(commit)
        selected = os.environ.get("TARTCI_FLEET_SOURCE_COMMIT")
        if getattr(self, "records_source", True) is False:
            selected = None
        self.installed_sources = getattr(self, "installed_sources", []) + [selected]
        receipt_path = self.home / ".config/tartci/macos-fleet-install.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["fleet_source"] = {
            "configured": bool(selected),
            "commit": selected,
            "repository": os.environ.get("TARTCI_FLEET_SOURCE_REPOSITORY") if selected else None,
            "root": os.environ.get("TARTCI_FLEET_SOURCE_ROOT") if selected else None,
        }
        receipt_path.write_text(json.dumps(receipt))


class TwoCommits:
    """One private repository at two commits: `head` is main, `older` is not."""

    def __init__(self, head, older) -> None:
        self.head, self.older = head, older
        self.repository = head.repository
        self.calls: list[str] = []

    @property
    def unreadable(self) -> bool:
        return self.head.unreadable

    @unreadable.setter
    def unreadable(self, value: bool) -> None:
        self.head.unreadable = self.older.unreadable = value

    @property
    def corrupt(self) -> set:
        return self.head.corrupt

    def __call__(self, path: str):
        self.calls.append(path)
        if path.endswith("/commits/main"):
            return self.head(path)
        for repo in (self.head, self.older):
            rc, out, err = repo(path)
            if rc == 0:
                return rc, out, err
        return 1, "", f"HTTP 404: {path}"


class FleetSourceFixture(Base):
    """A private fleet source served by a fake GitHub; no tests of its own."""
    OLDER = "a1" * 20      # a private-source commit installed before main moved

    def setUp(self) -> None:
        super().setUp()
        import test_fleet_source_sync as fixture
        self.fixture = fixture
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in su.FLEET_SOURCE_ENV:
            os.environ.pop(name, None)
        profiles = {path.name: path.read_text()
                    for path in sorted((ROOT / "profiles").glob("*-macos-fleet.toml"))}
        files = fixture.source_files(profiles)
        self.repo = TwoCommits(fixture.FakeRepository(files),
                               fixture.FakeRepository(files, commit=self.OLDER, tree="b1" * 20))
        self.sys = PrivateSourceSystem(self.home, self.repo)
        self.cfg.fleet_source_repository = fixture.PRIVATE
        self.cfg.fleet_source_canary = "m1"     # this host


class FleetSourceSyncTests(FleetSourceFixture):

    def test_settings_name_the_private_repository(self) -> None:
        self.cfg.settings.write_text('[fleet_source]\nrepository = "ORG/private-fleet-authority"\n')
        self.assertEqual("ORG/private-fleet-authority",
                         su.load_config(self.home).fleet_source_repository)
        self.cfg.settings.unlink()
        self.assertIsNone(su.load_config(self.home).fleet_source_repository)

    def test_a_malformed_setting_is_a_clean_refusal(self) -> None:
        for body in ('fleet_source = "ORG/x"\n', '[fleet_source]\nrepository = 3\n',
                     '[fleet_source]\nrepo = "ORG/x"\n', '[fleet_source]\ncanary = ""\n'):
            with self.subTest(body=body):
                self.cfg.settings.write_text(body)
                with self.assertRaisesRegex(su.ConfigError, r"\[fleet_source\]"):
                    su.load_config(self.home)
                out = io.StringIO()
                with contextlib.redirect_stderr(out):
                    self.assertEqual(su.EXIT_REFUSED, su.main(["--home", str(self.home)]))
                self.assertIn("self-update: REFUSED", out.getvalue())
        self.cfg.settings.write_text('[fleet_source]\nrepository = "ORG/x"\ncanary = "m1"\n')
        loaded = su.load_config(self.home)
        self.assertEqual(("ORG/x", "m1"), (loaded.fleet_source_repository, loaded.fleet_source_canary))

    def test_pruning_keeps_the_commit_the_running_generation_reads(self) -> None:
        receipt = json.loads(self.cfg.install_receipt.read_text())
        receipt["fleet_source"] = {"configured": True, "commit": self.OLDER}
        self.cfg.install_receipt.write_text(json.dumps(receipt))
        store = self.cfg.fleet_source_store
        (store / self.OLDER).mkdir(parents=True)
        os.utime(store / self.OLDER, (1, 1))                  # the oldest export
        newer = [f"{i}" * 40 for i in range(2, 7)]
        for i, commit in enumerate(newer):
            (store / commit).mkdir()
            os.utime(store / commit, (NOW - 100 + i, NOW - 100 + i))
        su.sync_fleet_source(self.cfg, self.sys)
        self.assertTrue((store / self.OLDER).is_dir(), "the installed generation's export was pruned")
        self.assertTrue((store / self.fixture.COMMIT).is_dir())
        # Control: the same store with nothing installed from it loses that export.
        receipt["fleet_source"] = {"configured": False, "commit": None}
        self.cfg.install_receipt.write_text(json.dumps(receipt))
        su.sync_fleet_source(self.cfg, self.sys)
        self.assertFalse((store / self.OLDER).exists())

    def test_a_plan_writes_and_prunes_nothing(self) -> None:
        store = self.cfg.fleet_source_store
        with self.assertRaisesRegex(su.Refused, "not exported yet"):
            su.sync_fleet_source(self.cfg, self.sys, write=False)
        self.assertFalse(store.exists())
        self.assertEqual(su.EXIT_UNKNOWN, self.plan())
        self.assertFalse(store.exists())
        root = Path(su.sync_fleet_source(self.cfg, self.sys)["root"])
        stale = [f"{i}" * 40 for i in range(2, 7)]
        for commit in stale:
            (store / commit).mkdir()
            os.utime(store / commit, (1, 1))
        before = {p.name: p.stat().st_mtime for p in store.iterdir()}
        for name in su.FLEET_SOURCE_ENV:
            os.environ.pop(name, None)
        selected = su.sync_fleet_source(self.cfg, self.sys, write=False)
        self.assertEqual(str(root), selected["root"])
        self.assertEqual(before, {p.name: p.stat().st_mtime for p in store.iterdir()})

    def test_without_the_setting_nothing_is_read_or_selected(self) -> None:
        self.cfg.fleet_source_repository = None
        self.assertIsNone(su.sync_fleet_source(self.cfg, self.sys))
        self.assertEqual([], self.repo.calls)
        self.assertNotIn("TARTCI_FLEET_SOURCE_ROOT", os.environ)

    def test_the_configured_source_is_exported_and_selected(self) -> None:
        selected = su.sync_fleet_source(self.cfg, self.sys)
        root = self.cfg.fleet_source_store / self.fixture.COMMIT
        source = fleet_source.FleetSource(
            root, root / "profiles", root / fleet_source.PUBLISHED_SUPPLY, True)
        self.assertEqual({"repository": self.fixture.PRIVATE, "commit": self.fixture.COMMIT,
                          "root": str(root), "why": "canary: main's head",
                          **source.provenance}, selected)
        self.assertEqual(str(root), os.environ["TARTCI_FLEET_SOURCE_ROOT"])
        self.assertEqual(self.fixture.COMMIT, os.environ["TARTCI_FLEET_SOURCE_COMMIT"])
        self.assertEqual(self.fixture.PRIVATE, os.environ["TARTCI_FLEET_SOURCE_REPOSITORY"])
        # The profile self-update installs now comes from the private export.
        self.assertEqual(root.resolve() / "profiles" / "m1-macos-fleet.toml",
                         su.checked_in_profile(self.cfg))
        # And the installer it runs inherits the selection.
        self.assertEqual(self.fixture.COMMIT, su.census_env()["TARTCI_FLEET_SOURCE_COMMIT"])

    def test_an_unreadable_source_refuses_and_never_falls_back(self) -> None:
        self.repo.unreadable = True
        self.assertEqual(su.EXIT_UNKNOWN, self.plan())
        skew = json.loads((self.cfg.state_dir / "skew.json").read_text())
        self.assertEqual("unknown", skew["state"])
        self.assertIn("fleet source ORG/private-fleet-authority", skew["reason"])
        self.assertNotIn("TARTCI_FLEET_SOURCE_ROOT", os.environ)
        self.assertEqual([], self.sys.mutations())

    def test_a_tampered_source_refuses(self) -> None:
        self.repo.corrupt.add(fleet_source_supply())
        with self.assertRaisesRegex(su.Refused, "does not match blob"):
            su.sync_fleet_source(self.cfg, self.sys)

    def test_a_moved_source_is_a_refresh_reason(self) -> None:
        su.sync_fleet_source(self.cfg, self.sys)
        receipt = {"support": {"source_commit": INSTALLED}}
        self.cfg.install_receipt.write_text(json.dumps(receipt))
        self.assertIn("fleet source moved to cccccccccccc (installed from the public profiles)",
                      su.refresh_reason(self.cfg, self.sys))
        receipt["fleet_source"] = {"configured": True, "commit": "9" * 40}
        self.cfg.install_receipt.write_text(json.dumps(receipt))
        self.assertIn("installed from 999999999999", su.refresh_reason(self.cfg, self.sys))
        receipt["fleet_source"]["commit"] = self.fixture.COMMIT
        self.cfg.install_receipt.write_text(json.dumps(receipt))
        self.assertIsNone(su.refresh_reason(self.cfg, self.sys))

    def test_a_source_move_ignores_the_previous_generation_rate_limit(self) -> None:
        # A source-only refresh keeps the TartCI generation unchanged. A recent
        # attempt of that generation must not strand the new private profiles.
        attempts = self.cfg.state_dir / "attempts"
        attempts.mkdir(parents=True, exist_ok=True)
        (attempts / "recent.json").write_text(json.dumps({
            "mode": "apply", "target": INSTALLED, "status": "succeeded",
            "started_at": su._iso(NOW - 60),
            "fleet_source": {"repository": self.fixture.PRIVATE,
                             "commit": self.OLDER},
        }))
        self.sys.log_lines = ""
        self.assertEqual(su.EXIT_OK, self.apply(), self.last())
        self.assertEqual([self.fixture.COMMIT], self.sys.installed_sources)

    def test_a_current_host_reinstalls_from_a_moved_source(self) -> None:
        self.sys.log_lines = ""   # current with main
        self.assertEqual(su.EXIT_OK, self.apply(), self.last())
        self.assertEqual(INSTALLED, self.last()["target"])
        self.assertEqual([self.fixture.COMMIT], self.sys.installed_sources)
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        self.assertEqual(self.fixture.COMMIT, receipt["fleet_source"]["commit"])
        # Installed from that commit, the next run has nothing to do.
        self.assertIsNone(su.refresh_reason(self.cfg, self.sys))
        self.assertEqual(su.EXIT_NOTHING, self.apply())


class FleetSourceCanaryTests(FleetSourceFixture):
    """A private-source move is one host first: the canary installs and verifies
    it; every other host adopts only that commit, after it has served there."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.fleet_source_canary = "studio"   # another host

    def installed_from(self, commit: str | None) -> None:
        receipt = json.loads(self.cfg.install_receipt.read_text())
        receipt["fleet_source"] = {
            "configured": commit is not None, "commit": commit,
            "repository": self.fixture.PRIVATE if commit else None,
            "root": str(self.cfg.fleet_source_store / commit) if commit else None,
        }
        self.cfg.install_receipt.write_text(json.dumps(receipt))

    def canary(self, record: dict | None):
        return mock.patch.object(su, "read_canary_verified", return_value=record)

    def test_a_mistyped_canary_refuses_loudly(self) -> None:
        self.cfg.fleet_source_canary = "stuido"
        with self.canary(None), self.assertRaisesRegex(
                su.Refused, r"canary 'stuido' is not a published host id \(.*studio.*\)"):
            su.sync_fleet_source(self.cfg, self.sys)
        self.assertEqual([], self.repo.calls)
        # The same mistake reaches the run's status: UNKNOWN, with the reason.
        self.assertEqual(su.EXIT_UNKNOWN, self.plan())
        skew = json.loads((self.cfg.state_dir / "skew.json").read_text())
        self.assertIn("is not a published host id", skew["reason"])

    def test_without_a_canary_the_setting_refuses(self) -> None:
        self.cfg.fleet_source_canary = None
        with self.assertRaisesRegex(su.Refused, "no canary"):
            su.sync_fleet_source(self.cfg, self.sys)
        self.assertEqual([], self.repo.calls)

    def test_a_host_stays_public_until_the_canary_has_verified_a_commit(self) -> None:
        with self.canary(None):
            self.assertIsNone(su.sync_fleet_source(self.cfg, self.sys))
        self.assertEqual([], self.repo.calls)
        self.assertNotIn("TARTCI_FLEET_SOURCE_ROOT", os.environ)

    def test_a_host_stays_on_its_installed_commit_while_the_canary_soaks(self) -> None:
        self.installed_from(self.OLDER)
        young = {"repository": self.fixture.PRIVATE, "commit": self.fixture.COMMIT,
                 "age": su.FLEET_SOURCE_SOAK_SECONDS - 60}
        with self.canary(young):
            selected = su.sync_fleet_source(self.cfg, self.sys)
        self.assertEqual(self.OLDER, selected["commit"])
        self.assertIn("only 29 min ago", selected["why"])
        self.assertIsNone(su.refresh_reason(self.cfg, self.sys))
        self.assertFalse(any(call.endswith("/commits/main") for call in self.repo.calls))

    def test_a_host_adopts_the_canary_commit_after_the_soak(self) -> None:
        self.installed_from(self.OLDER)
        soaked = {"repository": self.fixture.PRIVATE, "commit": self.fixture.COMMIT,
                  "age": su.FLEET_SOURCE_SOAK_SECONDS}
        with self.canary(soaked):
            selected = su.sync_fleet_source(self.cfg, self.sys)
        self.assertEqual(self.fixture.COMMIT, selected["commit"])
        self.assertIn("fleet source moved to cccccccccccc", su.refresh_reason(self.cfg, self.sys))

    def test_a_malformed_installed_source_receipt_refuses_before_source_sync(self) -> None:
        receipt = json.loads(self.cfg.install_receipt.read_text())
        receipt["fleet_source"] = {"configured": True, "commit": self.OLDER}
        self.cfg.install_receipt.write_text(json.dumps(receipt))
        self.assertEqual(su.EXIT_UNKNOWN, self.apply())
        self.assertEqual([], self.repo.calls)
        self.assertEqual([], self.sys.mutations())
        self.assertFalse(self.cfg.fleet_source_store.exists())
        self.assertNotIn("TARTCI_FLEET_SOURCE_ROOT", os.environ)
        self.assertIn("lacks the source root and repository",
                      json.loads((self.cfg.state_dir / "skew.json").read_text())["reason"])

    def test_a_non_boolean_configured_marker_also_refuses_before_source_sync(self) -> None:
        receipt = json.loads(self.cfg.install_receipt.read_text())
        receipt["fleet_source"] = {"configured": "true", "commit": self.OLDER,
                                    "repository": self.fixture.PRIVATE,
                                    "root": str(self.cfg.fleet_source_store / self.OLDER)}
        self.cfg.install_receipt.write_text(json.dumps(receipt))
        self.assertEqual(su.EXIT_UNKNOWN, self.apply())
        self.assertEqual([], self.repo.calls)
        self.assertEqual([], self.sys.mutations())
        self.assertFalse(self.cfg.fleet_source_store.exists())
        self.assertNotIn("TARTCI_FLEET_SOURCE_ROOT", os.environ)

    def test_adopted_source_reinstalls_while_tartci_commit_is_still_soaking(self) -> None:
        """A source move must not wait for unrelated TartCI code to soak."""
        self.installed_from(self.OLDER)
        self.sys.log_lines = f"{T_NEW} {int(NOW - 60)}\n"
        soaked = {"repository": self.fixture.PRIVATE, "commit": self.fixture.COMMIT,
                  "age": su.FLEET_SOURCE_SOAK_SECONDS}
        with self.canary(soaked):
            self.assertEqual(su.EXIT_OK, self.apply(), self.last())
        self.assertEqual(INSTALLED, self.last()["target"])
        self.assertEqual([self.fixture.COMMIT], self.sys.installed_sources)

    def _assert_adopted_source_reinstalls_for_skew_state(self, state: str) -> None:
        self.installed_from(self.OLDER)
        self.sys.log_lines = ""
        soaked = {"repository": self.fixture.PRIVATE, "commit": self.fixture.COMMIT,
                  "age": su.FLEET_SOURCE_SOAK_SECONDS}
        skew = {"state": state, "target": None, "explicit": False}
        with self.canary(soaked), mock.patch.object(su, "measure_skew", return_value=skew):
            self.assertEqual(su.EXIT_OK, self.apply(), self.last())
        self.assertEqual(INSTALLED, self.last()["target"])
        self.assertEqual([self.fixture.COMMIT], self.sys.installed_sources)

    def test_adopted_source_reinstalls_while_tartci_is_behind(self) -> None:
        self._assert_adopted_source_reinstalls_for_skew_state("behind")

    def test_adopted_source_reinstalls_while_tartci_is_unverified(self) -> None:
        self._assert_adopted_source_reinstalls_for_skew_state("unverified")

    def test_a_record_for_another_repository_is_not_adopted(self) -> None:
        self.installed_from(self.OLDER)
        other = {"repository": "ORG/other", "commit": self.fixture.COMMIT, "age": 99999}
        with self.canary(other):
            self.assertEqual(self.OLDER, su.sync_fleet_source(self.cfg, self.sys)["commit"])

    def test_the_canary_record_is_read_on_its_own_clock(self) -> None:
        record = {"repository": self.fixture.PRIVATE, "commit": self.fixture.COMMIT,
                  "verified_at": 1000}
        def ssh(argv):
            if argv[0] == "ssh" and su.FLEET_SOURCE_VERIFIED in argv[-1]:
                return su.Result(0, "1600\n" + json.dumps(record) + "\n")
            return None
        with mock.patch.object(su, "published_peers", return_value={"studio": "m3"}), \
                mock.patch.object(self.sys, "run", side_effect=lambda argv, **kw: ssh(argv)
                                  or su.Result(255, "", "unreachable")):
            self.assertEqual({"repository": self.fixture.PRIVATE, "commit": self.fixture.COMMIT,
                              "age": 600.0}, su.read_canary_verified(self.cfg, self.sys, "studio"))
            record["commit"] = "not-a-commit"
            self.assertIsNone(su.read_canary_verified(self.cfg, self.sys, "studio"))
        with mock.patch.object(su, "published_peers", return_value={"studio": "m3"}), \
                mock.patch.object(self.sys, "run", return_value=su.Result(255, "", "timed out")):
            self.assertIsNone(su.read_canary_verified(self.cfg, self.sys, "studio"))

    def test_a_host_whose_canary_failed_does_not_move(self) -> None:
        # The canary never published the new commit: this host stays put and
        # its apply has nothing to do.
        self.installed_from(self.OLDER)
        self.sys.log_lines = ""   # current with main
        with self.canary({"repository": self.fixture.PRIVATE, "commit": self.OLDER,
                          "age": 99999}):
            self.assertEqual(su.EXIT_NOTHING, self.apply())
        self.assertEqual([], self.sys.mutations())


class ReinstallCurrentTests(FleetSourceFixture):
    """--reinstall-current: apply a settings change (here a removed
    [fleet_source]) to the generation already installed, in one command."""

    def setUp(self) -> None:
        super().setUp()
        receipt = self.home / ".config/tartci/macos-fleet-install.json"
        value = json.loads(receipt.read_text())
        value["fleet_source"] = {"configured": True, "repository": self.fixture.PRIVATE,
                                 "commit": self.OLDER,
                                 "root": str(self.cfg.fleet_source_store / self.OLDER)}
        receipt.write_text(json.dumps(value))
        self.cfg.fleet_source_repository = None      # the operator removed it
        self.sys.log_lines = ""                      # the installed commit is current

    def reinstall(self, apply: bool = True) -> int:
        return su.plan_or_apply(self.cfg, self.sys, apply=apply, target_ref="origin/main",
                                reinstall=True)

    def test_without_it_a_removed_source_waits_for_a_new_commit(self) -> None:
        # The gap: nothing to do, and the receipt keeps the stale source.
        self.assertEqual(su.EXIT_NOTHING, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertEqual(self.OLDER, su.installed_fleet_source_commit(self.cfg))

    def test_it_reinstalls_the_running_generation_and_clears_the_stale_source(self) -> None:
        self.assertEqual(su.EXIT_OK, self.reinstall(), self.last())
        self.assertEqual("succeeded", self.last()["status"])
        self.assertEqual(INSTALLED, self.last()["target"])
        self.assertIsNone(su.installed_fleet_source_commit(self.cfg))
        receipt = json.loads((self.home / ".config/tartci/macos-fleet-install.json").read_text())
        self.assertIs(False, receipt["fleet_source"]["configured"])
        joined = [" ".join(m) if isinstance(m, list) else m for m in self.sys.mutations()]
        self.assertTrue(any("pool drain" in m for m in joined), "the normal drain path")
        self.assertTrue(any("pool on" in m for m in joined))
        attempt = json.loads(Path(self.last()["receipt"]).read_text())
        self.assertIn("--reinstall-current", attempt["refresh"])

    def test_an_inherited_source_does_not_survive_a_removed_setting(self) -> None:
        # The caller's shell (or an agent plist) still exports the old source.
        os.environ.update({
            "TARTCI_FLEET_SOURCE_ROOT": str(self.cfg.fleet_source_store / self.OLDER),
            "TARTCI_FLEET_SOURCE_COMMIT": self.OLDER,
            "TARTCI_FLEET_SOURCE_REPOSITORY": self.fixture.PRIVATE,
        })
        self.assertEqual(su.EXIT_OK, self.reinstall(), self.last())
        self.assertEqual([None], self.sys.installed_sources)
        self.assertIsNone(su.installed_fleet_source_commit(self.cfg))

    def test_a_plan_changes_nothing(self) -> None:
        self.assertEqual(su.EXIT_OK, self.reinstall(apply=False))
        self.assertEqual([], self.sys.mutations())
        self.assertEqual(self.OLDER, su.installed_fleet_source_commit(self.cfg))

    def test_a_recent_install_of_the_same_commit_does_not_hold_it_back(self) -> None:
        self.assertEqual(su.EXIT_OK, self.reinstall(), self.last())
        self.sys.clock += 600
        self.assertEqual(su.EXIT_OK, self.reinstall(), self.last())
        self.assertEqual("succeeded", self.last()["status"])

    def configured_source(self, recorded: str, repository: str | None = None) -> None:
        """[fleet_source] configured (this host is its canary); the receipt
        records `recorded` as the installed private-source commit."""
        self.cfg.fleet_source_repository = self.fixture.PRIVATE
        receipt = self.home / ".config/tartci/macos-fleet-install.json"
        value = json.loads(receipt.read_text())
        value["fleet_source"] = {"configured": True,
                                 "repository": repository or self.fixture.PRIVATE,
                                 "commit": recorded,
                                 "root": str(self.cfg.fleet_source_store / recorded)}
        receipt.write_text(json.dumps(value))

    def test_the_same_commit_from_another_repository_is_a_new_source(self) -> None:
        self.configured_source(self.fixture.COMMIT, repository="ORG/another-fleet-authority")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(su.EXIT_REFUSED, self.reinstall())
        self.assertEqual([], self.sys.mutations())
        self.assertIn("installed from ORG/another-fleet-authority@", out.getvalue())

    def test_it_never_installs_a_new_private_source(self) -> None:
        # The canary would select main's head, newer than the installed source:
        # only the canary-gated update path may take it.
        self.configured_source(self.OLDER)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(su.EXIT_REFUSED, self.reinstall())
        self.assertEqual([], self.sys.mutations())
        self.assertIn("--reinstall-current refused: it would install private source "
                      f"{self.fixture.PRIVATE}@{self.fixture.COMMIT[:12]}", out.getvalue())
        self.assertEqual(self.OLDER, su.installed_fleet_source_commit(self.cfg))

    def test_it_reinstalls_on_the_installed_private_source(self) -> None:
        # Control: the selected source is the one already installed.
        self.configured_source(self.fixture.COMMIT)
        self.assertEqual(su.EXIT_OK, self.reinstall(), self.last())
        self.assertEqual("succeeded", self.last()["status"])
        self.assertEqual(self.fixture.COMMIT, su.installed_fleet_source_commit(self.cfg))

    def test_a_halt_still_holds_an_operator_reinstall(self) -> None:
        for n in range(su.MAX_CONSECUTIVE_FAILURES):
            su._write_json(self.cfg.state_dir / "attempts" / f"2027010{n}T000000Z-x.json",
                           {"mode": "apply", "status": "failed", "target": INSTALLED,
                            "started_at": f"2027-01-0{n + 1}T00:00:00Z"})
        self.assertTrue(su.halt_reason(self.cfg.state_dir))
        self.assertEqual(su.EXIT_REFUSED, self.reinstall())
        self.assertEqual([], self.sys.mutations())

    def test_a_pause_still_holds_an_operator_reinstall(self) -> None:
        su._write_json(self.cfg.state_dir / "scheduled-pause.json",
                       {"at": su._iso(self.sys.now()), "reason": "held"})
        self.assertEqual(su.EXIT_REFUSED, self.reinstall())
        self.assertEqual([], self.sys.mutations())

    # Base's pause test needs an update to refuse; on this current host there is
    # none without the flag. The test above covers the pause for a reinstall.
    test_scheduled_pause_refuses_real_apply_before_any_mutation = None

    def test_the_command_takes_no_target_and_is_never_scheduled(self) -> None:
        for extra in (["--target", T_OLD], ["--scheduled"]):
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit) as caught, \
                        mock.patch("sys.stderr", new=io.StringIO()):
                    su.main(["--apply", "--reinstall-current", "--home", str(self.home),
                             *extra])
                self.assertEqual(2, caught.exception.code)


class FleetSourceCanaryHostTests(FleetSourceFixture):
    """On the canary itself (this host is m1)."""

    def verified(self) -> dict | None:
        return su._read_json(self.cfg.state_dir / su.FLEET_SOURCE_VERIFIED)

    def test_a_verified_install_is_published_for_the_fleet(self) -> None:
        self.sys.log_lines = ""
        self.assertEqual(su.EXIT_OK, self.apply(), self.last())
        record = self.verified()
        self.assertEqual((self.fixture.PRIVATE, self.fixture.COMMIT),
                         (record["repository"], record["commit"]))
        self.assertEqual(self.sys.now(), record["verified_at"])

    def test_an_install_that_fails_verification_publishes_nothing(self) -> None:
        # The installer recorded the new commit, but pool on refused it.
        self.sys.log_lines = ""
        self.sys.on_rc = 1               # pool on keeps refusing
        self.assertNotEqual(su.EXIT_OK, self.apply())
        # The failed source move rolled back to the public source the host was
        # running before the attempt.
        self.assertIsNone(su.installed_fleet_source_commit(self.cfg))
        self.assertIsNone(self.verified())

    def test_failed_install_restores_previous_source_before_pool_on(self) -> None:
        """An installer self-rollback must not serve old code with new profiles."""
        self.sys.log_lines = ""
        self.sys.install_rcs = [1]
        self.sys.rollback_install_rc = 1
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertIsNone(os.environ.get("TARTCI_FLEET_SOURCE_COMMIT"))
        self.assertIsNone(os.environ.get("TARTCI_FLEET_SOURCE_REPOSITORY"))
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        self.assertTrue(any(step["step"] == "source-restore" for step in receipt["steps"]))

    def test_failed_install_restores_previous_private_source_before_pool_on(self) -> None:
        previous_root = self.cfg.fleet_source_store / self.OLDER
        previous_root.mkdir(parents=True)
        receipt_path = self.home / ".config/tartci/macos-fleet-install.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["fleet_source"] = {
            "configured": True, "commit": self.OLDER,
            "repository": self.fixture.PRIVATE, "root": str(previous_root),
        }
        receipt_path.write_text(json.dumps(receipt))
        self.sys.log_lines = ""
        self.sys.rollback_install_rc = 1
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertEqual(self.OLDER, os.environ.get("TARTCI_FLEET_SOURCE_COMMIT"))
        self.assertEqual(self.fixture.PRIVATE,
                         os.environ.get("TARTCI_FLEET_SOURCE_REPOSITORY"))

    def test_rollback_restores_the_previous_private_source(self) -> None:
        previous_root = self.cfg.fleet_source_store / self.OLDER
        previous_root.mkdir(parents=True)
        receipt_path = self.home / ".config/tartci/macos-fleet-install.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["fleet_source"] = {
            "configured": True, "commit": self.OLDER,
            "repository": self.fixture.PRIVATE, "root": str(previous_root),
        }
        receipt_path.write_text(json.dumps(receipt))
        self.sys.log_lines = ""
        # The target's pool on fails every attempt; the rollback's succeeds.
        self.sys.on_rcs = [1] * su.Run.POOL_ON_ATTEMPTS
        self.assertNotEqual(su.EXIT_OK, self.apply())
        self.assertEqual([self.fixture.COMMIT, self.OLDER], self.sys.installed_sources)
        restored = json.loads(receipt_path.read_text())["fleet_source"]
        self.assertEqual(self.OLDER, restored["commit"])
        self.assertEqual(self.fixture.PRIVATE, restored["repository"])
        self.assertEqual(str(previous_root), restored["root"])

    def installed_from_older(self, tree_sha256: str) -> None:
        receipt_path = self.home / ".config/tartci/macos-fleet-install.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["fleet_source"] = {
            "configured": True, "commit": self.OLDER, "repository": self.fixture.PRIVATE,
            "root": str(self.cfg.fleet_source_store / self.OLDER), "tree_sha256": tree_sha256,
        }
        receipt_path.write_text(json.dumps(receipt))

    def test_a_new_commit_with_the_installed_bytes_is_not_reinstalled(self) -> None:
        # main moved (a backup, say) but its profiles and supply are the bytes
        # the host already runs: no drain.
        selected = su.sync_fleet_source(self.cfg, self.sys)
        for name in su.FLEET_SOURCE_ENV:
            os.environ.pop(name, None)
        self.installed_from_older(selected["tree_sha256"])
        self.sys.log_lines = ""
        self.assertEqual(su.EXIT_NOTHING, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertEqual(self.OLDER, su.installed_fleet_source_commit(self.cfg))

    def test_a_new_commit_with_other_bytes_is_reinstalled(self) -> None:
        self.installed_from_older("0" * 64)
        self.sys.log_lines = ""
        self.assertEqual(su.EXIT_OK, self.apply(), self.last())
        self.assertEqual(self.fixture.COMMIT, su.installed_fleet_source_commit(self.cfg))

    def test_a_source_move_that_fails_verification_after_pool_on_is_rolled_back(self) -> None:
        # Same generation, new source: pool on succeeds, then the host is not
        # ready while the moved profiles are live.
        def hook(argv: list[str]) -> None:
            moved = su.installed_fleet_source_commit(self.cfg) == self.fixture.COMMIT
            self.sys.status_problems = ["broken"] if moved else []
        self.sys.hook = hook
        self.sys.log_lines = ""
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertEqual([self.fixture.COMMIT, None], self.sys.installed_sources)
        self.assertIsNone(su.installed_fleet_source_commit(self.cfg))
        self.assertIsNone(self.verified())
        receipt = json.loads(Path(self.last()["receipt"]).read_text())
        self.assertTrue(any(step["step"] == "rollback-install" for step in receipt["steps"]))

    def test_failed_previous_reinstall_keeps_the_target_source_selected(self) -> None:
        self.sys.log_lines = ""
        self.sys.on_rc = 1
        self.sys.rollback_install_rc = 1
        self.assertNotEqual(su.EXIT_OK, self.apply())
        self.assertEqual(self.fixture.COMMIT, os.environ.get("TARTCI_FLEET_SOURCE_COMMIT"))
        self.assertEqual(self.fixture.PRIVATE,
                         os.environ.get("TARTCI_FLEET_SOURCE_REPOSITORY"))

    def test_a_run_that_did_not_install_the_selected_commit_publishes_nothing(self) -> None:
        self.sys.log_lines = ""
        self.sys.records_source = False
        self.assertEqual(su.EXIT_OK, self.apply())
        self.assertIsNone(self.verified())

    def test_a_failed_install_publishes_nothing_and_backs_off(self) -> None:
        self.sys.log_lines = ""
        # A same-generation reinstall installs the running commit, which the
        # fake installer treats like a rollback: make that install fail.
        self.sys.rollback_install_rc = 1
        self.assertNotEqual(su.EXIT_OK, self.apply())
        self.assertNotEqual("succeeded", self.last()["status"])
        self.assertIsNone(self.verified())
        drained = len(self.sys.mutations())
        self.sys.clock += 1800          # the next scheduled turn
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual(drained, len(self.sys.mutations()), "no second drain within rate_hours")


class SealedHostFleetSourceTests(Base):
    """A sealed-launcher host refuses [fleet_source] before anything happens: a
    source move would not rebuild the launcher, whose policy digest covers the
    moved profile, so the install would fail after the drain every turn."""
    sealed = True

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in su.FLEET_SOURCE_ENV:
            os.environ.pop(name, None)
        self.cfg.fleet_source_repository = "ORG/private-fleet-authority"
        self.cfg.fleet_source_canary = "studio"
        self.pin = self.home / ".config/tartci/m3-launcher-approved.sha256"
        self.pin_before = self.pin.read_bytes()

    def assertNothingMoved(self) -> None:
        self.assertEqual([], self.sys.mutations())
        self.assertFalse(any("drain" in a for a, _ in self.sys.calls), "no drain call")
        self.assertFalse(any(len(a) > 2 and "private-fleet-authority" in a[2]
                             for a, _ in self.sys.calls if a[:1] == [FAKE_GH]),
                         "the private source is never read")
        self.assertFalse(self.cfg.fleet_source_store.exists(), "nothing exported")
        self.assertEqual(self.pin_before, self.pin.read_bytes(), "launcher pin unchanged")
        self.assertNotIn("TARTCI_FLEET_SOURCE_ROOT", os.environ, "no source selected")
        skew = json.loads((self.cfg.state_dir / "skew.json").read_text())
        self.assertEqual("unknown", skew["state"])
        self.assertIn("fleet source refused on a sealed-launcher host", skew["reason"])
        self.assertIn("remove [fleet_source]", skew["reason"])

    def test_an_apply_refuses_before_any_drain(self) -> None:
        self.assertEqual(su.EXIT_UNKNOWN, self.apply())
        self.assertNothingMoved()

    def test_a_plan_refuses_the_same_way(self) -> None:
        self.assertEqual(su.EXIT_UNKNOWN, self.plan())
        self.assertNothingMoved()

    def test_without_the_setting_a_sealed_host_is_unaffected(self) -> None:
        self.cfg.fleet_source_repository = None
        self.assertIsNone(su.sync_fleet_source(self.cfg, self.sys))

    # Base's pause test runs with this class's [fleet_source], which now
    # refuses first by design; Base covers it without the setting.
    test_scheduled_pause_refuses_real_apply_before_any_mutation = None


def fleet_source_supply() -> str:
    import fleet_source
    return fleet_source.PUBLISHED_SUPPLY


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


def make_v2_host(test, team: str = "95CX6P84C4") -> None:
    """A sealed host already on launcher v2: its stable app (inputs digest "f"*64,
    which the fake measures for any target), its pointer at a generation of
    INSTALLED, a v2 receipt, its opt-in file and the dedicated keychain."""
    config = test.home / ".config/tartci"
    app = test.home / "libexec/TartCILauncher.app"
    (app / "Contents/Resources/bundle.json").unlink()
    (app / "Contents/Resources/inputs.json").write_text(
        json.dumps({"schema": 1, "sha256": "f" * 64}))
    generation = test.home / ".local/share/tartci-generations" / f"{INSTALLED}-1"
    generation.mkdir(parents=True)
    (config / "launcher-generation.json").write_text(
        json.dumps({"schema": 1, "generation": str(generation)}))
    (config / "macos-fleet-install.json").write_text(json.dumps(
        {"launch_helper": {"schema": 2, "layout": "v2"},
         "support": {"source_commit": INSTALLED}}))
    test.sys.v2_generation = generation
    test.sys._set_installed(INSTALLED)
    opt_in = config / "launcher-v2.enabled"
    opt_in.write_text("")
    opt_in.chmod(0o600)
    secrets = test.home / ".config/pulp/secrets/keychain.env"
    secrets.parent.mkdir(parents=True)
    secrets.write_text('PULP_SIGN_KEYCHAIN="$HOME/Library/Keychains/pulp-signing.keychain-db"\n')
    helper = {"path": str(app), "identifier": "com.danielraffel.tartci.launcher",
              "team_id": team,
              "approval_sha256_path": str(config / "m3-launcher-approved.sha256")}
    patch = mock.patch.object(su, "launch_helper", return_value=helper)
    patch.start()
    test.addCleanup(patch.stop)


class LauncherV2SelfUpdateTests(Base):
    """Launcher v2: an update that leaves the stable app's inputs, signing
    identity and team equal builds nothing and needs no consent; any
    difference, or the move from v1, is a launcher change keyed on all three.
    Rollback points back at the previous generation."""
    sealed = True
    TEAM = "95CX6P84C4"

    def setUp(self) -> None:
        super().setUp()
        make_v2_host(self)
        # The owner's override; LauncherV2AutoApplyTests covers the default.
        self.cfg.launcher_auto_apply = False

    def ack_path(self) -> Path:
        return self.home / ".config/tartci/launcher-change-ack"

    def installed_v1(self) -> None:
        """The live app is v1: its receipt says so and its bundle.json names the commit."""
        (self.home / ".config/tartci/macos-fleet-install.json").write_text(json.dumps(
            {"launch_helper": {"schema": 1}, "support": {"source_commit": INSTALLED}}))
        (self.home / "libexec/TartCILauncher.app/Contents/Resources/bundle.json").write_text(
            json.dumps({"source_commit": INSTALLED}))

    def fail_probe_until_rollback(self) -> None:
        """pool on fails the volume probe until a rollback install has run."""
        self.sys.on_rc = 1
        self.sys.on_text = LauncherProbeStopTests.PROBE

        def recover(argv):
            if "--apply" in argv and "rollback" in " ".join(argv):
                self.sys.on_rc = 0
        self.sys.hook = recover

    def pin(self) -> Path:
        return self.home / ".config/tartci/m3-launcher-approved.sha256"

    def builds(self) -> list[list[str]]:
        return [a for a, _ in self.sys.calls if a[:2] == ["bash", "scripts/build_macos_launcher.sh"]]

    def installs(self) -> list[list[str]]:
        return [a for a, _ in self.sys.calls if "--apply" in a and "install" in a]

    def latest(self) -> dict:
        attempts = sorted((self.cfg.state_dir / "attempts").glob("*.json"))
        return json.loads(attempts[-1].read_text())

    def step(self, name: str) -> str:
        return next(s["detail"] for s in self.latest()["steps"] if s["step"] == name)

    def expected_ack(self, inputs: str = "f" * 64, identity: str = IDENTITY,
                     team: str = TEAM) -> str:
        return f"v2 inputs={inputs} identity={identity} team={team}"

    def test_the_installed_commit_is_read_through_the_pointer(self) -> None:
        self.assertEqual(INSTALLED, su.installed_commit(self.cfg)[0])

    def test_no_launcher_change_builds_nothing_and_needs_no_consent(self) -> None:
        self.assertUpdated(self.apply())
        self.assertEqual([], self.builds())
        install = self.installs()[0]
        self.assertNotIn("--launch-helper-source", install)
        self.assertEqual(IDENTITY, install[install.index("--launch-set-identity") + 1])
        self.assertTrue(install[install.index("--launch-set-keychain") + 1]
                        .endswith("pulp-signing.keychain-db"))
        self.assertEqual("0" * 64 + "\n", self.pin().read_text(), "the approval pin stays")
        self.assertIn("no launcher change", self.step("launcher"))
        # An unchanged v2 launcher has no volume probe to fail: no store write.
        self.assertEqual([], self.sys.store_writes)

    def test_changed_inputs_need_the_exact_ack_before_any_drain(self) -> None:
        app = self.home / "libexec/TartCILauncher.app/Contents/Resources/inputs.json"
        app.write_text(json.dumps({"schema": 1, "sha256": "e" * 64}))
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertEqual([], self.builds())
        error = self.latest()["error"]
        self.assertIn(f"inputs {'e' * 12} -> {'f' * 12}", error)
        self.assertIn(f"echo '{self.expected_ack()}' > {self.ack_path()}", error)
        # The printed line, written as printed, lets the change through.
        self.ack_path().write_text(self.expected_ack() + "\n")
        self.assertUpdated(self.apply())
        build = self.builds()[0]
        self.assertEqual("v2", build[build.index("--layout") + 1])
        install = self.installs()[0]
        self.assertIn("--launch-helper-source", install)
        self.assertEqual("d" * 64 + "\n", self.pin().read_text(), "the new app's approval")

    def test_an_ack_for_any_other_inputs_identity_or_team_is_refused(self) -> None:
        app = self.home / "libexec/TartCILauncher.app/Contents/Resources/inputs.json"
        app.write_text(json.dumps({"schema": 1, "sha256": "e" * 64}))
        for stale in (self.expected_ack(inputs="e" * 64),
                      self.expected_ack(identity="A" * 40),
                      self.expected_ack(team="ZZZZZZZZZZ"),
                      T_OLD):     # a v1 commit ack never covers a v2 change
            with self.subTest(stale=stale[:40]):
                self.ack_path().write_text(stale + "\n")
                self.assertEqual(su.EXIT_REFUSED, self.apply())
                self.assertEqual([], self.sys.mutations())
                self.assertIn("launcher change needs consent", self.latest()["error"])

    def test_a_rotated_signing_certificate_is_a_launcher_change(self) -> None:
        self.sys.keychain_identities = ["B" * 40]   # the live app's cert is gone
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        error = self.latest()["error"]
        self.assertIn(f"identity {IDENTITY[:12]} -> {'B' * 12}", error)
        self.assertIn(self.expected_ack(identity="B" * 40), error)

    def test_the_turn_after_an_acked_rotation_sees_no_change(self) -> None:
        self.sys.keychain_identities = ["B" * 40]   # the live app's cert is gone
        self.ack_path().write_text(self.expected_ack(identity="B" * 40) + "\n")
        self.assertUpdated(self.apply())
        self.assertEqual("B" * 40, self.sys.built_identity)
        # The next turn reads the new live app's certificate: no re-detection.
        helper = su.launch_helper(self.cfg)
        with tempfile.TemporaryDirectory() as scratch:
            live = su.extract_signing_identity(self.sys, Path(helper["path"]), Path(scratch))
        signer = su.choose_signer(self.sys, self.home, live)
        change = su.v2_launcher_change(self.cfg, self.sys, helper, su.checked_in_profile(self.cfg),
                                       signer, live)
        self.assertEqual([], change.differs)

    def test_the_ack_team_is_the_certificates_and_must_be_the_profiles(self) -> None:
        # The chosen certificate is for another team: refuse, before any drain,
        # rather than build a launcher its own policy would reject.
        self.sys.keychain_identities = [(IDENTITY, "ZZZZZZZZZZ")]
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertIn("is for team ZZZZZZZZZZ, not the profile's launch_helper.team_id "
                      "95CX6P84C4", self.latest()["error"])

    def test_a_new_v2_app_must_carry_the_measured_inputs(self) -> None:
        app = Path(self.td.name) / "new.app"
        resources = app / "Contents" / "Resources"
        resources.mkdir(parents=True)
        (resources / "inputs.json").write_text(json.dumps({"schema": 1, "sha256": "e" * 64}))
        with self.assertRaisesRegex(su.Refused, "inputs e+ != the target's f+"):
            su.verify_bundle_v2(self.sys, app, "f" * 64)
        with self.assertRaisesRegex(su.Refused, "!= the target's None"):
            su.verify_bundle_v2(self.sys, app, None)
        (resources / "inputs.json").write_text(json.dumps({"schema": 1, "sha256": "f" * 64}))
        su.verify_bundle_v2(self.sys, app, "f" * 64)        # control: matching inputs pass
        (resources / "bundle.json").write_text("{}")
        with self.assertRaisesRegex(su.Refused, "v1 bundle.json"):
            su.verify_bundle_v2(self.sys, app, "f" * 64)
        (resources / "bundle.json").unlink()
        self.sys.codesign_verify_rc = 1
        with self.assertRaisesRegex(su.Refused, "codesign --verify"):
            su.verify_bundle_v2(self.sys, app, "f" * 64)

    def test_a_different_team_is_a_launcher_change(self) -> None:
        self.sys.live_team = "ZZZZZZZZZZ"
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertIn(f"team ZZZZZZZZZZ -> {self.TEAM}", self.latest()["error"])

    def test_a_move_from_v1_is_a_launcher_change(self) -> None:
        self.installed_v1()
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertIn("layout v1 -> v2", self.latest()["error"])

    def test_the_plan_says_which_of_the_three_differed(self) -> None:
        self.sys.live_team = "ZZZZZZZZZZ"
        self.plan()
        launcher = self.step("launcher")
        self.assertIn("launcher change (team ZZZZZZZZZZ -> 95CX6P84C4)", launcher)
        self.assertNotIn("inputs", launcher)
        self.assertNotIn("identity", launcher)

    def test_a_v2_host_whose_opt_in_is_gone_refuses_before_any_drain(self) -> None:
        (self.home / ".config/tartci/launcher-v2.enabled").unlink()
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertIn("--launcher-layout v1", self.latest()["error"])

    def test_a_receiptless_v2_app_is_seen_by_the_plan_before_any_drain(self) -> None:
        (self.home / ".config/tartci/macos-fleet-install.json").unlink()
        (self.home / ".config/tartci/launcher-generation.json").unlink()
        (self.home / ".config/tartci/launcher-v2.enabled").unlink()
        policy = self.home / "libexec/TartCILauncher.app/Contents/Resources/policy.json"
        policy.write_text(json.dumps({"schema": 2}))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(su.EXIT_REFUSED, self.plan())
        self.assertEqual([], self.sys.mutations())
        self.assertIn("install receipt is missing but a launcher v2 app", output.getvalue())

    def test_without_the_dedicated_keychain_v2_refuses_before_any_drain(self) -> None:
        (self.home / ".config/pulp/secrets/keychain.env").unlink()
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertIn("dedicated signing keychain", self.latest()["error"])

    def test_a_failed_probe_without_a_launcher_change_rolls_back(self) -> None:
        # No new app, so the probe failure cannot be a lost grant: roll back
        # (the installer points the launcher back at the previous generation).
        self.fail_probe_until_rollback()
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertEqual("rolled_back", self.last()["status"], self.last())
        self.assertEqual(INSTALLED, self.sys.running())
        rollback = self.installs()[-1]
        self.assertIn("rollback", " ".join(rollback))
        self.assertEqual("v2", rollback[rollback.index("--launcher-layout") + 1])
        self.assertEqual(IDENTITY, rollback[rollback.index("--launch-set-identity") + 1])

    def test_a_failed_probe_after_a_launcher_change_stops_without_rollback(self) -> None:
        # Control: with a new app the #579 stop still applies.
        app = self.home / "libexec/TartCILauncher.app/Contents/Resources/inputs.json"
        app.write_text(json.dumps({"schema": 1, "sha256": "e" * 64}))
        self.ack_path().write_text(self.expected_ack() + "\n")
        self.fail_probe_until_rollback()
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertIn("STOPPED and NOT rolled back", self.last()["error"])
        self.assertEqual(1, len(self.installs()), "no rollback install")

    def test_a_rollback_after_a_move_from_v1_reinstalls_v1_explicitly(self) -> None:
        self.installed_v1()
        self.ack_path().write_text(self.expected_ack() + "\n")
        self.sys.broken_target = True
        self.assertEqual(su.EXIT_FAILED, self.apply())
        rollback = self.installs()[-1]
        self.assertEqual("v1", rollback[rollback.index("--launcher-layout") + 1])
        self.assertNotIn("--launch-set-identity", rollback)


class LauncherV2AutoApplyTests(Base):
    """Plan item 12 on a launcher v2 host: a launcher change keyed on inputs,
    identity and team applies by itself when the team is the installed app's."""
    sealed = True
    TEAM = "95CX6P84C4"

    def setUp(self) -> None:
        super().setUp()
        make_v2_host(self)
        inputs = self.home / "libexec/TartCILauncher.app/Contents/Resources/inputs.json"
        inputs.write_text(json.dumps({"schema": 1, "sha256": "e" * 64}))

    def latest(self) -> dict:
        attempts = sorted((self.cfg.state_dir / "attempts").glob("*.json"))
        return json.loads(attempts[-1].read_text())

    def installs(self) -> list[list[str]]:
        return [a for a, _ in self.sys.calls if "--apply" in a and "install" in a]

    def test_changed_inputs_with_the_same_team_apply_automatically(self) -> None:
        self.assertUpdated(self.apply())
        ack = self.latest()["launcher_ack"]
        self.assertEqual(("automatic", self.TEAM, self.TEAM),
                         (ack["mode"], ack["team_id"], ack["installed_team_id"]))
        self.assertEqual(f"v2 inputs={'f' * 64} identity={IDENTITY} team={self.TEAM}", ack["key"])
        self.assertIn("--launch-helper-source", self.installs()[0])

    def test_a_team_change_waits_for_the_ack(self) -> None:
        self.sys.live_team = "ZZZZZZZZZZ"
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertIn(f"the new launcher is for team {self.TEAM}, the installed one for "
                      "ZZZZZZZZZZ", self.latest()["launcher_ack"]["auto_refused"])
        self.assertIn("launcher change needs consent", self.latest()["error"])

    def test_the_move_from_v1_waits_for_the_ack(self) -> None:
        (self.home / ".config/tartci/macos-fleet-install.json").write_text(json.dumps(
            {"launch_helper": {"schema": 1}, "support": {"source_commit": INSTALLED}}))
        (self.home / "libexec/TartCILauncher.app/Contents/Resources/bundle.json").write_text(
            json.dumps({"source_commit": INSTALLED}))
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertIn("v1 to v2", self.latest()["launcher_ack"]["auto_refused"])

    def test_a_lost_grant_rolls_back_to_the_previous_generation(self) -> None:
        self.sys.on_rc = 1
        self.sys.on_text = LauncherProbeStopTests.PROBE

        def recover(argv):
            if "--apply" in argv and "rollback" in " ".join(argv):
                self.sys.on_rc = 0
        self.sys.hook = recover
        self.assertEqual(su.EXIT_FAILED, self.apply())
        self.assertEqual("rolled_back", self.last()["status"], self.last())
        self.assertEqual(INSTALLED, self.sys.running())
        rollback = self.installs()[-1]
        self.assertEqual("v2", rollback[rollback.index("--launcher-layout") + 1])
        hold = su._read_json(self.cfg.state_dir / su.LAUNCHER_AUTO_APPLY_NAME)
        self.assertIn("v2 inputs=", hold["acknowledge"])


class LauncherV2FleetSourceTests(FleetSourceFixture):
    """On a launcher v2 host a [fleet_source] move is measured like any update:
    a refresh when the stable app's inputs are unchanged, an acknowledged
    launcher change when they are not. v1 sealed hosts keep refusing it."""
    sealed = True

    def setUp(self) -> None:
        super().setUp()
        make_v2_host(self)
        self.cfg.fleet_source_canary = su.self_host_id(self.cfg)   # this sealed host
        self.sys.log_lines = ""          # main has not moved: only the source has

    def installs(self) -> list[list[str]]:
        return [a for a, _ in self.sys.calls if "--apply" in a and "install" in a]

    def builds(self) -> list[list[str]]:
        return [a for a, _ in self.sys.calls if a[:2] == ["bash", "scripts/build_macos_launcher.sh"]]

    def latest(self) -> dict:
        attempts = sorted((self.cfg.state_dir / "attempts").glob("*.json"))
        return json.loads(attempts[-1].read_text())

    def ack(self) -> str:
        return f"v2 inputs={'f' * 64} identity={IDENTITY} team=95CX6P84C4"

    def test_a_source_move_with_unchanged_inputs_is_a_refresh_that_keeps_the_app(self) -> None:
        self.assertEqual(su.EXIT_OK, self.apply(), self.last())
        self.assertIn("fleet source moved", self.latest()["refresh"])
        self.assertEqual([], self.builds())
        install = self.installs()[0]
        self.assertNotIn("--launch-helper-source", install)
        self.assertIn("--launch-set-identity", install)
        self.assertEqual(self.fixture.COMMIT, su.installed_fleet_source_commit(self.cfg))
        launcher = next(s["detail"] for s in self.latest()["steps"] if s["step"] == "launcher")
        self.assertIn("no launcher change", launcher)

    def test_a_source_move_that_changes_the_inputs_waits_for_the_ack(self) -> None:
        inputs = self.home / "libexec/TartCILauncher.app/Contents/Resources/inputs.json"
        inputs.write_text(json.dumps({"schema": 1, "sha256": "e" * 64}))
        self.assertEqual(su.EXIT_REFUSED, self.apply())
        self.assertEqual([], self.sys.mutations())
        self.assertIn(f"echo '{self.ack()}'", self.latest()["error"])
        (self.home / ".config/tartci/launcher-change-ack").write_text(self.ack() + "\n")
        self.assertEqual(su.EXIT_OK, self.apply(), self.last())
        build = self.builds()[0]
        self.assertEqual("v2", build[build.index("--layout") + 1])
        self.assertIn("--launch-helper-source", self.installs()[0])

    def test_a_v1_sealed_host_still_refuses_the_source_even_with_the_opt_in(self) -> None:
        # Control: #570's refusal stands until the host has installed v2.
        (self.home / ".config/tartci/macos-fleet-install.json").write_text(json.dumps(
            {"launch_helper": {"schema": 1}, "support": {"source_commit": INSTALLED}}))
        with self.assertRaisesRegex(su.Refused, "fleet source refused on a sealed-launcher host"):
            su.sync_fleet_source(self.cfg, self.sys)
        self.assertNotIn("TARTCI_FLEET_SOURCE_ROOT", os.environ)

    def test_a_v2_host_whose_opt_in_is_gone_refuses_the_source_before_reading_it(self) -> None:
        (self.home / ".config/tartci/launcher-v2.enabled").unlink()
        with self.assertRaisesRegex(su.Refused, "--launcher-layout v1"):
            su.sync_fleet_source(self.cfg, self.sys)
        self.assertFalse(any(len(a) > 2 and self.fixture.PRIVATE in a[2]
                             for a, _ in self.sys.calls if a[:1] == [FAKE_GH]))

    # Base's pause test does not configure a v2 host's source; Base covers it.
    test_scheduled_pause_refuses_real_apply_before_any_mutation = None


if __name__ == "__main__":
    unittest.main()
