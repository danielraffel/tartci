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

## Fleet setup — assemble a pool, or add one Mac to it

Two ordered paths. Both lean on **`tartci setup`** (installs prereqs, creates
stores, auto-derives + persists this host's role, and runs the governor verify
gate — see "Onboarding a new host") and **`tartci goldens sync`** (copy goldens
between hosts instead of re-baking). GitHub is the scheduler; each Mac is a
label-matched runner, so there is no central "fleet controller" to stand up —
you onboard hosts one at a time and GitHub load-balances across them.

**Roles are auto-derived** (one per host, from cores + `hw.model`):
`dedicated-builder` (biggest/always-on box; also hosts the required macOS gate),
`dev-overflow` (a capable laptop that also does interactive dev), or `light`
(small/travel laptop). `tartci host-profile` shows the derived role + core/memory
budgets; pin only if you disagree (see "Onboarding a new host").

### A. From scratch (new pool)

1. **Pick your always-on host** — it becomes the gate anchor and will derive
   `dedicated-builder`.
2. On it: clone tartci → `tartci setup` → bake goldens per lane
   (§2 macOS, §3 Linux, §4 Windows).
3. Register its GitHub Actions runners with your lane labels; `tartci pool on`.
4. Verify governed: `tartci host-profile` (role + budgets) and
   `tartci leases status` (store answering).
5. Add every other Mac via path **B**.

### B. Add one Mac to an existing pool  ← the common case

1. **Install + onboard.** On the new Mac: clone tartci, generate its clean
   support manifest, and use the receipt-bound fleet installer to publish an
   immutable generation plus `~/.local/bin/tartci`; then run
   **`tartci setup`** — it installs prereqs, creates stores, **auto-derives +
   persists the role**, and runs the governor verify gate. A half-provisioned
   host is surfaced rather than reported clean.
2. **Reachability.** Ensure SSH and (recommended) **Tailscale** so this host and
   the pool can reach each other by stable name — needed for `goldens sync`.
3. **Get goldens without re-baking.** `tartci goldens sync --from <existing-host>`
   pulls the canonical golden(s) over the fastest link (Thunderbolt → LAN →
   Tailscale), verifies, and repoints this host's runner. (Baking per §2–§4 also
   works but is slow.)
4. **Register runners** for the lanes this host will serve, using the pool's
   label scheme (`<repo>-build` + a host-pin `<repo>-build-<tag>`); install them
   as launchd agents (or `tartci serve <os>`). GitHub then routes matching jobs
   here whenever this host is idle.
5. **Join the pool.** `tartci pool on` (or the GUI "All lanes" toggle).
   `tartci pool status` confirms the runners are loaded and participating.
6. **Verify governed.** `tartci host-profile` shows the role + core/memory
   budgets; `tartci leases status` shows the store answering. The governor now
   bounds this host's builds + VMs automatically (core + memory admission).
The new Mac is now governed, serving its lanes, and drainable exactly like the
rest of the pool. Use `tartci pool drain` before roaming or disconnecting;
`pool off` unloads its agents immediately and remains an emergency/idle-only
operation: it refuses (exit 12) while an owned lane is mid-job or its state is
unreadable, and `--now` is the explicit kill; `pool off --plan` shows what it
would stop first. Both refuse when this host is the only one serving a required
gate label; `--allow-last-serving-host` takes that label to zero deliberately.

---

## 1. Prereqs + host setup

**Tools (scripted by `./tartci setup`; manual fallback shown):**

```bash
brew install openai/tools/tart qemu sshpass
# qemu     — Windows VM substrate (hvf accel)
# tart     — macOS + Linux VM substrate (Apple Virtualization)
# sshpass  — non-interactive first-boot SSH during provisioning
```

Tart moved from `cirruslabs/tart` to `openai/tart`; use the official
`openai/tools/tart` formula on macOS 15 or later. Its Softnet dependency
currently requires macOS 15, as does the legacy tap's current Softnet formula.
On Ventura/Sonoma, `tartci setup` preserves a working pre-existing Tart binary
but refuses a fresh formula install; upgrade that host to macOS 15 or later
before fresh onboarding or channel migration. Do not replace Tart while a VM or
provider job is running. To migrate an existing host from the old tap, first prevent new
placement on that host in the scheduler and prove every runner belonging to it
reports `busy=false`. Then prove the local provider has no guest or `tart run`
process. The exact scheduler query depends on the repository and host labels;
do not substitute an empty local process list for the authoritative runner-busy
check. Only after both checks are terminal-idle may you invoke `pool off`, which
unloads LaunchAgents immediately rather than draining them:

```bash
# Both registration scopes. A repository listing omits organization-registered
# runners silently, so a repository-only census reports a smaller fleet than
# exists and its zero reads as "nothing here".
scripts/runner_census.py --repo OWNER/REPO --label pulp-build-pr-head
# Every runner for this host must report busy=false, and routing/admission for
# the host must remain disabled for the duration of the migration. A census that
# prints UNREACHABLE for a scope has not proven anything about that scope.

pgrep -fl 'tart run'                      # must print no active VM process
/opt/homebrew/bin/tart list --format json # every entry must report Running=false
tartci pool off --plan                    # what off would stop; mid-job lanes listed
tartci pool off                           # immediate unload; NOT a drain. Exit 12 =
                                          # a lane is mid-job/unreadable: re-check, do
                                          # not reach for --now during a migration

# Cache both sides before removing either installed keg. The legacy bottles are
# the offline rollback path if installation of the new channel fails.
brew fetch --force cirruslabs/cli/softnet cirruslabs/cli/tart

brew tap openai/tools
brew trust --formula openai/tools/softnet
brew trust --formula openai/tools/tart
brew fetch --force openai/tools/softnet openai/tools/tart
brew uninstall cirruslabs/cli/tart cirruslabs/cli/softnet
if ! brew install openai/tools/softnet openai/tools/tart; then
  # Remove either partially installed new keg before restoring the cached old
  # channel. `reinstall` is invalid because the legacy kegs were uninstalled.
  brew list openai/tools/tart >/dev/null 2>&1 && \
    brew uninstall openai/tools/tart || true
  brew list openai/tools/softnet >/dev/null 2>&1 && \
    brew uninstall openai/tools/softnet || true
  HOMEBREW_NO_AUTO_UPDATE=1 brew install \
    cirruslabs/cli/softnet cirruslabs/cli/tart
  exit 1
fi
/opt/homebrew/bin/tart --version

tartci doctor
# Dispatch one non-required canary job that requires the unique HOST_CANARY
# label, then drive it directly through the governed ephemeral provider while
# normal host routing remains disabled. This path owns the VM lease as well as
# clone, boot/network, shared mount, job execution, release, and discard.
TARTCI_GH_CLI=ghapp tartci serve macos --once --repo OWNER/REPO \
  --labels self-hosted,macOS,ARM64,HOST_CANARY
tartci leases status
/opt/homebrew/bin/tart list --format json # canary clone must be gone
tartci pool on
tartci pool status
```

Canary one overflow host first, then migrate remaining hosts one at a time at
natural idle boundaries. Require that one-shot workflow to finish terminal-green
and prove inventory, CoW clone, VM boot/network, shared-directory mount, discard,
governor lease accounting, and restored runner participation before advancing.
The guarded install above removes partial new
kegs and restores the prefetched legacy formulae on failure; never leave a
half-migrated host marked healthy. Remove the obsolete Cirrus tap only after
every provider host reports the intended version and completes a real ephemeral
job.

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

**Secondary Apple Silicon hosts (M-series pool members).** Keep host-specific
aliases in your local SSH and Shipyard config, not in this repo. The reusable
shape is:

```bash
# Prove non-interactive SSH and the host's Tart install/store.
ssh <m-series-ssh-alias> 'hostname; sw_vers -productVersion; sysctl -n machdep.cpu.brand_string'
ssh <m-series-ssh-alias> 'TART_HOME=/Users/<you>/VMs /opt/homebrew/bin/tart list --format json'
```

Use an explicit Homebrew Tart path because non-interactive SSH may not load
Homebrew's PATH. Prefer a home-backed Tart store for launchd-operated macOS CI:
`TART_HOME=/Users/<you>/VMs` on each host, with the macOS golden copied or baked
there. In Shipyard, keep the matching operator-local capacity config outside the
committed repo config:

```toml
[host_class.secondary]
ssh = "<m-series-ssh-alias>"
cap = 2
tart_bin = "/opt/homebrew/bin/tart"
tartci_bin = "/Users/<you>/.local/bin/tartci"
tart_home = "/Users/<you>/VMs" # absolute path; no shell/tilde expansion
labels = ["self-hosted", "macos", "arm64", "<repo>-build-secondary"]
```

For TartCI's host-local watchdog and network-profile inventory probe, set
`TARTCI_TART_CLI=/opt/homebrew/bin/tart` when Tart is installed somewhere other
than the canonical Apple Silicon or Intel Homebrew locations. The built-in
resolver covers those canonical paths even under a minimal noninteractive SSH
PATH. The probe preserves an explicit `TART_HOME`; otherwise it resolves
`[host].tart_home` from the installed fleet profile. It fails closed instead of
inspecting Tart's default store when neither authority exists. An unavailable
executable/store or malformed inventory remains a typed `unavailable` result:
it blocks controller mutation without being mislabeled as a running VM.
Profile resolution requires Python 3.11+'s complete TOML parser; an older
system Python must receive explicit `TART_HOME` rather than partially parsing a
possibly torn profile.

The key invariant: the LaunchAgent, `tartci doctor`, and Shipyard capacity must
all point at the same Tart store. If one uses default `tart` state and another
uses `TART_HOME`, capacity and cleanup will disagree.
Shipyard's fleet health probe also shells `tartci doctor --reap --json` on each
host, so set `tartci_bin` to the same home-backed wrapper the LaunchAgent uses.
Do not diagnose installation state from raw `ssh host 'command -v tart'` output:
that command can fail solely because a stripped non-login shell omitted
`/opt/homebrew/bin`. Probe the configured absolute binary and report
`installed but unreachable from launch environment` separately from `absent`.

**Wire Shipyard's GitHub auth to the App token (do NOT skip).** After installing
Shipyard on a host, its GitHub auth must point at the GitHub-App **installation**
token — the `[github.auth]` `source = "command"` block (the
`shipyard-github-app-token` helper + App ID + private-key path) in
`~/Library/Application Support/shipyard/config.toml`. If that config is absent,
Shipyard silently falls back to the ambient `gh` token. For a personal GitHub
App with no user login, that ambient token is the **anonymous 60/hr** bucket, so
Shipyard runs unauthenticated and its menu bar shows **"updates paused"** with no
error surfaced — the exact failure seen on host `m1` (2026-07-06), which had no
`config.toml` at all and stayed paused for hours before anyone noticed.

Copy the config from an already-correct host with the supported
`shipyard auth` commands (the exported bundle is sanitized — it carries the
`token_command` + App ID + key **path**, never a secret):

```bash
# On a known-good host — emit a sanitized auth bundle (no secrets):
shipyard auth export > shipyard-auth.bundle

# On the new host — apply it globally, then confirm:
shipyard auth import shipyard-auth.bundle --scope global
shipyard auth doctor
#   github-auth: ok command helper (github-app-installation)   ← want this
#   github-auth: ... gh-cli (ambient)                          ← DEGRADED (60/hr)
```

The private key referenced by `token_command` is **not** in the bundle — it must
already exist at the referenced path on the new host (copy it out-of-band via
your own secret-transfer path; never commit it). `tartci doctor` runs
`shipyard auth doctor` for you and WARNs when the effective source is
`gh-cli (ambient)` rather than `github-app-installation`, so a degraded host is
caught the next time anyone runs `tartci doctor` on it. Hosts without Shipyard
installed stay green (the check is skipped, non-fatal).

### GitHub App runner-group access

The Mac Pro's external policy verifier uses Shipyard's App identity and needs an
organization permission that TartCI's ordinary repository polling does not:
**Self-hosted runners: Read-only**. Repository
`Actions` permission can read runs and jobs, but cannot read
`/orgs/<org>/actions/runner-groups/...`. The host-side verifier uses those organization
endpoints to fail closed unless the selected repositories, selected workflows,
and live runner membership still match the intended trust boundary.

### JIT registration preflight (all macOS fleet hosts)

Keep `TARTCI_GH_CLI=ghapp` for every M1, M3, and M5 lane. For runner group `1`,
TartCI uses the repository runner endpoint. Any non-default group uses the
organization JIT endpoint:

```bash
ghapp api -X POST orgs/<org>/actions/runners/generate-jitconfig \
  -f name=<unique-disposable-name> -F runner_group_id=<group-id> \
  -f 'labels[]=self-hosted' -f 'labels[]=macOS' -f 'labels[]=ARM64'
# Read .runner.id from the response, then remove the disposable registration.
ghapp api -X DELETE orgs/<org>/actions/runners/<runner-id>
```

This is an authorization probe only; it must not start a VM, dispatch a
workflow, or leave a runner record. A repository-endpoint 404 is not evidence
that the App cannot mint the organization endpoint. If the exact probe fails,
leave the lane's JIT denial fuse intact and repair the App installation or its
approved organization permission. Never fall back to ambient `gh`, a registry
credential, an image-pull token, or a project experiment token.

The M1 macOS-27 stackbench credential is GHCR-only. GHCR harnesses pin
`/usr/bin/curl` because an M1 PATH-shadowing wrapper once appended a second
Authorization header and caused a false 401. This curl rule is unrelated to
the Actions JIT path.

That read access enables more than a dashboard. It lets the deployment
prove that disposable Tart macOS runners, the separate Proxmox Linux pool, and
native Intel macOS capacity are attached to the right capability before work is
admitted. The result is useful local capacity without making a public-repository
self-hosted runner a general-purpose execution target. Grant **Read & write**
only to an unattended controller that must configure runner groups or remove
registrations; observation and verification need read-only access.

This verifier is a host-specific integration, not currently a built-in
`shipyard runner` check. Runner-group policy also is not a sandbox: any permitted
workflow can execute the code it checks out. Untrusted PR work still requires a
disposable guest with no host credentials or writable host mounts, along with
the repository's fork and approval controls.

Changing the GitHub App definition is only half the operation:

1. Save the new organization permission on the App.
2. Approve the pending permission update on the organization installation.
3. Expire or replace any locally cached installation token, then mint a new one.
4. Verify the exact group with the App-backed CLI:

   ```bash
   ghapp api orgs/<org>/actions/runner-groups/<group-id>
   ghapp api orgs/<org>/actions/runner-groups/<group-id>/repositories
   ghapp api orgs/<org>/actions/runner-groups/<group-id>/runners
   ```

If the App settings show the permission but these calls return
`403 Resource not accessible by integration`, first compare the installation's
approved permissions with the App definition, then refresh the token. Do not
work around the failure with a broader personal token.

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

### Release Rust must be baked, not downloaded per job

Pulp's release lane builds the Rust CLI on both Darwin architectures. The
`pulp-build-runner` golden must therefore carry stable Cargo plus the Intel
standard library. Homebrew's `rustup` formula is keg-only and does not include
`rustup-init`; installing the formula alone is not enough. Before tagging a
golden, run this as the guest's `admin` user:

```bash
eval "$(/opt/homebrew/bin/brew shellenv)"
brew install rustup
rustup_bin="$(brew --prefix rustup)/bin"
mkdir -p "$HOME/.cargo/bin"
for tool in rustup cargo rustc rustdoc rustfmt cargo-clippy clippy-driver; do
  test ! -x "$rustup_bin/$tool" || ln -sfn "$rustup_bin/$tool" "$HOME/.cargo/bin/$tool"
done
export PATH="$HOME/.cargo/bin:$PATH"
rustup default stable
rustup component add rustfmt clippy
rustup target add x86_64-apple-darwin
```

Verify a fresh clone, not the mutable bake VM: `~/.cargo/bin/cargo --version`,
`~/.cargo/bin/rustup target list --installed`, and a TLS probe to GitHub must
all succeed. This keeps releases working during a transient rustup outage and
proves the state that disposable runners actually inherit. Pulp's release
workflow deliberately probes `~/.cargo/bin` by absolute path and appends that
directory to `GITHUB_PATH`, so shell-profile PATH persistence is not required;
link both `cargo` and `rustup` there so the following Intel-target step inherits
both commands.

**Cache split (applies to every OS):**
- **Immutable + expensive → baked into the golden** (Skia/Dawn static libs).
  CoW-shared, ~free per clone.
- **Mutable + growing → host-mounted virtio-fs** (ccache, FetchContent). Match
  guest/host uid so the shared cache is writable both ways.

### What the JIT runner declares to each job

The macOS JIT runner writes three optional keys into the guest runner's `.env`,
after stripping any preserved copies so a golden cannot forge them:

- `TARTCI_GUEST_CORES` / `TARTCI_GUEST_MEM_MB` — the VM lease the clone was
  sized to (`tartci_set_tart_vm_size`). An in-guest build governor with no host
  profile can read these instead of inferring its budget; Pulp's
  `tools/ci/governed-build.sh` and its ctest step treat them as a ceiling that
  can only narrow what the guest sees. They change no number on their own: the
  guest's memory is already sized so its tier-0 bound is `-j(C-1)`. A faster
  gate on a small host comes from a larger lease (`vm_pool_cores`, the
  `TARTCI_VM_LEASE_MAX_MEM_MB` ceiling), not from these keys.
