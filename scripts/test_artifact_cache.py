#!/usr/bin/env python3
"""Tests for the optional read-only artifact cache served to macOS guests."""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers/tart-macos/artifact-cache.lib.sh"
SYNC = ROOT / "scripts/artifact-cache.sh"
MAC_JIT = ROOT / "providers/tart-macos/runner.sh"
WARM = ROOT / "providers/tart-macos/warm-vm.lib.sh"
REFRESH_LABEL = "com.danielraffel.tartci.artifact-cache-refresh"
REFRESH_TEMPLATE = ROOT / f"launchd/{REFRESH_LABEL}.plist.template"
REFRESH_INSTALLER = ROOT / "scripts/install_artifact_cache_refresh_agent.sh"
RUNNER_TEMPLATE = ROOT / "launchd/com.danielraffel.pulp.tart-runner-macos.plist.template"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()


class ReadyTests(unittest.TestCase):
    def _ready(self, path: str) -> bool:
        proc = subprocess.run(
            ["bash", "-c", f'source "{LIB}"; artifact_cache_ready "$1"', "_", path],
            capture_output=True, text=True, check=False,
        )
        return proc.returncode == 0

    def test_a_blob_or_a_mirror_makes_it_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sha256").mkdir()
            (Path(tmp) / "sha256" / ("0" * 64)).write_bytes(b"x")
            self.assertTrue(self._ready(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "git/owner/repo.git").mkdir(parents=True)
            self.assertTrue(self._ready(tmp))

    def test_absent_empty_or_staging_only_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self._ready(tmp))
            self.assertFalse(self._ready(str(Path(tmp) / "missing")))
            (Path(tmp) / "sha256").mkdir()
            (Path(tmp) / "sha256/.staging.abc").write_bytes(b"partial")
            (Path(tmp) / "git/owner").mkdir(parents=True)
            self.assertFalse(self._ready(tmp))

    def test_unshareable_paths_are_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            colon = Path(tmp) / "a:b"
            (colon / "sha256").mkdir(parents=True)
            (colon / "sha256" / ("0" * 64)).write_bytes(b"x")
            self.assertFalse(self._ready(str(colon)))
        self.assertFalse(self._ready("relative/cache"))
        self.assertFalse(self._ready(""))


