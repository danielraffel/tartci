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

Every managed lane also declares its exact non-Default GitHub runner group.
The renderer exports that value as `TARTCI_RUNNER_GROUP_ID`; omitting it would
silently register disposable runners in Default even when a workflow requests
a protected group, leaving the job queued while VMs repeatedly start and exit.
The checked-in IDs were verified against the live organization runner-group
API and workflow routing on 2026-08-27:

| Lane | Group ID | GitHub runner group | Live access evidence |
|---|---:|---|---|
| Pulp | 3 | `pulp-trusted-build` | Selected to Pulp and restricted to protected `pulp/.github/workflows/build.yml@refs/heads/main` |
| Forge | 11 | `forge-pr-safe-build` | Selected to Forge and restricted to protected `forge/.github/workflows/build.yml@refs/heads/main` |
| Vellum | 8 | `vellum-macos-build` | Selected to Vellum; its GPU and README macOS workflows use `VELLUM_MACOS_RUNS_ON_JSON` |

Runner-group IDs are organization state, not values to infer from repository
names. Re-verify the live group name, selected repository, and protected
workflow access before changing any checked-in ID.

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

No V2 Pulp runner advertises the legacy `pulp-gate-fast` label. Existing
measurements put it materially behind M3/M5, and the settled placement contract
keeps M1 at `vm` lease priority with a ten-minute queue-age delay. M1 still
renders both governed supervisor identities so temporary free capacity is not
lost to a static one-slot declaration; the governor decides whether either can
admit a complete guest. M3 and M5 retain gate lease priority without leaking the
legacy selector into JIT registration or idle capability reporting.

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