- `TARTCI_PIP_WHEELHOUSE` — set only when a host wheelhouse was mounted (below).
- `TARTCI_ARTIFACT_CACHE` — set only when a host artifact cache was mounted (below).

### Optional pip wheelhouse (no rebake)

A job that pip-installs wheels otherwise reaches PyPI through the guest's
egress relay, so an allowlist gap or an index outage fails a required gate. A
host directory of pre-downloaded, hash-verified wheels removes that network
dependency without touching the golden:

```bash
scripts/pip-wheelhouse.sh sync \
  --lock /path/to/pulp/tools/motion/visual/requirements.lock \
  --python-version 3.14 --platform macosx_14_0_arm64
```

This fills `${TARTCI_CI_CACHE:-~/.cache/pulp-ci}/pip-wheelhouse` (override
with `TARTCI_PIP_WHEELHOUSE_DIR` or `--dir`). The next VM boot mounts it
read-only as `pip-wheelhouse` and declares `TARTCI_PIP_WHEELHOUSE`; no service
restart is needed, and an empty or absent directory leaves boots unchanged.
`--python-version` is the guest interpreter the job installs into (Pulp's gate
installs into the Homebrew Python CMake resolves, 3.14 on the current golden),
not the host's. The sync refuses an unhashed lock, never rewrites a wheel in
place (a guest may be reading it), and is additive: re-run it after the lock
changes. The consuming job still installs with `--require-hashes`, so the
wheelhouse decides where the bytes come from, never which bytes are accepted.

### Signing keychain: never a password dialog

A process in the GUI login session that touches a locked non-login keychain
makes securityd show a password dialog for that keychain, and only
`~/.config/pulp/secrets/keychain.env` holds that password. macOS locks those
keychains again at every login and logout. Three things keep the dialog away:

- Every tartci signer names its keychain (`codesign --keychain <dedicated>`)
  and unlocks it in its own session first. The dedicated keychain is
  keychain.env's `PULP_SIGN_KEYCHAIN`, or its `-unattended` sibling when
  `pulp ship doctor` has built one.
- `com.danielraffel.tartci.keychain-unlock` (installed by `tartci setup`, and
  reinstalled by the launchd watchdog wherever keychain.env exists) runs
  `tartci keychain-unlock` at login and every 15 minutes. It unlocks only the
  dedicated keychain, with the password on `security -i`'s standard input, and
  clears any auto-lock. State: `~/.tartci/state/keychain-unlock/last.json`.
- `tartci pool status` prints `signing prompts:` and `tartci doctor fleet`
  reports `signing_prompts_*`. It flags a second signing keychain on the user
  search list, a password that no longer unlocks the dedicated keychain, a
  keychain that re-locks on its own, and an unlock agent that is missing,
  failing or stopped.

Never run `security show-keychain-info` (or anything that reads a keychain)
against a keychain that may be locked: on a GUI host that read is itself what
raises the dialog. Unlock with `unlock-keychain -p` first, which never prompts.

### Optional artifact cache: git mirrors and pinned archives (no rebake)

Every job on a disposable guest otherwise re-downloads the same bytes. Measured
on Pulp's required `macos` gate (2026-09-30): a depth-2 checkout (56 MiB) plus
the GPU-provenance `--unshallow` (57 MiB) on every job, the pinned Chrome for
Testing archive (187 MB) and the prebuilt Skia archive (57 MB), and the iOS
simulator Skia slice (69 MB) whenever the iOS gate runs. A host directory
shared read-only lets the guest take those bytes from local disk:

```bash
scripts/artifact-cache.sh git-sync --repo Generous-Corp/pulp
scripts/artifact-cache.sh add --url <archive url> --sha256 <pinned digest>
scripts/artifact-cache.sh status
scripts/artifact-cache.sh prune --older-than-days 30
scripts/artifact-cache.sh refresh [--compact-above 16]
```

`tartci artifact-cache <args>` runs the same script through the installed
wrapper.

This fills `${TARTCI_CI_CACHE:-~/.cache/pulp-ci}/artifact-cache` (override with
`TARTCI_ARTIFACT_CACHE_DIR` or `--dir`). The next VM boot mounts it read-only
as `artifact-cache` and declares `TARTCI_ARTIFACT_CACHE`; no service restart is
needed, and an empty or absent directory leaves boots unchanged.

- `git/<owner>/<repo>.git` is a bare mirror of one branch. A job adds its
  `objects` directory as a Git alternate before fetching, so the server sends
  only what the mirror lacks. A stale mirror still saves every byte it holds;
  re-run `git-sync` to keep the saving near total. The mirror never collects
  garbage on its own and `git-sync` only adds packs; fold them with
  `compact --repo ...`, which refuses while any Tart VM is running because a
  guest reads the packs through the share for its whole job. Never delete a
  mirror while VMs are running.
- `refresh` keeps every mirror that already exists current: it re-fetches each
  branch the mirror holds and never creates a mirror, so a host that never ran
  `git-sync` is unchanged and an absent cache is a no-op. With
  `--compact-above N` it also folds a mirror holding more than N packs, but only
  when Tart reports zero running VMs at that moment; a running or unknown VM
  count defers compaction to a later pass. A failed mirror does not stop the
  others and the pass exits 1. The `com.danielraffel.tartci.artifact-cache-refresh`
  LaunchAgent (`launchd/com.danielraffel.tartci.artifact-cache-refresh.plist.template`)
  runs `refresh --compact-above 16` every 6 hours (`StartInterval`, so not
  aligned to the clock; `RunAtLoad` off) at `ProcessType=Background` with
  `LowPriorityIO`, against the same `TARTCI_CI_CACHE` the runner plists use.
  `tartci setup` installs it with `scripts/install_artifact_cache_refresh_agent.sh
  --install` (idempotent; `--plan` shows what it would do). Its log is
  `~/Library/Logs/tartci/tartci-artifact-cache-refresh.log`.
- `sha256/<hex>` holds a file whose SHA-256 is `<hex>`. `add` takes the digest
  the consuming job already pins and refuses bytes that do not match it, and a
  consuming job must re-verify the digest and fall back to its own download
  when the entry is absent: the cache decides where the bytes come from, never
  which bytes are accepted. Re-adding a present blob re-verifies it and
  refreshes its age; `prune` removes blobs nobody re-added within the window.

Nothing is rewritten in place (a guest may be reading it): blobs and new mirrors
are staged beside their destination and renamed into place, and one writer runs
at a time per cache. Size is bounded by what you add: one mirror per repository
plus the pinned archives you list.

### Optional per-job ccache write isolation

By default every guest mounts the host ccache read-write, so a job torn down
mid-build (a Tart hang, a timeout, a cancel) can leave half-written direct-mode
manifests that every later job trusts. On m3 in September 2026 that made every
gate build link the wrong objects for 17 hours. Setting
`ccache_write_isolation = true` on a `[[lane]]` (it renders
`TARTCI_CCACHE_WRITE_ISOLATION=1`) makes each job:

- read the shared store through ccache remote storage marked `read-only`;
- write new entries only to its own layer (`remote_only`, so a hit is never
  copied back into the layer);
- have that layer promoted into the shared store by the host only when the
  job's verdict is green (clean listener exit, the runner's own
  `completed with result: Succeeded`, no lifecycle quarantine, and a GitHub
  `success` conclusion whenever the API observed one) **and** `tart list`
  proves the VM is gone. Everything else is deleted without touching the
  shared store.

