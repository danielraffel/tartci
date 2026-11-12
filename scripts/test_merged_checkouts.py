#!/usr/bin/env python3
"""merged_checkouts: which merged agent worktrees the reclaim pass removes.

Every fixture is a real git repository: a bare "GitHub" origin (reached
through a url.insteadOf rewrite, so the remote URL reads as github.com), a
parent clone outside the worktree root, and linked worktrees inside it. The
only fake is the one GraphQL read, which the tests hand a canned answer.
"""

from __future__ import annotations

import testing_support  # noqa: F401,E402
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import merged_checkouts as mc  # noqa: E402
import tmp_checkouts as tc  # noqa: E402

DAY = 86400
SLUG = "acme/widget"
URL = f"git@github.com:{SLUG}.git"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "GIT_ALLOW_PROTOCOL": "file"}


def git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "protocol.file.allow=always", "-C", str(cwd), *args],
                          check=True, capture_output=True, text=True, env=ENV).stdout


def age(path: pathlib.Path, days: float) -> None:
    """Backdate a checkout and everything the idle gate reads."""
    stamp = time.time() - days * DAY
    targets = [path, *path.iterdir()]
    dot_git = path / ".git"
    gitdir = dot_git if dot_git.is_dir() else pathlib.Path(
        dot_git.read_text().split("gitdir:", 1)[1].strip())
    targets += [gitdir / name for name in ("index", "HEAD", "logs/HEAD")
                if (gitdir / name).exists()]
    for target in targets:
        os.utime(target, (stamp, stamp), follow_symlinks=False)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = self.base / "agent-worktrees"
        self.root.mkdir()
        self.origin = self.base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
                       check=True)
        # Every repository here reaches the bare origin through the GitHub URL.
        self.gitconfig = self.base / "gitconfig"
        self.gitconfig.write_text(f'[url "{self.origin}"]\n\tinsteadOf = {URL}\n'
                                  "[protocol \"file\"]\n\tallow = always\n")
        self._env = mock_env(self, {"GIT_CONFIG_GLOBAL": str(self.gitconfig),
                                    "GIT_CONFIG_NOSYSTEM": "1"})
        self.seed = self.base / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.seed)], check=True)
        (self.seed / "a.txt").write_text("a\n")
        git(self.seed, "add", "a.txt")
        git(self.seed, "commit", "-qm", "seed")
        git(self.seed, "remote", "add", "origin", URL)
        git(self.seed, "push", "-q", "origin", "main")
        self.parent = self.base / "parent"
        git(self.base, "clone", "-q", URL, str(self.parent))
        self.graphql_calls: list[tuple[str, list]] = []
        self.pr_answer: dict[str, list] = {}

    # -- builders --
    def worktree(self, name: str, *, commit: bool = True, push: bool = True) -> pathlib.Path:
        path = self.root / name
        git(self.parent, "worktree", "add", "-q", "-b", name, str(path), "origin/main")
        if commit:
            (path / f"{name}.txt").write_text(name + "\n")
            git(path, "add", f"{name}.txt")
            git(path, "commit", "-qm", f"work on {name}")
        if push:
            git(path, "push", "-q", "-u", "origin", name)
        return path

    def merge(self, branch: str, number: int = 12) -> None:
        """Land `branch` on main with a merge commit, as GitHub's queue does."""
        git(self.seed, "fetch", "-q", "origin")
        git(self.seed, "merge", "-q", "--no-ff", "-m",
            f"Merge pull request #{number} from acme/{branch}", f"origin/{branch}")
        git(self.seed, "push", "-q", "origin", "main")

    def squash(self, branch: str) -> None:
        """Land `branch` as one new commit and delete the remote branch."""
        git(self.seed, "fetch", "-q", "origin")
        git(self.seed, "merge", "-q", "--squash", f"origin/{branch}")
        git(self.seed, "commit", "-qm", f"{branch} (#13)")
        git(self.seed, "push", "-q", "origin", "main")
        git(self.seed, "push", "-q", "origin", "--delete", branch)

    def head(self, path: pathlib.Path) -> str:
        return git(path, "rev-parse", "HEAD").strip()

    def proof(self, **kw) -> mc.Proof:
        kw.setdefault("commands", "")
        kw.setdefault("app_env", {})
        kw.setdefault("graphql", self.fake_graphql)
        kw.setdefault("roots", [self.root])
        kw.setdefault("cwd_reader", lambda: ["/"])
        return mc.Proof(**kw)

    def fake_graphql(self, slug: str, lookups: list) -> dict:
        self.graphql_calls.append((slug, lookups))
        return {item["branch"]: self.pr_answer.get(item["branch"], []) for item in lookups}

    def sweep(self, *, fix: bool = True, proof: mc.Proof | None = None,
              cwds: list[str] | None = None, in_use=None, days: float = 3) -> dict:
        for child in self.root.iterdir():
            if child.is_dir():
                age(child, days)
        proof = proof or self.proof(cwd_reader=lambda: list(cwds or ["/"]))
        in_use = in_use or (lambda path: False)
        report = tc.scan(self.root, fix=fix, idle_hours=24, in_use=in_use,
                         cwds=cwds or ["/"], merged=proof)
        tc.finish_pending(proof, {report["root"]: report}, fix=fix, in_use=in_use,
                          cwds=cwds or ["/"])
        if fix:
            mc.prune(proof, subprocess.run)
        self.last_proof = proof
        return report

    def lineage(self, branch: str, field: str) -> str:
        return subprocess.run(["git", "-C", str(self.parent), "config", "--get",
                               f"branch.{branch}.pulpWorktree{field}"],
                              capture_output=True, text=True).stdout.strip()


