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
from typing import Any
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import state_age  # noqa: E402
import tool_freshness as tf  # noqa: E402
import tool_freshness_canary as canary  # noqa: E402
from test_tool_freshness import HOUR, NOW, Host, feed, iso, make_generation, settings  # noqa: E402

MIN = 60.0


def stamp(ts: float) -> str:
    return tf._iso(ts).replace("-", "").replace(":", "")


class FleetHost(Host):
    """Host whose fleet-update installs the --to release and whose ssh reads peers.

    `reply` answers every ssh read, or maps an ssh destination to its answer.
    `stuck_rollback` makes a fleet-update to an older release exit 0 with a
    verified summary while installing nothing.
    """

    def __init__(self, versions: dict, reply: Any = None, generation: str | None = None):
        super().__init__(versions, generation)
        self.reply = reply if reply is not None else (255, "ssh: timeout")
        self.stuck_rollback = False
        self.fail_pulp_install = False

    def run(self, argv: list[str], timeout: float) -> tuple[int, str]:
        if argv[0] == "ssh":
            self.calls.append(argv)
            if isinstance(self.reply, dict):
                return self.reply.get(argv[-2], (255, f"ssh: {argv[-2]}: unknown host"))
            return self.reply
        if "pulp-install" in argv and self.fail_pulp_install:
            raise RuntimeError("watchdog killed the pass")
        if "fleet-update" in argv:
            target = argv[argv.index("--to") + 1].lstrip("v")
            if self.stuck_rollback and tf._version(target) < tf._version(self.versions["shipyard"]):
                self.calls.append(argv)
                return 0, json.dumps({"event": "fleet_summary", "verdict": "verified"})
            self.apply_installs["shipyard"] = target
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
        self.feed_calls = 0
        self.feed_fails: set[int] = set()  # 1-based feed reads that fail

    def feed(self, repo: str) -> str:
        self.feed_calls += 1
        if self.feed_calls in self.feed_fails:
            raise OSError("feed timed out")
        return self.feeds[repo]

    def refresh(self, host: Host, me: str, now: float = NOW, stewards: list | None = None,
                clock: Any = None, **overrides) -> dict:
        value = settings(**{"host_class": me,
                            "tool_rollout": {"canary": "m5", "steward_hosts": stewards or []},
                            "ssh_targets": {"m5": "m5", "studio": "m3"}, **overrides})
        value["tools"].pop("tart", None)
        ready = {"ready": True, "missing": [], "sums": None, "detail": "ready"}
        return tf.refresh(self.home, now, settings=value, run=host.run, feed=self.feed,
                          probe=lambda name, tag: {"platform": "darwin-arm64", "result": ready},
                          clock=clock)

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

    def test_an_unidentified_host_never_applies_even_once_the_canary_cleared(self) -> None:
        # No fleet profile and no host_class: this host cannot tell whether it is
        # the canary or a steward host, so it must not apply a cleared release.
        verified = {"event": "tool_steward_verified", "tool": "shipyard",
                    "at": iso(NOW - 2 * HOUR), "version": "0.221.1"}
        host = self.behind(canary_events(deployed("0.221.1", NOW - 3 * HOUR, steward_host=True),
                                         verified))
        row = self.refresh(host, None, stewards=["m5"])["tools"]["shipyard"]
        self.assertEqual(host.fleet_updates(), [])
        self.assertIn("this host's id is unknown", row["apply"])
        self.assertFalse(any(c[0] == "ssh" for c in host.calls), "nothing to read until identified")

    def test_the_gate_itself_refuses_an_unidentified_host(self) -> None:
        # The gate fails closed on its own, whether or not the apply command
        # later needs {host_class}.
        host = self.behind(canary_events(deployed("0.221.1", NOW - 3 * HOUR)))
        value = settings(**{"host_class": None, "tool_rollout": {"canary": "m5", "steward_hosts": []},
                            "ssh_targets": {"m5": "m5"}})
        soaked = [{"version": "0.221.1", "tag": "v0.221.1"}]
        got, note = canary.gate_candidate("shipyard", {"canary_gate": True}, soaked, self.home,
                                          NOW, value, self.state, host.run, {})
        self.assertIsNone(got)
        self.assertIn("this host's id is unknown", note)
        value["host_class"] = "studio"  # the same cleared canary releases an identified host
        got, _ = canary.gate_candidate("shipyard", {"canary_gate": True}, soaked, self.home,
                                       NOW, value, self.state, host.run, {})
        self.assertEqual(got, soaked[-1])

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
        row = self.refresh(host, "studio",
                           ssh_targets={"m5": "-oProxyCommand=evil"})["tools"]["shipyard"]
        self.assertFalse(any(c[0] == "ssh" for c in host.calls))
        self.assertEqual(host.fleet_updates(), [])
        self.assertIn("not a safe SSH destination", row["apply"])

    def test_the_ssh_target_comes_from_the_published_host_row(self) -> None:
        self.assertEqual(canary.host_target("studio", {}), "m3")
        self.assertEqual(canary.host_target("nohost", {}), "tartci-nohost")
        self.assertEqual(canary.host_target("m5", {"ssh_targets": {"m5": "m5-lan"}}), "m5-lan")

    def test_self_host_id_reads_the_profile_without_tomllib(self) -> None:
        profile = self.home / ".config/tartci/macos-fleet-profile.toml"
        profile.parent.mkdir(parents=True)
        profile.write_text('[runner]\nid = "x"\n[host]\nid = "m5"  # canary\nssh = "m5"\n')
        self.assertEqual(canary.self_host_id(self.home, {"host_class": "studio"}), "m5")


    # ── the canary's later record, the soak boundary, the verified order ──

    def test_the_newest_deploy_of_a_release_decides(self) -> None:
        # A failed first apply, then a verified redeploy: the redeploy is what serves.
        failed = {"event": "tool_apply_failed", "tool": "shipyard",
                  "at": iso(NOW - 9 * HOUR), "target": "0.221.1"}
        host = self.behind(canary_events(deployed("0.221.1", NOW - 10 * HOUR), failed,
                                         deployed("0.221.1", NOW - 3 * HOUR)))
        self.refresh(host, "studio")
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])

    def test_the_soak_clears_at_exactly_its_length(self) -> None:
        host = self.behind(canary_events(deployed("0.221.1", NOW - 2 * HOUR)))
        self.refresh(host, "studio")
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])

    def test_a_steward_verification_from_before_the_deploy_does_not_count(self) -> None:
        stale = {"event": "tool_steward_verified", "tool": "shipyard",
                 "at": iso(NOW - 5 * HOUR), "version": "0.221.1"}
        host = self.behind(canary_events(stale, deployed("0.221.1", NOW - 3 * HOUR,
                                                         steward_host=True)))
        self.refresh(host, "studio")
        self.assertEqual(host.fleet_updates(), [])

    # ── steward hosts gate the rest of the fleet (published roles) ──

    def test_other_hosts_also_wait_for_the_steward_host_to_prove_a_release(self) -> None:
        cleared_canary = canary_events(deployed("0.221.1", NOW - 3 * HOUR))
        steward_deploy = deployed("0.221.1", NOW - 0.5 * HOUR, steward_host=True)
        # The steward host itself waits only for the canary.
        host = self.behind({"m5": cleared_canary})
        self.refresh(host, "studio", stewards=["studio"])
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])
        # Any other host waits for the steward host's clean tick too...
        # A steward host must prove it with a clean tick even when its deploy
        # event does not say it runs the steward (its tick was down then).
        for studio in ((255, "ssh: timeout"), canary_events(),
                       canary_events(steward_deploy),
                       canary_events(deployed("0.221.1", NOW - 3 * HOUR))):
            with self.subTest(studio=studio[1][:40]):
                self.switch_host()
                host = self.behind({"m5": cleared_canary, "m3": studio})
                row = self.refresh(host, "m1", stewards=["studio"])["tools"]["shipyard"]
                self.assertEqual(host.fleet_updates(), [])
                self.assertIn("steward host studio", row["apply"])
        # ...and goes once it has one.
        verified = {"event": "tool_steward_verified", "tool": "shipyard",
                    "at": iso(NOW - 0.2 * HOUR), "version": "0.221.1"}
        self.switch_host()
        host = self.behind({"m5": cleared_canary, "m3": canary_events(steward_deploy, verified)})
        row = self.refresh(host, "m1", stewards=["studio"])["tools"]["shipyard"]
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])
        self.assertIn("steward host studio: deployed 0.221.1 and a steward tick", row["canary"])

    def test_pulp_does_not_wait_for_the_steward_host(self) -> None:
        self.feeds["Generous-Corp/pulp"] = feed(("v0.877.2", NOW - 30 * HOUR),
                                               ("v0.877.3", NOW - 4 * HOUR))
        pulp = dict(deployed("0.877.3", NOW - 3 * HOUR), tool="pulp", **{"from": "0.877.2"})
        host = FleetHost({"shipyard": "0.221.1", "pulp": "0.877.2"},
                         {"m5": canary_events(pulp), "m3": (255, "ssh: timeout")})
        host.apply_installs["pulp"] = "0.877.3"
        self.refresh(host, "m1", stewards=["studio"])
        self.assertTrue(any("pulp-install" in c for c in host.calls))
        self.assertFalse(any(c[0] == "ssh" and "m3" in c for c in host.calls))

    def test_the_roles_come_from_the_published_supply(self) -> None:
        published = json.loads(canary.PUBLISHED.read_text())["tool_rollout"]
        self.assertEqual(canary.rollout({}), {"canary": published["canary"],
                                              "steward_hosts": published["steward_hosts"],
                                              "error": None})
        self.assertEqual(published, {"canary": "m5", "steward_hosts": ["studio"]})
        with mock.patch.object(canary, "PUBLISHED", Path(self.tmp.name) / "missing.json"):
            self.assertIsNotNone(canary.rollout({})["error"])
            host = self.behind(canary_events(deployed("0.221.1", NOW - 3 * HOUR)))
            value = settings(host_class="m5")
            value.pop("tool_rollout")
            row = tf.refresh(self.home, NOW, settings=value, run=host.run, feed=self.feed,
                             probe=lambda name, tag: {"platform": "darwin-arm64",
                                                      "result": {"ready": True}})["tools"]
        # No published canary: nobody applies, not even the host named m5.
        self.assertEqual(host.fleet_updates(), [])
        self.assertIn("no tool_rollout canary", row["shipyard"]["apply"])

    def test_tool_freshness_toml_cannot_name_the_canary(self) -> None:
        if tf.tomllib is None:
            self.skipTest("needs tomllib")
        path = self.home / "tool-freshness.toml"
        path.write_text('canary_host = "m1"\ncanary_ssh = "m1"\n')
        value = tf.load_settings(self.home, path)
        self.assertNotIn("canary_host", value)
        self.assertNotIn("tool_rollout", value)

    # ── a deploy whose verification failed is re-recorded ──

    def test_a_deploy_measured_unknown_is_re_recorded_once_verified(self) -> None:
        host = self.behind()
        self.feed_fails = {2}  # the re-read right after the swap fails
        self.refresh(host, "m5")
        first = [e for e in self.events() if e["event"] == "tool_deployed"]
        self.assertEqual([(e["to"], e["verify"]) for e in first], [("0.221.1", "unknown")])
        self.refresh(host, "m5", now=NOW + 30 * MIN)
        deploys = [e for e in self.events() if e["event"] == "tool_deployed"]
        self.assertEqual([(e["to"], e["verify"], e.get("rerecorded")) for e in deploys],
                         [("0.221.1", "unknown", None), ("0.221.1", "current", True)])
        self.refresh(host, "m5", now=NOW + 60 * MIN)
        self.assertEqual(len([e for e in self.events() if e["event"] == "tool_deployed"]), 2)
        reply = canary_events(*self.events(), clock=NOW + 3 * HOUR)
        self.switch_host()
        other = self.behind(reply)
        self.refresh(other, "studio", now=NOW + 3 * HOUR)
        self.assertEqual(other.fleet_updates(), ["v0.221.1"])


