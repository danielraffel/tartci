#!/usr/bin/env python3
"""LaunchAgent templates must preserve each host's declared Tart VM store."""

from __future__ import annotations

import unittest
import plistlib
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TART_TEMPLATES = (
    ROOT / "launchd/com.danielraffel.pulp.tart-runner-macos.plist.template",
    ROOT / "launchd/com.danielraffel.pulp.tart-runner-macos-release.plist.template",
    ROOT / "launchd/com.danielraffel.pulp.tart-runner-linux.plist.template",
    ROOT / "launchd/com.danielraffel.tartci.reap.plist.template",
)


class TartHomeTemplateTests(unittest.TestCase):
    def test_launch_agents_render_a_host_declared_store(self) -> None:
        for template in TART_TEMPLATES:
            body = template.read_text(encoding="utf-8")
            self.assertIn(
                "<key>TART_HOME</key>\n        <string>$TART_HOME</string>",
                body,
                template.name,
            )
            self.assertNotIn(
                "<key>TART_HOME</key>\n        <string>$HOME/VMs</string>",
                body,
                f"{template.name} would reset an external-store host on reinstall",
            )

    def test_fresh_gate_migration_requires_and_renders_tart_home(self) -> None:
        body = (ROOT / "scripts/migrate_macos_gate_agent.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("${TART_HOME:?fresh migration requires", body)
        self.assertIn("render_launchd_template.py", body)

    def test_renderer_preserves_legal_path_metacharacters(self) -> None:
        template = TART_TEMPLATES[0]
        tart_home = "/Volumes/Builds & VMs/pipe|slash\\VMs"
        result = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts/render_launchd_template.py"),
                str(template),
                "--set",
                f"TART_HOME={tart_home}",
                "--set",
                "HOME=/Users/tester",
            ],
            check=True,
            capture_output=True,
        )
        value = plistlib.loads(result.stdout)
        self.assertEqual(value["EnvironmentVariables"]["TART_HOME"], tart_home)


if __name__ == "__main__":
    unittest.main(verbosity=2)
