#!/usr/bin/env python3
"""Failed gate VMs kept for debugging (debug_hold.py, debug-hold.lib.sh)."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import debug_hold as dh  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers" / "tart-macos" / "debug-hold.lib.sh"
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
T0 = 2_000_000_000.0
ON = {"enabled": True, "ttl_hours": 24, "max_per_host": 3, "min_free_gb": 200,
      "inspect_idle_minutes": 60}


class FakeSystem(dh.System):
    def __init__(self) -> None:
        self.t = T0
        self.vms: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.spawned: list[list[str]] = []
        self.ssh_fails = 0
        self.prs: dict[int, str] = {}
        self.free = 500 * dh.GIB

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def free_bytes(self, path: str):
        return self.free

    def spawn(self, argv, log) -> int:
        self.spawned.append(argv)
        self.vms[argv[-1]] = "running"
        return 1

    def run(self, argv, timeout=60):
        self.calls.append(argv)
        if argv[:2] == ["tart", "list"]:
            return 0, json.dumps([{"Name": n, "State": s, "Source": "local"}
                                  for n, s in self.vms.items()]), ""
        if argv[:2] == ["tart", "stop"]:
            self.vms[argv[2]] = "stopped"
            return 0, "", ""
        if argv[:2] == ["tart", "delete"]:
            self.vms.pop(argv[2], None)
            return 0, "", ""
        if argv[:2] == ["tart", "ip"]:
            return 0, "192.168.64.9\n", ""       # answers at once, even stale
        if argv[0] == "ssh":
            if self.ssh_fails:
                self.ssh_fails -= 1
                return 255, "", "refused"
            return 0, "", ""
        if argv[1:3] == ["api", "graphql"]:
            return 0, json.dumps({"data": {"repository": {
                f"p{n}": {"state": s} for n, s in self.prs.items()}}}), ""
        if argv[1] == "api":
            return 0, json.dumps({"head_sha": "abc", "pull_requests": [],
                                  "head_branch": "gh-readonly-queue/main/pr-9876-deadbeef"}), ""
        return 1, "", "unexpected"


class DebugHold(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = self.tmp / "debug-hold"
        self.sys = FakeSystem()
        cap = self.tmp / "cap"
        cap.write_text("2\n")
        env = {"TARTCI_DEBUG_HOLD_DIR": str(self.dir), "TARTCI_MACOS_VM_CAP_FILE": str(cap),
               "TARTCI_FLEET_PROFILE": str(self.tmp / "missing.toml"), "TARTCI_GH_CLI": "ghapp"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def keep(self, name: str = "held-studio-pulp-gate-01-1-1", pr: int | None = 9876,
             at: float = T0) -> str:
        self.sys.vms[name] = "stopped"
        dh._write(self.dir / "held" / f"{name}.json", {
            "name": name, "repo": "Generous-Corp/pulp", "prs": [pr] if pr else [],
            "created_at": at, "expires_at": at + 24 * 3600, "run_id": "1", "job_id": "2"})
        return name

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", new=io.StringIO()):
            rc = dh.main(list(argv), self.sys)
        return rc, out.getvalue()

    # settings and admission
    def test_off_by_default_and_enabled_needs_a_measured_floor(self):
        self.assertEqual(dh.load_settings(self.tmp / "missing.toml")[0]["enabled"], False)
        self.assertEqual(dh.validate_table({"enabled": True}),
                         ["debug_hold.min_free_gb is required when enabled = true"])
        self.assertEqual(dh.validate_table(ON), [])
        self.assertIn("unknown debug_hold keys: ['evict']", dh.validate_table({"evict": 1}))
        self.assertEqual(dh.admit({"enabled": False}, [], 1 << 50), (False, "disabled"))
        self.assertEqual(dh.admit(None, [], 1 << 50), (False, "settings_unreadable"))

    def test_the_cap_refuses_and_never_evicts_an_older_hold(self):
        held = [{"name": f"held-{i}"} for i in range(3)]
        ok, why = dh.admit(ON, held, 500 * dh.GIB)
        self.assertEqual((ok, why), (False, "cap max_per_host=3 held=3"))
        self.assertEqual(dh.admit(ON, held[:2], 500 * dh.GIB), (True, "ok"))

    def test_the_disk_floor_refuses(self):
        self.assertEqual(dh.admit(ON, [], 199 * dh.GIB),
                         (False, "disk free_gb=199 min_free_gb=200"))
        self.assertEqual(dh.admit(ON, [], None), (False, "free_disk_unknown"))

    def test_record_finds_the_merge_queue_pr(self):
        rc, out = self.cli("record", "--name", "held-v", "--vm", "v", "--repo",
                           "Generous-Corp/pulp", "--run-id", "11", "--job-id", "22")
        self.assertEqual(rc, 0)
        value = json.loads(out)
        self.assertEqual((value["prs"], value["mechanism"]), ([9876], "stop"))
        self.assertEqual(value["expires_at"] - value["created_at"], 24 * 3600)
        self.assertEqual(dh.pr_numbers({"pull_requests": [{"number": 5}], "head_branch": "x"}), [5])

    # the slot
    def test_a_stopped_held_vm_frees_its_slot_and_a_running_one_takes_one(self):
        import tart_inventory
        rows = [{"Name": "held-a", "State": "stopped", "OS": "darwin", "Source": "local"},
                {"Name": "studio-pulp-gate-01-1-2", "State": "running", "OS": "darwin",
                 "Source": "local"}]
        with mock.patch.object(tart_inventory, "run_bounded", return_value=json.dumps(rows)):
            self.assertEqual(tart_inventory.count_running_macos(5), 1)
        rows[0]["State"] = "running"
        with mock.patch.object(tart_inventory, "run_bounded", return_value=json.dumps(rows)):
            self.assertEqual(tart_inventory.count_running_macos(5), 2)

    def test_inspect_is_refused_when_no_slot_is_free(self):
        name = self.keep()
        self.sys.vms.update({"lane-a": "running", "lane-b": "running"})
        rc, _ = self.cli("inspect", name)
        self.assertEqual(rc, 4)
        self.assertEqual(self.sys.spawned, [])

    def test_inspect_boots_without_shares_and_trusts_only_ssh(self):
        name = self.keep()
        self.sys.ssh_fails = 3              # `tart ip` answers at once; SSH does not yet
        rc, out = self.cli("inspect", name)
        self.assertEqual(rc, 0)
        self.assertEqual(self.sys.spawned, [["tart", "run", "--no-graphics", name]])
        self.assertTrue(out.startswith("ssh -i "), out)
        ssh_calls = [c for c in self.sys.calls if c[0] == "ssh"]
        self.assertEqual(len(ssh_calls), 4, "the address is trusted only once SSH answers")

    def test_inspect_refuses_a_name_that_is_not_held(self):
        self.sys.vms["studio-pulp-gate-01-1-2"] = "stopped"
        rc, _ = self.cli("inspect", "studio-pulp-gate-01-1-2")
        self.assertEqual(rc, 2)
        rc, _ = self.cli("delete", "studio-pulp-gate-01-1-2")
        self.assertEqual(rc, 1)
        self.assertIn("studio-pulp-gate-01-1-2", self.sys.vms)

    # expiry
    def test_ttl_deletes(self):
        name = self.keep(pr=None)
        self.sys.t = T0 + 24 * 3600 - 1
        self.assertEqual(dh.expire(self.sys, self.dir)["deleted"], [])
        self.sys.t = T0 + 24 * 3600
        self.assertEqual(dh.expire(self.sys, self.dir)["deleted"],
                         [{"name": name, "reason": "ttl"}])
        self.assertNotIn(name, self.sys.vms)
        self.assertEqual(dh.records(self.dir), [])

    def test_a_merged_pr_deletes_with_one_batched_read(self):
        a = self.keep("held-a", pr=1)
        b = self.keep("held-b", pr=2)
        self.sys.prs = {1: "MERGED", 2: "OPEN"}
        out = dh.expire(self.sys, self.dir)
        self.assertEqual(out["deleted"], [{"name": a, "reason": "pr_closed"}])
        self.assertEqual(len([c for c in self.sys.calls if c[1:3] == ["api", "graphql"]]), 1)
        self.assertIn(b, self.sys.vms)
        # Within PR_READ_SECS no second read; the remembered state still applies.
        self.sys.t += 60
        self.sys.prs = {2: "CLOSED"}
        dh.expire(self.sys, self.dir)
        self.assertEqual(len([c for c in self.sys.calls if c[1:3] == ["api", "graphql"]]), 1)
        self.assertIn(b, self.sys.vms)
        self.sys.t += dh.PR_READ_SECS
        out = dh.expire(self.sys, self.dir)
        self.assertEqual(out["deleted"], [{"name": b, "reason": "pr_closed"}])

    def test_an_inspection_left_running_is_stopped(self):
        name = self.keep()
        self.cli("inspect", name)
        self.sys.t += 59 * 60
        self.assertEqual(dh.expire(self.sys, self.dir)["stopped"], [])
        self.sys.t += 60
        self.assertEqual(dh.expire(self.sys, self.dir)["stopped"], [name])
        self.assertEqual(self.sys.vms[name], "stopped")

    # the declared prefix
    def test_vm_reap_never_selects_a_held_vm(self):
        import vm_reap
        self.assertTrue(vm_reap.is_protected_name("held-studio-pulp-gate-01-1-1", []))
        self.assertFalse(vm_reap.is_protected_name("studio-pulp-gate-01-1-1", []))

    def test_image_prune_never_selects_a_held_vm(self):
        import tart_image_prune as tip
        vms = [{"Name": "held-pulp-build-runner-x", "Source": "local", "Size": 60,
                "Accessed": "2020-01-01T00:00:00Z", "State": "stopped"}]
        report = tip.plan(vms, vms_dir=self.tmp, profile_values={"pulp-build-runner"},
                          manifest={"pulp-build-runner"}, text="", opened=[], now=T0,
                          min_idle_days=1)
        self.assertEqual([(i["name"], i["verdict"], i["reason"]) for i in report["images"]],
                         [("held-pulp-build-runner-x", "keep", "debug_hold")])

    def test_the_profile_validator_reads_the_table(self):
        # Read as source: the validator module needs a 3.11 interpreter, and
        # this test runs under the hosts' 3.9 too.
        src = (ROOT / "scripts" / "macos_fleet_lanes.py").read_text()
        self.assertIn('"reuse_canary", "debug_hold",', src)
        self.assertIn("debug_hold.validate_table(hold)", src)


class LaneLib(unittest.TestCase):
    """tartci_debug_hold_current_vm against a fake guest, GitHub and tart."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.guest = self.tmp / "guest"
        label = "com.tartci.aqua.lane-vm-1"
        (self.guest / ".tartci" / "aqua-runner" / label).mkdir(parents=True)
        (self.guest / ".tartci" / "aqua-runner" / label / "jit.cfg").write_text("secret")
        (self.guest / "actions-runner" / ".runner").mkdir(parents=True)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.log = self.tmp / "calls"
        (self.tmp / "listed").write_text("")       # runner ids GitHub lists by name
        (self.tmp / "delete-works").write_text("1")
        (self.tmp / "listener").write_text("")     # non-empty: a listener survives
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for name, body in {
            "gh": f'echo "gh $*" >>{self.log}\n'
                  f'if [ "$2" = -X ]; then [ -s {self.tmp}/delete-works ] && : >{self.tmp}/listed; exit 0; fi\n'
                  f'cat {self.tmp}/listed\n',
            "tart": f'echo "tart $*" >>{self.log}\n',
            "pgrep": f'[ -s {self.tmp}/listener ]\n',
        }.items():
            (bindir / name).write_text("#!/bin/bash\n" + body)
            (bindir / name).chmod(0o755)
        self.env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
                    "TARTCI_DEBUG_HOLD_DIR": str(self.tmp / "hold")}

    def run_hold(self, result: str = "Failed", verdict: str = "hold held-lane-vm-1") -> tuple:
        (self.state / "lane-vm-1.actions-runner.log").write_text(
            f"2026-10-09 07:00:00Z: Job macos completed with result: {result}\n")
        script = f'''
set -uo pipefail
TARTCI_ROOT={str(self.tmp)!r}
STATE_DIR={str(self.state)!r}
GH_CLI=gh; RUNNER_API_ROOT=repos/o/r/actions/runners; CURRENT_RUNNER_API_ROOT=""
CURRENT_VM=lane-vm-1; CURRENT_IP=192.168.64.9; CURRENT_AQUA_LABEL=com.tartci.aqua.lane-vm-1
CURRENT_RPID=""; REPO=o/r; RUNNER_NAME=lane; SLOT=1; VM_USER=admin; SSH_KEY_PRIV=k
SSH_OPTS=(-o BatchMode=yes)
event(){{ echo "event $*" >>{self.log}; }}
note(){{ :; }}
runner_log_completion_result(){{ grep -o 'completed with result: .*' "$1" | sed 's/.*: //'; }}
stop_current_aqua_runner(){{ echo "aqua-stop" >>{self.log}; }}
terminate_current_guardian(){{ return 0; }}
bounded_teardown_command(){{ shift; "$@"; }}
ssh(){{ local cmd="${{@: -1}}"; HOME={str(self.guest)!r} bash -c "$cmd"; }}
python3(){{
  case "$2" in
    admit) printf '%s\\n' {verdict!r}; case {verdict!r} in hold*) return 0;; *) return 1;; esac ;;
    record) echo "record $*" >>{self.log} ;;
  esac
}}
source {str(LIB)!r}
tartci_debug_hold_current_vm; rc=$?
echo "rc=$rc vm=${{CURRENT_VM}}"
'''
        out = subprocess.run(["bash", "-c", script], env=self.env, capture_output=True,
                             text=True, timeout=60)
        calls = self.log.read_text() if self.log.exists() else ""
        return out.stdout.strip().splitlines()[-1], calls

    def test_a_failed_job_is_kept_with_no_way_to_take_a_job(self):
        (self.tmp / "listed").write_text("41\n")   # GitHub still lists the runner
        last, calls = self.run_hold()
        self.assertEqual(last, "rc=0 vm=")
        jit = self.guest / ".tartci" / "aqua-runner" / "com.tartci.aqua.lane-vm-1"
        self.assertFalse(jit.exists(), "the JIT config is gone")
        self.assertFalse((self.guest / "actions-runner" / ".runner").exists())
        self.assertIn("gh api -X DELETE repos/o/r/actions/runners/41", calls)
        self.assertLess(calls.index("gh api -X DELETE"), calls.index("tart stop lane-vm-1"))
        self.assertLess(calls.index("tart stop lane-vm-1"),
                        calls.index("tart rename lane-vm-1 held-lane-vm-1"))
        self.assertIn("event debug_hold_kept vm=lane-vm-1 held=held-lane-vm-1", calls)

    def test_an_unproved_deregistration_deletes_instead(self):
        # Negative control: GitHub still lists the runner after the delete.
        (self.tmp / "listed").write_text("41\n")
        (self.tmp / "delete-works").write_text("")
        last, calls = self.run_hold()
        self.assertEqual(last, "rc=1 vm=lane-vm-1")
        self.assertNotIn("tart rename", calls)
        self.assertIn("reason=deregistration_unproved", calls)

    def test_a_surviving_listener_deletes_instead(self):
        (self.tmp / "listener").write_text("1")
        last, calls = self.run_hold()
        self.assertEqual(last, "rc=1 vm=lane-vm-1")
        self.assertIn("reason=guest_scrub_unproved", calls)
        self.assertNotIn("tart rename", calls)

    def test_a_passed_job_or_a_refusal_keeps_nothing(self):
        last, calls = self.run_hold(result="Succeeded")
        self.assertEqual(last, "rc=1 vm=lane-vm-1")
        self.assertNotIn("tart", calls)
        last, calls = self.run_hold(verdict="refused cap max_per_host=3 held=3")
        self.assertIn("event debug_hold_refused vm=lane-vm-1 reason=cap max_per_host=3", calls)
        self.assertNotIn("tart rename", calls)

    def test_disabled_is_silent(self):
        last, calls = self.run_hold(verdict="refused disabled")
        self.assertEqual(last, "rc=1 vm=lane-vm-1")
        self.assertNotIn("debug_hold_refused", calls)

    def test_the_runner_tries_a_hold_before_the_delete(self):
        src = RUNNER.read_text()
        self.assertIn('source "$TARTCI_ROOT/providers/tart-macos/debug-hold.lib.sh"', src)
        teardown = src.index('event teardown "rc=$rc"')
        self.assertLess(teardown, src.index("if tartci_debug_hold_current_vm; then", teardown))
        self.assertLess(src.index("if tartci_debug_hold_current_vm; then", teardown),
                        src.index("elif ! discard_current_vm; then", teardown))


if __name__ == "__main__":
    unittest.main()
