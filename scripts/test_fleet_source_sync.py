#!/usr/bin/env python3
"""Exporting a private fleet source at one commit through the GitHub API."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_source
import fleet_source_sync as sync

PRIVATE = "ORG/private-fleet-authority"
COMMIT = "c" * 40
TREE = "d" * 40


def source_files(profiles: dict[str, str]) -> dict[str, bytes]:
    """Profiles plus a published supply whose profile digest matches them."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "profiles").mkdir()
        for name, text in profiles.items():
            (root / "profiles" / name).write_text(text)
        supply = root / fleet_source.PUBLISHED_SUPPLY
        digest = fleet_source.FleetSource(root, root / "profiles", supply, True).profiles_sha256
    files = {f"profiles/{name}": text.encode() for name, text in profiles.items()}
    files[fleet_source.PUBLISHED_SUPPLY] = (json.dumps({
        "schema": "tartci.advertised-labels/v1",
        "generated_from": {"repo": PRIVATE, "commit": None,
                           "profiles": sorted(f"profiles/{name}" for name in profiles),
                           "profiles_sha256": digest},
        "registrations": [],
    }, indent=2) + "\n").encode()
    return files


class FakeRepository:
    """The GitHub API reads of one private repository at one commit."""

    def __init__(self, files: dict[str, bytes], *, repository: str = PRIVATE,
                 commit: str = COMMIT, tree: str = TREE) -> None:
        self.repository, self.commit, self.tree = repository, commit, tree
        self.files = dict(files)
        self.calls: list[str] = []
        self.truncated = False
        self.unreadable = False
        self.corrupt: set[str] = set()   # paths whose served bytes differ from the tree
        self.extra: list[dict] = []        # raw tree entries served as listed
        self.ancestors: set[str] = set()   # other commits main contains
        self.off_main: set[str] = set()    # commits held, but not on main
        self.compare_fails = False         # the compare read itself fails

    def entries(self) -> list[dict]:
        out = [{"path": "README.md", "type": "blob",
                "sha": fleet_source.blob_sha(b"not part of the source\n")},
               {"path": "profiles", "type": "tree", "sha": "e" * 40}]
        out += [{"path": path, "type": "blob", "sha": fleet_source.blob_sha(data)}
                for path, data in sorted(self.files.items())]
        return out + self.extra

    def __call__(self, path: str) -> tuple[int, str, str]:
        self.calls.append(path)
        if self.unreadable:
            return 1, "", "HTTP 404: Not Found"
        base = f"repos/{self.repository}"
        if path in (f"{base}/commits/main", f"{base}/commits/{self.commit}"):
            return 0, json.dumps({"sha": self.commit,
                                  "commit": {"tree": {"sha": self.tree}}}), ""
        compare = re.fullmatch(re.escape(base) + r"/compare/([0-9a-f]{40})\.\.\.main\?per_page=1",
                               path)
        if compare:
            asked = compare.group(1)
            if self.compare_fails:
                return 1, "", "error connecting to api.github.com"
            if asked == self.commit:
                status, ahead, behind, merge_base = "identical", 0, 0, asked
            elif asked in self.ancestors:
                status, ahead, behind, merge_base = "ahead", 1, 0, asked
            elif asked in self.off_main:
                status, ahead, behind, merge_base = "diverged", 1, 1, "f" * 40
            else:
                return 1, "", f"HTTP 404: {path}"
            return 0, json.dumps({"status": status, "ahead_by": ahead, "behind_by": behind,
                                  "merge_base_commit": {"sha": merge_base}}), ""
        if path == f"{base}/git/trees/{self.tree}?recursive=1":
            return 0, json.dumps({"tree": self.entries(), "truncated": self.truncated}), ""
        for item, data in self.files.items():
            if path == f"{base}/git/blobs/{fleet_source.blob_sha(data)}":
                served = data + b"tampered\n" if item in self.corrupt else data
                return 0, json.dumps({"encoding": "base64",
                                      "content": base64.b64encode(served).decode()}), ""
        return 1, "", f"HTTP 404: {path}"


