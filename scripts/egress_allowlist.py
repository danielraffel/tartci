#!/usr/bin/env python3
"""GitHub's published self-hosted-runner egress set, as Softnet allow rules.

A lint-class Linux VM boots with `--net-softnet-block=0.0.0.0/0` and an allow
list derived here, never typed into a config, from two sources:

  1. `GET /meta`: the `web`, `api`, `git` and `actions` IPv4 ranges;
  2. the documented self-hosted-runner hostnames (GitHub's "Accessible domains
     by function"), resolved on the host, several times so anycast answers are
     unioned. meta's ranges do not cover the Front Door hosts the runner's job
     long-poll uses, so source 1 alone fails jobs.

The host resolver is trusted; the guest's is not. Softnet filters IPv4 only, so
IPv6 is dropped and the guest boots with IPv6 disabled.

This is not "GitHub only". The `actions` key carries the Azure ranges the
runner uploads logs and results to, several thousand CIDRs. It is the set a
self-hosted runner needs to work, and the docs and receipts call it that.

  egress_allowlist.py fetch  [--cache FILE] [--max-age-hours N] [--hard-limit-hours N]
                                                                   # refresh if stale, print summary JSON
  egress_allowlist.py rules  [--cache FILE]                        # comma-joined CIDRs for --net-softnet-allow
  egress_allowlist.py check  [--cache FILE] [--fresh-meta FILE] [--lead-hours N]

Reading /meta must not depend on a person's GitHub login, and a rate limit or a
lost credential must not stop the lane. `fetch` reads /meta first through
TARTCI_META_GH_CLI when the fleet provides a credentialed CLI for it, then
anonymously with the cached ETag (a 304 confirms the cached ranges without
re-downloading them). If both fail, the lane keeps booting on the verified cache
until it is --hard-limit-hours old (7 days), printing a WARN each time; past
that, or with no cache, it refuses with an ALERT that names the fix. A /meta
answer the derivation refuses (a missing key, a non-public range) is not a
transport failure and still refuses at once.

`check` compares the cached set with a fresh read. It fails on a CIDR that is in
the cache but no longer published once the cache is older than the lead window
(GitHub announces meta changes ahead of enforcement), and on a published CIDR
missing from a cache older than the refresh window.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

META_KEYS = ("web", "api", "git", "actions")
# The concrete names a runner and the lint jobs reach, from
# https://docs.github.com/en/actions/reference/runners/self-hosted-runners
# ("Accessible domains by function"). Wildcards are listed by the members the
# runner uses; *.blob.core.windows.net is already inside meta's actions ranges.
# Adding a name widens the lane and is a reviewed change.
RUNNER_HOSTNAMES = (
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "pipelines.actions.githubusercontent.com",
    "broker.actions.githubusercontent.com",
    "pkg.actions.githubusercontent.com",
    "token.actions.githubusercontent.com",
    "results-receiver.actions.githubusercontent.com",
    "objects.githubusercontent.com",
    "objects-origin.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "github-registry-files.githubusercontent.com",
    "release-assets.githubusercontent.com",
)
RESOLVE_ROUNDS = 3
DEFAULT_CACHE = Path(os.environ.get(
    "TARTCI_EGRESS_CACHE",
    str(Path.home() / ".tartci" / "state" / "github-egress-allowlist.json"),
))
DEFAULT_MAX_AGE_HOURS = 24
DEFAULT_HARD_LIMIT_HOURS = 7 * 24
DEFAULT_LEAD_HOURS = 72
META_URL = os.environ.get("TARTCI_META_URL", "https://api.github.com/meta")
FIX_HINT = ("Fix: restore a /meta read (set TARTCI_META_GH_CLI to the fleet's credentialed "
            "GitHub CLI, or let the shared anonymous rate limit reset), then run "
            "`scripts/egress_allowlist.py fetch`.")


class MetaUnavailable(RuntimeError):
    """Every /meta source failed in transport: rate limit, auth, network."""


class ResolveFailed(ValueError):
    """A documented runner hostname did not resolve on the host."""


def fetch_meta(etag: str | None = None) -> tuple[dict | None, str | None, str]:
    """Read GET /meta. Returns (meta, etag, source); meta is None on a 304.

    The credentialed CLI is tried first, when configured; the anonymous request
    carries the cached ETag so an unchanged /meta costs a 304.
    """
    errors = []
    cli = os.environ.get("TARTCI_META_GH_CLI")
    if cli:
        try:
            out = subprocess.run([cli, "api", "meta"], capture_output=True, text=True, timeout=60)
            if out.returncode == 0:
                return json.loads(out.stdout), None, f"cli:{Path(cli).name}"
            errors.append(f"{Path(cli).name}: {out.stderr.strip()[:160]}")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            errors.append(f"{Path(cli).name}: {exc}")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "tartci-egress-allowlist"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        with urllib.request.urlopen(urllib.request.Request(META_URL, headers=headers), timeout=30) as resp:
            return json.loads(resp.read()), resp.headers.get("ETag"), "anonymous"
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and etag:
            return None, exc.headers.get("ETag") or etag, "anonymous-304"
        errors.append(f"anonymous: HTTP {exc.code} {exc.reason}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        errors.append(f"anonymous: {exc}")
    raise MetaUnavailable("GET /meta failed: " + "; ".join(errors))


def resolve_hostnames(names=RUNNER_HOSTNAMES, rounds=RESOLVE_ROUNDS, resolver=None) -> dict[str, list[str]]:
    """IPv4 addresses for each name, unioned over several lookups."""
    import socket
    lookup = resolver or (lambda name: [a[4][0] for a in socket.getaddrinfo(name, 443, socket.AF_INET)])
    out: dict[str, list[str]] = {}
    for name in names:
        seen: set[str] = set()
        for _ in range(rounds):
            try:
                seen.update(lookup(name))
            except OSError:
                pass
        if not seen:
            raise ResolveFailed(f"documented runner hostname {name} did not resolve")
        out[name] = sorted(seen, key=ipaddress.ip_address)
    return out


def derive(meta: dict, hostnames: dict[str, list[str]] | None = None) -> list[str]:
    nets = []
    for addresses in (hostnames or {}).values():
        nets.extend(ipaddress.ip_network(f"{a}/32") for a in addresses
                    if ipaddress.ip_address(a).version == 4)
    for key in META_KEYS:
        values = meta.get(key)
        if not isinstance(values, list) or not values:
            raise ValueError(f"meta key {key!r} is missing or empty")
        for cidr in values:
            net = ipaddress.ip_network(cidr, strict=False)
            if net.version == 4:
                nets.append(net)
    collapsed = sorted(ipaddress.collapse_addresses(nets))
    for net in collapsed:
        # A default route in the allow list would void the block entirely.
        # is_global also excludes 100.64.0.0/10 (shared address space: the tailnet).
        if net.prefixlen == 0 or not net.is_global:
            raise ValueError(f"refusing a non-public allow rule from meta: {net}")
    return [str(n) for n in collapsed]


def meta_digest(meta: dict) -> str:
    return hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest()


def build_record(meta: dict, fetched_at: float,
                 hostnames: dict[str, list[str]] | None = None,
                 etag: str | None = None, meta_source: str | None = None) -> dict:
    cidrs = derive(meta, hostnames)
    joined = ",".join(cidrs)
    kept = {key: list(meta[key]) for key in META_KEYS}
    return {
        "schema": 3,
        "meta": kept,
        "meta_sha256": meta_digest(kept),
        "etag": etag,
        "meta_source": meta_source,
        "source": "api.github.com/meta + documented runner hostnames resolved on the host",
        "keys": list(META_KEYS),
        "hostnames": hostnames or {},
        "name": "GitHub's published self-hosted-runner egress set",
        "fetched_at": int(fetched_at),
        "resolved_at": int(fetched_at),
        "cidr_count": len(cidrs),
        "sha256": hashlib.sha256(joined.encode()).hexdigest(),
        "cidrs": cidrs,
    }


def load(cache: Path) -> dict | None:
    try:
        record = json.loads(cache.read_text())
    except (OSError, ValueError):
        return None
    joined = ",".join(record.get("cidrs", []))
    if hashlib.sha256(joined.encode()).hexdigest() != record.get("sha256"):
        return None  # a hand-edited or truncated cache is not an allowlist
    if record.get("schema", 2) >= 3:
        # The ranges must be exactly what the kept /meta copy and hostnames
        # derive, so neither can be edited alone, and a 304 never rebuilds from
        # an altered copy.
        meta = record.get("meta")
        if not isinstance(meta, dict) or meta_digest(meta) != record.get("meta_sha256"):
            return None
        try:
            if derive(meta, record.get("hostnames") or {}) != record["cidrs"]:
                return None
        except (ValueError, TypeError):
            return None
    return record


def save(cache: Path, record: dict) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1))
    tmp.replace(cache)


def summary(record: dict) -> dict:
    return {k: record.get(k) for k in ("name", "source", "keys", "fetched_at", "resolved_at",
                                       "cidr_count", "sha256", "meta_source")} | {
        "hostnames": sorted(record.get("hostnames", {}))}


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def cmd_fetch(args) -> int:
    record = load(args.cache)
    now = time.time()
    age_h = (now - record["fetched_at"]) / 3600 if record else None
    if record is not None and age_h <= args.max_age_hours:
        print(json.dumps(summary(record)))
        return 0
    try:
        # Only a cache that kept /meta itself can be rebuilt from a 304.
        etag = record.get("etag") if record and "meta" in record else None
        meta, new_etag, source = fetch_meta(etag)
        if meta is None:
            meta = record["meta"]
        fresh = build_record(meta, now, resolve_hostnames(), etag=new_etag, meta_source=source)
    except (MetaUnavailable, ResolveFailed) as exc:
        if record is None or age_h > args.hard_limit_hours:
            state = "no verified cache" if record is None else f"the cached allowlist is {age_h:.0f}h old"
            print(f"ALERT egress allowlist: {exc}; {state} (limit {args.hard_limit_hours:.0f}h), "
                  f"so the lint lane refuses to boot. {FIX_HINT}", file=sys.stderr)
            return 3
        print(f"WARN egress allowlist: {exc}; booting on the verified cache from "
              f"{iso(record['fetched_at'])} ({age_h:.0f}h old, refused after "
              f"{args.hard_limit_hours:.0f}h). {FIX_HINT}", file=sys.stderr)
        print(json.dumps(summary(record) | {"stale": True, "age_hours": round(age_h, 1),
                                            "refresh_error": str(exc)}))
        return 0
    save(args.cache, fresh)
    print(json.dumps(summary(fresh)))
    return 0


def cmd_rules(args) -> int:
    record = load(args.cache)
    if record is None:
        print("no valid egress allowlist cache; run `fetch` first", file=sys.stderr)
        return 2
    print(",".join(record["cidrs"]))
    return 0


def cmd_check(args) -> int:
    record = load(args.cache)
    if record is None:
        print(json.dumps({"ok": False, "reason": "no valid cache"}))
        return 1
    fresh_meta = json.loads(Path(args.fresh_meta).read_text()) if args.fresh_meta else fetch_meta()[0]
    fresh_hosts = (json.loads(Path(args.fresh_hostnames).read_text()) if args.fresh_hostnames
                   else resolve_hostnames())
    fresh = set(derive(fresh_meta, fresh_hosts))
    cached = set(record["cidrs"])
    age_h = (time.time() - record["fetched_at"]) / 3600
    withdrawn = sorted(cached - fresh)
    added = sorted(fresh - cached)
    problems = []
    if withdrawn and age_h > args.lead_hours:
        problems.append(f"{len(withdrawn)} withdrawn CIDRs still allowed after {age_h:.0f}h")
    if added and age_h > args.max_age_hours:
        problems.append(f"{len(added)} published CIDRs missing from a {age_h:.0f}h-old cache")
    print(json.dumps({"ok": not problems, "age_hours": round(age_h, 1),
                      "withdrawn": len(withdrawn), "added": len(added), "problems": problems}))
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch", "rules", "check"):
        p = sub.add_parser(name)
        p.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
        p.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
        if name == "fetch":
            p.add_argument("--hard-limit-hours", type=float, default=DEFAULT_HARD_LIMIT_HOURS)
        if name == "check":
            p.add_argument("--fresh-meta")
            p.add_argument("--fresh-hostnames", help="JSON {name: [ipv4...]} instead of resolving")
            p.add_argument("--lead-hours", type=float, default=DEFAULT_LEAD_HOURS)
    args = parser.parse_args(argv)
    return {"fetch": cmd_fetch, "rules": cmd_rules, "check": cmd_check}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