def mock_env(case: unittest.TestCase, values: dict[str, str]) -> None:
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    ENV.update(values)

    def restore() -> None:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
                ENV.pop(key, None)
            else:
                os.environ[key] = value
                ENV[key] = value
    case.addCleanup(restore)


class MergedRule(Fixture):
    def test_merged_and_clean_is_removed_through_git(self) -> None:
        wt = self.worktree("done")
        head = self.head(wt)
        self.merge("done")  # after the parent's last fetch: the pass must fetch
        report = self.sweep()
        self.assertEqual((report["removed"], report["kept"]), (1, {}), report)
        self.assertFalse(wt.exists())
        self.assertNotIn(str(wt), git(self.parent, "worktree", "list"))
        # The local branch ref is kept, at the exact head.
        self.assertEqual(git(self.parent, "rev-parse", "refs/heads/done").strip(), head)
        self.assertEqual(self.lineage("done", "Status"), "merged")
        self.assertEqual(self.lineage("done", "DurableSha"), head)
        self.assertEqual(self.lineage("done", "Pr"), f"https://github.com/{SLUG}/pull/12")
        self.assertEqual(self.last_proof.status["lineage_recorded"], 1)
        self.assertEqual(self.graphql_calls, [])  # ancestry needs no API read

    def test_dry_run_counts_and_removes_nothing(self) -> None:
        wt = self.worktree("done")
        self.merge("done")
        report = self.sweep(fix=False)
        self.assertEqual(report["removed"], 1)
        self.assertTrue(wt.exists())
        self.assertEqual(self.lineage("done", "Status"), "")

    def test_merged_but_dirty_is_kept_and_listed(self) -> None:
        tracked = self.worktree("edited")
        untracked = self.worktree("scratch")
        self.merge("edited", 20)
        self.merge("scratch", 21)
        (tracked / "a.txt").write_text("changed\n")
        (untracked / "notes.md").write_text("mine\n")
        report = self.sweep()
        self.assertEqual((report["removed"], report["kept"]), (0, {"dirty": 2}), report)
        self.assertTrue(tracked.exists() and untracked.exists())
        held = {entry["path"]: entry["reason"] for entry in self.last_proof.holding}
        self.assertEqual(held, {str(tracked): "dirty", str(untracked): "dirty"})

    def test_unmerged_is_kept_and_unpushed_is_listed(self) -> None:
        pushed = self.worktree("open-pr")
        local = self.worktree("local-only", push=False)
        report = self.sweep()
        self.assertEqual(report["removed"], 0, report)
        self.assertEqual(report["kept"], {"unmerged": 1, "unpushed": 1})
        self.assertTrue(pushed.exists() and local.exists())
        self.assertEqual([e["path"] for e in self.last_proof.holding], [str(local)])

    def test_active_lineage_is_kept(self) -> None:
        wt = self.worktree("owned")
        self.merge("owned")
        git(self.parent, "config", "branch.owned.pulpWorktreeStatus", "active")
        report = self.sweep()
        self.assertEqual(report["kept"], {"lineage_active": 1})
        self.assertTrue(wt.exists())

    def test_a_process_inside_or_naming_it_keeps_it(self) -> None:
        inside = self.worktree("cwd-inside")
        named = self.worktree("named")
        built = self.worktree("built")
        for name in ("cwd-inside", "named", "built"):
            self.merge(name, 30 + len(name))
        proof = self.proof(commands=f"/usr/bin/vim {named}/a.txt\n",
                           cwd_reader=lambda: ["/", str(inside / "sub")])
        report = self.sweep(proof=proof, cwds=["/", str(inside / "sub")],
                            in_use=lambda path: path == built)
        self.assertEqual((report["removed"], report["kept"]), (0, {"in_use": 3}), report)
        self.assertTrue(inside.exists() and named.exists() and built.exists())

    def test_an_unreadable_command_table_removes_nothing(self) -> None:
        wt = self.worktree("done")
        self.merge("done")
        report = self.sweep(proof=self.proof(commands=None))
        self.assertEqual(report["kept"], {"process_scan_unavailable": 1})
        self.assertTrue(wt.exists())

    def test_recently_used_is_kept(self) -> None:
        wt = self.worktree("fresh")
        self.merge("fresh")
        report = self.sweep(days=0.5)
        self.assertEqual(report["kept"], {"recent": 1})
        self.assertTrue(wt.exists())

    def test_a_stash_made_on_it_keeps_it(self) -> None:
        wt = self.worktree("parked")
        self.merge("parked")
        (wt / "a.txt").write_text("parked\n")
        git(wt, "stash", "-q")
        report = self.sweep()
        self.assertEqual(report["kept"], {"stash": 1})
        self.assertTrue(wt.exists())
        self.assertEqual(self.last_proof.holding[0]["reason"], "stash")

    def test_a_stash_on_its_detached_head_keeps_it(self) -> None:
        wt = self.root / "detached"
        git(self.parent, "worktree", "add", "-q", "--detach", str(wt), "origin/main")
        (wt / "a.txt").write_text("parked\n")
        git(wt, "stash", "-q")  # "WIP on (no branch)": only its base names it
        report = self.sweep()
        self.assertEqual(report["kept"], {"stash": 1}, report)
        self.assertTrue(wt.exists())

    def test_a_stash_labelled_with_its_branch_keeps_it_after_new_commits(self) -> None:
        wt = self.worktree("parked", push=False)
        (wt / "a.txt").write_text("parked\n")
        git(wt, "stash", "-q")
        (wt / "later.txt").write_text("later\n")
        git(wt, "add", "later.txt")
        git(wt, "commit", "-qm", "later")  # the stash base is no longer HEAD
        git(wt, "push", "-q", "-u", "origin", "parked")
        self.merge("parked")
        report = self.sweep()
        self.assertEqual(report["kept"], {"stash": 1}, report)
        self.assertTrue(wt.exists())

    def test_someone_elses_stash_does_not_keep_it(self) -> None:
        other = self.worktree("other", commit=False, push=False)
        wt = self.worktree("done")
        self.merge("done")
        (other / "a.txt").write_text("theirs\n")
        git(other, "stash", "-q")
        report = self.sweep()
        self.assertFalse(wt.exists(), report)
        self.assertTrue(other.exists())


