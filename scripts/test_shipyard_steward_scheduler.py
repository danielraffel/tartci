#!/usr/bin/env python3
"""Hermetic tests for the single-controller Shipyard carrier scheduler."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from scripts import shipyard_steward_scheduler as scheduler


SCRIPT = Path(__file__).with_name("shipyard_steward_scheduler.py")
HEAD = "a" * 40

# A fake Shipyard. Each repository's plan comes from $PLANS/<owner>_<name>.json
# (a JSON list of plan rows); a missing file plans nothing. `--apply` records
# the intent file it was handed and answers with one mutation per action.
FAKE_SHIPYARD = """#!/bin/sh
set -eu
fence=absent
[ ! -f "$QUARANTINE_FILE" ] || fence=present
printf '%s|cwd=%s|gh=%s|github=%s|fence=%s\\n' "$*" "$PWD" "${GH_TOKEN-unset}" "${GITHUB_TOKEN-unset}" "$fence" >> "$CALLS"
if [ "$*" = "--json runner carrier --replay /dev/null" ]; then
  [ "$NO_CARRIER" = 1 ] && exit 2
  printf '{"schema_version":1,"command":"runner.carrier","apply":false,"replay":"/dev/null","plans":[]}\\n'
  exit 0
fi
if [ "$1 $2 $3" = "--json runner carrier" ]; then
  repo="$5"
  key=$(printf '%s' "$repo" | tr '/' '_')
  case "$*" in
    *--apply*)
      intent=$(printf '%s' "$*" | sed 's/.*--intent //')
      cp "$intent" "$APPLY_SAW_INTENT"
      [ "$SLOW_APPLY" = 1 ] && sleep 5
      python3 - "$repo" "$intent" <<'PY'
import json, sys
repo, intent = sys.argv[1], sys.argv[2]
actions = [a for a in json.load(open(intent))["actions"] if a["repo"] == repo]
prs = [{"number": a["number"], "head_sha": a["head_sha"], "decision": "propose",
        "action": a["action"], "mutation": "armed " + a["head_sha"]} for a in actions]
print(json.dumps({"schema_version": 1, "command": "runner.carrier", "apply": True,
                  "classes": [], "repos": [{"repo": repo, "base": "main", "prs": prs, "errors": []}]}))
PY
      exit 0
      ;;
  esac
  case ",$SLOW_REPOS," in *",$repo,"*) sleep 3 ;; esac
  case ",$NOISY_REPOS," in *",$repo,"*) python3 -c 'print("x" * (5 * 1024 * 1024))'; exit 0 ;; esac
  case ",$DETACHED_REPOS," in
    *",$repo,"*)
      python3 -c 'import os,sys,time; os.setsid(); open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(20)' "$DETACHED_PID_FILE" &
      sleep 3
      ;;
  esac
  prs="[]"
  [ ! -f "$PLANS/$key.json" ] || prs=$(cat "$PLANS/$key.json")
  printf '{"schema_version":1,"command":"runner.carrier","apply":false,"classes":[],"repos":[{"repo":"%s","base":"main","prs":%s,"errors":[]}]}\\n' "$repo" "$prs"
  exit 0
