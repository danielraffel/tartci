#!/usr/bin/env bash
# make-golden.sh — tag a pristine, compressed Windows CI golden from a running
# QEMU VM. Codifies the proven 2026-06 procedure: clean-shutdown the guest over
# SSH → wait for QEMU to exit → `qemu-img convert -c` the powered-off disk →
# write a sha256 + a provenance sidecar. The golden stays generic/headless; CI
# and the UTM bench each clone it, neither boots it directly.
#
# Usage:   make-golden.sh [golden-name]
# Env:
#   TARTCI_WIN          dir holding the working VM disk   (default ~/.tartci/windows)
#   TARTCI_GOLDENS      dir to write the golden into      (default ~/.tartci/goldens)
#   TARTCI_WIN_DISK     working qcow2                      (default $TARTCI_WIN/qemu/win.qcow2)
#   TARTCI_WIN_SSH_KEY  private key for the guest         (default ~/.ssh/id_ed25519)
#   TARTCI_WIN_SSH_USER guest admin user                  (default admin)
#   TARTCI_WIN_SSH_PORT host-forwarded SSH port           (default 2222)
set -euo pipefail

TARTCI_WIN="${TARTCI_WIN:-$HOME/.tartci/windows}"
TARTCI_GOLDENS="${TARTCI_GOLDENS:-$HOME/.tartci/goldens}"
DISK="${TARTCI_WIN_DISK:-$TARTCI_WIN/qemu/win.qcow2}"
KEY="${TARTCI_WIN_SSH_KEY:-$HOME/.ssh/id_ed25519}"
USER="${TARTCI_WIN_SSH_USER:-admin}"
PORT="${TARTCI_WIN_SSH_PORT:-2222}"
NAME="${1:-pulp-windows-build-$(date +%Y-%m-%d)}"

[ -f "$DISK" ] || { echo "no working disk at $DISK"; exit 1; }
mkdir -p "$TARTCI_GOLDENS"
DST="$TARTCI_GOLDENS/$NAME.qcow2"
[ -e "$DST" ] && { echo "golden already exists: $DST (pick another name)"; exit 1; }

SSH=(ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -i "$KEY" -p "$PORT" "$USER@127.0.0.1")

echo "→ clean-shutdown the guest"
"${SSH[@]}" "shutdown /s /t 0 /f" 2>/dev/null || true

echo "→ wait for QEMU to power off"
qpid="$(pgrep -f 'qemu-system-aarch64 .*pulp-windows' | head -1 || true)"
if [ -n "$qpid" ]; then
  for _ in $(seq 1 60); do kill -0 "$qpid" 2>/dev/null || break; sleep 3; done
  kill -0 "$qpid" 2>/dev/null && { echo "QEMU still running after 180s — aborting (won't snapshot a live disk)"; exit 1; }
fi
echo "  QEMU stopped."

echo "→ compress-convert → $DST (this takes a few minutes)"
qemu-img convert -p -O qcow2 -c "$DISK" "$DST"
qemu-img check "$DST" | tail -2

echo "→ checksum + sidecar"
shasum -a 256 "$DST" | tee "$DST.sha256"
SHA="$(awk '{print $1}' "$DST.sha256")"
SIZE="$(qemu-img info "$DST" | awk -F': ' '/disk size/{print $2}')"
cat > "$TARTCI_GOLDENS/$NAME.md" <<EOF
# Golden: $NAME

Pristine Windows CI build golden (QEMU, ARM64), tagged $(date +%Y-%m-%d).

- **Format**: qcow2, zlib-compressed; on-disk $SIZE
- **sha256**: \`$SHA\`
- **Hygiene**: generic + headless — no live Tailscale identity, no baked private
  keys, no repo checkout. Per-run + GUI state lives in clones, not here.

## Use — never boot this directly
- CI: clone (CoW) per job → ephemeral, unique hostname + hostfwd port.
- bench: \`bench/bench.sh windows\` → persistent UTM clone (ramfb→virtio-gpu).

Recipe + gotchas: docs/runbook.md, docs/gotchas.md.
EOF
echo "✓ golden tagged: $DST"
