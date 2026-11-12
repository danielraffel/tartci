"""Every `launchctl bootstrap` of a RunAtLoad agent is followed by a kickstart.

A RunAtLoad launch is speculative, and launchd can defer it indefinitely on a
busy host: `launchctl print` then shows `runs = 0` and `pended nondemand spawn
= speculative`, and the agent never starts. On 2026-10-04 m3 deferred every
such launch for hours while already-running agents carried on, so an
installer, a rollback or a watchdog re-render that only bootstraps leaves its
agent loaded and silent. `launchctl kickstart` turns the first run into an
on-demand spawn.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = re.compile(
    r"""(?:launchctl|LAUNCHCTL["}]?|launchctl_command)\s+bootstrap\b"""
    r"""|["']launchctl["'],\s*["']bootstrap["']"""
)
# How many lines after a bootstrap the kickstart may sit.
WINDOW = 8

# Bootstraps that need no kickstart of their own, by file and stripped line.
EXEMPT: dict[tuple[str, str], str] = {
    ("scripts/install_artifact_cache_refresh_agent.sh",
     '[ "$loaded" = 1 ] || "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$TARGET"'):
        "RunAtLoad is false: the agent's first run is its interval, not a load",
    ("scripts/install_self_update_agent.sh",
     '"$LAUNCHCTL" bootstrap "gui/$(id -u)" "$TARGET"'):
        "RunAtLoad is false: an immediate self-update on install is not wanted",
    ("scripts/install_shipyard_queue_tick.sh",
     'launchctl bootstrap "$DOMAIN" "$plist" >/dev/null 2>&1 || true'):
        "inside bootstrap_reliably(); both callers kickstart once it returns",
}


def bootstraps() -> list[tuple[str, int, str, list[str]]]:
    """(file, line number, stripped line, following lines) for every bootstrap call."""
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "scripts", "providers",
                              "tartci", "tartci-lib"],
                             capture_output=True, text=True, check=True).stdout.split()
    found = []
    for name in tracked:
        if "/test_" in name or name.startswith("scripts/test_") or name.endswith(".md"):
            continue
        try:
            lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for index, line in enumerate(lines):
            stripped = line.strip()
            # Comments and messages that name the command are not calls.
            if stripped.startswith(("#", "echo", "c_warn")) or not BOOTSTRAP.search(line):
                continue
            following = []
            for later in lines[index + 1 : index + 1 + WINDOW]:
                # The window ends at the next bootstrap: that one needs its own.
                if BOOTSTRAP.search(later):
                    break
                # A comment that mentions a kickstart is not one.
                if not later.strip().startswith("#"):
                    following.append(later)
            found.append((name, index + 1, stripped, following))
    return found


class KickstartFollowsBootstrapTests(unittest.TestCase):
    def test_the_scan_sees_the_known_bootstraps(self) -> None:
        # Control: an empty scan would pass the real assertion vacuously.
        names = {name for name, *_ in bootstraps()}
        for expected in ("scripts/install_reclaim_agent.sh",
                         "scripts/install_shipyard_steward_scheduler.sh",
                         "scripts/tartci_launchd_watchdog.py",
                         "providers/tart-macos/guest-aqua-runner.sh"):
            self.assertIn(expected, names)

    def test_every_bootstrap_is_followed_by_a_kickstart(self) -> None:
        missing = [
            f"{name}:{number}: {line}"
            for name, number, line, following in bootstraps()
            if (name, line) not in EXEMPT
            and not any("kickstart" in later for later in [line, *following])
        ]
        self.assertEqual(missing, [], "kickstart the agent after bootstrapping it")

    def test_every_exemption_still_names_a_real_bootstrap(self) -> None:
        present = {(name, line) for name, _, line, _ in bootstraps()}
        self.assertEqual(set(EXEMPT) - present, set(), "remove stale EXEMPT entries")


if __name__ == "__main__":
    unittest.main()