class HiddenWork(Fixture):
    def test_a_commit_only_its_detached_reflog_reaches_keeps_it(self) -> None:
        wt = self.root / "detached"
        git(self.parent, "worktree", "add", "-q", "--detach", str(wt), "origin/main")
        (wt / "y.txt").write_text("y\n")
        git(wt, "add", "y.txt")
        git(wt, "commit", "-qm", "lost on a detached head")
        git(wt, "checkout", "-q", "--detach", "origin/main")
        report = self.sweep()
        self.assertEqual(report["kept"], {"reflog_only": 1}, report)
        self.assertTrue(wt.exists())

    def test_a_per_worktree_ref_keeps_it(self) -> None:
        wt = self.worktree("bisecting")
        self.merge("bisecting")
        git(wt, "update-ref", "refs/bisect/bad", "HEAD")
        report = self.sweep()
        self.assertEqual(report["kept"], {"worktree_refs": 1}, report)

    def test_an_edit_hidden_by_assume_unchanged_keeps_it(self) -> None:
        wt = self.worktree("hidden")
        self.merge("hidden")
        git(wt, "update-index", "--assume-unchanged", "a.txt")
        (wt / "a.txt").write_text("invisible to git status\n")
        report = self.sweep()
        self.assertEqual(report["kept"], {"hidden_changes": 1}, report)
        self.assertTrue(wt.exists())

    def _ignore(self, wt: pathlib.Path, pattern: str) -> None:
        exclude = pathlib.Path(git(wt, "rev-parse", "--git-path", "info/exclude").strip())
        if not exclude.is_absolute():
            exclude = wt / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as handle:
            handle.write(pattern + "\n")

    def test_ignored_notes_keep_it_but_build_output_does_not(self) -> None:
        notes = self.worktree("notes")
        built = self.worktree("built")
        self.merge("notes", 40)
        self.merge("built", 41)
        for wt in (notes, built):
            for pattern in ("/.private/", "/build/", "__pycache__/"):
                self._ignore(wt, pattern)
        (notes / ".private").mkdir()
        (notes / ".private" / "notes.md").write_text("mine\n")
        (built / "build" / "CMakeFiles").mkdir(parents=True)
        (built / "build" / "CMakeCache.txt").write_text("cache\n")
        (built / "tools" / "__pycache__").mkdir(parents=True)
        report = self.sweep()
        self.assertEqual((report["removed"], report["kept"]), (1, {"ignored_files": 1}), report)
        self.assertTrue(notes.exists())
        self.assertFalse(built.exists())

    def test_an_ignored_symlink_is_unlinked_and_its_target_survives(self) -> None:
        wt = self.worktree("linked")
        (wt / "external").mkdir()
        (wt / "external" / "README").write_text("tracked\n")  # as in Pulp
        git(wt, "add", "external/README")
        git(wt, "commit", "-qm", "external")
        git(wt, "push", "-q", "origin", "linked")
        self.merge("linked")
        cache = self.base / "fetchcontent-src" / "vst3sdk"
        cache.mkdir(parents=True)
        (cache / "keep.txt").write_text("shared\n")
        (wt / "external" / "vst3sdk").symlink_to(cache)
        self._ignore(wt, "/external/vst3sdk")
        report = self.sweep()
        self.assertEqual((report["removed"], report["kept"]), (1, {}), report)
        self.assertTrue((cache / "keep.txt").exists())

    def test_a_paused_rebase_keeps_it(self) -> None:
        wt = self.worktree("rebasing")
        self.merge("rebasing")
        gitdir = pathlib.Path(git(wt, "rev-parse", "--absolute-git-dir").strip())
        (gitdir / "rebase-merge").mkdir()
        (gitdir / "rebase-merge" / "git-rebase-todo").write_text("pick abc\n")
        report = self.sweep()
        self.assertEqual(report["kept"], {"operation_in_progress": 1}, report)
        self.assertTrue(wt.exists())

    def test_an_ignored_nested_clone_with_local_commits_keeps_it(self) -> None:
        wt = self.worktree("vendored")
        self.merge("vendored")
        self._ignore(wt, "/external-src/")
        git(self.base, "clone", "-q", URL, str(wt / "external-src"))
        nested = wt / "external-src"
        (nested / "patch.txt").write_text("local patch\n")
        git(nested, "add", "patch.txt")
        git(nested, "commit", "-qm", "never pushed")
        report = self.sweep()
        self.assertEqual(report["kept"], {"nested_repo_unpushed": 1}, report)
        self.assertTrue((nested / "patch.txt").exists())

    def test_an_ignored_nested_clone_that_is_pushed_goes_with_it(self) -> None:
        wt = self.worktree("vendored")
        self.merge("vendored")
        self._ignore(wt, "/external-src/")
        git(self.base, "clone", "-q", URL, str(wt / "external-src"))
        report = self.sweep()
        self.assertEqual((report["removed"], report["kept"]), (1, {}), report)

    def test_a_process_that_arrives_after_judgement_keeps_it(self) -> None:
        wt = self.worktree("resumed")
        self.merge("resumed")
        reads = []

        def reader() -> list[str]:
            reads.append(1)
            return ["/", str(wt)]  # an agent cd'd in while the pass ran
        report = self.sweep(proof=self.proof(cwd_reader=reader))
        self.assertEqual(report["kept"], {"in_use": 1}, report)
        self.assertTrue(wt.exists())
        self.assertTrue(reads)

    def test_a_superseded_lineage_record_is_left_alone(self) -> None:
        wt = self.worktree("moved-on")
        self.merge("moved-on")
        git(self.parent, "config", "branch.moved-on.pulpWorktreeStatus", "superseded")
        git(self.parent, "config", "branch.moved-on.pulpWorktreeSuccessor", "other-branch")
        report = self.sweep()
        self.assertFalse(wt.exists(), report)
        self.assertEqual(self.lineage("moved-on", "Status"), "superseded")
        self.assertEqual(self.lineage("moved-on", "Successor"), "other-branch")

    def test_a_clone_with_submodule_git_dirs_or_local_tags_is_kept(self) -> None:
        clone = self.base / "clone"
        git(self.base, "clone", "-q", URL, str(clone))
        self.assertIsNone(mc.clone_reason(clone, subprocess.run))
        git(clone, "tag", "local-only", "HEAD")
        (clone / "t.txt").write_text("t\n")
        git(clone, "add", "t.txt")
        git(clone, "commit", "-qm", "tagged locally")
        git(clone, "tag", "-f", "local-only", "HEAD")
        git(clone, "reset", "-q", "--hard", "origin/main")
        self.assertEqual(mc.clone_reason(clone, subprocess.run), "unpushed")
        git(clone, "tag", "-d", "local-only")
        (clone / ".git" / "modules" / "planning").mkdir(parents=True)
        self.assertEqual(mc.clone_reason(clone, subprocess.run), "clone_submodules")


