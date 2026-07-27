#!/usr/bin/env bash
# Bind the Linux guest's canonical ccache directory to the Tart host share.
set -euo pipefail

host_cache="${1:-/mnt/host/ccache}"
cache_link="${2:-$HOME/.ccache}"
mount_root="$(dirname "$host_cache")"
mount_info="${TARTCI_CCACHE_MOUNT_INFO:-}"

if [ -z "$mount_info" ]; then
  mount_info="$(findmnt -n -o FSTYPE,SOURCE --target "$mount_root" 2>/dev/null)" \
    || mount_info=""
fi
case "$mount_info" in
  "virtiofs com.apple.virtio-fs.automount") ;;
  *)
    printf 'TARTCI_DIAG ccache_binding=wrong_mount mount_root=%s mount_info=%s\n' \
      "$mount_root" "${mount_info:-<missing>}" >&2
    exit 70
    ;;
esac

if [ ! -d "$host_cache" ] || [ ! -w "$host_cache" ]; then
  printf 'TARTCI_DIAG ccache_binding=unusable host_cache=%s\n' "$host_cache" >&2
  exit 70
fi

# A golden may already contain a real ~/.ccache directory. `ln -sfn TARGET DIR`
# nests a link inside that directory rather than replacing it, silently leaving
# ccache on the ephemeral disk. Replace this one exact cache path, then verify
# its physical resolution before the JIT runner is registered.
if [ -e "$cache_link" ] || [ -L "$cache_link" ]; then
  rm -rf -- "$cache_link"
fi
ln -s -- "$host_cache" "$cache_link"

expected="$(cd "$host_cache" && pwd -P)"
resolved="$(cd "$cache_link" && pwd -P)"
if [ ! -L "$cache_link" ] || [ "$resolved" != "$expected" ]; then
  printf 'TARTCI_DIAG ccache_binding=mismatch link=%s resolved=%s expected=%s\n' \
    "$cache_link" "$resolved" "$expected" >&2
  exit 70
fi

printf 'TARTCI_DIAG ccache_binding=host link=%s resolved=%s\n' \
  "$cache_link" "$resolved"
