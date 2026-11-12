# Syncing goldens across hosts

Goldens are large (a Windows golden is ~26 GB) and are **not** in git — each host
holds its own copy under `$TARTCI_GOLDENS`. When you re-bake or patch a golden on
one host, the others drift until you copy the new file over and re-point their
runners. This page is the recipe today, and the design for making it one command.

> Project-agnostic. The worked example below uses the maintainer's 3-Mac pool
> (a Mac Studio + two MacBooks), but nothing here is Pulp-specific — any tartci
> host pool syncs goldens the same way.

## The model

- **One canonical golden per OS**, newest wins (e.g.
  `pulp-windows-build-24h2-arm64-<date>-cacheopt.qcow2`). It usually lives first
  on the host that baked it (the "primary" — the Mac Studio in our pool).
- Each host reads goldens from `$TARTCI_GOLDENS` (set in the runner's launchd
  plist). The Windows provider's `runner.sh` defaults `GOLDEN` to
  `$TARTCI_GOLDENS/<canonical-name>`, so a host with **no** `TARTCI_WIN_GOLDEN`
  pin auto-uses the canonical file *once it's present*. A host with an explicit
  `TARTCI_WIN_GOLDEN` pin must be re-pointed (or the pin removed).
- The slow part is the byte transfer. **Thunderbolt** (a USB-C cable between two
  Macs → an auto-configured `bridge0` with `169.254.x` link-local IPs) moves
  ~26 GB at 350–450 MB/s (~60–75 s). LAN (gigabit) is ~10× slower; Tailscale
  relay is slower still. So: connect the two machines, transfer over the
  link-local, done.

## Per-host reference (maintainer's pool)

| Host | alias | `TARTCI_GOLDENS` | Volume note |
|------|-------|------------------|-------------|
| Mac Studio | `macstudio` | `/Users/<you>/VMs/goldens` | primary / baker; internal data vol, runs hot — prune with care |
| M5 laptop | (local) | `/Users/<you>/VMs/goldens` | dev + overflow |
| MacBook Pro (M1) | `m1` | `/Users/<you>/VMs/goldens` | travel; explicit `TARTCI_WIN_GOLDEN` pin (must re-point) |

All three use the same path today; confirm `$TARTCI_GOLDENS` per host rather than
assuming — a host on an external/remote volume would differ.

## Manual recipe (works today)

1. **Connect the two Macs** with a USB-C/Thunderbolt cable. A `bridge0` comes up
   with a `169.254.x` address on each end. Find the peer's TB IP (each host's own
   is `ipconfig getifaddr bridge0`; the peer's is the other `169.254.x` on the
   same `/16` — read it off the peer, or `arp -a`/`ndp -a` on the bridge).
