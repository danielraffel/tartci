# tartci

[![lint](https://github.com/danielraffel/tartci/actions/workflows/ci.yml/badge.svg)](https://github.com/danielraffel/tartci/actions/workflows/ci.yml)

tartci turns an Apple Silicon Mac into a pool of disposable CI machines. It
boots a fresh macOS, Linux or Windows virtual machine for each GitHub Actions
job, runs the job, and throws the machine away. Build caches live on the host,
so each new VM starts warm. A per-host budget keeps CI from starving everything
else on the Mac.

## Who it's for

Use tartci if you:

- own one or more Apple Silicon Macs and want them to run your GitHub Actions
  jobs;
- want every job to start from a clean machine, without paying for hosted
  macOS minutes;
- share those Macs with other work (local builds, a DAW, an editor) and need
  CI to stay inside a budget.

tartci is a set of shell and Python scripts plus docs. It holds no VM images:
those are baked or pulled onto each host and never committed.

The public contract is the CLI, provider behavior, and per-project manifests.
Host profiles, runner labels, receipts, host paths, and fleet snapshots describe
one deployment. Do not copy those values into a new installation. Keep a real
fleet's desired state and live receipts in its private operator repository;
examples in this repository are not requirements for using TartCI.

## How it works

```
GitHub Actions queues a job with self-hosted labels
        |
        v
tartci on a Mac sees a matching queued job
        |
        v
takes a lease from the host budget (CPU, memory, disk)
        |
        v
clones a golden VM image (copy-on-write, seconds)
and mounts the host's build caches
        |
        v
registers a one-job runner in the VM; GitHub assigns the job
        |
        v
job finishes -> VM deleted, lease released
```

- **macOS and Linux** guests run on [Tart](https://github.com/openai/tart)
  (Apple Virtualization).
- **Windows** guests run on QEMU, since Apple Virtualization cannot install
  Windows.
- **Golden images** are built or tagged per lane. `tartci prepare linux` builds
  the Pulp Linux golden, `tartci prepare macos` provides macOS golden helpers,
  and `tartci windows golden` snapshots a prepared Windows VM. Copy Windows
  images with `tartci goldens sync`; copy Tart images with `tartci tart-image-sync`.
- **LaunchAgents** in `launchd/` keep the serving loops running across
  reboots.
- **`tartci pool drain` and `tartci pool on`** take a Mac out of the pool and
  bring it back. Drain lets the running job finish.

## Quick start

Requirements: an Apple Silicon Mac on macOS 15 or later, and
[Homebrew](https://brew.sh).

```bash
git clone https://github.com/danielraffel/tartci.git && cd tartci
./tartci doctor          # check prerequisites; changes nothing
./tartci setup           # install Tart, QEMU and tools; create local stores
./tartci status --json   # this host's providers, capacity and role
./tartci host-profile    # the CPU and memory budget tartci derived for this Mac
```

To create a profile, follow the [profile-authoring guide](docs/profile-authoring.md)
and its clean inline example. The public
[`normal-local-fast.toml`](profiles/normal-local-fast.toml) profile is a fuller
operator reference. A private fleet source can replace the checked-in
`*-macos-fleet.toml` profiles and advertised labels used by fleet tooling;
TartCI validates its commit and digests when installing, starting with the
canary host.

To serve one GitHub Actions job from a fresh macOS VM, you need a macOS
golden image on this host and a `gh` login that may register self-hosted
runners on your repository. Point tartci at the repository, the golden and the
labels your workflow asks for:

```bash
export TARTCI_RUNNER_REPO=owner/repo
export TARTCI_MACOS_GOLDEN=my-macos-golden
./tartci serve macos --once --labels self-hosted,macOS,ARM64,my-label
```

`--once` serves one job and exits. `--loop` keeps serving; the LaunchAgent
templates in `launchd/` run it that way. The [runbook](docs/runbook.md) covers
baking goldens, onboarding a host and installing the agents.

## What it doesn't do

- It does not schedule jobs. GitHub assigns jobs to runners by label.
- It does not merge, queue or cancel pull requests.
  [Shipyard](https://github.com/danielraffel/Shipyard) or your own tooling
  does that.
- It does not run x86_64 natively. Guests are arm64. x86_64 Linux code can be
  cross-built and smoke-tested under Rosetta, but that is not a substitute for
  an x86_64 runner.
- It does not ship VM images. You bake or copy them on each host.
- The on-demand `tartci up` lanes and the bake recipes in `providers/` build
  [Pulp](https://github.com/Generous-Corp/pulp) today. The serving lanes take
  any repository, golden image and labels from the environment.

## Tart and TartCI

[Tart](https://github.com/openai/tart) provides the Apple Virtualization
VM engine. TartCI is an independent project that adds the CI layer: disposable
per-job GitHub runners, host resource budgets, warm caches, Windows through
QEMU, and coordination with [Shipyard](https://github.com/danielraffel/Shipyard).
TartCI is not affiliated with Tart or its maintainers.

## Learn more

- [Runbook](docs/runbook.md): set up a host, build images, and serve jobs.
- [Prepare](docs/prepare.md): what each `prepare` command does and when to use it.
- [Reference](docs/reference.md): resource limits, serving behavior, diagnostics,
  and environment variables.
- [Gotchas](docs/gotchas.md): known failure modes and practical fixes.
- [Onboarding a new repository](docs/new-repo-agent-guide.md): connect a repo to
  an existing TartCI host.
- [LaunchAgents](launchd/README.md) and the
  [published label table](fleet/README.md).
- [AGENTS.md](AGENTS.md): a dense reference for coding agents.

## Contributing

Run the same check CI runs before you push:

```bash
./scripts/lint.sh   # shellcheck, bash -n, py_compile and TOML parsing
```

## License

See [LICENSE](LICENSE).
