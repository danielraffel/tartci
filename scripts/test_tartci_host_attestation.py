#!/usr/bin/env python3
"""Hermetic tests for the host attestation writer.

Every test here is a **negative control**: each one asserts the detector
*fires* on data shaped like the 2026-09-13 incident, so that a change which
silences it fails here rather than in production six hours into an outage.

The pairing rule the repository standards require is honoured throughout: each
"this must be a fault" assertion sits next to a "this must be healthy" case on
the same code path, because a classifier that returns `broken` for everything
passes the first kind of test and is worthless.

Run: python3 scripts/test_tartci_host_attestation.py
"""

from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tartci_host_attestation as att  # noqa: E402


PRINT_CRASH_LOOP = """\
com.apple.xpc.launchd.user.domain.501.100004.Aqua/actions.runner.x.y = {
\tactive count = 0
\tstate = spawn scheduled
\tprogram = /Users/x/actions-runner/run.sh
\truns = 3684
\tlast exit code = 1
}
"""

PRINT_HEALTHY = """\
com.apple.xpc.launchd.user.domain.501.100004.Aqua/actions.runner.x.y = {
\tactive count = 1
\tstate = running
\tpid = 4242
\tprogram = /Users/x/actions-runner/run.sh
\truns = 1
\tlast exit code = (never exited)
}
"""


class ParseLaunchctlPrint(unittest.TestCase):
    def test_crash_loop_state_and_runs_are_both_visible(self):
        parsed = att.parse_launchctl_print(PRINT_CRASH_LOOP)
        self.assertEqual(parsed["state"], "spawn scheduled")
        self.assertEqual(parsed["runs"], 3684)
        self.assertEqual(parsed["last_exit_code"], "1")

    def test_healthy_service_reads_differently(self):
        # The control. `launchctl list` renders BOTH of these as `- 0`; if this
        # test and the one above ever agree, the instrument has gone blind in
        # exactly the way that hid the M5 preamble runner.
        parsed = att.parse_launchctl_print(PRINT_HEALTHY)
        self.assertEqual(parsed["state"], "running")
        self.assertEqual(parsed["runs"], 1)
        self.assertIsNone(parsed["last_exit_code"])

    def test_unparseable_output_measures_nothing_rather_than_zero(self):
        self.assertEqual(att.parse_launchctl_print("Could not find service"), {})


class RunnerRegistration(unittest.TestCase):
    def test_bom_prefixed_runner_file_parses(self):
        # GitHub writes .runner with a UTF-8 BOM. A plain utf-8 read raises,
        # and a reader that swallows that reports a healthy runner as
        # unregistered. This is the single most load-bearing encoding in the
        # module.
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"agentId": 28732, "gitHubUrl": "https://github.com/Generous-Corp/pulp"}
            with open(os.path.join(tmp, ".runner"), "wb") as handle:
                handle.write(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))
            registered, slug, _, detail = att.read_runner_registration(tmp)
        self.assertTrue(registered, detail)
        self.assertEqual(slug, "Generous-Corp/pulp")

    def test_missing_runner_file_is_unregistered(self):
        with tempfile.TemporaryDirectory() as tmp:
            registered, slug, _, detail = att.read_runner_registration(tmp)
        self.assertFalse(registered)
        self.assertIsNone(slug)
        self.assertIn(".runner absent", detail)

    def test_slug_match_flattens_the_slash(self):
        self.assertTrue(att.slug_matches_repo("Generous-Corp-pulp", "Generous-Corp/pulp"))
        # The pre-org-move slug on a host serving the post-move repo: the exact
        # drift that survived 2026-07-19 unnoticed.
        self.assertFalse(att.slug_matches_repo("danielraffel-pulp", "Generous-Corp/pulp"))
        self.assertFalse(att.slug_matches_repo(None, "Generous-Corp/pulp"))


