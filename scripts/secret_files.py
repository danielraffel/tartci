#!/usr/bin/env python3
"""Read pulp's secrets files without ever letting a value reach a message.

~/.config/pulp/secrets/keychain.env and notary.env hold the signing-keychain
password and the notary key settings. On 2026-10-04 a keychain.env written
without line breaks during a keychain rotation parsed as one long
PULP_SIGN_KEYCHAIN value that contained the password, and keychain-unlock
printed that "keychain path" into its log. Two rules keep that from
recurring:

  - a value is only used when it has the shape its key promises: a keychain
    path must look like a keychain path. A malformed value is reported by its
    key name and file name only, never by any part of its text;
  - every message that leaves a secrets-handling module goes through
    redact(), which removes each secret-named value and every fragment of a
    malformed value, whatever the message was built from.

Nothing here prints, logs or raises with file content.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

FILES = ("keychain.env", "notary.env")
REDACTED = "<redacted>"
# Keys whose values are secrets outright. Other keys hold paths and ids.
SECRET_KEY = re.compile(r"(_PW|PASSWORD|PASSPHRASE|SECRET|TOKEN|_PASS)$", re.IGNORECASE)
KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A value that holds another assignment or a quote swallowed its neighbours:
# the file lost its line breaks or its quoting.
SWALLOWED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=|[\x00-\x1f\"']")
KEYCHAIN_PATH = re.compile(r"[^\x00-\x1f\"'=\\`$]+\.keychain(-db)?")
# Fragments shorter than this are too common to redact without mangling text.
MIN_FRAGMENT = 4


def secrets_dir(home: Path) -> Path:
    return Path(os.environ.get("PULP_SECRETS_DIR") or home / ".config" / "pulp" / "secrets")


class EnvFile:
    """One secrets file: usable values, and the key names that did not parse."""

    def __init__(self, name: str, values: dict[str, str], malformed: list[str],
                 raw: list[str]) -> None:
        self.name = name
        self.values = values
        self.malformed = malformed
        self._raw = raw

    def secret_texts(self) -> set[str]:
        """Every text redact() must remove: secret values, malformed raw text."""
        texts = {v for k, v in self.values.items() if SECRET_KEY.search(k)}
        texts.update(self._raw)
        for raw in self._raw:
            texts.update(p for p in re.split(r"[\s\"'=\\]+", raw) if len(p) >= MIN_FRAGMENT)
        return {t for t in texts if len(t) >= MIN_FRAGMENT}


def parse(name: str, text: str) -> EnvFile:
    """Parse KEY=value lines the way the shell that sources the file would.

    A line that is not `[export ]KEY=<one shell word>` is malformed: its key
    name (or "line N" when there is none) is recorded and its text is kept
    only so redact() can remove it.
    """
    values: dict[str, str] = {}
    malformed: list[str] = []
    raw: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        key = key.removeprefix("export ").strip()
        if not sep or not KEY.fullmatch(key):
            malformed.append(f"line {number}")
            raw.append(stripped)
            continue
        try:
            words = shlex.split(value, comments=True, posix=True)
        except ValueError:
            words = None
        if words is None or len(words) > 1 or (words and SWALLOWED.search(words[0])
                                                  and not SECRET_KEY.search(key)):
            malformed.append(key)
            raw.append(value)
            continue
        values[key] = words[0] if words else ""
        if SECRET_KEY.search(key):
            raw.append(value)
    return EnvFile(name, values, malformed, raw)


def load(home: Path, name: str) -> EnvFile | None:
    try:
        text = (secrets_dir(home) / name).read_text()
    except (OSError, UnicodeDecodeError):
        return None
    return parse(name, text)


def keychain_path_ok(value: str) -> bool:
    return bool(KEYCHAIN_PATH.fullmatch(value))


def redact(text: object, home: Path | None = None) -> str:
    """`text` with every secret from the secrets files and env removed."""
    out = str(text)
    home = home or Path.home()
    secrets: set[str] = set()
    for name in FILES:
        env = load(home, name)
        if env is not None:
            secrets |= env.secret_texts()
    secrets.update(v for k, v in os.environ.items()
                   if SECRET_KEY.search(k) and k.startswith(("PULP_", "TARTCI_"))
                   and len(v) >= MIN_FRAGMENT)
    for secret in sorted(secrets, key=len, reverse=True):
        out = out.replace(secret, REDACTED)
    return out
