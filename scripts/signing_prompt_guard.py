#!/usr/bin/env python3
"""Whether this host's keychain setup can put a password dialog on screen.

A dialog appears when a process in the GUI session touches a non-login
keychain that is locked in that session: securityd starts SecurityAgent and
asks for that keychain's password, which only keychain.env holds. On
2026-10-02 the legacy `pulp-signing.keychain-db` sat on m3's and m5studio's
user search list beside its `-unattended` replacement. A bare
`codesign --sign` walks that list, so every unpinned signer was one lock away
from a dialog asking Daniel for a password he does not have.

status() reports, without ever prompting:
  - every keychain on the user search list other than login and the dedicated
    signing keychain (a legacy or leftover keychain is what a search walks
    into);
  - whether the dedicated keychain unlocks with keychain.env's password
    (`unlock-keychain -p` never prompts; a refusal means the recorded
    password has drifted);
  - after that unlock, so reading settings cannot prompt, whether the keychain
    re-locks on its own (an inactivity timeout or lock-on-sleep);
  - whether the keychain-unlock LaunchAgent is installed and its last run
    succeeded recently. macOS locks the keychain again at every login, and
    that agent is what unlocks it in the GUI session, the only session the
    dialogs come from. Nothing here can read the GUI session's lock state
    without risking a dialog: even `show-keychain-info` on a locked keychain
    raises one, so it is only ever run right after a successful unlock.

States: ok | risk | not_applicable (no keychain.env) | unknown.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_self_update as su  # noqa: E402
import keychain_unlock  # noqa: E402
import secret_files  # noqa: E402

# The agent runs every 15 minutes; two missed runs is a stopped agent.
UNLOCK_STALE_SECS = 2 * keychain_unlock.INTERVAL_SECS + 300

Runner = Callable[[list[str]], tuple[int, str]]


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def search_list(text: str) -> list[str]:
    return [line.strip().strip('"') for line in text.splitlines() if line.strip()]


def unlock_agent_risk(home: Path, now: float | None = None) -> str | None:
    """Why the keychain-unlock agent cannot be relied on, or None."""
    import time
    now = time.time() if now is None else now
    plist = home / "Library" / "LaunchAgents" / f"{keychain_unlock.LABEL}.plist"
    if not plist.is_file():
        return (f"the keychain-unlock agent is not installed ({plist}); after the next login "
                "the keychain is locked in the GUI session again. "
                "Run scripts/install_keychain_unlock_agent.sh --install")
    last = keychain_unlock.last(home)
    if not last:
        return "the keychain-unlock agent has not recorded a run yet"
    if last.get("state") == "failed":
        return f"the keychain-unlock agent's last run FAILED: {last.get('detail')}"
    age = now - float(last.get("at") or 0)
    if age > UNLOCK_STALE_SECS:
        return (f"the keychain-unlock agent's last run was {int(age // 60)} min ago "
                f"(it runs every {keychain_unlock.INTERVAL_SECS // 60} min)")
    return None


def status(home: Path | None = None, run: Runner = _run,
           now: float | None = None) -> dict[str, Any]:
    home = home or Path.home()
    secrets = su.signing_secrets(home)
    dedicated = su.signing_keychain(home)
    password = secrets.get("PULP_SIGN_KEYCHAIN_PW")
    problem = su.signing_secrets_problem(home)
    if problem and (not dedicated or not password):
        return {"state": "risk", "risks": [problem]}
    if not dedicated or not password:
        return {"state": "not_applicable", "risks": [],
                "detail": "no dedicated signing keychain in keychain.env"}
    return _redacted(_status(home, run, now, dedicated, password), home)


def _redacted(value: Any, home: Path) -> Any:
    """`value` with every secrets-file value removed from its strings."""
    if isinstance(value, str):
        return secret_files.redact(value, home)
    if isinstance(value, list):
        return [_redacted(v, home) for v in value]
    if isinstance(value, dict):
        return {k: _redacted(v, home) for k, v in value.items()}
    return value


def _status(home: Path, run: Runner, now: float | None, dedicated: str,
            password: str) -> dict[str, Any]:
    def call(argv: list[str]) -> tuple[int, str]:
        # Redact before any slicing: a truncated secret no longer matches.
        rc, out = run(argv)
        return rc, secret_files.redact(out, home)

    rc, out = call(["security", "list-keychains", "-d", "user"])
    if rc != 0:
        return {"state": "unknown", "risks": [], "detail": f"search list unreadable: {out[:200]}"}
    risks = []
    for path in search_list(out):
        if Path(path).name == "login.keychain-db" or path == dedicated:
            continue
        risks.append(f"{path} is on the user search list beside the dedicated "
                     f"{Path(dedicated).name}; an unpinned codesign that walks into it while "
                     "it is locked raises a password dialog")
    rc, out = call(["security", "unlock-keychain", "-p", password, dedicated])
    if rc != 0:
        risks.append(f"{dedicated} does not unlock with keychain.env's password "
                     f"({out.strip()[:120]}); run `pulp ship doctor`")
    else:
        rc, out = call(["security", "show-keychain-info", dedicated])
        if rc == 0 and (re.search(r"timeout=\d+", out) or "lock-on-sleep" in out):
            risks.append(f"{dedicated} re-locks on its own ({out.strip()[:120]}); "
                         "`pulp ship doctor` sets it to no-timeout")
    agent = unlock_agent_risk(home, now)
    if agent:
        risks.append(agent)
    return {"state": "risk" if risks else "ok", "risks": risks, "dedicated": dedicated}


def describe(value: dict[str, Any]) -> str:
    state = value.get("state")
    if state == "ok":
        return f"signing prompts: ok (only {Path(value['dedicated']).name} and login searched)"
    if state == "risk":
        return "signing prompts: RISK: " + " | ".join(value["risks"])
    if state == "not_applicable":
        return ""
    return f"signing prompts: UNKNOWN ({value.get('detail')})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="signing_prompt_guard")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    value = status()
    if args.json:
        print(json.dumps(value, sort_keys=True))
    else:
        line = describe(value)
        if line:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
