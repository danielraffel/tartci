# Authoring a TartCI profile

TartCI profiles describe routing policy and host capability. They do not
contain secrets, VM images, runner tokens, or live receipts. TartCI ships
checked-in public profiles and uses them by default; a private fleet source can
replace the `*-macos-fleet.toml` profiles and advertised labels used by fleet
tooling for a deployment with private host paths, identities, labels, or
receipts. It does not replace generic routing profiles used by `tartci profile`.

## Start with a public example

The inline profile below is the clean, repository-neutral starting point. The
[`profiles/normal-local-fast.toml`](../profiles/normal-local-fast.toml) file is
a fuller operator reference for repository-scoped policy, ordered fallback,
hosted fallbacks, and separate coverage/release lanes; it contains real
operator vocabulary and should not be copied as a private fleet configuration.
The
[`docs/examples/host-network-profile.toml`](examples/host-network-profile.toml)
file is a separate network configuration example, not a routing profile.

Copy the inline example to `profiles/example-profile.toml`. Keep the `name`
value equal to the filename stem, then replace the repository and target
identifiers. Target IDs are profile vocabulary, not GitHub labels:

```toml
name = "example-profile"
description = "Public profile authoring example."

[repo."example/project".pr.macos]
strategy = "ordered-fallback"
targets = ["builder.macos-arm64-vm", "github.macos-arm64"]
github_variable = "EXAMPLE_MACOS_RUNS_ON_JSON"

[targets."builder.macos-arm64-vm"]
provider = "tartci"
arch = "arm64"
ephemeral = true
runs_on_json = ["self-hosted", "macOS", "ARM64", "example-build"]

[targets."github.macos-arm64"]
provider = "github"
arch = "arm64"
runs_on_json = ["macos-15"]
```

The target list is ordered: a live fleet resolver selects the first healthy
target and falls back only when the earlier target is unavailable. The
read-only `plan` command selects the first configured target and does not probe
health. Keep coverage and
secret-bearing work on explicitly clean or hosted targets rather than silently
sharing a warm build lane.

## Validate before sharing

Run the read-only checks from the repository root:

```sh
./tartci profile validate example-profile
./tartci profile explain example-profile --repo example/project
./tartci profile plan example-profile --repo example/project
```

`validate` checks provider names and that every lane target exists in the
catalog. `explain` shows the fallback chain and the generated `runs-on`
selector. `plan` prints the
read-only variable changes a caller would apply; it does not change GitHub or
any host.

Before committing, also run:

```sh
./scripts/lint.sh
```

## Keep operational profiles private

When a deployment needs private host paths, identities, labels, receipts, or
other recovery state, keep those values in a private operator repository. A
private fleet source must contain the exported layout below, plus its
`.tartci-fleet-source.json` marker:

```text
profiles/*-macos-fleet.toml
fleet/advertised-labels.json
.tartci-fleet-source.json
```

Keep the source files at a clean, reviewed commit of the private fleet
repository. TartCI's source exporter creates the immutable
`.tartci-fleet-source.json` marker, which records that repository, commit, and
file blob IDs. The generated supply records `profiles_sha256`; the installed
receipt carries `published_supply_sha256` and `tree_sha256`, with the tree
digest covering the profile bytes. Install it through the canary-gated
`[fleet_source]` self-update path described in
[`fleet-profile-source.md`](fleet-profile-source.md). Never put private host
names, filesystem paths, runner labels, receipts, or credentials into a public
example.
