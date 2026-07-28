#!/usr/bin/env python3
"""Fleet drift guard — catch the silent config rot that kills event delivery.

Every check here exists because it FAILED SILENTLY in production on
2026-07-28, and none of the existing surfaces reported it. `tartci doctor`
checks host prereqs, `tartci pool status` lists agents, and `shipyard runner
audit` checks label naming — none of them notices that GitHub has been unable
to reach a host for weeks.

What rotted, and the check that would have caught it:

1. A Tailscale node re-registered with a `-1` suffix. The webhook kept pointing
   at the old name, whose node went OFFLINE FOR 41 DAYS. Every delivery 502'd.
   → `webhook-url-matches-tunnel`
2. The registrar only manages hooks it created, so the renamed node left an
   untracked ORPHAN hook behind, duplicating a working one.
   → `no-orphan-webhooks`
3. `gh` is at /opt/homebrew/bin, which a non-interactive shell's PATH omits.
   A daemon started without it logs `gh CLI not found on PATH` forever and
   registers nothing.
   → `daemon-can-reach-gh`
4. A repo was renamed (`danielraffel/*` → `Generous-Corp/*`). The stale name
   307-redirects, the registrar's PATCH does not follow redirects, and
   registration fails permanently.
   → `no-stale-repo-registrations`
5. A tartci checkout sat on a merged feature branch 13 commits behind `main`,
   so a migration script did not exist on the one host that needed it.
   → `checkout-current`
6. A token helper under /Volumes/* works interactively but fails for every
   launchd job, because TCC denies launchd agents read access there.
   → `token-helper-internal`
7. Tailscale Funnel was never configured on a host, so its daemon could never
   publish a tunnel and thus never registered a webhook at all.
   → `tunnel-active`
8. Two hosts' gate supervisors advertised `pulp-build-studio` where the required
   macOS check asks for `pulp-build-vm`, so they were structurally blind to gate
   work and logged `queued=0` forever. Fleet gate concurrency was 1 instead of 3
   and the merge queue went five hours without landing a PR.
   → `gate-labels-match-required`
9. A failed `migrate_macos_gate_agent.sh` left a host with no gate agent at all,
   invisible to `tartci pool status`, which still listed it.
   → `gate-agent-installed`

Read-only. Never mutates a host or GitHub. Exit 0 clean, 1 drift, 2 usage.

    ./scripts/fleet_preflight.py --repo OWNER/NAME [--repo OWNER/OTHER]
    ./scripts/fleet_preflight.py --repo OWNER/NAME --json
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

# A host serving CI must be able to run these from the daemon's environment.
REQUIRED_ON_PATH = ("gh", "tart")
SEARCH_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                "/usr/sbin", "/sbin")
# The daemon binary itself installs under the user prefix, which is NOT part of
# the minimal PATH above. Kept separate so `daemon-can-reach-*` still judges
# only the system dirs a launchd job actually inherits.
EXEC_DIRS = SEARCH_DIRS + (f"{pathlib.Path.home()}/.local/bin",)


def run(argv: list[str], timeout: int = 25) -> tuple[int, str]:
    """Run a command, returning (rc, combined-output). Never raises."""
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PATH": os.pathsep.join(EXEC_DIRS)},
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out after {timeout}s"
    except OSError as exc:  # pragma: no cover - defensive
        return 1, f"{argv[0]}: {exc}"


class Report:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []

    def add(self, check: str, ok: bool | None, detail: str) -> None:
        # ok=None means "could not determine" — reported as drift, never as
        # healthy. An unverifiable surface must not read as a clean one.
        self.rows.append(
            {"check": check,
             "status": "ok" if ok else ("unknown" if ok is None else "DRIFT"),
             "detail": detail}
        )

    @property
    def failed(self) -> list[dict[str, str]]:
        return [r for r in self.rows if r["status"] != "ok"]


def shipyard_bin() -> str | None:
    for cand in ("shipyard", f"{pathlib.Path.home()}/.local/bin/shipyard"):
        if shutil.which(cand) or pathlib.Path(cand).is_file():
            return cand
    return None


def daemon_status(sy: str) -> dict | None:
    rc, out = run([sy, "daemon", "status", "--json"])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def check_path_tools(rep: Report) -> None:
    """A daemon started from a login-less shell inherits a minimal PATH."""
    for tool in REQUIRED_ON_PATH:
        found = next(
            (d for d in SEARCH_DIRS if pathlib.Path(d, tool).exists()), None
        )
        rep.add(
            f"daemon-can-reach-{tool}",
            bool(found),
            f"{tool} at {found}" if found
            else f"{tool} not in {list(SEARCH_DIRS)} — a daemon launched without "
                 "it registers nothing and logs 'not found on PATH' forever",
        )


def check_checkout(rep: Report) -> None:
    root = pathlib.Path(__file__).resolve().parent.parent
    rc, branch = run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0:
        rep.add("checkout-current", None, "not a git checkout")
        return
    run(["git", "-C", str(root), "fetch", "--quiet", "origin", "main"], timeout=60)
    rc, behind = run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD..origin/main"]
    )
    n = behind.strip() if rc == 0 else "?"
    on_main = branch.strip() == "main"
    current = n == "0"
    rep.add(
        "checkout-current",
        on_main and current,
        f"branch={branch.strip()} behind={n}"
        + ("" if on_main and current
           else " — a stale checkout hides the very scripts that repair drift"),
    )


def check_token_helper(rep: Report) -> None:
    """`token_command[0]` must exist AND be on the internal disk.

    launchd agents are denied read access to /Volumes/* by TCC — `stat`
    succeeds while reads return "Operation not permitted" — so a helper under
    an external volume works interactively and fails for every launchd-run
    job. That asymmetry is why this is worth a check rather than a comment:
    a human testing by hand cannot reproduce the failure.
    """
    cfg = pathlib.Path.home() / "Library/Application Support/shipyard/config.toml"
    if not cfg.exists():
        rep.add("token-helper-internal", None, "no shipyard config.toml")
        return
    text = cfg.read_text(errors="replace")
    helper = None
    # token_command = [ "…/ghapp", "auth", "token" ] — first array element,
    # possibly on the following line. Deliberately no TOML parser: this must
    # import on a stock python3 without tomllib (3.9 ships on these hosts).
    idx = text.find("\ntoken_command")
    if idx != -1:
        for raw in text[idx + 1:].splitlines():
            stripped = raw.strip()
            # Skip comments: the surrounding config is heavily commented and a
            # quoted phrase inside a comment is not the helper path.
            if stripped.startswith("#"):
                continue
            if '"' not in stripped:
                if stripped.startswith("]"):
                    break
                continue
            candidate = stripped.split('"')[1]
            if candidate.startswith("/") or candidate.startswith("~"):
                helper = candidate
                break
            break
    if helper is None:
        rep.add("token-helper-internal", True, "no command helper configured")
        return
    path = pathlib.Path(helper)
    external = str(path).startswith("/Volumes/")
    rep.add(
        "token-helper-internal",
        path.exists() and not external,
        f"{path}"
        + ("" if path.exists() else " — MISSING")
        + (" — on an EXTERNAL VOLUME; TCC denies launchd agents read access "
           "there, so every launchd job fails while manual runs succeed"
           if external else ""),
    )


def check_gate_labels(rep: Report, repos: list[str]) -> None:
    """The gate supervisor must advertise EVERY label the required check asks for.

    GitHub assigns a job only to a runner advertising all of the job's labels.
    A supervisor whose `--labels` omit one of them is *structurally blind*: it
    scans with its own label set, finds nothing it can serve, logs `queued=0`
    forever, and boots no VM — while its host looks perfectly healthy and its
    other runners sit idle.

    This is not hypothetical. On 2026-07-28 two of three hosts advertised
    `pulp-build-studio` where the required macOS gate asks for `pulp-build-vm`,
    so fleet gate concurrency was 1 instead of 3 and the merge queue went five
    hours without landing a PR. Nothing reported it.
    """
    plist = (pathlib.Path.home() / "Library/LaunchAgents"
             / "com.danielraffel.pulp.tart-runner-macos-gate.plist")
    if not plist.exists():
        rep.add("gate-agent-installed", None,
                "no tart-runner-macos-gate plist — this host serves no gate "
                "work unless a legacy tart-runner agent does it")
        return
    text = plist.read_text(errors="replace")
    advertised: set[str] = set()
    for line in text.splitlines():
        if "self-hosted" in line and "<string>" in line:
            advertised = {p.strip() for p in
                          line.split("<string>")[1].split("</string>")[0].split(",")}
            break
    if not advertised:
        rep.add("gate-labels-match-required", None, "could not parse plist labels")
        return

    for repo in repos:
        for client in ("ghapp", "gh"):
            rc, out = run([client, "api",
                           f"repos/{repo}/actions/variables/"
                           "PULP_LOCAL_MACOS_RUNS_ON_JSON"], timeout=30)
            if rc == 0:
                break
        if rc != 0:
            continue  # repo may not define a self-hosted macOS gate
        try:
            required = set(json.loads(json.loads(out)["value"]))
        except Exception:
            continue
        missing = required - advertised
        rep.add(
            f"gate-labels-match-required[{repo}]",
            not missing,
            f"advertises={sorted(advertised)}"
            + (f" MISSING={sorted(missing)} — this host can never take the "
               "required gate job; it will log queued=0 forever while looking "
               "healthy" if missing else ""),
        )
        # Guard the other direction: extra ADVISORY labels on a gate
        # registration let GitHub hand the VM an optional job instead, and a
        # JIT runner cannot be retargeted once registered.
        extra = {l for l in advertised - required if l.endswith("-secondary")}
        if extra:
            rep.add(f"gate-labels-no-advisory[{repo}]", False,
                    f"advisory labels on the gate registration: {sorted(extra)}")


def check_daemon(rep: Report, status: dict | None, repos: list[str]) -> None:
    if status is None:
        rep.add("daemon-running", False,
                "shipyard daemon status unavailable — nothing receives events")
        return
    rep.add("daemon-running", bool(status.get("running")), "")
    tunnel = (status.get("tunnel") or {}).get("url") or ""
    rep.add(
        "tunnel-active",
        bool(tunnel),
        tunnel or "no tunnel — check `tailscale funnel status`; 'No serve "
                  "config' means Funnel was never configured on this host",
    )
    registered = status.get("registered_repos") or []
    missing = [r for r in repos if r not in registered]
    rep.add(
        "repos-registered",
        not missing,
        f"registered={registered}"
        + (f" missing={missing} — the endpoint answers and ignores those events"
           if missing else ""),
    )
    stale = [r for r in registered if r not in repos]
    rep.add(
        "no-stale-repo-registrations",
        not stale,
        f"stale={stale} — a renamed repo 307-redirects and registration fails "
        "permanently" if stale else "",
    )


def check_webhooks(rep: Report, status: dict | None, repos: list[str]) -> None:
    """Compare each repo's hooks against this host's live tunnel URL."""
    tunnel = ((status or {}).get("tunnel") or {}).get("url") or ""
    for repo in repos:
        for client in ("ghapp", "gh"):
            rc, out = run([client, "api", f"repos/{repo}/hooks"], timeout=40)
            if rc == 0:
                break
        if rc != 0:
            rep.add(f"webhooks-readable[{repo}]", None,
                    "cannot read hooks (admin scope?) — unverified, not healthy")
            continue
        try:
            hooks = json.loads(out)
        except json.JSONDecodeError:
            rep.add(f"webhooks-readable[{repo}]", None, "unparseable hook list")
            continue
        urls = [(h.get("config") or {}).get("url", "") for h in hooks
                if h.get("active")]
        mine = [u for u in urls if tunnel and u.startswith(tunnel)]
        rep.add(
            f"webhook-url-matches-tunnel[{repo}]",
            bool(mine) if tunnel else None,
            f"tunnel={tunnel or '(none)'} active_hooks={len(urls)}"
            + ("" if mine else
               " — no hook points at this host's live tunnel URL; a renamed "
               "node leaves the old URL 502ing forever"),
        )
        # Any hook whose host resolves to nothing we know about is an orphan.
        # We can only judge our own; flag exact-duplicate hosts as suspicious.
        hosts = [u.split("//")[-1].split("/")[0] for u in urls]
        dupes = {h for h in hosts if hosts.count(h) > 1}
        rep.add(
            f"no-orphan-webhooks[{repo}]",
            not dupes,
            f"duplicate endpoint hosts={sorted(dupes)}" if dupes
            else f"{len(urls)} active hook(s)",
        )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="tartci fleet drift guard")
    ap.add_argument("--repo", action="append", default=[],
                    help="OWNER/NAME this host should serve (repeatable)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.repo:
        ap.error("--repo OWNER/NAME is required (repeatable)")

    rep = Report()
    sy = shipyard_bin()
    if sy is None:
        rep.add("shipyard-installed", False, "shipyard not found")
        status = None
    else:
        rep.add("shipyard-installed", True, sy)
        status = daemon_status(sy)

    check_path_tools(rep)
    check_checkout(rep)
    check_token_helper(rep)
    check_daemon(rep, status, args.repo)
    check_gate_labels(rep, args.repo)
    check_webhooks(rep, status, args.repo)

    rc, host = run(["hostname"])
    hostname = host.strip() if rc == 0 else "(unknown)"

    if args.json:
        print(json.dumps({"host": hostname, "checks": rep.rows,
                          "drift": len(rep.failed)}, indent=2))
    else:
        print(f"fleet-preflight: {hostname}")
        for r in rep.rows:
            mark = {"ok": "  ok  ", "DRIFT": " DRIFT", "unknown": "  ??  "}[r["status"]]
            print(f"{mark} {r['check']}"
                  + (f"  — {r['detail']}" if r["detail"] else ""))
        if rep.failed:
            print(f"\n{len(rep.failed)} check(s) need attention. Repair with "
                  "`shipyard daemon refresh --repo … --repo …` from a shell whose "
                  "PATH includes /opt/homebrew/bin, then re-run.")
        else:
            print("\nno drift.")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)
