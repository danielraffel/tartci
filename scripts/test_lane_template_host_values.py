"""Lane templates carry the host label as a token their render recipe must fill.

The Linux and Windows lane templates hard-coded `pulp-host-macstudio`, and the
documented sed render had no step for it: rendering on m1 or m5 would have
produced a lane advertising the Mac Studio's host label.
"""

from __future__ import annotations

import plistlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANES = ("tart-runner-linux", "qemu-runner-windows")


class LaneTemplateHostValueTests(unittest.TestCase):
    def test_the_host_label_is_a_token_the_recipe_fills(self) -> None:
        for name in LANES:
            with self.subTest(name=name):
                text = (ROOT / "launchd" / f"com.danielraffel.pulp.{name}.plist.template").read_text()
                value = plistlib.loads(re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).encode())
                labels = value["ProgramArguments"][value["ProgramArguments"].index("--labels") + 1]
                self.assertTrue(labels.endswith(",$TARTCI_HOST_LABEL"), labels)
                self.assertNotIn("pulp-host-", labels)
                self.assertIn('-e "s|\\$TARTCI_HOST_LABEL|pulp-host-<this host>|g"', text)
                self.assertIn("Never re-run this render over an installed plist", text)


if __name__ == "__main__":
    unittest.main()
