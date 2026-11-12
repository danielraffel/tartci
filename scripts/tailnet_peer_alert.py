#!/usr/bin/env python3
"""Tell someone when a fleet host cannot be reached the way the fleet expects.

m1 was off Tailscale for about 4 h on 2026-10-14 (roughly 20:30Z to 00:47Z):
Tailscale had been turned off on the machine. Nothing alerted. It surfaced
only as Shipyard lanes, agent test runs and `shipyard who` stalling on ssh,
and its LAN fallback alias pointed at an address m1 no longer had.

Every watchdog pass, each fleet host reads its own `tailscale status --json`
and probes every route the fleet SSH table (danielraffel/fleet-ssh
fleet/hosts.tsv) lists for each other host: the tailnet address, the
Ethernet (`<host>-lan`) and the Wi-Fi (`<host>-wifi`) address. A probe is one
`ssh-keyscan` with a 5 s timeout: it proves the address answers AND that the
answer carries the host key known_hosts holds for that host, so a stale
address now owned by another machine reads `wrong_host`, not up. Everything
is read-only: nothing starts, stops or reconfigures Tailscale, touches
tailnet DNS or ACLs, or edits ssh config.

Episodes, each alerted after OFFLINE_ALERT_SECS (10 min):

- offline: a host's tailnet node reads Online=false (from its LastSeen).
- route_down: the node reads Online=true but this host's tailnet probe to it
  fails. That is the case a working LAN fallback hides: fleet-ssh quietly
  falls back to Ethernet and nothing else notices.
- local: this host's own tailscaled is not Running (Stopped, NeedsLogin,
  unreadable). A host off the tailnet judges no peers; GitHub is reached
  without Tailscale, so it still reports itself.
- local_route_down: control says two or more peers are online and this host
  reaches none of them over the tailnet, so the broken path is this host's.
- a fallback route (`-lan` or `-wifi`) that the table lists is down, answers
  with another host's key, or this host's ssh alias for it names a different
  address than the table.

Every address comes from the fleet-ssh table, a private repository read
through ghapp; this repository holds none. A host is matched to its tailnet
node by the table's tailscale address. A published host the table does not
list, or whose address no node has, is reported, never guessed. A route the
table marks optional (a laptop's Ethernet, present only when docked) is
probed and shown by --status but opens no issue.

Issues go through the peer-alert path (peer_stall_alert): one issue per host
for the tailnet (`[tartci] <host> is off the tailnet`) and one per broken
fallback route, opened or adopted by exact title. For another host only one
published host acts: the lowest published id, other than that host, whose
tailnet route this host sees up. Every body ends with the tartci-incident
marker; on recovery the marker is PATCHed with fixed_at and the issue
closes. A failed PATCH or close is retried on the next pass.
"""
from __future__ import annotations

import base64
import concurrent.futures
import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import fleet_source
import gate_job_demand
import host_off
import tartci_incident

OFFLINE_ALERT_SECS = 10 * 60
CLI_TIMEOUT_SECS = 20
APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
TITLE = "[tartci] {host} is off the tailnet"
INCIDENT_CLASS = "tailnet_offline"
ROUTE_TITLE = "[tartci] {alias} fallback route to {host} is broken"
ROUTE_CLASS = "fallback_route_broken"
FLEET_SSH_REPO = "danielraffel/fleet-ssh"
TABLE_PATH = "fleet/hosts.tsv"
TABLE_TTL_SECS = 3600
PROBE_TIMEOUT_SECS = 5
FALLBACK_KINDS = ("lan", "wifi")
# Read-only: lists this host's IPv4 address per interface.
ADDRESSES_CMD = ("for i in $(ifconfig -l); do a=$(ipconfig getifaddr $i 2>/dev/null) "
                 "&& echo \"$i $a\"; done")

Runner = Callable[[List[str]], Tuple[int, str, str]]


def setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


def state_root() -> pathlib.Path:
    override = os.environ.get("TARTCI_TAILNET_ALERT_DIR")
    if override:
        return pathlib.Path(override)
    home = os.environ.get("TARTCI_HOME") or str(pathlib.Path.home() / ".tartci")
    return pathlib.Path(home) / "state" / "tailnet"


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def epoch(value: Any) -> Optional[float]:
    """Seconds for an RFC 3339 time; None for missing or Go's zero time."""
    if not isinstance(value, str) or not value or value.startswith("0001-"):
        return None
    try:
        text = value.replace("Z", "+00:00")
        # Tailscale writes fractional seconds of any length; keep six digits.
        if "." in text:
            head, _, rest = text.partition(".")
            count = len(rest) - len(rest.lstrip("0123456789"))
            digits, zone = rest[:count], rest[count:]
            text = f"{head}.{(digits + '000000')[:6]}{zone}"
        return dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def cli() -> Optional[str]:
    """The Tailscale CLI. It is not on a launchd PATH (docs/gotchas.md)."""
    explicit = os.environ.get("TARTCI_TAILSCALE_CLI")
    if explicit:
        return explicit
    if os.path.exists(APP_CLI):
        return APP_CLI
    return shutil.which("tailscale")


