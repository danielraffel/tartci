#!/usr/bin/env python3
"""A peer dark long enough stops holding the self-update turn, and only then."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_doctor  # noqa: E402
import fleet_self_update as su  # noqa: E402
from test_fleet_self_update import NOW, Base, FakeSystem  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def setUpModule() -> None:
    if su.tomllib is None:
        raise unittest.SkipTest("self-update reads host profiles with tomllib (Python 3.11+)")

# m1, m3 (studio), m5 and m5studio, as on 2026-10-05. This fake host is m1.
FLEET = {"schema": "tartci.advertised-labels/v1",
         "registrations": [{"host_id": h} for h in ("m1", "studio", "m5", "m5studio")],
         "hosts": [{"host_id": "m1", "ssh": "m1"}, {"host_id": "studio", "ssh": "m3"},
                   {"host_id": "m5", "ssh": "m5"}, {"host_id": "m5studio", "ssh": "m5s"}]}


def at(hhmm: str) -> float:
    """2026-10-05 UTC wall time, on the fake clock (10:42Z, m1's first attempt, is NOW)."""
    hours, minutes = (int(x) for x in hhmm.split(":"))
    return NOW + ((hours * 60 + minutes) - (10 * 60 + 42)) * 60


class ReportingSystem(FakeSystem):
    """Each readable peer also prints its own record of hosts it cannot read."""

    def __init__(self, home: Path, **kwargs) -> None:
        super().__init__(home, **kwargs)
        self.reports: dict[str, dict] = {}   # ssh target -> its peer-unreadable.json

    def run(self, argv, *, cwd=None, env=None, timeout=900):
        result = super().run(argv, cwd=cwd, env=env, timeout=timeout)
        a = list(argv)
        if a[0] == "ssh" and "pool status" not in a[-1] and result.rc == 0:
            i = 1
            while a[i].startswith("-"):
                i += 2 if a[i] in ("-o", "-i", "-p") else 1
            report = self.reports.get(a[i])
            if report is not None:
                return su.Result(0, result.out + "\n" + su.UNREADABLE_SEPARATOR + "\n"
                                 + json.dumps(report), result.err)
        return result


class Fleet(Base):
    def setUp(self) -> None:
        super().setUp()
        self.sys = ReportingSystem(self.home)
        self.sys.published = json.loads(json.dumps(FLEET))
        self.sys.peers["m5s"] = {"state": "on", "participating": True}

    def dark(self, *targets: str) -> None:
        for target in targets:
            self.sys.peers.pop(target, None)

    def corroborate(self, about: str, since: float, *, by=("m3", "m5s"), age: float = 60) -> None:
        """`by` read `about` and failed, `age` seconds ago on their own clocks."""
        for target in by:
            self.sys.reports[target] = {about: {"since": since, "reads": 6,
                                                "last": self.sys.clock - age}}

    def attempt(self, hhmm: str) -> int:
        self.sys.clock = at(hhmm)
        ticket = self.sys.peer_waiting.get("m5s")
        if ticket:
            ticket["ts"] = self.sys.clock
        return self.apply()

    def events(self, name: str) -> list[dict]:
        path = self.cfg.state_dir / "events.jsonl"
        if not path.is_file():
            return []
        return [row for row in map(json.loads, path.read_text().splitlines())
                if row.get("event") == name]

    def reason(self) -> str:
        return su.waiting_ticket(self.cfg)["reason"]


class BoundTests(unittest.TestCase):
    """The bound is tied to the constants it is justified by, and cannot drift."""

    def test_the_bound_sits_between_a_stale_marker_and_starvation(self) -> None:
        bound = su.PEER_UNREACHABLE_EXCLUDE_SECONDS
        self.assertGreaterEqual(bound, su.ACTIVE_MARKER_TTL)
        longest_update = (su.INSTALL_ATTEMPTS * (su.INSTALL_TIMEOUT + su.INSTALL_TERM_GRACE)
                          + su.VERIFY_SETTLE_SECONDS + su.ANNOUNCE_SETTLE_SECONDS)
        self.assertGreaterEqual(bound, longest_update)
        self.assertLess(bound, su.STARVED_AFTER_SECONDS)

    def test_the_reads_floor_fits_inside_the_window(self) -> None:
        template = ROOT / "launchd" / "com.danielraffel.tartci.self-update.plist.template"
        found = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>",
                          template.read_text())
        self.assertIsNotNone(found)
        interval = int(found.group(1))
        self.assertEqual(interval, su.SELF_UPDATE_INTERVAL_SECONDS)
        self.assertLessEqual(su.PEER_UNREACHABLE_MIN_READS * interval,
                             su.PEER_UNREACHABLE_EXCLUDE_SECONDS)


