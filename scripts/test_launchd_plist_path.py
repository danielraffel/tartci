#!/usr/bin/env python3
"""Guard: every launchd plist template's PATH includes the system bin dirs.

A launchd agent gets only the PATH its plist declares. `sysctl` lives in
/usr/sbin, so a template that omits it hands every child process a PATH where
system binaries are unreachable — which is how the Linux and Windows VM lanes
died silently. host_profile/leases no longer depend on PATH for their own
probes, but the runner scripts these agents exec still expect a usable PATH,
so the templates must ship the system dirs too.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHD_DIR = REPO_ROOT / "launchd"
REQUIRED_PATH_DIRS = ("/usr/sbin", "/sbin")


def _templates() -> list[Path]:
    return sorted(LAUNCHD_DIR.glob("*.plist.template"))


class LaunchdPlistPathTests(unittest.TestCase):
    def test_templates_exist(self) -> None:
        self.assertTrue(_templates(), f"no plist templates found under {LAUNCHD_DIR}")

    def test_every_declared_path_includes_system_bin_dirs(self) -> None:
        checked = 0
        for template in _templates():
            # Templates carry $HOME/$TART_HOME placeholders and comments, so
            # they are not valid plists until rendered — scan the PATH string
            # textually rather than parsing.
            lines = template.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if line.strip() != "<key>PATH</key>":
                    continue
                value = lines[index + 1].strip()
                self.assertTrue(
                    value.startswith("<string>") and value.endswith("</string>"),
                    msg=f"{template.name}: PATH key is not followed by a <string> value",
                )
                path_value = value[len("<string>") : -len("</string>")]
                entries = path_value.split(":")
                for required in REQUIRED_PATH_DIRS:
                    self.assertIn(
                        required,
                        entries,
                        msg=(
                            f"{template.name} declares a PATH without {required}. "
                            "System binaries (sysctl lives in /usr/sbin) become "
                            "unreachable for everything this agent execs."
                        ),
                    )
                checked += 1
        self.assertGreater(checked, 0, "no PATH declarations found in any template")


if __name__ == "__main__":
    unittest.main(verbosity=2)
