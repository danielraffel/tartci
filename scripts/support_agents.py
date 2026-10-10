#!/usr/bin/env python3
"""Converge a host's support LaunchAgents onto what its fleet profile declares.

Why this exists: self-update installs the fleet lanes from the profile, but a
support agent (the disk reclaimer, the artifact-cache refresher, the keychain
unlocker, the schedule backstop) reaches a host only when someone runs its
install script there. Hand-installed agents drift: on 2026-10-04 a stopgap
agent written outside any installer ran beside the interval guard on every
host, and three hosts lacked agents their peers had. This module makes the
profile the source of truth:

    [support_agents]
    declared = ["reclaim", "artifact-cache-refresh", "keychain-unlock"]
    bootstrap = false      # false: render, compare, report; never write
                           # true: converge every declared agent
                           # ["reap"]: converge only the named agents

Each declared name is a REGISTRY entry. Its plist is rendered with the same
`render_launchd_template.py` arguments its install script uses, so a host the
script installed reads byte-identical. An agent's own settings stay in its own
profile table or key (`schedule_backstop`, `[reclaim]`); this table says only
which agents the host carries.

`plan` (always safe; writes only its own receipt) compares every declared
agent's installed plist with its render:

    match_bytes     identical
    match_plist     equal as plists; key order only (the launchd watchdog
                    rewrites agents with sorted keys)
    differs         the receipt names each differing key path
    missing         no installed plist

and lists every installed agent under the tartci and `tmp.` prefixes that no
declaration, lane, or other codified installer (OTHER_OWNERS) accounts for. An
undeclared agent is reported, never touched.

`apply` acts only when the profile turns bootstrap on: `true` for every
declared agent, or a list of declared names for only those (the others are
planned and reported, never written, and a list never drops an agent). It renders every
declared agent first; if any render fails it changes nothing in that pass.
Then, per agent: a missing or differing plist is written and (re)bootstrapped,
and kickstarted only if its template has RunAtLoad (exactly as its install
script does); a matching plist loaded from its own path is left alone; a
matching one not loaded, or loaded from another path, is bootstrapped. A
declaration dropped from a PRESENT table, that the previous receipt shows
declared under a present table, is booted out and its plist moved aside (the
log is kept). An absent table manages nothing and removes nothing: a profile
snapshot older than the table must never read as "drop everything".

`auto` is `apply` when the profile switch is on and `plan` otherwise; it is what
self-update runs after it verifies the lanes. `check-templates` is self-update's
pre-drain check that the target checkout can render every declared agent.
`status --json` is the summary `tartci doctor fleet` reads.

Exit codes: 0 converged or nothing to do, 3 refused (a render failed, or the
profile is unreadable), 4 an apply step failed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # Python < 3.11 (/usr/bin/python3 on macOS is 3.9)
    tomllib = None  # type: ignore[assignment]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reuse_canary  # noqa: E402
import schedule_backstop_mode  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TABLE = "support_agents"
TABLE_KEYS = {"declared", "bootstrap"}
LANE_PREFIX = "com.danielraffel.tartci.tart-runner-macos-fleet."
# Labels the undeclared scan covers: tartci's own agents, and `tmp.`, a class
# nothing of ours may ever ship under.
SCAN_PREFIXES = ("com.danielraffel.tartci.", "com.danielraffel.tmp.")
EXIT_OK, EXIT_REFUSED, EXIT_FAILED = 0, 3, 4


def _tart_home(profile: Dict[str, Any]) -> Dict[str, str]:
    """This host's Tart store, which the watchdog and reaper must not guess.

    A LaunchAgent inherits no login shell; rendered without it, the agent reads
    Tart's default store and sees an empty inventory.
    """
    host = profile.get("host") if isinstance(profile.get("host"), dict) else {}
    tart_home = host.get("tart_home")
    return {"TART_HOME": tart_home} if isinstance(tart_home, str) and tart_home else {}


def _backstop_env(profile: Dict[str, Any]) -> Dict[str, str]:
    mode, _ = schedule_backstop_mode.mode_of(profile)
    return dict(schedule_backstop_mode.ENVIRONMENT.get(mode or "", {}))


@dataclass(frozen=True)
class Agent:
    """One declarable support agent: how its install script renders it."""
    label: str
    installer: Optional[str]              # its install script; None when only declared
    kickstart: bool                       # bootstrap is followed by a kickstart
    environment: Callable[[Dict[str, Any]], Dict[str, str]] = field(
        default=lambda profile: {})
    # Template placeholders beyond $HOME, from the host's own profile.
    settings: Callable[[Dict[str, Any]], Dict[str, str]] = field(
        default=lambda profile: {})

    @property
    def template(self) -> Path:
        return ROOT / "launchd" / f"{self.label}.plist.template"


REGISTRY: Dict[str, Agent] = {
    "reclaim": Agent("com.danielraffel.tartci.reclaim",
                     "scripts/install_reclaim_agent.sh", kickstart=True),
    "artifact-cache-refresh": Agent("com.danielraffel.tartci.artifact-cache-refresh",
                                    "scripts/install_artifact_cache_refresh_agent.sh",
                                    kickstart=False),
    "keychain-unlock": Agent("com.danielraffel.tartci.keychain-unlock",
                             "scripts/install_keychain_unlock_agent.sh", kickstart=True),
    "schedule-backstop": Agent("com.danielraffel.pulp.schedule-backstop",
                               "scripts/install_schedule_backstop_agent.sh", kickstart=True,
                               environment=_backstop_env),
    # Installed only through this declaration; RunAtLoad is off because one
    # pass builds Pulp for hours, so nothing kickstarts it.
    "reuse-canary": Agent(reuse_canary.LABEL, None, kickstart=False),
    # The self-heal watchdog (heal pass, skew and tool-freshness refresh,
    # config warnings) and the Tier-2 reaper were rendered by hand from
    # launchd/README.md, so a host could be brought up without them: m5studio
    # served gate VMs with no watchdog and its freshness was never measured.
    "launchd-watchdog": Agent("com.danielraffel.tartci.launchd-watchdog", None,
                              kickstart=True, settings=_tart_home),
    "reap": Agent("com.danielraffel.tartci.reap", None, kickstart=True, settings=_tart_home),
}

# Agents in the scanned prefixes that another codified path installs or
# retires. Each names that path; the registry test holds this map and the
# registry to exactly the templates in launchd/.
OTHER_OWNERS: Dict[str, str] = {
    "com.danielraffel.tartci.self-update": "scripts/install_self_update_agent.sh",
    "com.danielraffel.tartci.http-connect-ssh-relay": "scripts/network_profile.py reconcile",
    "com.danielraffel.tartci.orchard-worker": "scripts/disable_orchard.sh (retirement)",
}


# ── profile ────────────────────────────────────────────────────────────────

def validate(data: Dict[str, Any]) -> List[str]:
    """Problems with a parsed profile's `[support_agents]` table; empty when fine.

    Shared by `macos_fleet_lanes validate` and the runtime reader, so a profile
    that installs is a profile this module acts on.
    """
    table = data.get(TABLE)
    if table is None:
        return []
    if not isinstance(table, dict):
        return ["support_agents must be a table"]
    problems = []
    unknown = set(table) - TABLE_KEYS
    if unknown:
        problems.append(f"unknown support_agents keys: {sorted(unknown)}")
    declared = table.get("declared", [])
    if not isinstance(declared, list) or not all(isinstance(n, str) for n in declared):
        return problems + ["support_agents.declared must be a list of names"]
    names = [n for n in declared if n not in REGISTRY]
    if names:
        problems.append(f"support_agents.declared has unknown agents {names}; "
                        f"known: {sorted(REGISTRY)}")
    if len(set(declared)) != len(declared):
        problems.append("support_agents.declared lists an agent twice")
    switch = table.get("bootstrap", False)
    if isinstance(switch, list):
        if (not switch or not all(isinstance(n, str) for n in switch)
                or len(set(switch)) != len(switch)
                or any(n not in declared for n in switch)):
            problems.append("support_agents.bootstrap list must name declared agents, "
                            "each once")
    elif type(switch) is not bool:
        problems.append("support_agents.bootstrap must be a boolean or a list of "
                        "declared agent names")
    canary = data.get(reuse_canary.TABLE)
    canary_on = isinstance(canary, dict) and canary.get("enabled") is True
    if canary_on != ("reuse-canary" in declared):
        problems.append("support_agents.declared must list reuse-canary exactly when "
                        "[reuse_canary] enabled = true")
    mode, _ = schedule_backstop_mode.mode_of(data)
    backstop_on = mode in ("live", "dry-run")
    if backstop_on != ("schedule-backstop" in declared):
        problems.append("support_agents.declared must list schedule-backstop exactly when "
                        f"schedule_backstop is live or dry-run (it is {mode!r})")
    return problems


def table_digest(table: Optional[Dict[str, Any]]) -> Optional[str]:
    if table is None:
        return None
    return hashlib.sha256(json.dumps(table, sort_keys=True).encode()).hexdigest()


def default_profile_path() -> Path:
    return Path(os.environ.get(
        "TARTCI_FLEET_PROFILE",
        str(Path.home() / ".config" / "tartci" / "macos-fleet-profile.toml"),
    )).expanduser()


def load_profile(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """(parsed profile, why). None means unreadable, never "nothing declared"."""
    if not path.exists():
        return {}, f"no fleet profile at {path}"
    if tomllib is None:
        return None, "no tomllib (needs Python 3.11+); cannot read the fleet profile"
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError) as exc:
        return None, f"cannot read {path}: {exc}"
    problems = validate(data)
    if problems:
        return None, "; ".join(problems)
    return data, str(path)


# ── effects ────────────────────────────────────────────────────────────────

class System:
    """Every external effect, so plan and apply run against fakes in tests."""

    def __init__(self, home: Path, launchctl: str = "/bin/launchctl") -> None:
        self.home = home
        self.launchctl = launchctl

    @property
    def agents_dir(self) -> Path:
        return Path(os.environ.get("TARTCI_AGENTS_DIR", str(self.home / "Library" / "LaunchAgents")))

    def now(self) -> float:
        return time.time()

    def render(self, agent: Agent, profile: Dict[str, Any]) -> Tuple[Optional[bytes], str]:
        """The install script's exact render: same script, same arguments."""
        argv = ["python3", str(ROOT / "scripts" / "render_launchd_template.py"),
                str(agent.template), "--set", f"HOME={self.home}"]
        for name, value in agent.settings(profile).items():
            argv += ["--set", f"{name}={value}"]
        for name, value in agent.environment(profile).items():
            argv += ["--environment", f"{name}={value}"]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if proc.returncode != 0:
            return None, (proc.stderr or b"").decode(errors="replace").strip()[-300:]
        try:
            plistlib.loads(proc.stdout)
        except Exception as exc:  # noqa: BLE001 - any parse failure refuses
            return None, f"render is not a plist: {exc}"
        return proc.stdout, ""

    def launchctl_run(self, *args: str) -> Tuple[int, str]:
        try:
            proc = subprocess.run([self.launchctl, *args], capture_output=True, text=True,
                                  timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def loaded_path(self, label: str) -> Optional[str]:
        """The plist launchd holds `label` from, "" when held from unknown, None when not loaded."""
        rc, out = self.launchctl_run("print", f"gui/{os.getuid()}/{label}")
        if rc != 0:
            return None
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("path = "):
                return stripped[len("path = "):]
        return ""

    def domain_guard(self, target: Path) -> Tuple[bool, str]:
        """The installers' own guard, so a temporary HOME never reaches gui/<uid>."""
        script = ROOT / "scripts" / "launchd_domain_guard.sh"
        proc = subprocess.run(
            ["bash", "-c", f'. "{script}"; tartci_launchd_domain_guard "$1" "$2"', "guard",
             str(target), self.launchctl],
            capture_output=True, text=True, env=dict(os.environ, HOME=str(self.home)),
            check=False)
        return proc.returncode == 0, (proc.stderr or "").strip()


def diff_paths(installed: Any, rendered: Any, prefix: str = "") -> List[Dict[str, Any]]:
    """Every key path where two plist values differ."""
    if isinstance(installed, dict) and isinstance(rendered, dict):
        out: List[Dict[str, Any]] = []
        for key in sorted(set(installed) | set(rendered)):
            path = f"{prefix}/{key}"
            if key not in installed:
                out.append({"path": path, "installed": None, "rendered": rendered[key]})
            elif key not in rendered:
                out.append({"path": path, "installed": installed[key], "rendered": None})
            else:
                out += diff_paths(installed[key], rendered[key], path)
        return out
    if installed != rendered:
        return [{"path": prefix or "/", "installed": installed, "rendered": rendered}]
    return []


def compare(installed: Optional[bytes], rendered: bytes) -> Tuple[str, List[Dict[str, Any]]]:
    if installed is None:
        return "missing", []
    if installed == rendered:
        return "match_bytes", []
    try:
        mine = plistlib.loads(installed)
    except Exception:  # noqa: BLE001 - an unreadable plist differs from the render
        return "differs", [{"path": "/", "installed": "<unreadable plist>", "rendered": "<plist>"}]
    theirs = plistlib.loads(rendered)
    if mine == theirs:
        return "match_plist", []
    return "differs", diff_paths(mine, theirs)


# ── the pass ───────────────────────────────────────────────────────────────

class Converger:
    def __init__(self, sys_: System, profile_path: Path, state_dir: Path) -> None:
        self.sys = sys_
        self.profile_path = profile_path
        self.state_dir = state_dir

    @property
    def receipt_path(self) -> Path:
        return self.state_dir / "last.json"

    def event(self, kind: str, detail: str, **fields: Any) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        record = {"ts": self.sys.now(), "event": kind, "detail": detail, **fields}
        with (self.state_dir / "events.jsonl").open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def previous(self) -> Optional[Dict[str, Any]]:
        try:
            value = json.loads(self.receipt_path.read_text())
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def undeclared(self, declared_labels: set) -> List[Dict[str, str]]:
        found = []
        agents_dir = self.sys.agents_dir
        if not agents_dir.is_dir():
            return found
        for path in sorted(agents_dir.glob("*.plist")):
            try:
                label = plistlib.loads(path.read_bytes()).get("Label")
            except Exception:  # noqa: BLE001 - name it by file when unreadable
                label = path.name[:-len(".plist")]
            if not isinstance(label, str) or not label.startswith(SCAN_PREFIXES):
                continue
            if label.startswith(LANE_PREFIX) or label in declared_labels or label in OTHER_OWNERS:
                continue
            found.append({"label": label, "plist": str(path)})
        return found

    def run(self, mode: str) -> int:
        """`plan`, `apply`, or `auto` (apply when the profile switch is on)."""
        data, why = load_profile(self.profile_path)
        receipt: Dict[str, Any] = {
            "ts": self.sys.now(), "profile": str(self.profile_path), "requested": mode,
            "agents": {}, "undeclared": [], "dropped": [], "refused": None, "failures": [],
        }
        if data is None:
            receipt.update(table_present=None, table_sha256=None, declared=[], bootstrap=False,
                           mode="plan", refused=f"profile unreadable: {why}")
            return self.finish(receipt, EXIT_REFUSED)
        table = data.get(TABLE)
        present = isinstance(table, dict)
        declared: List[str] = list(table.get("declared", [])) if present else []
        raw_switch = table.get("bootstrap", False) if present else False
        # A list converges only the named agents; True converges every one.
        only = [n for n in raw_switch if n in declared] if isinstance(raw_switch, list) else None
        switch = bool(only) if only is not None else raw_switch is True
        acting = mode == "apply" or (mode == "auto" and switch)
        if mode == "apply" and not switch:
            acting = False
            receipt["refused"] = "bootstrap = false in the profile; plan only"
        receipt.update(table_present=present, table_sha256=table_digest(table if present else None),
                       declared=declared, bootstrap=switch, mode="apply" if acting else "plan")
        if only is not None:
            receipt["bootstrap_only"] = only
        receipt["undeclared"] = self.undeclared({REGISTRY[n].label for n in declared})
        # Plan everything first: one failing render changes nothing in the pass.
        renders: Dict[str, bytes] = {}
        for name in declared:
            agent = REGISTRY[name]
            rendered, err = self.sys.render(agent, data)
            entry: Dict[str, Any] = {"label": agent.label}
            receipt["agents"][name] = entry
            if rendered is None:
                entry.update(state="render_failed", error=err)
                continue
            renders[name] = rendered
            target = self.sys.agents_dir / f"{agent.label}.plist"
            installed = target.read_bytes() if target.is_file() else None
            state, diff = compare(installed, rendered)
            entry.update(state=state, diff=diff)
            loaded = self.sys.loaded_path(agent.label)
            entry["loaded"] = loaded is not None
            entry["leaked"] = loaded is not None and loaded != str(target)
        failed = [n for n, e in receipt["agents"].items() if e["state"] == "render_failed"]
        if failed:
            receipt["refused"] = f"render failed for {failed}; nothing applied in this pass"
            receipt["mode"] = "plan"
            return self.finish(receipt, EXIT_REFUSED)
        if not acting:
            return self.finish(receipt, EXIT_OK)
        for name in (only if only is not None else declared):
            self.apply_one(name, renders[name], receipt)
        if only is None:
            self.drop(receipt, declared, present)
        return self.finish(receipt, EXIT_FAILED if receipt["failures"] else EXIT_OK)

    def apply_one(self, name: str, rendered: bytes, receipt: Dict[str, Any]) -> None:
        agent = REGISTRY[name]
        entry = receipt["agents"][name]
        target = self.sys.agents_dir / f"{agent.label}.plist"
        matches = entry["state"] in ("match_bytes", "match_plist")
        if matches and entry["loaded"] and not entry["leaked"]:
            entry["action"] = "none"
            return
        ok, why = self.sys.domain_guard(target)
        if not ok:
            entry["action"] = "refused"
            receipt["failures"].append(f"{name}: {why}")
            return
        domain = f"gui/{os.getuid()}"
        steps = []
        if entry["leaked"] or (entry["loaded"] and not matches):
            self.sys.launchctl_run("bootout", f"{domain}/{agent.label}")
            steps.append("bootout")
        if not matches:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            tmp.write_bytes(rendered)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
            steps.append("write")
        rc, out = self.sys.launchctl_run("bootstrap", domain, str(target))
        steps.append("bootstrap")
        if rc != 0:
            entry["action"] = "+".join(steps)
            receipt["failures"].append(f"{name}: bootstrap failed: {out.strip()[:200]}")
            return
        if agent.kickstart:
            self.sys.launchctl_run("kickstart", f"{domain}/{agent.label}")
            steps.append("kickstart")
        entry["action"] = "+".join(steps)
        if self.sys.loaded_path(agent.label) != str(target):
            receipt["failures"].append(f"{name}: not loaded from {target} after bootstrap")
        self.event("support_agent_applied", f"{name} {entry['action']}", label=agent.label)

    def drop(self, receipt: Dict[str, Any], declared: List[str], present: bool) -> None:
        prev = self.previous()
        if not present or not prev or not prev.get("table_present") or not prev.get("table_sha256"):
            return
        for name in prev.get("declared") or []:
            if name in declared or name not in REGISTRY:
                continue
            agent = REGISTRY[name]
            target = self.sys.agents_dir / f"{agent.label}.plist"
            ok, why = self.sys.domain_guard(target)
            if not ok:
                receipt["failures"].append(f"drop {name}: {why}")
                continue
            self.sys.launchctl_run("bootout", f"gui/{os.getuid()}/{agent.label}")
            aside = None
            if target.is_file():
                day = dt.datetime.fromtimestamp(self.sys.now(), dt.timezone.utc).strftime("%Y%m%d")
                folder = self.sys.home / ".local" / "share" / f"tartci-support-agents.retired-{day}"
                folder.mkdir(parents=True, exist_ok=True)
                aside = folder / target.name
                os.replace(target, aside)
            receipt["dropped"].append({"name": name, "label": agent.label,
                                       "moved_to": str(aside) if aside else None})
            self.event("support_agent_dropped", f"{name} booted out; plist moved aside",
                       label=agent.label, moved_to=str(aside) if aside else None)

    def finish(self, receipt: Dict[str, Any], code: int) -> int:
        receipt["exit_code"] = code
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.receipt_path.with_name(f".last.json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n")
        os.replace(tmp, self.receipt_path)
        changed = [n for n, e in receipt["agents"].items()
                   if e.get("state") in ("differs", "missing")]
        self.event("support_agents_pass", f"mode={receipt['mode']} exit={code} changed={changed} "
                   f"undeclared={[u['label'] for u in receipt['undeclared']]}",
                   mode=receipt["mode"], exit_code=code)
        return code


def check_templates(profile_path: Path, sys_: System) -> Tuple[bool, str]:
    """Self-update's pre-drain check: every declared agent renders from this checkout."""
    data, why = load_profile(profile_path)
    if data is None:
        return False, f"profile unreadable: {why}"
    table = data.get(TABLE)
    if not isinstance(table, dict):
        return True, "no [support_agents] table"
    for name in table.get("declared", []):
        agent = REGISTRY[name]
        if not agent.template.is_file():
            return False, f"{name}: missing template {agent.template}"
        rendered, err = sys_.render(agent, data)
        if rendered is None:
            return False, f"{name}: render failed: {err}"
    return True, f"{len(table.get('declared', []))} declared agents render"


# ── status for the doctor ──────────────────────────────────────────────────

def status(state_dir: Path) -> Dict[str, Any]:
    path = state_dir / "last.json"
    if not path.exists():
        return {"state": "never"}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return {"state": "unreadable", "error": str(exc)}
    if not isinstance(value, dict):
        return {"state": "unreadable", "error": "receipt is not an object"}
    if value.get("refused") and value.get("table_present") is None:
        return dict(value, state="unreadable", error=value["refused"])
    agents = value.get("agents") or {}
    only = value.get("bootstrap_only")
    if value.get("mode") == "apply" and not only:
        # An applied pass converged everything except what it reported failing.
        changes = sorted({f.split(":")[0].replace("drop ", "") for f in value.get("failures") or []})
    else:
        changes = sorted(n for n, e in agents.items()
                         if e.get("state") not in ("match_bytes", "match_plist")
                         or not e.get("loaded") or e.get("leaked"))
        if only and value.get("mode") == "apply":
            # The named agents were applied: only their failures remain.
            failed = {f.split(":")[0] for f in value.get("failures") or []}
            changes = sorted(n for n in changes if n not in only or n in failed)
    state = "ok" if not changes else ("drift" if value.get("bootstrap") else "pending")
    return dict(value, state=state, changes=changes)


# ── entry point ────────────────────────────────────────────────────────────

def default_state_dir() -> Path:
    return Path(os.environ.get("TARTCI_SUPPORT_AGENTS_DIR",
                               str(Path.home() / ".tartci" / "state" / "support-agents")))


def main(argv: Optional[Sequence[str]] = None, sys_: Optional[System] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["plan", "apply", "auto", "check-templates", "status"])
    parser.add_argument("--profile-file", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    profile = args.profile_file or default_profile_path()
    state_dir = args.state_dir or default_state_dir()
    sys_ = sys_ or System(Path.home(), os.environ.get("TARTCI_LAUNCHCTL_BIN", "/bin/launchctl"))
    if args.command == "status":
        value = status(state_dir)
        print(json.dumps(value, sort_keys=True, default=str) if args.json else value["state"])
        return 0
    if args.command == "check-templates":
        ok, why = check_templates(profile, sys_)
        print(why)
        return EXIT_OK if ok else EXIT_REFUSED
    code = Converger(sys_, profile, state_dir).run(args.command)
    value = status(state_dir)
    if args.json:
        print(json.dumps(value, sort_keys=True, default=str))
    else:
        print(f"support agents: {value['state']} (mode={value.get('mode')}, exit={code})")
        for name, entry in sorted((value.get("agents") or {}).items()):
            print(f"  {name}: {entry.get('state')}"
                  + (f" action={entry['action']}" if entry.get("action") else "")
                  + "".join(f"\n    {d['path']}: {d['installed']!r} -> {d['rendered']!r}"
                            for d in entry.get("diff") or []))
        for item in value.get("undeclared") or []:
            print(f"  undeclared: {item['label']} ({item['plist']}); reported, never touched")
        if value.get("refused"):
            print(f"  refused: {value['refused']}")
    return code


if __name__ == "__main__":
    sys.exit(main())
