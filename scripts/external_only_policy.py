#!/usr/bin/env python3
"""Keep build trees and caches off the internal disk on external-only hosts.

m5studio and m3 run builds, ccache, FetchContent/CPM sources, pip and uv caches
on an external APFS volume; only m1 and m5 use their internal disk. On
2026-11-17 m5studio's internal Data volume sat at 98% and tripped fleet_ready,
with caches, build trees and tool state all defaulting to $HOME.

A host opts in with `[host] external_root = "/Volumes/<name>"` in its fleet
profile. `[host] external_only_mode` is `report` only for now. `tartci leases
acquire` for a host build (not a VM lease, whose store is already judged by the
disk axis) resolves every build and cache directory it can see; when one
resolves, after symlinks, to the internal data volume it warns on stderr, admits
the build, and journals a `would_refuse` row. The warning names the path and the
fix. Refuse mode (exit 78) exists in code and is tested but is not reachable
from config until the follow-up checklist in the runbook is done. A host without
`external_root` is unchanged.

Directories judged, in order of authority:

  * `--build-dir` and `--cache-dir` (repeatable) on `leases acquire`; with no
    build dir declared, the working directory stands in for it;
  * CCACHE_DIR, or `cache_dir` in the file CCACHE_CONFIGPATH names or in
    ccache.conf, or ccache's default directory when it already exists;
  * pip's and uv's default cache directories when they already exist and
    their variables are unset;
  * CPM_SOURCE_CACHE, FETCHCONTENT_BASE_DIR, PIP_CACHE_DIR, UV_CACHE_DIR and
    TARTCI_BUILD_DIR when set.

A path that does not exist yet is judged by its nearest existing ancestor,
which is where the first byte would land. A path under external_root while the
volume is not mounted resolves to the internal volume, so it is reported (and
would be refused in refuse mode): the build would otherwise write a copy of the
cache to the boot disk.

`python3 scripts/external_only_policy.py check` runs the same judgement
read-only, `exports` prints `export` lines that point every unset cache at the
external root, and `--json` is available on both.

Python 3.9-safe: the lease store is launched by the stock interpreter.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

REASON = "external_only_internal_path"
REASON_UNREADABLE = "external_only_device_unreadable"
EXIT_REFUSED = 78  # EX_CONFIG: not a capacity denial, so --wait-secs never retries it.

# Environment variable -> directory under <external_root>/caches/.
CACHE_ENV: Dict[str, str] = {
    "CCACHE_DIR": "ccache",
    "CPM_SOURCE_CACHE": "cpm",
    "FETCHCONTENT_BASE_DIR": "fetchcontent",
    "PIP_CACHE_DIR": "pip",
    "UV_CACHE_DIR": "uv",
}
BUILD_ENV = ("TARTCI_BUILD_DIR",)
INTERNAL_DEVICE_PROBES = ("/System/Volumes/Data", "/")


def default_dirs(external_root: str) -> Dict[str, str]:
    """Where each cache lives by default on a host with this external root."""
    base = os.path.join(external_root.rstrip("/"), "caches")
    return {name: os.path.join(base, leaf) for name, leaf in CACHE_ENV.items()}


def ccache_reported_dir(env: Mapping[str, str]) -> Optional[str]:
    """The cache_dir ccache itself resolves (its own config precedence), or None."""
    import shutil
    import subprocess
    exe = shutil.which("ccache", path=env.get("PATH"))
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-k", "cache_dir"], capture_output=True, text=True,
                             timeout=5, env=dict(env), check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value.startswith("/") else None


def _ccache_conf_dir(env: Mapping[str, str], home: str) -> Optional[str]:
    """cache_dir from the user's ccache.conf, read without running ccache."""
    candidates = [
        os.path.join(home, "Library", "Preferences", "ccache", "ccache.conf"),
        os.path.join(env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
                     "ccache", "ccache.conf"),
        os.path.join(home, ".ccache", "ccache.conf"),
    ]
    if env.get("CCACHE_CONFIGPATH"):
        # ccache reads only this file when it is set.
        candidates = [env["CCACHE_CONFIGPATH"]]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                for raw in handle:
                    line = raw.split("#", 1)[0].strip()
                    name, sep, value = line.partition("=")
                    if sep and name.strip() == "cache_dir" and value.strip():
                        return os.path.expanduser(value.strip())
        except OSError:
            continue
    return None


