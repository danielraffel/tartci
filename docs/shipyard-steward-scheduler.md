# Shipyard carrier scheduler

`com.danielraffel.shipyard.steward-scheduler` is the fleet's single PR carrier:
the one controller that performs the mechanical steps an approved, armed pull
request needs when nobody is watching it. It is default-off, and exactly one
host in the fleet may run it in live mode; every other install is `plan` or
`disabled`. It does not replace or modify the legacy `shipyard.queue-tick`
service.

The scheduler obtains its entire authority from the user-owned, mode-600
`~/.config/shipyard/steward-scheduler.json` (schema 2). Unknown fields,
non-canonical checkouts, origin mismatches, duplicate repositories, a mode
that disagrees with `authority`, and live mode without a class all fail closed.

## What a tick does

Each tick acquires one nonblocking host lock (a second concurrent tick exits
at once and says so on stderr), strips ambient `GH_TOKEN` and `GITHUB_TOKEN`,
and then:

1. Probes the Shipyard capability by replaying an empty fact file through
   `shipyard runner carrier --replay /dev/null`, and probes the environment a
   child process sees. A missing carrier or a token that reaches a child makes
   the tick unhealthy before any GitHub read.
2. Runs `shipyard --json runner carrier --repo OWNER/REPO` once per configured
   repository. This pass never carries `--apply`. Shipyard reads only GitHub
   facts (the pull request, its required checks, the runs and jobs they name,
   the merge-queue timeline, and the approval record on the exact head) and
   prints one plan per pull request. Every plan, and the facts behind every
   proposed action, is appended to the plan ledger
   `~/Library/Logs/shipyard-steward-scheduler.plans.jsonl`.
3. In live mode only: collects the proposals whose class is enabled, writes
   them atomically to the intent file
   `~/.local/state/tartci/shipyard-steward-scheduler.intent.json`, and then
   runs one `runner carrier --apply --class ... --intent FILE` per repository
   that has an action. Shipyard re-observes GitHub and performs only the
   intended actions that a fresh plan still proposes on the same head. When
   every apply finishes the intent gains `completed_at`; an intent without it
   names the actions an interrupted tick did not finish.

The action classes, graduated to live one at a time:

| class | what it does | live |
|---|---|---|
| `redispatch` | reruns a cancelled required run on the current head of an armed, approved PR; at most two reruns per run (read from GitHub's `run_attempt`) and two per PR per hour | allowed |
| `rearm` | re-arms native auto-merge with `expectedHeadOid` on the exact head the queue removed, when every required merge-group job that did not pass starved (cancelled with no runner after waiting at least ten minutes) | allowed |
| `update_branch` | a merge-only update of a `BEHIND` armed PR | planned only; Shipyard refuses to apply it until the own-lines invariant exists |

The carrier never acts on a draft, conflicting, unarmed, queued, or
unapproved PR, never reruns a failed run, and never re-arms after a removal
for a real failure, a conflict, or a person's decision.

It atomically publishes a bounded report and health verdict under
`~/Library/Logs`, and rotates its own operational log and the plan ledger.
Launchd stdout and stderr go to `/dev/null`. Command output is drained only
into fixed in-memory caps, and a detached descendant that keeps a pipe open
cannot extend the drain deadline.

## Quarantine

Before an apply command starts, the scheduler durably writes the quarantine
file under `~/.local/state/tartci`. It is cleared only when the apply ends
within its bound. A timeout, a crash, or a SIGTERM while an apply is running
leaves it in place, and later ticks (plan and live) stay inert until an
operator proves no descendant remains and removes the file by hand. A plan
pass mutates nothing, so a plan timeout or a SIGTERM during a plan does not
quarantine.

## Install, canary, graduation, rollback

```sh
# Print the plan only.
scripts/install_shipyard_steward_scheduler.sh \
  --repo Generous-Corp/pulp=/absolute/pulp \
  --shipyard /absolute/canonical/shipyard --mode plan

# Inert canary: plan mode, zero mutations by construction.
scripts/install_shipyard_steward_scheduler.sh \
  --repo Generous-Corp/pulp=/absolute/pulp \
  --shipyard /absolute/canonical/shipyard --mode plan --install
```

`--shipyard` must be the canonical path to the executable, not a symlink. A
Shipyard update that moves the binary to a new generation directory needs the
installer re-run with the new path.

The installer stages the config, performs a full bootout and bootstrap, lets
`RunAtLoad` start exactly one tick (kickstarting only a job that has never
run), checks the live registration, and requires a fresh receipt: the
`disabled` health receipt for a disabled install, or the `started` startup
receipt for a plan or live install. That receipt is installation evidence
only; the first terminal health report is the canary gate. If any install step
fails, the installer restores the prior config and plist and reloads the
previously loaded job.

`scripts/verify_shipyard_carrier_canary.py` checks the canary acceptance
criteria on the host: `spawning` (launchd's `runs` count rises by three over
three intervals), `plans` (48 hours of ledger with zero mutations, every
proposal passing the structural negative controls, and, against a rulings
file, no false positive and a true positive per required class),
`quarantine`, `lock`, `tokens`, `rollback`, and `health`.

Only after the canary passes should one controller be armed, one class at a
time:

```sh
scripts/install_shipyard_steward_scheduler.sh \
  --repo Generous-Corp/pulp=/absolute/pulp \
  --shipyard /absolute/canonical/shipyard \
  --mode live --authority --class redispatch --install
```

Live mutation also needs Shipyard's machine-global
`[merge_queue] mutation_machine` to name this host's machine tag; otherwise
Shipyard's mutation guard refuses every action.

Roll back to inert by rerunning the installer with `--mode plan` (or
`disabled`); do not edit the installed plist or config by hand. Keep the
legacy queue tick until the live controller has separate canary and rollback
proof.
