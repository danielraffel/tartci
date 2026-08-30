#!/usr/bin/env bash
# Build a new immutable Pulp m153 Proxmox template from retained template 9005.
#
# This script is intentionally additive. It never rewrites or deletes a VM and
# leaves a failed candidate available for inspection. `qm template` is the last
# mutation and is unreachable until exact-source, deep provider receipts,
# executable capability probes, a warm build, and host-side receipt validation
# all pass.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$ROOT/manifests/pulp.linux.toml"
RENDER_VERIFIER="$ROOT/providers/common/pulp-render-generation.py"
QM_BIN="${TARTCI_QM_BIN:-qm}"
SSH_BIN="${TARTCI_SSH_BIN:-ssh}"
PARENT_VMID=9005
NEW_VMID=""
GUEST_HOST=""
GUEST_USER="${PULP_GOLDEN_GUEST_USER:-runner}"
SSH_KEY="${PULP_GOLDEN_SSH_KEY:-}"
NAME=""
RECEIPT_DIR="${PULP_GOLDEN_RECEIPT_DIR:-/var/lib/tartci/golden-receipts}"
LOCK_PATH="${PULP_GOLDEN_LOCK_PATH:-/run/lock/tartci-pulp-golden-refresh.lock}"
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10)

note(){ printf 'pulp-golden: %s\n' "$*" >&2; }
die(){ printf 'pulp-golden: ERROR: %s\n' "$*" >&2; exit 1; }

usage(){
  cat <<'USAGE'
Usage: bake-pulp-golden.sh --new-vmid VMID --guest-host HOST [options]

Required:
  --new-vmid VMID       unused VMID >= 9006; becomes the new template
  --guest-host HOST     SSH address of the newly cloned candidate

Options:
  --guest-user USER     candidate SSH user (default: runner)
  --ssh-key PATH        private key for candidate SSH
  --name NAME           new template name (default: pulp-linux-golden-m153-VMID)
  --receipt-dir PATH    host-side immutable receipt directory

Template 9005 is retained as the parent and rollback image. The script never
deletes any VM. A failed candidate remains a non-template for inspection.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --new-vmid) NEW_VMID="${2:-}"; shift 2 ;;
    --guest-host) GUEST_HOST="${2:-}"; shift 2 ;;
    --guest-user) GUEST_USER="${2:-}"; shift 2 ;;
    --ssh-key) SSH_KEY="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --receipt-dir) RECEIPT_DIR="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "$NEW_VMID" =~ ^[0-9]+$ ]] || die "--new-vmid must be numeric"
[ "$NEW_VMID" -ge 9006 ] || die "--new-vmid must be >= 9006 (9005 is retained)"
[ "$NEW_VMID" -le 999999 ] || die "--new-vmid exceeds Proxmox's supported range"
[ "$NEW_VMID" != "$PARENT_VMID" ] || die "new VMID cannot equal retained parent 9005"
[ -n "$GUEST_HOST" ] || die "--guest-host is required"
[ -n "$GUEST_USER" ] || die "--guest-user cannot be empty"
NAME="${NAME:-pulp-linux-golden-m153-$NEW_VMID}"
[[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] || die "--name contains unsupported characters"

for path in "$MANIFEST" "$RENDER_VERIFIER"; do
  [ -f "$path" ] || die "required versioned input missing: $path"
done
command -v "$QM_BIN" >/dev/null 2>&1 || die "qm not found: $QM_BIN"
command -v "$SSH_BIN" >/dev/null 2>&1 || die "ssh not found: $SSH_BIN"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v flock >/dev/null 2>&1 || die "flock is required"

source_identity="$(python3 - "$MANIFEST" <<'PY'
import re, sys, tomllib
with open(sys.argv[1], "rb") as handle:
    manifest = tomllib.load(handle)
source = manifest.get("source", {})
skia = manifest.get("skia", {})
v8 = manifest.get("v8", {})
repo = source.get("repository", "")
commit = source.get("commit", "")
if not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit("pulp.linux.toml source.commit is not immutable 40-hex")
if skia.get("release") != "chrome/m153":
    raise SystemExit("pulp.linux.toml is not pinned to chrome/m153")
if v8.get("disposition") != "baked-provider-only":
    raise SystemExit("pulp.linux.toml does not require the matched V8 provider")
print(repo)
print(commit)
PY
)"
PULP_REPOSITORY="${source_identity%%$'\n'*}"
PULP_SHA="${source_identity#*$'\n'}"
[ -n "$PULP_REPOSITORY" ] && [[ "$PULP_SHA" =~ ^[0-9a-f]{40}$ ]] \
  || die "could not resolve exact source manifest"

