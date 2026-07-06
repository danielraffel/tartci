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
| Mac Studio | `macstudio` | `/Users/danielraffel/VMs/goldens` | primary / baker; internal data vol, runs hot — prune with care |
| BlackBook (M5) | (local) | `/Users/danielraffel/VMs/goldens` | dev + overflow |
| MacBook Pro (M1) | `m1` | `/Users/danielraffel/VMs/goldens` | travel; explicit `TARTCI_WIN_GOLDEN` pin (must re-point) |

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
     -e "ssh -i ~/.ssh/<key> -o StrictHostKeyChecking=accept-new" \
     $TARTCI_GOLDENS/<golden>.qcow2 $TARTCI_GOLDENS/<golden>.qcow2.sha256 \
     danielraffel@<PEER_TB_IP>:$TARTCI_GOLDENS/
   ```
4. **Verify** on the destination: `cd $TARTCI_GOLDENS && shasum -a 256 -c <golden>.qcow2.sha256` → must print `OK`.
5. **Re-point the runner** only if it has an explicit pin: set (or delete)
   `TARTCI_WIN_GOLDEN` in `~/Library/LaunchAgents/com.danielraffel.pulp.qemu-runner-windows.plist`
   (back up first). No pin → the provider default already points at the canonical name.
6. **Reload the agent only when idle** (log tail shows `waiting … queued=0`, no
   Windows build running — reloading mid-build kills the job). If you changed
   `TARTCI_WIN_GOLDEN` (or any plist env var), you MUST do a **full
   bootout+bootstrap** — `launchctl kickstart -k` restarts the *process* but
   reuses the already-loaded `EnvironmentVariables`, so it comes back on the old
   (now-deleted) golden and dies with `golden not found`:
   ```bash
   L=com.danielraffel.pulp.qemu-runner-windows
   launchctl bootout gui/$(id -u)/$L 2>/dev/null; sleep 2
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$L.plist
   # (equivalently: `tartci launchd reload $L`)
   ```
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

## Turnkey goal — `tartci goldens sync`

The recipe above should collapse to: **connect the cable, run one command.**
Proposed subcommand (fits the existing `tartci` dispatcher):

```
tartci goldens list                 # canonical golden per OS + which hosts have it (drift report)
tartci goldens sync [--to HOST|--all] [--os windows] [--prune]
```

`sync` would:
1. **Discover the fastest link** to each target: probe Thunderbolt link-local
   first (detect the peer's `169.254.x` on `bridge0` via `ndp`/mDNS/known-host
   handshake), fall back to LAN, then Tailscale. Report which link it chose.
2. **rsync** the canonical golden(s) + `.sha256` into the target's
   `$TARTCI_GOLDENS` (resumable; `--partial`).
3. **Verify** the sha remotely; abort that host on mismatch.
4. **Re-point + reload** the runner only when idle (unset stale pins, confirm the
   `LOOP` line flips to the new golden).
5. **Prune** superseded goldens on the target (guarded: never one in use),
   opt-in via `--prune`.

The only step that can't be automated is plugging in the cable — so the UX is
"connect your machines, then `tartci goldens sync --to m1 --prune`." A later
enhancement could watch for a TB peer appearing and offer the sync automatically.

Tracked in issue #24 (this reconciliation was the manual first run of the above).