class ReplayTests(Fleet):
    """m1's view of 2026-10-05: m5 drained at 10:42Z and went dark at 11:12Z;
    m5studio had waited since 08:32Z."""

    def replay_until_m5studio_goes(self) -> list[str]:
        m5_dark_at = at("11:12")
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.sys.peer_waiting["m5s"] = {"host_id": "m5studio", "since": at("08:32"), "ts": 0}
        outcomes = []
        for hhmm in ("10:42", "11:12", "11:43", "12:14", "12:44", "13:14", "13:44", "14:14"):
            if hhmm == "11:12":
                self.dark("m5")
            if self.sys.clock >= m5_dark_at or hhmm == "11:12":
                self.corroborate("m5", m5_dark_at)
            self.attempt(hhmm)
            outcomes.append(f"{hhmm} {self.reason()}")
        return outcomes

    def test_the_fleet_moves_once_m5_has_been_dark_for_the_bound(self) -> None:
        outcomes = self.replay_until_m5studio_goes()
        for line in outcomes[:7]:
            self.assertRegex(line,
                             r"m5 is draining|m5 \(m5\) (?:pool status unreadable|SSH (?:transport|authentication) failed)",
                             line)
        self.assertIn("yielding the update turn to a host that has waited longer: m5studio",
                      outcomes[7])
        self.assertEqual(self.sys.mutations(), [], "m1 never went out ahead of m5studio")
        [excluded] = self.events("peer_unreachable_excluded")
        self.assertEqual(excluded["fields"]["peer"], "m5")
        self.assertEqual(excluded["fields"]["corroborated_by"], ["m5studio", "studio"])
        # m5studio takes its turn: m1 waits on its marker, never overlapping.
        self.sys.peer_waiting.pop("m5s")
        self.sys.peer_markers["m5s"] = {"host_id": "m5studio", "target": "c" * 40,
                                        "ts": at("14:20")}
        self.corroborate("m5", at("11:12"))
        self.attempt("14:44")
        self.assertIn("m5studio is self-updating", self.reason())
        self.assertEqual(self.sys.mutations(), [])
        # m5studio is done; m1 goes while m5 is still dark.
        self.sys.peer_markers.pop("m5s")
        self.corroborate("m5", at("11:12"))
        self.assertUpdated(self.attempt("15:14"))
        receipt = Path(self.last()["receipt"]).read_text()
        self.assertIn("unreachable, so no longer holding the turn", receipt)
        self.assertEqual(len(self.events("peer_unreachable_excluded")), 1, "once per episode")

    def test_under_the_old_rule_the_same_day_never_moves(self) -> None:
        with mock.patch.object(su, "PEER_UNREACHABLE_EXCLUDE_SECONDS", float("inf")):
            self.replay_until_m5studio_goes()
            self.sys.peer_waiting.pop("m5s")
            for hhmm in ("14:44", "16:14", "18:14", "20:14"):
                self.corroborate("m5", at("11:12"))
                self.attempt(hhmm)
                self.assertRegex(self.reason(),
                                 r"m5 \(m5\) (?:pool status unreadable|SSH (?:transport|authentication) failed)")
        self.assertEqual(self.sys.mutations(), [])
        self.assertEqual(self.events("peer_unreachable_excluded"), [])


