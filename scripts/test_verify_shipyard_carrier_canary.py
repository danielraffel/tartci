#!/usr/bin/env python3
"""Hermetic tests for the carrier canary verifier's readers and comparisons."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import verify_shipyard_carrier_canary as verify

HEAD = "a" * 40
OTHER = "b" * 40


def proposal(number: int, action: str, **facts: object) -> dict[str, object]:
    base_facts: dict[str, object] = {
        "head_sha": HEAD,
        "merge_state": "BLOCKED",
        "approved_head": True,
        "queue": {"state": "ejected", "new_head_since": False},
        "required": [{"context": "macos", "conclusion": "SUCCESS"}],
    }
    base_facts.update(facts)
    row: dict[str, object] = {
        "number": number,
        "head_sha": HEAD,
        "decision": "propose",
        "action": action,
        "facts": base_facts,
    }
    if action in {"rearm", "update_branch"}:
        row["head"] = HEAD
    else:
        row["run_ids"] = [9]
    return row


def tick(at: str, plans: list[dict[str, object]], repo: str = "owner/repo") -> dict[str, object]:
    return {"tick": at, "mode": "plan", "repo": repo, "plans": plans, "errors": []}


class CompareTests(unittest.TestCase):
    def test_a_clean_ledger_with_matching_rulings_passes(self) -> None:
        rows = [
            tick("2026-10-09T00:00:00Z", [proposal(1, "rearm")]),
            tick("2026-10-09T00:05:00Z", [proposal(1, "rearm"), proposal(2, "redispatch",
                 queue={"state": "armed_not_queued"})]),
        ]
        rulings = [
            {"repo": "owner/repo", "number": 1, "head_sha": HEAD, "class": "rearm"},
            {"repo": "owner/repo", "number": 2, "head_sha": HEAD, "class": "redispatch"},
        ]
        result = verify.compare_plans(rows, rulings, ["rearm", "redispatch"])
        self.assertEqual(result["false_positives"], [])
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["true_positives"], {"rearm": 1, "redispatch": 1})
        self.assertEqual(result["missing_classes"], [])
        self.assertEqual(result["ticks"], 2)

    def test_a_proposal_ruled_none_is_a_false_positive(self) -> None:
        rows = [tick("2026-10-09T00:00:00Z", [proposal(1, "rearm")])]
        rulings = [{"repo": "owner/repo", "number": 1, "head_sha": HEAD, "class": "none"}]
        result = verify.compare_plans(rows, rulings, ["rearm"])
        self.assertEqual(len(result["false_positives"]), 1)
        self.assertEqual(result["missing_classes"], ["rearm"])

    def test_each_negative_control_is_flagged_from_the_recorded_facts(self) -> None:
        cases = {
            "conflicting": {"merge_state": "DIRTY"},
            "no approval record": {"approved_head": False},
            "queue state is never_armed": {"queue": {"state": "never_armed"}},
            "pushed after its removal": {"queue": {"state": "ejected", "new_head_since": True}},
            "failed required": {"required": [{"context": "macos", "conclusion": "FAILURE"}]},
        }
        for needle, facts in cases.items():
            with self.subTest(needle=needle):
                problems = verify.structural_violations(proposal(1, "rearm", **facts))
                self.assertTrue(any(needle in problem for problem in problems), problems)
        moved = proposal(1, "rearm")
        moved["head"] = OTHER
        self.assertTrue(verify.structural_violations(moved))
        self.assertEqual(verify.structural_violations(proposal(1, "rearm")), [])

    def test_any_intent_outcome_or_mutation_in_a_plan_ledger_fails(self) -> None:
        mutated = proposal(1, "rearm")
        mutated["mutation"] = "armed"
        rows = [
            tick("2026-10-09T00:00:00Z", [mutated]),
            {"tick": "2026-10-09T00:00:00Z", "intent": {"actions": []}},
        ]
        result = verify.compare_plans(rows, [], [])
        self.assertEqual(len(result["mutations"]), 2)

    def test_holds_are_never_counted(self) -> None:
        rows = [tick("2026-10-09T00:00:00Z", [{"number": 1, "head_sha": HEAD,
                                                "decision": "hold", "hold": "conflicting"}])]
        result = verify.compare_plans(rows, [], [])
        self.assertEqual((result["true_positives"], result["unruled"]), ({}, []))


class ReaderTests(unittest.TestCase):
    def test_launchd_runs_is_read_from_print_output(self) -> None:
        self.assertEqual(verify.launchd_runs("\tstate = not running\n\truns = 14\n"), 14)
        self.assertIsNone(verify.launchd_runs("Could not find service"))

    def test_ledger_rows_include_rotated_generations_oldest_first(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "plans.jsonl"
            Path(f"{path}.2").write_text(json.dumps({"tick": "1"}) + "\n")
            Path(f"{path}.1").write_text(json.dumps({"tick": "2"}) + "\n")
            path.write_text(json.dumps({"tick": "3"}) + "\n\n")
            Path(f"{path}.bak").write_text("not json\n")
            self.assertEqual([row["tick"] for row in verify.ledger_rows(path)], ["1", "2", "3"])

    def test_plans_check_requires_the_full_window(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "plans.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in (tick("2026-10-09T00:00:00Z", []), tick("2026-10-09T12:00:00Z", []))
                )
                + "\n"
            )
            with mock.patch("builtins.print"):
                self.assertFalse(verify.check_plans(path, None, 48.0, []))
                self.assertTrue(verify.check_plans(path, None, 12.0, []))
                # Requiring classes without rulings can never pass.
                self.assertFalse(verify.check_plans(path, None, 12.0, ["rearm"]))

    def test_rollback_reads_a_non_live_config_and_agreeing_health(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "config.json"
            health = Path(raw) / "health.json"
            config.write_text(json.dumps({"mode": "plan", "authority": False, "classes": []}))
            health.write_text(json.dumps({"mode": "plan", "observed_at": "2999-01-01T00:00:00Z"}))
            with mock.patch.object(verify, "CONFIG", config), mock.patch.object(
                verify, "HEALTH", health
            ), mock.patch("builtins.print"):
                self.assertTrue(verify.check_rollback())
                config.write_text(json.dumps({"mode": "live", "authority": True, "classes": ["rearm"]}))
                self.assertFalse(verify.check_rollback())


class QuarantineFixtureTests(unittest.TestCase):
    def test_the_planted_timeout_fixture_quarantines_this_checkout_s_scheduler(self) -> None:
        dispatcher = Path(__file__).resolve().parent.parent / "tartci"
        with tempfile.TemporaryDirectory(dir=Path.home()) as raw, mock.patch.object(
            verify, "ENTRYPOINT", dispatcher
        ), mock.patch("builtins.print") as printed:
            self.assertTrue(verify.check_quarantine(Path(raw)), printed.call_args_list)


if __name__ == "__main__":
    unittest.main()
