# Fleet profile sources

TartCI uses the checked-in `profiles/` and `fleet/advertised-labels.json` by
default. This keeps a normal checkout self-contained.

An operator can select an installed private source for offline fleet tooling:

```sh
export TARTCI_FLEET_SOURCE_ROOT=/path/to/fleet-source
export TARTCI_FLEET_SOURCE_COMMIT=0123456789abcdef0123456789abcdef01234567
export TARTCI_FLEET_SOURCE_REPOSITORY=ORG/private-fleet-authority
```

The source directory must contain:

```text
profiles/*-macos-fleet.toml
fleet/advertised-labels.json
.tartci-fleet-source.json
```

The last file is the export marker. It names the repository and commit and the
git blob id of every other file, and it is written when the source is exported
at that commit. A file cannot name the commit that contains it, so the marker,
not `generated_from.commit` in the published supply, proves which commit the
directory holds. `generated_from.repo` and `.commit` are informational;
`generated_from.profiles` must name the exact profile files, and
`generated_from.profiles_sha256` must still match the profiles. Profiles must be
regular, parseable TOML files. Symlinks and malformed profiles are refused
before a consumer reads them.

The operator's fleet backup may use a different layout and may redact paths or
credentials. It is a record for recovery and review. It must be converted into
the export layout above from an unredacted, reviewed configuration before it is
used as a TartCI source.

## Self-update keeps the source

Name the private repository in `~/.config/tartci/self-update.toml`:

```toml
[fleet_source]
repository = "ORG/private-fleet-authority"
canary = "m1"          # the host that installs a new source commit first
```

Every host names the same canary. Without a canary the setting refuses; a
`[fleet_source]` that is not a table, or has another key, refuses the run as a
configuration error.

A move is one host first:

- The canary adopts the repository's `main`. When a self-update installs that
  commit and verifies the host (pool on and its checks), it records
  `{repository, commit, verified_at}` in
  `~/.tartci/state/self-update/fleet-source-verified.json`.
- Every other host reads that record from the canary over SSH, aged on the
  canary's clock. It adopts the canary's commit once it has served there for
  30 minutes, and otherwise stays on the commit it was installed from (or on
  the public profiles if it has never used the private source).
- A commit the canary fails to install or verify is never recorded, so no
  other host moves to it. A failed reinstall is not retried within the
  self-update rate limit (6 h).

Each self-update run then:

1. reads the selected commit through the GitHub API, using the same `ghapp
   api` path as the TartCI repository, so no git credential is needed;
2. exports `profiles/*-macos-fleet.toml` and `fleet/advertised-labels.json` at
   that commit into `~/.local/share/tartci/fleet-sources/<commit>/`, checking
   each file against its blob id, and makes the export read-only;
3. selects it by setting the three variables above for itself and for the
   installer it runs;
4. reinstalls the current generation when the installed receipt was built from
   a different source commit, unless that commit's profiles and published supply
   are the bytes already installed (the receipt's `tree_sha256`). A commit that
   changes only other files in the private repository, such as a backup, then
   drains nothing; the host stays pinned to the commit it was installed from.

The install receipt records the source commit and the SHA-256 digests of the
published supply and the complete source tree; the attempt receipt also records
the digest of the profile set. Those digests identify the bytes used by that
installation; they do not replace the commit marker.

A canary that is not a published host id refuses the run on every other
host, naming the published ids, rather than leaving them on their installed
commit with nothing saying why. A host asked for a verified commit exports
exactly that commit: an answer naming any other commit refuses.

A plan (`--plan`) writes and prunes nothing: it uses an export that already
exists and otherwise reports that an apply would export it.

Exports are immutable per commit, so an agent installed from one commit keeps
reading its own directory while a newer commit is exported beside it. The
newest three exports and the installed one are kept.

Once the setting is present, an unreadable or damaged source refuses the run
(`UNKNOWN`, recorded in `skew.json`); it never falls back to the public
profiles. Without the setting nothing changes.

Self-update takes the source only from `self-update.toml`. It clears any
`TARTCI_FLEET_SOURCE_*` variables it inherits (from a shell that exported them
for offline tooling, or from an agent plist) before it selects, so removing
`[fleet_source]` always moves the host to the public profiles.

A source move that installs and then fails verification is rolled back like a
new generation: the previous source is reinstalled, even though the TartCI
generation did not change.

TartCI validates the source, its export marker and its generated profile digest before reading it. If an explicit source
is missing, empty, or invalid, the command fails. It does not silently use the
public checkout instead. `TARTCI_FLEET_SOURCE_COMMIT` and
`TARTCI_FLEET_SOURCE_REPOSITORY` are required for an external source. They
identify and pin the desired-state revision so receipts and status output can
identify exactly what was selected.

This setting does not move or publish fleet profiles. Keep host identities,
paths, labels, receipts, and snapshots in a private operator repository. Keep
generic schemas and examples in TartCI.

## Settings that must arrive through the canary

A profile change that turns on a host-side action reaches the fleet only
through this path, one host first. In particular, no profile sets
`worktree_cleanup.apply = true` unless that change came through a canary
verified commit of the private source: enabling cleanup by any other route
(an edited installed profile, a public-profile change) skips the one-host
proof. The structural limits in `macos_fleet_lanes.worktree_cleanup_problem`
bound what the setting can do; the canary bounds who gets it first.

## Sealed-launcher hosts

A host with a **v1** sealed TartCI Launcher (`[launch_helper]` in its profile:
the external-volume hosts) refuses `[fleet_source]` before reading the source
or draining anything. The v1 launcher's policy digest covers the profile, and
a source move reinstalls the same generation without rebuilding the launcher,
so the installer would refuse the live launcher after the drain and roll back
on every turn. The refusal names the cause and the fix (remove
`[fleet_source]` on that host, or move it to launcher v2). A v1 host whose
launcher v2 opt-in file is present still refuses until it has installed v2.

A **launcher v2** host takes `[fleet_source]`. Its stable app holds only host
policy (`policy.json`), so a source move is measured like any update: when the
app's inputs digest, signing identity and team are unchanged the move is a
same-generation refresh that writes and signs a new launch set and keeps the
app; when they change (the moved profile changed `host.home`, `host.tart_home`
or the launch helper's team or identifier) it is a launcher change that waits
for the exact `v2 inputs=... identity=... team=...` acknowledgement and then
rebuilds the app.
