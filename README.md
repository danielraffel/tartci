# tartci — local CI VMs on macOS (Tart + QEMU + Shipyard)

[![lint](https://github.com/danielraffel/tartci/actions/workflows/ci.yml/badge.svg)](https://github.com/danielraffel/tartci/actions/workflows/ci.yml)

Stand up **fast, cached, disposable Linux / Windows / macOS build VMs on an Apple
Silicon Mac**, wired to GitHub runners + [Shipyard](https://github.com/danielraffel/shipyard), so you can build & test a repo
locally instead of (or alongside) GitHub-hosted runners. Headless CI is the
priority; the same goldens double as GUI **bench** VMs you can open in UTM to test things like
plugins in a DAW.

> Project-agnostic. [Pulp](https://github.com/danielraffel/pulp/) is the first consumer, but any repo (e.g. a Pulp-based
> plugin) plugs in with one `vm-image` manifest. This repo holds **scripts +
> configs + docs only — never the large VM images** (those stay local / pulled /
> baked, and are gitignored).

## What you get
- **macOS + Linux** via **[Tart](https://github.com/cirruslabs/tart/)** (Apple Virtualization) — native speed, CoW
  clones, host-mounted ccache warm across clones.
- **Windows** via **standalone QEMU** (hvf) — AVF can't install Windows (no inbox
  virtio-blk driver; black installer display), so Windows is first-class on QEMU
  with an NVMe disk + `ramfb` display.
- Host-mounted caches, SSH access + log collection, and a tiny metrics dashboard.

> **Project Status:** a working lab toolkit, hardening toward turnkey. Wired +
> proven today: the **Linux Tart lane** (`tartci up linux` — ephemeral clone →
> warm host ccache → full build + ctest → discard, host-validated green), the
> **Windows QEMU lane** (`tartci up windows` — per-job CoW overlay → dynamic SSH
> port → MSVC arm64 build + ctest → discard; host-validated: overlay/SSH
> mechanics + full 735-target compile green, ctest is the golden's proven 7287/7303),
> the **x86_64 cross/emulation smoke lane** (`tartci up linux --target-arch
> x86_64` — cross-compile + run tests under qemu-user-static; `--self-test`
> proves the toolchain+emulator chain golden-agnostically), the metrics, and
> `tartci doctor/bench/metrics`. **Not yet wired:** the `tart-macos` provider
> (`tartci up macos` points at the runbook) and the Windows/Prism cross lane
> (Linux/qemu-user is wired; Windows-on-ARM x64-via-Prism is documented as a
> follow-up). Emulated x64 is a SMOKE/debug signal only — GitHub-hosted x64
> stays the authoritative x64 gate.

## Quick start
```bash
git clone <this-repo> tartci && cd tartci
./tartci doctor           # report host prereqs + golden/bench stores (always safe)
./tartci setup            # brew-install tart/qemu/sshpass + create local stores
./tartci bench windows    # clone the Windows golden → open in UTM for GUI/DAW testing
./tartci metrics report   # text build/cache table (or `metrics dashboard` for HTML)
./tartci up linux         # ephemeral Linux build+test of a ref (clone→build→ctest→discard)
./tartci up linux --target-arch x86_64   # cross-build x64 + run tests under qemu-user (SMOKE)
./tartci up windows       # ephemeral Windows build+test (CoW overlay→build→ctest→discard)
./tartci windows run      # boot the Windows installer/single-operator VM (from-scratch)
```
`tartci doctor`, `bench`, `metrics`, `up linux`, and `up windows` are wired
today. `tartci up linux [--ref <git-ref>] [--no-gpu] [--keep]` clones the
`pulp-linux-build` golden, mounts the host ccache, and builds + ctests in-guest.
`tartci up windows [--ref <git-ref>] [--smoke] [--keep]` makes a per-job CoW
overlay off the Windows golden on a dynamic SSH port (concurrent-safe), builds
GPU-off under MSVC arm64, then discards the overlay (see `providers/`). `tartci
up macos` still points at the runbook. See `docs/runbook.md` for the
from-scratch, gotcha-by-gotcha guide and `docs/new-repo-agent-guide.md` to
onboard a new repo.

### x86_64 cross / emulation (smoke, not a gate)
The guest is ARM64 (Apple Virtualization has no x86). `tartci up linux
--target-arch x86_64` cross-compiles for x64 (gcc/g++-x86-64-linux-gnu) and runs
the test subset under **qemu-user-static** (binfmt) — a first line of defense for
atomics / SIMD / ISA-divergent behavior, but **not** a gate: GitHub-hosted x64
stays authoritative (sanitizers, SIMD/Highway dispatch, and RT timing are
unreliable emulated). It defaults **GPU OFF** because the prebuilt Skia maps both
Linux arches to the same `libskia.a` path (`docs/gotchas.md`); `--gpu` requires
an explicit x64 Skia tree via `--skia-dir`, else it fails loud. `--self-test`
proves just the toolchain+emulator chain with a trivial binary (golden-agnostic).
The manifest declares this with `target_arch`/`cross` + an `[emulation]` table —
see `manifests/example.x64.toml`.

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
providers/   tart-linux · qemu-windows  (per-OS provision + run; tart-macos: runbook + Pulp tools/ci)
manifests/   example vm-image.toml per project profile
metrics/     dashboard.py + report.py (file-based; no server) + sample.jsonl
bench/        helper to clone a golden → open in UTM for GUI testing
scripts/     lint.sh — repo hygiene gate (shellcheck + bash -n + py_compile + TOML)
docs/        runbook.md (human from-scratch) · new-repo-agent-guide.md (agent onboarding) · gotchas.md
```

## Contributing checks
Run the same gate CI runs before you push — it's one script, portable to macOS's
stock bash 3.2:
```bash
./scripts/lint.sh    # shellcheck -S warning + bash -n on every shell script
                     # (incl. the extensionless `tartci` dispatcher), py_compile
                     # on metrics/*.py, and a parse check on manifests/*.toml
```
`.github/workflows/ci.yml` runs this on every push + PR. It guards the class of
bug that only bites at run time on a CI host — e.g. a help line that executes as
a command before `set -e`.

## Metrics (file-based, no server)
Per-run build/configure/ctest times + cache-hit% land in a `metrics.jsonl` (one
JSON object per line). Two zero-dependency Python scripts read it:
```bash
python3 metrics/report.py    metrics/sample.jsonl                 # text table
python3 metrics/dashboard.py metrics/sample.jsonl index.html      # self-contained HTML
```
`sample.jsonl` carries the real 2026-06 bring-up numbers so the dashboard renders
out-of-box (a "Last run" hero + per-OS configure/build/ctest + cache-warmth
trends). Your own `metrics.jsonl` is gitignored.

## Status
Bring-up proven on a Mac Studio (2026-06): Linux build+test+cache green; Windows
24H2-ARM build green + tests pass. See `docs/runbook.md` for the recipe and the
hard-won gotchas (ISO 512-byte alignment, ramfb vs virtio-gpu, all-USB install
media, MSVC partial-install nuke, BOOTAA64 auto-boot, …).
