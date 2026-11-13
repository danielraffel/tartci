#!/usr/bin/env python3
"""The canary gate and steward check for tool-freshness's automatic applies.

Before this, every host applied a new Shipyard or pulp release on its own once
the release was 30 minutes old, with nothing between hosts, and nothing looked
at the merge steward after a Shipyard swap. A release that verified on the
host but broke the steward reached every host within one watchdog interval.

Roles. fleet/advertised-labels.json carries `tool_rollout` (generated from
fleet/tool-rollout.json): the `canary` host id and the `steward_hosts`, the
hosts that run the merge steward. Every host reads the same committed file, so
they cannot disagree about who goes first. A missing or unreadable role list
fails closed: no host applies.

Canary gate. The canary applies a release as before (30 minutes after it is
published). Every other host applies it only once the canary's events.jsonl
shows it landed and stayed healthy:

  - a `tool_deployed` event for the tool's CLI with `to` equal to the release
    and a `verify` of current or behind (the re-read install equals it); a
    deploy first recorded with another verify is re-recorded on the canary's
    next pass that measures it;
  - after the newest such deploy, no `tool_apply_failed` for it, no
    `tool_rolled_back` or `tool_steward_failed` for it, and no downgrade away
    from it;
  - then a health signal. When the canary runs the merge steward (its deploy
    event carries `steward_host: true`), a `tool_steward_verified` for the
    release recorded after that deploy. Otherwise `canary_soak_hours` (2) on
    the canary's clock since the deploy.

For a tool with a steward check (Shipyard), a host that is neither the canary
nor a steward host also waits for every published steward host to have
deployed the release and recorded `tool_steward_verified` for it, so a release
that breaks the steward stops at the steward host. The steward host itself
waits only for the canary.

Other hosts are read over fleet ssh, once per pass each. Reading fails closed:
an unreadable host, an unsafe ssh target or an unknown own host id means
wait, with one line per pass in canary-gate.log. Of the soaked candidates the
newest one every required host cleared is applied, so a canary that skipped a
release does not strand the fleet on it. There is no urgent-release escape:
none exists in Shipyard or pulp releases, and an operator who must move a host
now runs the fleet-update by hand.

Steward check. On a host whose summary log has a tick line from the last
hour, a Shipyard deploy is recorded in steward.json with the time of the swap.
Later passes judge it by complete ticks only: a tick is complete once a later
tick stamp exists or every repository in fleet-repos.txt has its final line.
A tick judges the release when it started after the swap and either ran it
(`ver=`) or could not run the steward at all (only `ERROR rc=` lines, no
pass). The baseline is the last tick known to have finished before the swap
(a later tick also started before it), so a tick in flight at the swap never
hides a regression. The first judging tick with every invariant ok and no
error line verifies it (`tool_steward_verified`). The first one with a failure
the baseline did not have fails it: an automatic apply is rolled back with
fleet-update to the release that ran before, recorded as `tool_apply_failed`
and `tool_rolled_back`, and that release is not retried on this host. A
failure on a deploy made by hand is recorded (`tool_steward_failed`) but not
rolled back. `invariants=ERROR` (the tick could not read its own pass output)
and a tick with only some repositories erroring are inconclusive: they neither
verify nor fail, and the next complete tick decides.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

import fleet_source

CANARY_SOAK_HOURS = 2.0
# A summary-log tick line this recent means this host runs the steward.
STEWARD_ACTIVE_HOURS = 1.0
# A deploy no tick has judged after this long is a problem (never a rollback).
STEWARD_WAIT_HOURS = 1.0
STEWARD_LOG = "Library/Logs/shipyard-steward-tick/summary.log"
STEWARD_REPOS = ".local/share/shipyard-steward/fleet-repos.txt"
STEWARD_TAIL_BYTES = 512 * 1024
ROLLBACK_MAX_TRIES = 3
EVENTS_TAIL_LINES = 4000
GATE_LOG_KEEP = 2000
SSH_ALIAS_CONVENTION = "tartci-{host_id}"
SUPPORT_ROOT = Path(__file__).resolve().parent.parent
PUBLISHED = SUPPORT_ROOT / "fleet" / "advertised-labels.json"
STEWARD_ERROR = "steward_could_not_run"

TICK_RE = re.compile(r"^(\d{8}T\d{6}Z) (\S+) (.*)$")
TOKEN_RE = re.compile(r"(?:^| )(ver|invariants)=(\S*)")
# A repository's final line in a tick: its pass, its ERROR, or a skip.
FINAL_RE = re.compile(r"^(rc=|ERROR |skipped=|missing config clone)")


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(text: Any) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    for fmt, size in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y%m%dT%H%M%S", 15)):
        try:
            return dt.datetime.strptime(text[:size], fmt).replace(
                tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            continue
    return None


# ── roles: who is the canary, who runs the steward, who am I ───────────────

def _published() -> dict:
    try:
        value = json.loads(PUBLISHED.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def rollout(settings: dict) -> dict:
    """{"canary", "steward_hosts", "error"} from the published supply.

    `settings["tool_rollout"]` replaces the file (load_settings fills it from
    the file; tests inject it). tool-freshness.toml cannot set it.
    """
    value = settings.get("tool_rollout")
    if value is None:
        value = _published().get("tool_rollout")
    if not isinstance(value, dict) or not isinstance(value.get("canary"), str) \
            or not value["canary"]:
        return {"canary": None, "steward_hosts": [],
                "error": f"no tool_rollout canary in {PUBLISHED}"}
    hosts = value.get("steward_hosts") or []
    return {"canary": value["canary"],
            "steward_hosts": [h for h in hosts if isinstance(h, str) and h], "error": None}


def self_host_id(home: Path, settings: dict) -> str | None:
    """This host's fleet id: the installed fleet profile's host.id, else host_class."""
    profile = home / ".config" / "tartci" / "macos-fleet-profile.toml"
    try:
        text = profile.read_text()
    except OSError:
        text = ""
    in_host = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_host = stripped == "[host]"
            continue
        match = re.fullmatch(r'id\s*=\s*"([^"]+)"\s*(#.*)?', stripped)
        if in_host and match:
            return match.group(1)
    value = settings.get("host_class")
    return str(value) if value else None


def host_target(host_id: str, settings: dict) -> str:
    """The SSH target for a fleet host: `ssh_targets` override, the published row, the convention."""
    overrides = settings.get("ssh_targets")
    if isinstance(overrides, dict) and isinstance(overrides.get(host_id), str):
        return overrides[host_id].strip()
    for row in _published().get("hosts", []):
        if isinstance(row, dict) and row.get("host_id") == host_id and row.get("ssh"):
            return str(row["ssh"])
    return SSH_ALIAS_CONVENTION.format(host_id=host_id)


# ── reading another host's events ──────────────────────────────────────────

def _parse_events(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def read_host(host_id: str, me: str | None, settings: dict, state: Path, now: float,
              run: Callable[[list[str], float], tuple[int, str]]) -> dict:
    """{"events", "clock"} on success, {"error"} when the host is unreadable."""
    if host_id == me:
        try:
            text = (state / "events.jsonl").read_text()
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            return {"error": f"own events.jsonl unreadable: {exc}"}
        return {"events": _parse_events(text), "clock": now}
    target = host_target(host_id, settings)
    destination = fleet_source.ssh_destination(target)
    if destination is None:
        return {"error": f"ssh destination {target!r} refused (not a safe SSH destination)"}
    rc, out = run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--",
                   destination,
                   'date +%s; f="${TARTCI_HOME:-$HOME/.tartci}/state/tool-freshness/events.jsonl"; '
                   f'if [ -e "$f" ]; then tail -n {EVENTS_TAIL_LINES} "$f"; fi'], 60)
    first, _, rest = out.partition("\n")
    if rc != 0 or not first.strip().isdigit():
        tail = " ".join(out.split())[-160:]
        return {"error": f"ssh {destination} failed (exit {rc}): {tail}"}
    return {"events": _parse_events(rest), "clock": float(first.strip())}


def clearance(events: list[dict], clock: float, tool: str, version: str,
              soak_hours: float, require_steward: bool = False) -> tuple[bool, str]:
    """(whether a host's events clear `version` of `tool`, why).

    `require_steward` asks for a clean steward tick whatever the deploy event
    says (a published steward host).
    """
    deploys = [e for e in events if e.get("event") == "tool_deployed" and e.get("tool") == tool
               and e.get("component", "cli") == "cli" and e.get("to") == version
               and e.get("verify") in ("current", "behind") and _epoch(e.get("at")) is not None]
    if not deploys:
        return False, f"has not deployed {version}"
    deploy = deploys[-1]
    since = _epoch(deploy["at"]) or 0.0
    for event in events:
        at = _epoch(event.get("at"))
        if event.get("tool") != tool or at is None or at < since:
            continue
        kind = event.get("event")
        if kind == "tool_apply_failed" and event.get("target") == version:
            return False, f"recorded tool_apply_failed for {version} at {event['at']}"
        if kind in ("tool_rolled_back", "tool_steward_failed") and version in (
                event.get("from"), event.get("version")):
            return False, f"recorded {kind} for {version} at {event['at']}"
        if (kind == "tool_deployed" and event.get("from") == version
                and event.get("downgrade")):
            return False, f"moved back from {version} to {event.get('to')} at {event['at']}"
    if deploy.get("steward_host") or require_steward:
        for event in events:
            if (event.get("event") == "tool_steward_verified" and event.get("tool") == tool
                    and event.get("version") == version
                    and (_epoch(event.get("at")) or 0.0) >= since):
                return True, f"deployed {version} and a steward tick on it was clean"
        return False, f"deployed {version} at {deploy['at']}; waiting for a clean steward tick"
    hours = (clock - since) / 3600
    if hours >= soak_hours:
        return True, f"deployed {version} {hours:.1f} h ago with no failure or rollback"
    return False, (f"deployed {version} {hours:.1f} h ago; healthy once "
                   f"{soak_hours:g} h pass with no failure or rollback")


def gate_candidate(name: str, tool: dict, soaked: list[dict], home: Path, now: float,
                   settings: dict, state: Path,
                   run: Callable[[list[str], float], tuple[int, str]],
                   context: dict) -> tuple[dict | None, str]:
    """(candidate this host may apply, note). The canary applies the newest soaked
    candidate; every other host the newest one every host it waits for cleared."""
    roles = rollout(settings)
    newest = soaked[-1]
    if roles["error"]:
        return None, f"waiting: {roles['error']}"
    canary = roles["canary"]
    me = self_host_id(home, settings)
    if not me:
        # Fail closed: a host that cannot name itself cannot tell whether it is
        # the canary or a steward host, so it never applies a gated release.
        return None, ("waiting: this host's id is unknown (install a fleet profile "
                      "or set host_class in tool-freshness.toml)")
    if me == canary:
        return newest, f"canary {canary}: applies first"
    # (host, needs a clean steward tick): the canary, then for a tool with a
    # steward check every steward host other than this one.
    required = [(canary, False)]
    if tool.get("steward_check"):
        required += [(host, True) for host in roles["steward_hosts"]
                     if host not in (me, canary)]
    reads = context.setdefault("reads", {})
    soak = float(settings.get("canary_soak_hours", CANARY_SOAK_HOURS))
    for host, _ in required:
        if host not in reads:
            reads[host] = read_host(host, me, settings, state, now, run)
        if "error" in reads[host]:
            role = "canary" if host == canary else "steward host"
            return None, f"waiting for {role} {host}: unreadable ({reads[host]['error']})"
    why = ""
    for candidate in reversed(soaked):
        version = str(candidate.get("version"))
        notes, blocked = [], None
        for host, steward in required:
            ok, why_one = clearance(reads[host]["events"], reads[host]["clock"], name, version,
                                    soak, require_steward=steward)
            role = "canary" if host == canary else "steward host"
            if not ok:
                blocked = f"{role} {host}: {candidate.get('tag')} {why_one}"
                break
            notes.append(f"{role} {host}: {why_one}")
        if blocked is None:
            return candidate, "cleared by " + "; ".join(notes)
        if candidate is newest:
            why = blocked
    return None, f"waiting for {why}"


def log_gate(state: Path, now: float, notes: list[str]) -> None:
    """One line per pass in canary-gate.log naming every tool that waited."""
    if not notes:
        return
    path = state / "canary-gate.log"
    try:
        state.mkdir(parents=True, exist_ok=True)
        lines = path.read_text().splitlines() if path.exists() else []
        line = " ".join("; ".join(notes).split())  # always exactly one line
        lines = lines[-(GATE_LOG_KEEP - 1):] + [f"{_iso(now)} {line}"]
        path.write_text("\n".join(lines) + "\n")
    except OSError:
        pass


# ── the steward check ──────────────────────────────────────────────────────

def _home_path(home: Path, raw: str) -> Path:
    path = Path(raw.replace("{home}", str(home))).expanduser()
    return path if path.is_absolute() else home / path


def steward_log(home: Path, tool: dict) -> Path:
    return _home_path(home, str(tool.get("steward_log") or STEWARD_LOG))


def steward_repos(home: Path, tool: dict) -> set[str]:
    """The repositories every tick passes over (fleet-repos.txt); empty when unreadable."""
    try:
        text = _home_path(home, str(tool.get("steward_repos") or STEWARD_REPOS)).read_text()
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")}


def _read_tail(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - STEWARD_TAIL_BYTES))
            data = handle.read()
    except OSError:
        return None
    text = data.decode("utf-8", "replace")
    return text if size <= STEWARD_TAIL_BYTES else text.partition("\n")[2]


def parse_ticks(text: str) -> list[dict]:
    """One dict per tick stamp, oldest first.

    `versions` holds the `ver=` each pass ran, `invariants` each pass's and the
    fleet line's token (ok, FAIL:<id>,..., off, ERROR), `done` the repositories
    whose final line is written, `passes` and `errors` how many of those were
    a pass and an `ERROR rc=` line.
    """
    ticks: dict[str, dict] = {}
    for line in text.splitlines():
        match = TICK_RE.match(line)
        if not match:
            continue
        stamp, subject, rest = match.groups()
        tick = ticks.setdefault(stamp, {"at": _epoch(stamp), "versions": set(),
                                        "invariants": [], "done": set(),
                                        "passes": 0, "errors": 0})
        tokens = dict(TOKEN_RE.findall(" " + rest))
        if tokens.get("ver"):
            tick["versions"].add(tokens["ver"])
        if "invariants" in tokens:
            tick["invariants"].append(tokens["invariants"])
        if "/" in subject and FINAL_RE.match(rest):
            tick["done"].add(subject)
            if rest.startswith("rc="):
                tick["passes"] += 1
            elif rest.startswith("ERROR "):
                tick["errors"] += 1
    return [ticks[stamp] for stamp in sorted(ticks) if ticks[stamp]["at"] is not None]


def _failures(tick: dict) -> set[str]:
    """Failure ids a tick shows: failing invariants, or a steward that could not run."""
    failed = set()
    for token in tick["invariants"]:
        if token.startswith("FAIL:"):
            failed.update(part for part in token[5:].split(",") if part)
    if tick["errors"] and not tick["passes"]:
        failed.add(STEWARD_ERROR)
    return failed


def _clean(tick: dict) -> bool:
    """Every token ok (off ignored), at least one ok, and no repository errored."""
    tokens = [t for t in tick["invariants"] if t != "off"]
    return bool(tokens) and all(t == "ok" for t in tokens) and not tick["errors"]


def runs_steward(home: Path, tool: dict, now: float) -> bool:
    text = _read_tail(steward_log(home, tool))
    if not text:
        return False
    return any(t["versions"] and now - t["at"] <= STEWARD_ACTIVE_HOURS * 3600
               for t in parse_ticks(text))


def steward_verdict(home: Path, tool: dict, version: str, deployed_at: float
                    ) -> tuple[str, str]:
    """("ok" | "failed" | "pending", detail) from the first conclusive complete tick."""
    text = _read_tail(steward_log(home, tool))
    if text is None:
        return "pending", "steward summary log unreadable"
    ticks = parse_ticks(text)
    repos = steward_repos(home, tool)
    # A tick is complete once a later one started (ticks run one at a time) or
    # every fleet repository has its final line.
    complete = [i < len(ticks) - 1 or bool(repos and repos <= ticks[i]["done"])
                for i in range(len(ticks))]
    # The baseline must have finished before the swap: the tick after it
    # started at or before the swap. The last tick stamped before the swap may
    # still have been running when the binary changed, so it never qualifies.
    baseline = [ticks[i] for i in range(len(ticks) - 1)
                if ticks[i + 1]["at"] <= deployed_at]
    preexisting = _failures(baseline[-1]) if baseline else set()
    stamp = lambda t: _iso(t["at"])  # noqa: E731
    for i, tick in enumerate(ticks):
        if tick["at"] < deployed_at or not complete[i]:
            continue
        ran_it = version in tick["versions"]
        could_not_run = not tick["versions"] and tick["errors"] and not tick["passes"]
        if not (ran_it or could_not_run):
            continue
        failed = _failures(tick) - preexisting
        if failed:
            return "failed", (f"steward tick {stamp(tick)} on {version}: "
                              + ("the steward could not run (only ERROR lines)"
                                 if failed == {STEWARD_ERROR} else
                                 f"invariants FAIL {','.join(sorted(failed))}"))
        if _clean(tick):
            return "ok", f"steward tick {stamp(tick)} on {version}: invariants=ok"
    return "pending", f"no conclusive complete steward tick on {version} since {_iso(deployed_at)}"


def record_deploy(steward: dict, name: str, version: str, previous: str | None,
                  by: str, swapped_at: float) -> None:
    steward[name] = {"version": version, "from": previous, "by": by,
                     "deployed_at": _iso(swapped_at)}


def steward_pass(name: str, tool: dict, row: dict, home: Path, now: float, settings: dict,
                 steward: dict, attempts: dict, append: Callable[[dict], None],
                 rollback: Callable[[str], tuple[bool, str]]) -> bool:
    """Judge a recorded deploy against the steward's ticks. True when rolled back.

    `rollback(tag)` runs the governed rollout to `tag` and returns (landed, detail).
    """
    entry = steward.get(name)
    if not isinstance(entry, dict):
        return False
    if entry.get("gave_up"):
        if row.get("effective") != entry.get("version"):
            steward.pop(name, None)  # someone moved the host off it
            return False
        row["steward_problem"] = (f"{name} {entry.get('version')}: steward failed and "
                                  f"{ROLLBACK_MAX_TRIES} rollbacks to v{entry.get('from')} did "
                                  "not land; roll back by hand")
        return False
    version, deployed_at = str(entry.get("version")), _epoch(entry.get("deployed_at"))
    if deployed_at is None:
        steward.pop(name, None)
        return False
    verdict, detail = steward_verdict(home, tool, version, deployed_at)
    if verdict == "ok":
        append({"event": "tool_steward_verified", "tool": name, "at": _iso(now),
                "version": version, "detail": detail})
        steward.pop(name, None)
        row["steward"] = detail
        return False
    if verdict == "pending":
        hours = (now - deployed_at) / 3600
        row["steward"] = detail
        if hours > STEWARD_WAIT_HOURS:
            row["steward_problem"] = (f"{name} {version}: {detail} after {hours:.1f} h "
                                      "(is the steward tick running?)")
        return False
    if entry.get("by") != "auto_apply" or not entry.get("from"):
        append({"event": "tool_steward_failed", "tool": name, "at": _iso(now),
                "version": version, "detail": detail, "by": entry.get("by")})
        steward.pop(name, None)
        row["steward_problem"] = f"{name} {version}: {detail} (deployed by hand; not rolled back)"
        return False
    tag = f"v{entry['from']}"
    landed, result = rollback(tag)
    tries = int(entry.get("rollback_tries") or 0) + 1
    attempts[name] = {"target": f"v{version}", "at": _iso(now), "rejected": True,
                      "result": f"FAILED ({detail}); rolled back to {tag}"}
    if not entry.get("failure_recorded"):
        append({"event": "tool_apply_failed", "tool": name, "at": _iso(now),
                "from": entry["from"], "target": version, "verdict": "steward_invariants",
                "detail": detail})
        entry["failure_recorded"] = True
    append({"event": "tool_rolled_back", "tool": name, "at": _iso(now), "from": version,
            "to": entry["from"], "landed": landed, "detail": result[-300:], "reason": detail})
    if landed:
        steward.pop(name, None)
    else:
        entry["rollback_tries"] = tries
        entry["gave_up"] = tries >= ROLLBACK_MAX_TRIES
    row["steward_problem"] = (f"{name} {version}: {detail}; rollback to {tag} "
                              + ("landed" if landed else f"FAILED (try {tries}): {result[-160:]}"))
    return True