class GuardTests(Fleet):
    """Each guard alone holds the turn: removing it lets the matching case through."""

    def dark_since(self, target: str, since: float, reads: int) -> None:
        self.dark(target)
        host = {"m5": "m5", "m5s": "m5studio", "m3": "studio"}[target]
        streaks = su.peer_streaks(self.cfg)
        streaks[host] = {"since": since, "reads": reads, "last": since, "excluded": False}
        su._write_json(self.cfg.state_dir / su.PEER_UNREADABLE_NAME, streaks)

    def test_a_qualified_dark_peer_is_excluded(self) -> None:
        # The positive control every guard below is measured against.
        self.dark_since("m5", NOW - 4 * 3600, 7)
        self.corroborate("m5", NOW - 4 * 3600)
        self.assertEqual(su.survey_peers(self.cfg, self.sys, "m1").excluded, ["m5"])

    def test_quorum_an_even_split_excludes_nobody(self) -> None:
        # m1 reads only studio; studio agrees m5 and m5studio are both dark.
        self.dark_since("m5", NOW - 4 * 3600, 7)
        self.dark_since("m5s", NOW - 4 * 3600, 7)
        self.sys.reports["m3"] = {h: {"since": NOW - 4 * 3600, "reads": 7, "last": NOW - 60}
                                  for h in ("m5", "m5studio")}
        survey = su.survey_peers(self.cfg, self.sys, "m1")
        self.assertEqual(survey.excluded, [])
        self.assertEqual(len(survey.busy), 2)

    def test_quorum_a_host_dark_to_everyone_excludes_nobody(self) -> None:
        for target in ("m3", "m5", "m5s"):
            self.dark_since(target, NOW - 4 * 3600, 7)
        self.assertEqual(su.survey_peers(self.cfg, self.sys, "m1").excluded, [])

    def test_unanimity_one_peer_that_still_reaches_it_keeps_it(self) -> None:
        # Asymmetric: studio cannot reach m5, m5studio can (it has no record).
        self.dark_since("m5", NOW - 4 * 3600, 7)
        self.corroborate("m5", NOW - 4 * 3600, by=("m3",))
        self.sys.reports["m5s"] = {}
        self.assertEqual(su.survey_peers(self.cfg, self.sys, "m1").excluded, [])

    def test_unanimity_a_peer_without_the_record_does_not_corroborate(self) -> None:
        self.dark_since("m5", NOW - 4 * 3600, 7)
        self.corroborate("m5", NOW - 4 * 3600, by=("m3",))   # m5studio runs an older generation
        self.assertEqual(su.survey_peers(self.cfg, self.sys, "m1").excluded, [])

    def test_freshness_a_stale_corroboration_does_not_count(self) -> None:
        self.dark_since("m5", NOW - 4 * 3600, 7)
        self.corroborate("m5", NOW - 4 * 3600, by=("m3",))
        self.corroborate("m5", NOW - 4 * 3600, by=("m5s",),
                         age=su.PEER_UNREACHABLE_FRESH_SECONDS + 60)
        self.assertEqual(su.survey_peers(self.cfg, self.sys, "m1").excluded, [])

    def test_time_bound_enough_reads_but_not_long_enough(self) -> None:
        self.dark_since("m5", NOW - su.PEER_UNREACHABLE_EXCLUDE_SECONDS + 600, 20)
        self.corroborate("m5", NOW - 4 * 3600)
        self.assertEqual(su.survey_peers(self.cfg, self.sys, "m1").excluded, [])

    def test_reads_floor_long_enough_but_too_few_reads(self) -> None:
        # Two reads either side of a long sleep: the second is this survey.
        self.dark_since("m5", NOW - 4 * 3600, su.PEER_UNREACHABLE_MIN_READS - 2)
        self.corroborate("m5", NOW - 4 * 3600)
        self.assertEqual(su.survey_peers(self.cfg, self.sys, "m1").excluded, [])

    def test_a_readable_draining_peer_never_accrues_dark_time(self) -> None:
        self.sys.peers["m5"] = {"state": "draining", "participating": False}
        self.corroborate("m5", NOW - 4 * 3600)
        for hours in range(8):
            self.sys.clock = NOW + hours * 3600
            survey = su.survey_peers(self.cfg, self.sys, "m1", record=True)
            self.assertEqual(survey.excluded, [])
        self.assertNotIn("m5", su.peer_streaks(self.cfg))

    def test_a_plan_reads_the_record_but_never_writes_it(self) -> None:
        self.dark("m5")
        su.survey_peers(self.cfg, self.sys, "m1")
        self.assertFalse((self.cfg.state_dir / su.PEER_UNREADABLE_NAME).exists())
        su.survey_peers(self.cfg, self.sys, "m1", record=True)
        self.assertEqual(su.peer_streaks(self.cfg)["m5"]["reads"], 1)


