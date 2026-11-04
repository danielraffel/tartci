#!/usr/bin/env python3
"""The fleet's pinned Tart, and the drained-window upgrade to it.

Tart runs every gate VM, so the version a host runs is a reviewed commit:
fleet/tart.toml pins Tart and its Softnet dependency to exact versions and
the sha256 the openai/tools tap's formulae must carry for them. A host moves
to a new pin only when all of these hold:

  * it opted in: `[tart] upgrade = true` in self-update.toml (off by default);
  * the tap offers exactly the pinned Tart and Softnet, with those checksums
    (brew runs with HOMEBREW_NO_AUTO_UPDATE, so drain time cannot pick a
    different version);
  * it is the canary (`[tart] canary`), or the canary has run the pinned Tart
    for `[tart] soak_seconds` after booting a VM with it and serving again;
  * the pool is off and no `tart run` process is left.

The upgrade keeps the previous kegs (HOMEBREW_NO_INSTALL_CLEANUP), then
proves the new Tart by cloning the host's golden, booting it headless and
waiting for its IP. On any failure it removes the new kegs so the previous
Tart and Softnet are linked again, and proves that Tart answers before the
pool comes back on. A pin that failed on a host is not tried there again
until the pin changes or an operator clears tart-failed.json.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

PIN_FILE = Path("fleet") / "tart.toml"
# The installed generation's root: a same-generation turn reads its own pin.
REPO_ROOT = Path(__file__).resolve().parent.parent
BREW = "/opt/homebrew/bin/brew"
TART = "/opt/homebrew/bin/tart"
OPT = Path("/opt/homebrew/opt")
# No tap refresh, no cleanup of the previous keg (the rollback relinks it).
BREW_ENV = {"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1",
            "HOMEBREW_NO_ENV_HINTS": "1", "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
            # Homebrew 7 autoremoves orphaned dependencies after an uninstall;
            # the rollback's uninstall must remove the one keg and nothing else.
            "HOMEBREW_NO_AUTOREMOVE": "1"}
# The pool is off for all of it: the two tarballs are small.
UPGRADE_TIMEOUT_S = 300
BOOT_WAIT_S = 180
TAP_CACHE = "tart-tap.json"
TAP_CACHE_SECONDS = 3600
VERIFIED = "tart-verified.json"
FAILED = "tart-failed.json"
GATE = "tart-gate.json"
# A Tart change in flight: what to come back to if the process dies mid-way.
PENDING = "tart-rollback-pending.json"
# A host deliberately held OFF because its Tart may not run (host_off.HOLD).
HOLD = "tart-broken.json"
# A refusal found only after the drain (a Tart VM still running) waits this
# long before the same pin may drain the host again.
DEFER_SECONDS = 24 * 3600
DEFERRED = "tart-deferred.json"
# The boot-check clone is a scratch VM with a record, so the launchd
# watchdog's scratch-VM TTL pass deletes it if this process dies mid-check.
CHECK_PREFIX = "scratch-tart-check-"
CHECK_TTL_SECONDS = 3600
# A Tart VM process: the tart binary itself (bin/tart, or the app inside its
# keg) running `run`, not any command line that merely contains "tart run".
TART_RUN_PATTERN = r"^([^ ]*/)?tart run "
DEFAULT_SOAK_SECONDS = 4 * 3600
SHA256 = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"\d+\.\d+\.\d+")

Run = Callable[..., tuple[int, str]]  # run(argv, timeout=, env=) -> (rc, text)


class PinError(ValueError):
    """fleet/tart.toml is missing or malformed."""


@dataclasses.dataclass(frozen=True)
class Pin:
    version: str
    sha256: str
    softnet_version: str
    softnet_sha256: str
    formula: str = "openai/tools/tart"
    softnet_formula: str = "openai/tools/softnet"


_TABLE = re.compile(r"\[([A-Za-z0-9_-]+)\]")
_STRING = re.compile(r'([A-Za-z0-9_-]+)\s*=\s*"([^"\\]*)"')


def parse_pin(text: str) -> dict[str, dict[str, str]]:
    """fleet/tart.toml: `[table]` lines, `key = "string"` lines, blank lines
    and `#` comments, nothing else. Parsed without tomllib, because
    tool-freshness runs under the hosts' /usr/bin/python3 3.9. Anything else
    is an error, never ignored. Raises PinError."""
    tables: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip() if '"' not in raw else raw.strip()
        if not line or line.startswith("#"):
            continue
        table = _TABLE.fullmatch(line)
        if table:
            current = tables.setdefault(table.group(1), {})
            continue
        pair = _STRING.fullmatch(line.split(" #", 1)[0].strip())
        if pair is None or current is None:
            raise PinError(f"line {number}: expected [table] or key = \"value\": {raw.strip()!r}")
        current[pair.group(1)] = pair.group(2)
    return tables


def load_pin(root: Path) -> Pin:
    path = root / PIN_FILE
    try:
        data = parse_pin(path.read_text())
    except OSError as exc:
        raise PinError(f"{path}: {exc}") from exc
    except PinError as exc:
        raise PinError(f"{path}: {exc}") from exc
    tart, softnet = data.get("tart"), data.get("softnet")
    if not isinstance(tart, dict) or not isinstance(softnet, dict):
        raise PinError(f"{path}: needs [tart] and [softnet] tables")
    for table, name in ((tart, "tart"), (softnet, "softnet")):
        if not VERSION.fullmatch(str(table.get("version", ""))):
            raise PinError(f"{path}: [{name}] version must be X.Y.Z")
        if not SHA256.fullmatch(str(table.get("sha256", ""))):
            raise PinError(f"{path}: [{name}] sha256 must be 64 lowercase hex")
    return Pin(tart["version"], tart["sha256"], softnet["version"], softnet["sha256"],
               str(tart.get("formula") or "openai/tools/tart"),
               str(softnet.get("formula") or "openai/tools/softnet"))


def vkey(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def installed_tart(run: Run) -> str | None:
    rc, out = run([TART, "--version"], timeout=30)
    match = VERSION.search(out) if rc == 0 else None
    return match.group(0) if match else None


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def tap_offer(run: Run, pin: Pin, state_dir: Path, now: float,
              fresh: bool = False) -> dict[str, Any]:
    """What the local tap offers for the pinned formulae: {"tart": (version,
    sha256), "softnet": (...)}. Cached for an hour per pin, because a plan
    asks; `fresh` reads the tap itself (right before a drain or an upgrade, a
    cached answer could be an hour old). Raises PinError when brew cannot
    say."""
    cache = None if fresh else _read_json(state_dir / TAP_CACHE)
    # Keyed by the whole pin: a moved pin never reads the old pin's answer.
    key = (f"{pin.formula} {pin.version} {pin.sha256} "
           f"{pin.softnet_formula} {pin.softnet_version} {pin.softnet_sha256}")
    if (cache and cache.get("key") == key and isinstance(cache.get("at"), (int, float))
            and 0 <= now - cache["at"] < TAP_CACHE_SECONDS):
        return {name: tuple(cache[name]) for name in ("tart", "softnet")}
    rc, out = run([BREW, "info", "--json=v2", pin.formula, pin.softnet_formula],
                  timeout=60, env=BREW_ENV)
    if rc != 0:
        raise PinError(f"brew info exit {rc}: {out.strip()[:160]}")
    try:
        formulae = json.loads(out).get("formulae") or []
    except (ValueError, AttributeError) as exc:
        raise PinError(f"brew info printed no JSON: {out.strip()[:160]}") from exc
    offered: dict[str, Any] = {}
    for formula in formulae:
        name = str(formula.get("full_name") or formula.get("name") or "")
        stable = (formula.get("urls") or {}).get("stable") or {}
        entry = ((formula.get("versions") or {}).get("stable"), stable.get("checksum"))
        if name in (pin.formula, pin.formula.rsplit("/", 1)[-1]):
            offered["tart"] = entry
        elif name in (pin.softnet_formula, pin.softnet_formula.rsplit("/", 1)[-1]):
            offered["softnet"] = entry
    if set(offered) != {"tart", "softnet"}:
        raise PinError(f"brew info did not describe {pin.formula} and {pin.softnet_formula}")
    _write_json(state_dir / TAP_CACHE, {"key": key, "at": now, **offered})
    return offered


def tap_refusal(pin: Pin, offered: dict[str, Any]) -> str | None:
    """Why the tap cannot install exactly the pin, or None."""
    for name, version, sha256 in (("tart", pin.version, pin.sha256),
                                  ("softnet", pin.softnet_version, pin.softnet_sha256)):
        got_version, got_sha = offered[name]
        if got_version != version or got_sha != sha256:
            return (f"the tap offers {name} {got_version} ({str(got_sha)[:12]}), the pin is "
                    f"{version} ({sha256[:12]}); refresh the tap with `brew update` or move "
                    "the pin in fleet/tart.toml")
    return None


def archive_sha256(run: Run, formula: str) -> tuple[str | None, str]:
    """(sha256, path) of the archive brew downloaded for `formula`
    (`brew --cache`); (None, why) when it cannot say. The tap ships no
    bottles, so this is the release tarball the pin's sha256 names."""
    rc, out = run([BREW, "--cache", formula], timeout=60, env=BREW_ENV)
    path = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else ""
    if not path:
        return None, f"brew --cache {formula} exit {rc}: {out.strip()[-120:]}"
    rc, out = run(["/usr/bin/shasum", "-a", "256", path], timeout=120)
    digest = out.split()[0] if rc == 0 and out.split() else ""
    if not SHA256.fullmatch(digest):
        return None, f"shasum {path} exit {rc}: {out.strip()[-120:]}"
    return digest, path