class AddTests(unittest.TestCase):
    """`add` stores verified bytes under their digest, through a stub curl."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "curl.calls"
        curl = self.bin / "curl"
        curl.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            echo "$*" >> {self.calls}
            out=""
            while [ "$#" -gt 0 ]; do [ "$1" = "--output" ] && out="$2"; shift; done
            [ -n "${{STUB_FAIL:-}}" ] && exit 22
            printf '%s' "${{STUB_BYTES:-payload}}" > "$out"
            """))
        curl.chmod(0o755)
        self.cache = self.tmp / "cache"
        self.sha = hashlib.sha256(b"payload").hexdigest()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, sha: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SYNC), "add", "--url", "https://example.invalid/a.zip",
             "--sha256", sha, "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", **env},
        )

    def test_verified_bytes_land_under_their_digest(self) -> None:
        proc = self._add(self.sha)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.cache / "sha256" / self.sha).read_bytes(), b"payload")
        self.assertEqual([p.name for p in (self.cache / "sha256").iterdir()], [self.sha])
        self.assertFalse((self.cache / ".lock").exists())

    def test_mismatched_bytes_never_land(self) -> None:
        proc = self._add(self.sha, STUB_BYTES="tampered")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("SHA-256 mismatch", proc.stderr)
        self.assertEqual(list((self.cache / "sha256").iterdir()), [])

    def test_failed_download_leaves_nothing(self) -> None:
        proc = self._add(self.sha, STUB_FAIL="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(list((self.cache / "sha256").iterdir()), [])

    def test_readd_verifies_without_downloading(self) -> None:
        self.assertEqual(self._add(self.sha).returncode, 0)
        self.calls.unlink()
        proc = self._add(self.sha)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already present", proc.stdout)
        self.assertFalse(self.calls.exists())

    def test_a_corrupted_blob_is_reported_not_trusted(self) -> None:
        self.assertEqual(self._add(self.sha).returncode, 0)
        (self.cache / "sha256" / self.sha).write_bytes(b"rot")
        proc = self._add(self.sha)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not hash to its name", proc.stderr)

    def test_rejects_bad_digest_and_plain_http(self) -> None:
        self.assertNotEqual(self._add("ABC").returncode, 0)
        proc = subprocess.run(
            ["bash", str(SYNC), "add", "--url", "http://example.invalid/a",
             "--sha256", self.sha, "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("must be https", proc.stderr)

    def test_prune_removes_only_stale_blobs(self) -> None:
        self.assertEqual(self._add(self.sha).returncode, 0)
        old = self.cache / "sha256" / ("1" * 64)
        old.write_bytes(b"old")
        os.utime(old, (0, 0))
        proc = subprocess.run(
            ["bash", str(SYNC), "prune", "--older-than-days", "7", "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(old.exists())
        self.assertTrue((self.cache / "sha256" / self.sha).exists())


class GitSyncTests(unittest.TestCase):
    """`git-sync` mirrors one branch of a local origin and serves as an alternate."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.base = self.tmp / "remote"
        self.origin = self.base / "owner/repo.git"
        self.origin.mkdir(parents=True)
        _git("init", "--quiet", "--bare", "-b", "main", str(self.origin))
        self.work = self.tmp / "work"
        _git("clone", "--quiet", str(self.origin), str(self.work))
        self._commit("one")
        self.cache = self.tmp / "cache"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _commit(self, name: str) -> str:
        (self.work / name).write_text(name)
        _git("add", name, cwd=self.work)
        _git("commit", "--quiet", "-m", name, cwd=self.work)
        _git("push", "--quiet", "origin", "HEAD:main", cwd=self.work)
        return _git("rev-parse", "HEAD", cwd=self.work)

    def _sync(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SYNC), "git-sync", "--repo", "owner/repo", "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "TARTCI_ARTIFACT_CACHE_GIT_BASE": str(self.base)},
        )

    def test_initial_sync_then_incremental_sync(self) -> None:
        proc = self._sync()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        mirror = self.cache / "git/owner/repo.git"
        first = _git("rev-parse", "refs/heads/main", cwd=mirror)
        self.assertEqual(_git("config", "gc.auto", cwd=mirror), "0")
        self.assertEqual(_git("config", "maintenance.auto", cwd=mirror), "false")
        second = self._commit("two")
        self.assertEqual(self._sync().returncode, 0)
        self.assertNotEqual(first, second)
        self.assertEqual(_git("rev-parse", "refs/heads/main", cwd=mirror), second)
        self.assertEqual(list((self.cache / "git/owner").glob(".staging*")), [])

    def _packs(self) -> list[Path]:
        return list((self.cache / "git/owner/repo.git/objects/pack").glob("*.pack"))

    def _compact(self, running: int) -> subprocess.CompletedProcess:
        tart = self.tmp / "tart"
        vms = ", ".join('{"Name": "v%d", "State": "running"}' % i for i in range(running))
        tart.write_text(f"#!/bin/bash\necho '[{vms}]'\n")
        tart.chmod(0o755)
        return subprocess.run(
            ["bash", str(SYNC), "compact", "--repo", "owner/repo", "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "TARTCI_TART_BIN": str(tart)},
        )

    def test_sync_never_deletes_a_pack(self) -> None:
        self.assertEqual(self._sync().returncode, 0)
        seen = set(self._packs())
        for n in range(18):
            self._commit(f"c{n}")
            self.assertEqual(self._sync().returncode, 0)
            now = set(self._packs())
            self.assertTrue(seen <= now, "a sync removed a pack a guest may be reading")
            seen = now
        self.assertEqual(len(seen), 19)

    def test_compact_refuses_while_a_vm_runs_and_folds_when_idle(self) -> None:
        self.assertEqual(self._sync().returncode, 0)
        for n in range(3):
            self._commit(f"c{n}")
            self.assertEqual(self._sync().returncode, 0)
        before = len(self._packs())
        proc = self._compact(running=1)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refusing to compact", proc.stderr)
        self.assertEqual(len(self._packs()), before)
        proc = self._compact(running=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self._packs()), 1)
        self.assertEqual(_git("fsck", "--connectivity-only", "--no-dangling",
                              cwd=self.cache / "git/owner/repo.git"), "")

    def test_a_clone_using_the_mirror_as_alternate_needs_only_new_objects(self) -> None:
        self.assertEqual(self._sync().returncode, 0)
        head = self._commit("newer")
        guest = self.tmp / "guest"
        _git("init", "--quiet", str(guest))
        (guest / ".git/objects/info/alternates").write_text(
            str(self.cache / "git/owner/repo.git/objects") + "\n")
        _git("fetch", "--quiet", "--no-tags", str(self.origin), "main", cwd=guest)
        self.assertEqual(_git("rev-parse", "FETCH_HEAD", cwd=guest), head)
        # The control: the guest's own store holds only what the mirror lacked.
        local = int(_git("count-objects", "-v", cwd=guest).split("in-pack: ")[1].split()[0] or 0) \
            + int(_git("count-objects", cwd=guest).split()[0])
        self.assertGreater(local, 0)
        self.assertLessEqual(local, 3)

    def test_rejects_a_malformed_repo(self) -> None:
        for bad in ("owner", "owner/repo/extra", "../x/y"):
            proc = subprocess.run(
                ["bash", str(SYNC), "git-sync", "--repo", bad, "--dir", str(self.cache)],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(proc.returncode, 0, bad)


class RunnerWiringTests(unittest.TestCase):
    body = MAC_JIT.read_text(encoding="utf-8")

    def test_runner_sources_the_library(self) -> None:
        self.assertIn('source "$TARTCI_ROOT/providers/tart-macos/artifact-cache.lib.sh"', self.body)

    def test_mount_is_read_only_and_gated_on_a_ready_cache(self) -> None:
        self.assertIn('if artifact_cache_ready "$ARTIFACT_CACHE_ROOT"; then', self.body)
        self.assertIn('--dir="artifact-cache:$ARTIFACT_CACHE_ROOT:ro"', self.body)

    def test_job_env_declares_the_cache_only_when_mounted(self) -> None:
        self.assertIn("if [ '$CURRENT_ARTIFACT_CACHE' = 1 ]; then printf 'TARTCI_ARTIFACT_CACHE=%s", self.body)
        self.assertIn('GUEST_ARTIFACT_CACHE="/Volumes/My Shared Files/artifact-cache"', self.body)

    def test_preserved_env_cannot_forge_the_declaration(self) -> None:
        self.assertIn("|TARTCI_PIP_WHEELHOUSE|TARTCI_ARTIFACT_CACHE)$/", self.body)

    def test_a_warm_vm_keeps_its_declaration(self) -> None:
        warm = WARM.read_text(encoding="utf-8")
        self.assertIn('WARM_ARTIFACT="$CURRENT_ARTIFACT_CACHE"', warm)
        self.assertIn('CURRENT_ARTIFACT_CACHE="$WARM_ARTIFACT"', warm)


class RefreshTests(unittest.TestCase):
    """`refresh` keeps existing mirrors current and never creates one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.base = self.tmp / "remote"
        self.cache = self.tmp / "cache"
        self.work: dict[str, Path] = {}
        for repo in ("owner/repo", "owner/other"):
            origin = self.base / f"{repo}.git"
            origin.mkdir(parents=True)
            _git("init", "--quiet", "--bare", "-b", "main", str(origin))
            work = self.tmp / "work" / repo
            _git("clone", "--quiet", str(origin), str(work))
            self.work[repo] = work
            self._commit(repo, "one")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _commit(self, repo: str, name: str, branch: str = "main") -> str:
        work = self.work[repo]
        (work / name).write_text(name)
        _git("add", name, cwd=work)
        _git("commit", "--quiet", "-m", name, cwd=work)
        _git("push", "--quiet", "origin", f"HEAD:{branch}", cwd=work)
        return _git("rev-parse", "HEAD", cwd=work)

    def _run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SYNC), *args, "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "TARTCI_ARTIFACT_CACHE_GIT_BASE": str(self.base),
                 "TARTCI_ARTIFACT_CACHE_RUNNER_SH": "", **(env or {})},
        )

    def _tart(self, running: int | None) -> dict:
        tart = self.tmp / "tart"
        if running is None:
            tart.write_text("#!/bin/bash\nexit 1\n")
        else:
            vms = ", ".join('{"Name": "v%d", "State": "running"}' % i for i in range(running))
            tart.write_text(f"#!/bin/bash\necho '[{vms}]'\n")
        tart.chmod(0o755)
        return {"TARTCI_TART_BIN": str(tart)}

    def _mirror(self, repo: str = "owner/repo") -> Path:
        return self.cache / f"git/{repo}.git"

    def _packs(self, repo: str = "owner/repo") -> list[Path]:
        return list((self._mirror(repo) / "objects/pack").glob("*.pack"))

    def test_an_absent_cache_is_left_absent(self) -> None:
        proc = self._run("refresh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nothing to refresh", proc.stdout)
        self.assertFalse(self.cache.exists(), "refresh created a cache this host never opted into")

    def test_a_cache_without_mirrors_gains_none(self) -> None:
        (self.cache / "sha256").mkdir(parents=True)
        proc = self._run("refresh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.cache / "git").exists())
        self.assertFalse((self.cache / ".lock").exists())

    def test_existing_mirrors_advance_and_no_new_mirror_appears(self) -> None:
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        self._commit("owner/repo", "dev-base", branch="dev")
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo", "--branch", "dev").returncode, 0)
        head = self._commit("owner/repo", "two")
        dev = self._commit("owner/repo", "three", branch="dev")
        proc = self._run("refresh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        mirror = self._mirror()
        self.assertEqual(_git("rev-parse", "refs/heads/main", cwd=mirror), head)
        self.assertEqual(_git("rev-parse", "refs/heads/dev", cwd=mirror), dev)
        self.assertFalse(self._mirror("owner/other").exists(),
                         "refresh created a mirror nobody asked for")
        self.assertFalse((self.cache / ".lock").exists())

    def test_refresh_without_compaction_never_deletes_a_pack(self) -> None:
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        seen = set(self._packs())
        for n in range(4):
            self._commit("owner/repo", f"c{n}")
            self.assertEqual(self._run("refresh", env=self._tart(0)).returncode, 0)
            now = set(self._packs())
            self.assertTrue(seen <= now, "refresh removed a pack a guest may be reading")
            seen = now
        self.assertEqual(len(seen), 5)

    def _grow(self, packs: int) -> None:
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        for n in range(packs - 1):
            self._commit("owner/repo", f"g{n}")
            self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        self.assertEqual(len(self._packs()), packs)

    def test_compaction_is_deferred_while_a_vm_runs_or_tart_cannot_say(self) -> None:
        self._grow(4)
        for running in (1, None):
            proc = self._run("refresh", "--compact-above", "2", env=self._tart(running))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("compaction deferred", proc.stdout)
            self.assertEqual(len(self._packs()), 4, f"repacked with running={running}")

    def test_compaction_folds_only_above_the_threshold_when_idle(self) -> None:
        self._grow(4)
        proc = self._run("refresh", "--compact-above", "4", env=self._tart(0))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self._packs()), 4)
        proc = self._run("refresh", "--compact-above", "3", env=self._tart(0))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self._packs()), 1)
        self.assertEqual(_git("fsck", "--connectivity-only", "--no-dangling", cwd=self._mirror()), "")

    def test_one_failed_mirror_does_not_stop_the_others(self) -> None:
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        self.assertEqual(self._run("git-sync", "--repo", "owner/other").returncode, 0)
        # Sorted first, so a fail-fast loop would never reach owner/repo.
        _git("-C", str(self._mirror("owner/other")), "remote", "set-url", "origin",
             str(self.tmp / "gone.git"))
        head = self._commit("owner/repo", "two")
        proc = self._run("refresh")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("fetch of owner/other main failed", proc.stderr)
        self.assertEqual(_git("rev-parse", "refs/heads/main", cwd=self._mirror()), head)

    def test_rejects_a_bad_threshold(self) -> None:
        proc = self._run("refresh", "--compact-above", "0")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--compact-above", proc.stderr)


