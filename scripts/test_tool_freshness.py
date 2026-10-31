#!/usr/bin/env python3
"""Hermetic tests for tool freshness, its status surfaces, and the heal pass.

Every tool run and feed read is a fake, so nothing here touches the real
Shipyard or pulp installs or the network. Each alarm is exercised with the
fault present and absent, so a check that can only pass would fail here.
"""

from __future__ import annotations

import testing_support  # noqa: E402
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import state_age  # noqa: E402
import tool_freshness as tf  # noqa: E402

HOUR = 3600.0
NOW = 1790000000.0  # 2026-09-21T13:33:20Z


def iso(ts: float) -> str:
    return tf._iso(ts)


def feed(*releases: tuple[str, float]) -> str:
    """An Atom feed like GitHub's, newest first, with a second tag family."""
    entries = "".join(
        f"""<entry><updated>{iso(at)}</updated>
        <link rel="alternate" type="text/html" href="https://github.com/o/r/releases/tag/{tag}"/>
        <title>{tag}</title></entry>"""
        for tag, at in sorted(releases, key=lambda r: -r[1]))
    plugin = f"""<entry><updated>{iso(NOW)}</updated>
        <link rel="alternate" href="https://github.com/o/r/releases/tag/plugin-v9.9.9"/></entry>"""
    return f'<feed xmlns="http://www.w3.org/2005/Atom">{plugin}{entries}</feed>'


