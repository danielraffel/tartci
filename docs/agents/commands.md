# Commands

`tartci` is a bash dispatcher at the repository root. On an installed host it
is `~/.local/bin/tartci`, a shim into the installed support generation. Every
command below is listed in the dispatcher's header comment.

Effect column: **read** changes nothing; **plan** reads and prints what it
would do; **write** changes host state.

## Inspect (read)

| command | effect | notes |
|---|---|---|
| `tartci doctor` | read | prerequisites, stores, Shipyard GitHub auth |
| `tartci doctor fleet [--json] [--no-census]` | read | executed generation, drain capability, runner census across repo and org scopes. Exit 0 healthy, 1 problem, 2 undetermined. Reason codes are in `scripts/fleet_reasons.json` |
| `tartci doctor --reap --json` | read | stale owned VM, overlay and runner residue. Exit 0 healthy, 1 stale or wedged, 2 unreadable |
| `tartci doctor build-disagreement [--json]` | read | cross-host compile or link disagreement (cache poisoning). Off by default |
| `tartci version [--json]` | read | the commit this copy runs: an installed generation's support manifest, or a checkout's HEAD (and whether it has uncommitted changes). Exit 1 when neither says |
| `tartci status --json` | read | providers, capacity, role, janitor install state |
| `tartci host-profile [--json]` | read | derived role and budget (`PULP_BUILD_JOBS`, `PULP_BUILD_MEM_BUDGET_MB`) |
| `tartci host-profile --delivery [--json]` | read | how code reaches each lane, and staleness |
| `tartci governor show\|explain\|fleet\|keys` | read | the per-host governor config |
| `tartci leases status` | read | host lease store |
| `tartci pool status [--json] [--require-ready] [--require-serving]` | read | admission state and participation |
| `tartci pool status --usage [--range 24h] [--json]` | read | slot occupancy, idle-with-demand, discards, per-phase VM time |
| `tartci fleet capacity --repo OWNER/REPO --target LABEL [--profile ABS] [--json]` | read | digest-bound observation-only capacity receipt; unknown state fails closed |
| `tartci pool log --status` | read | bounded history of pool status |
| `tartci observe macos [--json] [--no-guest] [--runner NAME]` | read | live macOS VM jobs. Exit 0 trustworthy, 3 coverage shortfall (`BLIND:` or `UNKNOWN:`) |
| `tartci profile list\|show\|explain\|plan` | read | routing profiles in `profiles/` |
| `tartci runtime summary\|recent\|export` | read | runtime records when `TARTCI_RUNTIME_MEASURE=1` |
| `tartci metrics report\|dashboard [file]` | read | build metrics table or HTML |
| `tartci timings` | read | per-job `timing.tsv` summary |
| `tartci goldens list`, `tartci goldens doctor` | read | canonical golden, drift, receipts |
| `tartci ccache scan` | read | counts suspect direct-mode manifests |
| `tartci hooks print` | read | prints agent hook settings; never writes them |
| `tartci launchd status` | read | serve LaunchAgent state |
| `tartci fleet-macos tool-freshness [--json]` | read | Shipyard and pulp CLI versus latest release |
| `tartci update --status [--json]` | read | one line: installed commit, skew, the live update step and how long it has run, last result |
| `tartci update --wait [--json] [--timeout S]` | read | silent until this host's running update ends, then one line; exit 0 succeeded, 3 refused, 4 failed, 124 timed out. Run it in the background to be woken |
| `tartci update [--yes]` | apply | update this host (same engine as `fleet-macos self-update --apply`): asks y/N on a terminal, never applies without one unless `--yes`; prints one line and keeps the full output in `~/.tartci/state/self-update/last-update-output.log` |
| `tartci fleet-macos self-update --plan` | plan | default mode; steps 1 to 4 of the update, read-only |
| `tartci pool <on\|drain\|off> --plan` | plan | prints the transition, writes nothing |
| `tartci launchd reload <label> --dry-run` | plan | prints the plan or `REFUSE` (exit 0 or 3) |
| `tartci reclaim`, `tartci image-prune`, `tartci retention` | plan | without `--fix` / `--apply` |

## Change host state (write)

| command | effect | refuses when |
|---|---|---|
| `tartci setup` | installs Tart (requires macOS 15 or later for a fresh install), QEMU, sshpass, jq, gh; creates stores under `TARTCI_HOME`; derives the host role; installs the disk reclaimer and support agents | Homebrew missing |
| `tartci pool drain` | writes participation 0 and pool state `draining`; disables restart of receipt-owned runner agents; the running job finishes | capacity floor (last host serving a required label); a persistent Actions runner without an authoritative idle receipt (exits nonzero, stays pending) |
| `tartci pool undrain` | exact inverse of a drain: re-enables the agents and reopens admission | pool is not `draining` |
| `tartci pool off [--now]` | closes admission and boots out owned agents immediately | capacity floor; a lane is mid-job (exit 12) unless `--now` |
| `tartci pool on` | restores exactly the services the fleet profile receipt names | receipt rejected |
| `tartci pool repair-lock` | removes an orphaned pool transition lock | admission open, owner alive, or a runner or VM is active (exit 5) |
| `tartci launchd reload <label>` | full bootout and bootstrap; re-reads the plist | the lane is mid-job (exit 3) unless `--allow-mid-job` |
| `tartci launchd heal` | heals a wedged serve LaunchAgent | |
| `tartci governor set K=V`, `unset K` | edits `~/.config/tartci/governor.toml` | |
| `tartci leases acquire\|release\|heartbeat\|reap` | edits the lease store | |
| `tartci serve <os> [--once\|--loop]` | runs the serving loop: boots a VM per matching queued job | lease denied, pool not admitting, JIT denial receipt present |
| `tartci up <os>` | one build and test in a throwaway clone, then discards it | |
| `tartci prepare linux\|macos`, `tartci windows golden` | bakes a golden image | |
| `tartci goldens sync --to HOST\|--from HOST` | copies the Windows golden between hosts, verifies, repoints | |
| `tartci tart-image-sync` | resumable, checksum-verified Tart image transfer | |
| `tartci doctor --reap --fix` | deletes owned stale residue (see invariants) | |
| `tartci reclaim --fix`, `image-prune --apply`, `retention --apply` | deletes idle build dirs, unreferenced images, old state | |
| `tartci ccache quarantine`, `ccache reset` | moves suspect manifests aside; resets a cache | reset: a VM runs or holds a lease (exit 3) |
| `tartci held stop\|delete\|expire\|inspect NAME` | manages failed gate VMs kept for debugging | inspect: no free slot |
| `tartci fleet-macos install CONFIG` | installs generated fleet lanes | |
| `tartci fleet-macos self-update --apply` | updates this host to main's tartci: drain, wait, off, install, on, verify; restores on failure | another host is draining, updating or unreadable (defers) |
| `tartci gate-slot2 install [--apply]` | installs a second managed gate lane; without `--apply` it is a dry run; `pool on` activates it | pool admission is open, or its agent is loaded (exit 3) |

## Guard

| command | effect |
|---|---|
| `tartci launchd guard --hook` | PreToolUse classifier for agent shells. Exit 2 blocks raw `launchctl kickstart\|bootout\|unload\|remove\|kill\|disable\|stop` against a tartci lane or `actions.runner.*` service, and broad process kills. `TARTCI_ALLOW_RAW_LAUNCHCTL=1` written in the command is the audited escape |