PROFILES = {"canary-macos-fleet.toml": "schema = 1\nname = \"canary\"\n",
            "other-macos-fleet.toml": "schema = 1\nname = \"other\"\n"}


class ExportWithoutTomllibTests(unittest.TestCase):
    """Runs on every interpreter, the hosts' Python 3.9 included: without
    tomllib the export refuses and publishes nothing. On 3.11+ tomllib is taken
    away for the test; on 3.9 it is already absent, and the test says so."""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.store = Path(self.td.name) / "fleet-sources"
        self.repo = FakeRepository(source_files(PROFILES))

    def test_an_interpreter_without_tomllib_refuses_and_publishes_nothing(self) -> None:
        from unittest import mock  # noqa: PLC0415
        native = fleet_source.tomllib is None
        if sys.version_info < (3, 11):
            self.assertTrue(native, "a 3.9 interpreter has no tomllib")
        with mock.patch.object(fleet_source, "tomllib", None):
            commit, tree = sync.head(self.repo, PRIVATE)
            with self.assertRaisesRegex(sync.SyncError, "requires Python 3.11"):
                sync.export(self.repo, PRIVATE, commit, tree, self.store)
        self.assertFalse((self.store / COMMIT).exists(), "no export published")
        self.assertEqual([], [p.name for p in self.store.iterdir()], "no staging left behind")

    def test_the_same_export_succeeds_where_tomllib_is_present(self) -> None:
        # Control, same fixture: the refusal above is the missing parser.
        if fleet_source.tomllib is None:
            self.skipTest("3.11-only control: this interpreter has no tomllib")
        commit, tree = sync.head(self.repo, PRIVATE)
        self.assertEqual(self.store / COMMIT,
                         sync.export(self.repo, PRIVATE, commit, tree, self.store))


@unittest.skipUnless(fleet_source.tomllib is not None,
                     "source export requires the pinned Python 3.11+ TOML parser")