class RefreshAgentTests(unittest.TestCase):
    """The scheduled agent runs refresh at background QoS on the guests' cache."""

    def _spec(self, home: str = "/Users/someone") -> dict:
        res = subprocess.run(
            ["python3", "scripts/render_launchd_template.py", str(REFRESH_TEMPLATE),
             "--set", f"HOME={home}"],
            cwd=ROOT, capture_output=True, text=True, check=True)
        return plistlib.loads(res.stdout.encode())

    def test_template_runs_refresh_at_background_qos(self) -> None:
        spec = self._spec()
        self.assertEqual(spec["Label"], REFRESH_LABEL)
        self.assertEqual(spec["ProgramArguments"][1:4],
                         ["/Users/someone/.local/bin/tartci", "artifact-cache", "refresh"])
        self.assertIn("--compact-above", spec["ProgramArguments"])
        self.assertEqual(spec["ProcessType"], "Background")
        self.assertIs(spec["LowPriorityIO"], True)
        # Interval, not calendar: the watchdog's staleness bound is derived
        # from StartInterval, and an interval is not aligned to :00/:30.
        self.assertGreaterEqual(int(spec["StartInterval"]), 3600)
        self.assertNotIn("StartCalendarInterval", spec)
        self.assertIs(spec["RunAtLoad"], False)

    def test_template_refreshes_the_cache_the_guests_mount(self) -> None:
        runner = plistlib.loads(subprocess.run(
            ["python3", "scripts/render_launchd_template.py", str(RUNNER_TEMPLATE),
             "--set", "HOME=/Users/someone", "--set", "TART_HOME=/Users/someone/VMs"],
            cwd=ROOT, capture_output=True, text=True, check=True).stdout.encode())
        self.assertEqual(self._spec()["EnvironmentVariables"]["TARTCI_CI_CACHE"],
                         runner["EnvironmentVariables"]["TARTCI_CI_CACHE"])

    def test_tartci_dispatches_the_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                ["bash", str(ROOT / "tartci"), "artifact-cache", "refresh",
                 "--dir", str(Path(tmp) / "absent")],
                capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("nothing to refresh", proc.stdout)

    def test_setup_installs_the_agent(self) -> None:
        body = (ROOT / "tartci").read_text()
        start = body.index("cmd_setup()")
        end = body.index("cmd_bench()", start)
        self.assertIn('install_artifact_cache_refresh_agent.sh" --install', body[start:end])

    def _installed_cache(self, profile_text: str | None) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            agents = Path(tmp) / "agents"
            double = Path(tmp) / "launchctl"
            double.write_text("#!/bin/sh\nexit 0\n")
            double.chmod(0o755)
            profile = Path(tmp) / "macos-fleet-profile.toml"
            if profile_text is not None:
                profile.write_text(profile_text)
            res = subprocess.run(
                [str(REFRESH_INSTALLER), "--install"], capture_output=True, text=True,
                check=False,
                env={**os.environ, "HOME": str(home), "TARTCI_AGENTS_DIR": str(agents),
                     "TARTCI_LAUNCHCTL_BIN": str(double),
                     "TARTCI_FLEET_PROFILE": str(profile)})
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            spec = plistlib.loads((agents / f"{REFRESH_LABEL}.plist").read_bytes())
            return spec["EnvironmentVariables"]["TARTCI_CI_CACHE"].replace(str(home), "~")

    def test_installer_refreshes_the_cache_root_the_profile_declares(self) -> None:
        # The runners mount [host].cache_root; a host that moved its CI cache
        # off the home default must have this agent refresh the moved cache.
        self.assertEqual(self._installed_cache(
            '[host]\nid = "studio"\ncache_root = "/Volumes/Example/ci/pulp-ci"\n'),
            "/Volumes/Example/ci/pulp-ci")

    def test_installer_keeps_the_home_default_without_a_declared_cache_root(self) -> None:
        self.assertEqual(self._installed_cache(None), "~/.cache/pulp-ci")
        self.assertEqual(self._installed_cache('[host]\nid = "studio"\n'), "~/.cache/pulp-ci")

    def test_installer_plan_writes_nothing_and_temp_home_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / "agents"
            calls = Path(tmp) / "launchctl.calls"
            double = Path(tmp) / "launchctl"
            double.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{calls}'\nexit 1\n")
            double.chmod(0o755)
            plan = subprocess.run(
                [str(REFRESH_INSTALLER), "--plan"], capture_output=True, text=True, check=False,
                env={**os.environ, "TARTCI_AGENTS_DIR": str(agents),
                     "TARTCI_LAUNCHCTL_BIN": str(double)})
            self.assertEqual(plan.returncode, 0, plan.stderr)
            self.assertIn("plan: write", plan.stdout)
            self.assertFalse((agents / f"{REFRESH_LABEL}.plist").exists())
            calls.unlink(missing_ok=True)
            home = Path(tmp) / "home"
            home.mkdir()
            res = subprocess.run(
                [str(REFRESH_INSTALLER), "--install"], capture_output=True, text=True, check=False,
                env={**os.environ, "HOME": str(home), "TARTCI_LAUNCHCTL_BIN": str(double),
                     "TARTCI_LAUNCHD_GUARD_TREAT_AS_REAL": "1"})
            self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
            self.assertFalse((home / "Library/LaunchAgents" / f"{REFRESH_LABEL}.plist").exists())
            self.assertFalse(calls.exists(), "launchctl was called from a temp HOME")


