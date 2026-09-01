# Dynamic macOS fleet worker

This design lets one Mac offer disposable Tart capacity to Pulp, Forge, and
Vellum without dedicating the machine to any repository. Each repository has a
small queue-aware supervisor, but all supervisors use the same host lease store,
capacity cap, durable pool switch, Tart store, and cache. A supervisor boots a
VM only after it sees an exact matching queued job and acquires a governed
lease. Failed queue discovery is fail-closed.

The checked-in declarations are `profiles/{m1,m3,m5}-macos-fleet.toml`. M1 and
M5 use `$HOME/VMs`; M3 uses `/Volumes/Workshop/VMs`. M3's durable Shipyard host
tag is `studio`, so its profile binds `host.id = "studio"` even though the file
uses the operator-facing M3 name. Paths are absolute in rendered LaunchAgents
because minimal launchd/SSH environments must not guess `HOME`, `PATH`, or
`TART_HOME`.

M3's external store is not permission-equivalent to the home-backed stores.
Its profile alone declares a stable Developer-ID app at
`~/.local/libexec/TartCILauncher.app`. The app signature seals the exact TartCI
support cohort and rendered M3 lane records. Its native process accepts only a
sealed lane enum, remains resident as the macOS privacy responsible identity,
and cannot launch a caller-selected executable or argument vector. The profile
and install receipt bind its bundle SHA-256, identifier, Team ID, designated
requirement, source commit, profile-policy digest, owner, and mode. Fleet
installation consumes an already-signed artifact; it never holds a signing
identity. Before admission,
`pool on` uses a temporary LaunchAgent to prove bounded write/read/delete access
to `/Volumes/Workshop/VMs`. Failure leaves participation closed and starts no
runner. This is private host policy encoded by the M3 profile, not a default
imposed on TartCI users.

M1 and M5 also declare the three references required by the Shipyard GitHub App
wrapper: App ID, private-key path, and token-cache directory. The renderer
places those references (never key or token contents) in each managed
LaunchAgent. Before publication, the installer requires the private key to be
a current-user-owned, non-symlink, mode-`0600` regular file and the cache to be
a current-user-owned, non-symlink, mode-`0700` directory. This prevents a
regenerated M1 fleet from silently losing the authenticated cache path that
interactive shells normally supply.

Each host profile also carries a dormant `[stacked_images]` contract. It keeps
stacked-disk adoption explicit and host-local: macOS and Tart minimum versions,
the retained flat rollback golden, and paths to separate GHCR username/token
files under `~/.config/pulp/secrets`. Provision those files independently on
each host as current-user-owned mode `0600` regular files. Secret values are
never committed, copied into a golden, rendered into a plist, or passed on
argv. Credential paths remain validated dormant profile metadata and are not
rendered into runner LaunchAgents. The same package credential may be installed
on M1, M3, and M5, but its presence is not activation authority.

`stacked_images.enabled` is checked in as `false` on all three hosts and the
validator currently rejects `true`. Keep it that way while the fully
provisioned flat/stacked/stacked-plus-safe-cache comparison remains DEFER.
Graduation requires a measured decision, provider support for immutable pinned
parents plus private writable overlays, exact digest provenance, safe cache
boundaries, and a flat rollback canary. The enabling change must update the
validator/provider and the reviewed host profile together; editing a live plist
or setting an ambient environment variable is not a supported shortcut.

Every managed lane also declares its exact GitHub runner registration contract.
Host profiles may also declare a bounded GitHub API subprocess timeout when
live measurements require more than the generic 15 seconds. This renders the
same `TARTCI_GH_TIMEOUT_SECS` into every lane on that host; it is not a per-lane
throughput knob. M1 records 30 seconds to preserve its measured production
margin across immutable installs, while M3 and M5 retain the default.
M1's Pulp lane also retains a bounded 180-second tier-zero receipt across VM
boot. This avoids a second exhaustive backlog scan only for merge-group work;
PR-head and every lower class still revalidate live so priority cannot invert.

The renderer exports the protected/default group as `TARTCI_RUNNER_GROUP_ID`;
Pulp's event-class tiers additionally export
`TARTCI_RUNNER_WORKFLOW_TIER_GROUPS`. Omitting either contract would
silently register disposable runners in Default even when a workflow requests
a protected group, leaving the job queued while VMs repeatedly start and exit.
The checked-in protected IDs were verified against the live organization
runner-group API and workflow routing on 2026-08-27. PR-head registration was
changed to repository scope after the 2026-08-30 incident where an M5 JIT
runner registered online/idle at organization scope but was absent from Pulp's
repository runner view and could not claim the queued job:

