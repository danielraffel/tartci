#!/usr/bin/env python3
"""Read-only observer for tartci macOS VM runners."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent


def run(argv: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            argv,
            124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or f"timed out after {timeout}s",
        )


def run_json(argv: list[str], *, timeout: int = 20) -> Any:
    proc = run(argv, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"{' '.join(argv)} failed with {proc.returncode}")
    return json.loads(proc.stdout or "null")


def load_digest(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    argv = [
        sys.executable,
        str(HERE / "vm_reap.py"),
        "--json",
        "--repo",
        args.repo,
        "--state-dir",
        args.state_dir,
        "--prefixes",
        args.prefixes,
        "--protected-names",
        args.protected_names,
        "--macos-cap",
        str(args.macos_cap),
    ]
    proc = run(argv, timeout=args.gh_timeout + 20)
    try:
        digest = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        digest = {
            "host": "",
            "capacity": {},
            "supervisors": [],
            "github_runners": [],
            "vms": [],
            "problems": [f"observe_digest_parse_failed:{proc.stderr.strip()}"],
        }
    return digest, proc.returncode


def active_step(job: dict[str, Any]) -> dict[str, Any] | None:
    steps = [step for step in job.get("steps", []) if isinstance(step, dict)]
    for step in steps:
        if step.get("status") == "in_progress":
            return step
    for step in reversed(steps):
        if step.get("conclusion") in ("failure", "cancelled", "timed_out"):
            return step
    for step in reversed(steps):
        if step.get("status") == "completed":
            return step
    return None


def job_summary(run: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    step = active_step(job)
    return {
        "run_id": run.get("id"),
        "run_name": run.get("name"),
        "run_status": run.get("status"),
        "run_conclusion": run.get("conclusion"),
        "run_url": run.get("html_url"),
        "head_branch": run.get("head_branch"),
        "head_sha": run.get("head_sha"),
        "job_id": job.get("id"),
        "job_name": job.get("name"),
        "job_status": job.get("status"),
        "job_conclusion": job.get("conclusion"),
        "runner_name": job.get("runner_name"),
        "labels": job.get("labels") or [],
        "active_step": step,
    }


def fetch_run(repo: str, run_id: str, timeout: int) -> dict[str, Any] | None:
    try:
        return run_json(["gh", "api", f"repos/{repo}/actions/runs/{run_id}"], timeout=timeout)
    except Exception:
        return None


def fetch_jobs(repo: str, run_id: str, timeout: int) -> list[dict[str, Any]]:
    try:
        data = run_json(["gh", "api", f"repos/{repo}/actions/runs/{run_id}/jobs"], timeout=timeout)
    except Exception:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else []
    return [job for job in jobs if isinstance(job, dict)]


def github_jobs_for(
    repo: str,
    *,
    runner: str,
    run_id: str,
    job_id: str,
    timeout: int,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    if run_id:
        run = fetch_run(repo, run_id, timeout) or {"id": run_id}
        for job in fetch_jobs(repo, run_id, timeout):
            if job_id and str(job.get("id") or "") != str(job_id):
                continue
            if runner and job.get("runner_name") and job.get("runner_name") != runner:
                continue
            summaries.append(job_summary(run, job))
        return summaries

    if not runner:
        return summaries

    try:
        runs = run_json(
            ["gh", "api", f"repos/{repo}/actions/runs?status=in_progress&per_page=30"],
            timeout=timeout,
        ).get("workflow_runs", [])
    except Exception:
        return summaries
    for run in runs:
        if not isinstance(run, dict) or not run.get("id"):
            continue
        for job in fetch_jobs(repo, str(run["id"]), timeout):
            if job.get("runner_name") == runner:
                summaries.append(job_summary(run, job))
    return summaries


def tart_ip(vm: str, timeout: int) -> str:
    if not vm:
        return ""
    proc = run(["tart", "ip", vm], timeout=timeout)
    if proc.returncode == 0:
        return proc.stdout.strip()
    return ""


def guest_snapshot(args: argparse.Namespace, ip: str) -> dict[str, Any]:
    if not ip or args.no_guest:
        return {"ip": ip, "available": False, "output": ""}
    command = f"""