class PinnedRefreshTests(unittest.TestCase):
    """`refresh` adds the archives a mirrored repository's manifest pins.

    Pulp gate jobs copy the pinned Skia archive from sha256/<pin> when the
    cache holds it; nothing ever added the current pin, so every job
    downloaded 58 MB from GitHub (24 of 24 sampled jobs, 2026-11-07..09) and
    a GitHub 504 failed the gate. These drive the real script against a local
    "remote", a stub curl and a pin file.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.base = self.tmp / "remote"
        self.cache = self.tmp / "cache"
        origin = self.base / "owner/repo.git"
        origin.mkdir(parents=True)
        _git("init", "--quiet", "--bare", "-b", "main", str(origin))
        self.work = self.tmp / "work"
        _git("clone", "--quiet", str(origin), str(self.work))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "curl.calls"
        curl = self.bin / "curl"
        curl.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            echo "$*" >> {self.calls}
            out=""
            url=""
            while [ "$#" -gt 0 ]; do
              case "$1" in --output) out="$2"; shift;; https://*) url="$1";; esac
              shift
            done
            [ -n "${{STUB_FAIL:-}}" ] && exit 22
            # The bytes are the URL's last path segment, so a test controls
            # which digest a download produces.
            printf '%s' "${{STUB_BYTES:-${{url##*/}}}}" > "$out"
            """))
        curl.chmod(0o755)
        self.pins = self.tmp / "pins.tsv"
        self.pins.write_text("# comment\n\nowner/repo\tmain\tdeps/manifest.json\tSkia\tmac-arm64\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _manifest(self, name: str, *, asset: str = "mac-arm64", sha: str | None = None) -> str:
        sha = sha or hashlib.sha256(name.encode()).hexdigest()
        manifest = {"dependencies": [
            {"name": "V8", "determinism": {"release_assets": {asset: {
                "url": "https://example.invalid/v8", "sha256": "0" * 64}}}},
            {"name": "Skia", "determinism": {"release_assets": {asset: {
                "url": f"https://example.invalid/{name}", "sha256": sha}}}},
        ]}
        (self.work / "deps").mkdir(exist_ok=True)
        (self.work / "deps/manifest.json").write_text(__import__("json").dumps(manifest))
        _git("add", "deps/manifest.json", cwd=self.work)
        _git("commit", "--quiet", "-m", name, cwd=self.work)
        _git("push", "--quiet", "origin", "HEAD:main", cwd=self.work)
        return sha

    def _run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SYNC), *args, "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                 "TARTCI_ARTIFACT_CACHE_GIT_BASE": str(self.base),
                 "TARTCI_ARTIFACT_CACHE_RUNNER_SH": "", **(env or {})},
        )

    def _refresh(self, **env: str) -> subprocess.CompletedProcess:
        return self._run("refresh", "--pins", str(self.pins), env=env)

    def _blobs(self) -> list[str]:
        root = self.cache / "sha256"
        return sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) if root.exists() else []

    def test_the_current_pin_is_added_then_only_reverified(self) -> None:
        sha = self._manifest("m158.zip")
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        proc = self._refresh()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._blobs(), [sha])
        self.assertEqual((self.cache / "sha256" / sha).read_bytes(), b"m158.zip")
        self.assertIn("https://example.invalid/m158.zip", self.calls.read_text())
        # Control: the V8 entry beside it, same asset key, was not taken.
        self.assertNotIn("/v8", self.calls.read_text())
        self.calls.unlink()
        proc = self._refresh()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already present", proc.stdout)
        self.assertFalse(self.calls.exists(), "an existing blob was downloaded again")

    def test_a_pin_bump_reaches_the_cache_at_the_next_refresh(self) -> None:
        old = self._manifest("m157.zip")
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        self.assertEqual(self._refresh().returncode, 0)
        new = self._manifest("m158.zip")
        proc = self._refresh()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._blobs(), sorted([old, new]))

    def test_bytes_that_miss_the_pin_never_land_and_the_mirror_still_refreshes(self) -> None:
        self._manifest("m158.zip", sha=hashlib.sha256(b"genuine").hexdigest())
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        head = self._manifest("m158b.zip", sha=hashlib.sha256(b"genuine").hexdigest())
        del head
        proc = self._refresh()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("SHA-256 mismatch", proc.stderr)
        self.assertEqual(self._blobs(), [])
        self.assertEqual(list((self.cache / "sha256").glob(".staging.*")), [])
        self.assertEqual(_git("rev-parse", "refs/heads/main", cwd=self.cache / "git/owner/repo.git"),
                         _git("rev-parse", "HEAD", cwd=self.work))
        self.assertFalse((self.cache / ".lock").exists())

    def test_a_failed_download_is_reported_and_leaves_nothing(self) -> None:
        self._manifest("m158.zip")
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        proc = self._refresh(STUB_FAIL="1")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("download failed", proc.stderr)
        self.assertEqual(self._blobs(), [])

    def test_an_unmirrored_repository_is_not_this_hosts_pin(self) -> None:
        self._manifest("m158.zip")
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        self.pins.write_text("other/repo\tmain\tdeps/manifest.json\tSkia\tmac-arm64\n")
        proc = self._refresh()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._blobs(), [])
        self.assertFalse(self.calls.exists())

    def test_an_unreadable_pin_fails_the_pass_without_downloading(self) -> None:
        self._manifest("m158.zip")
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        for line in ("owner/repo\tmain\tdeps/missing.json\tSkia\tmac-arm64\n",
                     "owner/repo\tmain\tdeps/manifest.json\tSkia\twin-x64\n",
                     "owner/repo\tmain\tdeps/manifest.json\tDawn\tmac-arm64\n",
                     "owner/repo\tmain\tdeps/manifest.json\n"):
            with self.subTest(line=line):
                self.pins.write_text(line)
                proc = self._refresh()
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertEqual(self._blobs(), [])
                self.assertFalse(self.calls.exists())

    def test_a_plain_http_or_malformed_digest_is_refused(self) -> None:
        self._manifest("m158.zip", sha="NOTAHEX")
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        proc = self._refresh()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertFalse(self.calls.exists())

    def test_no_pin_file_changes_nothing(self) -> None:
        self._manifest("m158.zip")
        self.assertEqual(self._run("git-sync", "--repo", "owner/repo").returncode, 0)
        proc = self._run("refresh", "--pins", str(self.tmp / "absent.tsv"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._blobs(), [])

    def test_the_checked_in_pin_list_names_pulps_skia_archive(self) -> None:
        rows = [line.split("\t") for line in
                (ROOT / "manifests/artifact-cache-pins.tsv").read_text().splitlines()
                if line and not line.startswith("#")]
        self.assertIn(["Generous-Corp/pulp", "main", "tools/deps/manifest.json", "Skia", "mac-arm64"], rows)
        self.assertTrue(all(len(row) == 5 for row in rows), rows)


class RunnerArchiveRefreshTests(unittest.TestCase):
    """`refresh` keeps the pinned Actions Runner archive in the cache.

    Every gate VM downloaded the runner from GitHub (9 to 14 s) and about 38 a
    day were thrown away on a GitHub 504 (2026-11-07..09). The pinned url and
    digest come from runner.sh, the file whose guest code verifies them.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cache = self.tmp / "cache"
        origin = self.tmp / "remote/owner/repo.git"
        origin.mkdir(parents=True)
        _git("init", "--quiet", "--bare", "-b", "main", str(origin))
        work = self.tmp / "work"
        _git("clone", "--quiet", str(origin), str(work))
        (work / "f").write_text("f")
        _git("add", "f", cwd=work)
        _git("commit", "--quiet", "-m", "f", cwd=work)
        _git("push", "--quiet", "origin", "HEAD:main", cwd=work)
        subprocess.run(["bash", str(SYNC), "git-sync", "--repo", "owner/repo", "--dir", str(self.cache)],
                       check=True, capture_output=True,
                       env={**os.environ, "TARTCI_ARTIFACT_CACHE_GIT_BASE": str(self.tmp / "remote")})
        self.payload = b"runner-archive-bytes"
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "curl.calls"
        curl = self.bin / "curl"
        curl.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            echo "$*" >> {self.calls}
            out=""
            while [ "$#" -gt 0 ]; do [ "$1" = "--output" ] && out="$2"; shift; done
            [ -n "${{STUB_FAIL:-}}" ] && exit 22
            printf '%s' "${{STUB_BYTES:-runner-archive-bytes}}" > "$out"
            """))
        curl.chmod(0o755)
        self.runner = self.tmp / "runner.sh"
        self._runner(f"https://example.invalid/runner.tar.gz {self.sha}")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, line: str, code: int = 0) -> None:
        self.runner.write_text(f"#!/bin/bash\n[ \"$1\" = --print-runner-archive ] || exit 9\n"
                               f"printf '%s\\n' '{line}'\nexit {code}\n")

    def _refresh(self, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SYNC), "refresh", "--pins", str(self.tmp / "none.tsv"),
             "--dir", str(self.cache)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                 "TARTCI_ARTIFACT_CACHE_RUNNER_SH": str(self.runner), **env},
        )

    def _blobs(self) -> list[str]:
        root = self.cache / "sha256"
        return sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) if root.exists() else []

    def test_the_pinned_runner_archive_is_added_then_only_reverified(self) -> None:
        proc = self._refresh()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._blobs(), [self.sha])
        self.assertEqual((self.cache / "sha256" / self.sha).read_bytes(), self.payload)
        self.assertIn("https://example.invalid/runner.tar.gz", self.calls.read_text())
        self.calls.unlink()
        proc = self._refresh()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already present", proc.stdout)
        self.assertFalse(self.calls.exists(), "an existing blob was downloaded again")

    def test_runner_bytes_that_miss_the_pin_never_land(self) -> None:
        proc = self._refresh(STUB_BYTES="a 504 error page")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SHA-256 mismatch", proc.stderr)
        self.assertEqual(self._blobs(), [])

    def test_a_failed_runner_download_leaves_nothing(self) -> None:
        proc = self._refresh(STUB_FAIL="1")
        self.assertIn("download failed", proc.stderr)
        self.assertEqual(self._blobs(), [])

    def test_an_unreadable_runner_pin_fails_the_pass_without_downloading(self) -> None:
        for line, code in (("not an archive line", 0), ("https://x.invalid/a " + "G" * 64, 0),
                           ("http://x.invalid/a " + self.sha, 0), ("", 1)):
            with self.subTest(line=line, code=code):
                self._runner(line, code)
                proc = self._refresh()
                self.assertEqual(proc.returncode, 1)
                self.assertIn("could not read the pinned runner archive", proc.stderr)
                self.assertEqual(self._blobs(), [])
                self.assertFalse(self.calls.exists())

    def test_a_host_that_mirrors_nothing_is_untouched(self) -> None:
        shutil.rmtree(self.cache / "git/owner/repo.git")
        proc = self._refresh()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self._blobs(), [])
        self.assertFalse(self.calls.exists())

    def test_the_real_pin_is_the_default_archive_the_guest_verifies(self) -> None:
        source = MAC_JIT.read_text()
        version = re.search(r"^DEFAULT_RUNNER_VERSION=(\S+)$", source, re.M).group(1)
        digest = re.search(r"^DEFAULT_RUNNER_SHA256=([0-9a-f]{64})$", source, re.M).group(1)
        out = subprocess.run(["bash", str(MAC_JIT), "--print-runner-archive"], check=True,
                             capture_output=True, text=True, cwd=ROOT).stdout.strip()
        self.assertEqual(out, "https://github.com/actions/runner/releases/download/"
                              f"v{version}/actions-runner-osx-arm64-{version}.tar.gz {digest}")

    def test_an_override_digest_is_published_lowercase(self) -> None:
        out = subprocess.run(
            ["bash", str(MAC_JIT), "--print-runner-archive"], check=True, capture_output=True,
            text=True, cwd=ROOT,
            env={**os.environ, "TARTCI_RUNNER_VERSION": "9.8.7", "TARTCI_RUNNER_SHA256": "AB" * 32},
        ).stdout.strip()
        self.assertTrue(out.endswith("actions-runner-osx-arm64-9.8.7.tar.gz " + "ab" * 32), out)