mkdir -p "$(dirname "$LOCK_PATH")" "$RECEIPT_DIR"
exec 9>"$LOCK_PATH"
flock -n 9 || die "another golden refresh owns $LOCK_PATH"
receipt="$RECEIPT_DIR/pulp-linux-template-$NEW_VMID.json"
candidate_receipt="$RECEIPT_DIR/pulp-linux-template-$NEW_VMID.candidate.json"
[ ! -e "$receipt" ] || die "receipt already exists for VMID $NEW_VMID: $receipt"
for incomplete in "$candidate_receipt" "$candidate_receipt.tmp"; do
  [ ! -e "$incomplete" ] || die "incomplete prior receipt exists: $incomplete"
done

# Resolve and seal the retained parent before creating anything. The config is
# the authoritative local identity; a name or VMID alone is insufficient.
parent_config="$($QM_BIN config "$PARENT_VMID")" \
  || die "retained parent VMID $PARENT_VMID is unavailable"
printf '%s\n' "$parent_config" | grep -Eq '^template:[[:space:]]*1$' \
  || die "retained parent VMID $PARENT_VMID is not an immutable template"
parent_digest="$(printf '%s\n' "$parent_config" | sha256sum | awk '{print $1}')"

# VMID availability and clone happen under one lock. Never infer availability
# from `qm list` and then release the lock: two refreshes could claim the same ID.
if "$QM_BIN" config "$NEW_VMID" >/dev/null 2>&1; then
  die "new VMID $NEW_VMID already exists; choose a different unused VMID"
fi

candidate_created=0
template_requested=0
templated=0
on_exit(){
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$candidate_created" -eq 1 ] && [ "$templated" -eq 0 ]; then
    if [ "$template_requested" -eq 0 ]; then
      note "candidate VMID $NEW_VMID retained as a non-template for inspection; no VM was deleted"
    else
      note "template request for VMID $NEW_VMID did not reach a verified terminal state; inspect it before any next action; no VM was deleted"
    fi
  fi
}
trap on_exit EXIT

note "full-cloning retained template $PARENT_VMID to unused candidate $NEW_VMID"
"$QM_BIN" clone "$PARENT_VMID" "$NEW_VMID" --full 1 --name "$NAME"
candidate_created=1
"$QM_BIN" start "$NEW_VMID"

if [ -n "$SSH_KEY" ]; then
  SSH_OPTS+=(-i "$SSH_KEY")
fi
guest=("$SSH_BIN" "${SSH_OPTS[@]}" "$GUEST_USER@$GUEST_HOST")
reachable=0
for _ in $(seq 1 90); do
  if "${guest[@]}" true >/dev/null 2>&1; then reachable=1; break; fi
  sleep 2
done
[ "$reachable" -eq 1 ] || die "candidate $NEW_VMID did not become reachable at $GUEST_HOST"

"${guest[@]}" 'mkdir -p "$HOME/.local/lib/tartci" && cat >"$HOME/.local/lib/tartci/pulp-render-generation.py"' \
  <"$RENDER_VERIFIER"

note "baking exact Pulp $PULP_SHA with deep m153 Skia/Dawn and V8 receipts"
"${guest[@]}" env \
  "PULP_REPOSITORY=$PULP_REPOSITORY" \
  "PULP_SHA=$PULP_SHA" \
  "PARENT_VMID=$PARENT_VMID" \
  "PARENT_DIGEST=$parent_digest" \
  bash -s <<'GUEST'
set -euo pipefail

repo="$HOME/pulp"
if [ ! -d "$repo/.git" ]; then
  git clone "$PULP_REPOSITORY" "$repo"
fi
git -C "$repo" remote set-url origin "$PULP_REPOSITORY"
git -C "$repo" fetch --no-tags origin "$PULP_SHA"
git -C "$repo" cat-file -e "$PULP_SHA^{commit}"
git -C "$repo" checkout --detach "$PULP_SHA"
git -C "$repo" reset --hard "$PULP_SHA"
git -C "$repo" clean -ffd
[ "$(git -C "$repo" rev-parse HEAD)" = "$PULP_SHA" ]