set +e
echo '[processes]'
ps -axo pid,ppid,stat,etime,pcpu,pmem,command | \\
  egrep 'ctest|pulp-test|cmake --build|cmake -S|ninja|clang|xcodebuild|git clone|gtimeout|Runner.Worker|actions-runner' | \\
  grep -v egrep | head -{args.process_limit} | \\
  sed -E 's/--jitconfig [A-Za-z0-9_+=\\/+.-]+/--jitconfig <redacted>/g' | \\
  awk '{{ if (length($0) > {args.process_line_width}) print substr($0, 1, {args.process_line_width}) "..."; else print }}'
echo
echo '[ctest-lasttest]'
find "$HOME/actions-runner/_work" -path '*/Testing/Temporary/LastTest.log' -type f -print 2>/dev/null | \\
  while IFS= read -r f; do stat -f '%m %N' "$f" 2>/dev/null; done | \\
  sort -nr | head -2 | cut -d' ' -f2- | \\
  while IFS= read -r f; do echo "--- $f"; tail -n {args.ctest_tail_lines} "$f"; done
"""
    argv = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "-o",
        f"ConnectTimeout={args.ssh_timeout}",
        "-o",
        "BatchMode=yes",
        "-i",
        args.ssh_key,
        f"{args.ssh_user}@{ip}",
        command,
    ]
    proc = run(argv, timeout=args.ssh_timeout + 10)
    return {
        "ip": ip,
        "available": proc.returncode == 0,
        "returncode": proc.returncode,
        "output": proc.stdout.strip(),
        "error": proc.stderr.strip(),
    }


def runner_log_tail(state_dir: str, runner: str, vm: str, lines: int) -> dict[str, Any] | None:
    root = pathlib.Path(state_dir).expanduser()
    candidates: list[pathlib.Path] = []
    for name in (vm, runner):
        if name:
            candidates.append(root / f"{name}.actions-runner.log")
    if root.exists():
        candidates.extend(sorted(root.glob("*.actions-runner.log"), key=lambda p: p.stat().st_mtime, reverse=True))
    seen: set[pathlib.Path] = set()
    for path in candidates:
        if path in seen or not path.exists():
            continue
        seen.add(path)
        try:
            text = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError as exc:
            return {"path": str(path), "error": str(exc), "tail": ""}
        return {"path": str(path), "tail": text}
    return None


def collect(args: argparse.Namespace) -> dict[str, Any]:
    digest, digest_rc = load_digest(args)
    supervisors = digest.get("supervisors") if isinstance(digest.get("supervisors"), list) else []
    if args.runner:
        supervisors = [s for s in supervisors if str(s.get("runner") or "") == args.runner]

    observations: list[dict[str, Any]] = []
    for supervisor in supervisors:
        runner = str(supervisor.get("runner") or "")
        vm = str(supervisor.get("vm") or "")
        repo = str(supervisor.get("repo") or args.repo)
        run_id = str(supervisor.get("run_id") or args.run_id or "")
        job_id = str(supervisor.get("job_id") or args.job_id or "")
        ip = str(supervisor.get("vm_ip") or "") or tart_ip(vm, args.tart_timeout)
        observations.append(
            {
                "supervisor": supervisor,
                "github_jobs": github_jobs_for(
                    repo,
                    runner=runner,
                    run_id=run_id,
                    job_id=job_id,
                    timeout=args.gh_timeout,
                ),
                "guest": guest_snapshot(args, ip),
                "runner_log": runner_log_tail(args.state_dir, runner, vm, args.log_lines),
            }
        )

    return {
        "digest_returncode": digest_rc,
        "digest": digest,
        "observations": observations,
    }


def format_age(value: Any) -> str:
    if value is None:
        return "?"
    return f"{value}s"


def print_human(data: dict[str, Any]) -> None:
    digest = data.get("digest") or {}
    capacity = digest.get("capacity") or {}
    problems = digest.get("problems") or []
    print(
        "tartci observe macos - "
        f"host={digest.get('host') or '?'} "
        f"capacity={capacity.get('running_macos_vms')}/{capacity.get('macos_cap')} "
        f"free={capacity.get('free')} problems={len(problems)}"
    )
    for problem in problems:
        print(f"  problem: {problem}")
    if not data.get("observations"):
        print("  no matching macOS supervisors")
        return

    for obs in data["observations"]:
        supervisor = obs.get("supervisor") or {}
        print()
        print(
            f"runner={supervisor.get('runner') or '?'} "
            f"phase={supervisor.get('phase') or '?'} "
            f"vm={supervisor.get('vm') or '-'} "
            f"heartbeat_age={format_age(supervisor.get('heartbeat_age_secs'))}"
        )
        for job in obs.get("github_jobs") or []:
            step = job.get("active_step") or {}
            step_name = step.get("name") or "-"
            step_status = step.get("status") or "-"
            step_conclusion = step.get("conclusion") or ""
            print(
                f"  github: run={job.get('run_id')} job={job.get('job_name')} "
                f"status={job.get('job_status')} conclusion={job.get('job_conclusion') or '-'} "
                f"step={step_name}({step_status}{('/' + step_conclusion) if step_conclusion else ''})"
            )
            if job.get("run_url"):
                print(f"  url: {job['run_url']}")
        guest = obs.get("guest") or {}
        if guest.get("ip"):
            print(f"  guest: ip={guest.get('ip')} available={guest.get('available')}")
        if guest.get("output"):
            print(indent_block(str(guest["output"]), "    "))
        elif guest.get("error"):
            print(indent_block(str(guest["error"]), "    guest-error: "))
        log = obs.get("runner_log")
        if log:
            print(f"  runner-log: {log.get('path')}")
            if log.get("tail"):
                print(indent_block(str(log["tail"]), "    "))


def indent_block(text: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only macOS Tart runner observer")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--repo", default=os.environ.get("TARTCI_RUNNER_REPO", "Generous-Corp/pulp"))
    parser.add_argument("--state-dir", default=os.environ.get("TARTCI_STATE_DIR", str(pathlib.Path.home() / ".tartci/state/macos")))
    parser.add_argument("--prefixes", default=os.environ.get("TARTCI_REAP_PREFIXES", "pulp-,linux-ephr-,win-ephr-,tartci-"))
    parser.add_argument("--protected-names", default=os.environ.get("TARTCI_REAP_PROTECTED_NAMES", "pulp-vm,rosetta-probe"))
    parser.add_argument("--macos-cap", type=int, default=int(os.environ.get("TARTCI_MACOS_VM_CAP", "2")))
    parser.add_argument("--runner", default="", help="limit to a runner name")
    parser.add_argument("--run-id", default="", help="GitHub Actions run id to inspect")
    parser.add_argument("--job-id", default="", help="GitHub Actions job id to inspect")
    parser.add_argument("--no-guest", action="store_true", help="skip SSH guest inspection")
    parser.add_argument("--ssh-user", default=os.environ.get("TARTCI_VM_USER", os.environ.get("PULP_VM_USER", "admin")))
    parser.add_argument("--ssh-key", default=os.environ.get("TARTCI_VM_SSH_KEY", os.environ.get("PULP_VM_SSH_KEY", str(pathlib.Path.home() / ".ssh/id_ed25519"))))
    parser.add_argument("--ssh-timeout", type=int, default=10)
    parser.add_argument("--gh-timeout", type=int, default=int(os.environ.get("TARTCI_GH_TIMEOUT_SECS", "15")))
    parser.add_argument("--tart-timeout", type=int, default=10)
    parser.add_argument("--process-limit", type=int, default=40)
    parser.add_argument("--process-line-width", type=int, default=280)
    parser.add_argument("--ctest-tail-lines", type=int, default=60)
    parser.add_argument("--log-lines", type=int, default=40)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    data = collect(args)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print_human(data)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
