import plistlib
import tempfile
import unittest
from pathlib import Path

from scan_blind_status import active_log_names, blind_problems

# m1's install receipt (~/.config/tartci/macos-fleet-install.json) on
# 2026-10-13: its `plists` keys, and the log the slot2 lane's plist declares.
M1_PLISTS = [
    "com.danielraffel.tartci.tart-runner-macos-fleet.m1.forge-gate.plist",
    "com.danielraffel.tartci.tart-runner-macos-fleet.m1.pulp-gate.plist",
    "com.danielraffel.tartci.tart-runner-macos-fleet.m1.pulp-gate.slot2.plist",
    "com.danielraffel.tartci.tart-runner-macos-fleet.m1.spectr-gate.plist",
    "com.danielraffel.tartci.tart-runner-macos-fleet.m1.vellum-gate.plist",
]
M1_SLOT2_LOG = "/Users/example/Library/Logs/tartci/macos-fleet-pulp-gate-slot2.log"
BLIND = "SCAN BLIND 1/9\nSCAN BLIND 2/9\nSCAN BLIND 3/9\n"


class ScanBlindStatusTests(unittest.TestCase):
    def test_retired_lane_log_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            active = root / "macos-fleet-pulp-gate.log"
            retired = root / "macos-fleet-old-gate.log"
            active.write_text("SCAN BLIND 1/9\nSCAN BLIND 2/9\nSCAN BLIND 3/9\n")
            retired.write_text("SCAN BLIND 1/9\nSCAN BLIND 2/9\nSCAN BLIND 3/9\n")
            receipt = {"plists": {
                "com.danielraffel.tartci.tart-runner-macos-fleet.m3.pulp-gate.plist": "sha",
            }}
            problems = blind_problems(receipt, [str(active), str(retired)], 3)
            self.assertEqual([str(active)], [row["label"] for row in problems])

    def test_active_lane_streak_is_reported(self):
        receipt = {"plists": {"pulp-gate.plist": "sha"}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "macos-fleet-pulp-gate.log"
            path.write_text("SCAN BLIND 1/9\nSCAN BLIND 2/9\n")
            self.assertEqual(
                [{"code": "scan_blind", "label": str(path), "cycles": 2}],
                blind_problems(receipt, [str(path)], 2),
            )

    def test_a_slot2_lane_maps_to_the_log_it_writes(self):
        names = active_log_names({"plists": {name: "sha" for name in M1_PLISTS}})
        self.assertEqual(
            sorted(names),
            [
                "macos-fleet-forge-gate.log",
                "macos-fleet-pulp-gate-slot2.log",
                "macos-fleet-pulp-gate.log",
                "macos-fleet-spectr-gate.log",
                "macos-fleet-vellum-gate.log",
            ],
        )

    def test_a_blind_slot2_lane_holds_the_pool_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / Path(M1_SLOT2_LOG).name
            log.write_text(BLIND)
            receipt = {"plists": {name: "sha" for name in M1_PLISTS}}
            self.assertEqual(
                [{"code": "scan_blind", "label": str(log), "cycles": 3}],
                blind_problems(receipt, [str(log)], 3),
            )

    def test_the_log_the_plist_declares_wins_over_its_name(self):
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "LaunchAgents"
            agents.mkdir()
            slot2 = M1_PLISTS[2]
            declared = Path(td) / "elsewhere-pulp-gate-slot2.log"
            # An excerpt of m1's real slot2 plist: its label, and a log path
            # moved into the temp tree so the test can write it.
            with (agents / slot2).open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": slot2.removesuffix(".plist"),
                        "StandardOutPath": str(declared),
                        "StandardErrorPath": str(declared),
                    },
                    handle,
                )
            receipt = {"agents_dir": str(agents), "plists": {slot2: "sha"}}
            self.assertEqual(active_log_names(receipt), {declared.name})
            declared.write_text(BLIND)
            self.assertEqual(len(blind_problems(receipt, [str(declared)], 3)), 1)
            # An unreadable plist falls back to the name mapping.
            (agents / slot2).write_bytes(b"not a plist")
            self.assertEqual(active_log_names(receipt), {"macos-fleet-pulp-gate-slot2.log"})


if __name__ == "__main__":
    unittest.main()
