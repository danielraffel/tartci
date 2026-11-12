#!/usr/bin/env python3
"""Hermetic tests for the pool capacity floor.

Both directions are asserted for every guard: a fixture that must refuse AND a
fixture that must proceed. A guard tested only on its refusal passes just as
well when it refuses everything, which is the failure mode that trains an
operator to reach for the override by reflex.
"""

from __future__ import annotations

import testing_support  # noqa: E402
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capacity_floor
import runner_census

REPO = "Generous-Corp/pulp"
REPO_ENDPOINT = "repos/Generous-Corp/pulp/actions/runners"
ORG_ENDPOINT = "orgs/Generous-Corp/actions/runners"
GATE = "pulp-build-pr-head"
BASE = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]

FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    fixture = json.load(open(os.environ["FAKE_GH_FIXTURE"]))
    endpoint = sys.argv[2].split("?")[0]
    entry = fixture.get(endpoint)
    if entry is None:
        entry = {"runners": []}
    if "fail" in entry:
        sys.stderr.write(entry["fail"] + "\\n")
        sys.exit(1)
    json.dump([{"runners": entry["runners"]}], sys.stdout)
    """
)

# A fake `ssh`: the target is the argument before the remote command, and the
# fixture maps it to that peer's `pool status --json` (or an exit code).
FAKE_SSH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    fixture = json.load(open(os.environ["FAKE_SSH_FIXTURE"]))
    entry = fixture.get(sys.argv[-2], {"rc": 255, "stderr": "ssh: connect: Connection refused"})
    if "status" in entry:
        json.dump(entry["status"], sys.stdout)
    sys.stderr.write(entry.get("stderr", ""))
    sys.exit(entry.get("rc", 0))
    """
)


def live_status(**fleet_overrides) -> dict:
    """A peer's `pool status --json` that proves it mints on demand now."""
    fleet = {
        "managed": True,
        "fleet_ready": True,
        "problems": [],
        "expected_supervisors": 2,
        "verified_running_supervisors": 2,
        "serving": {"blocked": False},
        "config": {"supply": {"state": "match"}},
    }
    fleet.update(fleet_overrides)
    return {"state": "on", "participating": True, "fleet": fleet}


def published(*hosts: str, label: str = GATE, repo: str = REPO) -> dict:
    """A published supply in which each host declares `label` for `repo`."""
    return {
        "hosts": [{"host_id": host, "ssh": f"ssh-{host}"} for host in ("studio", *hosts)],
        "registrations": [
            {"host_id": host, "repo": repo, "class_label": label, "labels": BASE + [label]}
            for host in ("studio", *hosts)
        ],
    }


PROFILE = textwrap.dedent(
    """\
    schema = 1
    name = "studio-macos-fleet"

    [host]
    id = "studio"
    home = "/Users/ci"

    [[lane]]
    id = "pulp-gate"
    repo = "Generous-Corp/pulp"
    supervisors = 2
    labels = ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm"]

    [[lane.tier]]
    label = "pulp-build-pr-head"
    workflow = "Build and Test"
    """
)


def runner(runner_id: int, name: str, *, status: str = "online", labels: list[str] | None = None) -> dict:
    return {
        "id": runner_id,
        "name": name,
        "status": status,
        "busy": False,
        "labels": [{"name": label} for label in (labels if labels is not None else BASE + [GATE])],
    }


def census(repo_runners=(), org_runners=(), *, unreachable=()) -> runner_census.RunnerCensus:
    pages = {REPO_ENDPOINT: list(repo_runners), ORG_ENDPOINT: list(org_runners)}

    def fetch(scope: str, endpoint: str) -> list[dict]:
        if scope in unreachable:
            raise runner_census.CensusScopeError(scope, endpoint, "http_403", "not accessible")
        return pages[endpoint]

    return runner_census.collect(REPO, fetch)