class SquashMerged(Fixture):
    def test_pr_merged_for_this_exact_head_is_removed_when_clean(self) -> None:
        wt = self.worktree("squashed")
        head = self.head(wt)
        self.squash("squashed")
        self.pr_answer = {"squashed": [
            {"url": f"https://github.com/{SLUG}/pull/13", "state": "MERGED",
             "headRefOid": head}]}
        report = self.sweep()
        self.assertEqual((report["removed"], report["kept"]), (1, {}), report)
        self.assertFalse(wt.exists())
        self.assertEqual(self.lineage("squashed", "Pr"), f"https://github.com/{SLUG}/pull/13")
        self.assertEqual(len(self.graphql_calls), 1)
        self.assertEqual(self.graphql_calls[0][0], SLUG)

    def test_squash_merged_but_dirty_is_kept(self) -> None:
        wt = self.worktree("squashed")
        head = self.head(wt)
        self.squash("squashed")
        (wt / "a.txt").write_text("after merge\n")
        self.pr_answer = {"squashed": [{"url": "u", "state": "MERGED", "headRefOid": head}]}
        report = self.sweep()
        self.assertEqual(report["kept"], {"dirty": 1})
        self.assertTrue(wt.exists())

    def test_a_commit_after_the_merged_head_is_kept_and_listed(self) -> None:
        wt = self.worktree("squashed")
        merged_head = self.head(wt)
        self.squash("squashed")
        (wt / "later.txt").write_text("more\n")
        git(wt, "add", "later.txt")
        git(wt, "commit", "-qm", "unpushed follow-up")
        self.pr_answer = {"squashed": [{"url": "u", "state": "MERGED",
                                        "headRefOid": merged_head}]}
        report = self.sweep()
        self.assertEqual(report["kept"], {"unpushed": 1}, report)
        self.assertTrue(wt.exists())
        self.assertEqual([e["path"] for e in self.last_proof.holding], [str(wt)])

    def test_an_open_pr_is_not_proof(self) -> None:
        wt = self.worktree("open")
        self.pr_answer = {"open": [{"url": "u", "state": "OPEN", "headRefOid": self.head(wt)}]}
        report = self.sweep()
        self.assertEqual(report["kept"], {"unmerged": 1})
        self.assertTrue(wt.exists())

    def test_without_the_read_only_app_squash_proof_is_off(self) -> None:
        wt = self.worktree("squashed")
        head = self.head(wt)
        self.squash("squashed")
        self.pr_answer = {"squashed": [{"url": "u", "state": "MERGED", "headRefOid": head}]}
        report = self.sweep(proof=self.proof(app_env=None))
        self.assertEqual(report["kept"], {"pr_proof_unavailable": 1}, report)
        self.assertTrue(wt.exists())
        self.assertEqual(self.graphql_calls, [])

    def test_a_failed_read_keeps_everything(self) -> None:
        wt = self.worktree("squashed")
        self.squash("squashed")

        def broken(slug: str, lookups: list) -> dict:
            raise RuntimeError("rate limited")
        report = self.sweep(proof=self.proof(graphql=broken))
        self.assertEqual(report["kept"], {"pr_proof_unavailable": 1})
        self.assertTrue(wt.exists())
        self.assertIn("rate limited", self.last_proof.status["pr_lookup"])

    def test_one_read_per_pass_for_many_branches(self) -> None:
        paths = [self.worktree(f"sq{i}") for i in range(3)]
        heads = {f"sq{i}": self.head(p) for i, p in enumerate(paths)}
        for name in heads:
            self.squash(name)
        self.pr_answer = {name: [{"url": f"https://github.com/{SLUG}/pull/{i}",
                                  "state": "CLOSED", "headRefOid": head}]
                          for i, (name, head) in enumerate(heads.items())}
        report = self.sweep()
        self.assertEqual(report["removed"], 3, report)
        self.assertEqual(len(self.graphql_calls), 1)
        self.assertEqual(sorted(b["branch"] for b in self.graphql_calls[0][1]),
                         sorted(heads))

    def test_a_checkout_that_changes_while_waiting_is_kept(self) -> None:
        wt = self.worktree("squashed")
        head = self.head(wt)
        self.squash("squashed")
        self.pr_answer = {"squashed": [{"url": "u", "state": "MERGED", "headRefOid": head}]}
        proof = self.proof()
        age(wt, 3)
        report = tc.scan(self.root, fix=True, idle_hours=24, in_use=lambda p: False,
                         cwds=["/"], merged=proof)
        self.assertEqual(report["kept"], {"awaiting_pr_proof": 1})
        (wt / "a.txt").write_text("touched while the read was in flight\n")
        tc.finish_pending(proof, {report["root"]: report}, fix=True,
                          in_use=lambda p: False, cwds=["/"])
        self.assertEqual(report["kept"], {"changed_before_removal": 1})
        self.assertTrue(wt.exists())


