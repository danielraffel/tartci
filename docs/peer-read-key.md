# Peer-read key (forced command) — owner step, not enabled

Fleet hosts read each other over SSH with a key that opens a full shell. The
reads themselves are a handful of fixed command strings. `scripts/tartci_peer_read.py`
is a forced-command dispatcher that runs exactly those strings and refuses
everything else, so a key restricted to it can read peers and do nothing more.

**Status:** shipped as code and tests only. Nothing installs it, no
`authorized_keys` entry names it, and no client sends a verb yet. Turning it on
is an owner action (the steps below edit `~/.ssh` on fleet hosts, which agents
never do — see `docs/agents/never.md`).

## What it allows

sshd puts the command the client asked for in `SSH_ORIGINAL_COMMAND`. The
dispatcher accepts either the short verb or the byte-exact string a client in
this tree sends today, and runs the string through `/bin/sh -c`, so output is
identical to the full-shell key:

| verb | command (sent by) |
|---|---|
| `pool-status` | `cd ~ && ~/.local/bin/tartci pool status --json` (`fleet_self_update` peer probe, `capacity_floor`) |
| `self-update-marker` | `fleet_self_update._PEER_MARKER` (imported, same bytes) |
| `fleet-source-verified` | the canary read in `fleet_self_update.read_canary_verified` |
| `job-claim-status` | `cd ~ && ~/.local/bin/tartci job-claim status --publish` (`job_claim.gather_peers`) |

Anything else — an empty command, a shell, an allowed string with one byte
added or removed — exits **77** and writes one audit line (client address,
reason, length, sha256 prefix, first 64 characters) to stderr and to
`${TARTCI_HOME:-~/.tartci}/state/peer-read/audit.log` (rotated at 1 MiB).

Not on the list, so they keep needing the operator key: `peer_stall_alert`'s
`PEER_READ`, `tailnet_peer_alert`'s address read, `goldens sync`,
`tart-image-sync`, and any interactive `ssh <host>`.

## Enabling it (owner action, per host, additive)

Do this only after the fleet runs a generation that contains
`scripts/tartci_peer_read.py`. Every step adds; the existing key and aliases
keep working, so a mistake cannot lock a host out.

1. On the reading host, create the peer key (no passphrase; it is used
   unattended):
   ```bash
   ssh-keygen -t ed25519 -N '' -C "tartci-peer-read $(scutil --get LocalHostName)" \
     -f ~/.ssh/id_ed25519_tartci_peer
   ```
2. On each **peer**, install a stable entrypoint that runs the dispatcher from
   the installed, non-writable generation (`support.root` in
   `~/.config/tartci/macos-fleet-install.json`), so it follows self-update:
   ```bash
   cat > ~/.local/bin/tartci-peer-read <<'EOF'
   #!/bin/sh
   root="$(/usr/bin/python3 -c 'import json, os; print(json.load(open(os.path.expanduser("~/.config/tartci/macos-fleet-install.json")))["support"]["root"])')" || exit 77
   exec /usr/bin/python3 "$root/scripts/tartci_peer_read.py"
   EOF
   chmod 0755 ~/.local/bin/tartci-peer-read
   ```
   Check it before going on: `SSH_ORIGINAL_COMMAND=id ~/.local/bin/tartci-peer-read; echo $?`
   prints one `refused` line and `77`.
3. On each peer, **append** the reading host's public key to
   `~/.ssh/authorized_keys`:
   ```
   restrict,command="/Users/<you>/.local/bin/tartci-peer-read" ssh-ed25519 AAAA… tartci-peer-read <host>
   ```
   Tailscale and LAN connections share this file, so the `fleet-connect`
   fallback is unaffected.
4. On the reading host, add `Host tartci-peer-<id>` aliases that use the new
   key with the same `HostName`/`ProxyCommand` as the existing alias.
5. Prove it, from the reading host:
   ```bash
   ssh -i ~/.ssh/id_ed25519_tartci_peer <peer> pool-status | python3 -m json.tool   # JSON
   ssh -i ~/.ssh/id_ed25519_tartci_peer <peer> id; echo $?                          # 77
   ```
   and on the peer, `tail -1 ~/.tartci/state/peer-read/audit.log` shows the
   refused `id`.

Pointing the fleet's clients at the restricted aliases, and removing the
full-shell key from `authorized_keys`, are later steps (execution plan D4,
after a 48-hour soak of green peer reads).

## Rollback

Delete the `restrict,command=…` line from `authorized_keys` on the peer. The
entrypoint and key do nothing without it.