def record(name: str, *, status: str = "online", labels: list[str] | None = None):
    """A normalised census record, the shape the ownership matcher consumes."""
    return runner_census.normalise(
        runner(1, name, status=status, labels=labels),
        runner_census.REPOSITORY_SCOPE,
        REPO_ENDPOINT,
    )


def owned_studio(record) -> bool:
    return capacity_floor.owner_matcher("studio", ("studio-pulp-gate",), ())(record)


PROTECTED = (capacity_floor.Protected(REPO, GATE),)


class DecisionCoreTests(unittest.TestCase):
    """Both cells of the capacity question, decided without any I/O."""

    def decide(self, census_obj, **kwargs) -> capacity_floor.Decision:
        return capacity_floor.classify(
            host="studio",
            action="drain",
            protected=PROTECTED,
            censuses={REPO: census_obj},
            owned=owned_studio,
            **kwargs,
        )

    def test_last_serving_host_refuses(self) -> None:
        decision = self.decide(census([runner(1, "studio-pulp-gate-01-612-7")]))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, capacity_floor.REASON_LAST_SERVING_HOST)
        self.assertIn(GATE, decision.message)
        self.assertIn("studio", decision.message)
        self.assertEqual(decision.exit_code(), capacity_floor.EXIT_LAST_SERVING_HOST)

    def test_another_host_serving_proceeds(self) -> None:
        decision = self.decide(
            census([runner(1, "studio-pulp-gate-01-612-7"), runner(2, "m5-pulp-gate-01-99-1")])
        )

        self.assertTrue(decision.allowed)
        self.assertFalse(decision.overridden)
        self.assertEqual(decision.findings[0].verdict, capacity_floor.SERVED_ELSEWHERE)
        self.assertEqual(decision.findings[0].remaining, ("m5-pulp-gate-01-99-1",))

    def test_peer_registered_only_on_the_organization_proceeds(self) -> None:
        # The capacity floor inherits the dual-scope census: a peer that exists
        # only at organization scope is capacity, and a repository-only reading
        # would refuse this drain for no reason.
        decision = self.decide(
            census([runner(1, "studio-pulp-gate-01-612-7")], [runner(9, "pulp-intel-macmini")])
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.findings[0].remaining, ("pulp-intel-macmini",))

    def test_offline_peer_is_not_capacity(self) -> None:
        decision = self.decide(
            census([runner(1, "studio-pulp-gate-01-612-7"), runner(2, "m5-pulp-gate-01", status="offline")])
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, capacity_floor.REASON_LAST_SERVING_HOST)
        self.assertIn("m5-pulp-gate-01", decision.findings[0].detail)

    def test_unreachable_scope_refuses_as_indeterminate(self) -> None:
        decision = self.decide(
            census([runner(1, "studio-pulp-gate-01-612-7")], unreachable=(runner_census.ORGANIZATION_SCOPE,))
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, capacity_floor.REASON_CAPACITY_UNKNOWN)
        self.assertEqual(decision.exit_code(), capacity_floor.EXIT_INDETERMINATE)
        self.assertIn("organization", decision.message)

    def test_an_unreachable_scope_outranks_a_refusal_it_could_explain(self) -> None:
        # The unread scope is exactly where the missing peer would live, so the
        # verdict is "unknown", never "you are the last one".
        decision = self.decide(census(unreachable=(runner_census.ORGANIZATION_SCOPE,)))

        self.assertEqual(decision.reason, capacity_floor.REASON_CAPACITY_UNKNOWN)

    def test_override_proceeds_and_names_the_consequence(self) -> None:
        decision = self.decide(
            census([runner(1, "studio-pulp-gate-01-612-7")]), allow_last_serving_host=True
        )

        self.assertTrue(decision.allowed)
        self.assertTrue(decision.overridden)
        self.assertEqual(decision.reason, capacity_floor.REASON_LAST_SERVING_HOST)
        self.assertIn("zero runners", decision.message)

    def test_override_does_not_paper_over_an_unknown_census(self) -> None:
        decision = self.decide(
            census(unreachable=(runner_census.ORGANIZATION_SCOPE,)), allow_last_serving_host=True
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, capacity_floor.REASON_CAPACITY_UNKNOWN)

    def test_no_protected_label_proceeds(self) -> None:
        decision = capacity_floor.classify(
            host="studio", action="drain", protected=(), censuses={}, owned=owned_studio
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.findings, ())

    def test_a_missing_census_is_unknown_not_empty(self) -> None:
        decision = capacity_floor.classify(
            host="studio", action="drain", protected=PROTECTED, censuses={}, owned=owned_studio
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, capacity_floor.REASON_CAPACITY_UNKNOWN)


