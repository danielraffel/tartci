# tartci runbook — from-scratch setup (macOS + Linux + Windows)

This is the honest, command-first guide to standing up local CI build VMs on a
fresh Apple Silicon Mac. It mirrors the proven bring-up: **Linux + macOS on Tart
(Apple Virtualization), Windows on standalone QEMU** (AVF can't install Windows).

What's **scripted** vs **manual** is called out per section. Where a wrapper
exists (`./tartci up <os>`), use it; where it doesn't yet, the raw commands here
are the ground truth.

Conventions used below (no operator-specific data — substitute your own):

| Placeholder | Meaning |
|---|---|
| `$TARTCI_HOME` | This repo's checkout root |
| `<vm-store>` | Directory holding VM disks / qcow2 images (gitignored, Spotlight-excluded) |
| `~/.ssh/id_ed25519.pub` | A public key you want injected into guests (any number) |
| `<iso-store>` | Directory holding ISOs (Windows install media, virtio-win) |
| `pulp-linux` / `pulp-win` | SSH host aliases this toolkit maintains for you |

> **One fact that shapes everything: Tart is ARM-only.** Apple
> Virtualization.framework boots **ARM64 guests only** on Apple Silicon. Every VM
> here is arm64. You build arm64 natively; you reach x86_64 via cross-compile +
> emulation (Rosetta on Linux, Prism on Windows) as a *signal*, not an
> authoritative gate. GitHub-hosted x64 stays the required check.

---

## 1. Prereqs + host setup

**Tools (scripted by `./tartci setup`; manual fallback shown):**

```bash
brew install cirruslabs/cli/tart qemu sshpass
# qemu     — Windows VM substrate (hvf accel)
# tart     — macOS + Linux VM substrate (Apple Virtualization)
# sshpass  — non-interactive first-boot SSH during provisioning
```

**A VM store directory, excluded from Spotlight** (large disks should never be
indexed — it wastes IO and CPU):

```bash
mkdir -p <vm-store> <iso-store>
touch <vm-store>/.metadata_never_index    # tells Spotlight to skip this tree
```

Tart keeps its own VM registry under `~/.tart` by default; point heavy disk
stores and qcow2 images at `<vm-store>` and gitignore them. **This repo ships
scripts + configs + docs only — never the multi-GB images.**

**SSH key(s).** You can inject any number of public keys; private keys are
*never* baked into a golden. Have at least one:

```bash
ls ~/.ssh/id_ed25519.pub    # or generate: ssh-keygen -t ed25519
```

Configure the set the provisioner injects via either:

- env `PULP_CI_PUBKEYS` — colon-separated pubkey *file* paths, or
- the manifest `[access].authorized_keys` list (pubkey files),
- default `~/.ssh/id_ed25519.pub`.

**Tailscale (optional but recommended).** Bake it into the Tier 0 golden and run
`tailscale up` once per golden (or headless `--authkey`). You then get a stable
MagicDNS name and can `ssh`/log-pull from anywhere without port juggling. Prefer
the MagicDNS name over the per-boot vmnet IP. Disable Tailscale SSH on persistent
operator boxes to avoid re-auth prompts.

---

## 2. macOS lane (Tart)

Native, fast, CoW clones. **Layered golden tiers** keep re-bakes cheap:

```
base  (cirruslabs macOS) → toolchain (Xcode CLT, brew deps, ccache) → project (Skia/Dawn baked, ccache warm)
```

Summary recipe (the macOS plan has the full detail):

```bash
# 1. Pull the cirruslabs macOS base image
tart pull ghcr.io/cirruslabs/macos-sequoia-xcode:latest

# 2. Bake Tier 0 (toolchain): clone the base, install brew deps + ccache, inject keys
tart clone ghcr.io/cirruslabs/macos-sequoia-xcode:latest macos-build-base
#   ...provision inside (brew bundle, ccache, sshd keys, Tailscale)...

# 3. Bake Tier 1 (project): clone Tier 0, bake immutable/expensive artifacts
#    (Skia/Dawn static libs) into the golden so each clone gets them CoW-free
tart clone macos-build-base pulp-mac-build
```

**Cache split (applies to every OS):**
- **Immutable + expensive → baked into the golden** (Skia/Dawn static libs).
  CoW-shared, ~free per clone.
- **Mutable + growing → host-mounted virtio-fs** (ccache, FetchContent). Match
  guest/host uid so the shared cache is writable both ways.

**Ephemeral runner concept:** an ephemeral per-job GitHub Actions runner clones
the golden, mounts the host caches, runs **one** job, and self-destructs. The
golden is never mutated; all per-run state lives in the disposable clone.

> macOS-guest **2-running-VM kernel cap** applies (Apple Virtualization limit).
> Linux/Windows guests are **uncapped** — that's where local parallelism lives.

---

## 3. Linux lane (Tart) — the easy, fully-proven win

End-to-end: pull base → bump resources → durable keys + mounts → deps → Skia →
build → test. Native arm64 builds at full speed; x64 is cross+emulate (§3.8).

### 3.1 Pull a **pinned** Ubuntu 24.04 arm64 base (never `:latest`)

`:latest` drifts glibc/sysroot underneath your golden. Pin a concrete tag or
digest and record it in the manifest:

```bash
tart pull ghcr.io/cirruslabs/ubuntu:24.04        # then re-pin to a digest you record
# manifest: base = "ghcr.io/cirruslabs/ubuntu:24.04@sha256:<pin>"
tart clone ghcr.io/cirruslabs/ubuntu:24.04 pulp-linux-build
```

### 3.2 Bump disk / RAM / CPU (on a **stopped** VM)

`tart set` only works while the VM is stopped. Cloud-init auto-grows the root
partition to fill the larger disk on next boot.

```bash
tart set pulp-linux-build --disk-size 80 --memory 16384 --cpu 8
tart run  pulp-linux-build --no-graphics &     # boot once; cloud-init grows the FS
```

### 3.3 Durable SSH keys (work around cloud-init)

cirruslabs cloud-init images **re-apply the default `~/.ssh/authorized_keys` on
every boot**, so anything you add there is wiped. The durable pattern:

1. Write your injected keys to an **unmanaged** file cloud-init doesn't touch:
   `~/.ssh/authorized_keys_ci`.
2. Add an sshd drop-in that tells sshd to read it:

   ```
   # /etc/ssh/sshd_config.d/10-ci-keys.conf
   AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys_ci
   ```

This survives reboot; the standard file is **not** durable here. Never bake
private keys.

Then sync your host `~/.ssh/config` so `ssh pulp-linux` works (a managed block
between `# >>> pulp-ci` / `# <<< pulp-ci` markers, one `Host` stanza per running
VM pointing at the current `tart ip`):

```bash
# tart-sshconfig.sh sync   (vmnet IPs are per-boot; this keeps the alias current)
ssh pulp-linux 'echo ok'
```

### 3.4 Durable host-ccache mount (systemd .mount, NOT fstab)

cloud-init **reverts `/etc/fstab` on every boot**, so an fstab line for the
virtio-fs cache won't stick. Use a **systemd `.mount` unit** instead (or mount at
job runtime). The virtio-fs mechanics:

