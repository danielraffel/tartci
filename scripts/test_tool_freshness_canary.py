#!/usr/bin/env python3
"""Hermetic tests for the tool-freshness canary gate and steward check.

The fake host answers the tool commands, fleet-update (installing whatever
--to names, so a rollback really moves it back) and the ssh read of the
canary's events.jsonl. Each gate is exercised with its signal present and
absent. Settings are passed as dicts, never read from TOML, so the tests
also run under the hosts' tomllib-less Python.
"""

from __future__ import annotations

import testing_support  # noqa: E402,F401
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import state_age  # noqa: E402
import tool_freshness as tf  # noqa: E402
import tool_freshness_canary as canary  # noqa: E402
from test_tool_freshness import HOUR, NOW, Host, feed, iso, settings  # noqa: E402

MIN = 60.0


def stamp(ts: float) -> str:
    return tf._iso(ts).replace("-", "").replace(":", "")


class FleetHost(Host):
    """Host whose fleet-update installs the --to release and whose ssh reads a canary."""

    def __init__(self, versions: dict, canary_reply: tuple[int, str] | None = None):
        super().__init__(versions)
        self.canary_reply = canary_reply if canary_reply is not None else (255, "ssh: timeout")

    def run(self, argv: list[str], timeout: float) -> tuple[int, str]:
        if argv[0] == "ssh":
            self.calls.append(argv)
            return self.canary_reply
        if "fleet-update" in argv:
            self.apply_installs["shipyard"] = argv[argv.index("--to") + 1].lstrip("v")
        return super().run(argv, timeout)

    def fleet_updates(self) -> list[str]:
        return [c[c.index("--to") + 1] for c in self.calls if "fleet-update" in c]


def canary_events(*events: dict, clock: float = NOW) -> tuple[int, str]:
    return 0, f"{int(clock)}\n" + "".join(json.dumps(e) + "\n" for e in events)


def deployed(version: str, at: float, **extra) -> dict:
    value = {"event": "tool_deployed", "tool": "shipyard", "component": "cli", "at": iso(at),
             "from": "0.221.0", "to": version, "by": "auto_apply", "downgrade": False,
             "verify": "current"}
    value.update(extra)
    return value


class _Fleet(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.home / ".tartci")})
        patcher.start()
        self.addCleanup(patcher.stop)
        clock = mock.patch.object(state_age, "clock", lambda: NOW + 60)
        clock.start()
        self.addCleanup(clock.stop)
        self.state = tf.state_dir_for(self.home)
        self.feeds = {
            "danielraffel/Shipyard": feed(("v0.221.0", NOW - 30 * HOUR),
                                          ("v0.221.1", NOW - 4 * HOUR)),
            "Generous-Corp/pulp": feed(("v0.877.2", NOW - 30 * HOUR)),
        }

    def refresh(self, host: Host, me: str, now: float = NOW, **overrides) -> dict:
        value = settings(**{"host_class": me, "canary_host": "m5", "canary_ssh": "m5",
                            **overrides})
        value["tools"].pop("tart")
        ready = {"ready": True, "missing": [], "sums": None, "detail": "ready"}
        return tf.refresh(self.home, now, settings=value, run=host.run,
                          feed=lambda repo: self.feeds[repo],
                          probe=lambda name, tag: {"platform": "darwin-arm64", "result": ready})

    def switch_host(self) -> None:
        """Move to a fresh home and state dir: another host's own disk."""
        self.hosts = getattr(self, "hosts", 0) + 1
        self.home = Path(self.tmp.name) / f"host{self.hosts}"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.home / ".tartci")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = tf.state_dir_for(self.home)

    def events(self) -> list[dict]:
        path = self.state / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def behind(self, reply: tuple[int, str] | None = None) -> FleetHost:
        return FleetHost({"shipyard": "0.221.0", "pulp": "0.877.2"}, reply)


