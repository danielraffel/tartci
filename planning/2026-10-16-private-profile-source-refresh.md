# Private fleet profile source refresh

Status: implementation merged; external-source rollout intentionally gated

## Goal

Keep TartCI's checked-in profiles as the default for ordinary users while
allowing an operator to select a pinned, immutable fleet profile source with
clear provenance. The public repository must not contain private fleet paths,
addresses, credentials, or host snapshots.

## Current authority

The private fleet record is maintained outside this repository. It is a
snapshot and rebuild record, not a directly consumable TartCI profile source.
Its collector redacts sensitive values and stores profiles below per-machine
paths, while TartCI's external-source contract requires a validated export with
`profiles/*-macos-fleet.toml`, `fleet/advertised-labels.json`, and an export
marker. A redacted snapshot must therefore never be selected as a live source.

The private authority was compared with the current TartCI profile contract.
It does not yet contain a complete validated export, so no private snapshot is
being copied into this repository or activated on a host as part of this
change.

## Implementation

1. Strengthen source resolution so an explicit source is rejected before use
   when its files are not regular files, its profile metadata does not name the
   exact exported profile set, or a profile is not parseable TOML.
2. Keep the default source path unchanged when no external source is selected.
3. Include source tree, profile, and published-supply digests in selected-source
   provenance so receipts can identify the exact bytes used.
4. Add focused tests for malformed profiles, metadata/profile-set drift,
   symlinked files, and provenance digests.
5. Keep the private fleet record as the backup and desired-state authority. A
   future generated export may be used only after it is produced from a live,
   unredacted host configuration and passes the same validation.

## Validation gates

- Run the focused source and self-update tests in a fresh worktree.
- Run the related macOS lane/source tests and `./scripts/lint.sh` on M1.
- Ask Ultra for adversarial review of the authority decision and changes.
- Confirm the public-path audit finds no private addresses, paths, tokens, or
  redacted snapshot data.
- Do not configure or roll out an external source until a real unredacted
  export exists and its receipt, profile validation, canary, soak, and rollback
  gates pass.

## Rollout status

No fleet mutation has been performed. Current installed hosts report the
ordinary checked-in profile path and no configured external source. The canary
and rollback steps remain gated on a validated private export.

## Scope boundary

This change improves source integrity and receipt evidence. It does not repair
volume I/O, Tart inventory stalls, pool transitions, or guest failures. Those
need separate, narrowly scoped changes with their own canary and rollback
evidence.

## Progress update

The resolver now rejects symlinked source files, malformed profile TOML,
profile metadata that names a different file set, malformed repository
coordinates, and non-UTF-8 marker or supply files. Selected-source receipts now
include the profile, published-supply, and complete-tree digests. The focused
source, source-sync, self-update, published-supply, lane, and installer tests
pass. The exact published head was checked out in an isolated M1 worktree;
M1 lint and the source, source-sync, and self-update suites passed there.

The implementation was merged to `main` in
https://github.com/danielraffel/tartci/commit/f0cc51c1ec3e2f635669134ff186ea8d9e681d3e
through https://github.com/danielraffel/tartci/pull/589. It has not been
installed on a fleet host and no external source is configured.

The private authority still lacks a complete export with the required profile
set and published supply document. A partial export would omit routing and
capacity authority, so it is not being created or rolled out.

The remaining work is operational rather than another public-code change:
produce the authenticated private export, validate it against the merged
resolver, run the least-capacity-critical canary with a receipt, soak it while
watching pool readiness, and prove rollback to the checked-in default. Until
those receipts exist, every host continues to use the public default.

The first Python 3.9 run exposed a compatibility gap: stock Python 3.9 has no
`tomllib`, so valid configured sources were rejected. Export now performs full
TOML validation under the pinned 3.11+ interpreter before writing its immutable
marker; Python 3.9 verifies the marker and resolves the already-validated
bytes. The source, sync, self-update, and system-Python coverage tests pass
locally after this fix.

## Stop conditions

Stop and retain the public default if the private authority remains redacted,
if its generated export cannot be authenticated, if profile validation or
provenance disagrees, if M1 validation fails, or if the canary cannot prove
pool readiness and rollback.