def opt_links(names: tuple[str, ...]) -> dict[str, str | None]:
    """Where /opt/homebrew/opt/<name> points now: the linked keg to come back to."""
    links: dict[str, str | None] = {}
    for name in names:
        try:
            links[name] = os.readlink(OPT / name)
        except OSError:
            links[name] = None
    return links


def installed_softnet() -> str | None:
    """The version of the Softnet keg currently linked into the host."""
    link = opt_links(("softnet",))["softnet"]
    match = VERSION.search(link or "")
    return match.group(0) if match else None


CLEANUP_TRIES = 3
CLEANUP_RETRY_S = 10


def boot_check(run: Run, golden: str, tart_home: str, pid: int, now: float,
               records: Path, sleep: Callable[[float], None] | None = None
               ) -> tuple[str | None, str | None]:
    """Clone the golden, boot it headless and wait for its IP.

    (failure, cleanup): failure is None when it booted; cleanup names a stop
    or delete that did not succeed. The clone is recorded as a scratch VM
    under `records` before it exists, so a clone this process cannot delete
    (or never gets to) is reclaimed by the watchdog at its TTL; the record
    goes only after the delete is confirmed. A stop or delete that fails is
    retried CLEANUP_TRIES times; a clone still there after that is a failure
    even when the boot was good, so the pool never comes back on beside it.
    """
    name = f"{CHECK_PREFIX}{pid}"
    env = {"TART_HOME": tart_home}
    record = records / f"{name}.json"
    _write_json(record, {"name": name, "store": tart_home, "owner": "self-update tart boot check",
                         "created_at": now, "expires_at": now + CHECK_TTL_SECONDS})
    rc, out = run([TART, "clone", golden, name], timeout=300, env=env)
    if rc != 0:
        record.unlink(missing_ok=True)
        return f"tart clone {golden} failed (exit {rc}): {out.strip()[-200:]}", None
    failure = None
    try:
        # Detached: `tart run` lives as long as the VM. The clone has no
        # runner config, so it never registers as a runner.
        run(["/bin/sh", "-c", f'nohup "{TART}" run --no-graphics "{name}" >/dev/null 2>&1 &'],
            timeout=30, env=env)
        rc, out = run([TART, "ip", "--wait", str(BOOT_WAIT_S), name], timeout=BOOT_WAIT_S + 30,
                      env=env)
        if rc != 0 or not out.strip():
            failure = (f"a clone of {golden} did not report an IP within {BOOT_WAIT_S}s "
                       f"(exit {rc})")
    finally:
        cleanup: list[str] = []
        gone = False
        for attempt in range(CLEANUP_TRIES):
            if attempt and sleep is not None:
                sleep(CLEANUP_RETRY_S)
            cleanup = []
            rc, out = run([TART, "stop", "--timeout", "30", name], timeout=90, env=env)
            if rc != 0 and "does not exist" not in out.lower():
                cleanup.append(f"tart stop {name} exit {rc}: {out.strip()[-120:]}")
            rc, out = run([TART, "delete", name], timeout=120, env=env)
            if rc == 0 or "does not exist" in out.lower():
                gone = True
                break
            cleanup.append(f"tart delete {name} exit {rc}: {out.strip()[-120:]}")
        if gone:
            record.unlink(missing_ok=True)
        else:
            cleanup.append(f"still there after {CLEANUP_TRIES} tries; the watchdog deletes it "
                           f"after {_iso_utc(now + CHECK_TTL_SECONDS)}")
            if failure is None:
                failure = f"the boot-check clone {name} could not be removed"
    return failure, ("; ".join(cleanup) if not gone else None)


