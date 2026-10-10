#!/usr/bin/env bash
# Cold start of a Tart Linux golden, the way the lint lane boots it: lease, CoW
# clone, size from the host profile, boot, first SSH with the tartci lint key.
# N runs; prints one TSV row per run and a median/p90 summary. Proves the golden
# is unchanged (disk sha256 before == after). No network is exercised and no job
# runs, so this is a timing result only, never an egress result.
#
#   scripts/measure_linux_cold_start.sh <golden> [N]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GOLDEN="$1"; N="${2:-10}"
export TART_HOME="${TART_HOME:-$HOME/.tart}"
KEY="${TARTCI_VM_SSH_KEY:-$HOME/.config/tartci/keys/lint-vm_ed25519}"
USER_="${TARTCI_VM_USER:-admin}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o BatchMode=yes)
export TARTCI_ROOT="$ROOT"
# shellcheck source=providers/common/vm-lease.lib.sh
source "$ROOT/providers/common/vm-lease.lib.sh"
cores="$(tartci_vm_lease_cores tart-linux-lint)"; mem="$(tartci_vm_lease_mem_mb tart-linux-lint)"
disk="$TART_HOME/vms/$GOLDEN/disk.img"
before="$(shasum -a 256 "$disk" | awk '{print $1}')"
now(){ python3 -c 'import time;print(f"{time.time():.3f}")'; }
printf 'run\tlease_s\tclone_s\tboot_to_ssh_s\ttotal_s\tclone_growth_mb\n'
for i in $(seq 1 "$N"); do
  vm="coldstart-$$-$i"
  t0=$(now)
  tartci leases acquire --id "$vm" --cores "$cores" --mem-mb "$mem" --priority vm \
    --kind tart-linux-lint-measure --pid $$ --json >/dev/null
  t1=$(now)
  tart clone "$GOLDEN" "$vm"; tart set "$vm" --cpu "$cores" --memory "$mem"
  t2=$(now)
  tart run --no-graphics "$vm" >/dev/null 2>&1 &
  rp=$!
  ip=""; for _ in $(seq 1 300); do ip="$(tart ip "$vm" 2>/dev/null || true)"; [ -n "$ip" ] && break; sleep 0.2; done
  for _ in $(seq 1 300); do ssh -n "${SSH_OPTS[@]}" -i "$KEY" "$USER_@$ip" true 2>/dev/null && break; sleep 0.2; done
  t3=$(now)
  growth=$(du -sm "$TART_HOME/vms/$vm" | awk '{print $1}')
  tart stop "$vm" >/dev/null 2>&1 || true; wait "$rp" 2>/dev/null || true
  tart delete "$vm"
  tartci leases release --id "$vm" >/dev/null
  python3 -c "print(f'$i\t{$t1-$t0:.2f}\t{$t2-$t1:.2f}\t{$t3-$t2:.2f}\t{$t3-$t0:.2f}\t$growth')"
done | tee /dev/stderr | python3 -c '
import sys, statistics
rows=[l.split("\t") for l in sys.stdin if l[0].isdigit()]
tot=sorted(float(r[4]) for r in rows); boot=sorted(float(r[3]) for r in rows)
p90=lambda v: v[min(len(v)-1, int(round(0.9*(len(v)-1))))]
print(f"SUMMARY n={len(rows)} total_median={statistics.median(tot):.2f}s total_p90={p90(tot):.2f}s boot_median={statistics.median(boot):.2f}s boot_p90={p90(boot):.2f}s")
' 
after="$(shasum -a 256 "$disk" | awk '{print $1}')"
echo "GOLDEN $GOLDEN sha256_before=$before sha256_after=$after unchanged=$([ "$before" = "$after" ] && echo yes || echo NO)"
echo "LEASE cores=$cores mem_mb=$mem (from host profile)"
