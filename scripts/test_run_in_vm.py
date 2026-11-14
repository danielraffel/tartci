#!/usr/bin/env python3
"""`tartci run-in-vm` end to end against a fake tart, a fake ssh and a real
lease store in a temporary directory. No VM is booted."""
from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import run_in_vm  # noqa: E402

FAKE_TART = r'''#!/usr/bin/env python3
import json, os, sys, time
state = os.environ["FAKE_STATE"]
db = os.path.join(state, "vms.json")
def load():
    try:
        return json.load(open(db))
    except (OSError, ValueError):
        return {}
def save(d):
    tmp = db + ".tmp"; json.dump(d, open(tmp, "w")); os.replace(tmp, db)
with open(os.path.join(state, "tart.log"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
cmd, args = sys.argv[1], sys.argv[2:]
vms = load()
if cmd == "list":
    print(json.dumps([{"Name": n, "State": v, "OS": "darwin", "Source": "local"} for n, v in vms.items()]))
elif cmd == "clone":
    if os.environ.get("FAKE_CLONE_RC", "0") != "0":
        sys.stderr.write("clone \x1b[31mexploded\x1b[0m\n")
        sys.exit(int(os.environ["FAKE_CLONE_RC"]))
    if os.environ.get("FAKE_CLONE_ADDS_GATE") == "1":
        vms["gate-vm-late"] = "running"
    vms[args[1]] = "stopped"; save(vms)
    if os.environ.get("FAKE_CLONE_SLEEP"):
        open(os.path.join(state, "clone.pid"), "w").write(str(os.getpid()))
        time.sleep(float(os.environ["FAKE_CLONE_SLEEP"]))
elif cmd == "set":
    pass
elif cmd == "run":
    name = args[-1]
    if os.environ.get("FAKE_RUN_FAIL") == "1":
        sys.stdout.write("Error: The number of VMs exceeds the system limit\n")
        sys.exit(1)
    for a in args:
        if a.startswith("--dir=src:"):
            root = a[len("--dir=src:"):].rsplit(":", 1)[0]
            listing = {}
            for dirpath, dirnames, filenames in os.walk(root):
                for n in dirnames + filenames:
                    full = os.path.join(dirpath, n)
                    rel = os.path.relpath(full, root)
                    if os.path.islink(full):
                        listing[rel] = "link:" + os.readlink(full)
                    elif os.path.isfile(full):
                        listing[rel] = open(full).read()
            open(os.path.join(state, "src-listing.json"), "w").write(json.dumps(listing))
    vms[name] = "running"; save(vms)
    open(os.path.join(state, "run-argv.json"), "w").write(json.dumps(sys.argv[1:]))
    while load().get(name) == "running":
        time.sleep(0.1)
elif cmd == "ip":
    if load().get(args[0]) == "running":
        print("192.168.64.77")
    else:
        sys.exit(1)
elif cmd == "stop":
    if args[0] in vms:
        vms[args[0]] = "stopped"; save(vms)
elif cmd == "delete":
    if os.environ.get("FAKE_DELETE_FAILS") != "1":
        vms.pop(args[0], None); save(vms)
elif cmd == "get":
    print(json.dumps({"OS": "darwin"}))
'''

FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, sys, time
state = os.environ.get("FAKE_STATE") or open(os.path.join(os.path.dirname(__file__), "state-path")).read().strip()
remote = sys.argv[-1]
data = sys.stdin.buffer.read() if remote == "/bin/bash -s" else b""
with open(os.path.join(state, "ssh.log"), "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ),
                          "stdin": data.decode(errors="replace")}) + "\n")
cfg = json.load(open(os.path.join(state, "ssh-config.json")))
if remote == "true":
    sys.exit(0)
if remote == "/bin/bash -s":
    sys.exit(cfg.get("prep_rc", 0))
if "git diff --cached --binary" in remote:
    sys.stderr.write("w" * cfg.get("patch_stderr_bytes", 0)); sys.stderr.flush()
    sys.stdout.write(cfg.get("patch", "")); sys.exit(0)
if "tar -cf -" in remote:
    sys.stdout.write("x" * cfg.get("tar_bytes", 10)); sys.exit(0)