class Host:
    """Fake tool installs: CLI versions, the ghapp generation, fleet-update, a call log."""

    def __init__(self, versions: dict[str, str | None], generation: str | None = None):
        self.versions = versions
        self.generation = generation
        self.calls: list[list[str]] = []
        self.apply_installs: dict[str, str | None] = {}
        self.apply_generation: str | None = None  # default: the generation moves with the CLI
        self.verdict = "verified"
        self.pulp_results: list[tuple[int, str]] = []  # scripted pulp-install outcomes, in order
        self.fleet_update_results: list[tuple[int, str]] = []  # scripted fleet-update outcomes

    def run(self, argv: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append(argv)
        if "auth-generations" in argv[0]:
            return (0, f"shipyard {self.generation}") if self.generation else (1, "exec failed")
        if "pulp-install" in argv:
            tag = argv[argv.index("pulp-install") + 1]
            if self.pulp_results:
                rc, out = self.pulp_results.pop(0)
                if rc != 0:
                    return rc, out
            installed = self.apply_installs.get("pulp")
            if installed is None:
                return 1, f"curl: (22) {tag} installer unavailable"
            self.versions["pulp"] = installed
            return 0, "Extracting to ~/.pulp/bin..."
        name = Path(argv[0]).name
        if name == "tart":
            version = self.versions.get("tart")
            return (0, version) if version else (127, "not installed")
        if "fleet-update" in argv:
            if self.fleet_update_results:
                rc, out = self.fleet_update_results.pop(0)
                if rc != 0:
                    return rc, out
            installed = self.apply_installs.get(name)
            if installed is None:
                return 1, json.dumps({"event": "fleet_summary", "verdict": "failed"}, indent=2)
            self.versions[name] = installed
            if self.generation is not None:
                self.generation = self.apply_generation or installed
            receipt = {"event": "host_verification", "host_class": argv[argv.index("--host-class") + 1],
                       "verdict": self.verdict}
            summary = {"event": "fleet_summary", "target": argv[argv.index("--to") + 1],
                       "verdict": self.verdict}
            # Shipyard streams one pretty-printed document per event.
            return 0, json.dumps(receipt, indent=2) + "\n" + json.dumps(summary, indent=2)
        version = self.versions.get(name)
        if version is None:
            return 127, "not installed"
        return 0, f"{name} {version}" if name == "shipyard" else f"pulp v{version}"


def make_generation(home: Path) -> Path:
    """The layout Shipyard installs: a symlink to ghapp inside a generation dir."""
    gen = home / ".local/share/shipyard/auth-generations/75f602aa0abf"
    gen.mkdir(parents=True)
    (gen / "ghapp").write_text("#!/bin/sh\n")
    link = home / ".local/bin/ghapp.shipyard-generation"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(gen / "ghapp")
    return gen


def settings(**overrides) -> dict:
    value = {"stale_hours": 12.0, "apply_after_minutes": 30.0, "apply_retry_hours": 6.0,
             "host_class": "studio",
             "tools": {name: dict(tool, enabled=True) for name, tool in tf.DEFAULT_TOOLS.items()}}
    value.update(overrides)
    return value


class ToolFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.home / ".tartci")})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Tests do not depend on the repository's real pin, which moves.
        self.pin("2.40.1")
        self.state = tf.state_dir_for(self.home)
        # Status reads ages against this clock: just after the fixed NOW.
        clock = mock.patch.object(state_age, "clock", lambda: NOW + 60)
        clock.start()
        self.addCleanup(clock.stop)
        self.feeds = {
            "danielraffel/Shipyard": feed(("v0.221.0", NOW - 30 * HOUR), ("v0.221.1", NOW - 20 * HOUR)),
            "Generous-Corp/pulp": feed(("v0.877.1", NOW - 3 * HOUR), ("v0.877.2", NOW - 1 * HOUR)),
        }

    def feed(self, repo: str) -> str:
        return self.feeds[repo]

    READY = {"ready": True, "missing": [], "sums": None, "detail": "ready"}

    def refresh(self, host: Host, now: float = NOW, **kw) -> dict:
        release = kw.pop("release", self.READY)
        return tf.refresh(self.home, now, settings=kw.pop("settings", settings()),
                          run=host.run, feed=self.feed,
                          probe=lambda name, tag: {"platform": "darwin-arm64",
                                                   "result": release() if callable(release) else release},
                          **kw)

    def events(self) -> list[dict]:
        path = self.state / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_current_tools_raise_no_problem(self) -> None:
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2", "tart": "2.40.1"})
        value = self.refresh(host)
        self.assertEqual({row["state"] for row in value["tools"].values()}, {"current"})
        summary = tf.summary(self.home)
        self.assertIsNone(summary["problem"])
        self.assertIn("shipyard: 0.221.1 current with latest v0.221.1", "\n".join(summary["lines"]))

    def test_behind_is_timed_from_the_first_newer_release_and_goes_stale(self) -> None:
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.1"})
        tools = self.refresh(host, settings=settings(tools={
            name: dict(tool, enabled=True, auto_apply=False)
            for name, tool in tf.DEFAULT_TOOLS.items()}))["tools"]
        # Shipyard: v0.221.1 was published 20 h ago, so 20 h behind: STALE at 12 h.
        self.assertEqual(tools["shipyard"]["behind_hours"], 20.0)
        self.assertTrue(tools["shipyard"]["stale"])
        # pulp: v0.877.2 is 1 h old: behind, not stale. The plugin-v9.9.9 tag is
        # another release family and must not count as newer.
        self.assertEqual(tools["pulp"]["latest_tag"], "v0.877.2")
        self.assertEqual(tools["pulp"]["releases_behind"], 1)
        self.assertFalse(tools["pulp"]["stale"])
        summary = tf.summary(self.home)
        self.assertEqual(summary["problem"], "shipyard 20 h behind v0.221.1")
        self.assertIn("STALE", "\n".join(summary["lines"]))

    def test_atom_feed_draft_is_excluded_from_published_release_channel(self) -> None:
        releases = [(tf._version("0.364.0"), "v0.364.0", NOW - HOUR),
                    (tf._version("0.372.0"), "v0.372.0", NOW)]
        tool = dict(tf.DEFAULT_TOOLS["shipyard"])
        calls: list[tuple[str, str, list[str]]] = []

        def readiness(repo: str, tag: str, assets: list[str]) -> dict:
            calls.append((repo, tag, assets))
            return {"ready": tag == "v0.364.0"}

        published = tf.published_releases(tool, releases, readiness)
        self.assertEqual(published, [releases[0]])
        self.assertEqual([call[1] for call in calls], ["v0.364.0", "v0.372.0"])
        self.assertEqual(calls[0][2], ["shipyard-macos-arm64.dmg", "checksums.sha256"])

    def test_atom_feed_prerelease_is_excluded_from_published_release_channel(self) -> None:
        releases = tf.parse_releases(feed(("v0.372.0-rc.1", NOW),
                                           ("v0.371.0", NOW - HOUR)))
        self.assertEqual(releases, [(tf._version("0.371.0"), "v0.371.0", NOW - HOUR)])

    def test_an_install_older_than_the_feed_is_a_lower_bound_that_never_moves_later(self) -> None:
        no_apply = settings(tools={name: dict(tool, enabled=True, auto_apply=False)
                                   for name, tool in tf.DEFAULT_TOOLS.items()})
        host = Host({"shipyard": "0.221.1", "pulp": "0.305.0"})
        first = self.refresh(host, settings=no_apply)["tools"]["pulp"]
        self.assertTrue(first["behind_since_lower_bound"])
        self.assertEqual(first["behind_since"], iso(NOW - 3 * HOUR))
        # The feed scrolls: its oldest entry is now newer. The recorded bound stays.
        self.feeds["Generous-Corp/pulp"] = feed(("v0.877.2", NOW - HOUR), ("v0.878.0", NOW + HOUR))
        later = self.refresh(host, NOW + 2 * HOUR, settings=no_apply)["tools"]["pulp"]
        self.assertEqual(later["behind_since"], iso(NOW - 3 * HOUR))
        self.assertEqual(later["behind_hours"], 5.0)
        self.assertIn(">=5 h", tf.render_row(later))

    def test_missing_binary_is_reported_but_not_a_problem(self) -> None:
        self.refresh(Host({"shipyard": "0.221.1", "pulp": None}))
        summary = tf.summary(self.home)
        self.assertIsNone(summary["problem"])
        self.assertIn("pulp: not installed", "\n".join(summary["lines"]))

    def test_unreadable_feed_or_version_is_unknown_and_a_problem(self) -> None:
        def broken(repo: str) -> str:
            raise OSError("network down")
        tf.refresh(self.home, NOW, settings=settings(), feed=broken,
                   run=Host({"shipyard": "0.221.1", "pulp": "0.877.2"}).run)
        summary = tf.summary(self.home)
        self.assertEqual(summary["problem"], "pulp freshness unknown; shipyard freshness unknown")
        self.assertIn("UNKNOWN (releases unreadable: OSError: network down)",
                      "\n".join(summary["lines"]))

    def test_auto_apply_waits_for_a_young_release(self) -> None:
        self.feeds["danielraffel/Shipyard"] = feed(("v0.221.0", NOW - 30 * HOUR),
                                                   ("v0.221.1", NOW - 600))
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.221.1"
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertEqual(row["state"], "behind")
        self.assertIn("waiting", row["apply"])
        self.assertFalse(any("fleet-update" in call for call in host.calls))

    def test_releases_faster_than_the_soak_still_apply_the_newest_soaked_one(self) -> None:
        # Shipyard v0.232.0 was held back because v0.233.0 was under 30 minutes
        # old; with a release every 20 minutes the host never updated at all.
        self.feeds["danielraffel/Shipyard"] = feed(
            ("v0.229.0", NOW - 30 * HOUR), ("v0.230.0", NOW - 60 * 60),
            ("v0.231.0", NOW - 40 * 60), ("v0.232.0", NOW - 20 * 60), ("v0.233.0", NOW))
        host = Host({"shipyard": "0.229.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.231.0"
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertIn("v0.231.0", [c[c.index("--to") + 1] for c in host.calls if "--to" in c])
        self.assertEqual(row["apply"], "applied v0.231.0: ok")
        self.assertEqual(row["installed"], "0.231.0")
        # Twenty minutes and one more release later, the next soaked one lands.
        self.feeds["danielraffel/Shipyard"] = feed(
            ("v0.229.0", NOW - 30 * HOUR), ("v0.231.0", NOW - 40 * 60),
            ("v0.232.0", NOW - 20 * 60), ("v0.233.0", NOW), ("v0.234.0", NOW + 20 * 60))
        host.apply_installs["shipyard"] = "0.232.0"
        row = self.refresh(host, NOW + 20 * 60 + 1)["tools"]["shipyard"]
        self.assertEqual(row["apply"], "applied v0.232.0: ok")

    def test_a_shipyard_rollout_lock_is_not_a_spent_attempt(self) -> None:
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.221.1"
        host.fleet_update_results = [(tf.NOT_READY_EXIT, "another fleet rollout holds the "
                                      "controller lock; not starting a second one")]
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertIn("not ready yet", row["apply"])
        self.assertNotIn("shipyard", json.loads((self.state / "attempts.json").read_text()))
        row = self.refresh(host, NOW + 31 * 60)["tools"]["shipyard"]
        self.assertEqual(row["apply"], "applied v0.221.1: ok")

    def test_a_legacy_exit_75_shipyard_record_does_not_hold_the_host_back(self) -> None:
        # m3's record for v0.231.0, written before exit 75 was "not ready".
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.221.1"
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "attempts.json").write_text(json.dumps({"shipyard": {
            "target": "v0.221.1", "at": iso(NOW - 600),
            "result": "FAILED (verdict no fleet_summary, exit 75, installed 0.221.0, generation "
                      "0.221.0): another fleet rollout holds the controller lock; not starting "
                      "a second one"}}))
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertEqual(row["apply"], "applied v0.221.1: ok")

    def test_a_legacy_failure_that_is_not_exit_75_still_holds(self) -> None:
        # Control: the same record with a real failure keeps the 6 h guard.
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.221.1"
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "attempts.json").write_text(json.dumps({"shipyard": {
            "target": "v0.221.1", "at": iso(NOW - 600),
            "result": "FAILED (verdict failed, exit 1, installed 0.221.0, generation 0.221.0): "
                      "probe failed"}}))
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertIn("already attempted v0.221.1", row["apply"])

    def test_a_shipyard_tag_whose_release_is_still_a_draft_is_not_ready_not_attempted(self) -> None:
        # The Release job queues behind the single Shipyard macOS runner, so a
        # tag exists while its release is a draft and fleet-update --to it
        # 404s. That 404 used to be a spent attempt: the tag was skipped 6 h.
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.221.1"
        draft = {"ready": False, "missing": ["shipyard-macos-arm64.dmg", "checksums.sha256"],
                 "sums": None, "detail": "asset shipyard-macos-arm64.dmg missing"}
        row = self.refresh(host, release=draft)["tools"]["shipyard"]
        self.assertIn("not ready yet", row["apply"])
        self.assertFalse(any("fleet-update" in call for call in host.calls))
        self.assertNotIn("shipyard", json.loads((self.state / "attempts.json").read_text()))
        # Published a few minutes later: the next pass applies it.
        row = self.refresh(host, NOW + 5 * 60)["tools"]["shipyard"]
        self.assertEqual(row["apply"], "applied v0.221.1: ok")

    def test_shipyard_readiness_checks_the_macos_release_assets(self) -> None:
        self.assertEqual(tf.DEFAULT_TOOLS["shipyard"]["release_assets"],
                         ["shipyard-macos-arm64.dmg", "checksums.sha256"])

    def test_auto_apply_installs_verifies_and_records_one_deploy_event(self) -> None:
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.221.1"
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertIn([str(self.home / ".local/bin/shipyard"), "runner", "fleet-update", "--to",
                       "v0.221.1", "--host-class", "studio", "--apply", "--json"], host.calls)
        self.assertEqual(row["state"], "current")
        self.assertEqual(row["apply"], "applied v0.221.1: ok")
        self.assertEqual(self.events(), [{
            "event": "tool_deployed", "tool": "shipyard", "component": "cli", "at": iso(NOW),
            "from": "0.221.0", "to": "0.221.1", "latest": "0.221.1", "by": "auto_apply",
            "downgrade": False, "verify": "current"}])
        # pulp is already current, so it is not installed.
        self.assertFalse(any("pulp-install" in c for c in host.calls))

    def test_a_behind_pulp_cli_is_installed_from_its_pinned_release_installer(self) -> None:
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.1"})
        host.apply_installs["pulp"] = "0.877.2"
        self.feeds["Generous-Corp/pulp"] = feed(("v0.877.1", NOW - 30 * HOUR),
                                               ("v0.877.2", NOW - 28 * HOUR))
        row = self.refresh(host)["tools"]["pulp"]
        call = next(c for c in host.calls if "pulp-install" in c)
        self.assertEqual(call[0:2], ["/bin/bash", "-c"])
        self.assertEqual(call[2], tf.PULP_INSTALL_SCRIPT)
        self.assertEqual(call[3:], ["pulp-install", "v0.877.2", str(self.home), "darwin-arm64", ""])
        self.assertEqual((row["state"], row["apply"]), ("current", "applied v0.877.2: ok"))
        self.assertEqual([(e["tool"], e["component"], e["from"], e["to"], e["by"], e["verify"])
                          for e in self.events()],
                         [("pulp", "cli", "0.877.1", "0.877.2", "auto_apply", "current")])

    def test_a_pulp_install_that_does_not_land_is_a_failure(self) -> None:
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.1"})
        self.feeds["Generous-Corp/pulp"] = feed(("v0.877.1", NOW - 30 * HOUR),
                                               ("v0.877.2", NOW - 28 * HOUR))
        row = self.refresh(host)["tools"]["pulp"]
        self.assertIn("FAILED", row["apply"])
        self.assertEqual([(e["event"], e["tool"]) for e in self.events()],
                         [("tool_apply_failed", "pulp")])

    def test_a_lagging_ghapp_generation_makes_a_current_cli_stale(self) -> None:
        make_generation(self.home)
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2"}, generation="0.221.0")
        off = settings(tools={name: dict(tool, enabled=True, auto_apply=False)
                              for name, tool in tf.DEFAULT_TOOLS.items()})
        row = self.refresh(host, settings=off)["tools"]["shipyard"]
        self.assertEqual((row["installed"], row["generation"], row["effective"]),
                         ("0.221.1", "0.221.0", "0.221.0"))
        self.assertEqual(row["state"], "behind")
        self.assertTrue(row["stale"])
        line = tf.render_row(row)
        self.assertIn("0.221.1 (ghapp generation 0.221.0) behind latest v0.221.1", line)
        self.assertIn("STALE", line)
        self.assertEqual(tf.summary(self.home)["problem"], "shipyard 20 h behind v0.221.1")
        host.generation = "0.221.1"
        row = self.refresh(host, NOW + HOUR, settings=off)["tools"]["shipyard"]
        self.assertEqual(row["state"], "current")
        self.assertEqual([(e["component"], e["from"], e["to"], e["by"]) for e in self.events()],
                         [("auth_generation", "0.221.0", "0.221.1", "observed")])

    def test_an_unreadable_generation_is_unknown_not_current(self) -> None:
        make_generation(self.home)
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2"}, generation=None)
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertEqual(row["state"], "unknown")
        self.assertIn("generation 75f602aa0abf version unreadable", row["reason"])

    def test_fleet_update_moves_cli_and_generation_and_logs_both(self) -> None:
        make_generation(self.home)
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2"}, generation="0.221.0")
        host.apply_installs["shipyard"] = "0.221.1"
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertEqual(row["apply"], "applied v0.221.1: ok")
        self.assertEqual((row["state"], row["generation"]), ("current", "0.221.1"))
        self.assertEqual([(e["component"], e["from"], e["to"], e["by"]) for e in self.events()],
                         [("auth_generation", "0.221.0", "0.221.1", "auto_apply")])

    def test_a_fleet_update_verdict_other_than_verified_is_a_failure(self) -> None:
        make_generation(self.home)
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"}, generation="0.221.0")
        host.apply_installs["shipyard"] = "0.221.1"
        host.verdict = "rollback_failed"
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertIn("FAILED (verdict rollback_failed", row["apply"])
        failed = [e for e in self.events() if e["event"] == "tool_apply_failed"]
        self.assertEqual([e["verdict"] for e in failed], ["rollback_failed"])

    def test_a_verified_verdict_with_the_generation_left_behind_is_a_failure(self) -> None:
        make_generation(self.home)
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"}, generation="0.221.0")
        host.apply_installs["shipyard"] = "0.221.1"
        host.apply_generation = "0.221.0"
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertIn("FAILED (verdict verified", row["apply"])
        self.assertIn("generation 0.221.0", row["apply"])

    def test_no_host_class_refuses_to_apply(self) -> None:
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.221.1"
        no_class = settings()
        del no_class["host_class"]
        row = self.refresh(host, settings=no_class)["tools"]["shipyard"]
        self.assertIn("refused: no host class", row["apply"])
        self.assertFalse(any("fleet-update" in call for call in host.calls))

    def test_host_class_defaults_to_the_fleet_profile_host_id(self) -> None:
        if tf.tomllib is None:
            self.skipTest("needs tomllib")
        profile = self.home / ".config/tartci/macos-fleet-profile.toml"
        profile.parent.mkdir(parents=True)
        profile.write_text('[host]\nid = "m5"\nssh = "m5"\n')
        self.assertEqual(tf.host_class(self.home, {}), "m5")
        self.assertEqual(tf.host_class(self.home, {"host_class": "m1"}), "m1")

    NOT_READY = {"ready": False, "missing": ["pulp-darwin-arm64.tar.gz"], "sums": None,
                 "detail": "asset pulp-darwin-arm64.tar.gz missing"}

    def pulp_behind(self) -> Host:
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.1"})
        host.apply_installs["pulp"] = "0.877.2"
        self.feeds["Generous-Corp/pulp"] = feed(("v0.877.1", NOW - 30 * HOUR),
                                               ("v0.877.2", NOW - 2 * HOUR))
        return host

    def installs(self, host: Host) -> int:
        return len([c for c in host.calls if "pulp-install" in c])

    def attempts(self) -> dict:
        return json.loads((self.state / "attempts.json").read_text())

    def test_a_release_whose_asset_404s_then_appears_installs_on_a_later_pass(self) -> None:
        # v0.884.0 on 2026-09-30: the tag was in the feed an hour before the
        # release uploaded its archive; the install got a 404 and the
        # attempt guard then held m1, m3 and m5 back.
        host = self.pulp_behind()
        row = self.refresh(host, release=self.NOT_READY)["tools"]["pulp"]
        self.assertEqual(row["apply"], "pulp v0.877.2 not ready yet "
                                       "(asset pulp-darwin-arm64.tar.gz missing), waiting")
        self.assertEqual(self.installs(host), 0)                    # nothing attempted
        self.assertNotIn("pulp", self.attempts())                   # nothing spent
        self.assertIsNone(tf.summary(self.home)["problem"])          # nothing alerted
        line = next(l for l in tf.summary(self.home)["lines"] if l.startswith("pulp:"))
        self.assertIn("[pulp v0.877.2 not ready yet (asset pulp-darwin-arm64.tar.gz missing), "
                      "waiting]", line)
        self.assertNotIn("FAILED", line)
        row = self.refresh(host, NOW + 30 * 60)["tools"]["pulp"]    # the asset is up now
        self.assertEqual((row["state"], row["apply"]), ("current", "applied v0.877.2: ok"))
        self.assertEqual(self.installs(host), 1)
        self.assertEqual([e["event"] for e in self.events()], ["tool_deployed"])

    def test_a_download_error_during_install_is_not_ready_not_a_spent_attempt(self) -> None:
        host = self.pulp_behind()
        host.pulp_results = [(tf.NOT_READY_EXIT, "not ready: .../pulp-darwin-arm64.tar.gz "
                              "could not be downloaded")]
        row = self.refresh(host)["tools"]["pulp"]
        self.assertIn("not ready yet", row["apply"])
        self.assertNotIn("pulp", self.attempts())
        row = self.refresh(host, NOW + 30 * 60)["tools"]["pulp"]
        self.assertEqual(row["state"], "current")

    def test_a_release_that_stays_incomplete_alerts_only_after_the_window(self) -> None:
        host = self.pulp_behind()
        for hours in (0, 1, 5.5):
            self.refresh(host, NOW + hours * HOUR, release=self.NOT_READY)
            self.assertIsNone(tf.summary(self.home)["problem"], hours)
            self.assertEqual(self.events(), [])
        self.refresh(host, NOW + 6.5 * HOUR, release=self.NOT_READY)
        self.assertIn("pulp v0.877.2 still not ready after 6.5 h", tf.summary(self.home)["problem"])
        self.refresh(host, NOW + 7.5 * HOUR, release=self.NOT_READY)
        self.assertEqual([e["event"] for e in self.events()], ["tool_release_incomplete"])
        self.assertEqual(self.installs(host), 0)

    def test_a_failure_after_the_downloads_still_trips_the_guard(self) -> None:
        host = self.pulp_behind()
        host.pulp_results = [(4, "refused: .../pulp-darwin-arm64.tar.gz has sha256 aa, "
                                 "SHA256SUMS says bb")]
        self.assertIn("FAILED", self.refresh(host)["tools"]["pulp"]["apply"])
        row = self.refresh(host, NOW + 31 * 60)["tools"]["pulp"]
        self.assertIn("already attempted v0.877.2", row["apply"])
        self.assertEqual(self.installs(host), 1)

    def test_a_legacy_404_record_does_not_hold_the_host_back(self) -> None:
        # The record m1, m3 and m5 carry for v0.884.0, written before this.
        host = self.pulp_behind()
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "attempts.json").write_text(json.dumps({"pulp": {
            "target": "v0.877.2", "at": iso(NOW - 600),
            "result": "FAILED (exit 1, installed 0.877.1, generation None): ...\n"
                      "Error: could not download pulp-darwin-arm64 for this version.\n"
                      "curl: (56) The requested URL returned error: 404"}}))
        row = self.refresh(host)["tools"]["pulp"]
        self.assertEqual((row["state"], row["apply"]), ("current", "applied v0.877.2: ok"))

    def test_a_local_archive_is_used_only_when_its_checksum_matches(self) -> None:
        archive_dir = self.home / "pulp-archives" / "v0.877.2"
        archive_dir.mkdir(parents=True)
        local = archive_dir / "pulp-darwin-arm64.tar.gz"
        local.write_bytes(b"the release archive")
        good = hashlib.sha256(b"the release archive").hexdigest()
        tools = {name: dict(tool, enabled=True) for name, tool in tf.DEFAULT_TOOLS.items()}
        tools["pulp"]["local_archive_dir"] = "{home}/pulp-archives"
        for sums, expect_local in ((f"{'0' * 64}  pulp-darwin-arm64.tar.gz\n", False),
                                   (f"{good}  pulp-darwin-arm64.tar.gz\n", True)):
            with self.subTest(expect_local=expect_local):
                (self.state / "pending.json").unlink(missing_ok=True)
                host = self.pulp_behind()
                release = dict(self.NOT_READY, sums=sums)
                row = self.refresh(host, release=release,
                                   settings=settings(tools=tools))["tools"]["pulp"]
                calls = [c for c in host.calls if "pulp-install" in c]
                if expect_local:
                    self.assertEqual(calls[0][-1], str(local))
                    self.assertEqual(row["state"], "current")
                else:
                    self.assertEqual(calls, [])
                    self.assertIn("refused: sha256", row["local_archive"])
                    self.assertIn("not ready yet", row["apply"])

    def test_a_failed_apply_is_recorded_and_not_retried_inside_the_window(self) -> None:
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertIn("FAILED", row["apply"])
        self.assertEqual([e["event"] for e in self.events()], ["tool_apply_failed"])
        calls = len([c for c in host.calls if "fleet-update" in c])
        row = self.refresh(host, NOW + HOUR)["tools"]["shipyard"]
        self.assertEqual(len([c for c in host.calls if "fleet-update" in c]), calls)
        self.assertIn("already attempted v0.221.1", row["apply"])
        self.refresh(host, NOW + 7 * HOUR)
        self.assertEqual(len([c for c in host.calls if "fleet-update" in c]), calls + 1)

    def test_auto_apply_off_leaves_the_tool_alone(self) -> None:
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.221.1"
        off = settings(tools={name: dict(tool, enabled=True, auto_apply=False)
                              for name, tool in tf.DEFAULT_TOOLS.items()})
        self.refresh(host, settings=off)
        self.assertFalse(any("fleet-update" in call for call in host.calls))

    def test_a_deployment_made_elsewhere_is_observed_and_recorded(self) -> None:
        host = Host({"shipyard": "0.221.1", "pulp": "0.305.0"})
        off = settings(tools={name: dict(tool, enabled=True, auto_apply=False)
                              for name, tool in tf.DEFAULT_TOOLS.items()})
        self.refresh(host, settings=off)
        host.versions["pulp"] = "0.877.2"
        self.refresh(host, NOW + HOUR, settings=off)
        self.assertEqual([(e["tool"], e["from"], e["to"], e["by"], e["verify"])
                          for e in self.events()],
                         [("pulp", "0.305.0", "0.877.2", "observed", "current")])

    def pin(self, version: str) -> None:
        root = self.home / "generation"
        (root / "fleet").mkdir(parents=True, exist_ok=True)
        (root / "fleet" / "tart.toml").write_text(
            f'[tart]\nversion = "{version}"\nsha256 = "{"a" * 64}"\n'
            f'[softnet]\nversion = "0.24.0"\nsha256 = "{"b" * 64}"\n')
        patcher = mock.patch.object(tf, "SUPPORT_ROOT", root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tart_is_measured_against_its_pin_not_releases_or_the_tap(self) -> None:
        # openai/tart had released 2.41.1 and the tap offered 2.40.1; only the
        # reviewed pin says what the fleet runs.
        self.pin("2.40.1")
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2", "tart": "2.36.0"})
        row = self.refresh(host)["tools"]["tart"]
        self.assertEqual(row["state"], "behind")
        self.assertEqual(row["latest_tag"], "2.40.1")
        self.assertEqual(row["behind_since"], iso(NOW))
        self.assertFalse(any("openai/tart" in str(call) or Path(call[0]).name == "brew"
                             for call in host.calls))
        host.versions["tart"] = "2.40.1"
        self.assertEqual(self.refresh(host, NOW + HOUR)["tools"]["tart"]["state"], "current")

    def gate(self, **value) -> None:
        path = self.home / ".tartci" / "state" / "self-update" / "tart-gate.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_tart_behind_its_pin_is_information_unless_the_host_is_due(self) -> None:
        # Merged with every host opted out, Tart is behind the pin everywhere:
        # that must not turn doctor red fleet-wide 12 h later.
        self.pin("2.40.1")
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2", "tart": "2.36.0"})
        for gate in (None, {"upgrade": False, "due": False, "pin": "2.40.1"},
                     {"upgrade": True, "due": False, "pin": "2.40.1"},
                     {"upgrade": True, "due": True, "pin": "2.39.0"}):
            with self.subTest(gate=gate):
                if gate is not None:
                    self.gate(**gate)
                row = self.refresh(host, NOW + 13 * HOUR)["tools"]["tart"]
                self.assertEqual("behind", row["state"])
                self.assertFalse(row["stale"])
                self.assertIsNone(tf.summary(self.home)["problem"])
                self.assertIn("information only", "; ".join(tf.summary(self.home)["lines"]))

    def test_a_tart_behind_its_pin_is_timed_from_first_sight_and_goes_stale(self) -> None:
        self.pin("2.40.1")
        self.gate(upgrade=True, due=True, pin="2.40.1")
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2", "tart": "2.36.0"})
        self.refresh(host)
        later = self.refresh(host, NOW + 13 * HOUR)["tools"]["tart"]
        self.assertEqual(later["behind_since"], iso(NOW))
        self.assertTrue(later["stale"])
        self.assertIn("tart 13 h behind 2.40.1", tf.summary(self.home)["problem"])

    def test_tart_is_never_applied_by_tool_freshness(self) -> None:
        self.pin("2.40.1")
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2", "tart": "2.36.0"})
        self.refresh(host, NOW + 48 * HOUR)
        self.assertFalse(any(Path(call[0]).name == "brew" for call in host.calls))
        self.assertEqual(host.versions["tart"], "2.36.0")

    def test_the_repository_pin_parses_without_tomllib_and_bad_lines_are_refused(self) -> None:
        import tart_pin
        pin = tart_pin.load_pin(Path(__file__).resolve().parent.parent)
        self.assertRegex(pin.version, r"^\d+\.\d+\.\d+$")
        self.assertRegex(pin.sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(pin.softnet_sha256, r"^[0-9a-f]{64}$")
        for bad in ('[tart]\nversion = 2.40.1\n', 'version = "2.40.1"\n',
                    '[tart]\nversion = "2.40.1" trailing\n'):
            with self.subTest(bad=bad), self.assertRaises(tart_pin.PinError):
                tart_pin.parse_pin(bad)

    def test_a_missing_pin_makes_tart_unknown_never_current(self) -> None:
        patcher = mock.patch.object(tf, "SUPPORT_ROOT", self.home / "no-generation")
        patcher.start()
        self.addCleanup(patcher.stop)
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2", "tart": "2.36.0"})
        row = self.refresh(host)["tools"]["tart"]
        self.assertEqual(row["state"], "unknown")
        self.assertIn("releases unreadable", row["reason"])

    def test_if_older_skips_a_fresh_cache_and_summary_never_runs_a_tool(self) -> None:
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2"})
        self.refresh(host)
        before = len(host.calls)
        self.refresh(host, NOW + 60, if_older=1800)
        self.assertEqual(len(host.calls), before)
        self.refresh(host, NOW + 1900, if_older=1800)
        self.assertGreater(len(host.calls), before)
        with mock.patch.object(tf, "run_command", side_effect=AssertionError("ran a tool")), \
                mock.patch.object(tf, "fetch_feed", side_effect=AssertionError("fetched")):
            self.assertEqual(len(tf.summary(self.home)["lines"]), 3)

    def test_a_disabled_tool_is_neither_run_nor_reported(self) -> None:
        host = Host({"shipyard": "0.221.1", "pulp": "0.305.0"})
        tools = dict(tf.DEFAULT_TOOLS)
        value = self.refresh(host, settings=settings(tools={
            "shipyard": dict(tools["shipyard"], enabled=True),
            "pulp": dict(tools["pulp"], enabled=False)}))
        self.assertEqual(sorted(value["tools"]), ["shipyard"])
        self.assertFalse(any(Path(call[0]).name == "pulp" for call in host.calls))

    def test_settings_file_overrides_defaults(self) -> None:
        if tf.tomllib is None:
            self.skipTest("needs tomllib")
        path = self.home / "tool-freshness.toml"
        path.write_text("stale_hours = 2\n[tools.pulp]\nenabled = false\n"
                        "[tools.shipyard]\nauto_apply = false\n")
        value = tf.load_settings(self.home, path)
        self.assertEqual(value["stale_hours"], 2.0)
        self.assertFalse(value["tools"]["pulp"]["enabled"])
        self.assertFalse(value["tools"]["shipyard"]["auto_apply"])
        self.assertEqual(value["tools"]["shipyard"]["repo"], "danielraffel/Shipyard")


class StatusSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def vitals(self, fsev: object) -> Path:
        path = self.dir / "host_vitals.json"
        reading = {"level": "green", "sampled_at": int(time.time())}
        if fsev is not None:
            reading["fseventsd"] = fsev
        path.write_text(json.dumps(reading))
        return path

    @testing_support.requires_tomllib
    def test_fseventsd_over_its_limit_is_a_problem_and_under_is_not(self) -> None:
        import macos_fleet_lanes as lanes
        big = lanes.host_vitals_summary(self.vitals(
            {"rss_mb": 10854, "cpu_pct": 107.5, "warn": True, "warn_mb": 1024}))
        self.assertEqual(big["problem"], "fseventsd 10854 MB RSS > 1024 MB")
        self.assertIn("WARN (> 1024 MB; restart with sudo killall fseventsd)", big["lines"][0])
        small = lanes.host_vitals_summary(self.vitals(
            {"rss_mb": 20, "cpu_pct": 1.0, "warn": False, "warn_mb": 1024}))
        self.assertIsNone(small["problem"])
        self.assertTrue(small["lines"][0].startswith("fseventsd: 20 MB RSS, 1.0% CPU"))

    @testing_support.requires_tomllib
    def test_fseventsd_absent_or_unpublished_reads_unknown_never_ok(self) -> None:
        import macos_fleet_lanes as lanes
        self.assertIn("UNKNOWN (host-vitals reading has no fseventsd field",
                      lanes.host_vitals_summary(self.vitals(None))["lines"][0])
        self.assertIn("UNKNOWN (no host-vitals reading",
                      lanes.host_vitals_summary(self.dir / "absent.json")["lines"][0])

    @testing_support.requires_tomllib
    def test_status_lines_and_watchdog_warning_carry_both(self) -> None:
        import macos_fleet_lanes as lanes
        import tartci_launchd_watchdog as wd
        value = {"profile_drift": {"state": "in_sync"}, "supply": {"state": "match"},
                 "self_update": {"lines": ["tartci: current with main"], "problem": None},
                 "tool_freshness": {"lines": ["shipyard: 0.219.0 behind ... STALE"],
                                    "problem": "shipyard 20 h behind v0.221.1"},
                 "host_vitals": {"lines": ["fseventsd: 10854 MB RSS WARN"],
                                 "problem": "fseventsd 10854 MB RSS > 1024 MB"}}
        rendered = lanes.render_config_verdicts(value)
        self.assertIn("shipyard: 0.219.0 behind ... STALE", rendered)
        self.assertIn("fseventsd: 10854 MB RSS WARN", rendered)
        self.assertEqual(wd.config_problem(value),
                         "tool_freshness=shipyard 20 h behind v0.221.1; "
                         "host_vitals=fseventsd 10854 MB RSS > 1024 MB")
        clean = dict(value, tool_freshness={"lines": [], "problem": None},
                     host_vitals={"lines": [], "problem": None})
        self.assertIsNone(wd.config_problem(clean))

    def test_doctor_reports_stale_current_and_unmeasured(self) -> None:
        import fleet_doctor as fd
        self.assertEqual(fd.check_tool_freshness(None).code, "tool_freshness_unmeasured")
        self.assertEqual(fd.check_tool_freshness({"state": None}).code, "tool_freshness_unmeasured")
        stale = fd.check_tool_freshness({"state": {}, "lines": ["x"], "problem": "pulp 2800 h"})
        self.assertEqual((stale.code, stale.state), ("tool_freshness_stale", fd.PROBLEM))
        ok = fd.check_tool_freshness({"state": {}, "lines": ["x"], "problem": None})
        self.assertEqual((ok.code, ok.state), ("tool_freshness_current", fd.OK))
        for code in ("tool_freshness_current", "tool_freshness_stale", "tool_freshness_unmeasured"):
            self.assertIn(code, fd.CODES)


class PulpInstallScriptTests(unittest.TestCase):
    """PULP_INSTALL_SCRIPT itself, against a stub curl and a stub installer."""

    BASE = "https://github.com/Generous-Corp/pulp/releases/download/v0.880.0"
    INSTALLER_URL = "https://raw.githubusercontent.com/Generous-Corp/pulp/v0.880.0/tools/install/install.sh"
    ARCHIVE = "archive\n"
    INSTALLER = ('echo "INSTALLER-RAN PULP_VERSION=$PULP_VERSION DIR=$PULP_INSTALL_DIR '
                 'NOPATH=$PULP_NO_MODIFY_PATH NOSDK=$PULP_SKIP_SDK_INSTALL '
                 'ARCHIVE=$(cat "$PULP_INSTALL_ARCHIVE")" >> "$RECORD"\n')

    def run_script(self, installer_body: str | None = None, missing: str = "",
                   sums: str | None = None, local: str | None = None
                   ) -> tuple[subprocess.CompletedProcess, Path]:
        """`missing`: a URL substring the stub curl answers with a 404 (exit 22)."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (tmp / "install.sh").write_text(self.INSTALLER if installer_body is None else installer_body)
        digest = hashlib.sha256(self.ARCHIVE.encode()).hexdigest()
        (tmp / "SHA256SUMS").write_text(sums if sums is not None else
                                        f"{digest}  pulp-darwin-arm64.tar.gz\n{'1' * 64}  x.tgz\n")
        (tmp / "archive").write_text(self.ARCHIVE)
        record = tmp / "record"
        (bin_dir / "curl").write_text(textwrap.dedent(f"""\
            #!/bin/bash
            out=""; url=""
            while [ $# -gt 0 ]; do
              case "$1" in -o) out="$2"; shift 2 ;; http*) url="$1"; shift ;; *) shift ;; esac
            done
            echo "url=$url" >> {record}
            if [ -n "{missing}" ] && [[ "$url" == *"{missing}"* ]]; then
              echo "curl: (22) The requested URL returned error: 404" >&2; exit 22
            fi
            case "$url" in
              *install.sh) cp {tmp}/install.sh "$out" ;;
              *SHA256SUMS) cp {tmp}/SHA256SUMS "$out" ;;
              *) cp {tmp}/archive "$out" ;;
            esac
            """))
        (bin_dir / "curl").chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_dir}:/usr/bin:/bin", RECORD=str(record))
        argv = ["/bin/bash", "-c", tf.PULP_INSTALL_SCRIPT, "pulp-install",
                "v0.880.0", str(tmp / "home"), "darwin-arm64", local or ""]
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30)
        record.touch()
        return proc, record

    def test_verifies_the_downloaded_archive_then_installs_it_pinned(self) -> None:
        proc, record = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = record.read_text().splitlines()
        self.assertEqual(lines[:3], [f"url={self.INSTALLER_URL}", f"url={self.BASE}/SHA256SUMS",
                                     f"url={self.BASE}/pulp-darwin-arm64.tar.gz"])
        home_bin = str(Path(record).parent / "home" / ".pulp" / "bin")
        self.assertEqual(lines[3], f"INSTALLER-RAN PULP_VERSION=0.880.0 DIR={home_bin} NOPATH=1 NOSDK=1 "
                                   "ARCHIVE=archive")

    def test_any_download_404_is_not_ready_and_installs_nothing(self) -> None:
        for missing in ("install.sh", "SHA256SUMS", "pulp-darwin-arm64.tar.gz"):
            with self.subTest(missing=missing):
                proc, record = self.run_script(missing=missing)
                self.assertEqual(proc.returncode, tf.NOT_READY_EXIT, proc.stderr)
                self.assertIn("not ready", proc.stderr)
                self.assertNotIn("INSTALLER-RAN", record.read_text())

    def test_a_checksum_file_without_this_platform_is_not_ready(self) -> None:
        proc, record = self.run_script(sums=f"{'1' * 64}  pulp-linux-x64.tar.gz\n")
        self.assertEqual(proc.returncode, tf.NOT_READY_EXIT, proc.stderr)
        self.assertNotIn("INSTALLER-RAN", record.read_text())

    def test_a_downloaded_archive_with_the_wrong_checksum_is_refused(self) -> None:
        proc, record = self.run_script(sums=f"{'0' * 64}  pulp-darwin-arm64.tar.gz\n")
        self.assertEqual(proc.returncode, 4)
        self.assertIn("refused", proc.stderr)
        self.assertNotIn("INSTALLER-RAN", record.read_text())

    def test_a_local_archive_is_installed_only_when_its_checksum_matches(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        good, bad = tmp / "good.tar.gz", tmp / "bad.tar.gz"
        good.write_text(self.ARCHIVE)
        bad.write_text("tampered\n")
        proc, record = self.run_script(local=str(good), missing="pulp-darwin-arm64.tar.gz")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ARCHIVE=archive", record.read_text())
        self.assertNotIn("url=" + self.BASE + "/pulp-darwin-arm64.tar.gz", record.read_text())
        proc, record = self.run_script(local=str(bad))
        self.assertEqual(proc.returncode, 4)
        self.assertIn("refused: local archive", proc.stderr)
        self.assertNotIn("INSTALLER-RAN", record.read_text())

    def test_refuses_an_installer_that_strands_the_runtime(self) -> None:
        proc, record = self.run_script("tar --exclude='libwgpu_native.dylib' -xzf x\n"
                                       "echo INSTALLER-RAN >> \"$RECORD\"\n")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("strands pulp-cpp without its runtime", proc.stderr)
        self.assertNotIn("INSTALLER-RAN", record.read_text())


class ReleaseProbeTests(unittest.TestCase):
    def test_every_asset_must_answer_head_200(self) -> None:
        seen = []

        def http(url, method="GET"):
            seen.append((method, url))
            if url.endswith("pulp-darwin-arm64.tar.gz"):
                return (200, b"") if self.uploaded else (404, b"")
            return (200, b"abc  pulp-darwin-arm64.tar.gz\n")
        assets = ["pulp-darwin-arm64.tar.gz", "SHA256SUMS"]
        self.uploaded = False
        value = tf.probe_release("Generous-Corp/pulp", "v1.0.0", assets, http)
        self.assertEqual((value["ready"], value["missing"]), (False, ["pulp-darwin-arm64.tar.gz"]))
        self.assertEqual(value["detail"], "asset pulp-darwin-arm64.tar.gz missing")
        self.uploaded = True
        value = tf.probe_release("Generous-Corp/pulp", "v1.0.0", assets, http)
        self.assertTrue(value["ready"])
        self.assertIn(("HEAD", "https://github.com/Generous-Corp/pulp/releases/download/v1.0.0/"
                               "pulp-darwin-arm64.tar.gz"), seen)
        self.assertEqual(tf.published_sha256(value["sums"], "pulp-darwin-arm64.tar.gz"), None)
        self.assertEqual(tf.published_sha256(f"{'a' * 64} *pulp-darwin-arm64.tar.gz",
                                             "pulp-darwin-arm64.tar.gz"), "a" * 64)

    def test_a_network_error_is_not_ready(self) -> None:
        value = tf.probe_release("o/r", "v1", ["x.tgz"], lambda url, method="GET": (0, b"timed out"))
        self.assertEqual(value["detail"], "asset x.tgz unreachable (HTTP none)")


class HostAgentsTests(unittest.TestCase):
    def test_a_managed_host_missing_its_watchdog_is_a_problem(self) -> None:
        import fleet_doctor as fd
        rows = [{"label": "com.danielraffel.tartci.self-update"},
                {"label": "com.danielraffel.tartci.tart-runner-macos-fleet.m5studio.pulp-gate"}]
        missing = fd.check_host_agents(rows, managed=True)
        self.assertEqual((missing.state, missing.code), (fd.PROBLEM, "host_agents_missing"))
        self.assertIn("com.danielraffel.tartci.launchd-watchdog", missing.detail)
        both = rows + [{"label": "com.danielraffel.tartci.launchd-watchdog"}]
        self.assertEqual(fd.check_host_agents(both, managed=True).code, "host_agents_ok")
        self.assertEqual(fd.check_host_agents(rows, managed=False).code,
                         "host_agents_not_applicable")
        self.assertEqual(fd.check_host_agents(None, managed=True, error="x").state, fd.UNKNOWN)
        for code in ("host_agents_missing", "host_agents_ok", "host_agents_not_applicable",
                     "host_agents_unreadable"):
            self.assertIn(code, fd.CODES)


    def test_doctor_fleet_reports_it_on_a_profiled_host(self) -> None:
        import fleet_doctor as fd
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "Library" / "LaunchAgents").mkdir(parents=True)
        config = tmp / ".config" / "tartci"
        config.mkdir(parents=True)
        (config / "macos-fleet-profile.toml").write_text('[host]\nid = "m5studio"\n')

        def launchd(argv: list[str]) -> tuple[int, str, str]:
            if argv[1] == "list":
                return 0, "PID\tStatus\tLabel\n-\t0\tcom.danielraffel.tartci.self-update\n", ""
            return 0, f"\tpath = {tmp}/Library/LaunchAgents/x.plist\n", ""

        rows = fd.collect(home=tmp, skip_census=True, probe=lambda root: {"error": "stub"},
                          launchd_run=launchd)
        agents = next(row for row in rows if row.check == "host_agents")
        self.assertEqual(agents.code, "host_agents_missing")


class HealPassTests(unittest.TestCase):
    """`tartci launchd heal` must run the watchdog even when the relay reconcile fails."""

    def run_heal(self, reconcile_rc: int,
                 line: str = "network-profile: FAIL: authenticated relay probe timed out after 15s",
                 ) -> tuple[subprocess.CompletedProcess, Path]:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = tmp / "tartci-root"
        (root / "scripts").mkdir(parents=True)
        shutil.copy(HERE.parent / "tartci", root / "tartci")
        # The shim sources its Python resolver from this lib.
        (root / "providers" / "common").mkdir(parents=True)
        shutil.copy(HERE.parent / "providers" / "common" / "toml-python.lib.sh",
                    root / "providers" / "common" / "toml-python.lib.sh")
        marker = tmp / "watchdog-ran"
        (root / "scripts" / "network_profile.py").write_text(textwrap.dedent(f"""\
            import sys
            print({line!r})
            sys.exit({reconcile_rc})
            """))
        (root / "scripts" / "tartci_launchd_watchdog.py").write_text(textwrap.dedent(f"""\
            import pathlib, sys
            pathlib.Path({str(marker)!r}).write_text(" ".join(sys.argv[1:]))
            """))
        env = dict(os.environ, TARTCI_PYTHON=sys.executable, TARTCI_HOME=str(tmp / "home"))
        proc = subprocess.run(["bash", str(root / "tartci"), "launchd", "heal",
                               "--stale-log-seconds", "4500"],
                              capture_output=True, text=True, env=env, timeout=60)
        return proc, marker

    def test_a_failed_reconcile_still_runs_the_watchdog_and_exits_6(self) -> None:
        if sys.version_info < (3, 11):
            self.skipTest("the tartci shim needs a tomllib interpreter")
        proc, marker = self.run_heal(1)
        self.assertTrue(marker.exists(), proc.stdout + proc.stderr)
        self.assertEqual(marker.read_text(), "--stale-log-seconds 4500")
        self.assertEqual(proc.returncode, 6)
        self.assertIn("relay probe timed out", proc.stdout)

    def test_a_deferred_reconcile_is_printed_but_is_not_exit_6(self) -> None:
        # Deferral (lock held, a VM running) is the normal state of a busy gate
        # host. Reporting it as 6 made launchd's last exit look like the relay
        # was down on nearly every pass.
        if sys.version_info < (3, 11):
            self.skipTest("the tartci shim needs a tomllib interpreter")
        proc, marker = self.run_heal(
            3, "network-profile: DEFERRED: network-profile reload deferred while a Tart VM is running")
        self.assertTrue(marker.exists(), proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("DEFERRED: network-profile reload deferred", proc.stdout)

    def test_a_clean_reconcile_runs_the_watchdog_quietly(self) -> None:
        if sys.version_info < (3, 11):
            self.skipTest("the tartci shim needs a tomllib interpreter")
        proc, marker = self.run_heal(0)
        self.assertTrue(marker.exists(), proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("network-profile", proc.stdout)


if __name__ == "__main__":
    unittest.main()