def _iso_utc(ts: float) -> str:
    import time  # noqa: PLC0415
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def roll_back(run: Run, formula: str) -> str | None:
    """Remove the keg `opt` links now (the new one), so the previous keg is
    linked again. None on success, else why not.

    Homebrew's `brew link` links the highest installed version and re-points
    `opt` at it, so relinking cannot go back while the new keg exists. With
    `opt` still on the new keg, `brew uninstall --ignore-dependencies` removes
    only that keg; `brew link --overwrite` then links the one left.
    """
    rc, out = run([BREW, "uninstall", "--ignore-dependencies", formula], timeout=120,
                  env=BREW_ENV)
    if rc != 0:
        return f"brew uninstall {formula} failed: {out.strip()[-160:]}"
    rc, out = run([BREW, "link", "--overwrite", formula], timeout=60, env=BREW_ENV)
    if rc != 0:
        return f"brew link {formula} failed: {out.strip()[-160:]}"
    return None


def published_verified(state_dir: Path, pin: Pin, now: float) -> None:
    _write_json(state_dir / VERIFIED, {
        "version": pin.version,
        "sha256": pin.sha256,
        "softnet_version": pin.softnet_version,
        "softnet_sha256": pin.softnet_sha256,
        "verified_at": now,
    })


def read_verified(value: dict | None, clock: float) -> dict | None:
    """The full Tart+Softnet identity and age from a canary receipt.

    Older Tart-only receipts deliberately do not qualify as proof for a pin
    whose dependency changed; callers can see the missing fields and require
    a fresh canary run.
    """
    if (not isinstance(value, dict) or not isinstance(value.get("verified_at"), (int, float))
            or not VERSION.fullmatch(str(value.get("version", "")))):
        return None
    return {
        "version": value["version"],
        "sha256": str(value.get("sha256") or ""),
        "softnet_version": str(value.get("softnet_version") or ""),
        "softnet_sha256": str(value.get("softnet_sha256") or ""),
        "age": clock - float(value["verified_at"]),
    }