class CrashLoopRate(unittest.TestCase):
    def test_rate_is_computed_from_the_previous_sample(self):
        now = 10_000.0
        prior = {"a": (100, now - 3600)}
        self.assertAlmostEqual(att.launch_rate_per_hour("a", 160, prior, now), 60.0)

    def test_a_deliberately_restarted_runner_is_not_a_crash_loop(self):
        # The control that keeps the detector from crying wolf: one restart an
        # hour after a prior sample is a restart, not a loop.
        now = 10_000.0
        prior = {"a": (100, now - 3600)}
        rate = att.launch_rate_per_hour("a", 101, prior, now)
        self.assertLess(rate, att.CRASH_LOOP_RUNS_PER_HOUR)

    def test_no_prior_sample_yields_no_rate(self):
        self.assertIsNone(att.launch_rate_per_hour("a", 100, {}, 10_000.0))


def _write_plist(directory: str, label: str, working_dir: str | None = None) -> str:
    path = os.path.join(directory, f"{label}.plist")
    payload = {"Label": label, "ProgramArguments": ["/bin/true"]}
    if working_dir:
        payload["WorkingDirectory"] = working_dir
    with open(path, "wb") as handle:
        plistlib.dump(payload, handle)
    return path


class PersistentRunnerVerdicts(unittest.TestCase):
    """The five facts, each broken in turn, each producing its own verdict."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.label = "actions.runner.Generous-Corp-pulp.pulp-preamble-m3"
        self.runner_dir = os.path.join(self.tmp.name, "runner")
        os.makedirs(self.runner_dir)
        self.plist = _write_plist(self.tmp.name, self.label, self.runner_dir)
        self._printed = {}
        self._orig_run = att._run
        att._run = self._fake_run
        self.addCleanup(setattr, att, "_run", self._orig_run)

    def _fake_run(self, cmd, timeout=20):  # noqa: ARG002
        if cmd[:2] == ["launchctl", "print"]:
            target = cmd[2].split("/")[-1]
            if target in self._printed:
                return 0, self._printed[target], ""
            return 113, "", "Could not find service"
        return self._orig_run(cmd, timeout)

    def _register(self, url="https://github.com/Generous-Corp/pulp"):
        with open(os.path.join(self.runner_dir, ".runner"), "wb") as handle:
            handle.write(b"\xef\xbb\xbf" + json.dumps({"gitHubUrl": url}).encode("utf-8"))

    def test_not_loaded_is_broken(self):
        record = att.assess_persistent_runner(
            self.label, self.plist, "Generous-Corp/pulp", ["pulp-preamble"], 300
        )
        self.assertEqual(record["verdict"], "broken")
        self.assertIn("launchd does not have it loaded", record["reason"])

    def test_crash_loop_is_broken_and_says_how_many_spawns(self):
        self._printed[self.label] = PRINT_CRASH_LOOP
        self._register()
        record = att.assess_persistent_runner(
            self.label, self.plist, "Generous-Corp/pulp", ["pulp-preamble"], 300
        )
        self.assertTrue(record["crash_loop"])
        self.assertEqual(record["verdict"], "broken")
        self.assertIn("3684", record["reason"])

    def test_loaded_and_alive_but_unregistered_is_its_own_verdict(self):
        # The M5 state exactly: plist present, loaded, running, and GitHub has
        # never heard of it. Four of five facts pass.
        self._printed[self.label] = PRINT_HEALTHY
        record = att.assess_persistent_runner(
            self.label, self.plist, "Generous-Corp/pulp", ["pulp-preamble"], 300
        )
        self.assertEqual(record["verdict"], "unregistered")

    def test_registered_against_the_pre_org_move_repo_is_stale_repo(self):
        self._printed[self.label] = PRINT_HEALTHY
        self._register("https://github.com/danielraffel/pulp")
        record = att.assess_persistent_runner(
            self.label, self.plist, "Generous-Corp/pulp", ["pulp-preamble"], 300
        )
        self.assertEqual(record["verdict"], "stale_repo")
        self.assertIn("danielraffel/pulp", record["reason"])

    def test_a_fully_healthy_runner_is_healthy(self):
        # The control. Without it every assertion above is satisfied by a
        # function that returns "broken" unconditionally.
        self._printed[self.label] = PRINT_HEALTHY
        self._register()
        record = att.assess_persistent_runner(
            self.label, self.plist, "Generous-Corp/pulp", ["pulp-preamble"], 300
        )
        self.assertEqual(record["verdict"], "healthy", record["reason"])
        self.assertTrue(record["registered"])
        self.assertFalse(record["crash_loop"])

    def test_unknown_labels_are_flagged_so_nothing_silently_matches(self):
        self._printed[self.label] = PRINT_HEALTHY
        self._register()
        record = att.assess_persistent_runner(
            self.label, self.plist, "Generous-Corp/pulp", [], 300
        )
        self.assertTrue(record["advertises_unknown"])
        self.assertEqual(record["advertises"], [])


class JitLaneAttestation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.state = os.path.join(self.home, ".tartci", "state", "macos-fleet", "pulp-gate")
        os.makedirs(self.state)

    def _heartbeat(self, age_secs: float):
        stamp = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_secs)
        )
        path = os.path.join(self.state, "runner-1.state.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"ts": stamp, "phase": "idle-wait"}, handle)

    def test_a_fresh_heartbeat_attests_the_lane(self):
        self._heartbeat(5)
        record = att.assess_jit_lane(
            {"id": "pulp-gate", "repo": "r", "labels": ["a"], "supervisors": 2},
            self.home,
            time.time(),
            300,
        )
        self.assertEqual(record["verdict"], "attested")
        self.assertEqual(record["fresh"], 1)

    def test_a_stale_heartbeat_does_not_attest(self):
        self._heartbeat(4000)
        record = att.assess_jit_lane(
            {"id": "pulp-gate", "repo": "r", "labels": ["a"], "supervisors": 2},
            self.home,
            time.time(),
            300,
        )
        self.assertEqual(record["verdict"], "unattested")
        self.assertIn("old", record["reason"])

    def test_no_heartbeat_file_says_so_rather_than_reporting_zero_quietly(self):
        record = att.assess_jit_lane(
            {"id": "pulp-gate", "repo": "r", "labels": ["a"], "supervisors": 2},
            self.home,
            time.time(),
            300,
        )
        self.assertEqual(record["verdict"], "unattested")
        self.assertIn("no supervisor heartbeat file", record["reason"])


class LaunchdSelfCheck(unittest.TestCase):
    """The sensor reporting its own failure — the deliverable's own bar."""

    def setUp(self):
        self._orig_run = att._run
        self.addCleanup(setattr, att, "_run", self._orig_run)

    def test_unreadable_domain_is_reported_not_treated_as_empty(self):
        att._run = lambda cmd, timeout=20: (113, "", "Could not find service")
        ok, detail = att.launchd_self_check("com.danielraffel.tartci.launchd-watchdog")
        self.assertFalse(ok)
        self.assertIn("read as absent", detail)

    def test_a_domain_that_answers_yes_to_everything_is_rejected(self):
        att._run = lambda cmd, timeout=20: (0, PRINT_HEALTHY, "")
        ok, detail = att.launchd_self_check("com.danielraffel.tartci.launchd-watchdog")
        self.assertFalse(ok)
        self.assertIn("cannot exist reported present", detail)

    def test_a_discriminating_domain_passes(self):
        def fake(cmd, timeout=20):  # noqa: ARG001
            if "control-never-installed" in cmd[2]:
                return 113, "", "Could not find service"
            return 0, PRINT_HEALTHY, ""

        att._run = fake
        ok, detail = att.launchd_self_check("com.danielraffel.tartci.launchd-watchdog")
        self.assertTrue(ok, detail)

    def test_no_known_present_label_is_a_refusal_not_a_pass(self):
        att._run = lambda cmd, timeout=20: (113, "", "")
        ok, detail = att.launchd_self_check(None)
        self.assertFalse(ok)
        self.assertIn("cannot be told apart", detail)