class CanaryGateTests(_Fleet):
    # ── canary applies, others wait ──

    def test_the_canary_applies_and_the_others_wait(self) -> None:
        canary_host = self.behind()
        row = self.refresh(canary_host, "m5")["tools"]["shipyard"]
        self.assertEqual(canary_host.fleet_updates(), ["v0.221.1"])
        self.assertEqual(row["state"], "current")
        self.assertFalse(any(c[0] == "ssh" for c in canary_host.calls))  # never reads itself
        deploy = [e for e in self.events() if e["event"] == "tool_deployed"]
        self.assertEqual([(e["to"], e["verify"]) for e in deploy], [("0.221.1", "current")])

        self.switch_host()
        other = self.behind(canary_events())  # canary readable, nothing deployed yet
        row = self.refresh(other, "studio")["tools"]["shipyard"]
        self.assertEqual(other.fleet_updates(), [])
        self.assertIn("waiting for canary m5: v0.221.1 has not deployed 0.221.1", row["apply"])
        ssh = [c for c in other.calls if c[0] == "ssh"]
        self.assertEqual(len(ssh), 1, "the canary is read once per pass")
        self.assertIn("m5", ssh[0])

    # ── canary verified and healthy releases the others ──

    def test_a_canary_deploy_healthy_for_the_soak_releases_the_others(self) -> None:
        young = self.behind(canary_events(deployed("0.221.1", NOW - 1 * HOUR)))
        row = self.refresh(young, "studio")["tools"]["shipyard"]
        self.assertEqual(young.fleet_updates(), [])
        self.assertIn("deployed 0.221.1 1.0 h ago; healthy once 2 h pass", row["apply"])

        soaked = self.behind(canary_events(deployed("0.221.1", NOW - 3 * HOUR)))
        row = self.refresh(soaked, "studio")["tools"]["shipyard"]
        self.assertEqual(soaked.fleet_updates(), ["v0.221.1"])
        self.assertIn("cleared by canary m5", row["canary"])

    def test_a_deploy_that_did_not_verify_does_not_clear(self) -> None:
        host = self.behind(canary_events(deployed("0.221.1", NOW - 3 * HOUR, verify="unknown")))
        self.refresh(host, "studio")
        self.assertEqual(host.fleet_updates(), [])

    def test_a_steward_canary_clears_only_after_a_clean_tick(self) -> None:
        deploy = deployed("0.221.1", NOW - 3 * HOUR, steward_host=True)
        waiting = self.behind(canary_events(deploy))
        row = self.refresh(waiting, "studio")["tools"]["shipyard"]
        self.assertEqual(waiting.fleet_updates(), [])
        self.assertIn("waiting for a clean steward tick", row["apply"])

        verified = {"event": "tool_steward_verified", "tool": "shipyard",
                    "at": iso(NOW - 2 * HOUR), "version": "0.221.1"}
        host = self.behind(canary_events(deploy, verified))
        self.refresh(host, "studio")
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])

    def test_the_newest_release_the_canary_cleared_is_applied(self) -> None:
        self.feeds["danielraffel/Shipyard"] = feed(
            ("v0.221.0", NOW - 30 * HOUR), ("v0.221.1", NOW - 6 * HOUR), ("v0.221.2", NOW - HOUR))
        host = self.behind(canary_events(deployed("0.221.1", NOW - 5 * HOUR),
                                         deployed("0.221.2", NOW - 0.5 * HOUR, **{"from": "0.221.1"})))
        self.refresh(host, "studio")
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])

    # ── canary rollback blocks them ──

    def test_a_canary_rollback_or_failure_blocks_the_others(self) -> None:
        deploy = deployed("0.221.1", NOW - 5 * HOUR)
        blockers = {
            "rolled_back": {"event": "tool_rolled_back", "tool": "shipyard",
                            "at": iso(NOW - 4 * HOUR), "from": "0.221.1", "to": "0.221.0"},
            "apply_failed": {"event": "tool_apply_failed", "tool": "shipyard",
                             "at": iso(NOW - 4 * HOUR), "target": "0.221.1"},
            "downgrade": deployed("0.221.0", NOW - 4 * HOUR, **{"from": "0.221.1",
                                                                "downgrade": True}),
            "steward_failed": {"event": "tool_steward_failed", "tool": "shipyard",
                               "at": iso(NOW - 4 * HOUR), "version": "0.221.1"},
        }
        for name, blocker in blockers.items():
            with self.subTest(name):
                host = self.behind(canary_events(deploy, blocker))
                row = self.refresh(host, "studio")["tools"]["shipyard"]
                self.assertEqual(host.fleet_updates(), [])
                self.assertIn("waiting for canary m5", row["apply"])
        # Control: the same deploy with no blocker clears.
        host = self.behind(canary_events(deploy))
        self.refresh(host, "studio")
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])

    def test_a_failure_before_the_deploy_does_not_block(self) -> None:
        earlier = {"event": "tool_apply_failed", "tool": "shipyard",
                   "at": iso(NOW - 6 * HOUR), "target": "0.221.1"}
        host = self.behind(canary_events(earlier, deployed("0.221.1", NOW - 5 * HOUR)))
        self.refresh(host, "studio")
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])

    # ── unreadable canary keeps them waiting ──

    def test_an_unreadable_canary_keeps_them_waiting_and_logs_one_line_per_pass(self) -> None:
        self.feeds["Generous-Corp/pulp"] = feed(("v0.877.2", NOW - 30 * HOUR),
                                               ("v0.877.3", NOW - 3 * HOUR))
        for reply in ((255, "ssh: connect to host m5: Operation timed out"),
                      (0, "not-a-clock\n"),
                      # the clock arrived, then the connection dropped mid-read
                      (255, f"{int(NOW)}\nclient_loop: send disconnect: Broken pipe")):
            with self.subTest(reply=reply):
                host = FleetHost({"shipyard": "0.221.0", "pulp": "0.877.2"}, reply)
                host.apply_installs["pulp"] = "0.877.3"
                rows = self.refresh(host, "studio")["tools"]
                self.assertEqual(host.fleet_updates(), [])
                self.assertFalse(any("pulp-install" in c for c in host.calls))
                for tool in ("shipyard", "pulp"):
                    self.assertIn("waiting for canary m5: unreadable", rows[tool]["apply"])
                self.assertEqual(len([c for c in host.calls if c[0] == "ssh"]), 1)
        lines = (self.state / "canary-gate.log").read_text().splitlines()
        self.assertEqual(len(lines), 3)  # one line per pass, naming both tools
        self.assertIn("shipyard: waiting for canary m5: unreadable", lines[0])
        self.assertIn("pulp: waiting for canary m5: unreadable", lines[0])

    def test_an_unsafe_canary_target_is_refused_not_run(self) -> None:
        host = self.behind(canary_events(deployed("0.221.1", NOW - 3 * HOUR)))
        row = self.refresh(host, "studio", canary_ssh="-oProxyCommand=evil")["tools"]["shipyard"]
        self.assertFalse(any(c[0] == "ssh" for c in host.calls))
        self.assertEqual(host.fleet_updates(), [])
        self.assertIn("not a safe SSH destination", row["apply"])

    def test_the_canary_target_comes_from_the_published_host_row(self) -> None:
        self.assertEqual(canary.canary_target("studio", {}), "m3")
        self.assertEqual(canary.canary_target("nohost", {}), "tartci-nohost")
        self.assertEqual(canary.canary_target("m5", {"canary_ssh": "m5-lan"}), "m5-lan")

    def test_self_host_id_reads_the_profile_without_tomllib(self) -> None:
        profile = self.home / ".config/tartci/macos-fleet-profile.toml"
        profile.parent.mkdir(parents=True)
        profile.write_text('[runner]\nid = "x"\n[host]\nid = "m5"  # canary\nssh = "m5"\n')
        self.assertEqual(canary.self_host_id(self.home, {"host_class": "studio"}), "m5")