def _run(argv: List[str]) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=CLI_TIMEOUT_SECS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 255, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def read_status(run: Optional[Runner] = None) -> Dict[str, Any]:
    """{"ok": bool, "status": dict | None, "error": str}. Read-only."""
    path = cli()
    if not path:
        return {"ok": False, "status": None, "error": "no tailscale CLI found"}
    rc, out, err = (run or _run)([path, "status", "--json"])
    try:
        value = json.loads(out) if out.strip() else None
    except ValueError:
        value = None
    if isinstance(value, dict):
        # A stopped or logged-out daemon still answers, with its BackendState.
        return {"ok": True, "status": value, "error": ""}
    return {"ok": False, "status": None,
            "error": f"exit {rc}: {(err or out).strip()[:200]}"}


def local_state(read: Dict[str, Any]) -> Tuple[bool, str]:
    """(this host is on the tailnet, its state as text)."""
    if not read.get("ok"):
        return False, f"tailscaled unreadable ({read.get('error')})"
    state = str((read.get("status") or {}).get("BackendState") or "unknown")
    return state == "Running", state


def nodes_by_ip(status: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Each tailnet IPv4 -> its node (Self included, marked)."""
    out: Dict[str, Dict[str, Any]] = {}
    rows = [dict(status.get("Self") or {}, _self=True)] + [
        node for node in (status.get("Peer") or {}).values() if isinstance(node, dict)]
    for node in rows:
        for ip in node.get("TailscaleIPs") or []:
            out[str(ip)] = node
    return out


# ── the fleet SSH table and route probes ────────────────────────────────────

def parse_table(text: str) -> Dict[str, Dict[str, Any]]:
    """fleet-ssh hosts.tsv:
    name|aliases|tailscale|lan|wifi|user|identity_file[|optional].

    An empty field means that route does not exist for the host. The optional
    eighth column lists the fallback routes (lan, wifi) that may be absent
    without that being a fault, space-separated."""
    rows: Dict[str, Dict[str, Any]] = {}
    for line in (text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 5 or not fields[0]:
            continue
        optional = set(fields[7].split()) & set(FALLBACK_KINDS) if len(fields) > 7 else set()
        rows[fields[0]] = {"name": fields[0], "tailscale": fields[2],
                           "lan": fields[3], "wifi": fields[4], "optional": sorted(optional)}
    return rows


def _fetch_table(run: Optional[Runner] = None) -> str:
    """The table from GitHub, through ghapp in the self-update checkout as the
    other alert paths call it."""
    args = ["api", f"repos/{FLEET_SSH_REPO}/contents/{TABLE_PATH}", "--jq", ".content"]
    if run is not None:
        rc, out, err = run(["ghapp", *args])
    else:
        rc, out = host_off._ghapp(args, host_off._update_checkout())
        err = out
    if rc != 0:
        raise RuntimeError(f"ghapp exit {rc}: {(err or out).strip()[:200]}")
    return base64.b64decode(out).decode()


def load_table(directory: pathlib.Path, now: float,
               fetch: Optional[Callable[[], str]] = None) -> Tuple[Dict[str, Dict[str, str]], str]:
    """(the table, a note). A copy younger than TABLE_TTL_SECS is used as is;
    otherwise it is fetched, and a failed fetch falls back to the last copy."""
    cached = directory / "hosts.tsv"
    try:
        age = now - cached.stat().st_mtime
    except OSError:
        age = None
    if age is not None and 0 <= age < TABLE_TTL_SECS:
        return parse_table(cached.read_text()), ""
    try:
        text = (fetch or _fetch_table)()
        if not parse_table(text):
            raise RuntimeError("no rows")
        cached.write_text(text)
        return parse_table(text), ""
    except Exception as exc:  # noqa: BLE001 - an unreadable table degrades, never stops the pass
        if age is not None:
            return (parse_table(cached.read_text()),
                    f"fleet-ssh {TABLE_PATH} unreadable ({exc}); using a copy {int(age // 60)} min old")
        return {}, f"fleet-ssh {TABLE_PATH} unreadable ({exc}); fallback routes not checked"


def fleet_routes(published: Any,
                 table: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """host_id -> {"name", "listed", "tailscale", "lan", "wifi", "optional"}.

    A table row belongs to its canonical id (m3 is studio, m5s is m5studio).
    Rows for machines tartci does not publish (macmini, macpro) are hosts
    too; a published host with no row is kept, unlisted."""
    def blank() -> Dict[str, Any]:
        return {"name": None, "listed": False, "tailscale": "", "lan": "", "wifi": "",
                "optional": []}
    out: Dict[str, Dict[str, Any]] = {host: blank() for host in published}
    for name, row in sorted(table.items()):
        entry = out.setdefault(gate_job_demand.canonical_host(name), blank())
        entry.update(name=name, listed=True, tailscale=row["tailscale"], lan=row["lan"],
                     wifi=row["wifi"], optional=list(row.get("optional") or []))
    return out


def known_keys(alias: str, run: Optional[Runner] = None) -> set:
    """(type, blob) pairs known_hosts holds for `alias` (the HostKeyAlias)."""
    path = os.path.expanduser("~/.ssh/known_hosts")
    rc, out, _ = (run or _run)(["ssh-keygen", "-F", alias, "-f", path])
    keys = set()
    for line in out.splitlines() if rc == 0 else []:
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#"):
            keys.add((parts[-2], parts[-1]))
    return keys


def probe(addr: str, alias: Optional[str], run: Optional[Runner] = None) -> Dict[str, str]:
    """One read-only probe: {"status": up|down|wrong_host, "detail"}."""
    rc, out, err = (run or _run)(["ssh-keyscan", "-T", str(PROBE_TIMEOUT_SECS), "-t", "ed25519",
                                  addr])
    seen = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#"):
            seen.add((parts[1], parts[2]))
    if not seen:
        tail = (err or "").strip().splitlines()
        return {"status": "down",
                "detail": f"no ssh answer from {addr} in {PROBE_TIMEOUT_SECS} s"
                + (f" ({tail[-1][:120]})" if tail else "")}
    expected = known_keys(alias, run) if alias else set()
    if expected and not seen & expected:
        return {"status": "wrong_host",
                "detail": f"{addr} answers with a host key that is not {alias}'s"}
    return {"status": "up", "detail": f"{addr} answers as {alias}" if expected
            else f"{addr} answers (no known key for {alias} to compare)"}


def alias_address(alias: str, run: Optional[Runner] = None) -> Optional[str]:
    """The HostName this host's ssh config resolves `alias` to (ssh -G, read-only)."""
    rc, out, _ = (run or _run)(["ssh", "-G", alias])
    for line in out.splitlines() if rc == 0 else []:
        key, _, value = line.partition(" ")
        if key == "hostname":
            return value.strip()
    return None


def probe_all(fleet: Dict[str, Dict[str, Any]], me: Optional[str], on_tailnet: bool,
              run: Optional[Runner] = None) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """(host, kind) -> probe result for every route of every other host, in
    parallel. The tailnet route is probed only while this host is on it."""
    jobs: Dict[Tuple[str, str], Tuple[str, Optional[str]]] = {}
    for host, entry in fleet.items():
        if host == me:
            continue
        alias = entry.get("name") or host
        for kind in ("tailscale",) + FALLBACK_KINDS:
            addr = entry.get(kind)
            if addr and (kind != "tailscale" or on_tailnet):
                jobs[(host, kind)] = (addr, alias)
    if not jobs:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        futures = {key: pool.submit(probe, addr, alias, run) for key, (addr, alias) in jobs.items()}
        out = {key: dict(future.result(), addr=jobs[key][0]) for key, future in futures.items()}
    for (host, kind), result in out.items():
        name = fleet[host].get("name")
        if kind in FALLBACK_KINDS and name:
            result["alias"] = f"{name}-{kind}"
            result["alias_addr"] = alias_address(result["alias"], run)
    return out


def peer_addresses(name: str, run: Optional[Runner] = None) -> Optional[str]:
    """The addresses a host reports for itself, read over its default fleet
    route with a read-only command. None when it cannot be read."""
    rc, out, _ = (run or _run)(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                                name, ADDRESSES_CMD])
    rows = [line.strip() for line in out.splitlines() if line.strip()] if rc == 0 else []
    return ", ".join(rows) or None