```bash
# Each `tart run --dir="NAME:host-path"` exposes the share under the automount tag.
tart run pulp-linux-build --dir="ccache:<vm-store>/linux-ccache" --no-graphics &

# Inside the guest, the automount tag is com.apple.virtio-fs.automount;
# each named --dir appears as <mnt>/NAME:
sudo mount -t virtiofs com.apple.virtio-fs.automount <mnt>
ls <mnt>/ccache        # the rw named subdir (the share ROOT is perm-restricted)
```

> **ccache hashing config MUST match** between the cache-populating build and the
> warm build, or the keys differ and you get near-zero hits. Set `CCACHE_BASEDIR`
> / `CCACHE_NOHASHDIR` identically in both. A mismatched prime once gave a
> misleading 10.69%; matched config gave **99.93%**.

### 3.5 Install the full build dependency set

Mirror the project's canonical Linux dependency list **first**, then add the CI
extras. (For Pulp this is `build.yml`'s "Install Linux dependencies" step.)

```bash
ssh pulp-linux sudo apt-get update
ssh pulp-linux sudo apt-get install -y \
  libasound2-dev libdbus-1-dev libdrm-dev libegl1-mesa-dev \
  libfontconfig1-dev libgbm-dev libgl1-mesa-dev libx11-dev libxext-dev \
  libxfixes-dev libxi-dev libxinerama-dev libxkbcommon-dev libxrandr-dev \
  libxrender-dev libxss-dev libxtst-dev libwayland-dev wayland-protocols \
  libicu-dev \
  cmake ninja-build clang lld ccache git git-lfs python3 \
  gcc-x86-64-linux-gnu g++-x86-64-linux-gnu binfmt-support
```

- `libicu-dev` is needed because Pulp opts into direct `icu::Locale` /
  BreakIterator calls when Skia + ICU public headers are present; libskia exports
  SkUnicode, not ICU's own symbols (see gotchas: ICU link).
- `libjack-jackd2-dev` is **deliberately omitted** — base Linux compiles fine
  without it; only add it for a JACK-enabled lane.
- `gcc/g++-x86-64-linux-gnu` + `binfmt-support` are for the x64 smoke lane
  (§3.8). The provision script also installs Rosetta-for-Linux and the amd64
  runtime libraries needed by dynamic x64 binaries.

### 3.6 Fetch prebuilt Skia (linux-arm64) + bake it

Bake the static lib into the golden so each clone gets it CoW-free:

```bash
ssh pulp-linux 'cd pulp && python3 tools/deps/fetch_skia_for_release.py --arch linux-arm64'
```

> **Arch-path collision (matters for §3.8):** the fetch script maps **both**
> `linux-arm64` and `linux-x64` to the **same** `linux-gpu/lib/Release/libskia.a`
> (`arch_subdir=""` for Linux), and Skia is selected by OS, not target arch. You
> **cannot bake both Linux arches into one tree** as-is — use separate `SKIA_DIR`
> roots per target arch, or add a Linux arch-subdir to the fetch script. Native
> arm64 alone (this section) is unaffected.

### 3.7 Configure Release, build, test

```bash
ssh pulp-linux 'cd pulp && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release'
ssh pulp-linux 'cmake --build pulp/build -j8'
ssh pulp-linux 'ctest --test-dir pulp/build --output-on-failure'
```

**Proven results (Tart Linux golden, arm64):** build **1003/1003 green**
(VST3+CLAP+standalone); `ctest` **99% (9366/9370)** — the 4 failures are
env/golden-baseline (no git remote, arm64 raster goldens, fs iteration order),
not regressions. Cold build ≈ **1:47** compile (configure ≈ 273 s); **warm build
20.8 s @ 99.93% ccache hits** across a CoW clone (beats the macOS lane's 88%).

A fast inner-loop variant: `-DPULP_ENABLE_GPU=OFF` for a no-Skia smoke.

### 3.8 Linux x86_64 — cross-compile + Rosetta-emulated test (wired)

The guest is ARM64; you reach x86_64 by cross-compiling in-guest and running the
test subset under Rosetta-for-Linux (binfmt). This is wired into the provider —
`tartci up linux --target-arch x86_64` (or `providers/tart-linux/run.sh
--target-arch x86_64`). The manifest declares it with `target_arch = "x86_64"`,
`cross = true`, and an `[emulation]` table (see `manifests/example.x64.toml`).

What the provider does when `target_arch != arch`:

1. **Toolchain** — install-if-missing `gcc-x86-64-linux-gnu` /
   `g++-x86-64-linux-gnu`, plus Rosetta binfmt registration. CMake is
   configured with `-DCMAKE_SYSTEM_PROCESSOR=x86_64
   -DCMAKE_C_COMPILER=x86_64-linux-gnu-gcc -DCMAKE_CXX_COMPILER=…-g++`.
2. **Rosetta runtime** — host Rosetta is installed with
   `softwareupdate --install-rosetta --agree-to-license`, Tart boots x64-smoke
   clones with `--rosetta=rosetta`, the guest mounts the Rosetta virtiofs share
   at `/mnt/rosetta`, and systemd re-registers binfmt after reboot. The golden
   also carries an amd64 apt source + `libc6:amd64 libstdc++6:amd64
   libgcc-s1:amd64 zlib1g:amd64 libtinfo6:amd64 libxml2:amd64`, so dynamic x64
   binaries have `/lib64/ld-linux-x86-64.so.2`. The binfmt register string must
   be written with literal `\xHH` escapes (`printf '%s'`); `binfmt_misc` decodes
   them itself.
3. **GPU off by default.** The fetch script maps both `linux-arm64` and
   `linux-x64` to the SAME `build/linux-gpu/lib/Release/libskia.a`
   (`arch_subdir=""` — §3.6), so you can't reuse the baked arm64 Skia for an x64
   link. The cross build therefore defaults `-DPULP_ENABLE_GPU=OFF`. To build
   GPU-on, fetch the `linux-x64` Skia into a **separate** tree and pass
   `--gpu --skia-dir <that tree>` (or `[emulation].skia_dir`); without an
   explicit x64 `SKIA_DIR` the provider refuses `--gpu` rather than silently
   linking the arm64 lib.
4. **Full cross-LINK also needs x64 system libs.** ALSA / X11 / wayland / etc.
   must be present for x64 (`dpkg --add-architecture amd64` + the `:amd64` -dev
   packages, or a baked x64 sysroot). If absent, the configure/link fails with a
   clear missing-lib error — it never emits an arm64 artifact under an x64 name.
5. **Tests** run via `ctest` under Rosetta binfmt, excluding
   `sanitizer|simd|gpu|timing` labels.

**Prove just the chain, golden-agnostic:** `tartci up linux --target-arch
x86_64 --self-test` cross-compiles a dynamic trivial program and runs it under
Rosetta — no Pulp checkout or Skia needed. A project with V8's bundled clang can
also verify the real workload toolchain with
`third_party/llvm-build/Release+Asserts/bin/clang --version`; it should print
`Target: x86_64-unknown-linux-gnu`.

**Treat emulated-x64 green as a smoke signal, NOT a gate.** Sanitizers
(ASan/TSan/UBSan/MSan/RTSan) don't translate reliably under emulation — run those on real
x64 (GitHub). SIMD/Highway dispatch, futex/signal semantics, and RT-audio timing
are all unreliable emulated. GitHub-hosted x64 stays authoritative.

> **Windows x86_64 (Prism).** The Windows-on-ARM analog runs x64 binaries under
> Prism, but the cross-build toolchain story there (MSVC x64 cross + x64 deps) is
> heavier and not yet wired — `--target-arch` is Linux/Rosetta today. Tracked
> as a follow-up; until then the Windows lane builds native ARM64.

---

## 4. Windows lane (QEMU) — the hard-won recipe

AVF can't install Windows (no inbox virtio-blk driver → 0 bytes written; black
installer display), so Windows is **first-class on standalone QEMU/hvf**, not a
fallback. The golden ends up a qcow2 you can also open in UTM as a GUI bench.

Approximate timing once set up: **Windows cold build ≈ 7 min** on an 8-core arm
QEMU VM.

### 4.1 Get a Win11 **24H2** ARM64 ISO (NOT 25H2)

25H2 install media fails to boot with BCD `0xc000000d` across every QEMU/AVF
permutation — it's a media/version incompatibility, not config. **Use 24H2.**

If Microsoft's download page blocks your IP (anti-VPN, e.g. via Tailscale), build
the ISO with **UUP dump**'s macOS converter, which pulls directly from the
Windows Update CDN:

```bash
# UUP dump → "Download using aria2 + convert" → run the macOS converter script.
# chntpw won't build on Apple Silicon → stub it (no-op). The autounattend handles
# the registry bypass chntpw would otherwise do, so the stub is harmless.
```

### 4.2 512-byte-pad the ISO (and any disk image)

AVF/QEMU reject disk/ISO images whose byte size isn't a multiple of 512
("Invalid disk image. The disk image format is not recognized.").

```bash
# Pad <iso-store>/win11-24h2-arm64.iso up to the next 512-byte boundary.
# (hdiutil-produced ISOs are already aligned; UUP/MS ones often are not.)
```

### 4.3 Author `autounattend.xml`

Generated by `providers/qemu-windows/make-autounattend.sh` (keys come from a
**configurable** set; never bake private keys). It must include:

- **LabConfig bypass** for TPM / SecureBoot / RAM / CPU checks.
- **Local admin + autologon**, OOBE-skip attempt.
- **OpenSSH server enabled**, with your **public** keys injected into
  `administrators_authorized_keys`.
- **viostor arm64 DriverPaths** (`Microsoft-Windows-PnpCustomizationsWinPE`) so
  Setup can see disks if needed.

```bash
providers/qemu-windows/make-autounattend.sh \
  --pubkey ~/.ssh/id_ed25519.pub \
  --out <vm-store>/win-provision/autounattend.xml
```

### 4.4 The QEMU flags that matter

Reference `providers/qemu-windows/qemu-run.sh`. The load-bearing choices:

- `-accel hvf -machine virt,highmem=on -cpu host`
- **NVMe** system disk (`-device nvme,...`) — Win-ARM has an **inbox NVMe
  driver**, sidestepping the AVF virtio-blk wall.
- **`-device ramfb`** display — **NOT** virtio-gpu (WinPE has no virtio-gpu driver
  → boot-splash hang).
- **ALL install media on `-device usb-storage`** — **NOT** virtio-scsi (WinPE
  can't read virtio-scsi → autounattend.xml is never found).
- `-netdev user,hostfwd=tcp::2222-:22` + `-device virtio-net-pci` (network works
  via netkvm → FOD/downloads + SSH on `localhost:2222`).
- `-vnc 127.0.0.1:N` so you can drive the headless install.

```bash
providers/qemu-windows/qemu-run.sh \
  --disk    <vm-store>/win-provision/win.qcow2 \
  --install <iso-store>/win11-24h2-arm64.iso \
  --virtio  <iso-store>/virtio-win.iso \
  --autounattend <vm-store>/win-provision/autounattend.xml
```

### 4.5 Drive the headless install via vncdotool

```bash
pip install --user vncdotool
```

Flow: **UEFI Boot Manager → boot the install CD → dense-keyspam the "press any
key to boot from CD" prompt → WinPE/Setup picks up the autounattend.** vncdotool
mistypes some shifted chars (e.g. `>`); it's fine for `:` and `\`.

### 4.6 Make the ESP self-booting (auto-boot on reboot)

After the image is applied, copy the boot manager into the fallback path so
reboots boot Windows directly — no UEFI-shell babysitting:

```powershell
mountvol S: /s
copy S:\EFI\Microsoft\Boot\bootmgfw.efi S:\EFI\Boot\BOOTAA64.EFI
```

Verified: reboot → SSH back in ~15 s.

### 4.7 Get SSH up, then provision via **direct SSH commands**

```bash
ssh -p 2222 admin@localhost 'whoami'    # alias this as pulp-win in ~/.ssh/config
```

**Do NOT scp `.cmd` batch files and run them** — they mis-execute. Run commands
directly: `ssh pulp-win '<cmd>'`. The OpenSSH default shell is `cmd.exe`; for
complex PowerShell, dodge cmd quoting/`%`/`>` mangling with base64:

```bash
ssh pulp-win "powershell -EncodedCommand $(printf '%s' "$PS_SCRIPT" \
  | iconv -t UTF-16LE | base64)"
```

Install the toolchain over SSH:

- **CMake / Ninja / Git** (msi / zip / exe).
- **Python (arm64)**.
- **MSVC Build Tools** — arm64 VCTools + Win11 SDK.

> **MSVC gotcha (the big one):** a *partial* VS install makes the installer
> silently no-op — exits 0, installs nothing. Fully nuke `BuildTools` +
> `Packages` + `Setup`, **reboot**, then clean-install. **Verify `cl.exe`
> exists** under `VC\Tools\MSVC\<ver>\bin\Hostarm64\arm64` — do not trust the
> installer's exit code. (Prefer an offline VS layout / host-side cache over a
> live web installer for reproducibility.)

### 4.8 Build + test

```bash
# Git bash: fetch deps only
ssh pulp-win 'C:\path\to\bash setup.sh --ci --deps-only'

# Configure under the MSVC env, GPU off (no Windows Skia yet)
ssh pulp-win 'vcvarsall arm64 && cmake -S pulp -B pulp\build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DPULP_ENABLE_GPU=OFF'

ssh pulp-win 'cmake --build pulp\build'
ssh pulp-win 'ctest --test-dir pulp\build'   # apply the CI exclude set (gpu/visual labels)
```

Two Windows-specific musts:

- **Create `C:\tmp`** — tests use POSIX `/tmp/...` paths, which resolve to
  `C:\tmp` on Windows. Without it those tests fail.
- **If `cl` hangs on a translation unit, kill it and resume** — Ninja is
  incremental, and the arm→x64 emulation can transiently stall a TU.

Tag the golden when green: `pulp-windows-build:<date>`. Use **sccache** (not
ccache) for Windows cache warmth.

---

## 5. Per-project manifest, bench (UTM), and metrics

### Per-project manifest (`vm-image` v2)

A repo plugs in by dropping one `.shipyard/vm-image.<os>.toml` (or an
`[[images]]` array) declaring `os` / `arch` / `target_arch` / `cross` / `base` /
OS-scoped `[packages]` / `[caches]` / `[[mounts]]`. tartci bakes or clones a
golden from it with zero hand-provisioning — keys, ssh-config, Tailscale, log
collection, and cache mounts are inherited framework defaults. See `manifests/`
in this repo and the `README` "Per-project use" section.

### Bench (UTM) for GUI / DAW testing

The **golden** is pristine, generic, headless, and never mutated. A **bench** is
a separate **persistent, snapshot-able** clone you open in UTM to install DAWs +
test plugins by hand:

1. Start from the golden of the target OS (never customize the golden).
2. Clone → bench (one persistent copy); snapshot before big changes.
3. Open the bench in UTM with a GUI display profile (`ramfb` → `virtio-gpu` for
   Windows) and install your DAWs / plugins.

Keep the NVMe controller constant between golden and bench; only the display
profile + installed apps differ. The bench may carry a live Tailscale identity +
activation; the golden must not. See the README "bench" section and the design
notes for the per-OS UTM story (Windows imports its qcow2 directly; Linux needs a
qcow2 export; macOS is recreate-from-IPSW).

### Metrics

Each VM job wraps configure → build → ctest with a timer and emits **one
structured JSONL record per run** (os, arch, git_sha, provider, phase wall-times,
ccache/sccache hit %, test pass/fail, ctest label times, cold/warm). Append to a
per-OS host store; a small reporter computes rolling median + flags >N%
deviation. `.ninja_log` + `ninjatracing` gives a per-target flamegraph on demand.
Graduate to a dashboard (VictoriaMetrics + Grafana, or Grafana + SQLite) only if
at-a-glance trends are wanted. See `metrics/` and the README.

---

## 6. Serve the GitHub Actions pool (per-job ephemeral runners)

§3–§4 cover **on-demand** builds (`tartci up <os>` — one build, then discard).
To make a host **serve the GitHub Actions pool** instead — boot a throwaway VM
per *queued job* and let the workflow drive the build — use the runner
supervisors. They are the pool-serving siblings of the `run.sh` provider
scripts, ported from Pulp's proven `tools/ci/{tart-runner-linux,qemu-runner-windows}.sh`.

```bash
# One job then exit (pilot-safe): mint a JIT runner, boot a clone, run one job, discard.
tartci serve linux
tartci serve windows

# Keep serving (what the LaunchAgents run):
tartci serve linux --loop --labels self-hosted,Linux,ARM64,pulp-build-linux
```

What the supervisor does each job: mint a **Just-In-Time** (single-job) runner
config via `gh api .../generate-jitconfig` (needs repo admin), clone the golden
(Linux) or CoW-overlay it on a free SSH port (Windows), run the Actions agent
once with that JIT config, then discard the VM. The agent processes exactly one
job and deregisters — no long-lived runner state. The `--loop` gate only boots
when there is queued work (counts queued runs of `TARTCI_RUNNER_WORKFLOW_NAME`,
default `Build and Test`), so idle hosts don't spin VMs.

Everything is env-driven for genericity: `TARTCI_RUNNER_REPO`,
`TARTCI_LINUX_GOLDEN` / `TARTCI_WIN_GOLDEN`, `TARTCI_RUNNER_LABELS`,
`TARTCI_RUNNER_GROUP_ID`, `TARTCI_RUNNER_VERSION` (Windows agent). Defaults
target `danielraffel/pulp` (the first consumer).

**Windows gotchas preserved from the Pulp original** (debugged live; don't
"simplify" them away): the multi-KB JIT blob is **streamed via ssh stdin into a
file**, never on a command line (cmd.exe's 8191-char limit blows through the
ssh→cmd→powershell chain); the agent runs as `Runner.Listener.exe` reading that
file; `vcvarsall` is discovered via `Get-ChildItem` in base64-encoded PowerShell
(vswhere returns empty for a BuildTools-only install); the supervisor **bails the
moment QEMU dies** (`kill -0 $qpid`) so a free-port TOCTOU surfaces fast instead
of burning the full ~10 min SSH window; and a post-extract integrity check
asserts `Runner.Listener.exe` exists before running.

### Serve across reboots (LaunchAgent)

Install one of the templates in `launchd/` so the supervisor runs under
`launchd` and survives reboot. The two shipped templates are **Pulp's concrete
instance** — their labels (`com.danielraffel.pulp.tart-runner-linux`,
`com.danielraffel.pulp.qemu-runner-windows`) are what the
[shipyard-macos-gui](https://github.com/danielraffel/shipyard-macos-gui) "Serve
CI builds from this Mac" switch toggles via `launchctl load/unload`. See
`launchd/README.md` for the install `sed` recipe and how to serve a different
repo. Two traps carried over from the Pulp lane: launchd does **not** expand
`$HOME`/`$TARTCI_REPO` (the install `sed` must write absolute paths), and a
LaunchAgent can't read a `/Volumes` golden store without **Full Disk Access**.

### Emulation note

Pool jobs build whatever arch the **workflow** targets. The emulated **x86_64**
lane belongs on the **on-demand** side (`tartci up <os>`, see the cross-arch
manifest work), not as a pool-serving lane — a serving runner that "picks up
jobs" would misrepresent an emulated local build. GitHub-hosted x64 stays the
authoritative gate.

---

## Where the lanes stand

- **Linux:** done — green build + 99% ctest + 99.93% warm ccache, golden tagged.
- **Windows:** 24H2-ARM golden boots headless + auto-boots; toolchain installs;
  non-GPU build/test is the MVP target. GPU/Skia lane is a tracked follow-up
  (needs Windows skia-builder slices + the Windows GPU-host product work).
- **macOS:** the proven lane this toolkit generalizes from.
- **Pool serving:** `tartci serve linux|windows` wired (ported from Pulp's
  proven `tools/ci` supervisors); LaunchAgent templates in `launchd/`.