if "exec " in remote:
    time.sleep(cfg.get("cmd_sleep", 0))
    sys.stdout.write("guest stdout\n")
    sys.stdout.write("y" * cfg.get("out_bytes", 0))
    sys.exit(cfg.get("cmd_rc", 0))
sys.exit(99)
'''


def make_exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rivm-test-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)], check=False)
        self.bin = self.tmp / "bin"
        self.state = self.tmp / "state"
        self.home = self.tmp / "home"
        self.store = self.tmp / "tart"
        self.cache = self.tmp / "cache"
        for d in (self.bin, self.state, self.home, self.store / "vms" / "golden", self.cache):
            d.mkdir(parents=True)
        make_exe(self.bin / "tart", FAKE_TART)
        make_exe(self.bin / "ssh", FAKE_SSH)
        (self.bin / "state-path").write_text(str(self.state))
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "a.txt").write_text("a\n")
        git = ["git", "-C", str(self.src), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", str(self.src)], check=True)
        subprocess.run([*git, "add", "a.txt"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
        self.ssh_config({})

    def ssh_config(self, cfg: dict) -> None:
        (self.state / "ssh-config.json").write_text(json.dumps(cfg))

    def env(self, **extra: str) -> dict:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home),
            "TART_HOME": str(self.store),
            "TARTCI_HOME": str(self.home / ".tartci"),
            "TARTCI_LEASE_DIR": str(self.home / "leases"),
            "TARTCI_CI_CACHE": str(self.cache),
            "TARTCI_TART": str(self.bin / "tart"),
            "TARTCI_TART_BIN": str(self.bin / "tart"),
            "TARTCI_SSH": str(self.bin / "ssh"),
            "TARTCI_VM_SSH_KEY": str(self.tmp / "key"),
            "FAKE_STATE": str(self.state),
            "SSH_AUTH_SOCK": "/tmp/should-never-reach-ssh",
            "TARTCI_HOME_VOLUME_FLOOR": "0",
        }
        env.update(extra)
        return env

    def run_cli(self, *args: str, timeout: float = 120, **extra: str) -> subprocess.CompletedProcess:
        argv = [sys.executable, str(HERE / "run_in_vm.py"), "--src", str(self.src),
                "--owner", "tester", "--result-json", str(self.tmp / "result.json"), *args]
        return subprocess.run(argv, env=self.env(**extra), capture_output=True, text=True,
                              timeout=timeout, check=False)

    def result(self) -> dict:
        return json.loads((self.tmp / "result.json").read_text())

    def ssh_calls(self) -> list:
        path = self.state / "ssh.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def vms(self) -> dict:
        try:
            return json.loads((self.state / "vms.json").read_text())
        except OSError:
            return {}

    def leases(self) -> list:
        path = self.home / "leases" / "leases.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text())
        return data.get("leases", data) if isinstance(data, dict) else data

    def assert_clean(self) -> None:
        self.assertEqual([n for n in self.vms() if n.startswith("scratch-rivm-")], [])
        self.assertEqual([r for r in self.leases() if "rivm" in json.dumps(r)], [])
        records = self.home / ".tartci" / "state" / "scratch-vms"
        self.assertEqual(list(records.glob("*.json")) if records.exists() else [], [])


class RunInVmTest(Harness):
    # ── the happy path ─────────────────────────────────────────────────────

    def test_success_streams_patch_and_tears_down(self) -> None:
        shared = self.cache / "ccache-layers-v1" / "shared"
        shared.mkdir(parents=True)
        (self.cache / "configure-checks").mkdir()
        self.ssh_config({"patch": "diff --git a/a.txt b/a.txt\n"})
        proc = self.run_cli("--patch-out", str(self.tmp / "out.patch"), "--",
                            "tools/scripts/gates.sh", "origin/main", "it's; $(rm -rf /)")
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertEqual((self.tmp / "out.patch").read_text(), "diff --git a/a.txt b/a.txt\n")
        self.assertIn("guest stdout", proc.stdout)
        self.assertEqual(self.result()["guest_rc"], 0)
        self.assertEqual(self.result()["teardown"], "proved")
        self.assert_clean()

        argv = json.loads((self.state / "run-argv.json").read_text())
        dirs = [a for a in argv if a.startswith("--dir=")]
        src_dirs = [d for d in dirs if d.startswith("--dir=src:")]
        self.assertEqual(len(src_dirs), 1)
        # The live work tree is never mounted: only a host-side export of it,
        # on the cache volume, removed with the run.
        self.assertNotIn(f"--dir=src:{self.src.resolve()}:ro", dirs)
        self.assertTrue(src_dirs[0].startswith(f"--dir=src:{self.cache / 'run-in-vm'}/run-"))
        self.assertEqual(list((self.cache / "run-in-vm").glob("run-*")), [])
        self.assertIn(f"--dir=ccache-shared:{shared}:ro", dirs)
        self.assertTrue(any(d.startswith("--dir=gitbundle:") and d.endswith(":ro") for d in dirs))
        for d in dirs:
            self.assertTrue(d.endswith(":ro"), f"writable mount: {d}")
            self.assertNotIn("configure-checks", d)
            self.assertNotIn(f"{self.cache}/ccache:", d)
            self.assertNotIn(str(self.home), d.split(":", 1)[1].replace(str(self.home / ".tartci"), ""))

        calls = self.ssh_calls()
        self.assertTrue(calls)
        for call in calls:
            argv = call["argv"]
            self.assertNotIn("-A", argv)
            joined = " ".join(argv)
            for option in ("-F /dev/null", "IdentitiesOnly=yes", "IdentityAgent=none",
                           "ForwardAgent=no", "ClearAllForwardings=yes", "ControlPath=none"):
                self.assertIn(option, joined)
            self.assertNotIn("SSH_AUTH_SOCK", call["env"])
            # Nothing from the caller's environment reaches ssh (the fake's own
            # interpreter may add a few of its own, such as SDKROOT).
            for leaked in ("FAKE_STATE", "TARTCI_LEASE_DIR", "TART_HOME", "SSH_AGENT_PID"):
                self.assertNotIn(leaked, call["env"])
        # The command reaches the guest as one exactly-quoted argv.
        run_call = [c for c in calls if "exec " in c["argv"][-1]][0]
        script = shlex.split(run_call["argv"][-1])[2]
        self.assertTrue(script.endswith("exec tools/scripts/gates.sh origin/main "
                                        + shlex.quote("it's; $(rm -rf /)")))
        prep = [c for c in calls if c["argv"][-1] == "/bin/bash -s"][0]["stdin"]
        self.assertIn("|read-only", prep)
        self.assertIn("--exclude=/.git", prep)
        self.assertIn('git fetch -q "$SHARED/gitbundle/src.bundle"', prep)

    def test_no_shared_store_means_no_ccache_mount(self) -> None:
        (self.cache / "ccache").mkdir()
        proc = self.run_cli("--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        argv = json.loads((self.state / "run-argv.json").read_text())
        self.assertFalse([a for a in argv if "ccache" in a])
        prep = [c for c in self.ssh_calls() if c["argv"][-1] == "/bin/bash -s"][0]["stdin"]
        self.assertNotIn("CCACHE_REMOTE_STORAGE", prep)

    def test_lease_is_background_priority_and_held_during_the_run(self) -> None:
        self.ssh_config({"cmd_sleep": 3})
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "run_in_vm.py"), "--src", str(self.src), "--owner",
             "tester", "--", "true"], env=self.env(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            seen = []
            for _ in range(100):
                seen = [r for r in self.leases() if "rivm" in json.dumps(r)]
                if seen and any(n.startswith("scratch-rivm-") for n in self.vms()):
                    break
                time.sleep(0.1)
            self.assertTrue(seen)
            self.assertEqual(seen[0]["priority"], 10)
            self.assertEqual(seen[0]["command_kind"], "tart-macos-vm")
            records = list((self.home / ".tartci" / "state" / "scratch-vms").glob("scratch-rivm-*.json"))
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text())
            self.assertEqual(record["owner"], "run-in-vm:tester")
            self.assertGreater(record["expires_at"], time.time())
        finally:
            out, err = proc.communicate(timeout=120)
        self.assertEqual(proc.returncode, 0, err)
        self.assert_clean()

    # ── failures and their codes ───────────────────────────────────────────

    def test_command_failure_is_1_and_still_collects(self) -> None:
        self.ssh_config({"cmd_rc": 3, "patch": "p"})
        proc = self.run_cli("--patch-out", str(self.tmp / "o.patch"), "--", "false")
        self.assertEqual(proc.returncode, 1, proc.stderr[-3000:])
        self.assertEqual(self.result()["guest_rc"], 3)
        self.assertEqual((self.tmp / "o.patch").read_text(), "p")
        self.assert_clean()

    def test_timeout_tears_down_and_returns_124(self) -> None:
        self.ssh_config({"cmd_sleep": 60})
        start = time.monotonic()
        proc = self.run_cli("--timeout", "8", "--", "sleep", "60")
        self.assertEqual(proc.returncode, 124, proc.stderr[-3000:])
        self.assertLess(time.monotonic() - start, 50)
        self.assert_clean()

    def test_boot_failure_is_76_and_discards(self) -> None:
        proc = self.run_cli("--", "true", FAKE_CLONE_RC="1")
        self.assertEqual(proc.returncode, 76, proc.stderr[-3000:])
        self.assert_clean()

    def test_prep_failure_is_76_and_discards(self) -> None:
        self.ssh_config({"prep_rc": 1})
        proc = self.run_cli("--", "true")
        self.assertEqual(proc.returncode, 76, proc.stderr[-3000:])
        self.assert_clean()

    def test_ssh_connection_loss_is_76(self) -> None:
        self.ssh_config({"cmd_rc": 255})
        proc = self.run_cli("--", "true")
        self.assertEqual(proc.returncode, 76, proc.stderr[-3000:])
        self.assert_clean()

    def test_oversized_output_is_77(self) -> None:
        self.ssh_config({"tar_bytes": 5000})
        proc = self.run_cli("--artifacts-out", str(self.tmp / "a.tar"), "--artifact", "build/x",
                            "--max-output-bytes", "1000", "--", "true")
        self.assertEqual(proc.returncode, 77, proc.stderr[-3000:])
        self.assertFalse((self.tmp / "a.tar").exists())
        self.assert_clean()

    def test_unproved_delete_is_78_and_keeps_the_ttl_record(self) -> None:
        proc = self.run_cli("--", "true", FAKE_DELETE_FAILS="1")
        self.assertEqual(proc.returncode, 78, proc.stderr[-3000:])
        records = list((self.home / ".tartci" / "state" / "scratch-vms").glob("scratch-rivm-*.json"))
        self.assertEqual(len(records), 1, "the watchdog needs the record to delete the VM")

    def test_sigterm_tears_down_and_returns_130(self) -> None:
        self.ssh_config({"cmd_sleep": 60})
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "run_in_vm.py"), "--src", str(self.src), "--owner",
             "tester", "--", "sleep", "60"], env=self.env(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        for _ in range(200):
            if any("exec " in c["argv"][-1] for c in self.ssh_calls()):
                break
            time.sleep(0.1)
        proc.terminate()
        _, err = proc.communicate(timeout=120)
        self.assertEqual(proc.returncode, 130, err)
        self.assert_clean()

    # ── admission ──────────────────────────────────────────────────────────

    def test_second_run_on_the_host_is_refused(self) -> None:
        lock = self.home / ".tartci" / "state" / "run-in-vm" / "host.lock"
        lock.parent.mkdir(parents=True)
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, os, time
            fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT)
            fcntl.flock(fd, fcntl.LOCK_EX)
            print("held", flush=True)
            time.sleep(60)
        """)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        assert holder.stdout
        holder.stdout.readline()
        proc = self.run_cli("--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr[-3000:])
        self.assertIn("another run-in-vm", proc.stderr[-3000:])
        self.assertFalse((self.state / "tart.log").exists() and "clone" in (self.state / "tart.log").read_text())

    def test_leftover_run_in_vm_vm_is_refused(self) -> None:
        (self.state / "vms.json").write_text(json.dumps({"scratch-rivm-old-1": "running"}))
        proc = self.run_cli("--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr[-3000:])

    def test_keeps_a_slot_free_for_the_gates(self) -> None:
        (self.state / "vms.json").write_text(json.dumps({"gate-vm-1": "running"}))
        proc = self.run_cli("--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr[-3000:])
        self.assertIn("free for the gates", proc.stderr[-3000:])

    def test_drained_pool_is_refused(self) -> None:
        cfg = self.home / ".config" / "tartci"
        cfg.mkdir(parents=True)
        (cfg / "pool-state").write_text("draining\n")
        proc = self.run_cli("--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr[-3000:])

    def test_usage_errors_are_2_and_start_nothing(self) -> None:
        for args in ([], ["--artifact", "../x", "--artifacts-out", str(self.tmp / "a"), "--", "true"],
                     ["--timeout", "999999", "--", "true"]):
            with self.subTest(args=args):
                proc = self.run_cli(*args)
                self.assertEqual(proc.returncode, 2, proc.stderr[-3000:])
        self.assertFalse((self.state / "tart.log").exists())


class ReviewFixTest(Harness):
    """The failure modes the adversarial review named, each driven for real."""

    def records(self) -> list:
        d = self.home / ".tartci" / "state" / "scratch-vms"
        return list(d.glob("scratch-rivm-*.json")) if d.exists() else []

    def test_export_hides_git_ignored_and_untracked_secrets(self) -> None:
        (self.src / ".gitignore").write_text("build/\n.env\n")
        (self.src / "build").mkdir()
        (self.src / "build" / "obj.o").write_text("o")
        (self.src / ".env").write_text("TOKEN=secret")
        (self.src / "new.txt").write_text("untracked but not ignored\n")
        (self.src / "a.txt").write_text("modified\n")
        (self.src / "skip").mkdir()
        (self.src / "skip" / "x.txt").write_text("x")
        os.symlink(str(self.home), self.src / "home-link")
        proc = self.run_cli("--exclude", "/skip", "--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        listing = json.loads((self.state / "src-listing.json").read_text())
        self.assertFalse([k for k in listing if k == ".git" or k.startswith(".git/")], listing)
        for hidden in ("build", "build/obj.o", ".env", "skip", "skip/x.txt"):
            self.assertNotIn(hidden, listing)
        self.assertEqual(listing["a.txt"], "modified\n")
        self.assertEqual(listing["new.txt"], "untracked but not ignored\n")
        # Copied as a link, never followed into the host home.
        self.assertEqual(listing["home-link"], f"link:{self.home}")
        self.assert_clean()

    def test_clone_timeout_kills_the_writer_and_does_not_brick_the_host(self) -> None:
        start = time.monotonic()
        proc = self.run_cli("--timeout", "6", "--", "true", FAKE_CLONE_SLEEP="120")
        self.assertEqual(proc.returncode, 124, proc.stderr[-3000:])
        self.assertLess(time.monotonic() - start, 60)
        pid = int((self.state / "clone.pid").read_text())
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assert_clean()
        again = self.run_cli("--", "true")
        self.assertEqual(again.returncode, 0, again.stderr[-3000:])

    def test_clone_timeout_with_unproved_delete_keeps_the_record(self) -> None:
        proc = self.run_cli("--timeout", "6", "--", "true", FAKE_CLONE_SLEEP="120",
                            FAKE_DELETE_FAILS="1")
        self.assertEqual(proc.returncode, 78, proc.stderr[-3000:])
        self.assertEqual(len(self.records()), 1)

    def test_boot_failure_reason_carries_the_tart_run_log(self) -> None:
        proc = self.run_cli("--", "true", FAKE_RUN_FAIL="1")
        self.assertEqual(proc.returncode, 76, proc.stderr[-3000:])
        self.assertIn("exceeds the system limit", self.result()["reason"])
        self.assertIn("exceeds the system limit", self.result()["tart_run_log_tail"])
        self.assert_clean()

    def test_reason_has_no_terminal_escapes(self) -> None:
        proc = self.run_cli("--", "true", FAKE_CLONE_RC="1")
        self.assertEqual(proc.returncode, 76, proc.stderr[-3000:])
        reason = self.result()["reason"]
        self.assertIn("exploded", reason)
        self.assertNotIn("\x1b", reason)

    def test_heavy_stderr_on_collect_does_not_stall(self) -> None:
        self.ssh_config({"patch": "p", "patch_stderr_bytes": 4 * 1024 * 1024})
        start = time.monotonic()
        proc = self.run_cli("--timeout", "60", "--patch-out", str(self.tmp / "o.patch"),
                            "--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertLess(time.monotonic() - start, 45)
        self.assertEqual((self.tmp / "o.patch").read_text(), "p")

    def test_gate_that_starts_during_the_clone_refuses_before_boot(self) -> None:
        proc = self.run_cli("--", "true", FAKE_CLONE_ADDS_GATE="1")
        self.assertEqual(proc.returncode, 75, proc.stderr[-3000:])
        self.assertIn("free for the gates", self.result()["reason"])
        self.assertFalse((self.state / "run-argv.json").exists(), "tart run must not start")
        self.assert_clean()

    def test_gate_waiter_preempts_the_run(self) -> None:
        import leases  # noqa: PLC0415

        lease_dir = self.home / "leases"
        lease_dir.mkdir(parents=True, exist_ok=True)
        me = leases.process_identity(os.getpid())
        (lease_dir / "waiters.json").write_text(json.dumps([{
            "id": "vm-tart-macos-vm-gate-waiting", "command_kind": "tart-macos-vm",
            "priority": 100, "priority_class": "gate", "lease_size_cores": 7,
            "pid": me["pid"], "process_start_time": me["process_start_time"],
            "host_boot_time": me["host_boot_time"],
            "seen_at": leases.iso(leases.utcnow()), "waiting_since": leases.iso(leases.utcnow()),
        }]))
        self.ssh_config({"cmd_sleep": 60})
        start = time.monotonic()
        proc = self.run_cli("--", "sleep", "60", TARTCI_RIVM_HEARTBEAT_SECS="1")
        self.assertEqual(proc.returncode, 79, proc.stderr[-3000:])
        self.assertLess(time.monotonic() - start, 45)
        self.assertIn("gate-waiting", self.result()["reason"])
        self.assert_clean()

    def test_guest_output_is_capped(self) -> None:
        self.ssh_config({"out_bytes": 200_000})
        proc = self.run_cli("--max-log-bytes", "1000", "--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertLessEqual(len(proc.stdout.encode()), 1000)
        self.assertTrue(self.result().get("log_truncated"))

    def test_stale_scratch_dir_of_a_dead_run_is_swept(self) -> None:
        stale = self.cache / "run-in-vm" / "run-old"
        (stale / "src").mkdir(parents=True)
        true_proc = subprocess.Popen(["true"]); true_proc.wait()
        (stale / "owner.json").write_text(json.dumps({"pid": true_proc.pid, "pid_start": "x"}))
        proc = self.run_cli("--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertFalse(stale.exists())

    def leftover(self, pid: int, start: str) -> None:
        (self.state / "vms.json").write_text(json.dumps({"scratch-rivm-old-1": "running"}))
        d = self.home / ".tartci" / "state" / "scratch-vms"
        d.mkdir(parents=True, exist_ok=True)
        (d / "scratch-rivm-old-1.json").write_text(json.dumps({
            "name": "scratch-rivm-old-1", "owner": "run-in-vm:old", "store": str(self.store),
            "from": "golden", "created_at": time.time(), "expires_at": time.time() + 999,
            "warned_at": None, "pid": pid, "pid_start": start}))

    def test_leftover_with_a_dead_owner_is_reclaimed(self) -> None:
        dead = subprocess.Popen(["true"]); dead.wait()
        self.leftover(dead.pid, "gone")
        proc = self.run_cli("--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertNotIn("scratch-rivm-old-1", self.vms())
        self.assert_clean()

    def test_leftover_with_a_live_owner_is_still_refused(self) -> None:
        self.leftover(os.getpid(), run_in_vm.pid_start(os.getpid()))
        proc = self.run_cli("--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr[-3000:])
        self.assertIn("scratch-rivm-old-1", self.vms())


class PureTest(unittest.TestCase):
    def test_safe_artifact(self) -> None:
        self.assertTrue(run_in_vm.safe_artifact("build/out.json"))
        for bad in ("/etc/passwd", "../x", "a/../../b", ""):
            self.assertFalse(run_in_vm.safe_artifact(bad), bad)

    def test_exit_codes_are_distinct_and_documented(self) -> None:
        self.assertEqual(len(set(run_in_vm.EXIT_CODES)), len(run_in_vm.EXIT_CODES))
        runbook = (ROOT / "docs" / "runbook.md").read_text()
        for code in run_in_vm.EXIT_CODES:
            self.assertIn(f"| {code} |", runbook)

    def test_cli_dispatches(self) -> None:
        text = (ROOT / "tartci").read_text()
        self.assertIn('run-in-vm) shift; tartci_verified_python_run "$HERE/scripts/run_in_vm.py"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