class GuestRunnerInstallTests(unittest.TestCase):
    """The in-VM runner install takes the cached archive only when it verifies.

    Runs the real guest script out of runner.sh with a stub curl and a fake
    Runner.Listener, so the cache path and the download fallback are both
    exercised end to end.
    """

    VERSION = "9.9.9"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cache = self.tmp / "cache"
        (self.cache / "sha256").mkdir(parents=True)
        self.archive = self._tarball(self.VERSION)
        self.sha = hashlib.sha256(self.archive).hexdigest()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "curl.calls"
        self.download = self.tmp / "download.tar.gz"
        self.download.write_bytes(self.archive)
        curl = self.bin / "curl"
        curl.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            echo "$*" >> {self.calls}
            out=""
            while [ "$#" -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
            [ -z "${{STUB_FAIL:-}}" ] || exit 22
            cp {self.download} "$out"
            """))
        curl.chmod(0o755)
        source = MAC_JIT.read_text()
        start = source.index("ensure_runner_version(){")
        body = source[source.index("<<'GUEST'\n", start) + len("<<'GUEST'\n"):]
        self.guest = body[:body.index("\nGUEST\n")]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _tarball(version: str) -> bytes:
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            script = f"#!/bin/sh\necho {version}\n".encode()
            info = tarfile.TarInfo("bin/Runner.Listener")
            info.size, info.mode = len(script), 0o755
            tar.addfile(info, io.BytesIO(script))
        return buf.getvalue()

    def _install(self, cache_dir: str | None = None, sha: str | None = None,
                 **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/bash", "-s", "--", self.VERSION, (sha or self.sha).upper(), "",
             "https://example.invalid/runner.tar.gz",
             str(self.cache) if cache_dir is None else cache_dir],
            input=self.guest, capture_output=True, text=True, check=False,
            env={**os.environ, "HOME": str(self.home), "PATH": f"{self.bin}:{os.environ['PATH']}", **env},
        )

    def _cache(self, data: bytes, name: str | None = None) -> None:
        (self.cache / "sha256" / (name or self.sha)).write_bytes(data)

    def _installed(self) -> bool:
        return (self.home / "actions-runner/bin/Runner.Listener").exists()

    def test_a_verified_cached_archive_is_used_without_downloading(self) -> None:
        self._cache(self.archive)
        proc = self._install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("actions-runner-archive=cache", proc.stdout)
        self.assertIn(f"actions-runner-version={self.VERSION}", proc.stdout)
        self.assertTrue(self._installed())
        self.assertFalse(self.calls.exists(), "the guest downloaded despite a cached archive")

    def test_an_absent_cache_entry_falls_back_to_the_download(self) -> None:
        proc = self._install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("actions-runner-archive=download", proc.stdout)
        self.assertTrue(self.calls.exists())
        self.assertTrue(self._installed())

    def test_no_mounted_cache_falls_back_to_the_download(self) -> None:
        self._cache(self.archive)
        proc = self._install(cache_dir="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("actions-runner-archive=download", proc.stdout)
        self.assertTrue(self.calls.exists())

    def test_a_cached_blob_that_does_not_hash_to_its_name_is_never_installed(self) -> None:
        self._cache(self._tarball("1.1.1"))
        proc = self._install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("actions-runner-archive=download", proc.stdout)
        self.assertNotIn("archive=cache", proc.stdout)
        self.assertTrue(self.calls.exists())
        self.assertIn(f"actions-runner-version={self.VERSION}", proc.stdout)

    def test_a_bad_cache_and_a_bad_download_still_fail_closed(self) -> None:
        self._cache(b"rot")
        self.download.write_bytes(b"a 504 error page")
        proc = self._install()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not match expected", proc.stderr)
        self.assertFalse(self._installed())

    def test_an_unreadable_cache_entry_falls_back_to_the_download(self) -> None:
        # A directory under the digest name: present, but not a readable file.
        (self.cache / "sha256" / self.sha).mkdir()
        proc = self._install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("actions-runner-archive=download", proc.stdout)

    def test_a_current_runner_needs_neither_cache_nor_download(self) -> None:
        self.assertEqual(self._install().returncode, 0)
        self.calls.unlink()
        self._cache(self.archive)
        proc = self._install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("actions-runner-archive", proc.stdout)
        self.assertFalse(self.calls.exists())

    def test_the_host_hands_the_guest_the_cache_only_when_the_vm_mounted_it(self) -> None:
        source = MAC_JIT.read_text()
        start = source.index("ensure_runner_version(){")
        host = source[start:source.index("<<'GUEST'", start)]
        self.assertIn('[ "$CURRENT_ARTIFACT_CACHE" != 1 ] || runner_cache_dir="$GUEST_ARTIFACT_CACHE"', host)
        self.assertIn("'$RUNNER_ARCHIVE_URL' '$runner_cache_dir'", host)
        # The mount is declared before the runner is checked, so the flag is current.
        self.assertLess(source.index("CURRENT_ARTIFACT_CACHE=1\n  fi\n  tartci_vm_lease_guard_exec"),
                        source.index("if ! ensure_runner_version"))


if __name__ == "__main__":
    unittest.main()