| Lane/class | Group ID | Registration scope | Access contract |
|---|---:|---|---|
| Pulp merge-group | 1 | repository | repository JIT endpoint plus exact `pulp-build-merge-group` class |
| Pulp PR-head | 1 | repository | repository JIT endpoint plus exact `pulp-build-pr-head` class |
| Forge | 11 | organization | `forge-pr-safe-build`, freshly verified selected to Forge before JIT mint |
| Vellum | 8 | organization | `vellum-macos-build`, freshly verified selected to Vellum before JIT mint |

Runner-group IDs are organization state, not values to infer from repository
names. For every organization-scoped mint, TartCI now re-reads the group's
visibility and exhaustively checks its selected repositories at the final JIT
boundary. An absent repository, unknown policy, pagination uncertainty, or API
denial fails closed before `generate-jitconfig`. Re-verify the live group name,
selected repository, and protected workflow access before changing any checked-in ID.

## Stage without activation

```sh
./tartci fleet-macos validate profiles/m1-macos-fleet.toml
./tartci fleet-macos validate profiles/m3-macos-fleet.toml
./tartci fleet-macos validate profiles/m5-macos-fleet.toml
out="$(mktemp -d)"
./tartci fleet-macos render profiles/m1-macos-fleet.toml --output "$out"
for file in "$out"/*.plist; do plutil -lint "$file"; done
# Before any separately authorized install/bootstrap on M1:
ssh m1-lan 'mkdir -p "$HOME/Library/Logs/tartci"'
```

For M3, stage the exact profile-pinned signed launcher alongside the immutable
support cohort, then install only at a terminal idle boundary:

```sh
./tartci fleet-macos install profiles/m3-macos-fleet.toml \
  --support-source . --support-manifest .tartci-support-manifest.json \
  --launch-helper-source /absolute/staging/TartCILauncher.app --apply
./tartci pool on
./tartci pool status --require-ready
```

The migration gate is zero Shipyard jobs, zero production leases, zero running
production VMs, and zero Runner.Worker processes. Never preempt a job to obtain
that boundary. After the first explicit Removable Volumes grant, canary a real
JIT job and then replace the launcher once with the same identifier/Team ID to
prove the consent identity is durable.

Before a future stacked-image canary on any host, validate only metadata and
authorization without displaying credential values:

```sh
registry_secret_ok() {
  [ "$#" -eq 1 ] &&
    [ ! -L "$1" ] &&
    [ -f "$1" ] &&
    [ "$(stat -f '%u' "$1")" = "$(id -u)" ] &&
    [ "$(stat -f '%Lp' "$1")" = 600 ] &&
    [ -s "$1" ]
}
registry_secret_ok "$HOME/.config/pulp/secrets/ghcr-stackbench-username" &&
  registry_secret_ok "$HOME/.config/pulp/secrets/ghcr-stackbench-token"
```

Use the reviewed registry auth probe for effective pull/push verification; do
not print, source, or copy the files through an interactive command transcript.

Rendering never installs or loads a LaunchAgent. Stable host/lane identifiers
are local operational vocabulary; actual GitHub runner names remain ephemeral
per boot through the existing Tart provider.

## Install while admission is closed

Do not copy rendered plists or bootstrap individual lanes by hand. The managed
installer is dry-run by default and owns publication, declared legacy-agent
retirement, and the receipt that `tartci pool on` verifies before admitting
work:

```sh
tartci pool status --json
tartci fleet-macos install profiles/m1-macos-fleet.toml
tartci fleet-macos install profiles/m1-macos-fleet.toml --apply
tartci pool on
```

`--apply` requires terminal `pool off` state and refuses any loaded target or
declared replacement LaunchAgent. The locked profile `host.id` must exactly
match the machine's durable `shipyard runner tag`. It atomically publishes only
profile-rendered
plists, moves explicitly declared legacy plists into the recoverable
`~/Library/LaunchAgents/.tartci-retired/` archive, writes an exact digest
receipt from a locked profile snapshot installed at
`~/.config/tartci/macos-fleet-profile.toml`, and leaves admission closed. If
fleet plists exist without a valid
receipt, or the profile/plists change afterward, `pool on` fails closed before
changing durable participation. `pool off` continues to discover and stop all
runner agents; the receipt narrows installation authority, not emergency-stop
coverage.

Each host's Pulp lane exposes ordered merge-group and PR-head event tiers under
`event-class-v2`. The provider rechecks higher-priority merge-group demand
immediately before minting, so a PR cannot consume a slot for a merge-group
that arrived during admission. Each host renders two independently identified
Pulp supervisors; host leases and the two-guest hard cap remain admission
authority, so an idle supervisor does not reserve a VM slot. Forge and Vellum use their
current generic selectors until their workflows publish reviewed event-class
labels. Adding those labels is a workflow/governance change, not a fleet-render
side effect.