def collect(env: Mapping[str, str], home: str, build_dirs: Sequence[str] = (),
            cache_dirs: Sequence[str] = (), cwd: Optional[str] = None) -> List[Tuple[str, str]]:
    """(label, path) for every build and cache directory this build will use."""
    found: List[Tuple[str, str]] = []
    for path in build_dirs:
        found.append(("--build-dir", path))
    for path in cache_dirs:
        found.append(("--cache-dir", path))
    for name in BUILD_ENV:
        if env.get(name):
            found.append((name, env[name]))
    if cwd and not build_dirs and not any(env.get(n) for n in BUILD_ENV):
        # No declared build dir: the build runs where it was started.
        found.append(("working directory", cwd))
    for name in CACHE_ENV:
        if env.get(name):
            found.append((name, env[name]))
    xdg = env.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
    if env.get("XDG_CACHE_HOME"):
        uv_default = os.path.join(xdg, "uv")
    elif sys.platform == "darwin":
        uv_default = os.path.join(home, "Library", "Caches", "uv")
    else:
        uv_default = os.path.join(home, ".cache", "uv")
    for name, default in (("PIP_CACHE_DIR", os.path.join(home, "Library", "Caches", "pip")),
                          ("UV_CACHE_DIR", uv_default)):
        if not env.get(name) and os.path.isdir(default):
            found.append((name + " default dir", default))
    if not env.get("CCACHE_DIR"):
        # An explicit cache_dir is judged as written. ccache's own answer (which is
        # its default when nothing is configured) is judged only once the directory
        # exists: ccache may not be used at all.
        configured = _ccache_conf_dir(env, home)
        reported = ccache_reported_dir(env)
        default = os.path.join(home, "Library", "Caches", "ccache")
        # ccache's own answer wins (it knows directory-specific configs); the
        # parsed file is the fallback when the probe fails.
        path = reported or configured or default
        explicit = bool(configured) or (reported is not None and reported != default)
        if explicit or os.path.isdir(os.path.expanduser(path)):
            found.append(("ccache dir", path))
    return [(label, os.path.expanduser(path)) for label, path in found]


def nearest_existing(path: str) -> str:
    """Resolve symlinks, then walk up to the first ancestor that exists."""
    current = os.path.realpath(path)
    while current and not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current or "/"


def _internal_device_probe(
    device: Callable[[str], int],
) -> Tuple[Optional[int], List[Dict[str, str]]]:
    probes = INTERNAL_DEVICE_PROBES[:1] if sys.platform == "darwin" else INTERNAL_DEVICE_PROBES
    errors: List[Dict[str, str]] = []
    for probe in probes:
        try:
            # A later probe that answers makes an earlier miss moot (Linux has no
            # /System/Volumes/Data), so the misses are reported only when all fail.
            return device(probe), []
        except OSError as exc:
            errors.append({"label": "internal device probe", "path": probe, "error": str(exc)})
    return None, errors


def internal_device(device: Callable[[str], int]) -> Optional[int]:
    """Return the internal device, or None when it cannot be proven."""
    return _internal_device_probe(device)[0]


def _st_dev(path: str) -> int:
    return os.stat(path).st_dev


def judge(paths: Sequence[Tuple[str, str]], external_root: str, *,
          device: Optional[Callable[[str], int]] = None) -> Dict[str, Any]:
    """Judge each path; `violations` lists those on the internal data volume."""
    device = device or _st_dev
    internal, internal_device_errors = _internal_device_probe(device)
    if internal is None and not internal_device_errors:
        internal_device_errors = [{"label": "internal device probe", "path": "<unknown>",
                                   "error": "device id unavailable"}]
    violations: List[Dict[str, str]] = []
    judged: List[Dict[str, str]] = []
    unread: List[Dict[str, str]] = []
    for label, path in paths:
        resolved = nearest_existing(path)
        try:
            dev = device(resolved)
        except OSError as exc:
            # A probe failure must never become an outage; report it.
            unread.append({"label": label, "path": path, "error": str(exc)})
            continue
        row = {"label": label, "path": path, "resolved": resolved}
        judged.append(row)
        if internal is not None and dev == internal:
            violations.append(row)
    return {
        "external_root": external_root,
        "internal_device_known": internal is not None,
        "internal_device_errors": internal_device_errors,
        "judged": judged,
        "violations": violations,
        "unread": unread,
    }


def fix_text(violations: Sequence[Mapping[str, str]], external_root: str) -> str:
    defaults = default_dirs(external_root)
    lines = []
    for row in violations:
        name = row["label"]
        hint = defaults.get(name)
        if hint:
            fix = f"export {name}={hint}"
        elif name in ("--build-dir", "TARTCI_BUILD_DIR", "working directory"):
            fix = f"put the build dir under {external_root}/ (for example a worktree under {external_root}/Code)"
        elif name.startswith("ccache"):
            fix = f"set cache_dir = {defaults['CCACHE_DIR']} in ccache.conf (or export CCACHE_DIR)"
        else:
            fix = f"point it under {external_root}/caches/"
        lines.append(f"  {name}: {row['path']} resolves to {row['resolved']} on the internal disk; fix: {fix}")
    return "\n".join(lines)


def refusal_message(violations: Sequence[Mapping[str, str]], external_root: str) -> str:
    return (
        f"tartci: refusing to start a build on an external-only host "
        f"(external_root {external_root}); these paths are on the internal data volume:\n"
        f"{fix_text(violations, external_root)}\n"
        f"Existing data moves with docs/runbook.md, \"Keeping builds and caches on the external volume\"."
    )


