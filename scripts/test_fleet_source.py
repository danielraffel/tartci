#!/usr/bin/env python3
import os
import json
import shutil
import tempfile
import unittest
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_source


PRIVATE_REPOSITORY = "ORG/private-fleet-authority"
PRIVATE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def configured_source(root, supply=None):
    """Write a valid private fleet source under root; return its environment.

    supply: extra published-supply keys (hosts, registrations). Shared by the
    consumer tests, so one place knows what a valid configured source is.
    """
    root = Path(root)
    (root / "profiles").mkdir(parents=True)
    (root / "profiles" / "canary-macos-fleet.toml").write_text("schema = 1\n")
    (root / "fleet").mkdir()
    published = root / "fleet" / "advertised-labels.json"
    digest = fleet_source.FleetSource(root, root / "profiles", published, True).profiles_sha256
    value = {"schema": "tartci.advertised-labels/v1", "registrations": []}
    value.update(supply or {})
    value["generated_from"] = {"repo": PRIVATE_REPOSITORY, "commit": PRIVATE_COMMIT,
                               "profiles": ["profiles/canary-macos-fleet.toml"],
                               "profiles_sha256": digest}
    published.write_text(json.dumps(value) + "\n")
    fleet_source.write_marker(root, PRIVATE_REPOSITORY, PRIVATE_COMMIT)
    return {"TARTCI_FLEET_SOURCE_ROOT": str(root),
            "TARTCI_FLEET_SOURCE_COMMIT": PRIVATE_COMMIT,
            "TARTCI_FLEET_SOURCE_REPOSITORY": PRIVATE_REPOSITORY}


class FleetSourceTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.pop("TARTCI_FLEET_SOURCE_ROOT", None)
        self._old_commit = os.environ.pop("TARTCI_FLEET_SOURCE_COMMIT", None)
        self._old_repository = os.environ.pop("TARTCI_FLEET_SOURCE_REPOSITORY", None)

    def tearDown(self):
        if self._old is not None:
            os.environ["TARTCI_FLEET_SOURCE_ROOT"] = self._old
        else:
            os.environ.pop("TARTCI_FLEET_SOURCE_ROOT", None)
        if self._old_commit is not None:
            os.environ["TARTCI_FLEET_SOURCE_COMMIT"] = self._old_commit
        else:
            os.environ.pop("TARTCI_FLEET_SOURCE_COMMIT", None)
        if self._old_repository is not None:
            os.environ["TARTCI_FLEET_SOURCE_REPOSITORY"] = self._old_repository
        else:
            os.environ.pop("TARTCI_FLEET_SOURCE_REPOSITORY", None)

    def test_default_uses_checkout_without_validation_override(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = fleet_source.resolve(root)
            self.assertEqual(root.resolve(), source.root)
            self.assertFalse(source.configured)

    def test_valid_source_remains_selectable_without_tomllib(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(fleet_source, "tomllib", None):
            root = Path(td)
            os.environ.update(configured_source(root))
            source = fleet_source.resolve(Path("/public"))
            self.assertEqual(root.resolve(), source.root)

    def test_recorded_source_resolves_without_inheriting_environment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = configured_source(root)
            recorded = {
                "root": str(root.resolve()),
                "configured": True,
                "commit": env["TARTCI_FLEET_SOURCE_COMMIT"],
                "repository": env["TARTCI_FLEET_SOURCE_REPOSITORY"],
                "tree_sha256": fleet_source.FleetSource(
                    root, root / "profiles", root / fleet_source.PUBLISHED_SUPPLY, True
                ).tree_sha256,
                "published_supply_sha256": fleet_source.FleetSource(
                    root, root / "profiles", root / fleet_source.PUBLISHED_SUPPLY, True
                ).published_supply_sha256,
                "profiles_dir": str(root / "profiles"),
                "published_supply": str(root / fleet_source.PUBLISHED_SUPPLY),
            }
            os.environ["TARTCI_FLEET_SOURCE_ROOT"] = "/wrong/source"
            source = fleet_source.resolve_recorded(recorded, Path("/public"))
            self.assertEqual(root.resolve(), source.root)
            self.assertEqual(recorded["commit"], source.source_commit)
            self.assertEqual(recorded["repository"], source.repository)
            self.assertEqual("/wrong/source", os.environ["TARTCI_FLEET_SOURCE_ROOT"])

    def test_explicit_source_requires_profiles_and_published_supply(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            os.environ["TARTCI_FLEET_SOURCE_ROOT"] = str(root)
            with self.assertRaisesRegex(ValueError, "profiles directory"):
                fleet_source.resolve(Path("/public"))

    def test_explicit_source_is_validated_and_selected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "profiles").mkdir()
            (root / "profiles" / "canary-macos-fleet.toml").write_text("schema = 1\n")
            (root / "fleet").mkdir()
            profile_digest = fleet_source.FleetSource(
                root, root / "profiles", root / "fleet" / "advertised-labels.json", True
            ).profiles_sha256
            (root / "fleet" / "advertised-labels.json").write_text(json.dumps({
                "schema": "tartci.advertised-labels/v1",
                "generated_from": {
                    "repo": "ORG/private-fleet-authority",
                    "commit": "0123456789abcdef0123456789abcdef01234567",
                    "profiles": ["profiles/canary-macos-fleet.toml"],
                    "profiles_sha256": profile_digest,
                },
                "registrations": [{}],
            }) + "\n")
            fleet_source.write_marker(
                root, "ORG/private-fleet-authority", "0123456789abcdef0123456789abcdef01234567")
            os.environ["TARTCI_FLEET_SOURCE_ROOT"] = str(root)
            os.environ["TARTCI_FLEET_SOURCE_COMMIT"] = "0123456789abcdef0123456789abcdef01234567"
            os.environ["TARTCI_FLEET_SOURCE_REPOSITORY"] = "ORG/private-fleet-authority"
            source = fleet_source.resolve(Path("/public"))
            self.assertTrue(source.configured)
            self.assertEqual(root.resolve(), source.root)
            self.assertEqual(root.resolve() / "profiles", source.profiles_dir)
            self.assertEqual(
                "0123456789abcdef0123456789abcdef01234567", source.source_commit)
            self.assertEqual("ORG/private-fleet-authority", source.repository)
            self.assertRegex(source.tree_sha256, r"^[0-9a-f]{64}$")
            self.assertRegex(source.published_supply_sha256, r"^[0-9a-f]{64}$")
            self.assertEqual({"tree_sha256", "profiles_sha256", "published_supply_sha256"},
                             set(source.provenance))

    def _exported(self, root, *, commit="a" * 40, generated_commit=None):
        (root / "profiles").mkdir()
        (root / "profiles" / "canary-macos-fleet.toml").write_text("schema = 1\n")
        (root / "fleet").mkdir()
        supply = root / "fleet" / "advertised-labels.json"
        profile_digest = fleet_source.FleetSource(
            root, root / "profiles", supply, True
        ).profiles_sha256
        supply.write_text(json.dumps({
            "schema": "tartci.advertised-labels/v1",
            "generated_from": {
                "repo": "danielraffel/tartci",
                "commit": generated_commit,
                "profiles": ["profiles/canary-macos-fleet.toml"],
                "profiles_sha256": profile_digest,
            },
            "registrations": [],
        }) + "\n")
        fleet_source.write_marker(root, "ORG/private-fleet-authority", commit)
        os.environ["TARTCI_FLEET_SOURCE_ROOT"] = str(root)
        os.environ["TARTCI_FLEET_SOURCE_COMMIT"] = commit
        os.environ["TARTCI_FLEET_SOURCE_REPOSITORY"] = "ORG/private-fleet-authority"
        return supply

    def test_the_pinned_commit_is_proved_by_the_marker_not_generated_from(self):
        # A file cannot name the commit that contains it, so generated_from
        # .commit and .repo may name anything; the export marker decides.
        with tempfile.TemporaryDirectory() as td:
            self._exported(Path(td), generated_commit="b" * 40)
            source = fleet_source.resolve(Path("/public"))
            self.assertEqual("a" * 40, source.source_commit)

    def test_explicit_source_rejects_repository_or_commit_provenance_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            self._exported(Path(td))
            os.environ["TARTCI_FLEET_SOURCE_COMMIT"] = "b" * 40
            with self.assertRaisesRegex(ValueError, "provenance.*not ORG/private-fleet-authority@b{40}"):
                fleet_source.resolve(Path("/public"))
            os.environ["TARTCI_FLEET_SOURCE_COMMIT"] = "a" * 40
            os.environ["TARTCI_FLEET_SOURCE_REPOSITORY"] = "ORG/other-fleet-authority"
            with self.assertRaisesRegex(ValueError, "provenance"):
                fleet_source.resolve(Path("/public"))

    def test_explicit_source_rejects_malformed_repository(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._exported(root)
            os.environ["TARTCI_FLEET_SOURCE_REPOSITORY"] = "not-a-repository"
            with self.assertRaisesRegex(ValueError, "OWNER/REPOSITORY"):
                fleet_source.resolve(Path("/public"))

    def test_a_source_without_its_export_marker_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._exported(root)
            (root / fleet_source.MARKER).unlink()
            with self.assertRaisesRegex(ValueError, "no readable"):
                fleet_source.resolve(Path("/public"))

    def test_non_utf8_marker_is_refused_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._exported(root)
            (root / fleet_source.MARKER).write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(ValueError, "no readable"):
                fleet_source.resolve(Path("/public"))

    def test_non_utf8_published_supply_is_refused_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            supply = self._exported(root)
            supply.write_bytes(b"\xff\xfe")
            fleet_source.write_marker(root, "ORG/private-fleet-authority", "a" * 40)
            with self.assertRaisesRegex(ValueError, "invalid published supply"):
                fleet_source.resolve(Path("/public"))

    def test_a_file_edited_after_export_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            supply = self._exported(root)
            value = json.loads(supply.read_text())
            value["registrations"] = [{"planted": True}]
            supply.write_text(json.dumps(value) + "\n")
            with self.assertRaisesRegex(ValueError, "advertised-labels.json changed after export"):
                fleet_source.resolve(Path("/public"))

    def test_a_profile_added_after_export_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._exported(root)
            (root / "profiles" / "extra-macos-fleet.toml").write_text("schema = 1\n")
            # profiles_sha256 would catch this too; recompute the supply so
            # only the marker can refuse it.
            supply = root / "fleet" / "advertised-labels.json"
            value = json.loads(supply.read_text())
            value["generated_from"]["profiles_sha256"] = fleet_source.FleetSource(
                root, root / "profiles", supply, True).profiles_sha256
            supply.write_text(json.dumps(value) + "\n")
            with self.assertRaisesRegex(ValueError, "profile list does not match|files differ"):
                fleet_source.resolve(Path("/public"))

    def test_profile_metadata_must_name_the_exact_exported_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            supply = self._exported(root)
            value = json.loads(supply.read_text())
            value["generated_from"]["profiles"].append("profiles/ghost-macos-fleet.toml")
            supply.write_text(json.dumps(value) + "\n")
            # Re-export the marker so the failure comes from metadata, not a
            # post-export edit of the published supply.
            fleet_source.write_marker(root, "ORG/private-fleet-authority", "a" * 40)
            with self.assertRaisesRegex(ValueError, "profile list does not match"):
                fleet_source.resolve(Path("/public"))

    def test_a_profile_that_is_not_toml_is_refused(self):
        if fleet_source.tomllib is None:
            self.skipTest("Python 3.9 validates immutable exports at sync time")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "profiles").mkdir(parents=True)
            (root / "profiles" / "canary-macos-fleet.toml").write_text("schema = [\n")
            (root / "fleet").mkdir()
            supply = root / "fleet" / "advertised-labels.json"
            digest = fleet_source.FleetSource(
                root, root / "profiles", supply, True).profiles_sha256
            supply.write_text(json.dumps({
                "schema": "tartci.advertised-labels/v1", "registrations": [],
                "generated_from": {
                    "repo": "ORG/private-fleet-authority", "commit": "a" * 40,
                    "profiles": ["profiles/canary-macos-fleet.toml"],
                    "profiles_sha256": digest,
                },
            }) + "\n")
            fleet_source.write_marker(root, "ORG/private-fleet-authority", "a" * 40)
            os.environ.update({
                "TARTCI_FLEET_SOURCE_ROOT": str(root),
                "TARTCI_FLEET_SOURCE_COMMIT": "a" * 40,
                "TARTCI_FLEET_SOURCE_REPOSITORY": "ORG/private-fleet-authority",
            })
            with self.assertRaisesRegex(ValueError, "profile is invalid"):
                fleet_source.resolve(Path("/public"))

    def test_symlinked_source_files_are_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            os.environ.update(configured_source(root))
            profile = root / "profiles" / "canary-macos-fleet.toml"
            profile.unlink()
            profile.symlink_to(Path(__file__))
            with self.assertRaisesRegex(ValueError, "symlinked source files"):
                fleet_source.resolve(Path("/public"))

    def test_symlinked_source_directories_are_refused(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as source_td:
            root = Path(td)
            source = Path(source_td)
            env = configured_source(source)
            os.environ.update(env)
            (root / "profiles").symlink_to(source / "profiles", target_is_directory=True)
            (root / "fleet").symlink_to(source / "fleet", target_is_directory=True)
            os.environ["TARTCI_FLEET_SOURCE_ROOT"] = str(root)
            with self.assertRaisesRegex(ValueError, "symlinked source files"):
                fleet_source.resolve(Path("/public"))

    def test_real_profile_resolves_without_tomllib(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(fleet_source, "tomllib", None):
            root = Path(td)
            profiles = root / "profiles"
            profiles.mkdir(parents=True)
            original = Path(__file__).resolve().parents[1] / "profiles" / "m3-macos-fleet.toml"
            shutil.copyfile(original, profiles / original.name)
            (root / "fleet").mkdir()
            supply = root / "fleet" / "advertised-labels.json"
            digest = fleet_source.FleetSource(root, profiles, supply, True).profiles_sha256
            supply.write_text(json.dumps({
                "schema": "tartci.advertised-labels/v1", "registrations": [],
                "generated_from": {"repo": PRIVATE_REPOSITORY, "commit": PRIVATE_COMMIT,
                                   "profiles": ["profiles/m3-macos-fleet.toml"],
                                   "profiles_sha256": digest},
            }) + "\n")
            fleet_source.write_marker(root, PRIVATE_REPOSITORY, PRIVATE_COMMIT)
            os.environ.update({"TARTCI_FLEET_SOURCE_ROOT": str(root),
                               "TARTCI_FLEET_SOURCE_COMMIT": PRIVATE_COMMIT,
                               "TARTCI_FLEET_SOURCE_REPOSITORY": PRIVATE_REPOSITORY})
            self.assertEqual(root.resolve(), fleet_source.resolve(Path("/public")).root)

    def test_blob_sha_matches_git(self):
        self.assertEqual("e69de29bb2d1d6434b8b29ae775ad8c2e48c5391", fleet_source.blob_sha(b""))
        self.assertEqual("ce013625030ba8dba906f756967f9e9ca394464a",
                         fleet_source.blob_sha(b"hello\n"))

class SelectedSupplyTests(unittest.TestCase):
    NAMES = ("TARTCI_FLEET_SOURCE_ROOT", "TARTCI_FLEET_SOURCE_COMMIT",
             "TARTCI_FLEET_SOURCE_REPOSITORY", "TARTCI_ADVERTISED_LABELS")

    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in self.NAMES}
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / "source"

    def tearDown(self):
        for name, value in self.saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
        self.td.cleanup()

    def test_nothing_configured_leaves_the_caller_its_default(self):
        self.assertIsNone(fleet_source.selected_published_supply())

    def test_a_named_supply_without_a_source_is_returned_as_is(self):
        os.environ["TARTCI_ADVERTISED_LABELS"] = "/somewhere/advertised-labels.json"
        self.assertEqual(Path("/somewhere/advertised-labels.json"),
                         fleet_source.selected_published_supply())

    def test_a_configured_source_is_validated_and_selected(self):
        os.environ.update(configured_source(self.root))
        self.assertEqual(self.root.resolve() / "fleet" / "advertised-labels.json",
                         fleet_source.selected_published_supply())
        os.environ["TARTCI_ADVERTISED_LABELS"] = str(self.root / "fleet" / "advertised-labels.json")
        self.assertEqual(self.root.resolve() / "fleet" / "advertised-labels.json",
                         fleet_source.selected_published_supply())

    def test_a_named_supply_that_disagrees_with_the_source_is_refused(self):
        os.environ.update(configured_source(self.root))
        os.environ["TARTCI_ADVERTISED_LABELS"] = "/public/fleet/advertised-labels.json"
        with self.assertRaisesRegex(ValueError, "configured fleet source publishes"):
            fleet_source.selected_published_supply()

    def test_an_invalid_source_is_refused_not_skipped(self):
        os.environ.update(configured_source(self.root))
        (self.root / "profiles" / "canary-macos-fleet.toml").write_text("schema = 2\n")
        with self.assertRaisesRegex(ValueError, "stale or incomplete"):
            fleet_source.selected_published_supply()


class SshDestinationTests(unittest.TestCase):
    """The one rule for a peer's SSH destination, shared with the profile validator."""

    def test_plain_aliases_and_user_at_host_are_accepted(self):
        for value in ("m1", "tartci-m5", "m5s", "ci@m5studio.local", "a" * 128):
            self.assertEqual(value, fleet_source.ssh_destination(value))

    def test_options_and_shell_text_are_refused(self):
        for value in ("-oProxyCommand=touch /tmp/x", "-p22", "--", "", " m1", "m1 id",
                      "m1;id", "m1\nid", "$(id)", "a" * 129, None, 7, ["m1"]):
            self.assertIsNone(fleet_source.ssh_destination(value), value)

    def test_a_configured_source_with_an_option_as_host_ssh_is_refused(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, configured_source(
                Path(td), {"hosts": [{"host_id": "m3", "ssh": "-oProxyCommand=touch /tmp/x"}]})):
            with self.assertRaisesRegex(ValueError, "invalid host ssh for 'm3'"):
                fleet_source.resolve(Path("/public"))

    def test_a_configured_source_with_valid_or_absent_host_ssh_resolves(self):
        hosts = [{"host_id": "m3", "ssh": "m3"}, {"host_id": "m5", "ssh": None},
                 {"host_id": "m1"}]
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
                os.environ, configured_source(Path(td), {"hosts": hosts})):
            self.assertTrue(fleet_source.resolve(Path("/public")).configured)

    def test_self_update_peers_refuse_a_hostile_configured_host_ssh(self):
        import fleet_self_update as su
        cfg = su.Config(home=Path("/nonexistent-home"))
        with tempfile.TemporaryDirectory() as td:
            hostile = {"hosts": [{"host_id": "m3", "ssh": "-oProxyCommand=touch /tmp/x"}]}
            with mock.patch.dict(os.environ, configured_source(Path(td) / "bad", hostile)):
                with self.assertRaisesRegex(ValueError, "invalid host ssh"):
                    su.published_peers(cfg, mock.Mock())
            good = {"hosts": [{"host_id": "m3", "ssh": "m3"}]}
            with mock.patch.dict(os.environ, configured_source(Path(td) / "good", good)):
                self.assertEqual({"m3": "m3"}, su.published_peers(cfg, mock.Mock()))

    def test_the_profile_validator_uses_the_same_pattern(self):
        source = (Path(__file__).resolve().parent / "macos_fleet_lanes.py").read_text()
        self.assertIn(f'r"{fleet_source.SSH_DESTINATION.pattern}", ssh', source)


if __name__ == "__main__":
    unittest.main()