class Submodules(Fixture):
    def setUp(self) -> None:
        super().setUp()
        sub_origin = self.base / "planning.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(sub_origin)],
                       check=True)
        sub_seed = self.base / "planning-seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(sub_seed)], check=True)
        (sub_seed / "p.md").write_text("plan\n")
        git(sub_seed, "add", "p.md")
        git(sub_seed, "commit", "-qm", "plan")
        git(sub_seed, "remote", "add", "origin", str(sub_origin))
        git(sub_seed, "push", "-q", "origin", "main")
        git(self.seed, "submodule", "add", "-q", str(sub_origin), "planning")
        git(self.seed, "commit", "-qm", "add planning")
        git(self.seed, "push", "-q", "origin", "main")
        git(self.parent, "pull", "-q")

    def populated(self, name: str) -> pathlib.Path:
        wt = self.worktree(name)
        git(wt, "submodule", "update", "-q", "--init")
        self.merge(name)
        return wt

    def test_a_clean_pushed_submodule_is_removed_with_one_force(self) -> None:
        wt = self.populated("with-planning")
        report = self.sweep()
        self.assertEqual((report["removed"], report["kept"]), (1, {}), report)
        self.assertFalse(wt.exists())
        self.assertEqual(self.last_proof.status["forced_submodule_removals"], 1)

    def test_an_unpushed_submodule_branch_keeps_it(self) -> None:
        wt = self.populated("planning-work")
        sub = wt / "planning"
        git(sub, "checkout", "-qb", "notes")
        (sub / "n.md").write_text("unpushed\n")
        git(sub, "add", "n.md")
        git(sub, "commit", "-qm", "unpushed planning")
        git(sub, "checkout", "-q", "--detach", "HEAD~1")
        report = self.sweep()
        self.assertEqual(report["kept"], {"submodule_unpushed": 1}, report)
        self.assertTrue(wt.exists())