# ── judging ──────────────────────────────────────────────────────────────────

def _episode(prior: Dict[str, Any], state: str, now: float, threshold: int,
             seen: Optional[float] = None) -> Dict[str, Any]:
    """since/minutes/active for a condition first seen at `seen`, else when
    the previous pass first recorded `state`, else now."""
    first = prior.get("since") if prior.get("state") == state else None
    since = seen or epoch(first) or now
    return {"since": iso(since), "minutes": int((now - since) // 60),
            "active": now - since >= threshold}


def judge_routes(entry: Dict[str, Any], results: Dict[str, Dict[str, Any]],
                 prior: Dict[str, Any], now: float, threshold: int) -> Dict[str, Dict[str, Any]]:
    """kind -> route verdict for one host's fallback routes."""
    routes: Dict[str, Dict[str, Any]] = {}
    for kind in FALLBACK_KINDS:
        result = results.get(kind)
        if not entry.get(kind) or result is None:
            continue
        status = result["status"]
        stale = result.get("alias_addr") not in (None, result["addr"])
        if status == "up" and stale:
            status = "stale_alias"
        optional = kind in (entry.get("optional") or [])
        row = {"addr": result["addr"], "alias": result.get("alias"),
               "alias_addr": result.get("alias_addr"), "status": status,
               "detail": result.get("detail", ""), "active": False, "since": None,
               "minutes": None, "resolved": status == "up" or optional,
               "optional": optional}
        if status != "up" and not optional:
            held = (prior.get("routes") or {}).get(kind) or {}
            first = held.get("since") if held.get("status") not in (None, "up") else None
            since = epoch(first) or now
            row.update(since=iso(since), minutes=int((now - since) // 60),
                       active=now - since >= threshold)
        routes[kind] = row
    return routes


def judge(read: Dict[str, Any], hosts: Any, me: Optional[str],
          now: float, held: Dict[str, Any], threshold: int,
          probes: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
          fleet: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    """One verdict per host: {"state": online|offline|route_down|unmapped|
    unknown|local|local_route_down|no_tailnet, "active", "resolved", "since",
    "last_seen", "minutes", "node", "ip", "detail", "routes"}.

    `held` is the previous pass's verdicts, which carry when each condition
    was first seen. `probes` maps (host, route kind) to a probe result.
    """
    probes = probes or {}
    fleet = fleet if fleet is not None else fleet_routes(hosts, {})
    on_tailnet, local = local_state(read)
    status = read.get("status") or {}
    nodes = nodes_by_ip(status) if on_tailnet else {}
    out: Dict[str, Dict[str, Any]] = {}
    for host, entry in sorted(fleet.items()):
        ip = entry.get("tailscale") or None
        prior = held.get(host) if isinstance(held.get(host), dict) else {}
        row: Dict[str, Any] = {"ip": ip, "active": False, "resolved": False, "since": None,
                               "last_seen": None, "minutes": None, "node": None, "detail": "",
                               "name": entry.get("name"), "routes": {}}
        if host != me:
            row["routes"] = judge_routes(
                entry, {kind: probes[(host, kind)] for kind in FALLBACK_KINDS
                        if (host, kind) in probes}, prior, now, threshold)
        if host == me:
            if on_tailnet:
                row.update(state="online", resolved=True)
            else:
                row.update(state="local", detail=local,
                           **_episode(prior, "local", now, threshold))
            out[host] = row
            continue
        if not entry.get("listed"):
            row.update(state="unmapped", detail="no row in the fleet-ssh table")
            out[host] = row
            continue
        if not ip:
            row.update(state="no_tailnet", resolved=True, detail="no tailnet route listed")
            out[host] = row
            continue
        if not on_tailnet:
            # This host cannot see the tailnet, so it says nothing about peers.
            row.update(state="unknown", detail=f"this host's tailscaled: {local}")
            out[host] = row
            continue
        node = nodes.get(str(ip))
        if node is None:
            row.update(state="unmapped", detail=f"no tailnet node has {ip}")
            out[host] = row
            continue
        row["node"] = str(node.get("DNSName") or node.get("HostName") or "").rstrip(".")
        if node.get("Online") is False:
            seen = epoch(node.get("LastSeen"))
            row.update(state="offline", last_seen=iso(seen) if seen else None,
                       **_episode(prior, "offline", now, threshold, seen))
            out[host] = row
            continue
        result = probes.get((host, "tailscale"))
        if result is not None and result["status"] != "up":
            row.update(state="route_down",
                       detail=f"Online per control, but the tailnet path failed: {result['detail']}",
                       **_episode(prior, "route_down", now, threshold))
            out[host] = row
            continue
        row.update(state="online", resolved=True)
        out[host] = row

    # Every peer control calls online fails from here: the broken path is ours.
    claimed = [h for h, v in out.items() if h != me and v["state"] in ("online", "route_down")]
    if me in out and on_tailnet and len(claimed) >= 2 \
            and all(out[h]["state"] == "route_down" for h in claimed):
        prior = held.get(me) if isinstance(held.get(me), dict) else {}
        out[me].update(state="local_route_down", resolved=False,
                       detail=f"control lists {len(claimed)} peers online and none answers "
                              "over the tailnet from this host",
                       **_episode(prior, "local_route_down", now, threshold))
        for h in claimed:
            out[h].update(state="unknown", active=False, since=None, minutes=None,
                          detail="this host reaches no peer over the tailnet")

    # Every fallback route of two or more hosts the tailnet reaches is dead
    # from here, so the LAN is unreachable from this host's tartci, not from
    # them, while the tailnet (utun) still works (m5s, 2026-10-15, with an
    # interactive shell on the same host reaching the LAN). One other host is
    # not enough: a single dead LAN is that host's own fault.
    lan_hosts = [h for h, v in out.items()
                 if h != me and v["state"] == "online" and v["routes"]]
    if me in out and len(lan_hosts) >= 2 and all(
            route["status"] == "down" for h in lan_hosts for route in out[h]["routes"].values()):
        prior = held.get(me) if isinstance(held.get(me), dict) else {}
        blind = prior.get("lan_blind") if isinstance(prior.get("lan_blind"), dict) else {}
        out[me]["lan_blind"] = dict(
            detail=f"every Ethernet and Wi-Fi route of {len(lan_hosts)} hosts the tailnet "
                   "reaches is down from this host",
            **_episode({"since": blind.get("since"), "state": "blind"} if blind else {},
                       "blind", now, threshold))
        for h, v in out.items():
            for route in (v.get("routes") or {}).values():
                route.update(status="unchecked", active=False, resolved=False, since=None,
                             minutes=None, detail="this host cannot reach the LAN")
    return out


def acting_reader(peer: str, verdicts: Dict[str, Dict[str, Any]],
                  readers: Optional[set] = None) -> Optional[str]:
    """The host that alerts for `peer`: the lowest published id, other than
    the peer, whose tailnet route reads up from here."""
    online = sorted(h for h, v in verdicts.items()
                    if h != peer and v.get("state") == "online"
                    and (readers is None or h in readers))
    return online[0] if online else None


def alert_text(host: str, verdict: Dict[str, Any], me: str, now: float,
               detected_at: str) -> Tuple[str, str]:
    """(title, body): what, evidence, likely cause, fix, safe to auto-apply,
    how to verify, then the tartci-incident marker."""
    state = verdict.get("state")
    hours = round((verdict.get("minutes") or 0) / 60, 1)
    if state == "route_down":
        what = ("Tailscale is on but the tailnet route is down; SSH is falling back to LAN. "
                f"`{host}` reads online in Tailscale's control plane, but {me} has not reached "
                f"it over the tailnet for {verdict['minutes']} min ({hours} h). fleet-ssh "
                "falls back to Ethernet or Wi-Fi, so lanes keep working and the broken tailnet "
                "path goes unnoticed until the fallback fails too.")
        evidence = [f"- tailnet node `{verdict.get('node')}` ({verdict.get('ip')}): Online=true",
                    f"- {verdict['detail']}",
                    f"- failing since {verdict['since']}, probed by {me} at {iso(now)}"]
        evidence += _route_lines(verdict)
        cause = ("The tailnet path itself: a relay/DERP or stuck direct path, a firewall on "
                 "the utun interface, a route or exit-node setting, or an ACL change.")
        fix = [f"{APP_CLI} ping --c 3 {verdict.get('ip')}   # from {me}: direct, DERP, or no reply",
               f"{APP_CLI} netcheck   # on {host}, over its LAN route",
               f"# then on {host}: quit and reopen Tailscale (open -a Tailscale), "
               "and check recent ACL changes"]
    elif state == "local_route_down":
        what = (f"`{host}` reaches none of its peers over the tailnet ({verdict['detail']}) "
                f"for {verdict['minutes']} min. The broken path is on {host} itself.")
        evidence = [f"- {verdict['detail']}", f"- first seen: {verdict['since']}",
                    f"- read by {me} itself at {iso(now)}"]
        cause = ("This host's tailnet path: tailscaled connected to control but its data "
                 "path is stuck, a firewall on utun, or a route or exit-node setting.")
        fix = [f"{APP_CLI} netcheck", f"{APP_CLI} ping --c 3 <a peer's tailnet IP>",
               "# then quit and reopen Tailscale on this host (open -a Tailscale)"]
    elif state == "local":
        what = (f"`{host}` reports its own Tailscale is not running ({verdict['detail']}) "
                f"since {verdict['since']} ({verdict['minutes']} min). Peers cannot reach it "
                "over the tailnet: Shipyard lanes, agent runs and `shipyard who` stall on ssh.")
        evidence = [f"- `tailscale status --json` on {host}: {verdict['detail']}",
                    f"- first seen not running: {verdict['since']}",
                    f"- read by {me} itself at {iso(now)}"]
        cause = ("Tailscale was turned off or quit on the machine (Stopped / daemon "
                 "unreachable), or it is logged out (NeedsLogin).")
        fix = [f"# on {host}, at the machine or over the LAN:",
               "open -a Tailscale   # if the app was quit",
               f"{APP_CLI} up   # Stopped: turn it back on, no flags",
               f"{APP_CLI} login   # NeedsLogin: re-authenticate"]
    else:
        what = (f"`{host}` has been offline on the tailnet for {verdict['minutes']} min "
                f"({hours} h), last seen {verdict.get('last_seen') or 'unknown'}. Fleet hosts "
                "cannot reach it: Shipyard lanes, agent runs and `shipyard who` stall on ssh.")
        evidence = [f"- tailnet node `{verdict.get('node')}` ({verdict.get('ip')}): Online=false",
                    f"- LastSeen: {verdict.get('last_seen') or 'not reported'}; offline since "
                    f"{verdict['since']}",
                    f"- read by {me} from its own `tailscale status --json` at {iso(now)}"]
        evidence += _route_lines(verdict)
        cause = ("One of: Tailscale turned off or quit on the machine (the 2026-10-14 m1 "
                 "case); the machine asleep; the machine without power or network. If the "
                 "host still answers on the LAN, it is Tailscale; if nothing answers, it is "
                 "sleep or power.")
        fix = [f"# from a host on {host}'s LAN: ping its LAN address. An answer means",
               "# Tailscale is off; no answer means it is asleep or without power.",
               f"# Tailscale off: on {host}, open -a Tailscale; then {APP_CLI} up",
               f"# asleep: wake it, then check pmset -g (sleep 0) and System Settings > "
               "Energy > Prevent automatic sleeping",
               "# no power: restore power; the lanes start on boot"]
    body = "\n".join([
        "## What happened", what, "",
        "## Evidence", *evidence, "",
        "## Likely cause", cause, "",
        "## Fix", "```sh", *fix, "```", "",
        "## Safe to auto-apply",
        "No. tartci never starts or stops Tailscale and never changes tailnet DNS or ACLs; "
        "someone at the machine (or on its LAN) has to bring it back.", "",
        "## How to verify",
        f"`{APP_CLI} status` on any fleet host lists `{host}` online, and "
        f"`ssh-keyscan -T 5 {verdict.get('ip') or '<tailnet ip>'}` answers from a peer. This "
        "issue records the fix time on its marker and closes itself when a host reaches it "
        "over the tailnet again.",
    ])
    marker = tartci_incident.marker(host, INCIDENT_CLASS, verdict["since"], detected_at)
    return TITLE.format(host=host), tartci_incident.with_marker(body, marker)


def _route_lines(verdict: Dict[str, Any]) -> List[str]:
    """Which fallback routes reach the host, for an issue's evidence."""
    rows = []
    for kind, route in sorted((verdict.get("routes") or {}).items()):
        rows.append(f"- `{route.get('alias') or kind}` ({route['addr']}): {route['status']}")
    return rows


LAN_BLIND_TITLE = "[tartci] {host} cannot reach the LAN from tartci"
LAN_BLIND_CLASS = "lan_blind"


def lan_blind_text(host: str, blind: Dict[str, Any], now: float,
                   detected_at: str) -> Tuple[str, str]:
    """(title, body) for a host whose tartci cannot reach the LAN at all."""
    body = "\n".join([
        "## What happened",
        f"`{host}`'s tartci has reached no Ethernet or Wi-Fi address of any fleet host since "
        f"{blind['since']} ({blind['minutes']} min), while the same hosts answer over the "
        "tailnet. Its fallback-route checks are blind, so they report nothing.", "",
        "## Evidence", f"- {blind['detail']}",
        f"- `tailnet_peer_alert.py --status` on {host} lists every -lan/-wifi route `unchecked`",
        f"- read by {host} itself at {iso(now)}", "",
        "## Likely cause",
        "If several hosts report this at once, suspect the LAN itself (router or switch) "
        "rather than this host. Otherwise something on this host keeps its tartci process "
        "off the LAN while the tailnet works. Not yet established; candidates to check: the "
        "route the kernel picks for a LAN address (another interface, such as a Thunderbolt "
        "bridge or a link-local address), a firewall (the application firewall or LuLu) "
        "holding the probe binary, or macOS privacy settings (Local Network) for the binary "
        "that runs the probe, for example Homebrew Python, which needs a re-grant after each "
        "upgrade. An interactive ssh session is a different process tree, so a "
        "probe by hand can succeed while this one fails.", "",
        "## Fix", "```sh",
        f"# on {host}, from the watchdog's own context (not an interactive shell):",
        "route -n get <a LAN address from fleet-ssh hosts.tsv>   # interface and gateway",
        "ssh-keyscan -T 5 -t ed25519 <that address>   # the exact error",
        "/usr/libexec/ApplicationFirewall/socketfilterfw --listapps",
        "# then correct whichever of route, firewall or privacy setting is refusing it",
        "```", "",
        "## Safe to auto-apply",
        "No. The cause is a host network or security setting; someone has to look at it.", "",
        "## How to verify",
        f"The next pass on {host} lists -lan and -wifi routes as up; this issue records the "
        "fix time on its marker and closes itself.",
    ])
    marker = tartci_incident.marker(host, LAN_BLIND_CLASS, blind["since"], detected_at)
    return LAN_BLIND_TITLE.format(host=host), tartci_incident.with_marker(body, marker)


def route_alert_text(host: str, kind: str, route: Dict[str, Any], me: str, now: float,
                     detected_at: str, actual: Optional[str]) -> Tuple[str, str]:
    """(title, body) for one broken fallback route."""
    alias = route.get("alias") or f"{host}-{kind}"
    status = route["status"]
    if status == "stale_alias":
        what = (f"`{alias}` in {me}'s ssh config points at {route.get('alias_addr')}, but the "
                f"fleet-ssh table lists {host}'s {kind} address as {route['addr']}.")
        cause = "This host's ssh config predates the current fleet-ssh table."
    elif status == "wrong_host":
        what = (f"`{alias}` ({route['addr']}) answers, but with a host key that is not "
                f"{host}'s: the address now belongs to another machine.")
        cause = f"{host}'s {kind} address changed (DHCP) and the fleet-ssh table still has the old one."
    else:
        what = (f"`{alias}` ({route['addr']}) has not answered for {route['minutes']} min, so "
                f"the {kind} fallback to {host} is dead.")
        cause = (f"{host}'s {kind} address changed (DHCP), its {kind} interface is down, or the "
                 "machine is asleep or without power.")
    evidence = [f"- `{alias}`: {route['detail']}",
                f"- fleet-ssh table: {route['addr']}; this host's alias resolves to "
                f"{route.get('alias_addr') or 'unknown'}",
                f"- {host} reports its addresses as: {actual or 'unreadable from ' + me}",
                f"- broken since {route['since']}, probed by {me} at {iso(now)}"]
    body = "\n".join([
        "## What happened", what, "",
        "## Evidence", *evidence, "",
        "## Likely cause", cause, "",
        "## Fix", "```sh",
        f"# Correct {host}'s {kind} column in {FLEET_SSH_REPO} {TABLE_PATH}"
        + (f" (it reports: {actual})" if actual else ""),
        "# then run fleet/install.sh on every fleet machine", "```", "",
        "## Safe to auto-apply",
        "No. tartci never edits ssh config or the fleet-ssh table; that is Daniel's call.", "",
        "## How to verify",
        f"`ssh -o ConnectTimeout=5 {alias} hostname` from a fleet host prints {host}'s name. "
        "This issue records the fix time on its marker and closes itself when the route "
        "answers as the right host again.",
    ])
    marker = tartci_incident.marker(f"{host}:{kind}", ROUTE_CLASS, route["since"], detected_at)
    return (ROUTE_TITLE.format(alias=alias, host=host),
            tartci_incident.with_marker(body, marker))


def _close_with_fix(fixed_at: str) -> Callable[[str], Tuple[int, str]]:
    """A close that first PATCHes fixed_at into the issue's marker. Any
    failure returns non-zero, so episode_alert retries it next pass."""
    def close(number: str) -> Tuple[int, str]:
        if host_off._scratch_home():
            return 1, "refused: issue writes from a scratch TARTCI_HOME never reach GitHub"
        rc, body = host_off._ghapp(
            ["api", f"repos/{host_off.ISSUE_REPO}/issues/{number}", "--jq", ".body"],
            host_off._update_checkout())
        if rc != 0:
            return rc, body
        try:
            patched = tartci_incident.fixed(body, fixed_at)
        except ValueError:
            patched = None  # adopted without a marker: close it as it is
        if patched is not None:
            rc, text = host_off._patch_issue_body(number, patched)
            if rc != 0:
                return rc, text
        return host_off._close_issue(number)
    return close


def published_hosts(home: Optional[pathlib.Path] = None,
                    run: Optional[Runner] = None) -> Tuple[List[str], Optional[str]]:
    """(published host ids, this host's id or None) from main's published
    supply in the self-update checkout, as peer_stall_alert reads it."""
    home = home or pathlib.Path.home()
    try:
        configured = fleet_source.selected_published_supply()
    except ValueError as exc:
        raise RuntimeError(f"published supply refused: {exc}") from exc
    if configured is not None:
        try:
            value = json.loads(configured.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"published supply unreadable from {configured}: {exc}") from exc
        return _published_host_rows(value, home)
    checkout = home / ".local" / "share" / "tartci" / "update-checkout"
    rc, out, err = (run or _run)(["git", "-C", str(checkout), "show",
                                  "origin/main:fleet/advertised-labels.json"])
    try:
        value = json.loads(out) if rc == 0 else None
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise RuntimeError(f"published supply unreadable from {checkout}: {(err or out)[:200]}")
    return _published_host_rows(value, home)


def _published_host_rows(value: dict, home: pathlib.Path) -> Tuple[List[str], Optional[str]]:
    hosts = sorted({row["host_id"] for row in value.get("hosts") or []
                    if isinstance(row, dict) and isinstance(row.get("host_id"), str)})
    import vm_boot_alert  # noqa: PLC0415 - owns the profile's host id and its fallback
    me, _ = vm_boot_alert._alert_host()
    return hosts, (me if me in hosts else None)


def alert_pass(now: Optional[float] = None, directory: Optional[pathlib.Path] = None,
               hosts: Optional[List[str]] = None, me: Optional[str] = None,
               run: Optional[Runner] = None, issue: Any = None,
               close: Any = None, table_text: Optional[str] = None) -> Dict[str, Any]:
    """The watchdog's pass: {"skipped", "local", "table", "hosts": {host: verdict}}.

    `hosts` are the published host ids (only these read and act);
    `table_text` replaces the fleet-ssh table fetch."""
    now = time.time() if now is None else now
    directory = directory or state_root()
    threshold = setting("TARTCI_TAILNET_OFFLINE_ALERT_SECS", OFFLINE_ALERT_SECS)
    if hosts is None:
        checkout = pathlib.Path.home() / ".local" / "share" / "tartci" / "update-checkout"
        if not (checkout / ".git").exists() and not any(
                os.environ.get(name, "").strip()
                for name in ("TARTCI_ADVERTISED_LABELS", "TARTCI_FLEET_SOURCE_ROOT")):
            return {"skipped": True, "reason": "no self-update checkout", "hosts": {}}
        hosts, me = published_hosts()
    directory.mkdir(parents=True, exist_ok=True)
    last_path = directory / "last-read.json"
    held = (host_off._read_json(last_path) or {}).get("hosts") or {}
    read = read_status(run)
    on_tailnet, local = local_state(read)
    if table_text is not None:
        table, table_note = parse_table(table_text), ""
    else:
        table, table_note = load_table(directory, now,
                                       fetch=(lambda: _fetch_table(run)) if run else None)
    fleet = fleet_routes(hosts, table)
    probes = probe_all(fleet, me, on_tailnet, run)
    verdicts = judge(read, hosts, me, now, held, threshold, probes, fleet)
    readers = set(hosts)
    enabled = (os.environ.get("TARTCI_TAILNET_ISSUE", "1") != "0"
               and (issue is not None or not host_off._scratch_home()))
    opener = issue or _open_or_adopt
    closer = close or _close_with_fix(iso(now))
    detected = iso(now)
    for host, verdict in sorted(verdicts.items()):
        if host == me:
            acting, why = me is not None, "this host itself"
        else:
            reader = acting_reader(host, verdicts, readers)
            acting = me is not None and reader == me
            why = (f"{reader} is the acting reader" if reader and reader != me
                   else "acting reader" if acting else "no reader reaches the tailnet")
        verdict["acting"], verdict["why"] = acting, why
        state_path = directory / f"{host}.json"
        if acting or host_off._read_json(state_path):
            out = host_off.episode_alert(
                state_path, active=verdict["active"] and acting,
                resolved=verdict["resolved"], since=verdict["since"],
                raise_event=lambda host=host, verdict=verdict: host_off.event(
                    directory, "tailnet_offline",
                    f"{host}: {verdict['state']} since {verdict['since']} "
                    f"({verdict['detail'] or 'offline'})",
                    {"host": host, "state": verdict["state"], "since": verdict["since"],
                     "reader": me, "last_seen": verdict.get("last_seen")}, now),
                render=lambda host=host, verdict=verdict: alert_text(
                    host, verdict, me or "a fleet host", now, detected),
                issue=opener, close=closer, issues_enabled=enabled)
            verdict["issue"], verdict["closed"] = out.get("issue"), bool(out.get("closed"))
        if host == me:
            blind = verdict.get("lan_blind")
            blind_path = directory / f"{host}.lan-blind.json"
            if blind or host_off._read_json(blind_path):
                out = host_off.episode_alert(
                    blind_path, active=bool(blind and blind["active"]),
                    resolved=not blind and bool(verdicts) and any(
                        r.get("status") == "up" for v in verdicts.values()
                        for r in (v.get("routes") or {}).values()),
                    since=blind["since"] if blind else None,
                    raise_event=lambda host=host, blind=blind: host_off.event(
                        directory, "lan_blind", f"{host}: cannot reach the LAN since "
                        f"{blind['since']}", {"host": host, "since": blind["since"]}, now),
                    render=lambda host=host, blind=blind: lan_blind_text(
                        host, blind, now, detected),
                    issue=opener, close=closer, issues_enabled=enabled)
                if blind:
                    blind["issue"] = out.get("issue")
        actual: Dict[str, Optional[str]] = {}
        for kind, route in sorted((verdict.get("routes") or {}).items()):
            route_path = directory / f"{host}.route-{kind}.json"
            if not (acting or host_off._read_json(route_path)):
                continue
            if route["active"] and acting and "value" not in actual and verdict.get("name"):
                actual["value"] = peer_addresses(verdict["name"], run)

            def render(host=host, kind=kind, route=route):
                return route_alert_text(host, kind, route, me or "a fleet host", now, detected,
                                        actual.get("value"))
            out = host_off.episode_alert(
                route_path, active=route["active"] and acting, resolved=route["resolved"],
                since=route["since"],
                raise_event=lambda host=host, kind=kind, route=route: host_off.event(
                    directory, "fallback_route_broken",
                    f"{route.get('alias') or host + '-' + kind}: {route['status']} since "
                    f"{route['since']}",
                    {"host": host, "route": kind, "status": route["status"],
                     "addr": route["addr"], "alias_addr": route.get("alias_addr"),
                     "reader": me}, now),
                render=render, issue=opener, close=closer, issues_enabled=enabled)
            route["issue"], route["closed"] = out.get("issue"), bool(out.get("closed"))
    host_off._write_json(last_path, {"ts": now, "local": local, "on_tailnet": on_tailnet,
                                     "table": table_note or f"{len(table)} rows",
                                     "hosts": verdicts})
    return {"skipped": False, "local": local, "on_tailnet": on_tailnet,
            "table": table_note, "hosts": verdicts}


def route_summary(verdicts: Dict[str, Dict[str, Any]]) -> List[str]:
    """One line per host: which routes reach it from this host."""
    rows = []
    for host, v in sorted(verdicts.items()):
        tail = "tailnet " + {"online": "up", "offline": "OFFLINE", "route_down": "DOWN",
                             "no_tailnet": "none"}.get(v.get("state"), str(v.get("state")))
        for kind, route in sorted((v.get("routes") or {}).items()):
            tail += f", {route.get('alias') or kind} {route['status']}"
            if route.get("optional"):
                tail += " (optional)"
        rows.append(f"{host}: {tail}")
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    """`--status`: the last pass's routes per host (read from its state)."""
    import argparse  # noqa: PLC0415
    parser = argparse.ArgumentParser(description="fleet tailnet and fallback route status")
    parser.add_argument("--status", action="store_true")
    parser.parse_args(argv)
    last = host_off._read_json(state_root() / "last-read.json") or {}
    if not last:
        print("no tailnet pass recorded yet")
        return 1
    print(f"read at {iso(float(last.get('ts') or 0))}; this host: {last.get('local')}; "
          f"table: {last.get('table')}")
    for line in route_summary(last.get("hosts") or {}):
        print(line)
    return 0


def _open_or_adopt(title: str, body: str) -> Tuple[int, str]:
    import peer_stall_alert  # noqa: PLC0415 - owns the open-or-adopt-by-title issue path
    return peer_stall_alert._open_or_adopt(title, body)


if __name__ == "__main__":
    raise SystemExit(main())
