#!/usr/bin/env python3
"""Keep the dedicated signing keychain unlocked in the GUI login session.

macOS locks every non-login keychain at login and logout. A locked keychain on
the search list is what made securityd put password dialogs in front of the
user on m3 and m5studio (2026-10-02): any process in the GUI session that
searched it for an identity raised SecurityAgent and asked for a password
only keychain.env holds. tartci's own signers name their keychain and unlock
it in their own session, but other processes in the GUI session do not.

The com.danielraffel.tartci.keychain-unlock LaunchAgent runs this at load
(each login) and every few minutes. It unlocks only the dedicated keychain
(su.signing_keychain: keychain.env's PULP_SIGN_KEYCHAIN, or its -unattended
sibling) and clears any auto-lock timeout. The password goes to `security -i`
on standard input: it is never an argument (visible in `ps`) and never written
to the log or the state file. A malformed keychain.env is reported by key name
only, and every line printed or stored passes through secret_files.redact(). `unlock-keychain -p` cannot prompt; a wrong
password fails and is reported. Settings are only read after a successful
unlock, because reading them from a locked keychain is itself what prompts.

State: $TARTCI_HOME/state/keychain-unlock/last.json
  {"at", "state": ok|failed|not_applicable, "keychain", "detail"}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_self_update as su  # noqa: E402
import secret_files  # noqa: E402

LABEL = "com.danielraffel.tartci.keychain-unlock"
INTERVAL_SECS = 900

# security -i reads one command per line; quote and escape every token.
Interactive = Callable[[str], tuple[int, str]]


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _security_interactive(script: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["/usr/bin/security", "-i"], input=script, capture_output=True,
                              text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, type(exc).__name__
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def state_path(home: Path | None = None) -> Path:
    root = os.environ.get("TARTCI_HOME") or str((home or Path.home()) / ".tartci")
    return Path(root) / "state" / "keychain-unlock" / "last.json"


def _scrub(text: str, secret: str, home: Path) -> str:
    # Before any slicing: a truncated secret no longer matches.
    text = text.replace(secret, "<redacted>") if secret else text
    return secret_files.redact(text, home)


def run(home: Path | None = None, interactive: Interactive = _security_interactive,
        now: float | None = None) -> dict:
    home = home or Path.home()
    now = time.time() if now is None else now
    keychain = su.signing_keychain(home)
    password = su.signing_secrets(home).get("PULP_SIGN_KEYCHAIN_PW")
    problem = su.signing_secrets_problem(home)
    if problem and (not keychain or not password):
        value = {"state": "failed", "keychain": keychain, "detail": problem}
    elif not keychain or not password:
        value = {"state": "not_applicable", "keychain": keychain,
                 "detail": "no dedicated signing keychain in keychain.env"}
    elif not Path(keychain).is_file():
        value = {"state": "failed", "keychain": keychain,
                 "detail": "the dedicated keychain file does not exist; run `pulp ship doctor`"}
    else:
        rc, out = interactive(f"unlock-keychain -p {_quote(password)} {_quote(keychain)}\n")
        if rc != 0:
            value = {"state": "failed", "keychain": keychain,
                     "detail": f"unlock failed (exit {rc}): {_scrub(out, password, home).strip()[-200:]}"
                               "; run `pulp ship doctor`"}
        else:
            # Unlocked now, so these cannot raise a dialog.
            rc, out = interactive(f"set-keychain-settings {_quote(keychain)}\n"
                                  f"show-keychain-info {_quote(keychain)}\n")
            relocks = "timeout=" in out or "lock-on-sleep" in out
            value = {"state": "failed" if rc != 0 or relocks else "ok", "keychain": keychain,
                     "detail": ("unlocked, no auto-lock" if rc == 0 and not relocks else
                                f"unlocked but settings not cleared (exit {rc}): "
                                f"{_scrub(out, password, home).strip()[-160:]}")}
    value = {k: (secret_files.redact(v, home) if isinstance(v, str) else v)
             for k, v in value.items()}
    value["at"] = now
    path = state_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        tmp.write_text(json.dumps(value))
        os.replace(tmp, path)
    except OSError:
        pass
    return value


def last(home: Path | None = None) -> dict | None:
    try:
        return json.loads(state_path(home).read_text())
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci keychain-unlock")
    parser.parse_args(argv)
    value = run()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value["at"]))
    print(secret_files.redact(
        f"{stamp} keychain-unlock: {value['state']}: {value.get('keychain')}: {value['detail']}"))
    return 1 if value["state"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