def unread_message(unread: Sequence[Mapping[str, str]], external_root: str, *,
                   mode: str, internal_unknown: bool) -> str:
    details = "\n".join(
        f"  {row.get('label', 'probe')}: {row.get('path', '<unknown>')} "
        f"({row.get('error', 'unreadable')})"
        for row in unread
    )
    if mode == "refuse":
        action = "refusing"
        suffix = " in refuse mode"
    else:
        action = "admitting"
        suffix = " in report mode"
    if internal_unknown:
        subject = "could not prove the internal data volume"
    else:
        subject = "could not inspect every build or cache path"
    return (f"tartci: external-only policy {subject} (external_root {external_root}); "
            f"{action}{suffix} with these unreadable probes:\n{details}")


def check_acquire(args: Any, external_root: Optional[str], *, mode: str = "report",
                  env: Optional[Mapping[str, str]] = None, home: Optional[str] = None,
                  device: Optional[Callable[[str], int]] = None) -> Optional[Dict[str, Any]]:
    """A refusal result for `leases acquire` (refuse mode only), or None to admit.

    Host-build leases only: a VM lease's store is judged by the disk axis, and
    the lane environment is rendered, not an agent's shell.
    """
    if not external_root:
        return None
    if str(getattr(args, "kind", "") or "").endswith("-vm"):
        return None
    paths = collect(env if env is not None else os.environ,
                    home or os.path.expanduser("~"),
                    getattr(args, "build_dir", None) or (),
                    getattr(args, "cache_dir", None) or (), cwd=os.getcwd())
    verdict = judge(paths, external_root, device=device)
    internal_errors = verdict["internal_device_errors"]
    unread = internal_errors + verdict["unread"]
    internal_unknown = not verdict["internal_device_known"]
    if unread or internal_unknown:
        if mode == "refuse":
            # Fail closed: a path whose location cannot be read cannot be
            # proven external, whether or not the internal device is known.
            return {
                "ok": False,
                "reason": REASON_UNREADABLE,
                "message": unread_message(unread, external_root, mode="refuse",
                                           internal_unknown=internal_unknown),
                "external_root": external_root,
                "violations": verdict["violations"],
                "unread": unread,
            }
        print(unread_message(unread, external_root, mode="report",
                             internal_unknown=internal_unknown), file=sys.stderr)
        try:
            args.external_policy_unread = unread
        except AttributeError:
            pass
    if not verdict["violations"]:
        return None
    if mode != "refuse":
        # Report mode (the profile default): admit, and say what refuse would do,
        # so a host can migrate its existing caches before enforcement starts.
        print("tartci (external_only_mode=report, would refuse): "
              + refusal_message(verdict["violations"], external_root)[len("tartci: "):],
              file=sys.stderr)
        # For the journal: the evidence for deciding to enforce.
        try:
            args.external_would_refuse = verdict["violations"]
        except AttributeError:
            pass
        return None
    return {
        "ok": False,
        "reason": REASON,
        "message": refusal_message(verdict["violations"], external_root),
        "external_root": external_root,
        "violations": verdict["violations"],
    }


def check_acquire_safe(args: Any, external_root: Optional[str], *, mode: str = "report",
                       **kwargs: Any) -> Optional[Dict[str, Any]]:
    """check_acquire that can never fail an acquire: any error admits with one warning."""
    try:
        return check_acquire(args, external_root, mode=mode, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a probe failure must not become an outage
        print(f"tartci: external-only policy not judged ({type(exc).__name__}: {exc}); admitting",
              file=sys.stderr)
        return None


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="external-only host policy (read-only)")
    parser.add_argument("command", choices=("check", "exports"))
    parser.add_argument("--fleet-profile")
    parser.add_argument("--external-root", help="override the profile's external_root")
    parser.add_argument("--build-dir", action="append", default=[])
    parser.add_argument("--cache-dir", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import host_profile
    root = args.external_root or host_profile.external_root_setting(args.fleet_profile)
    if not root:
        print("not an external-only host: no [host] external_root in the fleet profile")
        return 0
    if args.command == "exports":
        exports = {name: path for name, path in default_dirs(root).items()
                   if not os.environ.get(name)}
        if args.json:
            print(json.dumps(exports, indent=2, sort_keys=True))
        else:
            for name, path in exports.items():
                print(f"export {name}={path}")
        return 0
    verdict = judge(collect(os.environ, os.path.expanduser("~"), args.build_dir, args.cache_dir,
                              cwd=os.getcwd()), root)
    if (not verdict["internal_device_known"] or verdict["internal_device_errors"]
            or verdict["unread"]):
        print(unread_message(verdict["internal_device_errors"] + verdict["unread"], root,
                             mode="report", internal_unknown=not verdict["internal_device_known"]),
              file=sys.stderr)
    incomplete = bool(not verdict["internal_device_known"] or verdict["internal_device_errors"]
                      or verdict["unread"])
    if args.json:
        print(json.dumps(verdict, indent=2, sort_keys=True))
    elif verdict["violations"]:
        print(refusal_message(verdict["violations"], root))
    elif incomplete:
        print("incomplete: some paths could not be read, so this is not proof that "
              "none are on the internal volume")
    else:
        print(f"ok: {len(verdict['judged'])} paths checked, none on the internal volume")
    if verdict["violations"]:
        return 1
    return 2 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(_main())
