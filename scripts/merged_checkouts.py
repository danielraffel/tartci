#!/usr/bin/env python3
"""Remove agent worktrees from the worktree roots once their work has merged.

Why this exists: agents finish a branch, the PR merges, and the worktree and
its build output stay. On 2026-11-09 m3's worktree volume held about 2,346
registered worktrees and 1,384 checkouts and its VM volume reached 92.8%; m5's
reached 92.6%. Both hosts dropped out of fleet_ready and deferred VMs. The
reclaim pass already walked the worktree roots (tmp_checkouts.py), but it asked
"is HEAD pushed?" with a `git fetch` per checkout, which reached ~250 of 1,497
checkouts a pass, and it never asked whether the work had LANDED.

This module is the "merged" rule tmp_checkouts.scan applies to a worktree root
while it is on. A checkout is removed only when every gate holds:

  1. merged: HEAD is an ancestor of the GitHub remote's `main`, fetched once per
     repository per pass; or, for a branch that landed squashed (HEAD is not an
     ancestor), a pull request for that branch is MERGED or CLOSED with
     `headRefOid` equal to HEAD exactly. The PR states come from ONE batched
     GraphQL read per pass, made through the read-only scan App
     (scripts/gh-scan-app, `[github_scan_app]` in the fleet profile). With no
     App configured there is no PR proof and squash-landed checkouts stay.
  2. clean: `git status --porcelain --untracked-files=normal
     --ignore-submodules=none` is empty (untracked files of any kind count;
     git's own `worktree remove` refuses them too); no assume-unchanged or
     skip-worktree entry hides an edit; every IGNORED path is build output or a
     tool cache (a CMake tree with its marker, target/, node_modules/,
     __pycache__ ...) or a nested clone that is itself clean and fully pushed;
     no stash entry names its branch or was made on its HEAD; and no commit is
     reachable only from the worktree's own HEAD reflog or per-worktree refs
     (refs/worktree/*, refs/bisect/*), which removal deletes.
  3. not in use: no process cwd is inside it, no running command line (any
     command, not only builds) names it, and no `.pulp-build-active` marker
     names a live pid. These are tmp_checkouts' gates plus a full `ps` scan.
  4. lineage is not `active` (Pulp's tools/scripts/worktree_lineage.sh keys,
     `branch.<b>.pulpWorktreeStatus` in the shared git config).
  5. idle for `merged_worktree_idle_hours` (default 24, never less).

A linked worktree is removed with `git worktree remove` and never with a
recursive delete. git refuses a worktree that contains initialized submodules
(Pulp's `planning`); such a worktree is removed with a single `--force` only
when every populated submodule is itself clean, has no stash and has no commit
its remotes lack. A locked worktree still needs a second --force, so it stays.
The local branch ref is kept, the branch's lineage is recorded `merged` (with
the PR URL when one is known: from the merge commit on main, or from the
GraphQL read; a superseded or archived record is left as written), and stale
worktree entries older than git gc's own 3-month expiry are pruned when every
one of them lies under a root that exists. Every in-use gate is asked again
against a fresh process table immediately before each removal.

A plain clone is eligible only where tmp_checkouts already allows clones (the
worktree root with `worktree_root_checkouts = true`); there it must pass
tmp_checkouts' every-branch-pushed check AND have a merged HEAD. git has no
command that removes a clone, so that path is tmp_checkouts' existing one.

A checkout kept because it holds work nobody else has (dirty, a stash, or
commits on no remote and in no PR) is never removed and is listed in
`merged-worktrees-report.json` beside the reclaim receipt.

Profile keys, all optional, in `[reclaim]`:

    merged_worktrees = true          # every pass; false: never; absent: only
                                     # while the root's volume is under pressure
    merged_worktree_idle_hours = 24  # 24..720

The roots are `worktrees_root` and `extra_worktree_roots`. A root not already
opted into tmp_checkouts is scanned worktrees-only.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import time
from typing import Any, Callable

Runner = Callable[..., subprocess.CompletedProcess]

DEFAULT_IDLE_HOURS = 24
MIN_IDLE_HOURS = 24
MAX_IDLE_HOURS = 720
GIT_TIMEOUT_S = 120
FETCH_TIMEOUT_S = 180
GRAPHQL_TIMEOUT_S = 60
# One GraphQL request per pass, holding at most this many branch lookups.
# Squash-landed checkouts past the cap wait for a later pass.
PR_LOOKUP_LIMIT = 80
MERGE_TABLE_LIMIT = 20000
LIST_LIMIT = 20
REPORT_FILE = "merged-worktrees-report.json"
# Kept for these reasons a checkout holds work no remote has.
HOLDS_WORK = frozenset({"dirty", "stash", "unpushed", "submodule_unpushed", "hidden_changes",
                        "ignored_files", "nested_repo_unpushed", "reflog_only",
                        "worktree_refs", "clone_submodules", "operation_in_progress"})
# An operation git has paused in this worktree (its state is in the admin dir,
# which removal deletes): the todo list and the stop point would be lost.
IN_PROGRESS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
               "REVERT_HEAD", "BISECT_LOG", "sequencer")
# Ignored content `git worktree remove` may take: regenerable build output and
# tool caches only. Anything else ignored (.env, .private/, notes, a nested
# clone) keeps the checkout, because ignored is not the same as disposable.
BUILD_DIR_MARKERS = ("CMakeCache.txt", "build.ninja", "CMakeFiles")
CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
DISPOSABLE_FILES = frozenset({".DS_Store", ".pulp-build-active"})
DISPOSABLE_SUFFIXES = (".pyc", ".profraw")
REFLOG_LIMIT = 500
# Stale-entry prune follows git gc's own default (gc.worktreePruneExpire), so a
# worktree that was renamed or moved is not forgotten while it is still in use.
PRUNE_EXPIRE = "3.months.ago"
LINEAGE_KEEP = frozenset({"superseded", "archived"})
LINEAGE_PREFIX = "pulpWorktree"
MAIN_BRANCHES = frozenset({"main", "master"})
SCAN_APP = pathlib.Path(__file__).resolve().parent / "gh-scan-app"
SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")


# ── settings ────────────────────────────────────────────────────────────────

def validate(table: dict[str, Any]) -> list[str]:
    problems = []
    if "merged_worktrees" in table and type(table["merged_worktrees"]) is not bool:
        problems.append("reclaim.merged_worktrees must be a boolean")
    idle = table.get("merged_worktree_idle_hours", DEFAULT_IDLE_HOURS)
    if type(idle) is not int or not MIN_IDLE_HOURS <= idle <= MAX_IDLE_HOURS:
        problems.append("reclaim.merged_worktree_idle_hours must be an integer from "
                        f"{MIN_IDLE_HOURS} through {MAX_IDLE_HOURS}")
    return problems


def settings(table: dict[str, Any]) -> dict[str, Any] | None:
    """The merged-rule settings of a valid `[reclaim]` table, or None when off.

    `mode` is "always" (merged_worktrees = true) or "pressure" (key absent).
    `roots` are the worktree roots; `clone_roots` the subset where a plain
    clone may be removed, which is exactly where tmp_checkouts already allows
    it (the worktree root under worktree_root_checkouts = true).
    """
    if table.get("merged_worktrees") is False:
        return None
    roots: list[str] = []
    clone_roots: list[str] = []
    root = table.get("worktrees_root")
    if isinstance(root, str):
        roots.append(root)
        if table.get("worktree_root_checkouts") is True:
            clone_roots.append(root)
    for extra in table.get("extra_worktree_roots", []) or []:
        if isinstance(extra, str) and extra not in roots:
            roots.append(extra)
    if not roots:
        return None
    return {"mode": "always" if table.get("merged_worktrees") is True else "pressure",
            "idle_hours": table.get("merged_worktree_idle_hours", DEFAULT_IDLE_HOURS),
            "roots": roots, "clone_roots": clone_roots}


def scan_app_env(data: dict[str, Any]) -> dict[str, str] | None:
    """The gh-scan-app environment from `[github_scan_app]`, else from the env."""
    app = data.get("github_scan_app") if isinstance(data, dict) else None
    if isinstance(app, dict) and all(isinstance(app.get(k), str)
                                     for k in ("id", "private_key_path", "cache_dir")):
        return {"TARTCI_SCAN_APP_ID": app["id"],
                "TARTCI_SCAN_APP_PRIVATE_KEY_PATH": app["private_key_path"],
                "TARTCI_SCAN_APP_CACHE_DIR": app["cache_dir"]}
    names = ("TARTCI_SCAN_APP_ID", "TARTCI_SCAN_APP_PRIVATE_KEY_PATH",
             "TARTCI_SCAN_APP_CACHE_DIR")
    if all(os.environ.get(name) for name in names):
        return {name: os.environ[name] for name in names}
    return None


# ── git helpers ─────────────────────────────────────────────────────────────

def _run(argv: list[str], runner: Runner, timeout: float,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        if runner is subprocess.run:
            import tmp_checkouts  # noqa: PLC0415 - avoids an import cycle
            return tmp_checkouts._bounded_git(argv, timeout=timeout, env=env)
        return runner(argv, capture_output=True, text=True, timeout=timeout,
                      check=False, **({"env": env} if env else {}))
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", "not found")


def git(path: pathlib.Path, *args: str, runner: Runner,
        timeout: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    # --no-optional-locks: asking must not refresh the index, or the idle gate
    # (which reads the index mtime) would see every examined checkout as used.
    return _run(["git", "--no-optional-locks", "-C", str(path), *args], runner, timeout)


def common_dir(checkout: pathlib.Path, runner: Runner) -> pathlib.Path | None:
    proc = git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir",
               runner=runner)
    text = proc.stdout.strip()
    if proc.returncode != 0 or not text:
        return None
    return pathlib.Path(text)


def github_slug(url: str) -> str | None:
    text = url.strip()
    match = re.match(r"^(?:https?://|ssh://)?(?:[^@/]+@)?github\.com[:/](.+?)(?:\.git)?/?$",
                     text, re.IGNORECASE)
    if not match:
        return None
    slug = match.group(1)
    return slug if SLUG.fullmatch(slug) else None


def github_remote(repo: pathlib.Path, runner: Runner) -> tuple[str, str] | None:
    """(remote name, OWNER/REPO) of the repository's one GitHub repository.

    `origin` when it is on GitHub; otherwise the only GitHub repository any
    remote names. Two different GitHub repositories and no `origin` among them
    is ambiguous, and an ambiguous answer proves nothing.
    """
    proc = git(repo, "config", "--get-regexp", r"^remote\..*\.url$", runner=runner)
    if proc.returncode not in (0, 1):
        return None
    remotes: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, _, url = line.partition(" ")
        name = key[len("remote."):-len(".url")]
        slug = github_slug(url)
        if slug and name:
            remotes[name] = slug
    if "origin" in remotes:
        return "origin", remotes["origin"]
    slugs = {slug.lower() for slug in remotes.values()}
    if len(slugs) != 1:
        return None
    name = sorted(remotes)[0]
    return name, remotes[name]


def all_command_lines(runner: Runner = subprocess.run) -> str | None:
    """Every running command line on the host, or None when ps did not answer."""
    try:
        proc = runner(["ps", "-axww", "-o", "command="], capture_output=True, text=True,
                      timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout


def named_by(commands: str, spellings: set[str]) -> bool:
    """A command line naming the checkout, or anything inside it.

    Deliberately a substring test: `/root/foo` also matches `/root/foo-bar`,
    which keeps a checkout that did not need keeping, never the reverse.
    """
    return any(spelling in commands for spelling in spellings)


# ── merge proof ─────────────────────────────────────────────────────────────

class Proof:
    """Merge evidence for one pass: one fetch per repository, one GraphQL read."""

    def __init__(self, *, runner: Runner = subprocess.run,
                 app_env: dict[str, str] | None = None,
                 graphql: Callable[[str, list[dict[str, str]]], dict] | None = None,
                 commands: str | None | bool = True, fetch: bool = True,
                 roots: list[pathlib.Path] | None = None,
                 cwd_reader: Callable[[], list[str] | None] | None = None) -> None:
        self.runner = runner
        self.app_env = app_env
        self.graphql = graphql or self._graphql
        self.fetch = fetch
        self.repos: dict[str, dict[str, Any]] = {}
        self.pending: list[dict[str, Any]] = []
        self.pr_heads: dict[tuple[str, str], list[dict[str, str]]] = {}
        self.cwd_reader = cwd_reader
        self.auto_commands = commands is True
        self.commands = all_command_lines(runner) if commands is True else commands
        self.roots = [pathlib.Path(r) for r in (roots or [])]
        self.touched: set[str] = set()
        self.holding: list[dict[str, Any]] = []
        self.examined: set[str] = set()
        self.status: dict[str, Any] = {"fetches": {}, "pr_lookup": "not needed",
                                       "pr_lookups": 0, "pr_deferred": 0,
                                       "lineage_recorded": 0, "lineage_failed": 0,
                                       "forced_submodule_removals": 0, "pruned": {}}

    def refresh_commands(self) -> None:
        if self.auto_commands:
            self.commands = all_command_lines(self.runner)

    def fresh_cwds(self) -> list[str] | None:
        """Every process cwd now, for the check immediately before a removal."""
        if self.cwd_reader is not None:
            return self.cwd_reader()
        import tmp_checkouts  # noqa: PLC0415 - avoids an import cycle
        return tmp_checkouts.process_cwds(self.runner)

    # -- repository facts --
    def repo(self, common: pathlib.Path) -> dict[str, Any]:
        key = str(common)
        if key in self.repos:
            return self.repos[key]
        info: dict[str, Any] = {"common": common, "remote": None, "slug": None,
                                "main": None, "merges": None}
        self.repos[key] = info
        remote = github_remote(common, self.runner)
        if remote is None:
            self.status["fetches"][key] = "no GitHub remote"
            return info
        info["remote"], info["slug"] = remote
        tracking = f"refs/remotes/{info['remote']}/main"
        state = "not fetched"
        if self.fetch:
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
            # A key that needs a passphrase must fail, not wait on a prompt.
            env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
            proc = _run(["git", "--git-dir", str(common), "fetch", "-q", "--no-tags",
                         "--no-recurse-submodules", "--no-write-fetch-head",
                         # no gc/maintenance side effects (it prunes worktrees too)
                         "--no-auto-maintenance",
                         info["remote"], f"+refs/heads/main:{tracking}"],
                        self.runner, FETCH_TIMEOUT_S, env=env)
            state = "fetched" if proc.returncode == 0 else f"fetch failed ({proc.returncode})"
        # A remote-tracking main that was not refreshed is still proof: a commit
        # that was ever on the remote's main has merged. It only proves less.
        sha = _run(["git", "--git-dir", str(common), "rev-parse", "--verify", "-q",
                    f"{tracking}^{{commit}}"], self.runner, GIT_TIMEOUT_S)
        if sha.returncode == 0 and SHA.fullmatch(sha.stdout.strip()):
            info["main"] = tracking
        else:
            state += "; no main to compare against"
        self.status["fetches"][key] = state
        return info

    def ancestor(self, info: dict[str, Any], head: str) -> bool:
        if not info.get("main"):
            return False
        proc = _run(["git", "--git-dir", str(info["common"]), "merge-base", "--is-ancestor",
                     head, info["main"]], self.runner, GIT_TIMEOUT_S)
        return proc.returncode == 0

    def merge_pr_url(self, info: dict[str, Any], head: str) -> str | None:
        """The PR whose merge commit on main has `head` as its second parent."""
        if not info.get("main") or not info.get("slug"):
            return None
        if info["merges"] is None:
            proc = _run(["git", "--git-dir", str(info["common"]), "log", "--first-parent",
                         "--merges", f"-n{MERGE_TABLE_LIMIT}", "--format=%P%x09%s",
                         info["main"]], self.runner, GIT_TIMEOUT_S)
            merges: dict[str, str] = {}
            for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
                parents, _, subject = line.partition("\t")
                parts = parents.split()
                found = re.match(r"^Merge pull request #([0-9]+) ", subject)
                if len(parts) >= 2 and found:
                    merges.setdefault(parts[1], found.group(1))
            info["merges"] = merges
        number = info["merges"].get(head)
        return f"https://github.com/{info['slug']}/pull/{number}" if number else None

    # -- the one GraphQL read --
    def _graphql(self, slug: str, lookups: list[dict[str, str]]) -> dict:
        owner, name = slug.split("/", 1)
        fields = []
        args = ["api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}"]
        variables = ["$owner:String!", "$name:String!"]
        for index, lookup in enumerate(lookups):
            variables.append(f"$b{index}:String!")
            args += ["-f", f"b{index}={lookup['branch']}"]
            fields.append(f"b{index}: pullRequests(headRefName: $b{index}, first: 20, "
                          "states: [MERGED, CLOSED]) { nodes { url state headRefOid } }")
        query = (f"query({', '.join(variables)}) {{ repository(owner: $owner, name: $name) "
                 f"{{ {' '.join(fields)} }} }}")
        env = dict(os.environ, **(self.app_env or {}), TARTCI_SCAN_APP_REPO=slug)
        proc = _run([str(SCAN_APP), *args, "-f", f"query={query}"], self.runner,
                    GRAPHQL_TIMEOUT_S, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"GraphQL read failed (rc={proc.returncode})")
        payload = json.loads(proc.stdout)
        repository = (payload.get("data") or {}).get("repository")
        if not isinstance(repository, dict):
            raise RuntimeError("GraphQL read returned no repository")
        return {lookups[int(key[1:])]["branch"]: (value or {}).get("nodes") or []
                for key, value in repository.items() if key.startswith("b")}

    def resolve(self) -> None:
        """Answer the queued squash lookups with one read, for one repository.

        One read per pass, so only one repository's lookups go out: the one
        with the most waiting (an installation token is per repository). The
        rest are counted as deferred and come back on a later pass.
        """
        if not self.pending:
            return
        if self.app_env is None:
            self.status["pr_lookup"] = "no read-only scan App configured; squash proof off"
            return
        by_slug: dict[str, list[dict[str, Any]]] = {}
        for item in self.pending:
            by_slug.setdefault(item["slug"], []).append(item)
        slug = sorted(by_slug, key=lambda s: (-len(by_slug[s]), s))[0]
        branches = sorted({item["pr_branch"] for item in by_slug[slug]})
        asked = branches[:PR_LOOKUP_LIMIT]
        self.status["pr_deferred"] = (len(self.pending) - sum(
            1 for item in by_slug[slug] if item["pr_branch"] in asked))
        try:
            answer = self.graphql(slug, [{"branch": b} for b in asked])
        except Exception as exc:  # noqa: BLE001 - any failure is "no proof"
            self.status["pr_lookup"] = f"failed: {exc}"
            return
        self.status["pr_lookup"] = f"ok: {slug}, {len(asked)} branch(es)"
        self.status["pr_lookups"] = len(asked)
        for branch in asked:
            nodes = answer.get(branch) or []
            self.pr_heads[(slug, branch)] = [
                node for node in nodes if isinstance(node, dict)
                and node.get("state") in ("MERGED", "CLOSED")
                and isinstance(node.get("headRefOid"), str)]

    def pr_for(self, slug: str, branch: str, head: str) -> str | None:
        for node in self.pr_heads.get((slug, branch), []):
            if node["headRefOid"] == head:
                url = node.get("url")
                return url if isinstance(url, str) else f"https://github.com/{slug}"
        return None

    def note_holding(self, checkout: pathlib.Path, reason: str, branch: str | None,
                     head: str | None) -> None:
        self.holding.append({"path": str(checkout), "reason": reason, "branch": branch,
                             "head": head})


# ── per-checkout judgement ──────────────────────────────────────────────────

def branch_of(checkout: pathlib.Path, runner: Runner) -> str | None:
    proc = git(checkout, "symbolic-ref", "--short", "-q", "HEAD", runner=runner)
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def pr_branch(checkout: pathlib.Path, branch: str, remote: str, runner: Runner) -> str:
    """The name the branch was pushed under: its upstream on that remote, else itself."""
    proc = git(checkout, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
               f"{branch}@{{upstream}}", runner=runner)
    upstream = proc.stdout.strip()
    if proc.returncode == 0 and upstream.startswith(remote + "/"):
        return upstream[len(remote) + 1:]
    return branch


def clean_reason(checkout: pathlib.Path, runner: Runner) -> str | None:
    status = git(checkout, "status", "--porcelain", "--untracked-files=normal",
                 "--ignore-submodules=none", runner=runner)
    if status.returncode != 0:
        return "git_error"
    if status.stdout.strip():
        return "dirty"
    # assume-unchanged (lowercase tag) and skip-worktree (S) entries hide local
    # edits from `git status`; `worktree remove` would delete them unseen.
    hidden = git(checkout, "ls-files", "-v", runner=runner)
    if hidden.returncode != 0:
        return "git_error"
    for line in hidden.stdout.splitlines():
        tag = line[:1]
        if tag == "S" or (tag.isalpha() and tag.islower()):
            return "hidden_changes"
    return None


def disposable(checkout: pathlib.Path, rel: str) -> bool:
    """Whether an ignored path is regenerable output `worktree remove` may take."""
    name = rel.rstrip("/").rsplit("/", 1)[-1]
    target = checkout / rel.rstrip("/")
    if os.path.islink(target):
        # `worktree remove` unlinks a symlink and never follows it; Pulp links
        # external/<sdk> into a shared cache that way.
        return True
    if not rel.endswith("/"):
        return name in DISPOSABLE_FILES or name.endswith(DISPOSABLE_SUFFIXES)
    if name in CACHE_DIRS:
        return True
    if name in ("build", "out", "install") or name.startswith("build-"):
        return any(os.path.lexists(target / marker) for marker in BUILD_DIR_MARKERS)
    import checkout_outputs  # noqa: PLC0415 - avoids an import cycle
    rules = checkout_outputs.OUTPUT_KINDS.get(checkout_outputs.kind_of(name) or "")
    return bool(rules) and any(os.path.lexists(target / m) for m in rules["markers"])


def repo_reason(repo: pathlib.Path, runner: Runner, reason: str) -> str | None:
    """`reason` when a nested repository holds anything its remotes lack."""
    if gitlinks(repo, runner):
        return "nested_submodule"
    if clean_reason(repo, runner):
        return reason
    stash = git(repo, "stash", "list", runner=runner)
    if stash.returncode != 0 or stash.stdout.strip():
        return reason
    local = git(repo, "rev-list", "-1", "HEAD", "--branches", "--tags", "--not", "--remotes",
                runner=runner)
    if local.returncode != 0 or local.stdout.strip():
        return reason
    return None


def ignored_reason(checkout: pathlib.Path, runner: Runner) -> str | None:
    """Ignored content that is not build output keeps the checkout.

    A nested clone (an ignored `external/<sdk>` checkout) may go only when it is
    itself clean, stash-free and fully pushed; any other ignored path that is
    not on the build-output list stays, since git would delete it unseen.
    """
    proc = git(checkout, "status", "--porcelain", "-z", "--ignored",
               "--untracked-files=normal", "--ignore-submodules=none", runner=runner)
    if proc.returncode != 0:
        return "git_error"
    for entry in proc.stdout.split("\0"):
        if not entry.startswith("!! "):
            continue
        rel = entry[3:]
        if disposable(checkout, rel):
            continue
        nested = checkout / rel.rstrip("/")
        if rel.endswith("/") and os.path.lexists(nested / ".git"):
            reason = repo_reason(nested, runner, "nested_repo_unpushed")
            if reason:
                return reason
            continue
        return "ignored_files"
    return None


def private_history_reason(checkout: pathlib.Path, runner: Runner) -> str | None:
    """Commits only this worktree's own refs reach keep it.

    Removing a worktree deletes its HEAD reflog and its per-worktree refs
    (refs/worktree/*, refs/bisect/*). A commit made on a detached HEAD and then
    left behind lives nowhere else. Amended or rebased-away commits that were
    never pushed count too: keeping them is the safe direction.
    """
    gitdir = git(checkout, "rev-parse", "--absolute-git-dir", runner=runner)
    if gitdir.returncode != 0 or not gitdir.stdout.strip():
        return "git_error"
    if any(os.path.lexists(os.path.join(gitdir.stdout.strip(), name)) for name in IN_PROGRESS):
        return "operation_in_progress"
    refs = git(checkout, "for-each-ref", "--format=%(refname)", "refs/worktree/",
               "refs/bisect/", runner=runner)
    if refs.returncode != 0:
        return "git_error"
    if refs.stdout.strip():
        return "worktree_refs"
    log = git(checkout, "log", "-g", "--format=%H", "HEAD", runner=runner)
    if log.returncode != 0:
        return "git_error"
    shas = sorted({line for line in log.stdout.split() if SHA.fullmatch(line)})
    if not shas:
        return None
    if len(shas) > REFLOG_LIMIT:
        return "reflog_only"
    lost = git(checkout, "rev-list", "-1", *shas, "--not", "--remotes", "--branches",
               "--tags", runner=runner)
    if lost.returncode != 0:
        return "git_error"
    return "reflog_only" if lost.stdout.strip() else None


def stash_reason(checkout: pathlib.Path, branch: str | None, head: str,
                 runner: Runner) -> str | None:
    """A stash entry made on this branch or on this HEAD keeps the checkout.

    The stash is repository-wide, so a worktree's own entries are the ones git
    labelled with its branch ("WIP on <b>:" / "On <b>:") or whose base commit
    is its HEAD. Removing the worktree would not drop them, but they are work
    the owner parked here, and the rule is that parked work is never reclaimed.
    """
    proc = git(checkout, "stash", "list", "--format=%P%x09%gs", runner=runner)
    if proc.returncode != 0:
        return "git_error"
    for line in proc.stdout.splitlines():
        parents, _, subject = line.partition("\t")
        first = parents.split()[0] if parents.split() else ""
        if first == head:
            return "stash"
        if branch and (subject.startswith(f"WIP on {branch}:")
                       or subject.startswith(f"On {branch}:")):
            return "stash"
    return None


def judge(checkout: pathlib.Path, kind: str, proof: Proof,
          runner: Runner) -> tuple[str, Any]:
    """("ready", info) | ("pending", info) | ("keep", reason).

    `info` carries what removal needs. Called after tmp_checkouts' in-use,
    idle, lineage and has-worktrees gates.
    """
    head_proc = git(checkout, "rev-parse", "--verify", "-q", "HEAD^{commit}", runner=runner)
    head = head_proc.stdout.strip()
    if head_proc.returncode != 0 or not SHA.fullmatch(head):
        return "keep", "git_error"
    branch = branch_of(checkout, runner)
    reason = (clean_reason(checkout, runner) or stash_reason(checkout, branch, head, runner)
              or ignored_reason(checkout, runner))
    if not reason and kind == "worktree":
        reason = private_history_reason(checkout, runner)
    if not reason and kind == "clone":
        reason = clone_reason(checkout, runner)
    if reason:
        if reason in HOLDS_WORK:
            proof.note_holding(checkout, reason, branch, head)
        return "keep", reason
    common = common_dir(checkout, runner)
    if common is None:
        return "keep", "git_error"
    info = proof.repo(common)
    item = {"checkout": checkout, "kind": kind, "head": head, "branch": branch,
            "common": common, "slug": info.get("slug"), "pr_url": None, "proof": None}
    if proof.ancestor(info, head):
        item["proof"] = "ancestor_of_main"
        item["pr_url"] = proof.merge_pr_url(info, head)
        return "ready", item
    if branch and info.get("slug") and info.get("remote"):
        item["pr_branch"] = pr_branch(checkout, branch, info["remote"], runner)
        url = proof.pr_for(info["slug"], item["pr_branch"], head)
        if url:
            item["proof"], item["pr_url"] = "pr_head_matches", url
            return "ready", item
        if (info["slug"], item["pr_branch"]) not in proof.pr_heads:
            return "pending", item
    return "keep", unmerged_reason(checkout, branch, head, proof, runner)


def unmerged_reason(checkout: pathlib.Path, branch: str | None, head: str, proof: Proof,
                    runner: Runner) -> str:
    """A checkout with no merge proof stays; one whose HEAD no remote has is listed."""
    pushed = git(checkout, "rev-list", "-1", "HEAD", "--not", "--remotes", runner=runner)
    if pushed.returncode != 0 or pushed.stdout.strip():
        proof.note_holding(checkout, "unpushed", branch, head)
        return "unpushed"
    return "unmerged"


def clone_reason(clone: pathlib.Path, runner: Runner) -> str | None:
    """A clone takes its submodule git dirs and its tags with it when removed."""
    modules = clone / ".git" / "modules"
    try:
        if modules.is_dir() and any(modules.iterdir()):
            return "clone_submodules"
    except OSError:
        return "clone_submodules"
    tags = git(clone, "rev-list", "-1", "--tags", "--not", "--remotes", runner=runner)
    if tags.returncode != 0:
        return "git_error"
    return "unpushed" if tags.stdout.strip() else None


# ── removal ─────────────────────────────────────────────────────────────────

def gitlinks(checkout: pathlib.Path, runner: Runner) -> list[str] | None:
    proc = git(checkout, "ls-files", "-s", "-z", runner=runner)
    if proc.returncode != 0:
        return None
    out = []
    for entry in proc.stdout.split("\0"):
        meta, _, path = entry.partition("\t")
        if meta.startswith("160000 ") and path:
            out.append(path)
    return out


def submodule_reason(checkout: pathlib.Path, runner: Runner) -> str | None:
    """Why a populated submodule makes the worktree unsafe to force-remove.

    `--force` deletes the submodules' git directories (they live in the
    worktree's admin dir), so each must hold nothing its remotes lack.
    """
    paths = gitlinks(checkout, runner)
    if paths is None:
        return "git_error"
    for path in paths:
        sub = checkout / path
        if not os.path.lexists(sub / ".git"):
            continue  # not populated: nothing of it lives here
        reason = repo_reason(sub, runner, "submodule_unpushed")
        if reason:
            return reason
    return None


def record_lineage(item: dict[str, Any], runner: Runner) -> bool:
    """Write Pulp's lineage `merged` record for the branch, as `mark` would."""
    branch = item.get("branch")
    if not branch or branch in MAIN_BRANCHES:
        return True
    base = ["git", "--git-dir", str(item["common"]), "config", "--local"]
    key = f"branch.{branch}.{LINEAGE_PREFIX}"
    # A superseded or archived record points somewhere (a successor, a bundle);
    # it already says more than "merged", so it is left as the owner wrote it.
    current = _run([*base, "--get", key + "Status"], runner, GIT_TIMEOUT_S)
    if current.stdout.strip() in LINEAGE_KEEP:
        return True
    fields = [("Status", "merged"), ("DurableSha", item["head"]),
              ("UpdatedAt", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
              ("LastPath", str(item["checkout"])),
              ("Note", f"worktree reclaimed by tartci after merge ({item['proof']})")]
    if item.get("pr_url"):
        fields.append(("Pr", item["pr_url"]))
    ok = True
    for field, value in fields:
        ok = _run([*base, key + field, value], runner, GIT_TIMEOUT_S).returncode == 0 and ok
    return ok


def remove(item: dict[str, Any], proof: Proof, runner: Runner) -> str | None:
    """Remove one merged worktree through git; the refusal, or None when gone."""
    checkout = item["checkout"]
    base = ["git", "--git-dir", str(item["common"]), "worktree", "remove"]
    proc = _run([*base, str(checkout)], runner, GIT_TIMEOUT_S)
    if proc.returncode != 0 and "submodule" in (proc.stderr or "") and checkout.exists():
        reason = submodule_reason(checkout, runner)
        if reason:
            if reason in HOLDS_WORK:
                proof.note_holding(checkout, reason, item.get("branch"), item["head"])
            return reason
        # Re-read the superproject immediately before the one force: --force
        # also skips git's clean check, so ours must be the last word.
        again = git(checkout, "rev-parse", "HEAD", runner=runner).stdout.strip()
        if again != item["head"] or clean_reason(checkout, runner):
            return "changed_before_removal"
        proc = _run([*base, "--force", str(checkout)], runner, GIT_TIMEOUT_S)
        if proc.returncode == 0:
            proof.status["forced_submodule_removals"] += 1
    if proc.returncode != 0 or checkout.exists():
        return "worktree_remove_refused"
    proof.touched.add(str(item["common"]))
    if record_lineage(item, runner):
        proof.status["lineage_recorded"] += 1
    else:
        proof.status["lineage_failed"] += 1
    return None


def prune(proof: Proof, runner: Runner) -> None:
    """`git worktree prune` each repository a removal touched, when it is safe.

    Prune forgets a worktree whose directory is missing, including one that was
    renamed while in use (its admin dir, index and reflog go with it) or sits
    on a volume that is merely unmounted. So it only takes entries older than
    git gc's own expiry (`--expire 3.months.ago`), and only when every such
    entry lies under a root that exists right now; otherwise nothing is pruned
    and the count is reported.
    """
    roots = sorted({str(spelling).rstrip("/") + "/" for root in proof.roots
                    if root.is_dir() for spelling in (root, root.resolve())})
    for common in sorted(proof.touched):
        dry = _run(["git", "--git-dir", common, "worktree", "prune", "--dry-run", "-v",
                    f"--expire={PRUNE_EXPIRE}"], runner, GIT_TIMEOUT_S)
        if dry.returncode != 0:
            proof.status["pruned"][common] = "dry run failed"
            continue
        ids = re.findall(r"^Removing worktrees/([^:]+):", dry.stdout + dry.stderr, re.M)
        outside = []
        for ident in ids:
            try:
                target = (pathlib.Path(common) / "worktrees" / ident / "gitdir").read_text()
            except OSError:
                outside.append(ident)
                continue
            where = pathlib.Path(target.strip())
            spellings = {str(where), str(where.resolve())}
            if not any(text.startswith(root) for text in spellings for root in roots):
                outside.append(ident)
        if outside:
            proof.status["pruned"][common] = f"skipped: {len(outside)} entry(ies) outside the roots"
            continue
        if not ids:
            proof.status["pruned"][common] = "nothing stale"
            continue
        done = _run(["git", "--git-dir", common, "worktree", "prune",
                     f"--expire={PRUNE_EXPIRE}"], runner, GIT_TIMEOUT_S)
        proof.status["pruned"][common] = (f"pruned {len(ids)}" if done.returncode == 0
                                          else "prune failed")


# ── the report of checkouts holding work ───────────────────────────────────

def write_report(path: pathlib.Path, proof: Proof, now: float) -> int:
    """Merge this pass's holding list into the standing report; returns its size.

    The cursor means one pass sees part of a root, so entries persist until a
    pass re-examines that checkout (and finds it no longer holding work) or the
    checkout is gone.
    """
    try:
        previous = json.loads(path.read_text()).get("checkouts", [])
    except (OSError, ValueError, AttributeError):
        previous = []
    fresh = {entry["path"]: dict(entry, seen_at=now) for entry in proof.holding}
    merged = dict(fresh)
    for entry in previous if isinstance(previous, list) else []:
        where = entry.get("path") if isinstance(entry, dict) else None
        if not where or where in merged or where in proof.examined:
            continue
        if os.path.lexists(where):
            merged[where] = entry
    body = {"schema": 1, "updated_at": now,
            "checkouts": sorted(merged.values(), key=lambda e: e["path"])}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError:
        pass
    return len(merged)