M1, M3, and M5 give each exhaustive assignment scan a 180-second overall
deadline and four API workers. The host-global observation lock admits only one
scan owner, so this is a four-request host ceiling rather than four workers per
overlapping supervisor. A one-worker canary on 2026-09-01 failed to exhaust the
large live Pulp queue inside 180 seconds; the prior four-worker scans completed.
The public/default profile behavior remains one worker. The scan remains
fail-closed, bounded, and limited by its existing per-call and API-call budgets. Scanner failures also
emit a bounded `assignment_scan_error` event instead of discarding stderr, so a
future timeout or API denial has host-local diagnostic evidence.

The same rendered Pulp contract fixes both selected classes to repository group
1. GitHub evaluates a merge-group workflow under its `gh-readonly-queue/...`
ref, which cannot satisfy organization group 3's
`build.yml@refs/heads/main` restriction. Both advertise exactly one event class,
omit `pulp-gate-fast`, and set
`TARTCI_ADMISSION_CLEAN_MODE=required`. Shipyard's typed admission-clean verdict
runs after guest preflight and immediately before repository-access verification,
the pool lock, final assignment/admission rechecks, and JIT minting.

No V2 Pulp runner advertises the legacy `pulp-gate-fast` label. Existing
measurements put M1 materially behind M3/M5, so it retains a ten-minute queue-age
delay. All three Pulp profiles omit an explicit lease priority: the selected V2
class derives merge-group `110` or PR-head `100`, preserving merge ordering and
allowing either class to use reserved gate cores. Fleet validation rejects an
explicit priority on a V2 lane.

M3 alone renders `TARTCI_MACOS_VM_CORES=12` for Pulp. Its 26-core host budget can
therefore admit two Pulp guests (`12 + 12`) or one Pulp guest alongside a
14-core Forge/Vellum guest (`12 + 14`). M1 and M5 retain their host-profile VM
sizes of 3 and 6 cores. The per-lane override does not resize other repositories'
guests, reserve a slot while idle, or change Tart's two-macOS-guest hard cap.

The GitHub App wrapper requires explicit repository authority even when the API
endpoint is organization-scoped for a protected runner group. JIT minting
therefore supplies both `SHIPYARD_GH_APP_REPO` and `GH_REPO` from the validated
lane repository. Omitting that context causes deterministic VM boot/discard
churn even though queue discovery succeeded.

## Rejoin and offline behavior

Rendered agents use `RunAtLoad` and `KeepAlive`, so a participating host resumes
queue observation after login/reboot. The durable pool state remains the
authority: an offline host that was `pool off` stays off when it returns. Queue
discovery, GitHub App auth, host health, disk floor, lease capacity, and VM
ownership must all pass before boot; otherwise the lane logs its refusal and
remains idle.

## Golden placement and resumable transfer

First inspect the destination with its declared store; a default shell can hide
an existing image:

```sh
TART_HOME="$HOME/VMs" tart list
TART_HOME="$HOME/VMs" tartci doctor --reap --json
```

Prefer an already proven destination golden. When a transfer is actually
needed, run from the source host and plan first:

```sh
./tartci tart-image-sync \
  --name pulp-build-runner:latest \
  --destination m1-lan --fallback m1 \
  --source-tart-home /Volumes/Workshop/VMs \
  --destination-tart-home /Users/danielraffel/VMs \
  --staging /Volumes/Workshop/VMs/transfers/m1
```

At an authorized idle boundary, add `--apply` to export and checksum a resumable
staged archive. Add `--import` only after verifying free disk; import uses a
unique `.incoming.TIMESTAMP` name and never replaces the active golden. The
operator separately reviews provenance, renames, canaries one job, and rolls
back before increasing fleet or merge-queue concurrency.

For planning, transfer wall time is `export + compressed_bytes / measured_link
+ checksum + import`. Do not estimate from Tart's 150 GB logical disk. Record
the exported `.tvm` byte count and a LAN probe at the idle boundary; a Tailscale
fallback is availability, not permission to accept an impractically slow copy.

## Canary boundary

The earliest safe live canary is when all existing M3 jobs have terminal
receipts, M1 still reports no running Tart VM/owned worker, M1's stale states are
reaped with the supported doctor command, the selected golden is provenance-
verified, and the new plists are installed while the pool remains off. Enable
one M1 lane for one exact low-risk job, then drain it again. Do not change
GitHub merge-queue `max_entries_to_build` until that receipt proves clone, cache,
lease, teardown, offline/rejoin, and log behavior.