class OnDemandPeerDecisionTests(unittest.TestCase):
    """An idle JIT fleet registers no runner, so a declaring live peer is capacity."""

    def decide(self, census_obj, evidence, **kwargs) -> capacity_floor.Decision:
        asked: list[tuple[str, str]] = []

        def peers(repo: str, label: str):
            asked.append((repo, label))
            return evidence

        decision = capacity_floor.classify(
            host="studio", action="off", protected=PROTECTED,
            censuses={REPO: census_obj}, owned=owned_studio, peers=peers, **kwargs,
        )
        self.asked = asked
        return decision

    def test_a_declaring_live_peer_serves_the_label(self) -> None:
        decision = self.decide(
            census(), [capacity_floor.PeerEvidence("m5", True, "pool on, fleet ready")]
        )

        self.assertTrue(decision.allowed, decision.message)
        self.assertFalse(decision.overridden)
        finding = decision.findings[0]
        self.assertEqual(finding.verdict, capacity_floor.SERVED_ELSEWHERE)
        self.assertEqual(finding.remaining, ("m5 (mints on demand)",))
        self.assertIn("m5 (mints on demand)", decision.message)

    def test_one_live_peer_is_enough_and_every_peer_is_named(self) -> None:
        decision = self.decide(census([runner(1, "studio-pulp-gate-01-612-7")]), [
            capacity_floor.PeerEvidence("m1", False, "pool is draining (participating=False)"),
            capacity_floor.PeerEvidence("m5", True, "pool on, fleet ready"),
        ])

        self.assertTrue(decision.allowed, decision.message)
        self.assertIn("m1 not counted (pool is draining", decision.findings[0].detail)
        self.assertIn("m5 counted", decision.findings[0].detail)

    def test_declaring_peers_that_are_not_provably_live_refuse(self) -> None:
        for why in ("pool is off (participating=False)",
                    "pool is draining (participating=False)",
                    "fleet problems: [{'code': 'heartbeat_stale'}]",
                    "pool status unreadable via ssh-m5 (exit 255): Connection refused"):
            with self.subTest(why=why):
                decision = self.decide(census(), [capacity_floor.PeerEvidence("m5", False, why)])

                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, capacity_floor.REASON_LAST_SERVING_HOST)
                self.assertEqual(decision.exit_code(), capacity_floor.EXIT_LAST_SERVING_HOST)
                self.assertIn(f"m5 not counted ({why})", decision.message)

    def test_no_declaring_peer_refuses_and_says_so(self) -> None:
        decision = self.decide(census(), [])

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, capacity_floor.REASON_LAST_SERVING_HOST)
        self.assertIn("no other host declares a lane tier for it", decision.message)

    def test_an_online_runner_elsewhere_still_serves_without_asking_peers(self) -> None:
        decision = self.decide(
            census([runner(2, "m5-pulp-gate-01-99-1")]),
            [capacity_floor.PeerEvidence("m5", False, "never consulted")],
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.findings[0].remaining, ("m5-pulp-gate-01-99-1",))
        self.assertEqual(self.asked, [])

    def test_a_live_peer_does_not_answer_an_unknown_census(self) -> None:
        decision = self.decide(
            census(unreachable=(runner_census.ORGANIZATION_SCOPE,)),
            [capacity_floor.PeerEvidence("m5", True, "pool on, fleet ready")],
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, capacity_floor.REASON_CAPACITY_UNKNOWN)

    def test_override_still_takes_an_unserved_label_to_zero(self) -> None:
        decision = self.decide(
            census(), [capacity_floor.PeerEvidence("m5", False, "pool is off (participating=False)")],
            allow_last_serving_host=True,
        )

        self.assertTrue(decision.allowed)
        self.assertTrue(decision.overridden)