2. **Confirm the canonical golden** on the source (newest `*-cacheopt` wins;
   don't hardcode a date):
   `ls -lht $TARTCI_GOLDENS/pulp-windows-build-*.qcow2 | head`
   Ensure a matching `.qcow2.sha256` exists (`shasum -a 256 <f> > <f>.sha256`).
3. **Transfer over the link-local** (openrsync ships with macOS; use
   `--partial --progress`, NOT `--info=progress2` which it lacks):
   ```bash
   rsync -a --partial --progress \
     -e "ssh -i ~/.ssh/<key> -o HostKeyAlias=<peer-alias> -o StrictHostKeyChecking=yes" \
     $TARTCI_GOLDENS/<golden>.qcow2 $TARTCI_GOLDENS/<golden>.qcow2.sha256 \
     <user>@<PEER_TB_IP>:$TARTCI_GOLDENS/
   ```
4. **Verify** on the destination: `cd $TARTCI_GOLDENS && shasum -a 256 -c <golden>.qcow2.sha256` → must print `OK`.
5. **Re-point the runner** only if it has an explicit pin: set (or delete)
   `TARTCI_WIN_GOLDEN` in `~/Library/LaunchAgents/com.danielraffel.pulp.qemu-runner-windows.plist`
   (back up first). No pin → the provider default already points at the canonical name.
   If the golden ships its own UEFI variable store (its boot entry lives there
   rather than in `\EFI\BOOT\BOOTAA64.EFI`), copy that file with it and set
   both `TARTCI_WIN_VARS_TEMPLATE` (its path) and `TARTCI_WIN_VARS_SHA256` (its
   SHA-256). Each job copies the template and verifies the copy before QEMU
   writes to it; a mismatch fails the job before boot. One without the other
   is refused at startup.
6. **Reload the agent only when idle** (log tail shows `waiting … queued=0`, no
   Windows build running — reloading mid-build kills the job). If you changed
   `TARTCI_WIN_GOLDEN` (or any plist env var), you MUST do a **full
   bootout+bootstrap** — `launchctl kickstart -k` restarts the *process* but
   reuses the already-loaded `EnvironmentVariables`, so it comes back on the old
   (now-deleted) golden and dies with `golden not found`:
   ```bash
   L=com.danielraffel.pulp.qemu-runner-windows
   tartci launchd reload $L --dry-run   # REFUSE (exit 3) while a VM is running
   tartci launchd reload $L
   ```
   Use the tartci reloader, not raw `launchctl bootout`: it refuses while that
   lane's supervisor owns a `qemu-system-*`/`tart run` VM or a `Runner.Worker`.
   Confirm the next `LOOP` log line shows `golden=<canonical name>`.
7. **Prune older goldens** once verified — but never delete one a VM is booted
   from (`ps aux | grep qemu-system | grep <old golden>` must be empty).

### Gotchas

- macOS `rsync` is `openrsync` — `--info=progress2`/`--info=name0` are unsupported;
  use `--partial --progress`.
- ICMP `ping` is often filtered on these hosts (shows "unreachable") even though
  SSH works — don't use ping to judge reachability.
- `launchctl kickstart -k` does NOT re-read plist env — use bootout+bootstrap
  after changing `TARTCI_WIN_GOLDEN` (see step 6).
- Prune old goldens **after** the reload confirms the new one loads, not before —
  and never delete a golden a booted VM's overlay still backs onto.
- Do one host at a time so the pool's other Windows lanes stay up.

## Turnkey — `tartci goldens` (shipped)

The recipe above now collapses to: **connect the cable, run one command.**
Implemented in `scripts/goldens.sh`, wired into the dispatcher:

```
tartci goldens list                                    # canonical golden + drift / prune candidates
tartci goldens sync --to HOST   [opts]                 # PUSH this host's canonical golden → HOST
tartci goldens sync --from HOST [opts]                 # PULL HOST's canonical golden → this host (new-machine setup)
#   opts: [--os windows] [--prune] [--dry-run] [--no-reload] [--via IP]
```

**Seeding a new host** — the common case is you're *on* the fresh machine and a
peer already has the golden: deploy tartci, then `tartci goldens sync --from
<peer>`. `--to` is the mirror (push from a host that has it). If no host has the
golden yet, bake one from scratch (runbook §4), then the rest copy via `--from`.

`sync`:
1. **Discovers the fastest link** to HOST — probes a Thunderbolt link-local peer
   (`169.254.x` neighbours on `bridge0` only, that identify as HOST, reusing
   the ssh alias's user/key), falls back to the alias (LAN/Tailscale). `--via IP`
   overrides the TB IP if auto-detect misses it. Every raw-IP connection (probe,
   rsync, `--via`) uses `-o HostKeyAlias=<HOST's known_hosts name>
   -o StrictHostKeyChecking=yes`, so a peer that cannot present HOST's existing
   host key is refused; nothing is ever added to `known_hosts`.
2. **rsyncs** the canonical golden + `.sha256` into the target's *own*
   `$TARTCI_GOLDENS` (read from its runner plist, not assumed); resumable
   (`--partial`), openrsync-safe flags. `--dry-run` stops here.
3. **Verifies** the sha remotely; aborts that host on mismatch (never repoints a
   bad copy).
4. **Re-points** an explicit `TARTCI_WIN_GOLDEN` pin (backs up the plist first; a
   host with no pin uses the provider default) and **reloads the runner only when
   idle** — via a full bootout+bootstrap (`tartci launchd reload`), because a pin
   is plist env and `kickstart -k` does not re-read it. `--no-reload` to skip.
   If the reloader refuses (exit 3: lane mid-job or busy state unknown), fails,
   or tartci is not installed on the target, the lane is left running — there is
   no raw `launchctl bootout` fallback — and a repointed pin takes effect after
   `tartci launchd reload <label>` once the lane is idle.
5. **Prunes** superseded goldens on the target, guarded (never one a running VM
   is backed by), opt-in via `--prune`.

The only manual step is plugging in the cable. macOS/Linux Tart goldens (not
portable qcow2 files) are out of scope for this MVP — they sync via tart
export/clone.

## Moving the Pulp macOS golden to a new Xcode

The Xcode, the guest macOS and the base image come from Pulp's
`tools/toolchain/manifest.toml`; `manifests/pulp.macos.toml` carries them
(`xcode`, `xcode_build`, `macos`, `macos_build`, and `base` pinned by digest)
and is regenerated with Pulp's `tools/toolchain/generate.py --tartci-root`,
never hand-edited. The bake itself is still Pulp's operator-led
`tools/ci/tart-provision.sh` tier chain (`base`, `apple-xcode`, `pulp`,
`runner`, then the pinned mise layer from `.shipyard/vm-image.mise.toml`); each
tier refuses a guest whose macOS build or Xcode build is not the pin, and the
mise layer refuses the golden unless `check_env.py --require-tools` passes in
the guest. A new Xcode that needs a newer macOS means a full Tier 0 rebake, not
a Tier 1 swap.

Roll it one host at a time, and never drain two hosts at once:

1. Drain the host with the most headroom (`tartci pool drain`; the running
   jobs finish, nothing new starts). The bake boots a VM, and Apple's
   two-VM-per-host cap means a host running two gate VMs has no slot for it.
   Bake there; the bake does not touch `:latest`. Tag the result with a dated
   name, not `:latest`.
2. Canary, still drained: serve one job from the dated golden on a pilot
   label (`tartci serve macos --once --golden <dated> --labels <pilot labels>`)
   and dispatch Pulp's `Build and Test` with `mise_candidate=true` on `main`.
3. Promote on that host: point `pulp-build-runner:latest` at the dated golden,
   `tartci pool undrain`, and confirm the next gate job passes. A red canary
   means `tartci pool undrain` with `:latest` untouched.
4. Copy the stopped dated golden to the next host (`rsync -aHS` of its
   `vms/<name>` directory into that host's own `TART_HOME`), verify it boots
   with `xcodebuild -version` and `sw_vers -buildVersion` matching the pins,
   then repeat step 3 there. Keep the previous `:latest` as a dated rollback
   until every host has run a green gate job on the new one.

Origin: issue #24 — this reconciliation was the manual first run; the command
automates it.