class SensorCensus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_run = att._run
        self.addCleanup(setattr, att, "_run", self._orig_run)

    def test_a_sensor_crash_looping_every_tick_is_a_finding(self):
        # com.danielraffel.pulp.queue-saturation: roughly 26,400 consecutive
        # crashed ticks over three months, and nothing reported it.
        label = "com.danielraffel.pulp.queue-saturation"
        path = _write_plist(self.tmp.name, label)
        att._run = lambda cmd, timeout=20: (0, PRINT_CRASH_LOOP, "")  # noqa: ARG005
        census = att.sensor_census([(label, path)], time.time(), expected_labels={label})
        self.assertEqual(len(census), 1)
        self.assertIsNotNone(census[0]["finding"], census[0])

    def test_an_unloaded_sensor_is_a_finding(self):
        label = "com.danielraffel.shipyard.queue-tick"
        path = _write_plist(self.tmp.name, label)
        att._run = lambda cmd, timeout=20: (113, "", "")  # noqa: ARG005
        census = att.sensor_census([(label, path)], time.time(), expected_labels=set())
        self.assertEqual(census[0]["finding"], "not loaded")

    def test_a_retired_runner_leftover_is_not_a_finding(self):
        # M3 carries eight unloaded Actions-runner plists from before the
        # 2026-07-19 organisation move. Each one reported as a fault is how an
        # alarm channel becomes noise, which is worse than silence.
        label = "actions.runner.danielraffel-pulp.pulp-studio-01"
        path = _write_plist(self.tmp.name, label)
        att._run = lambda cmd, timeout=20: (113, "", "")  # noqa: ARG005
        census = att.sensor_census([(label, path)], time.time(), expected_labels=set())
        self.assertTrue(census[0]["retired_leftover"])
        self.assertIsNone(census[0]["finding"])

    def test_a_DECLARED_runner_that_is_not_loaded_is_still_a_finding(self):
        # The control for the suppression above: suppressing a leftover must
        # not also suppress the declared runner whose absence is the fault.
        label = "actions.runner.Generous-Corp-pulp.pulp-preamble-m3"
        path = _write_plist(self.tmp.name, label)
        att._run = lambda cmd, timeout=20: (113, "", "")  # noqa: ARG005
        census = att.sensor_census([(label, path)], time.time(), expected_labels={label})
        self.assertFalse(census[0]["retired_leftover"])
        self.assertEqual(census[0]["finding"], "not loaded")

    def test_a_healthy_sensor_is_not_a_finding(self):
        # The control: without it, "every sensor is a finding" would pass both
        # tests above and tell nobody anything.
        label = "com.danielraffel.tartci.launchd-watchdog"
        path = _write_plist(self.tmp.name, label)
        att._run = lambda cmd, timeout=20: (0, PRINT_HEALTHY, "")  # noqa: ARG005
        census = att.sensor_census([(label, path)], time.time())
        self.assertIsNone(census[0]["finding"], census[0])