class PeerJudgementTests(unittest.TestCase):
    def test_a_fully_verified_peer_mints_on_demand(self) -> None:
        self.assertIsNone(capacity_floor.peer_mints_on_demand(live_status()))

    def test_every_unproven_field_disqualifies(self) -> None:
        cases = {
            "off": {**live_status(), "state": "off", "participating": False},
            "draining": {**live_status(), "state": "draining", "participating": False},
            "no fleet section": {"state": "on", "participating": True},
            "not ready": live_status(fleet_ready=False),
            "stale heartbeat": live_status(problems=[{"code": "heartbeat_stale"}]),
            "supervisor down": live_status(verified_running_supervisors=1),
            "serving blocked": live_status(serving={"blocked": True}),
            "supply drift": live_status(config={"supply": {"state": "drift"}}),
            "unreadable": None,
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                self.assertIsNotNone(capacity_floor.peer_mints_on_demand(value))

    def test_declaring_hosts_excludes_this_host_and_other_repositories(self) -> None:
        supply = published("m5", "m1")
        supply["registrations"].append(
            {"host_id": "m9", "repo": "other/repo", "class_label": GATE, "labels": [GATE]}
        )

        self.assertEqual(
            capacity_floor.declaring_hosts(supply, repo=REPO, label=GATE, exclude="studio"),
            ("m1", "m5"),
        )
        self.assertEqual(
            capacity_floor.declaring_hosts(supply, repo=REPO, label="unknown", exclude="studio"), ()
        )


class OwnershipTests(unittest.TestCase):
    def matcher(self, profile: dict):
        host = capacity_floor.host_identity(profile)
        return capacity_floor.owner_matcher(
            host,
            capacity_floor.owned_name_prefixes(profile, host),
            capacity_floor.persistent_runner_names(profile),
        )

    def profile(self, **overrides) -> dict:
        data = {
            "host": {"id": "studio"},
            "lane": [{"id": "pulp-gate", "repo": REPO, "supervisors": 2, "tier": [{"label": GATE}]}],
        }
        data["host"].update(overrides.pop("host", {}))
        data.update(overrides)
        return data

    def test_this_hosts_per_boot_registrations_are_owned(self) -> None:
        owned = self.matcher(self.profile())

        for name in (
            "studio-pulp-gate-01-612-7",
            "studio-pulp-gate-slot2-02-71829-2",
            "studio",
        ):
            with self.subTest(name=name):
                self.assertTrue(owned(record(name)))

    def test_another_hosts_registration_is_not_owned(self) -> None:
        owned = self.matcher(self.profile())

        for name in ("m5-pulp-gate-slot2-02-2331-39", "m1-forge-gate-01-1-1", "pulp-intel-macmini"):
            with self.subTest(name=name):
                self.assertFalse(owned(record(name)))

    def test_a_host_id_is_not_a_prefix_of_a_longer_host_id(self) -> None:
        owned = self.matcher(self.profile(host={"id": "m1"}))

        self.assertFalse(owned(record("m10-pulp-gate-01-1-1")))
        self.assertTrue(owned(record("m1-pulp-gate-01-1-1")))

    def test_a_persistent_service_of_this_host_is_owned(self) -> None:
        profile = self.profile(
            host={"persistent_runner_labels": ["actions.runner.danielraffel-pulp.pulp-preamble-m5"]}
        )

        owned = self.matcher(profile)

        self.assertTrue(owned(record("pulp-preamble-m5")))

    def test_a_persistent_service_of_this_host_is_not_peer_capacity(self) -> None:
        profile = self.profile(
            host={"persistent_runner_labels": ["actions.runner.danielraffel-pulp.pulp-preamble-m5"]}
        )
        owned = self.matcher(profile)

        decision = capacity_floor.classify(
            host="studio",
            action="drain",
            protected=PROTECTED,
            censuses={REPO: census([runner(1, "pulp-preamble-m5")])},
            owned=owned,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, capacity_floor.REASON_LAST_SERVING_HOST)

    def test_an_unnameable_persistent_service_refuses(self) -> None:
        with self.assertRaises(capacity_floor.GuardError) as raised:
            capacity_floor.persistent_runner_names({"host": {"persistent_runner_labels": ["nonsense"]}})

        self.assertEqual(raised.exception.reason, capacity_floor.REASON_PERSISTENT_RUNNER_NAME_UNKNOWN)

    def test_a_profile_without_a_host_id_refuses(self) -> None:
        with self.assertRaises(capacity_floor.GuardError) as raised:
            capacity_floor.host_identity({"lane": []})

        self.assertEqual(raised.exception.reason, capacity_floor.REASON_HOST_IDENTITY_UNKNOWN)


class ProtectedLabelTests(unittest.TestCase):
    def test_tier_labels_are_protected_and_base_labels_are_not(self) -> None:
        profile = {
            "lane": [
                {
                    "id": "pulp-gate",
                    "repo": REPO,
                    "labels": BASE,
                    "tier": [{"label": GATE}, {"label": "pulp-build-merge-group"}],
                }
            ]
        }

        protected = capacity_floor.protected_labels(profile)

        self.assertEqual(
            [entry.label for entry in protected], [GATE, "pulp-build-merge-group"]
        )

    def test_a_lane_without_tiers_protects_nothing(self) -> None:
        profile = {"lane": [{"id": "spectr-gate", "repo": "danielraffel/spectr", "labels": BASE}]}

        self.assertEqual(capacity_floor.protected_labels(profile), ())

    def test_caller_supplied_labels_extend_the_profile(self) -> None:
        profile = {"lane": [{"id": "pulp-gate", "repo": REPO, "tier": [{"label": GATE}]}]}

        protected = capacity_floor.protected_labels(profile, ["pulp-gate-fast", "o/r=custom"])

        self.assertIn(capacity_floor.Protected(REPO, "pulp-gate-fast"), protected)
        self.assertIn(capacity_floor.Protected("o/r", "custom"), protected)


class PeerSshDestinationTests(unittest.TestCase):
    """A published supply's `ssh` reaches ssh argv only as a plain destination."""

    def test_a_hostile_published_ssh_is_never_dialled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "pwned"
            calls = root / "calls"
            supply = published("m5", "m1")
            supply["hosts"][1]["ssh"] = f"-oProxyCommand=touch {marker}"   # m5
            supply_path = root / "advertised-labels.json"
            supply_path.write_text(json.dumps(supply), encoding="utf-8")
            ssh = root / "ssh"
            ssh.write_text("#!/bin/bash\n"
                           f"printf '%s\\n' \"$*\" >> {str(calls)!r}\n"
                           f"echo {json.dumps(json.dumps(live_status()))}\n",
                           encoding="utf-8")
            ssh.chmod(0o755)
            resolve = capacity_floor.peer_resolver(supply_path, host="studio", ssh=str(ssh),
                                                   timeout=20)
            evidence = {peer.host: peer for peer in resolve(REPO, GATE)}
            argv = calls.read_text().splitlines()

        # Negative: the hostile value is refused and never handed to ssh.
        self.assertFalse(evidence["m5"].counted)
        self.assertIn("refused ssh destination", evidence["m5"].why)
        self.assertFalse(marker.exists())
        self.assertFalse(any("ProxyCommand" in line for line in argv), argv)
        # Positive: the plain alias is read, with "--" before it.
        self.assertTrue(evidence["m1"].counted, evidence["m1"].why)
        self.assertEqual(1, len(argv), argv)
        self.assertIn("-- ssh-m1 " + capacity_floor.PEER_STATUS_COMMAND, argv[0])


class CliTests(unittest.TestCase):
    """End-to-end through the process boundary: exit codes and stderr."""

    def run_guard(self, fixture: dict, *extra: str, profile: str = PROFILE,
                  supply: dict | None = None, peers: dict | None = None):
        # Every run gets a hermetic published supply and a fake ssh, so no test
        # ever reads the real fleet's peers.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh = root / "ghapp"
            gh.write_text(FAKE_GH, encoding="utf-8")
            gh.chmod(0o755)
            ssh = root / "ssh"
            ssh.write_text(FAKE_SSH, encoding="utf-8")
            ssh.chmod(0o755)
            fixture_path = root / "fixture.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            peers_path = root / "peers.json"
            peers_path.write_text(json.dumps(peers or {}), encoding="utf-8")
            supply_path = root / "advertised-labels.json"
            supply_path.write_text(json.dumps(supply if supply is not None else published()),
                                   encoding="utf-8")
            profile_path = root / "profile.toml"
            profile_path.write_text(profile, encoding="utf-8")
            env = os.environ.copy()
            env["FAKE_GH_FIXTURE"] = str(fixture_path)
            env["FAKE_SSH_FIXTURE"] = str(peers_path)
            return subprocess.run(
                [
                    sys.executable,
                    str(Path(capacity_floor.__file__)),
                    "check",
                    "--action", "drain",
                    "--config", str(profile_path),
                    "--gh-cli", str(gh),
                    "--timeout", "20",
                    "--published-supply", str(supply_path),
                    "--ssh", str(ssh),
                    "--peer-timeout", "20",
                    "--json",
                    *extra,
                ],
                capture_output=True, text=True, env=env, timeout=60,
            )

    @testing_support.requires_tomllib
    def test_last_serving_host_exits_three(self) -> None:
        proc = self.run_guard({REPO_ENDPOINT: {"runners": [runner(1, "studio-pulp-gate-01-612-7")]}})

        self.assertEqual(proc.returncode, capacity_floor.EXIT_LAST_SERVING_HOST, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["allowed"])
        self.assertEqual(payload["reason"], capacity_floor.REASON_LAST_SERVING_HOST)
        self.assertIn(GATE, payload["message"])

    @testing_support.requires_tomllib
    def test_idle_fleet_with_a_live_declaring_peer_exits_zero(self) -> None:
        proc = self.run_guard(
            {}, supply=published("m5", "m1"),
            peers={"ssh-m5": {"status": live_status()},
                   "ssh-m1": {"status": {**live_status(), "state": "off",
                                         "participating": False}}},
        )

        self.assertEqual(proc.returncode, capacity_floor.EXIT_OK, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["allowed"])
        self.assertFalse(payload["overridden"])
        self.assertEqual(payload["findings"][0]["remaining"], ["m5 (mints on demand)"])
        verdicts = {peer["host"]: peer["counted"] for peer in payload["findings"][0]["peers"]}
        self.assertEqual(verdicts, {"m1": False, "m5": True})

    @testing_support.requires_tomllib
    def test_idle_fleet_whose_declaring_peers_are_unproven_exits_three(self) -> None:
        proc = self.run_guard(
            {}, supply=published("m5", "m1"),
            peers={"ssh-m5": {"status": live_status(problems=[{"code": "heartbeat_stale"}])}},
        )

        self.assertEqual(proc.returncode, capacity_floor.EXIT_LAST_SERVING_HOST, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("m5 not counted (fleet problems", payload["message"])
        self.assertIn("m1 not counted (pool status unreadable via ssh-m1 (exit 255)",
                      payload["message"])

    @testing_support.requires_tomllib
    def test_an_unreadable_published_supply_counts_no_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "absent.json")
            proc = self.run_guard({}, "--published-supply", missing,
                                  peers={"ssh-m5": {"status": live_status()}})

        self.assertEqual(proc.returncode, capacity_floor.EXIT_LAST_SERVING_HOST, proc.stderr)
        self.assertIn("is unreadable, so no peer can be judged", json.loads(proc.stdout)["message"])

    @testing_support.requires_tomllib
    def test_peer_serving_the_label_exits_zero(self) -> None:
        proc = self.run_guard(
            {
                REPO_ENDPOINT: {
                    "runners": [runner(1, "studio-pulp-gate-01-612-7"), runner(2, "m5-pulp-gate-01-9-1")]
                }
            }
        )

        self.assertEqual(proc.returncode, capacity_floor.EXIT_OK, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["allowed"])

    @testing_support.requires_tomllib
    def test_peer_only_on_the_organization_exits_zero(self) -> None:
        proc = self.run_guard(
            {
                REPO_ENDPOINT: {"runners": [runner(1, "studio-pulp-gate-01-612-7")]},
                ORG_ENDPOINT: {"runners": [runner(9, "pulp-intel-macmini")]},
            }
        )

        self.assertEqual(proc.returncode, capacity_floor.EXIT_OK, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["findings"][0]["remaining"], ["pulp-intel-macmini"])

    @testing_support.requires_tomllib
    def test_unreachable_organization_scope_exits_four(self) -> None:
        proc = self.run_guard(
            {
                REPO_ENDPOINT: {"runners": [runner(1, "studio-pulp-gate-01-612-7")]},
                ORG_ENDPOINT: {"fail": "Resource not accessible by integration (HTTP 403)"},
            }
        )

        self.assertEqual(proc.returncode, capacity_floor.EXIT_INDETERMINATE, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["reason"], capacity_floor.REASON_CAPACITY_UNKNOWN)

    @testing_support.requires_tomllib
    def test_override_exits_zero_and_records_the_override(self) -> None:
        proc = self.run_guard(
            {REPO_ENDPOINT: {"runners": [runner(1, "studio-pulp-gate-01-612-7")]}},
            "--allow-last-serving-host",
        )

        self.assertEqual(proc.returncode, capacity_floor.EXIT_OK, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["allowed"])
        self.assertTrue(payload["overridden"])

    @testing_support.requires_tomllib
    def test_a_profile_declaring_no_gate_label_exits_zero(self) -> None:
        profile = textwrap.dedent(
            """\
            schema = 1
            [host]
            id = "studio"

            [[lane]]
            id = "spectr-gate"
            repo = "danielraffel/spectr"
            labels = ["self-hosted", "macOS"]
            """
        )

        proc = self.run_guard({}, profile=profile)

        self.assertEqual(proc.returncode, capacity_floor.EXIT_OK, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["allowed"])

    @testing_support.requires_tomllib
    def test_an_unnameable_persistent_service_exits_four(self) -> None:
        profile = PROFILE.replace(
            'home = "/Users/ci"',
            'home = "/Users/ci"\npersistent_runner_labels = ["not-an-actions-runner-label"]',
        )

        proc = self.run_guard(
            {REPO_ENDPOINT: {"runners": [runner(2, "m5-pulp-gate-01-9-1")]}}, profile=profile
        )

        self.assertEqual(proc.returncode, capacity_floor.EXIT_INDETERMINATE, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout)["reason"],
            capacity_floor.REASON_PERSISTENT_RUNNER_NAME_UNKNOWN,
        )


if __name__ == "__main__":
    unittest.main()