fi
exit 97
"""


def rearm(number: int) -> dict[str, object]:
    return {
        "number": number,
        "head_sha": HEAD,
        "decision": "propose",
        "action": "rearm",
        "head": HEAD,
        "facts": {"number": number, "merge_state": "BLOCKED"},
    }


def redispatch(number: int) -> dict[str, object]:
    return {
        "number": number,
        "head_sha": HEAD,
        "decision": "propose",
        "action": "redispatch",
        "head": HEAD,
        "run_ids": [9],
        "facts": {"number": number},
    }


def held(number: int) -> dict[str, object]:
    return {
        "number": number,
        "head_sha": HEAD,
        "decision": "hold",
        "hold": "conflicting",
        "facts": {"number": number, "merge_state": "DIRTY"},
    }


class StewardSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        # The scheduler rejects state below a directory writable by other
        # local users, and Linux's tempfile default sits under world-writable
        # /tmp, so the fixture lives under HOME.
        self.temporary = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.temporary.name)
        self.calls = self.root / "calls"
        self.plans_dir = self.root / "plans"
        self.plans_dir.mkdir()
        self.shipyard = (self.root / "shipyard").resolve()
        self.shipyard.write_text(FAKE_SHIPYARD, encoding="utf-8")
        self.shipyard.chmod(0o755)
        self.repos: list[tuple[str, Path]] = []
        for name in ("one", "two", "three"):
            checkout = self.root / name
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            identity = f"owner/{name}"
            subprocess.run(
                ["git", "-C", str(checkout), "remote", "add", "origin", f"git@github.com:{identity}.git"],
                check=True,
            )
            self.repos.append((identity, checkout.resolve()))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def plan_for(self, repo: str, rows: list[dict[str, object]]) -> None:
        (self.plans_dir / f"{repo.replace('/', '_')}.json").write_text(json.dumps(rows))

    def write_config(
        self,
        path: Path,
        *,
        mode: str,
        authority: bool | None = None,
        classes: list[str] | None = None,
        timeout: int = 2,
        schema: int = 2,
        file_mode: int = 0o600,
    ) -> None:
        value = {
            "schema_version": schema,
            "mode": mode,
            "authority": (mode == "live") if authority is None else authority,
            "classes": (["rearm"] if mode == "live" else []) if classes is None else classes,
            "shipyard": str(self.shipyard),
            "repositories": [
                {"repo": identity, "checkout": str(checkout)} for identity, checkout in self.repos
            ],
            "carrier_timeout_seconds": timeout,
            "max_log_bytes": 1024 * 1024,
            "log_generations": 2,
        }
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(file_mode)

    def environment(self, **overrides: str) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "CALLS": str(self.calls),
                "PLANS": str(self.plans_dir),
                "SLOW_REPOS": "",
                "NOISY_REPOS": "",
                "DETACHED_REPOS": "",
                "SLOW_APPLY": "0",
                "NO_CARRIER": "0",
                "DETACHED_PID_FILE": str(self.root / "detached.pid"),
                "QUARANTINE_FILE": str(self.root / "scheduler.quarantine.json"),
                "APPLY_SAW_INTENT": str(self.root / "apply-saw-intent.json"),
                "GH_TOKEN": "must-not-leak",
                "GITHUB_TOKEN": "must-not-leak",
            }
        )
        environment.update(overrides)
        return environment

    def argv(self, config: Path, prefix: str = "") -> list[str]:
        return [
            str(SCRIPT),
            "--config", str(config),
            "--report", str(self.root / f"{prefix}report.json"),
            "--health", str(self.root / f"{prefix}health.json"),
            "--startup", str(self.root / f"{prefix}startup.json"),
            "--log", str(self.root / f"{prefix}scheduler.log"),
            "--plans", str(self.root / f"{prefix}plans.jsonl"),
            "--intent", str(self.root / "intent.json"),
            "--lock", str(self.root / "scheduler.lock"),
            "--quarantine", str(self.root / "scheduler.quarantine.json"),
        ]

    def run_scheduler(
        self, *, invalid_utf8: bool = False, env: dict[str, str] | None = None, **config: object
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None, dict[str, object] | None]:
        path = self.root / "config.json"
        self.write_config(path, **config)  # type: ignore[arg-type]
        if invalid_utf8:
            path.write_bytes(b"\xff\xfe")
            path.chmod(0o600)
        result = subprocess.run(
            self.argv(path),
            env=self.environment(**(env or {})),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        report = self.root / "report.json"
        health = self.root / "health.json"
        return (
            result,
            json.loads(report.read_text()) if report.exists() else None,
            json.loads(health.read_text()) if health.exists() else None,
        )

    def call_lines(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def ledger(self) -> list[dict[str, object]]:
        path = self.root / "plans.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_disabled_is_noop_with_atomic_status(self) -> None:
        result, report, health = self.run_scheduler(mode="disabled")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.call_lines(), [])
        self.assertEqual(report["status"], "disabled")
        self.assertEqual(health["status"], "disabled")

    def test_plan_mode_records_every_plan_and_never_applies(self) -> None:
        self.plan_for("owner/one", [rearm(1), held(2)])
        self.plan_for("owner/two", [redispatch(3)])
        result, report, health = self.run_scheduler(mode="plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.call_lines()
        self.assertTrue(calls[0].startswith("--json runner carrier --replay /dev/null|"))
        carrier = [line for line in calls if "--repo" in line]
        self.assertEqual(len(carrier), 3)
        for line, (identity, checkout) in zip(carrier, self.repos):
            self.assertTrue(line.startswith(f"--json runner carrier --repo {identity}|"), line)
            self.assertIn(f"cwd={checkout}", line)
            self.assertIn("gh=unset|github=unset", line)
            # A plan pass mutates nothing, so it runs without the fence.
            self.assertIn("fence=absent", line)
        self.assertFalse(any("--apply" in line for line in calls))
        self.assertEqual(report["child_environment_tokens"], {"GH_TOKEN": False, "GITHUB_TOKEN": False})
        self.assertEqual(report["proposals"], 2)
        self.assertEqual(report["mutations"], 0)
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["mode"], "plan")
        self.assertFalse((self.root / "intent.json").exists())
        rows = self.ledger()
        self.assertEqual([row["repo"] for row in rows], [identity for identity, _ in self.repos])
        first = rows[0]["plans"]
        self.assertEqual(first[0]["action"], "rearm")
        self.assertIn("facts", first[0], "an action keeps the facts behind it")
        self.assertEqual(first[1]["hold"], "conflicting")
        self.assertNotIn("facts", first[1])

    def test_a_plan_pass_that_reports_a_mutation_is_unhealthy(self) -> None:
        row = rearm(1)
        row["mutation"] = "armed"
        self.plan_for("owner/one", [row])
        result, report, health = self.run_scheduler(mode="plan")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["repositories"][0]["error"], "a plan pass reported a mutation")
        self.assertEqual(health["status"], "unhealthy")

    def test_live_writes_the_intent_before_applying_only_enabled_classes(self) -> None:
        self.plan_for("owner/one", [rearm(1), redispatch(2), held(3)])
        result, report, health = self.run_scheduler(mode="live", classes=["rearm"])
        self.assertEqual(result.returncode, 0, result.stderr)
        applies = [line for line in self.call_lines() if "--apply" in line]
        self.assertEqual(len(applies), 1, "only the repository with an action applies")
        self.assertIn("--repo owner/one --apply --class rearm --intent", applies[0])
        self.assertIn("fence=present", applies[0])
        seen = json.loads((self.root / "apply-saw-intent.json").read_text())
        self.assertEqual(
            seen["actions"],
            [{"repo": "owner/one", "number": 1, "head_sha": HEAD, "action": "rearm", "head": HEAD}],
        )
        self.assertNotIn("completed_at", seen, "the intent exists before the apply runs")
        final = json.loads((self.root / "intent.json").read_text())
        self.assertIn("completed_at", final)
        self.assertEqual(report["mutations"], 1)
        self.assertEqual(health["status"], "healthy")
        self.assertFalse((self.root / "scheduler.quarantine.json").exists())

    def test_a_timed_out_apply_quarantines_and_leaves_the_unfinished_intent(self) -> None:
        self.plan_for("owner/one", [rearm(1)])
        result, report, health = self.run_scheduler(
            mode="live", classes=["rearm"], env={"SLOW_APPLY": "1"}
        )
        pid_path = self.root / "detached.pid"
        self.assertFalse(pid_path.exists())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["status"], "quarantined")
        self.assertEqual(health["status"], "quarantined")
        self.assertTrue((self.root / "scheduler.quarantine.json").exists())
        intent = json.loads((self.root / "intent.json").read_text())
        self.assertNotIn("completed_at", intent)
        self.assertEqual(intent["actions"][0]["number"], 1)
        # Later ticks run nothing until an operator removes the quarantine.
        before = self.call_lines()
        blocked, blocked_report, _ = self.run_scheduler(mode="live", classes=["rearm"])
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(self.call_lines(), before)
        self.assertEqual(blocked_report["status"], "quarantined")
        blocked_plan, _, _ = self.run_scheduler(mode="plan")
        self.assertEqual(blocked_plan.returncode, 2)
        self.assertEqual(self.call_lines(), before)
        (self.root / "scheduler.quarantine.json").unlink()
        cleared, _, cleared_health = self.run_scheduler(mode="plan")
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertEqual(cleared_health["status"], "healthy")

    def test_concurrent_ticks_take_one_lock(self) -> None:
        config = self.root / "config.json"
        self.write_config(config, mode="plan", timeout=10)
        environment = self.environment(SLOW_REPOS="owner/one")
        first = subprocess.Popen(
            self.argv(config, "a-"), env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(
            "--repo owner/one" in line for line in self.call_lines()
        ):
            time.sleep(0.02)
        second = subprocess.run(
            self.argv(config, "b-"), env=environment, text=True, capture_output=True,
            check=False, timeout=10,
        )
        first.communicate(timeout=20)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertIn("another tick holds the scheduler lock", second.stderr)
        self.assertTrue((self.root / "a-report.json").exists())
        self.assertFalse((self.root / "b-report.json").exists())
        self.assertEqual(sum("--repo owner/one" in line for line in self.call_lines()), 1)

    def test_a_missing_carrier_capability_runs_no_plan(self) -> None:
        result, report, health = self.run_scheduler(mode="plan", env={"NO_CARRIER": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(health["status"], "unhealthy")
        self.assertIn("runner carrier", report["error"])
        self.assertFalse(any("--repo" in line for line in self.call_lines()))

    def test_a_token_reaching_a_child_makes_the_tick_unhealthy_before_any_plan(self) -> None:
        # Exercise the gate in-process: the probe reports a leaked token.
        path = self.root / "config.json"
        self.write_config(path, mode="plan")
        config = scheduler.load_config(path)
        logger = scheduler.SchedulerLog(self.root / "probe.log", 1024 * 1024, 2)
        plans = scheduler.SchedulerLog(self.root / "probe-plans.jsonl", 1024 * 1024, 2)
        leaked = {"GH_TOKEN": True, "GITHUB_TOKEN": False}
        with mock.patch.object(scheduler, "probe_child_environment", return_value=leaked), \
                mock.patch.dict(os.environ, self.environment()):
            code, report = scheduler.scheduler(config, logger, plans, self.root / "intent.json")
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "unhealthy")
        self.assertIn("ambient GitHub token", report["error"])
        self.assertEqual(report["child_environment_tokens"], leaked)
        self.assertFalse(any("--repo" in line for line in self.call_lines()))
        self.assertFalse((self.root / "probe-plans.jsonl").read_text())

    def test_mode_authority_and_classes_must_agree(self) -> None:
        cases = [
            ({"mode": "live", "classes": []}, "at least one class"),
            ({"mode": "live", "authority": False}, "authority"),
            ({"mode": "plan", "authority": True}, "authority"),
            ({"mode": "plan", "classes": ["rearm"]}, "at least one class"),
            ({"mode": "live", "classes": ["update_branch"]}, "classes"),
            ({"mode": "live", "classes": ["rearm", "rearm"]}, "classes"),
            ({"mode": "armed"}, "mode"),
            ({"mode": "plan", "schema": 1}, "schema 2"),
        ]
        for config, needle in cases:
            with self.subTest(config=config):
                result, _, health = self.run_scheduler(**config)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(health["status"], "unhealthy")
                self.assertIn(needle, health["reason"])
                self.assertEqual(self.call_lines(), [])

    def test_post_sigkill_reap_is_bounded(self) -> None:
        process = mock.Mock(pid=4242)
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["shipyard"], 2),
            subprocess.TimeoutExpired(["shipyard"], 2),
        ]
        process.poll.return_value = None
        with mock.patch.object(scheduler.os, "killpg") as killpg:
            self.assertFalse(scheduler.terminate_group(process))
        self.assertEqual(
            killpg.call_args_list,
            [mock.call(4242, signal.SIGTERM), mock.call(4242, signal.SIGKILL)],
        )

    def test_a_timed_out_plan_pass_does_not_quarantine(self) -> None:
        result, report, health = self.run_scheduler(
            mode="plan", timeout=1, env={"SLOW_REPOS": "owner/one"}
        )
        self.assertEqual(result.returncode, 1)
        self.assertTrue(report["repositories"][0]["timed_out"])
        self.assertEqual(len(report["repositories"]), 3, "peers still plan")
        self.assertEqual(health["status"], "unhealthy")
        self.assertFalse((self.root / "scheduler.quarantine.json").exists())

    def test_detached_pipe_holder_cannot_extend_the_drain_bound(self) -> None:
        started = time.monotonic()
        result, report, _ = self.run_scheduler(
            mode="plan", timeout=1, env={"DETACHED_REPOS": "owner/one"}
        )
        elapsed = time.monotonic() - started
        pid_path = self.root / "detached.pid"
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.assertEqual(result.returncode, 1)
        self.assertLess(elapsed, 12)
        self.assertTrue(report["repositories"][0]["drain_incomplete"])

    def test_insecure_config_fails_before_shipyard(self) -> None:
        (self.root / "report.json").write_text('{"status":"stale-healthy"}\n', encoding="utf-8")
        result, report, health = self.run_scheduler(mode="plan", file_mode=0o644)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["status"], "unhealthy")
        self.assertEqual(self.call_lines(), [])
        self.assertIn("mode 600", health["reason"])

    def test_non_utf8_config_replaces_stale_health_with_unhealthy(self) -> None:
        result, report, health = self.run_scheduler(mode="plan", invalid_utf8=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["status"], "unhealthy")
        self.assertIn("valid JSON", health["reason"])

    def test_checkout_writable_by_other_users_is_rejected(self) -> None:
        checkout = self.repos[0][1]
        checkout.chmod(0o777)
        try:
            result, _, health = self.run_scheduler(mode="plan")
        finally:
            checkout.chmod(0o755)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.call_lines(), [])
        self.assertIn("other local users", health["reason"])

    def test_output_is_capped_while_noisy_repo_is_drained(self) -> None:
        result, report, _ = self.run_scheduler(mode="plan", env={"NOISY_REPOS": "owner/one"})
        self.assertEqual(result.returncode, 1)
        self.assertTrue(report["repositories"][0]["stdout_truncated"])
        self.assertEqual(report["repositories"][1]["status"], "ok")

    def test_case_variant_duplicate_is_rejected_before_shipyard(self) -> None:
        self.repos.append(("OWNER/ONE", self.repos[0][1]))
        result, _, health = self.run_scheduler(mode="plan")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.call_lines(), [])
        self.assertIn("unique", health["reason"])

    def test_sigterm_quarantines_only_during_a_mutating_command(self) -> None:
        for mode, slow, quarantined in (
            ("plan", {"SLOW_REPOS": "owner/one"}, False),
            ("live", {"SLOW_APPLY": "1"}, True),
        ):
            with self.subTest(mode=mode):
                self.calls.unlink(missing_ok=True)
                (self.root / "scheduler.quarantine.json").unlink(missing_ok=True)
                self.plan_for("owner/one", [rearm(1)])
                config = self.root / "signal-config.json"
                self.write_config(config, mode=mode, timeout=30)
                process = subprocess.Popen(
                    self.argv(config, "signal-"), env=self.environment(**slow),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                marker = "--apply" if mode == "live" else "--repo owner/one"
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not any(
                    marker in line for line in self.call_lines()
                ):
                    time.sleep(0.02)
                time.sleep(0.2)
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=10)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM)
                self.assertEqual(
                    (self.root / "scheduler.quarantine.json").exists(), quarantined
                )


if __name__ == "__main__":
    unittest.main()
