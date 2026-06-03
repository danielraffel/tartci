#!/usr/bin/env bash
# Headless Win11-ARM64 install/boot under standalone QEMU (Homebrew) on Apple
# Silicon. Uses NVMe for the system disk (Win11-ARM has an inbox NVMe driver →
# sidesteps the AVF/virtio-blk wall). hvf acceleration. VNC for diagnosis;
# user-net with hostfwd 2222->22 for SSH once OpenSSH is up. TPM/SecureBoot are
# bypassed by the autounattend LabConfig, so no swtpm needed.
set -euo pipefail
TARTCI_WIN="${TARTCI_WIN:-$HOME/.tartci/windows}"
QDIR="$TARTCI_WIN/qemu"
ISO="${TARTCI_WIN_ISO:?Set TARTCI_WIN_ISO to your 512-padded Win11-24H2-ARM64 ISO}"
UA="$TARTCI_WIN/autounattend.iso"
VIRTIO="${TARTCI_VIRTIO:-$TARTCI_WIN/virtio-win.iso}"
VNC_DISP="${VNC_DISP:-10}"          # VNC on 127.0.0.1:$((5900+VNC_DISP))
SSH_FWD="${SSH_FWD:-2222}"
mkdir -p "$QDIR"

# Firmware: prefer brew's edk2, fall back to UTM's code fd.
FW=""
for c in /opt/homebrew/share/qemu/edk2-aarch64-code.fd \
         /Applications/UTM.app/Contents/Resources/qemu/edk2-aarch64-code.fd; do
  [ -f "$c" ] && FW="$c" && break
done
[ -n "$FW" ] || { echo "no edk2-aarch64-code.fd found"; exit 1; }
# Writable EFI vars store — MUST be seeded from the edk2 vars TEMPLATE, not
# zeros (a zeroed pflash has no NVRAM boot policy → edk2 drops to UEFI Shell
# instead of auto-booting removable install media). edk2-arm-vars.fd is the
# AAVMF vars template (used for aarch64 too).
VARS_TPL=""
for v in /opt/homebrew/share/qemu/edk2-aarch64-vars.fd \
         /opt/homebrew/share/qemu/edk2-arm-vars.fd; do
  [ -f "$v" ] && VARS_TPL="$v" && break
done
[ -n "$VARS_TPL" ] || { echo "no edk2 vars template found"; exit 1; }
if [ ! -f "$QDIR/efivars.fd" ]; then
  cp "$VARS_TPL" "$QDIR/efivars.fd"
fi
# System disk as qcow2 (attached via NVMe).
if [ ! -f "$QDIR/win.qcow2" ]; then
  qemu-img create -f qcow2 "$QDIR/win.qcow2" 90G >/dev/null
fi

exec qemu-system-aarch64 \
  -name pulp-windows \
  -accel hvf -machine virt,highmem=on -cpu host -smp 8 -m 8192 \
  -drive if=pflash,format=raw,readonly=on,file="$FW" \
  -drive if=pflash,format=raw,file="$QDIR/efivars.fd" \
  -device ramfb \
  -device qemu-xhci,id=usb -device usb-kbd -device usb-tablet \
  -netdev user,id=net0,hostfwd=tcp::${SSH_FWD}-:22 \
  -device virtio-net-pci,netdev=net0 \
  -device virtio-scsi-pci,id=scsi \
  -drive file="$QDIR/win.qcow2",if=none,id=nvm,format=qcow2 \
  -device nvme,drive=nvm,serial=pulpwin \
  -drive file="$ISO",if=none,id=cd0,media=cdrom,readonly=on \
  -device usb-storage,drive=cd0,bootindex=0 \
  -drive file="$UA",if=none,id=cd1,media=cdrom,readonly=on \
  -device usb-storage,drive=cd1 \
  -drive file="$VIRTIO",if=none,id=cd2,media=cdrom,readonly=on \
  -device usb-storage,drive=cd2 \
  -display none -vnc 127.0.0.1:${VNC_DISP}
