# Never

Each rule names the safe alternative.

## Network and access

- Never change Tailscale state, tailnet DNS or tailnet ACLs. tartci's network
  checks only read; a broken route is reported for a person to fix.
- Never edit `~/.ssh/config` or the private fleet SSH table. Read them.
- Never put a token in a LaunchAgent plist, a guest, a profile or a command
  line. Use the configured CLI (`TARTCI_GH_CLI`), which mints its own.
- Never fall back from a configured GitHub App CLI to ambient `gh`, and never
  reuse a registry, package or image-pull credential for runner registration.

## Processes and services

- Never run raw `launchctl kickstart`, `bootout`, `unload`, `remove`, `kill`,
  `disable` or `stop` against a tartci lane or an `actions.runner.*` service.
  They kill a running job's VM, and kickstart does not re-read the plist. Use
  `tartci launchd reload <label>` (refuses mid-job) or `tartci pool drain`.
  `tartci launchd guard --hook` blocks these when installed as a hook.
- Never kill processes by bare name (`pkill bash`, `killall python3`) or with
  `kill -9 -1`. Kill the specific process tree by its top PID, or use
  `tartci launchd heal`.
- Never run `agent-hygiene --reap` as an agent. It is for a person at a
  terminal and refuses agent contexts.

## Rollout and capacity

- Never change every host at once. Change one host, prove it with a real job,
  then the next. `tartci fleet-macos self-update` already works this way.
- Never pass `--allow-last-serving-host` to `pool drain` or `pool off` unless
  taking the label to zero runners is intended.
- Never set `TARTCI_VM_LEASES=0` outside break-glass debugging.
- Never turn on an off-by-default feature (`[debug_hold]`, admission-clean
  `required` mode, host-health yield) on more than the one host you are
  testing, and only with the owner's approval.
- Never create a pool drain's held-idle receipt by hand. Without its
  authoritative producer the drain stays pending; that is intended.

## Queue and jobs

- Never hold the merge queue, disable repository-wide auto-merge, or cancel
  unrelated merge groups to protect a routing change. Mark only the affected
  pull requests with the Shipyard opt-out label.
- Never bulk-cancel busy runners or reset a shared runner namespace. Capture
  two snapshots, inspect the job, and use the documented single-runner
  recovery.
- Never add a second scheduler. GitHub assigns jobs; Shipyard owns queue
  policy.

## Deletion and evidence

- Never run `tartci doctor --reap --fix` before reading a clean
  `tartci doctor --reap --json` report.
- Never delete a golden, a bench VM or a `held-` VM by hand. Use
  `tartci held delete NAME` for held VMs.
- Never clear a JIT denial receipt before repairing the credential route it
  names.
- Never read zero online runners, a `0` queue count or an empty observation as
  proof that a host is down or idle. Check the exit code: unknown has its own.
- Never commit VM images or anything under `TARTCI_HOME` to this repository.