class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.store = Path(self.td.name) / "fleet-sources"
        self.repo = FakeRepository(source_files(PROFILES))
        self._env = {name: os.environ.pop(name, None) for name in (
            "TARTCI_FLEET_SOURCE_ROOT", "TARTCI_FLEET_SOURCE_COMMIT",
            "TARTCI_FLEET_SOURCE_REPOSITORY")}

    def tearDown(self) -> None:
        for name, value in self._env.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
        for current, dirs, _files in os.walk(self.td.name):
            for name in dirs:
                os.chmod(os.path.join(current, name), 0o755)
        self.td.cleanup()

    def export(self) -> Path:
        commit, tree = sync.head(self.repo, PRIVATE)
        return sync.export(self.repo, PRIVATE, commit, tree, self.store)

    def test_export_holds_exactly_the_source_files_and_resolves(self) -> None:
        root = self.export()
        self.assertEqual(self.store / COMMIT, root)
        self.assertEqual(sorted(self.repo.files), fleet_source.source_files(root))
        self.assertFalse((root / "README.md").exists())
        for path, data in self.repo.files.items():
            self.assertEqual(data, (root / path).read_bytes())
        os.environ.update({"TARTCI_FLEET_SOURCE_ROOT": str(root),
                           "TARTCI_FLEET_SOURCE_COMMIT": COMMIT,
                           "TARTCI_FLEET_SOURCE_REPOSITORY": PRIVATE})
        source = fleet_source.resolve(Path("/public"))
        self.assertEqual(root.resolve(), source.root)
        self.assertEqual(COMMIT, source.source_commit)

    def test_export_is_read_only(self) -> None:
        root = self.export()
        with self.assertRaises(PermissionError):
            (root / fleet_source.PUBLISHED_SUPPLY).write_text("{}")
        with self.assertRaises(PermissionError):
            (root / "planted").write_text("x")

    def test_an_intact_export_is_reused_without_reading_blobs(self) -> None:
        self.export()
        self.repo.calls.clear()
        self.export()
        self.assertEqual([f"repos/{PRIVATE}/commits/main"], self.repo.calls)

    def test_a_damaged_export_is_replaced(self) -> None:
        root = self.export()
        supply = root / fleet_source.PUBLISHED_SUPPLY
        os.chmod(supply, 0o644)
        os.chmod(supply.parent, 0o755)
        supply.write_text("{}\n")
        self.assertIsNotNone(fleet_source.marker_problem(root, PRIVATE, COMMIT))
        again = self.export()
        self.assertIsNone(fleet_source.marker_problem(again, PRIVATE, COMMIT))
        self.assertEqual(self.repo.files[fleet_source.PUBLISHED_SUPPLY], supply.read_bytes())

    def test_bytes_that_do_not_match_their_blob_id_are_refused(self) -> None:
        self.repo.corrupt.add("profiles/canary-macos-fleet.toml")
        with self.assertRaisesRegex(sync.SyncError, "does not match blob"):
            self.export()
        self.assertFalse((self.store / COMMIT).exists())
        self.assertEqual([], [p for p in self.store.iterdir()])  # no staging left behind

    def test_invalid_profile_is_refused_before_export_is_published(self) -> None:
        self.repo.files["profiles/canary-macos-fleet.toml"] = b"schema = [\n"
        with self.assertRaisesRegex(sync.SyncError, "exported profile is invalid"):
            self.export()
        self.assertFalse((self.store / COMMIT).exists())

    def test_only_profiles_and_the_supply_are_written_never_outside_the_export(self) -> None:
        planted = b"planted\n"
        self.repo.files["profiles/../../escape-macos-fleet.toml"] = planted
        self.repo.files["profiles/nested/inner-macos-fleet.toml"] = planted
        self.repo.files["fleet/../profiles/x-macos-fleet.toml"] = planted
        root = self.export()
        self.assertEqual(sorted(path for path in self.repo.files if path.count("/") == 1
                                and ".." not in path),
                         fleet_source.source_files(root))
        written = [p for p in Path(self.td.name).rglob("*") if p.is_file()]
        self.assertFalse(any(p.read_bytes() == planted for p in written), written)
        self.assertTrue(all(root in p.parents for p in written), written)

    def test_a_tree_entry_without_a_blob_id_is_refused_before_any_read(self) -> None:
        for sha in ("../../repos/other/contents/x", "", None):
            with self.subTest(sha=sha):
                self.repo.extra = [{"path": "profiles/odd-macos-fleet.toml", "type": "blob",
                                    "sha": sha}]
                self.repo.calls.clear()
                with self.assertRaisesRegex(sync.SyncError, "no blob id"):
                    self.export()
                self.assertFalse(any(str(sha) in call for call in self.repo.calls if sha))

    def test_a_truncated_tree_is_refused(self) -> None:
        self.repo.truncated = True
        with self.assertRaisesRegex(sync.SyncError, "truncated"):
            self.export()

    def test_a_source_without_its_published_supply_is_refused(self) -> None:
        del self.repo.files[fleet_source.PUBLISHED_SUPPLY]
        with self.assertRaisesRegex(sync.SyncError, "no fleet/advertised-labels.json"):
            self.export()

    def test_a_source_without_profiles_is_refused(self) -> None:
        self.repo.files = {fleet_source.PUBLISHED_SUPPLY: self.repo.files[fleet_source.PUBLISHED_SUPPLY]}
        with self.assertRaisesRegex(sync.SyncError, "no profiles"):
            self.export()

    def test_an_unreadable_repository_is_refused(self) -> None:
        self.repo.unreadable = True
        with self.assertRaisesRegex(sync.SyncError, "404"):
            self.export()

    def test_a_commit_asked_for_by_id_must_be_the_commit_answered(self) -> None:
        other = {"sha": "d" * 40, "commit": {"tree": {"sha": "e" * 40}}}
        on_main = {"status": "identical", "behind_by": 0, "merge_base_commit": {"sha": "d" * 40}}
        api = lambda path: (0, json.dumps(on_main if "/compare/" in path else other), "")
        with self.assertRaisesRegex(sync.SyncError, "answered dddddddddddd"):
            sync.head(api, PRIVATE, "c" * 40)
        self.assertEqual(("d" * 40, "e" * 40), sync.head(api, PRIVATE, "main"))
        self.assertEqual(("d" * 40, "e" * 40), sync.head(api, PRIVATE, "d" * 40))

    def test_a_commit_asked_for_by_id_must_be_on_main(self) -> None:
        # Positive: main's head, and an ancestor of it.
        self.assertEqual((COMMIT, TREE), sync.head(self.repo, PRIVATE, COMMIT))
        self.assertIn(f"repos/{PRIVATE}/compare/{COMMIT}...main?per_page=1", self.repo.calls)
        ancestor = "a" * 40
        self.repo.ancestors = {ancestor}
        sync.require_on_main(self.repo, PRIVATE, ancestor)
        # Negative: a commit on an unmerged branch, and one GitHub does not know.
        self.repo.off_main = {"b" * 40}
        with self.assertRaisesRegex(sync.NotOnMain, "is not on main.*staying on the installed"):
            sync.require_on_main(self.repo, PRIVATE, "b" * 40)
        with self.assertRaisesRegex(sync.SyncError, "HTTP 404"):
            sync.require_on_main(self.repo, PRIVATE, "9" * 40)

    def test_an_unreadable_compare_refuses_the_commit(self) -> None:
        self.repo.compare_fails = True
        with self.assertRaisesRegex(sync.SyncError, "error connecting"):
            sync.head(self.repo, PRIVATE, COMMIT)
        # main's own head needs no ancestry proof, so the canary still reads it.
        self.assertEqual((COMMIT, TREE), sync.head(self.repo, PRIVATE))

    def test_a_compare_answer_that_does_not_prove_ancestry_refuses(self) -> None:
        commit = {"sha": COMMIT, "commit": {"tree": {"sha": TREE}}}
        for answer in ({"status": "ahead", "behind_by": 2, "merge_base_commit": {"sha": COMMIT}},
                       {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": "f" * 40}},
                       {"status": "behind", "behind_by": 0, "merge_base_commit": {"sha": COMMIT}},
                       {"status": "ahead"}):
            api = lambda path, a=answer: (0, json.dumps(a if "/compare/" in path else commit), "")
            with self.assertRaises(sync.NotOnMain, msg=answer):
                sync.head(api, PRIVATE, COMMIT)

    def test_a_malformed_repository_name_is_refused_before_any_read(self) -> None:
        with self.assertRaisesRegex(sync.SyncError, "OWNER/REPO"):
            sync.head(self.repo, "../etc")
        self.assertEqual([], self.repo.calls)

    def test_prune_keeps_the_newest_and_every_named_commit(self) -> None:
        commits = [f"{i:x}" * 40 for i in range(1, 7)]
        for age, commit in enumerate(commits):
            path = self.store / commit
            path.mkdir(parents=True)
            os.utime(path, (1000 - age, 1000 - age))   # commits[0] is newest
        staging_old = self.store / (sync.STAGING_PREFIX + "old")
        staging_new = self.store / (sync.STAGING_PREFIX + "new")
        staging_old.mkdir()
        staging_new.mkdir()
        os.utime(staging_old, (0, 0))
        os.utime(staging_new, (5000, 5000))
        removed = sync.prune(self.store, {commits[5]}, keep=3, now=5000)
        left = sorted(path.name for path in self.store.iterdir())
        self.assertEqual(sorted([*commits[:3], commits[5], staging_new.name]), left)
        self.assertEqual(sorted([commits[3], commits[4], staging_old.name]), sorted(removed))


if __name__ == "__main__":
    unittest.main()
