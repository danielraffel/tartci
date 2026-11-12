"""Nothing runs code from ~/.local/share/tartci except self-update's checkout.

That directory holds a hand-installed checkout that self-update never
refreshes. On 2026-10-02 two agents ran stale code from it: the
queue-saturation detector (its plist predated a required variable) and the
relay (a new template flag the old copy rejected, which rolled back m5's
update). Agents run through ~/.local/bin/tartci, which follows the installed
generation; only `update-checkout/`, self-update's own working checkout,
legitimately lives there.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STALE = re.compile(r"\.local/share/tartci(?!/update-checkout)(?![\w-])")

# Still to move onto the installed generation. Each entry is removed by the
# change that moves it; nothing may be added.
TRANSITIONAL: frozenset[str] = frozenset()


def referencing_files() -> set[str]:
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "launchd", "scripts",
                              "providers", "tartci"],
                             capture_output=True, text=True, check=True).stdout.split()
    found = set()
    for name in tracked:
        if "/test_" in name or name.startswith("scripts/test_"):
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines = [line for line in text.splitlines()
                 if STALE.search(line)]
        if lines:
            found.add(name)
    return found


class NoStaleCheckoutRefsTests(unittest.TestCase):
    def test_no_new_file_runs_code_from_the_stale_checkout(self) -> None:
        extra = referencing_files() - TRANSITIONAL
        self.assertEqual(extra, set(), f"run these through ~/.local/bin/tartci: {sorted(extra)}")

    def test_the_transitional_list_only_shrinks(self) -> None:
        # A file that no longer references the checkout leaves the list.
        moved = TRANSITIONAL - referencing_files()
        self.assertEqual(moved, set(), f"remove from TRANSITIONAL: {sorted(moved)}")

    def test_the_relay_and_backstop_run_the_installed_generation(self) -> None:
        for name, sub in (("com.danielraffel.tartci.http-connect-ssh-relay", "network-relay"),
                          ("com.danielraffel.pulp.schedule-backstop", "schedule-backstop")):
            text = (ROOT / "launchd" / f"{name}.plist.template").read_text()
            self.assertIn("<string>$HOME/.local/bin/tartci</string>", text, name)
            self.assertIn(f"<string>{sub}</string>", text, name)
            self.assertIn(f"  {sub}) shift;", (ROOT / "tartci").read_text(), sub)


    def test_runner_lane_templates_serve_through_the_installed_generation(self) -> None:
        import plistlib
        for name, os_name in (("tart-runner-macos", "macos"), ("tart-runner-macos-release", "macos"),
                              ("tart-runner-linux", "linux"), ("qemu-runner-windows", "windows")):
            raw = (ROOT / "launchd" / f"com.danielraffel.pulp.{name}.plist.template").read_bytes()
            value = plistlib.loads(re.sub(rb"<!--.*?-->", b"", raw, flags=re.DOTALL))
            self.assertEqual(value["ProgramArguments"][:4],
                             ["/bin/bash", "$HOME/.local/bin/tartci", "serve", os_name], name)
            self.assertNotIn("$TARTCI_REPO", raw.decode(), name)

    def test_queue_tick_and_steward_run_the_installed_generation(self) -> None:
        import plistlib
        for name, sub in (("com.danielraffel.shipyard.queue-tick", "queue-tick"),
                          ("com.danielraffel.shipyard.steward-scheduler", "steward-scheduler")):
            raw = (ROOT / "launchd" / f"{name}.plist.template").read_bytes()
            value = plistlib.loads(re.sub(rb"<!--.*?-->", b"", raw, flags=re.DOTALL))
            self.assertEqual(value["ProgramArguments"][:3],
                             ["/bin/bash", "$HOME/.local/bin/tartci", sub], name)
            self.assertIn(f"  {sub}) shift;", (ROOT / "tartci").read_text(), sub)

    def test_help_prints_usage_and_runs_nothing(self) -> None:
        # `tartci queue-tick --help` once ran a real tick: the tick ignores
        # its arguments.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home)
            for sub, expected in (("queue-tick", "usage: tartci queue-tick"),
                                  ("steward-scheduler", "usage:")):
                with self.subTest(sub=sub):
                    result = subprocess.run(["/bin/bash", str(ROOT / "tartci"), sub, "--help"],
                                            capture_output=True, text=True, env=env, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(expected, result.stdout)
                    self.assertNotIn("[queue-tick]", result.stdout + result.stderr)
            self.assertEqual(os.listdir(home), [], "a help request wrote state")
            stray = subprocess.run(["/bin/bash", str(ROOT / "tartci"), "queue-tick", "--apply"],
                                   capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(stray.returncode, 2)

if __name__ == "__main__":
    unittest.main()