class ProfileReadability(unittest.TestCase):
    """The bug the first deployment shipped: launchd ran this under macOS's
    /usr/bin/python3 (3.9, no tomllib), the profile parsed as {}, and the
    record declared zero lanes without saying it had failed to look."""

    def test_a_missing_profile_is_readable_and_honest(self):
        profile, readable, detail = att.load_profile("/nonexistent/profile.toml")
        self.assertEqual(profile, {})
        self.assertTrue(readable)
        self.assertIn("declares no fleet lanes", detail)

    def test_an_unparseable_profile_is_NOT_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "profile.toml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("this is not = = valid toml [[[\n")
            profile, readable, detail = att.load_profile(path)
        self.assertEqual(profile, {})
        self.assertFalse(readable, detail)

    def test_a_valid_profile_is_readable(self):
        # The control: without it, "nothing is ever readable" passes the test
        # above and makes every attestation useless.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "profile.toml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('schema = 1\n[host]\nid = "m5"\n')
            profile, readable, detail = att.load_profile(path)
        self.assertTrue(readable, detail)
        self.assertEqual(profile["host"]["id"], "m5")


class PriorSamples(unittest.TestCase):
    def test_prior_runs_are_read_back_from_the_last_attestation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "host-attestation.json")
            att.atomic_write_json(
                path,
                {
                    "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "persistent_runners": [{"label": "a", "runs": 42}],
                },
            )
            prior = att.load_prior_samples(path)
        self.assertIn("a", prior)
        self.assertEqual(prior["a"][0], 42)

    def test_a_missing_prior_file_yields_no_samples_rather_than_raising(self):
        self.assertEqual(att.load_prior_samples("/nonexistent/attestation.json"), {})


if __name__ == "__main__":
    unittest.main()