class LifecycleTests(Fleet):
    def exclude_m5(self) -> None:
        self.dark("m5")
        su._write_json(self.cfg.state_dir / su.PEER_UNREADABLE_NAME,
                       {"m5": {"since": NOW - 4 * 3600, "reads": 7, "last": NOW - 1800,
                               "excluded": False}})
        self.corroborate("m5", NOW - 4 * 3600)
        self.assertEqual(su.survey_peers(self.cfg, self.sys, "m1", record=True).excluded, ["m5"])

    def test_the_first_readable_read_rejoins_it_and_ends_the_streak(self) -> None:
        self.exclude_m5()
        self.sys.peers["m5"] = {"state": "on", "participating": True}
        self.assertEqual(su.survey_peers(self.cfg, self.sys, "m1", record=True).excluded, [])
        self.assertNotIn("m5", su.peer_streaks(self.cfg))
        [row] = self.events("peer_unreachable_rejoined")
        self.assertEqual(row["fields"], {"peer": "m5", "dark_for_s": 4 * 3600})
        # Dark again: a fresh streak, not the old one.
        self.dark("m5")
        self.assertEqual(su.survey_peers(self.cfg, self.sys, "m1", record=True).excluded, [])
        self.assertEqual(su.peer_streaks(self.cfg)["m5"]["reads"], 1)

    def test_an_excluded_peer_still_dark_at_the_announce_re_read_is_skipped(self) -> None:
        self.exclude_m5()
        self.assertUpdated(self.apply())

    def test_an_excluded_peer_that_answers_at_the_announce_re_read_is_obeyed(self) -> None:
        self.exclude_m5()

        def m5_comes_back_updating(seconds: float) -> None:
            if seconds == su.ANNOUNCE_SETTLE_SECONDS:
                self.sys.peers["m5"] = {"state": "on", "participating": True}
                self.sys.peer_markers["m5"] = {"host_id": "m5", "target": "c" * 40,
                                               "ts": self.sys.clock - 600}
        self.sys.on_sleep = m5_comes_back_updating
        self.apply()
        self.assertIn("peer announced first", self.reason())
        self.assertEqual(self.sys.mutations(), [])

    def test_a_peer_dark_only_at_the_announce_re_read_is_handled_as_today(self) -> None:
        # Readable in the survey, dark at the re-read: no exclusion mid-run.
        def m5_goes_dark(seconds: float) -> None:
            if seconds == su.ANNOUNCE_SETTLE_SECONDS:
                self.dark("m5")
        self.sys.on_sleep = m5_goes_dark
        self.apply()
        self.assertIn("peer changed after announcing", self.reason())
        self.assertEqual(self.sys.mutations(), [])

    def test_the_capacity_floor_still_judges_the_drain(self) -> None:
        self.exclude_m5()
        self.sys.floor = {"allowed": False, "reason": "census_unreadable",
                          "message": "no census", "findings": []}
        self.assertEqual(self.apply(), su.EXIT_REFUSED)
        self.assertEqual(self.sys.mutations(), [])


class DoctorTests(Fleet):
    def test_the_record_drives_one_finding(self) -> None:
        check = fleet_doctor.check_peer_reachability
        self.assertEqual(check(su.peer_reachability(self.home)).code, "peer_reachability_ok")
        self.dark("m5")
        su.survey_peers(self.cfg, self.sys, "m1", record=True)
        dark = check(su.peer_reachability(self.home))
        self.assertEqual((dark.state, dark.code), (fleet_doctor.PROBLEM, "peer_unreachable"))
        self.assertIn("m5 since", dark.detail)
        su._write_json(self.cfg.state_dir / su.PEER_UNREADABLE_NAME,
                       {"m5": {"since": NOW - 4 * 3600, "reads": 7, "last": NOW,
                               "excluded": True}})
        excluded = check(su.peer_reachability(self.home))
        self.assertEqual(excluded.code, "peer_unreachable_excluded")
        self.assertIn("excluded from update turns", excluded.detail)
        (self.cfg.state_dir / su.PEER_UNREADABLE_NAME).write_text("{not json")
        unreadable = check(su.peer_reachability(self.home))
        self.assertEqual((unreadable.state, unreadable.code),
                         (fleet_doctor.UNKNOWN, "peer_reachability_unreadable"))


if __name__ == "__main__":
    unittest.main()
