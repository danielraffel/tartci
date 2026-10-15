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
