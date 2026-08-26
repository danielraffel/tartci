# Dynamic macOS fleet worker

This design lets one Mac offer disposable Tart capacity to Pulp, Forge, and
Vellum without dedicating the machine to any repository. Each repository has a
small queue-aware supervisor, but all supervisors use the same host lease store,
capacity cap, durable pool switch, Tart store, and cache. A supervisor boots a
VM only after it sees an exact matching queued job and acquires a governed
lease. Failed queue discovery is fail-closed.

`profiles/m1-macos-fleet.toml` is the first host declaration. M1 and M5 use
`$HOME/VMs`; M3 uses `/Volumes/Workshop/VMs`. Paths are absolute in rendered
LaunchAgents because minimal launchd/SSH environments must not guess `HOME`,
`PATH`, or `TART_HOME`.

## Stage without activation

```sh
./tartci fleet-macos validate profiles/m1-macos-fleet.toml
out="$(mktemp -d)"
./tartci fleet-macos render profiles/m1-macos-fleet.toml --output "$out"
for file in "$out"/*.plist; do plutil -lint "$file"; done
# Before any separately authorized install/bootstrap on M1:
ssh m1-lan 'mkdir -p "$HOME/Library/Logs/tartci"'
```

Rendering never installs or loads a LaunchAgent. Stable host/lane identifiers
are local operational vocabulary; actual GitHub runner names remain ephemeral
per boot through the existing Tart provider.

Pulp exposes only the merge-group event tier. A lower PR tier is intentionally
absent until the provider can recheck higher-priority demand after the bounded
Shipyard admission wait; otherwise a PR could consume a slot for a merge-group
that arrived during admission. Forge and Vellum use their
current generic selectors until their workflows publish reviewed event-class
labels. Adding those labels is a workflow/governance change, not a fleet-render
side effect.

M1 deliberately does not advertise Pulp's `pulp-gate-fast` label. Existing
measurements put it materially behind M3/M5, and the settled placement contract
keeps it on generic rollback/non-required work with a ten-minute queue-age
delay. Making M1 a second required merge-group worker requires fresh comparative
timings and a separately reviewed contract/workflow change; this host profile
does not silently reverse that incident-bought decision.

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
