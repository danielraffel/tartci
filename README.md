# tartci — local CI VMs on macOS (Tart + QEMU + Shipyard)

Stand up **fast, cached, disposable Linux / Windows / macOS build VMs on an Apple
Silicon Mac**, wired to GitHub runners + Shipyard, so you can build & test a repo
locally instead of (or alongside) GitHub-hosted runners. Headless CI is the
priority; the same goldens double as GUI **bench** VMs you can open in UTM to test
plugins in a DAW.

> Project-agnostic. Pulp is the first consumer, but any repo (e.g. a Pulp-based
> plugin) plugs in with one `vm-image` manifest. This repo holds **scripts +
> configs + docs only — never the large VM images** (those stay local / pulled /
> baked, and are gitignored).

## What you get
- **macOS + Linux** via **Tart** (Apple Virtualization) — native speed, CoW
  clones, host-mounted ccache warm across clones.
- **Windows** via **standalone QEMU** (hvf) — AVF can't install Windows (no inbox
  virtio-blk driver; black installer display), so Windows is first-class on QEMU
  with an NVMe disk + `ramfb` display.
- Ephemeral per-job clones, host-mounted caches, SSH access + log collection,
  and a tiny metrics dashboard.

## Quick start (the goal: one command)
```bash
git clone <this-repo> tartci && cd tartci
./tartci setup            # installs tart/qemu/sshpass, pulls base images, host config
./tartci up linux         # bring up a Linux build VM for the current repo's manifest
./tartci bench windows    # clone the golden → open in UTM for GUI/DAW testing
```
(See `docs/runbook.md` for the from-scratch, gotcha-by-gotcha guide while the
one-command wrappers are being finished.)

## Per-project use
A repo drops a `.shipyard/vm-image.toml` (see `manifests/`) declaring its
`os`/`arch`/`toolchain`/`packages`/`caches`/`mounts`. tartci bakes or clones a
golden from it — zero hand-provisioning. Non-generic needs (extra SDKs, a special
toolchain) go in that manifest.

## Optional pulp-CLI integration
Soft dependency: if installed, `pulp doctor` reports "local CI VMs: available",
and `pulp vm up <os>` delegates here. If absent, Pulp is unaffected. Maximally
useful when present; never a punishment when missing.

## Layout
```
providers/   tart-macos · tart-linux · qemu-windows   (per-OS provision + run)
manifests/   example vm-image.toml per project profile
metrics/     dashboard.py + report.py (file-based; no server)
bench/        helper to clone a golden → open in UTM for GUI testing
docs/        runbook.md (from-scratch), gotchas, design
```

## Status
Bring-up proven on a Mac Studio (2026-06): Linux build+test+cache green; Windows
24H2-ARM build green + tests pass. See `docs/runbook.md` for the recipe and the
hard-won gotchas (ISO 512-byte alignment, ramfb vs virtio-gpu, all-USB install
media, MSVC partial-install nuke, BOOTAA64 auto-boot, …).