class StewardCheckTests(_Fleet):
    """A host that runs the steward judges a Shipyard swap by the next tick."""

    def log(self, *lines: str) -> None:
        path = self.home / canary.STEWARD_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write("".join(line + "\n" for line in lines))

    def tick(self, at: float, ver: str, invariants: str = "ok") -> None:
        self.log(f"{stamp(at)} fleet invariants={invariants}",
                 f"{stamp(at)} danielraffel/tartci rc=0 enqueued=0 errors=0 took=9s "
                 f"ver={ver} invariants={invariants}",
                 f"{stamp(at)} Generous-Corp/pulp rc=0 enqueued=1 took=20s ver={ver} "
                 "invariants=ok oldest=#1:required_pending:3m")

    def deploy_on_steward_host(self, before: str = "ok") -> FleetHost:
        """The canary is also the steward host here (me = m5)."""
        self.tick(NOW - 10 * MIN, "0.221.0", before)
        host = self.behind()
        self.refresh(host, "m5")
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])
        deploy = [e for e in self.events() if e["event"] == "tool_deployed"][-1]
        self.assertTrue(deploy["steward_host"])
        return host

    def test_a_steward_invariants_failure_rolls_back_and_is_not_retried(self) -> None:
        host = self.deploy_on_steward_host()
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:clones,arming")
        row = self.refresh(host, "m5", now=NOW + 10 * MIN)["tools"]["shipyard"]
        self.assertEqual(host.fleet_updates(), ["v0.221.1", "v0.221.0"])
        self.assertEqual(host.versions["shipyard"], "0.221.0")
        self.assertIn("rollback to v0.221.0 landed", row["steward_problem"])
        kinds = [e["event"] for e in self.events()]
        self.assertEqual(kinds, ["tool_deployed", "tool_apply_failed", "tool_rolled_back",
                                 "tool_deployed"])
        failed = self.events()[1]
        self.assertEqual((failed["target"], failed["verdict"]), ("0.221.1", "steward_invariants"))
        self.assertIn("arming,clones", failed["detail"])
        back = self.events()[3]
        self.assertEqual((back["to"], back["by"], back["downgrade"]),
                         ("0.221.0", "steward_rollback", True))
        # The rolled-back release is not re-applied here, even after the retry window.
        row = self.refresh(host, "m5", now=NOW + 8 * HOUR)["tools"]["shipyard"]
        self.assertEqual(host.fleet_updates(), ["v0.221.1", "v0.221.0"])
        self.assertIn("not retrying v0.221.1 here", row["apply"])
        # And the canary's record blocks every other host.
        reply = canary_events(*self.events(), clock=NOW + 8 * HOUR)
        self.switch_host()
        other = self.behind(reply)
        row = self.refresh(other, "studio", now=NOW + 8 * HOUR)["tools"]["shipyard"]
        self.assertEqual(other.fleet_updates(), [])
        self.assertIn("recorded tool_apply_failed for 0.221.1", row["apply"])

    def test_a_clean_tick_verifies_and_releases_the_others(self) -> None:
        host = self.deploy_on_steward_host()
        self.tick(NOW + 5 * MIN, "0.221.1", "ok")
        row = self.refresh(host, "m5", now=NOW + 10 * MIN)["tools"]["shipyard"]
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])
        self.assertIn("invariants=ok", row["steward"])
        self.assertEqual(self.events()[-1]["event"], "tool_steward_verified")
        other = self.behind(canary_events(*self.events(), clock=NOW + 10 * MIN))
        self.switch_host()
        self.refresh(other, "studio", now=NOW + 10 * MIN)
        self.assertEqual(other.fleet_updates(), ["v0.221.1"])

    def test_a_tick_on_the_old_version_or_before_the_swap_does_not_judge(self) -> None:
        host = self.deploy_on_steward_host()
        self.tick(NOW - 1 * MIN, "0.221.1", "FAIL:clones")  # started before the swap
        self.tick(NOW + 5 * MIN, "0.221.0", "FAIL:clones")  # ran the old binary
        row = self.refresh(host, "m5", now=NOW + 10 * MIN)["tools"]["shipyard"]
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])
        self.assertIn("no judging steward tick", row["steward"])
        self.assertNotIn("steward_problem", row)
        row = self.refresh(host, "m5", now=NOW + 2 * HOUR)["tools"]["shipyard"]
        self.assertIn("is the steward tick running?", row["steward_problem"])

    def test_a_failure_already_failing_before_the_swap_is_not_rolled_back(self) -> None:
        host = self.deploy_on_steward_host(before="FAIL:clones")
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:clones")
        self.refresh(host, "m5", now=NOW + 10 * MIN)
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])
        self.tick(NOW + 15 * MIN, "0.221.1", "FAIL:clones,arming")
        self.refresh(host, "m5", now=NOW + 20 * MIN)
        self.assertEqual(host.fleet_updates(), ["v0.221.1", "v0.221.0"])

    def test_a_host_without_a_running_steward_records_no_check(self) -> None:
        self.tick(NOW - 3 * HOUR, "0.221.0")  # an old log: the steward no longer runs here
        host = self.behind()
        self.refresh(host, "m5")
        self.assertNotIn("steward_host", self.events()[-1])
        self.assertEqual(json.loads((self.state / "steward.json").read_text()), {})

    def test_a_failed_rollback_is_retried_then_left_as_a_problem(self) -> None:
        host = self.deploy_on_steward_host()
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:clones")
        host.fleet_update_results = [(1, "rollout lock busy")] * canary.ROLLBACK_MAX_TRIES
        for n in range(canary.ROLLBACK_MAX_TRIES + 1):
            row = self.refresh(host, "m5", now=NOW + (10 + n) * MIN)["tools"]["shipyard"]
        self.assertEqual(len(host.fleet_updates()), 1 + canary.ROLLBACK_MAX_TRIES)
        self.assertIn("roll back by hand", row["steward_problem"])
        self.assertIn("roll back by hand", tf.summary(self.home)["problem"])
        self.assertEqual([e["event"] for e in self.events()].count("tool_apply_failed"), 1)


if __name__ == "__main__":
    unittest.main()
