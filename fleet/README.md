# Published fleet supply

`fleet/advertised-labels.json` says which GitHub Actions label sets the tartci
macOS fleet registers, per host and lane, and which workflows each registration
mints runners for. Any project can read it to answer "will a job with these
`runs-on` labels reach a tartci machine?" without asking a person or reading
tartci's code.

- **Stable path:** `fleet/advertised-labels.json` on `main`
- **Raw URL:** <https://raw.githubusercontent.com/danielraffel/tartci/main/fleet/advertised-labels.json>
- **Schema:** `tartci.advertised-labels/v1`

```json
{
  "schema": "tartci.advertised-labels/v1",
  "generated_from": {"repo": "danielraffel/tartci", "commit": null,
                     "profiles": ["profiles/m1-macos-fleet.toml", "..."]},
  "registrations": [
    {"profile": "m3-macos-fleet", "host_id": "studio", "lane": "pulp-gate",
     "repo": "Generous-Corp/pulp", "assignment_mode": "event-class-v2",
     "class_label": "pulp-build-pr-head",
     "labels": ["self-hosted", "macOS", "ARM64", "pulp-build", "pulp-build-vm",
                "pulp-build-pr-head"],
     "workflows": ["Build and Test"]}
  ],
  "persistent_runners": [
    {"profile": "m5-macos-fleet", "host_id": "m5",
     "launchd_label": "actions.runner.danielraffel-pulp.pulp-preamble-m5",
     "runner_name": "pulp-preamble-m5"}
  ]
}
```

`labels` is the exact set a registration passes to `generate-jitconfig`.
`hosts` (additive) lists each profile's `host_id` and `ssh`, the alias other
fleet hosts reach it by (null means the convention `tartci-<host_id>`); `tartci
fleet-macos self-update` reads it to check its peers one host at a time.
`persistent_runners` is additive to v1: host-owned Actions services whose
labels are set at registration outside tartci, so only their name is declared.

## Reachability rule

A job is reachable by a registration when all three hold:

1. its `runs-on` labels are a subset of `registration.labels` (case-insensitive,
   GitHub's own matching);
2. its workflow name is in `registration.workflows` (the supervisor only mints a
   runner when it sees queued work for these workflows);
3. its repository equals `registration.repo`.

GitHub assigns a runner by labels alone, so once a runner exists it can take
any job whose labels fit, including a workflow it was not minted for.
`supply_observed.py` reports that as `UNDECLARED_OBSERVED`.

## What this file is NOT

It is the **declared** supply: generated from the checked-in `profiles/`.
It is not live. A host can run a different installed profile, be drained or
offline, or be down to zero registrations at idle (normal for ephemeral lanes).
Check the other two layers below before relying on it.

## How it is produced and kept current

Every `profiles/*-macos-fleet.toml` is included, found by glob, so adding a host
means adding its profile and nothing else. There is no host list anywhere.

```bash
./tartci fleet-macos advertised-labels --publish > fleet/advertised-labels.json
./tartci fleet-macos advertised-labels --check fleet/advertised-labels.json   # exit 1 if stale
./tartci fleet-macos advertised-labels --all     # human view, with the current HEAD commit
```

The committed file carries `generated_from.commit: null` on purpose: a file
cannot name the commit that contains it, so any value would be stale the moment
it is committed. Its provenance is the git ref it was read from (for the raw
URL, `main`). `--all --json` prints the current `HEAD` for ad hoc use.

CI (`scripts/test_fleet_supply.py`, run by `.github/workflows/ci.yml`)
regenerates the file and fails if the committed copy differs, so it cannot
drift from `profiles/` inside the repository.

## How to fact-check supply

Git, the installed hosts and GitHub can disagree. Check each layer against the
declared file:

| Layer | Where it runs | Command |
|---|---|---|
| **Declared** (git) | anywhere | `./tartci fleet-macos advertised-labels --check fleet/advertised-labels.json` |
| **Installed** (host profile) | on a fleet host, read-only | `tartci fleet-macos verify-supply [--json] [--published FILE\|URL]` |
| **Observed** (GitHub job history) | anywhere with GitHub read | `scripts/supply_observed.py --repo OWNER/REPO [--lookback-hours N] [--json]` |

**Installed:** `verify-supply` computes the registrations of the host's
installed `~/.config/tartci/macos-fleet-profile.toml` with the same rule and
compares them with the declared registrations for that profile's `host_id`.
Per lane: `MATCH`, `INSTALLED_ONLY` (the host registers something git does not
publish), `DECLARED_ONLY` (git publishes something the host does not register),
or `LABELS_DIFFER` (labels, workflows, repo or mode). Exit 0 all match, 1 any
mismatch, 2 `UNKNOWN` (unreadable profile or published file, or a `host_id`
the file does not declare); unknown is never reported as a match.
`--published https://raw.githubusercontent.com/danielraffel/tartci/main/fleet/advertised-labels.json`
checks against `main` instead of the local checkout. `tartci doctor fleet`
reports the same comparison as its `supply` finding, next to `profile_drift`.

The installed layer also surfaces without anyone asking, report-only:
`tartci pool status` (text and `--json` under `fleet.config`) prints
`profile drift:` and `supply:` lines, where a missing or unreadable verdict is
`UNKNOWN`, never ok; the launchd watchdog heal pass (every 300 s) logs a
`WARN config:` line when either verdict is not ok, rate-limited to once per
distinct verdict per 6 hours, and records it in its `--json` output; `pool on`
and `pool on --plan` print both verdicts first. None of them refuses or acts:
refusing on drift would turn a configuration difference into a capacity
outage. `verify-supply` names the tartci commit the published file came from
and the commit of the host's installed generation when they differ, so a lane
change between the two is attributed rather than mistaken for drift. Each
installed generation carries its own `fleet/advertised-labels.json`.

**Observed:** `supply_observed.py` reads completed Actions jobs for one
repository and attributes each self-hosted job to a declared registration by
runner name (`<host_id>-<lane>[-slotN]-<NN>-<pid>-<boot>`, or a persistent
runner's registered name) and labels:

- `OBSERVED` (count, last seen): the host+lane served jobs within its declaration
- `NOT_OBSERVED`: it served none while jobs it could serve existed in the window
  (the report says who served them, or `<never assigned>`)
- `IDLE`: no demand for that label set in the window, so absence means nothing
- `UNDECLARED_OBSERVED`: a runner name or label set no declaration explains —
  the machine ran something git does not declare
- `PERSISTENT_OBSERVED`: a declared persistent runner served jobs

It is bounded (`--max-run-pages`, `--max-runs`, `--max-job-pages`) and prints
`TRUNCATED` when a bound cut the scan. It uses `ghapp` when present, else `gh`
(override with `--gh-cli` or `TARTCI_GH_CLI`); `ghapp` derives its repository
from the working directory, so run it from a checkout of that repository.
`--jobs-file` classifies a saved `actions/runs/<id>/jobs` response offline.
Exit 0 clean, 1 `NOT_OBSERVED` or `UNDECLARED_OBSERVED` present, 2 unreadable.