def record_failure(state_dir: Path, pin: Pin, now: float, reason: str) -> None:
    """This pin failed here: it is not tried again until the pin changes or an
    operator removes the record."""
    _write_json(state_dir / FAILED, {"version": pin.version, "sha256": pin.sha256,
                                     "at": now, "reason": reason[:500]})


def failure_for(state_dir: Path, pin: Pin) -> dict | None:
    value = _read_json(state_dir / FAILED)
    if value and value.get("version") == pin.version and value.get("sha256") == pin.sha256:
        return value
    return None


def record_deferral(state_dir: Path, pin: Pin, now: float, reason: str) -> None:
    _write_json(state_dir / DEFERRED, {"version": pin.version, "sha256": pin.sha256,
                                       "until": now + DEFER_SECONDS, "reason": reason[:300]})


def deferral_for(state_dir: Path, pin: Pin, now: float) -> dict | None:
    value = _read_json(state_dir / DEFERRED)
    if (value and value.get("version") == pin.version and value.get("sha256") == pin.sha256
            and isinstance(value.get("until"), (int, float)) and now < value["until"]):
        return value
    return None


def hold(state_dir: Path, now: float, reason: str) -> None:
    """Keep this host OFF: no automatic `pool on` and no self-update until Tart
    is fixed (host_off.HOLD). Call it after last.json records the episode: the
    hold names that record's `at` and counts only while it is the current one.
    Whatever was pending is now the hold's business."""
    episode = (_read_json(state_dir / "last.json") or {}).get("at")
    _write_json(state_dir / HOLD, {"at": now, "episode": episode, "reason": reason[:500]})
    (state_dir / PENDING).unlink(missing_ok=True)


def write_gate(state_dir: Path, value: dict) -> None:
    """Whether this host is due to run the pin (tool-freshness reads it to
    decide if Tart behind its pin is a problem or only information)."""
    _write_json(state_dir / GATE, value)
