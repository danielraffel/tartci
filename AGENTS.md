# AGENTS.md

Reference for coding agents working in this repository or on a host that runs
tartci. The human overview is [README.md](README.md).

Read these before acting:

- [docs/agents/invariants.md](docs/agents/invariants.md): rules the code
  enforces and the assumptions it is built on.
- [docs/agents/commands.md](docs/agents/commands.md): each command, what it
  changes, and its exit codes.
- [docs/agents/state.md](docs/agents/state.md): where tartci keeps state on a
  host.
- [docs/agents/never.md](docs/agents/never.md): actions that are never
  allowed, and the safe alternative for each.

Deeper material: [docs/reference.md](docs/reference.md) (behavior and
environment variables), [docs/runbook.md](docs/runbook.md) (procedures),
[docs/gotchas.md](docs/gotchas.md) (known failure modes).

Before pushing a change to this repository, run `./scripts/lint.sh`.

## How a PR lands here

- **Open the PR and stop.** Use `ghapp pr create -R danielraffel/tartci --base main`. Never arm, enqueue, auto-merge or merge; admission has exactly one writer.
- **Review:** a standing reviewer posts an exact-head APPROVE or a HOLD once required checks are green. Never approve your own team's work.
- **Landing:** this repo is user-owned, so there is no merge queue. The m3 steward tick (every 5 minutes) merges the exact approved head. The ruleset requires `lint` and `review-verdict`, bound to GitHub Actions.
- **On a HOLD:** fix what it names, with a test. Merge `origin/main` (never rebase or force-push). Push once and comment `repaired: ... at <sha>`; the PR re-enters review on its own.
- **macOS first.** A Windows- or Linux-only failure gets a `PARKED/<platform>` issue with evidence and never blocks a macOS landing.
- **Labels the pipeline skips:** `superseded`, `owner-decision`, `PARKED/<platform>`.
- Launch agents with `sr claude` / `sr codex` and use the smallest model that fits (Haiku or Codex Luna for mechanical work, Sonnet or Codex Terra/Sol for fixes, Opus/Fable/Codex Astra for security, signing and release diffs).
- Steward log on m3: `~/Library/Logs/shipyard-steward-tick/summary.log`.
