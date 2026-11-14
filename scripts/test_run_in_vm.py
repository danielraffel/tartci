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
        sys.exit(int(os.environ["FAKE_CLONE_RC"]))
    vms[args[1]] = "stopped"; save(vms)
elif cmd == "set":
    pass
elif cmd == "run":
    name = args[-1]
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
    sys.stdout.write(cfg.get("patch", "")); sys.exit(0)
if "tar -cf -" in remote:
    sys.stdout.write("x" * cfg.get("tar_bytes", 10)); sys.exit(0)
if "exec " in remote:
    time.sleep(cfg.get("cmd_sleep", 0))
    sys.stdout.write("guest stdout\n")
    sys.exit(cfg.get("cmd_rc", 0))
sys.exit(99)
'''


def make_exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class RunInVmTest(unittest.TestCase):
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
        self.assertIn(f"--dir=src:{self.src.resolve()}:ro", dirs)
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
