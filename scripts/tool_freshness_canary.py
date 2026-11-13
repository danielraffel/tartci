#!/usr/bin/env python3
"""The canary gate and steward check for tool-freshness's automatic applies.

Before this, every host applied a new Shipyard or pulp release on its own once
the release was 30 minutes old, with nothing between hosts, and nothing looked
at the merge steward after a Shipyard swap. A release that verified on the
host but broke the steward reached every host within one watchdog interval.

Canary gate. One host (`canary_host` in tool-freshness.toml, default m5, the
least loaded build host) applies as before. Every other host applies a
release only once the canary's events.jsonl shows it landed and stayed
healthy:

  - a `tool_deployed` event for the tool's CLI with `to` equal to the release
    and a `verify` of current or behind (the re-read install equals it);
  - after that deploy, no `tool_apply_failed` for it, no `tool_rolled_back`
    or `tool_steward_failed` for it, and no downgrade away from it;
  - then a health signal. When the canary runs the merge steward (its deploy
    event carries `steward_host: true`), a `tool_steward_verified` event for
    the release: a steward tick on it with invariants=ok. Otherwise
    `canary_soak_hours` (2) on the canary's clock since the deploy.

A non-canary host reads the canary's events over fleet ssh, once per pass.
It fails closed: an unreadable canary, an unknown own host id, or a canary
that has not cleared any candidate means wait, with one line per pass in
canary-gate.log. Of the soaked candidates the newest one the canary cleared is
applied, so a canary that skipped a release does not strand the fleet on it.
There is no urgent-release escape: none exists in Shipyard or pulp releases,
and an operator who must move a host now runs the fleet-update by hand.

Steward check. On a host that runs the steward (its summary log has a tick
line from the last hour), a Shipyard deploy is recorded in steward.json. Each
later pass reads the tick summary log for ticks that started after the deploy
and ran the new version (`ver=`). The first tick with every `invariants=` ok
verifies it (`tool_steward_verified`). The first tick with a failure that was
not already failing on the last tick before the deploy fails it: an automatic
apply is rolled back with fleet-update to the previous release, recorded as
`tool_apply_failed` and `tool_rolled_back`, and that release is not retried on
this host. A failure on a deploy made by hand is recorded
(`tool_steward_failed`) but not rolled back.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

import fleet_source

CANARY_HOST = "m5"
CANARY_SOAK_HOURS = 2.0
# A summary-log tick line this recent means this host runs the steward.
STEWARD_ACTIVE_HOURS = 1.0
# A deploy no tick has judged after this long is a problem (never a rollback).
STEWARD_WAIT_HOURS = 1.0
STEWARD_LOG = "Library/Logs/shipyard-steward-tick/summary.log"
STEWARD_TAIL_BYTES = 512 * 1024
ROLLBACK_MAX_TRIES = 3
EVENTS_TAIL_LINES = 4000
GATE_LOG_KEEP = 2000
SSH_ALIAS_CONVENTION = "tartci-{host_id}"
SUPPORT_ROOT = Path(__file__).resolve().parent.parent

TICK_RE = re.compile(r"^(\d{8}T\d{6}Z) (\S+) (.*)$")
TOKEN_RE = re.compile(r"(?:^| )(ver|invariants)=(\S+)")


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


def _version(text: Any) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", str(text or ""))
    return tuple(int(part) for part in match.groups()) if match else None


# ── who is the canary, and who am I ────────────────────────────────────────

def canary_host(settings: dict) -> str:
    value = settings.get("canary_host")
    return value.strip() if isinstance(value, str) and value.strip() else CANARY_HOST


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


def canary_target(canary: str, settings: dict) -> str:
    """The SSH target for the canary: `canary_ssh`, the published host row, the convention."""
    if isinstance(settings.get("canary_ssh"), str) and settings["canary_ssh"].strip():
        return settings["canary_ssh"].strip()
    try:
        supply = json.loads((SUPPORT_ROOT / "fleet" / "advertised-labels.json").read_text())
    except (OSError, json.JSONDecodeError):
        supply = {}
    for row in supply.get("hosts", []) if isinstance(supply, dict) else []:
        if isinstance(row, dict) and row.get("host_id") == canary and row.get("ssh"):
            return str(row["ssh"])
    return SSH_ALIAS_CONVENTION.format(host_id=canary)


# ── reading the canary ─────────────────────────────────────────────────────

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


def read_canary(home: Path, settings: dict, state: Path, now: float,
                run: Callable[[list[str], float], tuple[int, str]]) -> dict:
    """{"events", "clock"} on success, {"error"} when the canary is unreadable."""
    canary = canary_host(settings)
    if canary == self_host_id(home, settings):
        try:
            text = (state / "events.jsonl").read_text()
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            return {"error": f"own events.jsonl unreadable: {exc}"}
        return {"events": _parse_events(text), "clock": now}
    target = canary_target(canary, settings)
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
              soak_hours: float) -> tuple[bool, str]:
    """(whether the canary has cleared `version` of `tool`, why)."""
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
    if deploy.get("steward_host"):
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


def gate_candidate(name: str, soaked: list[dict], home: Path, now: float, settings: dict,
                   state: Path, run: Callable[[list[str], float], tuple[int, str]],
                   context: dict) -> tuple[dict | None, str]:
    """(candidate this host may apply, note). The canary applies the newest soaked
    candidate; every other host the newest one the canary has cleared."""
    canary = canary_host(settings)
    me = self_host_id(home, settings)
    newest = soaked[-1]
    if me == canary:
        return newest, f"canary {canary}: applies first"
    if "canary" not in context:
        context["canary"] = read_canary(home, settings, state, now, run)
    read = context["canary"]
    who = f"canary {canary}" + ("" if me else " (this host's id is unknown)")
    if "error" in read:
        return None, f"waiting for {who}: unreadable ({read['error']})"
    soak = float(settings.get("canary_soak_hours", CANARY_SOAK_HOURS))
    why = ""
    for candidate in reversed(soaked):
        ok, why_one = clearance(read["events"], read["clock"], name,
                                str(candidate.get("version")), soak)
        if ok:
            return candidate, f"cleared by {who}: {why_one}"
        if candidate is newest:
            why = why_one
    return None, f"waiting for {who}: {newest.get('tag')} {why}"


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

def steward_log(home: Path, tool: dict) -> Path:
    raw = str(tool.get("steward_log") or STEWARD_LOG)
    path = Path(raw.replace("{home}", str(home))).expanduser()
    return path if path.is_absolute() else home / path


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
    """[{"at", "versions", "invariants"}] per tick stamp, oldest first.

    `invariants` holds each repository's and the fleet line's token (ok,
    FAIL:<id>,..., off, ERROR); `versions` the `ver=` each repository ran.
    """
    ticks: dict[str, dict] = {}
    for line in text.splitlines():
        match = TICK_RE.match(line)
        if not match:
            continue
        stamp, _, rest = match.groups()
        tokens = dict(TOKEN_RE.findall(" " + rest))
        if not tokens:
            continue
        tick = ticks.setdefault(stamp, {"at": _epoch(stamp), "versions": set(),
                                        "invariants": []})
        if "ver" in tokens:
            tick["versions"].add(tokens["ver"])
        if "invariants" in tokens:
            tick["invariants"].append(tokens["invariants"])
    return [ticks[stamp] for stamp in sorted(ticks)]


def _failed_ids(tokens: list[str]) -> set[str]:
    failed = set()
    for token in tokens:
        if token.startswith("FAIL:"):
            failed.update(part for part in token[5:].split(",") if part)
        elif token not in ("ok", "off"):
            failed.add(token)  # ERROR or anything unrecognized
    return failed


def runs_steward(home: Path, tool: dict, now: float) -> bool:
    text = _read_tail(steward_log(home, tool))
    if not text:
        return False
    recent = [t for t in parse_ticks(text) if t["versions"] and t["at"] is not None
              and now - t["at"] <= STEWARD_ACTIVE_HOURS * 3600]
    return bool(recent)


def steward_verdict(home: Path, tool: dict, version: str, deployed_at: float
                    ) -> tuple[str, str]:
    """("ok" | "failed" | "pending", detail) for the first judging tick on `version`."""
    text = _read_tail(steward_log(home, tool))
    if text is None:
        return "pending", "steward summary log unreadable"
    ticks = [t for t in parse_ticks(text) if t["at"] is not None]
    before = [t for t in ticks if t["at"] < deployed_at and t["invariants"]]
    preexisting = _failed_ids(before[-1]["invariants"]) if before else set()
    stamp = lambda t: _iso(t["at"])  # noqa: E731
    for tick in ticks:
        if tick["at"] < deployed_at or version not in tick["versions"]:
            continue
        failed = _failed_ids(tick["invariants"])
        if failed and not failed <= preexisting:
            return "failed", (f"steward tick {stamp(tick)} on {version}: invariants FAIL "
                              f"{','.join(sorted(failed - preexisting))}")
        if not failed and "ok" in tick["invariants"]:
            return "ok", f"steward tick {stamp(tick)} on {version}: invariants=ok"
    return "pending", f"no judging steward tick on {version} since {_iso(deployed_at)}"


def record_deploy(steward: dict, name: str, version: str, previous: str | None,
                  by: str, now: float) -> None:
    steward[name] = {"version": version, "from": previous, "by": by,
                     "deployed_at": _iso(now)}


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