REPOS = ["danielraffel/tartci", "Generous-Corp/pulp", "danielraffel/Shipyard"]


class StewardCheckTests(_Fleet):
    """A host that runs the steward judges a Shipyard swap by complete ticks."""

    def setUp(self) -> None:
        super().setUp()
        self.repos(REPOS)

    def repos(self, names: list[str] | None) -> None:
        path = self.home / canary.STEWARD_REPOS
        path.parent.mkdir(parents=True, exist_ok=True)
        if names is None:
            path.unlink()
        else:
            path.write_text("# fleet repos\n" + "".join(n + "\n" for n in names))
            os.utime(path, (NOW - 24 * HOUR, NOW - 24 * HOUR))  # unchanged since long before

    def log(self, *lines: str) -> None:
        path = self.home / canary.STEWARD_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write("".join(line + "\n" for line in lines))

    def tick(self, at: float, ver: str, invariants: str = "ok") -> None:
        """A complete tick: the fleet line, then every repository's final line."""
        self.log(f"{stamp(at)} fleet invariants={invariants}",
                 f"{stamp(at)} danielraffel/tartci rc=0 enqueued=0 errors=0 took=9s "
                 f"ver={ver} invariants={invariants}",
                 f"{stamp(at)} Generous-Corp/pulp rc=0 enqueued=1 took=20s ver={ver} "
                 "invariants=ok oldest=#1:required_pending:3m",
                 f"{stamp(at)} danielraffel/Shipyard rc=0 took=30s ver={ver} invariants=ok")

    def deploy_on_steward_host(self, before: str = "ok") -> FleetHost:
        """The canary is also the steward host here (me = m5)."""
        self.tick(NOW - 10 * MIN, "0.221.0", before)
        host = self.behind()
        self.refresh(host, "m5")
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])
        deploy = [e for e in self.events() if e["event"] == "tool_deployed"][-1]
        self.assertTrue(deploy["steward_host"])
        return host

    def kinds(self) -> list[str]:
        return [e["event"] for e in self.events()]

    def test_a_steward_invariants_failure_rolls_back_and_is_not_retried(self) -> None:
        host = self.deploy_on_steward_host()
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:clones,arming")
        row = self.refresh(host, "m5", now=NOW + 10 * MIN)["tools"]["shipyard"]
        self.assertEqual(host.fleet_updates(), ["v0.221.1", "v0.221.0"])
        self.assertEqual(host.versions["shipyard"], "0.221.0")
        self.assertIn("rollback to v0.221.0 landed", row["steward_problem"])
        self.assertEqual(self.kinds(), ["tool_deployed", "tool_apply_failed", "tool_rolled_back",
                                        "tool_deployed"])
        failed = self.events()[1]
        self.assertEqual((failed["target"], failed["verdict"]), ("0.221.1", "steward_invariants"))
        self.assertIn("arming,clones", failed["detail"])
        back = self.events()[3]
        self.assertEqual((back["to"], back["by"], back["downgrade"]),
                         ("0.221.0", "steward_rollback", True))
        row = self.refresh(host, "m5", now=NOW + 8 * HOUR)["tools"]["shipyard"]
        self.assertEqual(host.fleet_updates(), ["v0.221.1", "v0.221.0"])
        self.assertIn("not retrying v0.221.1 here", row["apply"])
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
        self.assertEqual(self.kinds()[-1], "tool_steward_verified")
        reply = canary_events(*self.events(), clock=NOW + 10 * MIN)
        self.switch_host()
        other = self.behind(reply)
        self.refresh(other, "studio", now=NOW + 10 * MIN)
        self.assertEqual(other.fleet_updates(), ["v0.221.1"])

    # ── only complete ticks judge ──

    def test_a_partial_tick_neither_verifies_nor_hides_a_later_repos_failure(self) -> None:
        host = self.deploy_on_steward_host()
        at = NOW + 5 * MIN
        self.log(f"{stamp(at)} fleet invariants=ok",
                 f"{stamp(at)} danielraffel/tartci rc=0 took=9s ver=0.221.1 invariants=ok")
        row = self.refresh(host, "m5", now=NOW + 7 * MIN)["tools"]["shipyard"]
        self.assertNotIn("tool_steward_verified", self.kinds())
        self.assertIn("no conclusive complete steward tick", row["steward"])
        self.log(f"{stamp(at)} Generous-Corp/pulp rc=0 took=20s ver=0.221.1 invariants=ok",
                 f"{stamp(at)} danielraffel/Shipyard rc=0 took=60s ver=0.221.1 "
                 "invariants=FAIL:arming")
        self.refresh(host, "m5", now=NOW + 12 * MIN)
        self.assertEqual(host.fleet_updates(), ["v0.221.1", "v0.221.0"])

    def test_without_the_repo_list_a_tick_is_complete_once_the_next_one_starts(self) -> None:
        host = self.deploy_on_steward_host()
        self.repos(None)
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:arming")
        self.refresh(host, "m5", now=NOW + 7 * MIN)
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])  # last tick: maybe still running
        self.log(f"{stamp(NOW + 10 * MIN)} fleet invariants=ok")
        self.refresh(host, "m5", now=NOW + 12 * MIN)
        self.assertEqual(host.fleet_updates(), ["v0.221.1", "v0.221.0"])

    # ── the baseline and the version filter ──

    def test_a_tick_in_flight_at_the_swap_is_not_the_baseline(self) -> None:
        self.tick(NOW - 10 * MIN, "0.221.0", "ok")  # finished before the swap: the baseline
        host = self.behind()
        self.refresh(host, "m5")  # the swap at NOW
        # Started before the swap (old ver=); its later repos ran the new binary.
        at = NOW - 1 * MIN
        self.log(f"{stamp(at)} fleet invariants=ok",
                 f"{stamp(at)} danielraffel/tartci rc=0 took=9s ver=0.221.0 invariants=ok",
                 f"{stamp(at)} Generous-Corp/pulp rc=0 took=9s ver=0.221.0 invariants=ok",
                 f"{stamp(at)} danielraffel/Shipyard rc=0 took=60s ver=0.221.0 "
                 "invariants=FAIL:arming")
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:arming")
        self.refresh(host, "m5", now=NOW + 10 * MIN)
        self.assertEqual(host.fleet_updates(), ["v0.221.1", "v0.221.0"])

    def test_a_tick_on_the_old_version_or_before_the_swap_does_not_judge(self) -> None:
        host = self.deploy_on_steward_host()  # clean baseline
        self.tick(NOW - 1 * MIN, "0.221.1", "FAIL:straddle")  # started before the swap
        self.tick(NOW + 5 * MIN, "0.221.0", "FAIL:oldbinary")  # ran the old binary
        self.log(f"{stamp(NOW + 9 * MIN)} fleet invariants=ok")  # completes it
        row = self.refresh(host, "m5", now=NOW + 10 * MIN)["tools"]["shipyard"]
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])
        self.assertIn("no conclusive complete steward tick", row["steward"])
        self.assertNotIn("steward_problem", row)
        row = self.refresh(host, "m5", now=NOW + 2 * HOUR)["tools"]["shipyard"]
        self.assertIn("is the steward tick running?", row["steward_problem"])

    def test_a_failure_already_failing_before_the_swap_is_not_rolled_back(self) -> None:
        self.tick(NOW - 20 * MIN, "0.221.0", "FAIL:clones")
        host = self.deploy_on_steward_host(before="FAIL:clones")
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:clones")
        self.refresh(host, "m5", now=NOW + 10 * MIN)
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])
        self.assertNotIn("tool_steward_verified", self.kinds())
        self.tick(NOW + 15 * MIN, "0.221.1", "FAIL:clones,arming")
        self.refresh(host, "m5", now=NOW + 20 * MIN)
        self.assertEqual(host.fleet_updates(), ["v0.221.1", "v0.221.0"])

    # ── error lines and error tokens ──

    def test_a_steward_that_cannot_run_rolls_back(self) -> None:
        host = self.deploy_on_steward_host()
        at = NOW + 5 * MIN
        self.log(*(f"{stamp(at)} {repo} ERROR rc=101 thread main panicked" for repo in REPOS))
        row = self.refresh(host, "m5", now=NOW + 10 * MIN)["tools"]["shipyard"]
        self.assertEqual(host.fleet_updates(), ["v0.221.1", "v0.221.0"])
        self.assertIn("the steward could not run", row["steward_problem"])

    def test_a_steward_outage_that_predates_the_swap_is_not_the_releases_fault(self) -> None:
        self.tick(NOW - 20 * MIN, "0.221.0", "ok")
        # The tick in flight at the swap errored everywhere, including the
        # repositories that ran before the swap: GitHub was already down.
        self.log(*(f"{stamp(NOW - 5 * MIN)} {repo} ERROR rc=1 api.github.com 503" for repo in REPOS))
        host = self.behind()
        self.refresh(host, "m5")
        self.log(*(f"{stamp(NOW + 5 * MIN)} {repo} ERROR rc=1 api.github.com 503" for repo in REPOS))
        row = self.refresh(host, "m5", now=NOW + 10 * MIN)["tools"]["shipyard"]
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])
        self.assertIn("no conclusive complete steward tick", row["steward"])
        # A real regression after the outage still fails it.
        self.tick(NOW + 15 * MIN, "0.221.1", "FAIL:arming")
        self.refresh(host, "m5", now=NOW + 20 * MIN)
        self.assertEqual(host.fleet_updates(), ["v0.221.1", "v0.221.0"])

    def test_a_rollback_that_cannot_download_is_retried_and_the_failure_stands(self) -> None:
        host = self.deploy_on_steward_host()
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:arming")
        outage = canary.ROLLBACK_MAX_TRIES + 2
        host.fleet_update_results = [(75, "not ready: download failed")] * outage
        for n in range(outage):
            row = self.refresh(host, "m5", now=NOW + (10 + n) * MIN)["tools"]["shipyard"]
            self.assertIn("not ready yet, retried every pass", row["steward_problem"])
            if n == 1:
                # GitHub recovers for the steward first, and the failing tick
                # has left the log (truncated): a clean tick must not un-fail a
                # release whose failure is already on record.
                (self.home / canary.STEWARD_LOG).write_text("")
                self.tick(NOW + (10 + n) * MIN + 30, "0.221.1", "ok")
        self.assertNotIn("roll back by hand", row["steward_problem"])
        self.assertNotIn("tool_steward_verified", self.kinds())
        self.assertFalse(json.loads((self.state / "steward.json").read_text())["shipyard"]
                         .get("gave_up"))
        row = self.refresh(host, "m5", now=NOW + 30 * MIN)["tools"]["shipyard"]
        self.assertEqual(host.versions["shipyard"], "0.221.0")
        self.assertIn("rollback to v0.221.0 landed", row["steward_problem"])
        self.assertEqual(self.kinds().count("tool_apply_failed"), 1)

    def test_a_repo_list_changed_after_the_tick_started_does_not_complete_it(self) -> None:
        host = self.deploy_on_steward_host()
        at = NOW + 5 * MIN
        self.log(f"{stamp(at)} fleet invariants=ok",
                 f"{stamp(at)} danielraffel/tartci rc=0 took=9s ver=0.221.1 invariants=ok",
                 f"{stamp(at)} Generous-Corp/pulp rc=0 took=9s ver=0.221.1 invariants=ok")
        # danielraffel/Shipyard is removed from the list while the tick runs it.
        self.repos(REPOS[:2])
        os.utime(self.home / canary.STEWARD_REPOS, (at + 60, at + 60))
        self.refresh(host, "m5", now=NOW + 8 * MIN)
        self.assertNotIn("tool_steward_verified", self.kinds())
        # Control: the same list, unchanged since before the tick, completes it.
        os.utime(self.home / canary.STEWARD_REPOS, (at - 60, at - 60))
        self.refresh(host, "m5", now=NOW + 9 * MIN)
        self.assertEqual(self.kinds()[-1], "tool_steward_verified")

    def test_ticks_are_judged_from_the_swap_not_the_start_of_the_pass(self) -> None:
        # The apply took 3 minutes; a tick that started 1 minute in still ran
        # the old binary, and its errors are not the new release's.
        self.tick(NOW - 10 * MIN, "0.221.0", "ok")
        host = self.behind()
        self.refresh(host, "m5", clock=lambda: NOW + 3 * MIN)
        self.assertEqual(json.loads((self.state / "steward.json").read_text())["shipyard"]
                         ["deployed_at"], iso(NOW + 3 * MIN))
        self.log(*(f"{stamp(NOW + 1 * MIN)} {repo} ERROR rc=124 " for repo in REPOS))
        self.refresh(host, "m5", now=NOW + 10 * MIN)
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])

    def test_one_erroring_repository_or_an_error_token_is_inconclusive(self) -> None:
        host = self.deploy_on_steward_host()
        at = NOW + 5 * MIN
        self.log(f"{stamp(at)} fleet invariants=ok",
                 f"{stamp(at)} danielraffel/tartci ERROR rc=124 ",
                 f"{stamp(at)} Generous-Corp/pulp rc=0 took=9s ver=0.221.1 invariants=ok",
                 f"{stamp(at)} danielraffel/Shipyard rc=0 took=9s ver=0.221.1 invariants=ok")
        self.tick(NOW + 10 * MIN, "0.221.1", "ERROR")  # jq could not read its pass
        self.log(f"{stamp(NOW + 15 * MIN)} fleet invariants=ok")
        self.refresh(host, "m5", now=NOW + 16 * MIN)
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])
        self.assertNotIn("tool_steward_verified", self.kinds())
        self.tick(NOW + 20 * MIN, "0.221.1", "ok")
        self.refresh(host, "m5", now=NOW + 26 * MIN)
        self.assertEqual(self.kinds()[-1], "tool_steward_verified")
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])

    # ── who is rolled back, to what, and whether it landed ──

    def test_a_hand_deploy_that_fails_the_steward_is_recorded_not_rolled_back(self) -> None:
        self.tick(NOW - 10 * MIN, "0.221.0", "ok")
        host = FleetHost({"shipyard": "0.221.0", "pulp": "0.877.2"})
        off = {name: dict(tool, auto_apply=False) for name, tool in settings()["tools"].items()}
        self.refresh(host, "m5", tools=off)
        host.versions["shipyard"] = "0.221.1"  # an operator installs it by hand
        self.refresh(host, "m5", now=NOW + 1 * MIN, tools=off)
        self.assertEqual(self.events()[-1]["by"], "observed")
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:arming")
        row = self.refresh(host, "m5", now=NOW + 10 * MIN, tools=off)["tools"]["shipyard"]
        self.assertEqual(host.fleet_updates(), [])
        self.assertEqual(self.kinds()[-1], "tool_steward_failed")
        self.assertIn("deployed by hand; not rolled back", row["steward_problem"])

    def test_the_rollback_target_is_the_release_that_was_running(self) -> None:
        # The ghapp generation lagged the CLI, so 0.220.0 was what really ran.
        self.feeds["danielraffel/Shipyard"] = feed(
            ("v0.220.0", NOW - 40 * HOUR), ("v0.221.0", NOW - 30 * HOUR),
            ("v0.221.1", NOW - 4 * HOUR))
        make_generation(self.home)
        self.tick(NOW - 10 * MIN, "0.221.0", "ok")
        host = FleetHost({"shipyard": "0.221.0", "pulp": "0.877.2"}, generation="0.220.0")
        self.refresh(host, "m5")
        self.assertEqual(host.fleet_updates(), ["v0.221.1"])
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:arming")
        self.refresh(host, "m5", now=NOW + 10 * MIN)
        self.assertEqual(host.fleet_updates(), ["v0.221.1", "v0.220.0"])

    def test_a_rollback_that_exits_0_without_moving_back_has_not_landed(self) -> None:
        host = self.deploy_on_steward_host()
        host.stuck_rollback = True
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:arming")
        row = self.refresh(host, "m5", now=NOW + 10 * MIN)["tools"]["shipyard"]
        self.assertIn("FAILED (try 1)", row["steward_problem"])
        self.assertFalse([e for e in self.events() if e["event"] == "tool_rolled_back"][0]["landed"])

    def test_a_failed_rollback_is_retried_then_left_as_a_problem(self) -> None:
        host = self.deploy_on_steward_host()
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:clones")
        host.fleet_update_results = [(1, "rollout lock busy")] * canary.ROLLBACK_MAX_TRIES
        for n in range(canary.ROLLBACK_MAX_TRIES + 1):
            row = self.refresh(host, "m5", now=NOW + (10 + n) * MIN)["tools"]["shipyard"]
        self.assertEqual(len(host.fleet_updates()), 1 + canary.ROLLBACK_MAX_TRIES)
        self.assertIn("roll back by hand", row["steward_problem"])
        self.assertIn("roll back by hand", tf.summary(self.home)["problem"])
        self.assertEqual(self.kinds().count("tool_apply_failed"), 1)

    def test_a_rollback_is_saved_before_the_next_tool_runs(self) -> None:
        host = self.deploy_on_steward_host()
        self.tick(NOW + 5 * MIN, "0.221.1", "FAIL:arming")
        self.feeds["Generous-Corp/pulp"] = feed(("v0.877.2", NOW - 30 * HOUR),
                                               ("v0.877.3", NOW - 4 * HOUR))
        host.apply_installs["pulp"] = "0.877.3"
        host.fail_pulp_install = True  # the pass dies during the pulp apply
        with self.assertRaises(RuntimeError):
            self.refresh(host, "m5", now=NOW + 10 * MIN)
        attempts = json.loads((self.state / "attempts.json").read_text())
        self.assertTrue(attempts["shipyard"]["rejected"])
        self.assertEqual(json.loads((self.state / "steward.json").read_text()), {})

    def test_a_host_without_a_running_steward_records_no_check(self) -> None:
        self.tick(NOW - 3 * HOUR, "0.221.0")  # an old log: the steward no longer runs here
        host = self.behind()
        self.refresh(host, "m5")
        self.assertNotIn("steward_host", self.events()[-1])
        self.assertEqual(json.loads((self.state / "steward.json").read_text()), {})


@testing_support.requires_tomllib
class PublishedRolloutTests(unittest.TestCase):
    """fleet/tool-rollout.json is validated and published in the supply."""

    def test_roles_must_name_published_hosts(self) -> None:
        import macos_fleet_lanes as fleet  # noqa: PLC0415 - needs tomllib
        hosts = {"m1", "m5", "studio"}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tool-rollout.json"
            self.assertIsNone(fleet.tool_rollout(path, hosts))
            for bad in ({"canary": "m9", "steward_hosts": ["studio"]},
                        {"canary": "m5", "steward_hosts": ["m9"]},
                        {"canary": "m5", "extra": 1}, ["m5"],
                        {"canary": "m5", "steward_hosts": []}, {"canary": "m5"}):
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    fleet.tool_rollout(path, hosts)
            path.write_text(json.dumps({"canary": "m5", "steward_hosts": ["studio"]}))
            self.assertEqual(fleet.tool_rollout(path, hosts),
                             {"canary": "m5", "steward_hosts": ["studio"]})
        published = fleet.published_snapshot()
        self.assertEqual(published["tool_rollout"], {"canary": "m5", "steward_hosts": ["studio"]})


if __name__ == "__main__":
    unittest.main()