cd "$repo"
export PULP_SHARED_FETCHCONTENT_SOURCE_DIR="$HOME/.cache/pulp/fetchcontent-src"
mkdir -p "$PULP_SHARED_FETCHCONTENT_SOURCE_DIR" "$HOME/.config/tartci"
python3 tools/scripts/fetch_skia_for_release.py linux-x64
python3 tools/scripts/verify_skia_m153_capabilities.py \
  --platform linux-x64 \
  --skia-dir external/skia-build \
  --result "$HOME/.config/tartci/pulp-skia-capabilities.json"
python3 tools/scripts/fetch_v8_for_release.py linux-x64

# Prime the same source cache and optimized object graph used by protected
# Linux jobs. This is intentionally a one-time golden build, not a cloud build.
./setup.sh --ci --deps-only
cmake -S . -B build-golden-m153 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DPULP_BUILD_TESTS=ON \
  -DPULP_ENABLE_GPU=ON
cmake --build build-golden-m153 --parallel "${PULP_GOLDEN_BUILD_JOBS:-4}"

python3 "$HOME/.local/lib/tartci/pulp-render-generation.py" \
  --repo "$repo" \
  --pulp-sha "$PULP_SHA" \
  --pulp-repository "$PULP_REPOSITORY" \
  --platform linux-x64 \
  --capability-result "$HOME/.config/tartci/pulp-skia-capabilities.json" \
  --v8-disposition baked-provider-only \
  --parent-kind proxmox-template \
  --parent-identity "$PARENT_VMID" \
  --parent-digest "$PARENT_DIGEST" \
  --output "$HOME/.config/tartci/pulp-render-generation.json"
GUEST

receipt_tmp="$candidate_receipt.tmp"
"${guest[@]}" 'cat "$HOME/.config/tartci/pulp-render-generation.json"' >"$receipt_tmp"
python3 - "$receipt_tmp" "$PULP_SHA" "$PARENT_VMID" "$parent_digest" <<'PY'
import json, sys
path, pulp_sha, parent_vmid, parent_digest = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    receipt = json.load(handle)
checks = {
    "status": receipt.get("status") == "pass",
    "pulp": receipt.get("pulp", {}).get("commit") == pulp_sha,
    "parent": receipt.get("parent", {}).get("identity") == parent_vmid,
    "parent_digest": receipt.get("parent", {}).get("digest_sha256") == parent_digest,
    "skia": receipt.get("skia_dawn", {}).get("release") == "chrome/m153",
    "v8": receipt.get("v8", {}).get("disposition") == "baked-provider-only",
    "v8_receipt": bool(receipt.get("v8", {}).get("generation_receipt_sha256")),
}
failed = [name for name, ok in checks.items() if not ok]
if failed:
    raise SystemExit("golden receipt validation failed: " + ", ".join(failed))
PY
chmod 0444 "$receipt_tmp"
mv "$receipt_tmp" "$candidate_receipt"
note "host candidate receipt validated at $candidate_receipt"

# Remove clone-specific identity only after the durable host receipt exists.
# No credential is ever copied into the guest, and no runner registration may
# survive templating.
"${guest[@]}" bash -s <<'GUEST'
set -euo pipefail
if [ -e "$HOME/.config/gh/hosts.yml" ]; then
  echo "refusing to template a guest containing GitHub CLI credentials" >&2
  exit 1
fi
if find "$HOME" -maxdepth 4 \( -name .runner -o -name .credentials -o -name .credentials_rsaparams \) -print -quit | grep -q .; then
  echo "refusing to template a registered Actions runner identity" >&2
  exit 1
fi
sudo truncate -s 0 /etc/machine-id
sudo rm -f /var/lib/dbus/machine-id /etc/ssh/ssh_host_*
sudo shutdown -h now
GUEST

stopped=0
for _ in $(seq 1 90); do
  status="$($QM_BIN status "$NEW_VMID" 2>/dev/null || true)"
  if printf '%s\n' "$status" | grep -Eq 'status:[[:space:]]*stopped'; then
    stopped=1
    break
  fi
  sleep 2
done
[ "$stopped" -eq 1 ] || die "candidate did not stop; refusing to template"

note "all source/provider/build/receipt gates passed; templating new VMID $NEW_VMID"
template_requested=1
"$QM_BIN" template "$NEW_VMID"
final_config="$($QM_BIN config "$NEW_VMID")"
printf '%s\n' "$final_config" | grep -Eq '^template:[[:space:]]*1$' \
  || die "qm template returned without an immutable template marker"
templated=1
mv "$candidate_receipt" "$receipt"
trap - EXIT
note "created new template $NEW_VMID ($NAME); retained parent 9005 unchanged"
