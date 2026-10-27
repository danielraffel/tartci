from __future__ import annotations

import datetime as dt
import hashlib
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleet_capacity as fc  # noqa: E402


def report(**overrides):
    value = {
        "schema": "tartci.gate-supply/v1",
        "host": "m3",
        "repo": "Generous-Corp/pulp",
        "class": "macos",
        "verdict": "ok",
        "pool": "on",
        "free": 1,
        "in_flight": 0,
        "occupied": 1,
        "lanes": [{"lane": "a"}, {"lane": "b"}],
        "profile_sha256": "a" * 64,
    }
    value.update(overrides)
    return value


class FleetCapacityTests(unittest.TestCase):
    def setUp(self):
        self.profile = "a" * 64
        self.when = dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc)

    def sample(self, **kwargs):
        return fc.build_sample(
            report=kwargs.pop("report", report()),
            repo="Generous-Corp/pulp",
            target="macos",
            host_id=kwargs.pop("host_id", "m3"),
            host_boot=kwargs.pop("host_boot", "boot-1"),
            generation=kwargs.pop("generation", "generation-1"),
            profile_sha256=kwargs.pop("profile_sha256", self.profile),
            observed_at=kwargs.pop("observed_at", self.when),
            **kwargs,
        )

    def test_verified_sample_has_canonical_id_and_capacity_invariants(self):
        value = self.sample()
        self.assertEqual(value["schema"], "tartci.fleet-capacity/v1")
        self.assertEqual(value["verification"], "verified")
        self.assertEqual(value["eligible_slots"], 2)
        self.assertEqual(value["free_slots"] + value["occupied_slots"] + value["in_flight_slots"], 2)
        self.assertNotEqual(value["evidence_sha256"], value["census_sha256"])
        self.assertEqual(value["sample_id"], fc.digest_bytes(fc.canonical_bytes(value, omit_sample_id=True)))
        self.assertEqual(fc.validate_sample(value), value)

    def test_unknown_report_never_becomes_zero_capacity(self):
        value = self.sample(report=report(verdict="unknown", reason="stale heartbeat", free=0, in_flight=0))
        self.assertEqual(value["verification"], "unknown")
        self.assertIsNone(value["eligible_slots"])
        self.assertGreaterEqual(value["unknown_slots"], 1)
        fc.validate_sample(value)

    def test_unavailable_inventory_never_becomes_verified_zero_capacity(self):
        value = self.sample(report=report(inventory="unavailable", free=0,
                                          reason=None))
        self.assertEqual(value["verification"], "unknown")
        self.assertEqual(value["reason"], "capacity inventory is unavailable")
        self.assertIsNone(value["eligible_slots"])
        fc.validate_sample(value)

    def test_unrecognized_inventory_never_becomes_verified(self):
        value = self.sample(report=report(inventory="unknown"))
        self.assertEqual(value["verification"], "unknown")
        self.assertEqual(value["reason"], "capacity inventory is unrecognized")
        self.assertIsNone(value["eligible_slots"])
        fc.validate_sample(value)

    def test_unknown_report_without_lane_census_uses_unknown_sentinel(self):
        value = self.sample(report=report(verdict="unknown", lanes=[], reason="launchd"))
        self.assertEqual((value["configured_slots"], value["unknown_slots"]), (0, 0))
        self.assertIsNone(value["eligible_slots"])
        fc.validate_sample(value)

    def test_canonical_bytes_matches_rust_json_for_utf8_float_and_large_integer(self):
        value = report(label="café", threshold=1e-6, large=9007199254740993)
        encoded = fc.canonical_bytes(value).decode()
        self.assertIn('"label":"café"', encoded)
        self.assertIn('"threshold":1e-6', encoded)
        self.assertIn('"large":9007199254740993', encoded)

    def test_evidence_identity_and_slot_bindings_are_checked(self):
        value = self.sample()
        value["evidence"]["free"] = 0
        value["evidence_sha256"] = fc.digest_bytes(fc.canonical_bytes(value["evidence"]))
        value["sample_id"] = fc.digest_bytes(fc.canonical_bytes(value, omit_sample_id=True))
        with self.assertRaisesRegex(ValueError, "evidence free"):
            fc.validate_sample(value)

        value = self.sample()
        value["evidence"]["inventory"] = "unavailable"
        value["evidence_sha256"] = fc.digest_bytes(fc.canonical_bytes(value["evidence"]))
        value["sample_id"] = fc.digest_bytes(fc.canonical_bytes(value, omit_sample_id=True))
        with self.assertRaisesRegex(ValueError, "available capacity inventory"):
            fc.validate_sample(value)

        value = self.sample()
        value["evidence"]["inventory"] = "unknown"
        value["evidence_sha256"] = fc.digest_bytes(fc.canonical_bytes(value["evidence"]))
        value["sample_id"] = fc.digest_bytes(fc.canonical_bytes(value, omit_sample_id=True))
        with self.assertRaisesRegex(ValueError, "recognized capacity inventory"):
            fc.validate_sample(value)

    def test_inconsistent_report_fails_closed_with_unknown_count(self):
        value = self.sample(report=report(free=99, in_flight=99, occupied=0))
        self.assertEqual(value["verification"], "unknown")
        self.assertEqual(value["unknown_slots"], value["configured_slots"])
        fc.validate_sample(value)

    def test_profile_digest_must_match_measured_report(self):
        with self.assertRaisesRegex(ValueError, "profile digest"):
            self.sample(profile_sha256="b" * 64)

    def test_evidence_is_reconstructible(self):
        value = self.sample()
        self.assertEqual(value["evidence_sha256"], fc.digest_bytes(fc.canonical_bytes(value["evidence"])))
        value["evidence"]["free"] = 0
        value["sample_id"] = fc.digest_bytes(fc.canonical_bytes(value, omit_sample_id=True))
        with self.assertRaisesRegex(ValueError, "evidence free"):
            fc.validate_sample(value)

    def test_pool_draining_is_unknown(self):
        value = self.sample(report=report(pool="draining"))
        self.assertEqual(value["verification"], "unknown")
        self.assertIsNone(value["free_slots"])

    def test_tamper_and_unknown_fields_refuse(self):
        value = self.sample()
        value["free_slots"] = 99
        with self.assertRaisesRegex(ValueError, "sample_id"):
            fc.validate_sample(value)
        value = self.sample()
        value["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "unknown"):
            fc.validate_sample(value)

    def test_inconsistent_verified_counts_refuse(self):
        value = self.sample()
        value["eligible_slots"] = 1
        value["sample_id"] = fc.digest_bytes(fc.canonical_bytes(value, omit_sample_id=True))
        with self.assertRaisesRegex(ValueError, "partition"):
            fc.validate_sample(value)

        value = self.sample()
        value["unknown_slots"] = 1
        value["sample_id"] = fc.digest_bytes(fc.canonical_bytes(value, omit_sample_id=True))
        with self.assertRaisesRegex(ValueError, "partition"):
            fc.validate_sample(value)

    def test_unknown_sample_cannot_carry_partial_counts(self):
        value = self.sample(report=report(verdict="unknown", reason="blocked"))
        value["free_slots"] = 0
        value["sample_id"] = fc.digest_bytes(fc.canonical_bytes(value, omit_sample_id=True))
        with self.assertRaisesRegex(ValueError, "unknown sample"):
            fc.validate_sample(value)

    def test_profile_and_identity_are_required(self):
        with self.assertRaises(ValueError):
            self.sample(host_boot="")
        with self.assertRaises(ValueError):
            self.sample(generation="")
        with self.assertRaises(ValueError):
            self.sample(profile_sha256="bad")

    def test_report_identity_is_bound_to_sample_identity(self):
        with self.assertRaisesRegex(ValueError, "identity"):
            self.sample(report=report(repo="other/repo"))
        with self.assertRaisesRegex(ValueError, "host_id"):
            self.sample(report=report(host="other-host"))

    def test_valid_until_is_bounded(self):
        value = self.sample(valid_for_seconds=60)
        self.assertEqual(value["valid_until"], "2026-10-26T00:01:00Z")
        value["valid_until"] = value["observed_at"]
        value["sample_id"] = fc.digest_bytes(fc.canonical_bytes(value, omit_sample_id=True))
        with self.assertRaisesRegex(ValueError, "valid_until"):
            fc.validate_sample(value)

        with self.assertRaisesRegex(ValueError, "between 1 and 3600"):
            self.sample(valid_for_seconds=fc.MAX_VALID_FOR_SECONDS + 1)

        value = self.sample(valid_for_seconds=60)
        observed = dt.datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        value["valid_until"] = fc.iso(observed + dt.timedelta(seconds=fc.MAX_VALID_FOR_SECONDS + 1))
        value["sample_id"] = fc.digest_bytes(fc.canonical_bytes(value, omit_sample_id=True))
        with self.assertRaisesRegex(ValueError, "exceeds 3600"):
            fc.validate_sample(value)

    def test_timestamps_are_timezone_aware_and_not_materially_future(self):
        value = self.sample()
        value["observed_at"] = "2026-10-26T00:00:00"
        value["sample_id"] = fc.digest_bytes(fc.canonical_bytes(value, omit_sample_id=True))
        with self.assertRaisesRegex(ValueError, "timezone"):
            fc.validate_sample(value)

        value = self.sample()
        future = fc.utc_now() + dt.timedelta(seconds=fc.MAX_OBSERVATION_FUTURE_SECONDS + 1)
        value["observed_at"] = fc.iso(future)
        value["valid_until"] = fc.iso(future + dt.timedelta(seconds=60))
        value["sample_id"] = fc.digest_bytes(fc.canonical_bytes(value, omit_sample_id=True))
        with self.assertRaisesRegex(ValueError, "future"):
            fc.validate_sample(value)

    def test_cli_emits_verified_json_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = pathlib.Path(directory) / "profile.toml"
            profile.write_text("[host]\nname = 'test'\n")
            output = io.StringIO()
            with mock.patch.object(fc.gate_supply, "host_report", return_value=report()), \
                 mock.patch.object(fc, "profile_digest", return_value="a" * 64), \
                 contextlib.redirect_stdout(output):
                result = fc.main([
                    "--repo", "Generous-Corp/pulp", "--target", "macos",
                    "--profile", str(profile), "--host-id", "m3",
                    "--host-boot-id", "boot-1", "--generation", "gen-1", "--json",
                ])
            self.assertEqual(result, 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value["verification"], "verified")
            self.assertEqual(value["repo"], "Generous-Corp/pulp")
            self.assertEqual(value["target"], "macos")
            fc.validate_sample(value)

    def test_cli_returns_unknown_for_unverified_supply(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = pathlib.Path(directory) / "profile.toml"
            profile.write_text("[host]\nname = 'test'\n")
            output = io.StringIO()
            with mock.patch.object(fc.gate_supply, "host_report",
                                   return_value=report(verdict="unknown", reason="stale")), \
                 mock.patch.object(fc, "profile_digest", return_value="a" * 64), \
                 contextlib.redirect_stdout(output):
                result = fc.main([
                    "--repo", "Generous-Corp/pulp", "--target", "macos",
                    "--profile", str(profile), "--host-id", "m3",
                    "--host-boot-id", "boot-1", "--generation", "gen-1", "--json",
                ])
            self.assertEqual(result, 2)
            value = json.loads(output.getvalue())
            self.assertEqual(value["verification"], "unknown")
            self.assertIsNone(value["eligible_slots"])
            fc.validate_sample(value)

    def test_cli_emits_importable_unknown_when_report_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = pathlib.Path(directory) / "profile.toml"
            profile.write_text("[host]\nname = 'test'\n")
            output = io.StringIO()
            with mock.patch.object(fc.gate_supply, "host_report",
                                   side_effect=RuntimeError("launchd unavailable")), \
                 contextlib.redirect_stdout(output):
                result = fc.main([
                    "--repo", "Generous-Corp/pulp", "--target", "macos",
                    "--profile", str(profile), "--host-id", "m3",
                    "--host-boot-id", "boot-1", "--generation", "gen-1", "--json",
                ])
            self.assertEqual(result, 2)
            value = json.loads(output.getvalue())
            self.assertEqual(value["verification"], "unknown")
            self.assertEqual(value["configured_slots"], 0)
            fc.validate_sample(value)

    def test_cli_emits_importable_unknown_when_profile_changes_during_report(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = pathlib.Path(directory) / "profile.toml"
            profile.write_text("[host]\nname = 'test'\n")
            output = io.StringIO()
            changed = report(profile_sha256=None, verdict="unknown", reason="profile changed")
            with mock.patch.object(fc.gate_supply, "host_report", return_value=changed), \
                 contextlib.redirect_stdout(output):
                result = fc.main([
                    "--repo", "Generous-Corp/pulp", "--target", "macos",
                    "--profile", str(profile), "--host-id", "m3",
                    "--host-boot-id", "boot-1", "--generation", "gen-1", "--json",
                ])
            self.assertEqual(result, 2)
            value = json.loads(output.getvalue())
            self.assertEqual(value["verification"], "unknown")
            fc.validate_sample(value)

    def test_cli_emits_importable_unknown_when_ok_report_profile_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = pathlib.Path(directory) / "profile.toml"
            profile.write_text("[host]\nname = 'test'\n")
            output = io.StringIO()
            changed = report(profile_sha256="b" * 64)
            with mock.patch.object(fc.gate_supply, "host_report", return_value=changed), \
                 mock.patch.object(fc, "profile_digest", return_value="a" * 64), \
                 contextlib.redirect_stdout(output):
                result = fc.main([
                    "--repo", "Generous-Corp/pulp", "--target", "macos",
                    "--profile", str(profile), "--host-id", "m3",
                    "--host-boot-id", "boot-1", "--generation", "gen-1", "--json",
                ])
            self.assertEqual(result, 2)
            value = json.loads(output.getvalue())
            self.assertEqual(value["verification"], "unknown")
            self.assertEqual(value["reason"], "profile changed during capacity observation")
            fc.validate_sample(value)


if __name__ == "__main__":
    unittest.main()
