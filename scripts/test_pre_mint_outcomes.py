"""Pre-mint outcome counts per lane per day."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pre_mint_outcomes as pmo  # noqa: E402


def event(ts: str, name: str) -> str:
    return json.dumps({"ts": ts, "event": name, "runner": "r", "vm": "", "detail": ""})


class PreMintOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def lane(self, name: str, lines: list[str]) -> None:
        path = self.root / "macos-fleet" / name / "events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(lines + ['not json', event("2026-10-03T19:00:00Z", "mint_jit")])
                        + "\n", encoding="utf-8")

    def test_counts_retargets_and_discards_per_lane_per_day(self) -> None:
        # m3's gate lane on 2026-10-03: four denials, none retargetable then.
        self.lane("pulp-gate", [event(f"2026-10-03T19:{m}:00Z", pmo.DENIED) for m in (18, 25, 31, 37)]
                  + [event("2026-10-04T10:00:00Z", pmo.DENIED),
                     event("2026-10-04T10:00:01Z", pmo.RETARGET)])
        self.lane("pulp-gate-slot2", [event("2026-10-04T11:00:00Z", pmo.DENIED)])
        rows = pmo.outcomes(pmo.lane_logs(self.root))
        self.assertEqual(rows, [
            {"day": "2026-10-03", "lane": "pulp-gate", "denied": 4, "retargets": 0, "discards": 4},
            {"day": "2026-10-04", "lane": "pulp-gate", "denied": 1, "retargets": 1, "discards": 0},
            {"day": "2026-10-04", "lane": "pulp-gate-slot2", "denied": 1, "retargets": 0, "discards": 1},
        ])

    def test_a_vm_kept_by_the_hold_is_not_a_discard(self) -> None:
        self.lane("pulp-gate", [event("2026-10-25T02:00:00Z", pmo.DENIED),
                                event("2026-10-25T02:00:30Z", pmo.KEPT),
                                event("2026-10-25T03:00:00Z", pmo.DENIED)])
        self.assertEqual(pmo.outcomes(pmo.lane_logs(self.root)), [
            {"day": "2026-10-25", "lane": "pulp-gate", "denied": 2, "retargets": 1, "discards": 1},
        ])

    def test_a_lane_with_no_denials_reports_nothing(self) -> None:
        # Control: other events never count.
        self.lane("pulp-gate", [event("2026-10-03T19:00:00Z", "job_claim")])
        self.assertEqual(pmo.outcomes(pmo.lane_logs(self.root)), [])

    def test_the_cli_prints_the_table(self) -> None:
        self.lane("pulp-gate", [event("2026-10-03T19:18:00Z", pmo.DENIED)])
        out = subprocess.run([sys.executable, str(HERE / "pre_mint_outcomes.py"),
                              "--state-root", str(self.root), "--days", "0"],
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("2026-10-03  pulp-gate", out)
        self.assertIn("retargets", out)

    def test_tartci_dispatches_it(self) -> None:
        self.assertIn("pre-mint-outcomes) shift; cmd_pre_mint_outcomes",
                      (HERE.parent / "tartci").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