class Prune(Fixture):
    def backdate_admin(self, name: str) -> None:
        admin = self.parent / ".git" / "worktrees" / name
        stamp = time.time() - 120 * DAY
        for entry in [admin, *admin.rglob("*")]:
            os.utime(entry, (stamp, stamp), follow_symlinks=False)

    def test_a_renamed_live_worktree_keeps_its_admin_dir(self) -> None:
        wt = self.worktree("done")
        self.merge("done")
        wip = self.worktree("wip", commit=False, push=False)
        (wip / "a.txt").write_text("uncommitted\n")
        renamed = self.root / "wip-renamed"
        wip.rename(renamed)
        self.sweep()
        self.assertFalse(wt.exists())
        status = subprocess.run(["git", "-C", str(renamed), "status", "--porcelain"],
                                capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("a.txt", status.stdout)

    def test_prune_runs_only_when_every_stale_entry_is_under_a_root(self) -> None:
        wt = self.worktree("done")
        self.merge("done")
        elsewhere = self.base / "unmounted-volume" / "wt"
        git(self.parent, "worktree", "add", "-q", "--detach", str(elsewhere), "origin/main")
        shutil.rmtree(elsewhere)  # like a worktree on a volume that is not mounted
        self.backdate_admin("wt")
        self.sweep()
        self.assertFalse(wt.exists())
        status = self.last_proof.status["pruned"]
        self.assertTrue(any(v.startswith("skipped") for v in status.values()), status)
        self.assertIn("prunable", git(self.parent, "worktree", "list", "--porcelain"))

    def test_stale_entries_under_the_root_are_pruned(self) -> None:
        wt = self.worktree("done")
        self.merge("done")
        gone = self.worktree("gone", commit=False, push=False)
        shutil.rmtree(gone)
        self.backdate_admin("gone")
        self.sweep()
        self.assertFalse(wt.exists())
        self.assertNotIn(str(gone), git(self.parent, "worktree", "list"))


class Settings(unittest.TestCase):
    TABLE = {"worktrees_root": "/w", "extra_worktree_roots": ["/c"]}

    def test_on_by_default_only_under_pressure(self) -> None:
        settings, _ = tc.settings_from_table(dict(self.TABLE), "t")
        plan, why = tc.merged_plan(settings, lambda path: False)
        self.assertEqual(plan, {})
        plan, why = tc.merged_plan(settings, lambda path: str(path) == "/w")
        self.assertEqual(plan, {"/w": False})
        self.assertEqual(why["roots"]["/w"], "volume under pressure")

    def test_the_profile_key_turns_it_on_always_or_off(self) -> None:
        settings, _ = tc.settings_from_table(dict(self.TABLE, merged_worktrees=True), "t")
        self.assertEqual(tc.merged_plan(settings, lambda p: False)[0],
                         {"/w": False, "/c": False})
        settings, _ = tc.settings_from_table(
            dict(self.TABLE, merged_worktrees=False, tmp_checkouts=True), "t")
        self.assertEqual(tc.merged_plan(settings, lambda p: True)[0], {})

    def test_clones_only_where_tmp_checkouts_already_allows_them(self) -> None:
        settings, _ = tc.settings_from_table(
            dict(self.TABLE, merged_worktrees=True, worktree_root_checkouts=True), "t")
        self.assertEqual(tc.merged_plan(settings, None)[0], {"/w": True, "/c": False})

    def test_idle_hours_are_bounded_at_a_day(self) -> None:
        for bad in (23, 0, 721, "24", True):
            self.assertTrue(mc.validate({"merged_worktree_idle_hours": bad}), bad)
        self.assertEqual(mc.validate({"merged_worktree_idle_hours": 24}), [])
        self.assertTrue(mc.validate({"merged_worktrees": "yes"}))

    def test_github_slug_parsing(self) -> None:
        for url in ("git@github.com:acme/widget.git", "https://github.com/acme/widget",
                    "ssh://git@github.com/acme/widget.git"):
            self.assertEqual(mc.github_slug(url), "acme/widget", url)
        for url in ("/srv/git/widget.git", "git@gitlab.com:acme/widget.git",
                    "https://github.com/acme/widget/extra"):
            self.assertIsNone(mc.github_slug(url), url)


class RunPass(Fixture):
    def test_run_finishes_pending_writes_the_report_and_counts(self) -> None:
        self.worktree("merged")
        self.merge("merged")
        held = self.worktree("held", push=False)
        for child in self.root.iterdir():
            age(child, 3)
        verdicts = self.base / "state" / "checkout-verdicts.json"
        report = tc.run(fix=True, profile=self.base / "absent.toml", in_use=lambda p: False,
                        verdicts=verdicts, pressured=lambda p: True,
                        table={"worktrees_root": str(self.root)}, proof=self.proof())
        merged = report["merged_worktrees"]
        self.assertTrue(merged["enabled"], merged)
        self.assertEqual((merged["removed"], merged["holding_work"]), (1, 1), merged)
        written = json.loads((verdicts.parent / mc.REPORT_FILE).read_text())
        self.assertEqual([e["path"] for e in written["checkouts"]], [str(held)])
        self.assertEqual(report["by_root"][str(self.root)]["removed"], 1)

    def test_no_pressure_and_no_key_scans_nothing(self) -> None:
        wt = self.worktree("merged")
        self.merge("merged")
        age(wt, 3)
        report = tc.run(fix=True, profile=self.base / "absent.toml", in_use=lambda p: False,
                        verdicts=self.base / "state" / "v.json", pressured=lambda p: False,
                        table={"worktrees_root": str(self.root)}, proof=self.proof())
        self.assertFalse(report["merged_worktrees"]["enabled"])
        self.assertEqual(report["checkouts"], 0)
        self.assertTrue(wt.exists())


if __name__ == "__main__":
    unittest.main()
