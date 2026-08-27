# Pool transition lock scope

Status: implementation and local validation complete; publication pending

Base: `origin/main` at `1d2f2ae9d5aa15cec4a513be35c75d59980a9400`

Branch: `fix/pool-transition-lock-scope`

## Problem

Each ephemeral provider holds the host-global
`~/.config/tartci/pool-transition.lock` from immediately before JIT minting
until the listener reports assignment or exits. An idle secondary listener can
therefore block an unrelated repository or Forge listener for its whole idle
timeout.

## Safety boundary

The global lock must serialize pool on/drain against the final admission
recheck, JIT mint, and creation of a live listener owned by the provider. After
that spawn, the provider's state record, VM lease, listener PID, and cleanup
trap own the in-flight registration. Cooperative drain disables restart and
does not terminate the provider, so the accepted-job-before-`Runner.Worker`
interval remains protected. Persistent `actions.runner.*` bootout continues to
require the authoritative Shipyard held-idle receipt.

## Rollout gate

- Focused pool helper and provider contract tests pass.
- Full Python unit suite and `./scripts/lint.sh` pass.
- PR checks pass at the exact published head.
- Deploy TartCI one host at a time at an idle boundary; do not delete or repair
  any live transition lock and do not stop protected listeners.
- On the first host, prove two unrelated idle-capable lanes can both pass JIT
  mint while `tartci pool drain` still closes new admission and lets the owned
  listeners finish or time out normally.

## Receipt

- Common helper rejects dead listeners and non-owner handoffs; a live owned
  listener releases the global lock while remaining alive, and a second
  transition can acquire it.
- macOS, Linux, and Windows providers hand off after listener spawn and before
  their assignment/idle wait.
- `python3 -m unittest scripts.test_pool_lib -v`: 30 passed.
- `python3 -m unittest discover -s scripts -p 'test_*.py' -v`: 486 passed.
- `./scripts/lint.sh`: passed (40 shell scripts, 77 Python files, all TOML).
- Autoreview (`--mode local`, Codex): clean, no accepted/actionable findings,
  overall correctness `patch is correct` at confidence 0.89.
- Commit, pushed head, PR URL, and CI result: pending.