The layer is attached when the job starts in the guest, after the JIT mint, so
a VM booted before any job exists is covered the same way; the boot-time mount
is unchanged. Everything lives under `$CACHE_ROOT/ccache/tartci-layers-v1/`
(`shared/`, `jobs/<vm>/`, `green/`, `discard/`) in ccache's `file:` remote
layout, which differs from the legacy primary cache beside it; the two never
read or clean each other. The shared store therefore starts cold on the first
isolated lane of a host. Host-only state, including the promotion audit log
(`audit.jsonl`, one JSON line per attach / settle / promote / discard / trim,
with the job's hit and miss counters when the host has ccache) and the list of
keys each promotion wrote (`promotions/*.keys`), is in
`$CACHE_ROOT/ccache-layer-state/`. Promotion hard-links result entries so an
existing key is never overwritten, replaces a manifest only with a strictly
newer one, rejects anything that is not a ccache entry in a well-formed key
path, and runs after the job, off its critical path. A supervisor that dies
mid-job leaves its layer to the next attach's sweep, which discards it.

Enable on one lane, re-render, reload that lane at an idle boundary; roll back
by deleting the key. Verify with:

```bash
state=~/.cache/pulp-ci/ccache-layer-state
grep -c '"event": "promote"' "$state/audit.jsonl"                   # grows on green jobs
grep '"event": "settle"' "$state/audit.jsonl" | grep '"verdict": "red"'  # and no promote follows these
```

The guard is ccache's `read-only` flag, not the mount: the mount is fixed at
boot and shared with non-isolated lanes, so a hostile guest could still write
the directory. A read-only mount of the shared store is the next step once VM
boot is restructured.

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

### 3.9 Native Linux x64 Proxmox golden refresh

The Mac Pro's native x64 pool is not a Tart provider, but its golden refresh is
versioned here so it inherits the same Pulp render identity contract. Use
`providers/proxmox-linux/bake-pulp-golden.sh`; the complete host topology,
drain/canary procedure, and rollback boundary live in
[`proxmox-macpro.md`](proxmox-macpro.md#refreshing-the-render-toolchain-golden-m153).

The short contract is: preserve template `9005`; choose a new unused VMID; clone
additively; bind the supplied SSH peer to that exact VMID with a guest-agent
nonce; detach at `manifests/pulp.linux.toml`'s exact Pulp SHA; derive m153
Skia/Dawn/V8 identity from that checkout's exact manifest; deep-validate provider
receipts; compile/link both m153 Skia capabilities, execute the non-global
`GetInstance`/Graphite paths, and record that process-global `SetInstance` is
link-proven but intentionally not executed; warm the local Release
build; publish and independently validate the host receipt; scrub clone identity;
stop; then and only then template the new VMID. A failed candidate is retained,
not automatically destroyed. This repository step creates tooling only—running
it is a separately governed host operation.

```bash
providers/proxmox-linux/bake-pulp-golden.sh \
  --new-vmid <unused-vmid-at-or-above-9006> \
  --guest-host <candidate-ip>
```

The bake, `tart-linux/provision.sh` and `tart-macos/provision.sh pulp-readiness`
read their TOML manifests through `tartci_toml_python`
(`providers/common/toml-python.lib.sh`, the resolver the `tartci` shim sources),
not a bare `python3`: an operator's ssh login shell on m1 resolves `python3` to
`/usr/bin/python3` 3.9.6, which has no tomllib. With no 3.11+ interpreter found,
they stop and say `set TARTCI_PYTHON`.

> **Windows x86_64 (Prism).** The Windows-on-ARM analog runs x64 binaries under
> Prism, but the cross-build toolchain story there (MSVC x64 cross + x64 deps) is
> heavier and not yet wired — `--target-arch` is Linux/Rosetta today. Tracked
> as a follow-up; until then the Windows lane builds native ARM64.

### 3.10 macOS render-golden readiness is fail-closed

`manifests/pulp.macos.toml` records the exact m153 source/provider generation,
but the current generic macOS list/resize/tag helper cannot prove an existing
golden contains it. The manifest therefore says `golden_readiness.status =
"unready"`. This is deliberate: inventory operations and a rolling `:latest`
alias are not provider evidence.

Run `providers/tart-macos/provision.sh pulp-readiness` for the exact preparation
report. It prints the required Pulp, Skia, and V8 identities and exits nonzero.
A future implementation may turn this green only after it binds a deep render
receipt to the exact golden being promoted; changing the manifest status alone
fails closed.

---

## 4. Windows lane (QEMU) — the hard-won recipe

AVF can't install Windows (no inbox virtio-blk driver → 0 bytes written; black
installer display), so Windows is **first-class on standalone QEMU/hvf**, not a
fallback. The golden ends up a qcow2 you can also open in UTM as a GUI bench.

> **Two ways to get a host its Windows golden — copy first, bake only if you must.**
> The golden is a portable ~26 GB qcow2, so a new host almost never needs to bake
> its own:
> - **(A) Copy from the pool (preferred, fast).** If any host already has the
>   canonical golden, run **on the new host** (once tartci is deployed there):
>   `tartci goldens sync --from <peer-that-has-it>` — it picks the fastest link
>   (Thunderbolt → LAN → Tailscale), verifies the sha, and points the local runner
>   at it. (Or push from a host that has it: `tartci goldens sync --to <newhost>`.)
>   See `docs/golden-sync.md`.
> - **(B) Bake from scratch (below).** Only for the **first** golden in the pool
>   or a **new Windows version** — the ISO → autounattend → provision recipe in
>   §4.1–§4.8. Once baked, other hosts get it via (A).

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
- **Bake hosted-runner-compatible command paths** — Pulp's current GitHub
  workflow assumes `bash`, `choco`, and `ccache` can be found on `PATH`. Install
  Chocolatey, install `ccache`, and add `C:\Program Files\Git\bin`,
  `C:\Program Files\Git\usr\bin`, and `C:\ProgramData\chocolatey\bin` to the
  machine PATH before tagging the golden.
- **If `cl` hangs on a translation unit, kill it and resume** — Ninja is
  incremental, and the arm→x64 emulation can transiently stall a TU.

Tag the golden when green: `pulp-windows-build:<date>`. Prefer **sccache** for
new Windows-native cache work; if the consuming workflow still calls `ccache`,
bake `ccache` too so the first job does not have to install it.

Before tagging, run the golden optimizer against the booted single-operator VM:

```bash
tartci windows optimize
# Optional x64/Prism smoke validation in the same booted ARM64 Windows guest:
TARTCI_WIN_VCVARS_ARCHES=arm64,x64 tartci windows optimize
```

The optimizer is idempotent. It creates `C:\tmp`, persists the standard Git
Bash/Chocolatey/ccache PATH entries when those directories exist, prewarms common
PowerShell module analysis, preinstalls the configured Windows ARM64 Actions
runner version, creates the standard cache roots, configures ccache, fails if
hosted-runner compatibility tools are missing, and verifies `vcvarsall` + `cl`
for each requested architecture before `tartci windows golden <name>` shuts the
VM down and snapshots it.

Windows cache contract for projects:

- **C/C++ object cache:** use `ccache` first unless the project has already
  standardized on `sccache`. CMake projects should set
  `CMAKE_C_COMPILER_LAUNCHER=ccache` and `CMAKE_CXX_COMPILER_LAUNCHER=ccache`
  or auto-detect `ccache` like Pulp does. Restore/save
  `~/AppData/Local/ccache` in the workflow. This is the cache that turns repeated
  compile-heavy Pulp jobs from "compile the world" into "compile only changed
  translation units".
- **Rust or mixed-language cache:** if a project uses `sccache`, set
  `SCCACHE_DIR=%LOCALAPPDATA%\sccache`, `RUSTC_WRAPPER=sccache`, and for CMake
  use `sccache` as the compiler launcher. Restore/save `~/AppData/Local/sccache`.
  Do not enable both ccache and sccache for the same C/C++ target.
- **Dependency/source cache:** restore/save the project's source cache, not build
  outputs. For Pulp that is `~/AppData/Local/Pulp/fetchcontent-src`, which backs
  `PULP_SHARED_FETCHCONTENT_SOURCE_DIR` / the default `PulpFetchContent.cmake`
  lookup. This avoids re-fetching/re-unpacking dependencies; ccache then avoids
  recompiling them.

The current QEMU Windows lane has disposable overlays and no proven host-mounted
Windows filesystem cache yet. Until an SMB/virtiofs-style host mount is proven,
durability comes from workflow cache restore/save into the above guest paths.
Measure with the workflow's `Ccache stats` step plus `tartci timings`; a faster
boot without cache hits is not the win.

### 4.9 Serve Windows jobs from QEMU hosts

The Windows pool is intentionally QEMU, not Tart. Each GitHub job gets a fresh
qcow2 overlay from the golden, a dynamic localhost SSH port, a one-time JIT
Actions runner, and then the overlay is discarded. Use the same setup on every
Apple Silicon host that should participate in the Windows pool.

Host prerequisites:

```bash
brew install qemu
gh auth status -h github.com
mkdir -p "$HOME/.tartci/goldens" "$HOME/VMs/tmp" "$HOME/VMs/logs"
```

Install the Windows golden on each host:

```bash
cp /path/to/pulp-windows-build-24h2-arm64-YYYY-MM-DD.qcow2 \
  "$HOME/.tartci/goldens/pulp-windows-build-24h2-arm64-2026-06-12-cacheopt.qcow2"
shasum -a 256 "$HOME/.tartci/goldens/pulp-windows-build-24h2-arm64-2026-06-12-cacheopt.qcow2"
```

Keep the runner code on a home-backed path so launchd and non-interactive SSH do
not depend on a mounted workspace:

```bash
./tartci support-manifest write \
  --root . --output .tartci-support-manifest.json
./tartci fleet-macos install profiles/<host>-macos-fleet.toml \
  --support-source . \
  --support-manifest .tartci-support-manifest.json \
  --apply
```

Always run both commands with the **new checkout's own `./tartci`**, never an
older installed `tartci` pointed at a newer checkout with `--support-source`.
The support cohort's directory set is versioned with the code: a generation
that ships `fleet/` writes a manifest an older verifier rejects ("member path
is invalid: 'fleet/README.md'"). That refusal fails closed before any
mutation, but it looks like a broken checkout rather than a version mismatch.
A launcher re-seal (m3) likewise needs the launcher and the support cohort
from the same commit, which the receipt already enforces.

Generate the manifest only from the exact clean source commit being deployed.
It requires and binds the canonical `danielraffel/tartci` GitHub repository key
and every selected provider,
runtime helper, profile, and LaunchAgent template by path, mode, and SHA-256.
Before mutation, `fleet-macos install --apply` also uses `ghapp` to prove that
the exact commit exists in that repository. A profile `[github_app]` block, when
present, supplies host-local references for the proof and rendered services;
otherwise the proof uses `ghapp`'s installed machine-global Shipyard App
context. It verifies the
clean Git source, stages a
non-writable runtime generation under
`~/.local/share/tartci-generations`, atomically switches the canonical
`~/.local/bin/tartci` wrapper, and records the source commit, entrypoint, and
complete cohort in `macos-fleet-install.json`. LaunchAgents execute a
generation-local, non-writable verification entrypoint using the receipted
`/usr/bin/python3`; the mutable convenience wrapper is not launch authority.
Every CLI or supervisor start verifies the cohort, so an ordinary launchd
restart cannot execute post-install drift. Profile, plist, receipt, and wrapper
publication is file- and directory-synced in dependency order; a power-loss
subset either verifies as complete or keeps admission closed for a supported
reinstall. `tartci pool on` verifies the
installed cohort and activates only services named by that receipt, then
compares launchd's in-memory arguments and governed environment against the
receipt before opening admission. Unreceipted persistent or legacy runner
services require their own explicit install/activation authority; this fleet
transaction will not start them incidentally — and, for the same reason, will
not stop them. `tartci pool off` and `tartci pool drain` act on exactly the set
`pool on` can bring back, and print every runner agent they deliberately left
alone; `tartci pool status` marks the same distinction per runner (`pool_owned`
in `--json`). An operation that stops more than its inverse starts is not a
pause, it is a deletion: the unscoped version booted out and `launchctl
disable`d a foreign repository's persistent Actions runner that no `pool on`
would restore, twice taking the sole server of a required check offline with
the plist still sitting on disk. Stopping an unreceipted runner is a deliberate
act performed under its own authority. The composed readback is published as
`~/.config/tartci/macos-fleet-loaded.json`. Missing helpers, unreceipted runtime
files, symlinks, mixed generations, stale loaded arguments, or obsolete loaded
environment fail closed. The prior wrapper/generation remains available for
rollback; generation cleanup is a separate explicit idle operation.

One-shot proof, with the same PATH launchd will use:

```bash
PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
TARTCI_WIN_GOLDEN="$HOME/.tartci/goldens/pulp-windows-build-24h2-arm64-2026-06-12-cacheopt.qcow2" \
TARTCI_RUNNER_REPO=OWNER/REPO \
TARTCI_RUNNER_LABELS=self-hosted,Windows,ARM64,pulp-build-windows \
TARTCI_WIN_WORK="$HOME/VMs/tmp/tartci-win-proof" \
TARTCI_WIN_LOGS="$HOME/VMs/logs/tartci-win-proof" \
"$HOME/.local/bin/tartci" serve windows
```

To create a matching queued job, prefer a Windows-native workflow. For Pulp,
use `Build and Test` with a per-run selector override for a full proof:

```bash
gh workflow run build.yml -R Generous-Corp/pulp --ref main \
  -f runner_provider=github-hosted \
  -f 'windows_runner_selector_json=["self-hosted","Windows","ARM64","pulp-build-windows"]'
```

A tiny workflow can also prove assignment, but it must use a Windows-compatible
shell. A Unix-shell step such as `chmod +x tools/check-docs.sh` will correctly
prove that the runner claimed the job, then fail under Windows PowerShell. Treat
that as an availability probe only, not as a green-lane proof.

After a proof, inspect both GitHub and the host logs:

```bash
gh api repos/OWNER/REPO/actions/runs/RUN_ID/jobs \
  --jq '.jobs[] | [.name,.status,(.conclusion//""),.created_at,.started_at,.completed_at,((.labels//[])|join("|")),(.runner_name//"")] | @tsv'

find "$HOME/VMs/logs/tartci-win-proof" -name timing.tsv -print -exec cat {} \;
tail -F "$HOME/Library/Logs/tartci/qemu-runner-windows.log"
```

The supervisor writes:

- `preflight.log`: guest clock sync, PowerShell execution policy, GitHub/broker
  TCP checks, runner version, JIT config byte count, `vcvarsall` discovery, and
  `cl.exe` visibility after the MSVC environment import.
- `early-clock.log`: minimal guest clock sync before any HTTPS runner download.
- `runner-output.log`: stdout/stderr from `Runner.Listener.exe run --jitconfig`,
  including the runner-process `vcvarsall` import and `cl.exe` diagnostic before
  the Actions agent starts.
- `runner-diag.log`: tail of the latest Actions runner `_diag` logs.
- `qemu.log`: QEMU stderr.
- `timing.tsv`: `boot_to_ssh`, `preflight`, `runner_process`, `post_diag`, and
  `total` seconds for rough host-to-host and hosted-runner comparisons.

Only enable normal routing after a Windows-native workflow proves the lane:

```bash
gh variable set PULP_LOCAL_WINDOWS_RUNS_ON_JSON -R Generous-Corp/pulp \
  --body '["self-hosted","Windows","ARM64","pulp-build-windows"]'
```

Until that variable is set, ordinary Pulp Windows jobs continue to use
GitHub-hosted `windows-latest`. The QEMU supervisor is still safe to leave loaded
because `TARTCI_RUNNER_QUEUE_MATCH_LABELS=1` makes `--loop` boot only when a
fresh queued job's requested labels can be satisfied by this runner's labels.

### 4.10 Speed up the Windows QEMU lane

Optimize from `timing.tsv`, not from intuition. The first smoke proofs showed
QEMU startup was not the dominant cost: boot-to-SSH was roughly 25 seconds,
while preflight diagnostics took about a minute. A full Build and Test run will
mostly be build and test time, so keep both the host timing file and the GitHub
job timestamps when comparing against `windows-latest`.

```bash
tartci timings
tartci timings "$HOME/VMs/logs/tartci-win" "$HOME/VMs/logs/tartci-linux"
```

The Windows runner defaults to `TARTCI_WIN_PREFLIGHT_MODE=fast`: sync the clock,
verify the JIT config landed, record the runner listener version, then launch
the job. The old verbose probe path is still available with
`TARTCI_WIN_PREFLIGHT_MODE=full` when diagnosing a new golden or network/toolchain
issue.

Highest-return changes, in order:

1. **Move deterministic preflight into the golden.** The normal supervisor path
   verifies only clock, runner version, and JIT config by default. Toolchain
   discovery, PATH fixes, execution policy, certificate setup, SDK validation,
   and GitHub broker probes should be baked into the golden and proven during
   image creation. Run `tartci windows optimize` before tagging the qcow2, then
   keep `TARTCI_WIN_PREFLIGHT_MODE=full` for debug rather than paying for those
   probes on every job.
2. **Add a real Windows build cache.** Use `sccache` for C/C++ and Rust
   compilation, plus the project-specific package caches that matter
   (`CMake` downloads, `NuGet`, `Cargo`, `pnpm`/`npm`, and similar). The cache
   must be restored into the guest at job start or live outside the disposable
   qcow2 overlay so every fresh VM can reuse it.
3. **Prefer host-backed cache storage once the clean lane is stable.** A
   VirtIO-backed or otherwise host-mounted cache on local NVMe avoids virtual
   disk churn and survives VM recreation. Treat source trees and build outputs
   as disposable unless a project deliberately opts into an incremental build
   directory.
4. **Keep QEMU on the fast device path.** The runner already uses HVF
   acceleration, virtio networking, NVMe storage, and an ARM64 Windows guest on
   Apple Silicon. Do not spend time on hypervisor swaps until the guest-side
   timings show QEMU itself is the problem.
5. **Trim Windows background work in the golden.** Disable noisy services only
   when the lane is isolated for CI and the effect is measured. Search indexing,
   scheduled maintenance, update orchestration, and Defender scans can affect
   consistency, but they are not a substitute for build caches.
6. **Consider warm workers last.** A pool of already-booted VMs can remove most
   boot latency, but it complicates per-job cleanup, runner registration, and
   rollback. Keep the cold CoW overlay lane as the reliable baseline first; add
   warm workers only after the full Windows proof is green and cache behavior is
   understood.

For ARM64 Windows workloads, this lane can beat GitHub-hosted Windows when the
golden is current and caches are warm because there is no hosted-runner queue and
no x64 translation tax. For x64 coverage or test execution, Windows-on-ARM still
runs through Microsoft's x64 translation layer, so local hardware mainly helps
availability and cache locality rather than raw CPU efficiency.

Treat x64-on-Windows-ARM as a separate smoke lane until proven. The QEMU provider
boots an ARM64 Windows guest with `qemu-system-aarch64`; it does not emulate a
full Intel Windows machine. A repo can try the x64 MSVC environment with
`TARTCI_WIN_VCVARS_ARCH=x64`, but release-fidelity x64 gates should stay on
GitHub-hosted `windows-latest` until those smoke runs are consistently clean.

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
tartci serve macos
tartci serve linux
tartci serve windows

# Keep serving (what the LaunchAgents run):
tartci serve macos --loop --labels self-hosted,macOS,ARM64,pulp-build,pulp-build-vm
tartci serve linux --loop --labels self-hosted,Linux,ARM64,pulp-build-linux,pulp-host-macstudio
tartci serve windows --loop --labels self-hosted,Windows,ARM64,pulp-build-windows,pulp-host-macstudio
```

What the supervisor does each job: clone the golden (Linux/macOS) or make a
CoW overlay on a free SSH port (Windows), wait until the guest is reachable,
optionally require Shipyard's final admission-clean verdict, then mint a
**Just-In-Time** (single-job) runner config via
`gh api .../generate-jitconfig` (needs repo admin). It runs the Actions agent
once with that JIT config, then discards the VM. Minting after boot avoids
spending the time-sensitive JIT token during guest startup. The agent processes
exactly one job and deregisters — no long-lived runner state. The `--loop` gate only boots
when there is queued work matching `TARTCI_RUNNER_WORKFLOW_NAME` or any exact
newline-delimited name in `TARTCI_RUNNER_WORKFLOW_NAMES`, default
`Build and Test`. `TARTCI_RUNNER_WORKFLOW_TIERS` instead accepts ordered
`class-label|workflow` lines. The first tier with demand supplies the JIT
runner's extra labels; a lower tier is rechecked against all higher tiers after
VM boot and before JIT minting. Mutually exclusive job labels make priority
enforceable at GitHub's assignment boundary while preserving GitHub FIFO inside
each tier. `TARTCI_RUNNER_WORKFLOW_TIER_GROUPS` optionally maps those same class
labels, in the same order, to exact runner-group IDs. Group `1` uses the
repository JIT endpoint; a non-default ID uses the organization endpoint only
after a fresh, paginated proof that the target repository can access the group.
Discovery is intentionally bounded and rotated to keep GitHub
API use stable. Consequently, `--print-queue` returning `0` means no match in
that scan window, not that every workflow in the repository was inspected. Use
`shipyard runner fleet-status --repo OWNER/REPO --json` to diagnose the
merge-queue front and required contexts; use Tart CI state and logs to diagnose
VM capacity. `ERR` is the distinct scanner/authentication failure sentinel.

For an exception-only workflow whose availability matters more than avoiding a
rare losing boot, multiple Mac supervisors may watch one shared label. Stagger
them without long-lived runner registrations:

```bash
# Preferred M3 recovery worker
TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS=0

# M5 fallback after five minutes of unclaimed queue time
TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS=300

# M1 fallback after ten minutes
TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS=600
```

Use a distinct runner-name prefix on each host and keep the shared label
exclusive to that recovery workflow. The minimum age is checked from GitHub's
job/run timestamp before VM boot and defaults to zero, so every existing lane
is unchanged. A negative or malformed value fails before the serve loop starts.
Once GitHub assigns the single job, a returning preferred host cannot preempt
it; any losing disposable VM reaches the existing bounded idle teardown.

After the coordinated Shipyard deploy, set
`TARTCI_ADMISSION_CLEAN_MODE=required` on every Linux, macOS, and Windows
provider LaunchAgent. Optionally set `TARTCI_SHIPYARD_CLI` (default `shipyard`),
`TARTCI_ADMISSION_CLEAN_BASE` (default `main`), and the bounded
`TARTCI_ADMISSION_CLEAN_TIMEOUT_SECS` (default 300, range 1..1800). Required
mode fails closed: a typed `admit` is the only path to JIT registration.
`defer` or any operational/contract error tears down the still-unregistered VM,
releases its lease, and lets `--loop` back off by `TARTCI_VM_POLL`.

An `error` verdict is not one thing, and the difference decides whether the
fleet can stop. `mutation_failed` and `invalid_labels` are conclusive: Shipyard
either saw a superseded run it could not cancel, or the lane is misconfigured.
Those stay closed permanently. `observation_failed`, `authority_failed`, and
`revalidation_failed` mean Shipyard could not look at all, which says nothing
about the queue -- as does an `error` reason this TartCI generation does not
recognize, since Shipyard is released separately. Backoff alone does not bound
those: a blindness that outlasts the backoff stops every lane for the repo
indefinitely, and if the cause scales with the repo's own backlog the outage
prevents the draining that would end it.

So an inconclusive verdict opens a bounded circuit breaker instead. The first
`TARTCI_ADMISSION_CLEAN_DEGRADE_AFTER` (default 3) consecutive inconclusive
verdicts still fail closed, so a transient blip keeps the gate at full
strength. Past that the gate emits a degraded admit carrying
`tartci_degraded`, logs a loud line, and counts up to
`TARTCI_ADMISSION_CLEAN_DEGRADE_MAX` (default 20), after which it closes again
rather than staying open forever. Any real `admit` or `defer` resets the count.
Counters live per `(repo, base, labels)` under
`TARTCI_ADMISSION_CLEAN_STATE_DIR` so lanes never pool each other's failures.
A rejected envelope is written to `rejected-envelope.json` in that directory,
so a Shipyard contract skew is diagnosable without reading Shipyard's source.

A `defer` for `observation_in_progress` or `stewardship_in_progress` is not a
verdict about the queue: another caller on the host holds Shipyard's exact-key
observation (or stewardship) lock for the same `(repo, base, labels)`, and
Shipyard answers with a try-lock. Returning it discarded a booted VM for no
reason (130 of them in one day on the Pulp gate, when both lanes of a host asked
at once). The adapter now re-asks for up to
`TARTCI_ADMISSION_CLEAN_CONTENTION_WAIT_SECS` (default 90, range 0..600; 0 is
single-shot) at `TARTCI_ADMISSION_CLEAN_CONTENTION_POLL_SECS` intervals
(default 5, range 1..60). Every verdict that ends the wait is a fresh Shipyard
answer, so the gate stays fail-closed; when the budget runs out the contention
`defer` is returned exactly as before. Every other `defer` reason is a statement
about the queue and is returned at once. A verdict that waited carries
`tartci_contention_waits`, and the provider event renders it as
`contention_waits=N`.

On macOS the boundary's two network proofs, the admission verdict and the
runner group's repository-access proof, start in the background as soon as the
VM lease is held, beside the clone and boot, and the boundary consumes their
results (`boundary-proof.lib.sh`). This takes their duration off every job's
critical path without changing what they decide: a refusal still discards the
booted VM with the same code and events, and an admission verdict older than
`TARTCI_BOUNDARY_PROOF_MAX_AGE_SECS` (default 120, range 0..600) when the
boundary reads it, or any missing or partial result, is replaced by the
synchronous call the boundary always made. The `admission_check` event says
which answer was used (`source=parallel age=Ns` or `source=boundary`).
`TARTCI_BOUNDARY_PROOF_PARALLEL=0` restores the fully sequential boundary.

A repository-access proof that GitHub could not answer (a timed-out or failed
API call, not a denial) is asked once more, synchronously, before the booted VM
is discarded; each retry logs `jit_repository_access_retry` with a `reason=`.
A denial (`rc=3`, HTTP 401/403/404) is never retried and still logs
`jit_repository_access_denied`. A proof that still cannot be answered logs
`jit_repository_access_error`, so every discard at this boundary names its
cause in the lane's event log.

Degrading trades one ephemeral single-job VM that a superseded run may claim --
bounded, non-corrupting, and unable to satisfy the current head's required
checks -- against an unbounded fleet stop. Never widen this to `admit`
verdicts. Keep the
mode `disabled` only during the staged TartCI-before-Shipyard rollout.
The managed macOS fleet profiles always render `required`; for event-class V2,
the gate runs after guest preflight and immediately before repository-access
verification, the pool lock, live assignment/admission rechecks, and JIT minting.
Linux and Windows scan queued and in-progress workflow runs for queued jobs and
add two more default guards: they ignore queued jobs older than
`TARTCI_RUNNER_MAX_QUEUED_AGE_SECONDS` (default six hours), and
`TARTCI_RUNNER_QUEUE_MATCH_LABELS=1` requires a queued job's requested labels to
be satisfiable by the configured runner labels before a VM boots. Set it to `0`
only for debugging broad workflow polling. For coordinated multi-host routing,
add a host label such as `pulp-host-macstudio` or `pulp-host-m5` after the shared
`pulp-build-*` label, then point the workflow's primary and overflow selectors
at those exact label sets. Linux/Windows runners are JIT ephemeral, so they are
not visible as idle registered GitHub runners before a job is queued; the
workflow resolver should compare configured per-host capacity with in-progress
jobs already using each exact host selector. A GitHub Actions job cannot change
`runs-on` after it is queued, so GitHub-hosted fallback must be selected before
the job enters the queue. If multiple Windows hosts are accidentally configured
to race the same queued job, any VM that does not claim work exits after
`TARTCI_RUNNER_IDLE_TIMEOUT_SECS` (15 minutes by default), deletes its stale
GitHub runner registration by ephemeral runner name, and discards the overlay.
Linux applies the same assignment deadline and discards the Tart clone, runner
process, state, and VM lease if `Running job:` never appears. Once that marker
appears, the assignment deadline is disabled so it cannot terminate a valid
long-running build. At the deadline, an exact GitHub runner-state check protects
a runner already marked busy; operational uncertainty is retried a bounded
number of times, and confirmed-idle registrations are removed during teardown.
Invalid or zero timeout values fail before any VM boot.
Before Linux JIT registration, the provider exports `CCACHE_DIR=~/.ccache`,
which must physically resolve to the
writable `/mnt/host/ccache` share with the expected virtio-fs source tag; a stale real directory is replaced
and an unusable mount fails closed rather than silently running with an
ephemeral cold cache. The provider also exports
`CMAKE_BUILD_PARALLEL_LEVEL=${TARTCI_LINUX_BUILD_PARALLEL_LEVEL:-4}`, capped by
the acquired VM lease's cores, so ordinary `cmake --build` workflow steps use
bounded guest parallelism.
Ephemeral Windows runner names include a host-derived prefix by default; set
`TARTCI_RUNNER_NAME_PREFIX` only when a host needs a stable custom prefix.
Windows writes per-job timing to `$TARTCI_WIN_LOGS/<runner>/timing.tsv`; Linux
writes the same shape to `$TARTCI_LINUX_LOGS/<runner>/timing.tsv` (default
`$HOME/VMs/logs/tartci-linux`). Compare those files with `tartci timings` and
GitHub job timestamps before promoting local routing.
macOS supervisors atomically replace their heartbeat state file; `doctor`,
`observe`, and Shipyard fleet probes should treat an unreadable state file as a
real health problem, not as "no active runner."

### Classify a Pulp Actions wait before touching TartCI

First inspect the run's jobs endpoint. A workflow run with **zero jobs** has not
reached `runs-on`, a persistent preamble runner, TartCI, a host lease, or a VM;
changing fleet labels or restarting supervisors cannot repair it. A queued job
requesting `pulp-preamble` is also outside TartCI: verify the persistent runner
registration, launchd-owned process, exact labels, and a real assigned job. Only
a queued job with TartCI lane labels should lead to runner-group, exact-label,
admission, lease, disk, and VM-slot diagnosis.

If required Shipyard admission is repeatedly rejecting a managed M3 Pulp lane,
contain only that lane while preserving the preamble and other repositories:

```bash
uid=$(id -u)
for label in \
  com.danielraffel.tartci.tart-runner-macos-fleet.studio.pulp-gate \
  com.danielraffel.tartci.tart-runner-macos-fleet.studio.pulp-gate.slot2
do
  launchctl disable "gui/$uid/$label"
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
done
```

The disabled state is required because booting out a KeepAlive service alone
does not prevent resurrection. Re-enable only after the installed Shipyard
command passes and a one-job physical canary reaches assignment; an online JIT
registration without assignment is insufficient. Before any deploy or reload,
also prove each existing job terminal, JIT registration gone or appropriately
idle, lease released, and VM absent. Never preempt a live lease merely to make
the installed profile match `main` sooner.

Do not set a fixed `TARTCI_VM_LEASE_PRIORITY` on a managed Pulp event-class-V2
lane. Its exact selected label derives merge-group priority `110` or PR-head
priority `100`; fleet validation rejects an explicit priority that would flatten
that ordering or prevent M1 from using reserved gate cores. Other required-gate
lanes may explicitly set `gate`; an omitted lane priority renders no override
and delegates to the provider's exact-label policy. Advisory supervisors must
yield. Keep required and advisory workflows on distinct class labels. Once a JIT runner is online,
GitHub—not Tart CI—selects any queued job with a satisfiable label set, so
identical labels let an optional snapshot, example, coverage, or GPU job consume
capacity intended for the merge-queue front. Queue order, exact-head
re-enrollment, bounded reruns, and redundant-run coalescing belong to Shipyard.
Do not run Orchard alongside Shipyard and Tart CI.

Everything is env-driven for genericity: `TARTCI_RUNNER_REPO`,
`TARTCI_MACOS_GOLDEN` / `TARTCI_LINUX_GOLDEN` / `TARTCI_WIN_GOLDEN`,
`TARTCI_RUNNER_LABELS`, `TARTCI_RUNNER_GROUP_ID`,
`TARTCI_RUNNER_WORKFLOW_NAME`, `TARTCI_RUNNER_WORKFLOW_NAMES` (macOS
equal-priority multi-workflow lane), `TARTCI_RUNNER_WORKFLOW_TIERS` (macOS
ordered exclusive workflow classes), `TARTCI_RUNNER_WORKFLOW_TIER_GROUPS`
(matching per-class JIT runner-group IDs), `TARTCI_RUNNER_VERSION` (macOS and Windows agent),
`TARTCI_RUNNER_SHA256` (required with a non-default runner version),
`TARTCI_WIN_VCVARS_ARCH` (Windows MSVC environment, default `arm64`),
`TARTCI_WIN_PREFLIGHT_MODE` (`fast` by default, `full` for diagnostics),
`TARTCI_WIN_CPUS`, `TARTCI_WIN_MEMORY_MB`, `TARTCI_WIN_WORK`, and
`TARTCI_WIN_LOGS`. Defaults target `Generous-Corp/pulp`
(the first consumer). When multiple macOS hosts serve the same selector, keep
the workflow selector shared and make the runner name unique by adding an extra
host-specific label after the shared `pulp-build-*` pool label or by passing a
unique `--name-prefix` in the installed plist.

VM runners participate in the host-core lease store by default. Set
`TARTCI_MACOS_VM_CORES`, `TARTCI_LINUX_VM_CORES`, or `TARTCI_WIN_VM_CORES` when a
host needs a provider-specific lease size; otherwise the host profile's
`vm_pool_cores` value is used. A managed macOS fleet lane may declare positive
integer `vm_cores`, which renders the macOS override only for that lane. Pulp's
M3 lane uses 12 so two guests fit its 26-core budget; M1 and M5 inherit 3 and 6.
The macOS hard cap remains a separate <=2 guest
semaphore and fails closed: if `tart list` is unavailable or malformed, the
macOS serve loop treats the cap as already full and waits. Disable the lease
consumer with `TARTCI_VM_LEASES=0` only during operator-controlled break-glass
debugging.

A lane may also declare `process_type`, which sets the rendered LaunchAgent's
`ProcessType`. The accepted values are the four `launchd.plist(5)` documents:
`Background`, `Standard`, `Adaptive`, `Interactive`. A lane that omits the key
renders `Background`, which is what every lane had before the key existed. The
key matters because launchd throttles a `Background` job: the exhaustive
event-class queue scan is a long chain of short GitHub API calls, and the
per-call latency -- not the `assignment_scan_timeout_seconds` budget -- is what
decides whether the scan finishes. A supervisor cannot lift its own
classification (`taskpolicy -B` on itself does not restore the latency), so the
value has to be in the plist, which is rendered from the profile: editing an
installed plist by hand is reverted by the next render. Pulp's gate lanes
declare `Adaptive`; release lanes stay `Background`. Promotion out of the
background band is a launchd heuristic, so treat a declared `Adaptive` as a
request, and confirm what the host actually did with
`launchctl print gui/$(id -u)/<label> | grep 'spawn type'`.

**Windows gotchas preserved from the Pulp original** (debugged live; don't
"simplify" them away): the multi-KB JIT blob is **streamed via ssh stdin into a
file**, never on the outer ssh command line (cmd.exe's 8191-char limit blows
through the ssh→cmd→powershell chain); the agent is started through
`Runner.Listener.exe run --jitconfig` directly while the blob is read from that
file inside PowerShell; the configured Actions runner version is enforced before every JIT run;
stale `C:\actions-runner` registration files are removed because a golden may
cache the runner binary but must not cache `.runner` or `.credentials`;
long preflight / runner PowerShell probes are **streamed into guest `.ps1`
files** and executed there, because adding toolchain diagnostics can push
`powershell -EncodedCommand` past cmd.exe's command-line limit; `vcvarsall` is
discovered via `Get-ChildItem` (vswhere returns empty for a BuildTools-only
install) and imported before both preflight diagnostics and the Actions runner
process so workflow Bash steps can see MSVC; the supervisor **bails the moment
QEMU dies** (`kill -0 $qpid`) so a free-port TOCTOU surfaces fast instead of
burning the full ~10 min SSH window; and a post-extract integrity check asserts
`Runner.Listener.exe` exists before running.

### Serve across reboots (LaunchAgent)

Install one of the templates in `launchd/` so the supervisor runs under
`launchd` and survives reboot. The shipped templates are **Pulp's concrete
instance** — their known labels (`com.danielraffel.pulp.tart-runner-macos-gate`,
`com.danielraffel.pulp.tart-runner-macos-release`,
`com.danielraffel.pulp.tart-runner-linux`, and
`com.danielraffel.pulp.qemu-runner-windows`) are what the
[shipyard-macos-gui](https://github.com/danielraffel/shipyard-macos-gui) "Serve
CI builds from this Mac" switch toggles via `launchctl load/unload` once the
GUI knows about the label. See `launchd/README.md` for the install `sed` recipe
and how to serve a different repo. Two traps carried over from the Pulp lane:
launchd does **not** expand `$HOME`/`$TARTCI_REPO` (the install `sed` must write
absolute paths), and a LaunchAgent can't read a `/Volumes` golden store without
**Full Disk Access**.

For Pulp, keep the macOS workflow lanes distinct:

```text
Build and Test -> self-hosted,macOS,ARM64,pulp-build,pulp-build-vm
Tagged release -> self-hosted,macOS,ARM64,pulp-build-vm-release,pulp-release-tagged
PR release gate -> self-hosted,macOS,ARM64,pulp-build-vm-release,pulp-release-pr-gate
```

The `pulp-release-tagged` label maps to gate-priority host leases, allowing a
real release to use reserved cores even when an advisory VM owns the non-gate
budget. `pulp-release-pr-gate` intentionally stays at ordinary VM priority.
Conflicting release class labels also fail down to ordinary VM priority.
An explicit `TARTCI_VM_LEASE_PRIORITY` still overrides label-derived priority
for this non-V2 release lane; managed Pulp V2 profiles reject that override.

Load the release VM lane only as one separate LaunchAgent with ordered
`TARTCI_RUNNER_WORKFLOW_TIERS`: `Release CLI` and `Sign and Release` share the
first tagged-release class, while `Release-path PR gate` occupies the second.
Do not create one supervisor per workflow or point release jobs at
the Build and Test `pulp-build-vm` lane, and do not flip
`PULP_RELEASE_MACOS_RUNS_ON_JSON` away from the fallback lane until a real
Release CLI proof has claimed `pulp-build-vm-release` and completed.

### Lane python3 cannot import tomllib (`lane_python_no_tomllib`)

Lanes run `gate_supply.py decide` (gate placement), `macos_fleet_lanes.py
render` and `host_profile.py` with a bare `python3`. That is correct only while
the lane plist's PATH puts a 3.11+ python3 (Homebrew's `/opt/homebrew/bin`)
ahead of `/usr/bin`, because macOS's `/usr/bin/python3` is 3.9 and has no
tomllib. `tartci doctor fleet` takes the PATH from each installed lane plist,
resolves `python3` on it the way a shell would, runs it once, and reports
`lane_python_no_tomllib` with the interpreter path, its version and the lanes
that use it when it cannot import tomllib (`scripts/lane_python.py`). Fix the
interpreter (reinstall Homebrew's python3), not the helpers: they read the
fleet profile, and a guess in its place is a mis-placed gate. An interactive
ssh shell has a different PATH and is not what this checks.

### VM DHCP not answering (`vm_dhcp_unanswered`)

A booted VM gets its address from the host's DHCP server: bootpd, a
socket-activated system daemon that macOS Internet Sharing manages. When bootpd
stops answering, every boot waits 120 s for an address (`boot_failed no_ip`) and
is discarded. On m5 this happened on 2026-09-23 (13 VMs in 47 min) and
2026-10-04 (10 VMs in 40 min). On 10-04, bootpd ran nothing from 20:53Z until
macOS itself disabled and re-enabled it at 21:48:51Z.

Each host keeps one breaker (`scripts/vm_dhcp_breaker.py`,
`~/.tartci/state/vm-dhcp/breaker.json`):

- **Opens** on two `no_ip` in a row within 15 min, with no address in between.
  A single `no_ip` has never been an outage. `vm_dhcp_unanswered` records
  bootpd's state, run count and last exit at that moment, and the `no_ip`
  times.
- **While open,** no lane clones. Each pass is idle, not blocked, and takes no
  job claim: the breaker is the first pre-boot check, before the job claim and
  the pre-clone demand check. One lane probes with a single VM every 300 s, or
  at once when bootpd's run count moves (`vm_dhcp_probe result=ip|no_ip`).
- **Closes** on the first address any VM on the host gets, or when the host
  rebooted after the breaker opened. `vm_dhcp_recovered` reports `reason`,
  `open_s`, `vms_spent`, `probes`, and `latency_s` (time since the last probe,
  or since bootpd's run count moved).
- **The trade:** an outage costs about one VM per 300 s instead of one per lane
  every 2 to 4 min. Recovery is noticed within 300 s plus a boot instead of
  within minutes. `latency_s` above 300 s plus boot p99 means the cadence is
  wrong.
- **Fails open:** an unreadable breaker reads as closed. Writes are atomic
  under a lock.
- **Turning it off:** set `vm_dhcp_breaker = false` under `[host]` to disable
  it for one host.

Recovery needs root, and tartci never runs it:

1. `sudo launchctl print system/com.apple.bootpd` and
   `/usr/bin/log show --last 30m --predicate 'process == "bootpd"'` (expect
   silence).
2. `sudo launchctl kickstart -k system/com.apple.bootpd`.
3. If no address arrives within 2 min, run
   `sudo launchctl disable system/com.apple.bootpd && sudo launchctl enable system/com.apple.bootpd`,
   then if needed `sudo launchctl kickstart -k system/com.apple.NetworkSharing`.
   A reboot also clears it.

The probe fires at once when step 2 or 3 moves bootpd's run count. `tartci
doctor fleet` shows the open breaker as `vm_dhcp_unanswered`.

### Reloading a lane supervisor safely (`tartci launchd reload`)

launchd caches a job's spec, so `kickstart`/`KeepAlive` re-run the CACHED spec;
only `bootout`+`bootstrap` re-reads the plist. `tartci launchd reload <label>`
does that full cycle, and it refuses (exit 3, nothing changed) when the lane
is **mid-job** (`scripts/lane_busy.py`, per label, so a sibling lane building
does not block reloading an idle one). For the supervisor launchd runs now, a
lane is busy when any of these hold:

- a descendant is `tart run`, `tart clone`, `qemu-system-*` or a
  `Runner.Worker` (a persistent Actions runner executing a job);
- the supervisor (or a descendant) holds a VM lease in the host lease store
  (`~/.tartci/state/leases/leases.json`, read without its lock). A supervisor
  takes its lease and clones BEFORE `tart run` exists, so this is the only
  signal during the clone;
- its fresh heartbeat (`$TARTCI_STATE_DIR/<runner>.state.json`) names a phase
  past waiting: `booting`, `ensuring-runner`,
  `aqua-preflight`, `chrome-preflight`, `admission-check`,
  `admission-deferred`, `admission-error`, `minting-jit`, `idle-wait`,
  `idle-retarget-check`, `job-running`, `cancel-pending-terminal`. The
  waiting phases (`waiting`, `loop`, `yielding`, `draining`, `stopped`,
  `scan_blind`, `scan_blind_escalated`, `jit-admission-denied`,
  `vm-lease-denied`, `admission-precheck`, `admission-precheck-deferred`,
  `admission-precheck-error`, `backoff`) are idle. The supervisor writes
  `loop` (or `backoff` before its retry sleep) as soon as a work entry
  returns, so a finished or refused entry does not read busy through the sleep.

This closes the window the 2026-09-22 incident fell into: that lane held its
lease and had logged "launching JIT runner" (phase `idle-wait`) with no job
yet, and the process-tree check alone read it idle. A heartbeat older than
max(600 s, 10 x the lane's `TARTCI_VM_POLL`) is stale and ignored, so a
supervisor that died or wedged after a busy phase, holding no lease and no
VM, reads idle rather than refusing forever. Unknown refuses: an unreadable
`launchctl print` (other than "Could not find service"), process table or
lease store, or an unrecognised phase. A lane with no heartbeat from its
running supervisor (one that crash-loops before its first heartbeat, or
cannot write its state file) reads idle when the process tree and the lease
store show no VM, so it never blocks `pool off` or a reload. Wait for the lane to go idle,
or `tartci pool drain`; the explicit
override that accepts killing the job is `--allow-mid-job`.

`--dry-run` runs every precondition, including the mid-job probe, and prints
the plan (`would bootout …`, `would bootstrap …`, `would kickstart -k …`) or
`REFUSE: <reason>`. Exit codes for `--reload`: `0` reloaded / would proceed,
`1` a mutation ran and failed its postcondition, `3` refused before changing
anything. The unattended `tartci launchd heal` path is unchanged: it keeps its
host-wide "no VM running" gate.

### Published fleet supply and how to fact-check it

`fleet/advertised-labels.json` is the declared label supply other projects read
(raw URL and contract in `fleet/README.md`). Check a host against it with
`tartci fleet-macos verify-supply` (also the `supply` finding of `tartci doctor
fleet`), and check GitHub's job history against it with
`scripts/supply_observed.py --repo OWNER/REPO`.

### Keeping a host on main's tartci (`tartci fleet-macos self-update`)

`tartci fleet-macos self-update [--plan|--apply] [--target REF]` is the
2026-09-23 manual update procedure, codified, with verification and rollback.
`--plan` (the default) is read-only for the host; `--apply` performs it.

- **Target.** The installed commit is the executed cohort (the sealed
  launcher's `bundle.json` source_commit on a `[launch_helper]` host, else the
  installed generation's manifest). Main is read from a tartci-owned clean
  clone at `~/.local/share/tartci/update-checkout`, created atomically. The
  target is the newest **first-parent** main commit that is past the soak (30
  min) **and whose check runs are all green**; a red commit is skipped for an
  older green one, and with none green nothing happens (`unverified`). An
  explicit `--target` must be on main's first-parent chain and meet both.
- **Prepare** (changes nothing on the host): `support-manifest write`,
  `fleet-macos validate`, install dry-run; on a sealed host the signing
  identity is **extracted** from the live bundle's leaf certificate and proven
  by a timestamped `codesign` probe with a 60 s bound (a keychain prompt would
  hang an unattended run; the refusal says to run `pulp ship doctor`), then the
  launcher is built under the reseal runbook's immutability preconditions and
  verified.
- **Skew after a verified apply.** The run re-measures skew for the generation
  it just installed and verified, and rewrites `skew.json` (`recorded_by:
  verified_apply`) before finishing, so status reads current at once instead
  of the pre-update skew until the watchdog's next refresh. Checks are not
  re-queried; a failure to record never fails the update.
- **Which code runs which step (and why a change lands one update late).**
  The self-update agent runs `~/.local/bin/tartci`, so the **installed**
  generation orchestrates: the gates (one host at a time, capacity floor,
  rate limit), drain, the mid-job wait, pool off/on, verify, rollback and the
  decision to converge support agents are the code already on the host. The
  **target's** code runs only through the update checkout: `support-manifest
  write`, `fleet-macos validate`, `fleet-macos install` and the support-agent
  template check. A change to orchestration therefore takes effect from the
  update after the one that installs it; a change to validate or install takes
  effect in the update that carries it. This is deliberate: rollback authority
  stays with the known-good generation, and the update never re-executes into
  code that has not yet run on this host. Each attempt receipt records
  `orchestrator_generation` (the commit whose code ran it) beside `target`, so
  `~/.tartci/state/self-update/attempts/*.json` shows which code orchestrated
  each step. A PR that changes orchestration should say "effective from the
  update after next".
- **Gate-reserve ratchet.** Prepare runs `fleet-macos validate <profile>
  --check-reserve`, which fits each gate lane (no explicit priority, or
  `priority = "gate"`) into THIS host's gate reserve from its live
  host-profile, per axis: `supervisors x` the lane's VM cores (an explicit
  `vm_cores`, else the reserve share for `vm_cores_from = "gate-reserve"`, else
  `vm_pool_cores`) against `reserved_gate_cores`, and `supervisors x` the derived VM memory
  against `reserved_gate_mem_mb` (`scripts/gate_reserve_fit.py`). Every
  overcommitted pair is printed as `gate_reserve_overcommitted lane=...
  axis=... demand=... reserve=...` on every update, and `tartci pool status`
  and `tartci doctor fleet` (`gate_reserve_overcommitted`) show the same from
  the installed profile. The update is refused only when the target profile's
  overcommit on some (lane, axis) is strictly greater than the installed
  profile's, both against the same live reserve (`gate_reserve_worse`). This is
  a ratchet because a host whose installed profile overcommits (m1: 2 x 3
  against 3) must still be able to update; a check that let the overcommit
  grow would be no check (m3, 2026-10-04: 2 x 12 against 14 lease-denied the
  second Pulp slot while jobs queued, #373). Resizing is a profile decision
  with the host's owner and must not take agent cores. The resize that keeps
  the reserve and the agent cores as they are is `vm_cores_from =
  "gate-reserve"` on the lane: each slot's VM gets the largest core count, at
  most `vm_pool_cores`, at which all of the lane's slots fit the reserve on
  cores and on derived memory (`gate_reserve_fit.share_cores`; `python3
  scripts/gate_reserve_fit.py share-cores --slots N` prints it for this host).
  The plist carries the rule (`TARTCI_MACOS_VM_CORES_FROM`,
  `TARTCI_MACOS_VM_CORES_SLOTS`), not a number, and the lease helper, `tartci
  gate-supply` and this fit all compute the size the same way. m5 sizes its two
  Pulp slots this way (2 x 4 against 8); on m3's facts the rule gives the 7 that
  #373 chose by hand. A host that reserves no gate cores (a CI runner, or a role
  that keeps none for gates) has no reserve to fit lanes into, so the check
  reads `gate reserve: n/a (this host reserves no gate cores)` and the doctor
  `gate_reserve_not_applicable`, never "fits"; a missing memory reserve beside
  a cores reserve adds a `memory axis n/a` line. The flag is passed by the orchestrating (installed)
  generation, so it starts with the update after the one that installs it.
- **One host at a time.** Every other host in main's
  `fleet/advertised-labels.json` must be `on` and not self-updating, read over
  SSH. The marker's age is measured on the peer's own clock.
- **Update queue.** A host that defers keeps a ticket in
  `~/.tartci/state/self-update/waiting.json` whose `since` records when it
  joined the queue and survives new targets. Hosts take turns in `since`
  order, earliest first, ties to the lower host id: every host computes the
  same order from the same tickets, whenever its survey runs. An off peer and
  a ticket not refreshed within the TTL hold no place.
- **A peer that stays unreachable stops holding the turn.** An unreadable
  peer counts as busy, because it may be mid-update. Each survey records the
  peers it could not read in `~/.tartci/state/self-update/peer-unreadable.json`
  (`since`, `reads`, `last`); any readable read, whether the peer is on, off,
  draining or updating, drops its row. A peer is excluded from turn-taking,
  and only from turn-taking, when all of these hold:
  - this host has read it unreadable at least 4 times in a row over at least
    3 h. That is `ACTIVE_MARKER_TTL`, the age at which a peer's own update
    marker already counts as stale, so a dark peer gets no more trust than a
    seen one. It also outlasts the longest legitimate update;
  - this host reads more than half the published fleet, counting itself. A
    host cut off from the rest excludes nobody, and neither half of an even
    split can proceed;
  - at least one peer is readable, and every readable peer's own record shows
    the same host unreadable at its last read, within the last hour on that
    peer's clock. One peer that can still reach it means it is alive.

  An excluded peer still serves nothing for the capacity floor, so a drain
  that would leave a required label unserved still refuses. At the
  post-announce re-read it is skipped only while it stays unreadable; if it
  answers, the normal protocol applies. Events: `peer_unreachable_excluded`
  once per episode, and `peer_unreachable_rejoined` on its first readable
  read. Doctor: `peer_unreachable` (dark, still holding the turn) and
  `peer_unreachable_excluded`. A host that flaps between readable and
  unreadable never qualifies and keeps blocking; `self_update_starved`
  reports that after 6 h. Recovery of the dark host itself is manual: it
  needs someone at the machine.
- **A change to the queue or peer gates cannot fix a wedge it caused.** Each
  host decides with its *installed* tartci, so a fix to the deciding code only
  takes effect after some host updates. If the queue itself is wedged, unwedge
  it once by hand: move the waiting ticket of a host that is wrongly ahead
  aside (it rejoins at the back on its next attempt), or update the first host
  manually. Delete the moved ticket after that host updates.
- **Capacity floor.** `--allow-last-serving-host` only when every last-serving
  label is either idle by design (the pulp-release classes) or **minted on
  demand by another host**, logged in the receipt. On an ephemeral JIT fleet a
  runner is registered only while it holds a job, so "no other host has a
  runner registered right now" is the idle norm, not a missing server. A label
  counts as served when another host publishes a registration carrying it (for
  the same repository) in main's `fleet/advertised-labels.json` and that host's
  `pool status --json`, read over SSH, proves it can mint: pool `on` and
  participating, `managed` and `fleet_ready`, no `problems`, every expected
  supervisor verified running, `serving.blocked` false, and installed supply
  `match`ing the published supply. Any field missing or unreadable (including
  an older peer tartci) does not count, and a `capacity_unknown` census still
  refuses.
- **Snapshot, update, verify.** Before draining, the running generation is
  snapshotted under `~/.tartci/state/self-update/rollback/<time>-<commit>/`:
  the installed profile, the approval pin and (sealed) a `ditto` copy of the
  live launcher. Then drain, wait up to 90 min for no mid-job lane, `pool off`,
  pin the new approval, install dry-run and `--apply` (retried), relay
  reconcile, `pool on` through the installed shim, verify (pool on and fleet
  ready, serving not blocked, executed commit == target, `launchd guard`
  present).
- **A failure never leaves the host off if `pool on` can work.** Every
  failure path ends with `pool on` (three attempts, 15/45/90 s apart). If it
  still refuses (for example a receipt it rejects after a macOS update), the
  running generation is reinstalled from its own commit and `pool on` is tried
  again. A reinstall needs the pool off, so a still-draining host is first
  waited idle and taken off; if it never goes idle it is **undrained** instead
  (`tartci pool undrain`: re-enables the owned agents drain disabled and
  reopens admission, installing and verifying nothing), which restores exactly
  the service it had. A terminated run (launchd SIGKILL pending) only tries
  `pool on`. The receipt and `last.json` record the real resulting pool state
  (`pool_state`); anything but on/undrained is `host_off` and is shown by
  `pool status`, `doctor fleet` and the watchdog. Reinstalling a target that
  failed verification (because the rollback also failed and it is what the
  host runs) is written as such in the receipt.
  `pool on` itself now waits up to 45 s for a just-kickstarted persistent
  Actions runner to reach `running` instead of failing on the first read.
- **macOS updates.** A macOS update replaces `/usr/bin/python3`, so the
  install receipt's interpreter hash stops matching. When the OS-managed
  interpreter changed (still root-owned, same path and mode) and the OS build
  differs from the one the receipt records (`support.os_build`; for older
  receipts, `/Library/Receipts/InstallHistory.plist` records a "macOS
  <version>" install of the running version after the receipt was written;
  SystemVersion.plist's mtime is the sealed image's build date, not the
  install date, so it cannot say), `pool status`
  reports `interpreter_changed_by_os_update` with its remedy instead of a
  bare `receipt_mismatch`, and `self-update` reinstalls the same generation
  at its next idle window even when current with main. Any other interpreter
  difference is still an unexplained mismatch and fails closed. Until the
  reinstall, `pool on` on that host still refuses.
- **Rollback.** A failure before anything new is installed (the installer
  rolls its own failure back) restores the pin and runs `pool on`. A failure
  **after** a successful install (relay, pool on, verify) rolls back: wait for
  idle, `pool off`, check out the previous commit, reinstall it from the
  snapshot profile (sealed: the snapshot bundle, with the previous pin), `pool
  on`, and **verify the previous generation**. The receipt says `rolled_back`
  ("rolled back to X and verified"); if the rollback itself fails, the host is
  put back on if possible and the receipt says `ROLLBACK FAILED` with what the
  host now runs. `pool on` is retried; a host it could not bring back is
  recorded as `HOST LEFT OFF`. SIGTERM (launchd stopping the agent) raises
  inside the run: the host is put back on, the marker cleared and the receipt
  finished (no full rollback in the stop window; the receipt names what runs).
- **Receipts, rate limit, halt.** One receipt per attempt under
  `attempts/`; `last.json` holds only real outcomes (succeeded, failed,
  rolled_back), so a refusal never hides a failure. One attempt per target per
  6 h. After 3 consecutive failed or rolled-back attempts automatic attempts
  stop until `self-update --clear-halt`. Refusal receipts older than 7 days are
  pruned.
- **Always visible.** `pool status`, `doctor fleet` (`self_update`) and the
  watchdog show the skew line, `STALE` after 24 h, a failed or rolled-back last
  attempt, and a halt.
- **A host left OFF is put back, and it is loud** (`scripts/host_off.py`).
  When `last.json` says a failed update left the host OFF and nobody has
  changed the pool since (the pool-state file is not newer than that record,
  or than the last recovery attempt), both the self-update run (before any
  update logic, so the same-target guard cannot block it) and the launchd
  watchdog (every 5 min, first thing in its pass) run `pool on` for the
  generation installed now, at most once per 5 min. Recovery installs nothing,
  so the one-attempt-per-target guard does not apply to it. While the host is
  unexpectedly OFF, `pool status` shows `problem: host_off_unexpected` with the
  cause and duration and the watchdog logs `WARN host-off` every pass. Past
  15 min, `~/.tartci/state/self-update/events.jsonl` gets one
  `host_off_unexpected` event per episode and a GitHub issue is opened on
  danielraffel/tartci through `ghapp`, run from the update checkout
  (`TARTCI_HOST_OFF_ISSUE=0` turns the issue off). Recovery logs
  `host_off_recovered` and closes the issue. m3 on 2026-09-29 was OFF from
  08:40Z to 17:16Z: two updates and their rollbacks could not `pool on`
  because the launch helper's volume probe timed out on a stalled Workshop
  volume, and the same-target guard then refused every scheduled run.
- **Disk pressure.** `pool status` prints a `disk:` line for the VM store
  volume (`host.tart_home`) and the home volume: `WARN` at 85% used. At 92%
  the VM store volume is a readiness problem (`disk_pressure`, fleet ready: NO),
  because a volume that full stalls I/O. The home volume only warns: what fills
  it (iCloud, caches, Chrome's code-sign clones) is outside what the fleet can
  act on.
- **Launch helper volume probe.** `pool on` proves the signed launcher can
  write the VM store (`--probe-store`: write, read back, remove one small file
  under `tart_home`). It runs at `ProcessType` Standard (a Background job's disk
  I/O is throttled behind the host's own I/O) with a 60 s deadline
  (`TARTCI_LAUNCH_HELPER_PROBE_TIMEOUT_SECS`, 10-300). A denial fails at once
  with an exit code; only a slow volume reaches the deadline, and the error
  says so.
- **Periodic agent** (`launchd/com.danielraffel.tartci.self-update.plist.template`,
  every 30 min, `--apply --scheduled` with a per-host stagger, ExitTimeOut 120)
  is not installed by default: `scripts/install_self_update_agent.sh` prints
  the plan and the resolved peers, and `--install` loads it.

- **Interruptions.** The installer runs in its own process group, and its
  pgid and start time are recorded in `~/.tartci/state/self-update/installer.json`
  while it runs. launchd gives the agent 120 s (ExitTimeOut) after SIGTERM
  before SIGKILL, so a SIGTERM is deferred for at most 80 s while the
  installer finishes; after that the installer group gets TERM and 10 s for
  its restore trap, and recovery (re-pin to the live launcher, `pool on`,
  finished receipt) runs inside the window. The trap is not guaranteed to
  finish: an installer still running then is left recorded, and the next run
  refuses ("installer is still running (pgid N)") until it has exited. An
  install past 30 min gets TERM to its group and up to 120 s for its trap,
  and is never retried on top of itself.
- **Killed runs are recovered.** Every apply receipt is on disk from its
  first step with the run's pid and start time. The next run that finds a
  `running` receipt whose process is gone recovers it: re-pin to the live
  launcher, `pool on`, finish the receipt as `failed` ("interrupted"),
  write `last.json` (it counts toward the halt) and clear the stale
  `active.json`; it then stops, and the following run proceeds. A run killed
  before it announced changed nothing and is closed as refused. A receipt
  whose process is alive refuses the new run. `tartci fleet-macos self-update
  --verify` checks the running generation without changing anything; use it
  after any interrupted run.
- **Snapshots are verified up front.** On a sealed host the `ditto` copy of the
  live launcher must verify against the current approval pin before the drain
  (the copy rollback would reinstall), or the run refuses. The newest 5
  snapshots and 3 builds are kept.
- **Rolling back to a commit that predates self-update** (for example
  `ee28821`) leaves a host whose installed tartci has no `fleet-macos
  self-update`: the periodic agent then fails on every run (it fails closed;
  nothing is changed) and the host no longer updates itself. Recover by hand
  from a checkout of current main, with that checkout's own tartci: `./tartci
  fleet-macos self-update --plan`, then `--apply` (or the manual install
  procedure). After the fix that caused the rollback lands, the next scheduled
  run on a host that still has self-update picks it up normally.

### Shipyard and pulp CLI freshness (`tartci fleet-macos tool-freshness`)

tartci's skew covers tartci only. The other tools every gate and agent session
on a host runs are measured the same way, so a merged improvement is seen to
arrive, and a host that did not get it is loud:

- **What is measured.** The installed version (`~/.local/bin/shipyard
  --version`, `~/.pulp/bin/pulp version`) against the newest plain `vX.Y.Z`
  release in the repository's public Atom feed (no REST quota). A behind tool
  is dated from the first release newer than it; an install older than every
  release in the feed is shown as `>=` that oldest entry, and the bound never
  moves later. `STALE` once behind for more than `stale_hours` (12).
- **Where it shows.** A line per tool in `tartci pool status` and `tartci
  fleet-macos config-verdicts`, a `tool_freshness_*` finding in `tartci doctor
  fleet` (stale is a problem, so doctor exits 1), and a rate-limited WARN in
  the launchd watchdog log. Never folded into `fleet ready`. `pool status` also
  prints fseventsd from the host-vitals sensor's published reading
  (`~/.local/state/pulp/host_vitals.json`), warning above 1024 MB; the remedy is
  `sudo killall fseventsd`, which nothing here runs.
- **When it refreshes.** Every watchdog pass, at most every 30 minutes;
  `tartci fleet-macos tool-freshness --refresh` measures now.
- **Cached lines carry an age limit.** Every status line read from a cached
  measurement is flagged once it is older than three of its refresher's
  intervals (`scripts/state_age.py`, `STALE_FACTOR = 3`): tartci skew and tool
  freshness against the watchdog's 30 minutes, fseventsd against the
  host-vitals sensor's 60 s. The line then reads `STALE (measured N ago, older
  than 3 x <interval>; is the <refresher> running?)`, the same text becomes the
  surface's problem (doctor `self_update_problem` / `tool_freshness_stale`, the
  watchdog WARN), and an unreadable time counts as stale. One missed run is
  ordinary; three in a row mean the refresher is not running, which is how m3's
  skew read "1 commits behind main" for two days while the watchdog's pass
  never ran.
- **Automatic apply.** Shipyard updates itself from here by default: once its
  newest release is 30 minutes old, `shipyard runner fleet-update --to <tag>
  --host-class <this host> --apply --json`, the governed rollout that stages
  CLI, daemon, ghapp, token helper and close guard as one content-addressed
  generation, probes it, swaps atomically and rolls back on failure. Success
  needs the JSON `fleet_summary` verdict `verified` AND the re-read CLI and
  ghapp generation versions both equal to the release. The host class is
  `host_class` in the settings, else the installed fleet profile's `host.id`
  (studio, m1, m5); with neither, apply refuses. One attempt per release per
  6 hours. Never `shipyard update --refresh-daemon` here: it swaps only the
  CLI and daemon and leaves ghapp on its old generation (m3's ghapp stayed on
  0.217.0 under a 0.222.0 CLI).
  The pulp CLI is applied too: its own session-start update runs only from a
  checkout that carries the hook, and m1/m5 agents open primary checkouts
  thousands of commits behind that do not, which left both 27.8 h STALE. The
  watchdog fetches `tools/install/install.sh` at the release tag and runs it
  pinned (`PULP_VERSION`, into `~/.pulp/bin`, no PATH or SDK changes), refusing
  an installer that still excludes the WebGPU runtime; success is the re-read
  `pulp version` equal to the release. `~/.config/tartci/tool-freshness.toml`
  overrides per host:

      stale_hours = 12
      host_class = "studio"
      [tools.shipyard]
      auto_apply = false     # stop automatic Shipyard updates on this host

- **A fleet host must carry its maintenance agents.** `tartci doctor fleet`
  reports `host_agents_missing` (a problem) when a host with an installed
  fleet profile has no loaded `com.danielraffel.tartci.launchd-watchdog` or
  `com.danielraffel.tartci.self-update`. m5studio was brought up serving gate
  VMs with no watchdog, so its freshness was never measured and Shipyard sat
  on 0.224.2 behind 0.230.0.
- **The ghapp generation is measured too.** `readlink
  ~/.local/bin/ghapp.shipyard-generation` names the generation; that
  directory's `shipyard --version` is its release. The line reads `shipyard:
  0.222.0 (ghapp generation 0.217.0) behind ...`, and the older of the two
  decides behind and STALE. An unreadable generation is UNKNOWN, never
  current; a host with no generation link measures the CLI alone.
- **Deploy log.** `~/.tartci/state/tool-freshness/events.jsonl` gets one
  `tool_deployed` event (tool, `component` cli or auth_generation, from, to,
  `by` auto_apply or observed, verify) whenever an installed version changes, whoever changed it, and a
  `tool_apply_failed` event when an automatic apply does not land.
- **A pulp release is installed only when it is complete, and waited for
  patiently until then.** The pulp repository's Atom feed lists a tag the
  moment auto-release pushes it, about an hour before the release build uploads
  its archives (v0.884.0: tag 12:25Z, darwin-arm64 archive 13:29Z, release
  13:32Z), so the 30-minute minimum age alone does not cover it. Before any
  install the watchdog checks every asset the install needs
  (`pulp-<platform>.tar.gz`, `SHA256SUMS`) at its release download URL (HEAD
  200; no REST quota). Until they are there, `pool status` reads
  `pulp v0.884.0 not ready yet (asset pulp-darwin-arm64.tar.gz missing),
  waiting`. That is not an attempt, not an alert and not a problem; it is
  re-checked on every refresh. Only a release still incomplete after
  `not_ready_alert_hours` (6) becomes a problem and logs one
  `tool_release_incomplete` event. The install then downloads the installer,
  `SHA256SUMS` and the archive, and installs only an archive whose sha256
  matches. Any download failure exits 75 before anything is installed and is
  handled the same way. The one-attempt-per-target guard (`apply_retry_hours`)
  covers only failures after the downloads succeeded (a checksum mismatch, the
  install, the verify). Attempt records whose text shows a download 404 (written
  before this change) no longer hold a host back. Optional backup:
  `[tools.pulp] local_archive_dir` holding `<tag>/pulp-<platform>.tar.gz` is
  used only while the release archive is not downloadable, and only if its
  sha256 matches the release's own `SHA256SUMS`.
- **The watchdog must actually run.** `tartci launchd heal` reconciles the
  relay network profile first, and that reconcile defers whenever any Tart VM
  is running. It used to return 6 before the watchdog, so a busy host with a
  network profile never ran its heal pass, skew refresh or config warnings (m3,
  for two days). A failed reconcile is now printed to the watchdog log and the
  pass runs anyway; the exit stays 6 to keep the signal.
- **Retired lanes no longer hold the reconcile.** A runner LaunchAgent kept on
  disk but disabled in launchd (m3's two legacy `tart-runner-macos-release*`
  agents) cannot be bootstrapped, yet the reconcile planned a full reload for
  it and so deferred forever behind the host's running VMs. A disabled,
  unloaded controller is now staged (`stage-disabled`: plist kept current,
  receipt `staged`, never loaded) without waiting for idle; a controller that
  is enabled, or disabled but still loaded, still reloads only when no VM runs.

#### Adding a machine

1. Add `profiles/<host>-macos-fleet.toml` with `[host] ssh = "<alias>"`, the
   SSH alias the other fleet hosts reach it by (for example `m3`). Without it
   the convention `tartci-<host_id>` is used, so each host needs that alias in
   `~/.ssh/config`.
2. Regenerate `fleet/advertised-labels.json` (CI rejects a stale copy) and
   merge. Every host's one-at-a-time check now includes the new machine; no
   per-host file is edited. `~/.config/tartci/self-update.toml [peers]` exists
   only to override a target.
3. On the new machine: install, then `tartci fleet-macos self-update --peers`
   to see every peer it will check and the target it resolves to.

### Keep agents off raw `launchctl` (`tartci launchd guard`)

The 2026-09-22 incident was a raw `launchctl kickstart` by an agent on a lane
supervisor: no tartci code was on that path, so no tartci refusal could fire.
The choke point is the agent harness's PreToolUse hook. `tartci launchd guard
--hook` reads the hook's JSON on stdin and exits 2 (blocking the tool call,
with the reason on stderr) when a shell command would run `launchctl`
`kickstart|bootout|unload|remove|kill|disable|stop` against a runner/lane
supervisor (`com.danielraffel.<repo>.tart-runner*` / `.qemu-runner*`, which
includes the fleet lanes `com.danielraffel.tartci.tart-runner-macos-fleet.*`)
or an `actions.runner.*` service. tartci's non-runner agents (launchd-watchdog,
reap, reclaim, the relay) are not lanes and pass. Here-document bodies are
data (a commit message or file that mentions `launchctl bootout` passes)
unless the heredoc feeds `bash|sh|zsh|dash|ksh` or `ssh`. It sees through `&&`, `;`, `|`, newlines, `bash|sh|zsh -c '…'`,
`eval`, `$(…)`, `env`/`sudo`/`nohup` prefixes, `launchctl asuser`, `ssh host
'…'`, `/bin/launchctl`, and `gui/<uid>/<label>`, `user/<uid>/<label>`, plist
paths or bare labels. A target it cannot resolve statically (a loop variable,
a glob, labels piped into `xargs`, `bootout gui/<uid>` of the whole domain)
is blocked too. Read-only verbs (`print`, `list`, `print-disabled`) and
services outside those families pass silently. The explicit, auditable escape
hatch is `TARTCI_ALLOW_RAW_LAUNCHCTL=1` written in the command itself
(allowed with a warning). Malformed hook input exits 0 with a note so a broken
hook never breaks the agent's shell.

`tartci hooks print` prints (never writes) the settings snippet for Claude
Code (`~/.claude/settings.json`, PreToolUse matcher `Bash`) and for Codex
(`.codex/hooks.json`), both pointing at `hooks/claude-pretooluse-launchctl.sh`,
a shim that resolves `tartci` relative to itself. Merge it by hand. Try a
command without a hook: `tartci launchd guard --command '<cmd>'; echo $?`.

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
- **Pool serving:** `tartci serve macos|linux|windows` wired (ported from Pulp's
  proven `tools/ci` supervisors and the macOS tartci provider); LaunchAgent
  templates in `launchd/`.

## Host resource governance

A tartci host is shared: CI validation builds, agent builds, and VM runners all
land on the same Mac. tartci is the per-host **governor** that keeps them from
oversubscribing it (two hosts melted in July 2026 — one CPU-bound, one
memory-bound/OOM — before this existed). Three pieces tie together:

- **Weighted lease store** (`scripts/leases.py`) — every build and VM runner
  acquires a lease before it starts. Priority classes (`background` < `build` <
  `vm` < `runner` < `gate`) order contention, and a reserved gate-core headroom
  (`reserved_gate_cores`) keeps the required `macos` gate schedulable even when
  non-gate work fills the host. `tartci leases` inspects/acquires/releases it.
- **Memory as a second axis** — leases carry a memory weight (`--mem-mb`,
  capacity via `--capacity-mem-mb`); admission is `min(core-budget,
  memory-budget)`, so a build is refused when it would exhaust RAM even if cores
  are free. Legacy core-only records are estimated as `cores × per-job memory`
  so a mixed store never over-admits. The gate reserve applies on this axis too
  (`reserved_gate_mem_mb`, overridable with `--reserved-gate-mem-mb`): a
  non-gate lease is held to `capacity - reserve`, because a non-gate build that
  fits the non-gate *core* budget could otherwise consume the RAM the next gate
  VM needs and darken the required `macos` gate. The reserve is derived
  proportional to `reserved_gate_cores` and clamped so non-gate work always
  keeps at least one compile job's worth. A denial names which limit bound it
  (`memory_limit_class`: `non_gate` or `host`).
- **Disk as a third, per-volume axis** — macOS/Linux Tart clones reserve growth
  against `TART_HOME`; Windows overlays reserve against `TARTCI_WIN_WORK`.
  Device ID, not a spelling of the path, is the accounting key, so aliases on
  one volume contend while internal and external stores remain independent. The
  free-space check and reservation commit occur under the same `leases.lock`
  transaction as CPU/RAM admission. JSON status and denial records emit
  `free_bytes`, `reserved_bytes`, `requested_bytes`, and `required_bytes` for
  diagnosis.

- **Agent core floor (opt-in, default off)** — when the non-gate core budget
  is full, a build lease is denied and Pulp's `governed-build.sh` falls back to
  a leaseless `-j2`. Setting `agent_floor_cores` (and optionally
  `agent_floor_pool_cores`, default equal to it) in a fleet profile's `[host]`
  table lets a caller that passes `leases acquire --allow-floor` receive a
  *floor lease* instead: `min(requested, agent_floor_cores, pool remaining)`
  cores, marked `floor: true` and `qos: background`. Floor leases are excluded
  from every figure other admissions read (`used_cores`, `non_gate_used_cores`,
  and both memory totals), so gate, VM and ordinary build leases admit exactly
  as if they did not exist; they are reported as `floor_used_cores` /
  `floor_available_cores`. CPU is the only oversubscribed axis, arbitrated by
  background QoS (the caller must run the build under `taskpolicy -b`). Memory
  is not oversubscribed: a floor lease must still fit the non-gate memory limit
  counting every live lease, and the pool is clamped to the host's unleased
  memory (OS headroom + link/LTO reserve, e.g. 10 jobs on a dedicated builder).
  Gate and VM requests never take a floor. `TARTCI_AGENT_FLOOR_CORES` /
  `TARTCI_AGENT_FLOOR_POOL_CORES` override the profile for one shell; `tartci
  host-profile` exports the effective values.
- **One config surface: `tartci governor`** — every tuning knob lives in
  `~/.config/tartci/governor.toml` (flat `[governor]` table; override the path
  with `TARTCI_GOVERNOR_FILE`). `tartci governor show` prints each knob, its
  value and where it came from, plus the derived budget; `tartci governor set
  KEY=VALUE ...` edits the file (validated) and re-shows; `unset KEY` returns a
  key to its default; `explain` says what an interactive build, a background
  build, a class-less build and a gate VM would be granted right now; `fleet
  --hosts a,b` reads `show` from each host over SSH; `keys` lists every knob.
  Precedence per key: CLI flag > `TARTCI_GOV_<KEY>` env > governor.toml >
  legacy source (the role file; the fleet profile's `[host]
  agent_floor_cores`) > role default. Knobs: `role`, `human_reserved_cores`
  (headroom), `gate_guarantee_cores` (static gate reserve S),
  `gate_prompt_reserve_cores` (P), `interactive_share_cores`,
  `background_share_cores`, `interactive_min_cores`, `interactive_wait_secs`,
  `agent_floor_cores`, `agent_floor_pool_cores`, `dynamic_lending`,
  `fleet_hosts`.
- **Build classes** — `leases acquire --class interactive|background`.
  *Interactive* (someone is waiting: `pulp build`, a release build) runs at
  normal QoS, may take a partial lease (`--min-cores`), may wait locally for one
  (`--wait-secs`, retried every 2 s, no remote calls) and is never given a
  background-QoS floor lease. *Background* (Shipyard-local PR validations,
  opportunistic work) is capped at `background_share_cores` in total (defaults:
  dedicated builder half of N, laptop 4, light 2), never borrows, and keeps the
  role QoS and the agent floor. A lease without `--class` follows the exact
  pre-class rule. `host-profile` exports `TARTCI_GOVERNOR_SCHEMA=1` so a caller
  can tell the store accepts `--class`.
- **Dynamic gate lending (opt-in per host, default off)** — with
  `dynamic_lending = true`, an interactive lease may grow non-gate use past the
  guaranteed budget N = T - S, up to `max(N, T - G - P)` where G is the cores
  gate leases hold and P is the prompt reserve kept free for the next gate job
  (`auto`: one gate VM on a host whose fleet profile declares a gate lane — the
  size of the last gate lease admitted here, recorded in `gate_hint.json` — else
  0; a live gate-priority VM waiter raises it to the waiting cores). Non-gate
  use above N is **lent**: gate admission does not count it, so a gate lease is
  never admitted less often than without lending. When a gate grant overlaps
  lent cores, the store marks the newest borrowers `preempted` and moves their
  whole process tree to background QoS (`taskpolicy -b -p`); nothing is killed,
  and new non-gate admissions are already denied. The next acquire, release or
  heartbeat after the overlap clears moves them back (`taskpolicy -B -p`).
  `TARTCI_QOS_ACTION_LOG=<file>` records those calls instead of making them.
  Status reports `gate_used_cores`, `lent_cores`, `interactive_used_cores`,
  `background_used_cores` and `preempted_lease_ids`. Memory is not lent: the
  static gate memory reserve is unchanged. Turning lending off does not strand a
  live borrower; it stays preemptible until it ends.
- **Ranked VM lease waiters (opt-in, default off; m5 canary)** — `[leases]
  rank_vm_waiters = true` in a fleet profile lets a VM lane register as a
  waiter (`leases wait` / `leases withdraw`) and defers a VM `acquire` or
  `resize` (`reason=deferred_to_waiter`, rc 75) while a strictly
  higher-priority live waiter that fits now would not fit after it. Ties stay
  first-come, a waiter that cannot fit blocks nobody, and non-VM (agent build)
  leases are never ranked or deferred. Waiters live in `waiters.json` beside
  `leases.json` and expire after `waiter_fresh_secs` (default 90) without a
  refresh or when their owner process exits. Full rules and the canary proxy:
  `docs/assignment-v2-rollout.md`, "Ranked VM lease waiters".

- **A VM lease's memory is the guest's memory** — for a Tart lane, the figure
  charged on the memory axis is the figure the clone is booted with
  (`tart set --cpu C --memory M`). A clone otherwise inherits its golden's baked
  memory, so the charge and the boot size would agree only by coincidence, and
  Pulp's guest-side build governor derives its job count from the memory the
  guest can actually see. The size is derived after the non-gate core clamp, so
  a clamped lane is charged for the cores it receives; an explicit
  `TARTCI_<PROVIDER>_VM_MEM_MB` override is used verbatim instead.
  `TARTCI_VM_LEASE_MIN_MEM_MB` / `TARTCI_VM_LEASE_MAX_MEM_MB` bound the
  derivation. Raise the ceiling only against a fresh measurement of
  per-Virtualization-process RSS against configured guest memory: that ratio
  runs above 1, so concurrent guests cost more host RAM than they are
  configured for.

  Managed macOS fleet lanes also set one host-level
  `TARTCI_DISK_DENIAL_RECEIPT_DIR` and their configured stable
  `TARTCI_RECEIPT_HOST_ID` (`m1`, `studio`, or `m5`). After every lease attempt,
  TartCI atomically overwrites one receipt named for the exact stable runner
  identity. A denied receipt distinguishes
  `disk_capacity_insufficient`, `disk_probe_failed`, and
  `disk_floor_misconfigured` and
  carries one authoritative frame of `free_bytes`, `reserved_bytes`,
  `requested_growth_bytes`, `floor_bytes`, `required_bytes`,
  `available_after_reservations_bytes`, and
  `required_after_reservations_bytes`, plus probe path and device identity.
  `available_after_reservations_bytes` is clamped at zero when reservations
  exceed current free bytes; `required_after_reservations_bytes` is
  `floor_bytes + requested_growth_bytes`, while `required_bytes` is
  `floor_bytes + reserved_bytes + requested_growth_bytes`.
  A later success or non-disk denial overwrites it as `resolved`; observers must never
  infer disk pressure from exit 75, which is shared by several admission and
  supervisor outcomes. Receipt publication is best-effort telemetry only: a
  write or decode failure cannot change the lease decision or its exit status.
  Observer input is capped at 1 MiB and publication has a two-second wall-clock
  deadline; atomic replacement leaves either the preceding complete receipt or
  the new complete receipt if that deadline fires.
  TartCI does not delete user work in response to this receipt.

  A denial is a symptom, and nothing in the lease path fixes its cause. The
  `com.danielraffel.tartci.reclaim` LaunchAgent
  (`launchd/com.danielraffel.tartci.reclaim.plist.template`) is what keeps a
  host from reaching the denial at all: hourly, it removes regenerable build
  directories that are idle past an age gate, and exits non-zero when the host
  is still below `TARTCI_RECLAIM_FAIL_BELOW_GB` afterwards so a full disk
  surfaces as a failing agent rather than only as refused leases. That floor is
  judged on the volume lease admission probes, the one holding the Tart store
  (`TARTCI_RECLAIM_LEASE_PATH`, else `TART_HOME`, else the fleet profile's
  `[host].tart_home`); a scanned volume elsewhere (m3's boot-disk `~/Code`,
  mostly personal data) is reported and noted when low but cannot fail the
  pass. With no Tart store declared, every scanned volume is judged. Preview a host
  with `tartci reclaim` (dry run) before installing it; see
  `launchd/README.md`. Ask `tartci status` whether this host actually has that
  agent, whether launchd holds it, and how much room is left on each volume it
  scans: a host that never got the agent looks exactly like a host whose passes
  are all finding nothing, and that is how m3 ran a stale generation while m1
  and m5 carried no reap agent at all. This does not contradict the line above: the reclaimer
  deletes generated build output that carries no source marker, never a
  checkout.

  Pulp worktrees need Pulp's knowledge to be reclaimed, not an age gate: on m3
  on 2026-09-27 the pass above ran under pressure and reclaimed 0 bytes while
  1,330 GB sat in 40 merged worktrees' build dirs, and one 2-day-old 40 GB
  build-cov alone exceeded the 21 GiB left (a gate VM lease needs about 49).
  A host opts in with a fleet-profile table, off unless present:

      [reclaim]
      pulp_worktree_builds = true
      repo = "/Volumes/Workshop/Code/pulp"
      worktrees_root = "/Volumes/Workshop/Code/agent-worktrees"
      pressure_free_gb = 200          # optional
      worktree_build_idle_hours = 2   # optional, 2..24

  Each pass then materializes a sparse origin/main worktree of `repo`
  (tools/scripts + tools/ci) under `~/.tartci/state/reclaim/pulp-reapers` and
  runs Pulp's `clean_build_cov.sh --yes` every pass and
  `clean_worktree_builds.sh --yes` while the `worktrees_root` volume is below
  `pressure_free_gb`, both with `PULP_WORKTREES_ROOT=worktrees_root`. Their own
  gates (merged and proven, idle, no process using it, lineage not `active`,
  a fresh path check) are the safety; tartci adds and relaxes none.

  Finished git checkouts that agents left in `/private/tmp` are a separate
  opt-in (`scripts/tmp_checkouts.py`), because nothing else ever looks there:
  m5 held 432 on 2026-09-27. With `tmp_checkouts = true` (and optionally
  `tmp_checkout_idle_hours`, 24..720, default 48) in the same `[reclaim]`
  table, each pass removes a checkout directly under `/private/tmp` only when
  no process has its cwd inside it and no live build names it, it has been
  idle for the window, `git status --porcelain` is empty, HEAD (and, for a
  plain clone, every branch) is on a remote-tracking ref, and a clone has no
  stash. Worktrees go through `git worktree remove` without `--force`, so
  git's own refusals (submodules, locks) stand; clones are removed outright.
  A worktree whose parent repository is gone (orphaned) and a `.git`
  directory without a HEAD (broken) are counted and listed, never removed.
  An unreadable process table removes nothing. The `reclaim_pass` event's
  detail and fields carry what was removed, what was kept by reason, and the
  orphaned and broken counts.

  `worktree_root_checkouts = true` applies the same rules to `worktrees_root`
  (enabled on m3 only, where that root is agent territory; on m1 and m5 it is
  the directory beside the primary checkouts). Three more gates apply to every
  root: a live `.pulp-build-active` marker (in use), a clone other worktrees
  point into (`has_worktrees`) and a branch whose Pulp lineage is `active`
  (`lineage_active`). Git is asked with `--no-optional-locks`, so checking a
  checkout never refreshes its index and makes it look recently used.
  Keep-verdicts are cached in `~/.tartci/state/reclaim/checkout-verdicts.json`
  while the checkout's newest mtime is unchanged, for at most a day. The
  event's `by_root` gives each root's counts.

  Exit 3 means the pass ran and a scanned volume is still below the
  `--fail-below-gb` floor. `pool status` and `doctor fleet`
  (`reclaim_low_space`) name the volume and its free space instead of calling
  the pass failed. Exit 5 means the lease volume is fine but the boot data
  volume, which no lease floor covers (m3), is still below
  `--boot-floor-gb` (30); status says `boot_low` and the doctor
  `reclaim_boot_low`. Look in `/private/tmp` and the per-user temp dir: the
  pass's `scratch_dirs` field shows what the scratch reaper removed and why it
  kept the rest.

  With `gate_ccache_trim = true` (and optionally `gate_ccache_max_age_days`,
  default 14, and `gate_ccache_trim_interval_hours`, default 24) the pass runs
  `ccache -d $TARTCI_CI_CACHE/ccache --evict-older-than <N>d` on the gate ccache
  (the cache the runners mount; without the variable, the profile's
  `[host].cache_root`),
  at most once per interval and only while no Tart VM runs or holds a VM lease
  and the pre-boot guard's lock is free (`scripts/gate_ccache_trim.py`). ccache
  recounts its files and size counters during the eviction, which the gate
  cache needs: its counters drift about 100x low, so ccache's own cleanup never
  starts. The event's `gate_ccache_trim` field shows the entries and bytes on
  disk before and after. Never run a bare `ccache -c` on that cache from the host: the host's
  `ccache` has no `ccache.conf` there and treats its 5 GiB default as the cap,
  not the guests' 40G; once the counters are recounted that evicts by size.

  The same origin/main checkout carries Pulp's host-vitals sensor. Its
  installer copies `host_vitals.sh` and `host_vitals_sensor.sh` into
  `~/.local/bin`, so a change to them never reached a host: on 2026-09-29 m1,
  m3 and m5 ran the 09-25 copy without the fseventsd reading, and m5studio had
  none. Each pass compares the installed copies with the checkout by SHA-256
  and, in fix mode, re-runs Pulp's `install_host_vitals_sensor.sh` from it when
  they differ or when the sensor is missing (`scripts/host_vitals_sensor.py`).
  This runs even while the worktree root is missing. `pool status` prints
  `host-vitals sensor: DRIFT` or `NOT INSTALLED` until it matches. The pass
  records the outcome in the receipt as `pulp_reapers.host_vitals_sensor`.

  Every pass writes `~/.tartci/state/reclaim/last-run.json` and appends a
  `reclaim_pass` event (and one `pulp_reaper` event per reaper run, with free
  space before and after) to `~/.tartci/state/reclaim/events.jsonl`. `tartci
  pool status`, `tartci status` and `tartci doctor fleet` report that
  receipt's age and result, so a reclaim agent that stopped running reads as
  STALE rather than "installed and loaded"; `doctor fleet` also flags any
  loaded tartci LaunchAgent registered from a plist outside
  `~/Library/LaunchAgents` (a leaked test registration shadowing the real one).

  **The home volume has its own floor.** The disk axis above judges the
  volume holding the Tart store. When the store is on another volume
  (m5studio's `/Volumes/Atelier`), a VM lease also judges the home volume,
  which holds every supervisor's temp files and the build trees
  (`scripts/home_volume_floor.py`). m5studio, 2026-10-04: the boot Data volume
  reached 99% with coverage build dirs, ENOSPC killed a merge-group runner and
  every lane supervisor, and leases kept being granted. The floor is computed
  per host: `clamp(max(30 GiB, fill_rate x hours to the next reclaim pass),
  30 GiB, 20% of the volume)`, with the fill rate measured over at least 6 h of
  admission samples so a transient spike cannot inflate it. The profile's
  `[host] home_volume_floor_mode` decides what happens below it, and every
  shipped profile is `report`: the lease is admitted and the supervisor logs
  `home_volume_would_refuse volume=home free=... floor=...`. In `refuse` mode a
  NEW clone is denied instead (`lease_denied axis=disk
  reason=home_volume_below_floor volume=home free=... floor=...`). Flip a host
  to `refuse` only after a day of report data shows no would-refuse event that
  was not a genuinely full volume, and with Daniel's OK. Either way, running
  jobs and supervisors are never touched. An unreadable volume admits the lease and logs `disk_axis_unread`
  on every such admission. `tartci doctor fleet` reports `disk_floor_refusing`
  when the refusals run as long as the host's lane count (a floor that refuses
  everything looks exactly like a full disk; would-refusals count in report
  mode), and `disk_axis_unread` when the
  volume has been unreadable for a reclaim cadence. State:
  `~/.tartci/state/leases/home-volume.json`. `TARTCI_HOME_VOLUME_FLOOR=0` turns
  it off for a lane; `TARTCI_HOME_VOLUME_FLOOR_HOURS` (default 1, the reclaim
  agent's interval) sets the pass horizon.

  Defaults retain `TARTCI_VM_DISK_FREE_FLOOR_GB=25` after all reservations and
  charge `TARTCI_VM_DISK_GROWTH_GB=24` per VM. The 24 GiB value deliberately
  exceeds the approximately 19 GiB store growth observed during a Pulp full
  gate. Override only from measured evidence, globally or with
  `TARTCI_{MACOS,LINUX,WIN}_VM_DISK_GROWTH_GB`. Zero or `false`/`off`/`no`
  disables growth charging or the floor respectively; this is the rollback,
  but it restores the old concurrent-admission race and should be temporary.
  Existing core/memory leases remain readable. A live legacy **VM** lease has
  unknown disk growth, so new VM admission fails closed until that VM finishes
  and its supervisor is restarted on the upgraded tartci snapshot; native build
  leases remain backward-compatible. During a rolling upgrade, first drain VMs
  owned by the old supervisor snapshot, then restart that provider on the new
  snapshot and verify its lease store before admitting another VM. Do not
  restart every provider together or edit a live lease JSON record to bypass the
  mixed-version denial: the denial is the compatibility fence that prevents an
  old unaccounted VM from sharing a supposedly reserved volume.
  Normal exit and signal cleanup release the unified lease; dead-owner reaping
  releases its disk reservation after a crash or reboot, but an exact live
  Tart/QEMU guardian keeps the lease after its supervisor dies. Storage roots
  must already exist; admission never creates a missing configured root. Paths
  under `/Volumes/<name>` are pinned to that mount automatically. Other hosts
  can persist an equivalent check with
  `TARTCI_VM_DISK_EXPECTED_{DEVICE_ID,MOUNT_PATH}` or the provider-specific
  `TARTCI_{MACOS,LINUX,WIN}_VM_DISK_EXPECTED_{DEVICE_ID,MOUNT_PATH}` overrides.
  The recorded `st_dev` device ID is an identity for the current boot: it joins
  path aliases and detects a changed filesystem while leases are live, but it
  is not guaranteed stable across reboot or device remapping. Prefer the
  expected mount path as the durable external-volume assertion. Configure an
  expected device ID only on hosts where it is stable, or refresh that value as
  part of the host boot check before providers are enabled.
  Provider cache, log, and Windows work leaves are the cold-start exception:
  the supervisor creates a missing leaf relative to an already-open authority
  parent, verifies it stays on that parent's device, and refuses a
  `/Volumes/<name>/...` path unless `<name>` is an actual mounted filesystem.
  Home-backed defaults use the existing home directory as authority; ephemeral
  defaults use an already-existing `TMPDIR` or `/tmp`. A missing temporary root
  is a failed reboot/session prerequisite and is never recreated from `/`.
- **Role profiles** (`scripts/host_profile.py`) — each host derives a role from
  its cores + `hw.model` — **dedicated-builder**, **dev-overflow**, or
  **light** — each carrying a core budget *and* a memory budget. `tartci
  host-profile` emits the derived budget (`PULP_BUILD_JOBS`,
  `PULP_BUILD_MEM_BUDGET_MB`) that a consumer's build path reads; `tartci status`
  shows the resolved role + capacity. Onboarding persists the role and verifies
  the host is governed — see [Onboarding a new host](#onboarding-a-new-host).

### Warm pre-booted gate VM (opt-in, off by default)

A host with RAM and cores to spare can keep one gate VM booted and parked under
a memory-only lease (`leases.py acquire --memory-only`), upgraded in place to
its core lease at hand-off (`leases.py resize`). It yields to other lanes,
expires, and is reported by `tartci pool status` and `tartci doctor`. No host
enables it. Design, events, and the runbook for trying it on a new high-RAM
host (prerequisites, exact profile lines, idle-cost and minutes-saved
measurement, turning it off): [warm-vm.md](warm-vm.md).

## Fleet scheduling boundary

GitHub Actions is the only fleet scheduler. Shipyard supervises queue ordering,
merge enrollment, and wedge detection; Tart CI owns per-host disposable VM
capacity and lease governance. Orchard is not used, even if its binary or old
shadow configuration remains installed on a host. During an upgrade, run
`scripts/disable_orchard.sh` to preview the exact two retired labels, then
`scripts/disable_orchard.sh --apply` to boot them out, remove their installed
user plists, and verify they are absent. Do not start its controller or workers
and do not route any profile lane through it.

### Boot decisions: per-job claims and lease fit (macOS)

Two local checks run before a macOS lane spends a queue scan, a Shipyard
admission call, a lease or a clone. Both fail open: if either cannot answer,
the lane does exactly what it did before they existed.

**Lease fit** (`providers/tart-macos/lease-fit.lib.sh`, `scripts/lease_fit.py`).
Each poll, before scanning the queue, the lane asks the lease store, read-only
and with the same capacity model `leases.py acquire` uses, whether its VM lease
could be granted.
- *Not now* (another VM or agent build holds the cores): the lane waits a poll
  with heartbeat `lease-wait`. It does not scan the queue or ask Shipyard.
- *Never* (the VM is larger than the budget it is admitted against): the lane
  stops polling, logs a `CONFIGURATION` line, and reports heartbeat
  `lease-never-fits` and event `lease_never_fits`.
- Each lane writes its verdict to `$TARTCI_STATE_DIR/<runner>.lease-fit.json`,
  where `<runner>` is the name the supervisor derives from the plist (prefix +
  slot, `scripts/macos_runner_identity.py`); fleet plists rarely set
  `TARTCI_RUNNER_NAME`. `tartci doctor fleet` (`lease_fit` check) and
  `tartci pool status` (text line and `lease_fit` JSON key) read it. They
  report a lane that can never lease (`lane_lease_never_fits`) and more
  identical lanes (same VM size, budget, repository and runner labels) than
  the budget runs at once (`lanes_exceed_lease_capacity`), for example two
  12-core gate lanes in m5's 14-core universe. The `pool status` line also
  counts each lane's current verdict and its last 24 h of `lease_unfit_now` /
  `lease_fit_restored` transitions from the lane's event log, e.g.
  `lease fit: ok (6 lanes: 5 fits now, 1 not now; last 24h: 404 not-now waits, 399 restored)`.
- `TARTCI_LEASE_FIT_GATE=0` disables the check.

**Per-job claim** (`providers/tart-macos/job-claim.lib.sh`, `scripts/job_claim.py`).
Before cloning, a lane claims one queued job of its selected class (repo +
runner labels) in a host-wide store (`TARTCI_JOB_CLAIM_DIR`, default
`~/.tartci/state/job-claims`). The claim is granted only while the queued count
is larger than the claims already standing against that class. Two kinds of
claim stand:
- live claims of other lanes on this host;
- online, idle runners anywhere in the fleet whose labels cover the class, that
  is, lanes that have already minted and are waiting for GitHub to assign them
  a job. One runner listing per boot attempt; `TARTCI_JOB_CLAIM_FLEET=0` makes
  claims host-local.

If every queued job is covered, the lane does not boot (event
`job_claim_contended`, heartbeat `job-claim-covered`). Such a pass does not
count toward the serving-blocked streak.

An event-class V2 count is only "at least one", so the lane buys an exhaustive
count only when a sibling already holds a claim.

A claim is released when the runner is assigned its job, when `run_one`
returns, and in cleanup. A crashed supervisor's claim dies with it (pid +
start time) or at `TARTCI_JOB_CLAIM_TTL_SECS` (default 1800).

A lane on another host that is still booting and has not minted yet is not
visible to any state tartci publishes. That race remains, and the pre-mint
recheck still resolves it. `TARTCI_JOB_CLAIM=0` disables claims.

## Onboarding a new host

`tartci setup` is the one command to bring a fresh Mac into the pool. Beyond
installing prereqs + creating stores, it now:

1. **Persists the role** — writes `~/.config/tartci/role` from the role
   `host_profile.py` derives (cores + `hw.model`), unless an explicit role file
   already exists (operator intent wins; a re-image/rename can't silently
   re-classify the host).
2. **Runs a verification gate** — confirms `host-profile` advertises a build
   budget (`PULP_BUILD_JOBS`) and the lease store answers. If either fails,
   `tartci setup` reports the host is not fully onboarded instead of exiting
   clean, so a half-provisioned host is visible.
3. **Installs the host agents**: the disk reclaimer, the artifact-cache
   refresher, the keychain unlocker, and the schedule backstop where the fleet
   profile says `schedule_backstop = "live"` or `"dry-run"` (only m3 is live;
   see `launchd/README.md`, "Schedule backstop"). Each installer is idempotent
   and non-fatal.

After `tartci setup`, deploy the clean receipt-bound support generation through
`tartci fleet-macos install` as described above and — for a CI host — register
runners. Helpers: `providers/common/onboard.lib.sh`.

## Drain or opt a host out of the CI pool (`tartci pool`)

`tartci pool {on|drain|off|status}` is the host-level participation switch — "this
machine, not now". It is deliberately decoupled from the per-lane GUI toggles
and from any placement engine, so opting a Mac out can't silently vanish when
lanes change.

- `tartci pool drain` — atomically persist native participation `0`, then
  `pool-state=draining`, before process changes. Every tartci provider requires
  both records to be open, so the first write closes native and JIT admission.
  Every tartci provider also checks this
  at its idle loop and again immediately before JIT minting. An assigned JIT
  job keeps its exact lease and finishes; an unregistered VM is discarded.
  The host-global transition lock covers only that final admission check, JIT
  mint, and successful listener spawn. Once a live listener is owned by its
  provider state, VM lease, and cleanup trap, the global lock is released so an
  idle secondary repository listener cannot block unrelated JIT minting. Drain
  disables restart but does not terminate that in-flight listener, including
  the interval after GitHub accepts a job and before `Runner.Worker` appears.
  Runner LaunchAgents are disabled so reboot cannot re-admit work. A detached
  local watcher retires a persistent `actions.runner.*` service only after
  an authoritative Shipyard integration has removed the host from routing,
  confirmed every such runner idle, and atomically published `held-idle` at
  `~/.config/tartci/persistent-runner-admission-hold`. This includes host
  preamble runners that do not match the tart-runner naming family. The current
  supported Shipyard CLI does not include that integration or any command that
  produces this receipt. Do not write the file manually: without an
  authoritative producer, `pool drain` deliberately leaves the durable
  provider/native gates closed, exits 3, and reports drain pending. Local worker
  absence is not accepted because it misses the
  accepted-job-before-worker-spawn race.
  The state survives terminal disconnect and reboot.
- `tartci pool off` — write `~/.config/tartci/native-build-participation=0`
  and `pool-state=off`, disable restart, and immediately boot out every runner
  agent. It deliberately bypasses a provider's cooperative JIT-start lock, so
  it can terminate active work; use drain for normal roaming. Because of that,
  `off` first probes every owned lane (the same per-label probe as `tartci
  launchd reload`: its launchd pid owns a `tart run` VM or `Runner.Worker`) and
  refuses with exit 12, before writing anything, when one is mid-job or its
  state cannot be read. The message names the lane and process. `tartci pool
  off --now` is the emergency stop that kills that work anyway.
- `tartci pool <on|off|drain> --plan` — print the transition and write nothing
  (no participation/state record, no lock, no launchctl mutation, no drain
  watcher): the state change, the owned services it would stop or start, the
  unowned ones it leaves alone, the capacity-floor verdict, and which lanes are
  mid-job right now (would be KILLED by `off`, would finish under `drain`).
  For `off`/`drain` it runs every precondition the real transition runs and
  exits with the code it would refuse with (11 capacity floor, 12 mid-job
  `off`), else 0. `on --plan` is narrower: it checks only the installed
  receipt (exit 7). The launch-helper probe (9), the network-profile reconcile
  (6) and the loaded-generation verification cannot run without acting, so they
  run only on the real `pool on`, and a `0` from `on --plan` does not promise
  that `on` succeeds.
  **`--plan` is not free:** for `off`/`drain` it takes the same dual-scope
  runner census the real transition takes (two paginated GitHub API calls per
  protected repository). Do not run it in a tight loop; poll no faster than
  once a minute, and never on a host whose census CLI is anonymous (see below).
- **Capacity-floor refusals come in two kinds, and only one is overridable.**
  `last serving host` (the floor's exit 3) means the census answered and no
  other host serves the label: none has an online runner carrying it, and no
  host declaring it in the published supply proved, through its own `pool
  status --json` over SSH, that it is on, participating, fleet-ready, free of
  fleet problems (a stale heartbeat is one), with every supervisor running and
  supply matching. The refusal lists each declaring peer and why it was not
  counted; `--allow-last-serving-host` takes it to zero
  deliberately. `capacity unknown` (exit 4) means the census could not answer;
  `--allow-last-serving-host` does NOT override it, and the refusal names the
  cause. `census_unauthenticated` (e.g. `API rate limit exceeded for <ip>`):
  the census CLI reached GitHub anonymously and spent the per-IP 60/hour
  allowance every host behind that IP shares; run with `TARTCI_GH_CLI=ghapp`
  (the default whenever `ghapp` is on PATH or in `~/.local/bin`) or repair its
  login. `census_identity_lacks_access` (`Resource not accessible by
  integration`): the App token was minted for the wrong installation. The
  census now binds every call to the queried repository through
  `SHIPYARD_GHAPP_REPO`/`GH_REPO` (ghapp reads those, not the checkout it runs
  from), so this should not recur; if it does, check which installation ghapp
  minted for before touching GitHub App permissions. `tartci doctor fleet`
  reports the census CLI's identity as `census_identity[<repo>]` (60/hour core
  limit = anonymous).
- `tartci pool on` — persist `pool-state=on`, participation=1, re-enable and
  bootstrap the installed runner agents. On a receipt-managed macOS fleet, both
  dynamic controllers and persistent `actions.runner.*` services must be named
  and digest-bound by the exact verified profile receipt; arbitrary persistent
  plists and unreceipted legacy Tart controllers remain stopped. This is the only transition that
  reopens provider admission, so a reconnect cannot leave a half-on host.
- `tartci pool status [--json]` — durable state + participation + each runner agent's
  loaded/stopped state. It also reports the host-global transition lock as
  `absent`, `owned`, `orphaned`, `invalid`, or `unobservable`, with typed owner
  and filesystem identity evidence in JSON. `tartci doctor` includes the same
  classification in its human-readable output. Receipt-managed macOS fleets additionally report
  `fleet_ready`, `expected_supervisors`, `verified_running_supervisors`, and
  structured problems. Readiness requires the exact receipted launchd snapshot,
  running supervisors, fresh PID/start-bound provider heartbeats, no retired or
  unexpected managed service, and no live supervisor from an older installed
  generation. Supervisor counts are control-plane health, not the host's two-VM
  physical capacity. Use `tartci pool status --require-ready` for a nonzero gate;
  ordinary status remains observational.
- **`serving` is a separate verdict from `fleet_ready`, and both are printed.**
  A supervisor can be receipted, loaded, running and freshly heartbeating while
  serving nothing at all: a lane that takes queued work and fails before a job
  is assigned looks identical to a healthy idle one from every liveness signal.
  `pool status` therefore prints a `serving:` line reading `ok`, `BLOCKED` or
  `unknown`, and the JSON carries a `serving` object with the blocked lanes,
  each lane's serve-less streak, and the phase it last reached.
  A lane is reported blocked only when both gates trip: a streak of consecutive
  work entries that served nothing (`--blocked-serving-streak`, default 6) and
  elapsed time since the streak began (`--blocked-serving-seconds`, default
  5400). The streak is the shape test, so failures interleaved with served jobs
  never accumulate; the elapsed time is the transience test, so an upstream
  blip cannot raise a fleet-wide alarm. A lane with no queued demand clears its
  streak on every idle pass, so zero VMs at rest never reads as blocked.
  A blocked lane deliberately does NOT clear `fleet_ready` and does not
  decrement the verified supervisor count. `fleet_ready` is a host-local,
  host-fixable question, and the dominant cause of a blocked lane is upstream
  and hits every lane on every host at once; gating the fleet on a condition it
  cannot fix would turn a serving outage into a control-plane outage. Use
  `tartci pool status --require-serving` (exit 9) when you want a nonzero gate
  on service specifically.
- `tartci host-profile --delivery [--json]` reports how code actually reaches
  each lane on this host, read off the live plist. Reports the delivery mechanism
  (`generation` or `sealed-bundle`), the commit in force inside the artifact
  the plist really execs, whether that is stale relative to this checkout, and
  whether `fleet-macos install --apply` updates the lane at all. On a sealed
  host that command stages a generation the launcher never execs, so it is a
  silent no-op there; the report says so per lane rather than leaving it to be
  inferred from a hostname.
- `tartci pool repair-lock` — recover a transition lock orphaned by power loss,
  reboot, or SIGKILL. It refuses unless admission is already closed (`off` or
  `draining`, participation `0`) and the recorded owner PID is dead. If an
  orphan blocks rejoin, run `pool off`, then `pool repair-lock`, then `pool on`.
  `pool off` there refuses with exit 12 while an owned lane is mid-job (or its
  busy state is unreadable): wait for the job, or use `pool drain`, and re-run
  it. Reach for `pool off --now` only when killing that job is intended;
  `pool off --plan` names the lane and process first.
  Providers check for an already-present transition lock before allocating a
  port or VM lease and before cloning or booting. The existing serialized check
  immediately before JIT mint remains authoritative for a lock created after
  that early observation; the early check only suppresses known-doomed
  boot/discard churn and never reclaims a lock automatically.

Drain is deliberately not a second scheduler. New jobs keep their existing
shared GitHub labels, so GitHub may assign them to another eligible host (for
example M1/M3 after M5 drains). If connectivity disappears after assignment,
do not manufacture a duplicate while ownership is ambiguous: let the GitHub
job reach a terminal lost-runner result and let Shipyard reconcile its exact
receipt before retry/reassignment. On rejoin, `tartci pool status` must still
say `draining`; run `tartci doctor --reap --json` to classify stale registrations
and local ownership, verify no active lease/guest remains, and only then run
`tartci pool on`. An `offline_busy_unconfirmed_local_state` or
`offline_busy_orphaned_no_local_owner` result is a reconciliation hold, not
permission for tartci to guess or delete live/ambiguous state.

### Idle-only maintenance for very large reused checkouts

Shipyard should decide *when* a host is eligible; tartci only supplies the
provider-side safety boundary. Treat `git count-objects -vH` pack count >=64 or
`size-pack` >=20 GiB as a maintenance candidate, and >=128 packs or >=50 GiB as
urgent. These are scheduling thresholds, never permission to delete.

Maintenance may run only while the pool is `draining` or `off`, the lease store
has no active leases, no `Runner.Worker`, Tart/QEMU guest, Git lock file, or
checkout process exists, and the workspace identity is unchanged between the
first probe and execution. Ambiguous or dirty workspaces are retained and
reported. Prefer bounded `git maintenance run --task=incremental-repack` for
pack consolidation; do not run full `git gc`, prune objects, or delete a reused
workspace automatically. Recheck free space before and after, because repacking
temporarily needs additional disk.

For future jobs, avoid creating the problem: use shallow fetch for ordinary CI
(`fetch-depth: 1`, no tags) or a blob-filtered checkout when history is needed.
A request for full history in a repository already measuring tens of GiB should
be an explicit workflow exception, not the remote-runner default.

## Non-gate VM lanes are clamped to the non-gate core budget

A VM lane that runs at **non-gate** priority (linux, macOS-release) can never
lease more than the host's non-gate budget (`lease_capacity_cores -
reserved_gate_cores`); `leases.py` denies a larger request. On a host that is
both `dedicated-builder` **and** the gate host (e.g. m3: budget 26, reserved
gate 14 → 12-core non-gate budget), the role's `vm_pool_cores` (14) *exceeds*
that budget, so a naive linux lane would be un-leasable.

`tartci_acquire_vm_lease` therefore **clamps** a non-gate lane's cores to
`non_gate_capacity_cores`. This makes any over-sized `vm_pool_cores` (or a
hand-set `TARTCI_LINUX_VM_CORES` override) safe by construction — no per-host
override is load-bearing for safety, and no VM lane can encroach on the gate
reserve. The gate lane runs at gate priority and is **not** clamped.

Consequence for the m3 `TARTCI_LINUX_VM_CORES=6` override: it is now a *fairness*
knob (6 leaves room for the macOS-release lane + native builds within the 12-core
non-gate budget), **not** a redundant safety patch. Do **not** "pin
`TARTCI_ROLE=dev-overflow`" to shrink it — that would make the linux runner
acquire with dev-overflow's `reserved_gate_cores=0` and let it eat the gate
reserve. Keep the override (or remove it for a linux lane sized to the full
12-core budget); either way the clamp keeps the gate safe.
## M3 disk-denial worktree recovery

The private M3 profile may declare the strict `merged-main-v1` worktree-cleanup
provider. It runs only after the M3 Tart-store preflight or current lease
attempt reports an exact disk-only capacity denial. The checked-in default is
`apply = false`; changing
it requires integration review and a new signed fleet cohort. CPU, RAM, probe,
malformed, and persisted/stale denials never trigger it.

The provider takes one nonblocking lock, fetches the exact current `origin/main`
from the literal canonical HTTPS remote under isolated Git configuration, and
requires a complete fail-closed system `lsof` observation. It retains primary,
detached, locked, dirty, active,
unmerged, ambiguous, or branch-mismatched worktrees. Apply mode durably
checkpoints before and after every removal and stops as soon as measured free
space reaches the denial target. A removal uses non-forced
`git worktree remove`, retains the branch at the same HEAD, and is immediately
verified. Any ambiguity stops the batch; admission remains denied. The atomic
receipt under the disk-admission state directory records bounds, before/after
capacity, fetched main SHA, dispositions, removals, branch proofs, and whether
one exact admission retry is eligible.

## Cross-host build disagreement (probable cache poisoning)

`tartci doctor build-disagreement` flags a gate host whose Build step fails to
compile or link what another gate host builds green: the signature of a
poisoned shared compiler cache. Read-only, never schedules, default off. Rules,
detection floor, enablement and remedy: [build-disagreement.md](build-disagreement.md).
