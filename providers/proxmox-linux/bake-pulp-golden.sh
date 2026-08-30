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
SOURCE_PIN_RESOLVER="$ROOT/providers/common/pulp-source-pin.py"
QM_BIN="${TARTCI_QM_BIN:-qm}"
PVESH_BIN="${TARTCI_PVESH_BIN:-pvesh}"
SSH_BIN="${TARTCI_SSH_BIN:-ssh}"
PARENT_VMID=9005
NEW_VMID=""
GUEST_HOST=""
# Pulp's pinned protected Proxmox supervisor connects as ci; baking into a
# different home would warm and scrub state the protected consumer never uses.
GUEST_USER="ci"
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
NAME="${NAME:-pulp-linux-golden-m153-$NEW_VMID}"
[[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] || die "--name contains unsupported characters"

for path in "$MANIFEST" "$SOURCE_PIN_RESOLVER" "$RENDER_VERIFIER"; do
  [ -f "$path" ] || die "required versioned input missing: $path"
done
command -v "$QM_BIN" >/dev/null 2>&1 || die "qm not found: $QM_BIN"
command -v "$PVESH_BIN" >/dev/null 2>&1 || die "pvesh not found: $PVESH_BIN"
command -v "$SSH_BIN" >/dev/null 2>&1 || die "ssh not found: $SSH_BIN"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v flock >/dev/null 2>&1 || die "flock is required"

source_identity="$(python3 "$SOURCE_PIN_RESOLVER" "$MANIFEST" \
  --require-skia-release chrome/m153 \
  --require-v8-disposition baked-provider-only)" \
  || die "could not resolve immutable Pulp source from $MANIFEST"
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

# Ask the cluster allocator whether this exact VMID is free. A failed `qm
# config` is ambiguous (missing VM, denied permission, quorum/API failure), so
# it is never accepted as absence. The cluster query and clone remain under the
# refresh lock; Proxmox's clone allocation is the final atomic race guard.
if ! available_vmid="$($PVESH_BIN get /cluster/nextid \
  --vmid "$NEW_VMID" --output-format json 2>&1)"; then
  die "could not authoritatively prove new VMID $NEW_VMID is unused: $available_vmid"
fi
python3 - "$available_vmid" "$NEW_VMID" <<'PY' \
  || die "cluster allocator did not confirm exact unused VMID $NEW_VMID"
import json, sys
actual = json.loads(sys.argv[1])
if str(actual) != sys.argv[2]:
    raise SystemExit(1)
PY

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

# Bind the operator-supplied SSH address to the exact Proxmox candidate before
# trusting any proof returned over SSH.  The nonce enters only through the
# guest agent for NEW_VMID, so a stale or mistyped address cannot validate a
# different machine and leave the actual clone unverified.
binding_nonce="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"
binding_path="/run/tartci-pulp-golden-$NEW_VMID.binding"

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
binding_placed=0
for _ in $(seq 1 30); do
  if "$QM_BIN" guest exec "$NEW_VMID" -- /bin/sh -c \
    "set -eu; uid=\$(id -u '$GUEST_USER'); gid=\$(id -g '$GUEST_USER'); tmp='$binding_path.tmp'; umask 077; printf '%s' '$binding_nonce' > \"\$tmp\"; chown \"\$uid:\$gid\" \"\$tmp\"; chmod 0400 \"\$tmp\"; mv \"\$tmp\" '$binding_path'" \
    >/dev/null 2>&1; then
    binding_placed=1
    break
  fi
  sleep 2
done
[ "$binding_placed" -eq 1 ] \
  || die "could not place VMID binding nonce through the guest agent"
ssh_binding="$("${guest[@]}" cat "$binding_path" 2>/dev/null || true)"
[ "$ssh_binding" = "$binding_nonce" ] \
  || die "SSH peer $GUEST_HOST does not match candidate VMID $NEW_VMID"

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
export CCACHE_COMPILERCHECK=content
export CCACHE_NODEPEND=true
export CCACHE_SLOPPINESS=time_macros
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
  -DPULP_BUILD_EXAMPLES=OFF \
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
python3 - "$receipt_tmp" "$PULP_REPOSITORY" "$PULP_SHA" "$PARENT_VMID" "$parent_digest" <<'PY'
import json, re, sys
path, pulp_repository, pulp_sha, parent_vmid, parent_digest = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    receipt = json.load(handle)
hex40 = re.compile(r"[0-9a-f]{40}")
hex64 = re.compile(r"[0-9a-f]{64}")
pulp = receipt.get("pulp", {})
parent = receipt.get("parent", {})
skia = receipt.get("skia_dawn", {})
v8 = receipt.get("v8", {})
checks = {
    "schema": receipt.get("schema") == 1,
    "status": receipt.get("status") == "pass",
    "pulp_repository": pulp.get("repository") == pulp_repository,
    "pulp_commit": pulp.get("commit") == pulp_sha,
    "pulp_manifest": bool(hex64.fullmatch(str(pulp.get("manifest_sha256", "")))),
    "parent_kind": parent.get("kind") == "proxmox-template",
    "parent_identity": parent.get("identity") == parent_vmid,
    "parent_digest": parent.get("digest_sha256") == parent_digest,
    "skia_release": skia.get("release") == "chrome/m153",
    "skia_commit": bool(hex40.fullmatch(str(skia.get("skia_commit", "")))),
    "dawn_commit": bool(hex40.fullmatch(str(skia.get("built_dawn", "")))),
    "skia_platform": skia.get("platform") == "linux-x64",
    "skia_asset": bool(hex64.fullmatch(str(skia.get("asset_sha256", "")))),
    "skia_receipt": bool(hex64.fullmatch(str(skia.get("generation_receipt_sha256", "")))),
    "capability_result": bool(hex64.fullmatch(str(skia.get("capability_result_sha256", "")))),
    "capabilities": skia.get("capabilities") == [
        "SkLogHandler.GetInstance.execute",
        "SkLogHandler.SetInstance.compile-link-only",
        "Graphite.ContextOptions.fExecutor.execute",
    ],
    "capability_limitations": skia.get("limitations") == [
        "SkLogHandler.SetInstance is not executed because it installs process-global first-install-wins state",
    ],
    "probe_count": skia.get("probe_count") == 1,
    "v8_disposition": v8.get("disposition") == "baked-provider-only",
    "v8_version": isinstance(v8.get("version"), str) and "m153" in v8["version"],
    "v8_platform": v8.get("platform") == "linux-x64",
    "v8_asset": bool(hex64.fullmatch(str(v8.get("asset_sha256", "")))),
    "v8_receipt": bool(hex64.fullmatch(str(v8.get("generation_receipt_sha256", "")))),
    "v8_runtime_policy": v8.get("runtime_policy") == "provider-cached; Pulp defaults to QuickJS",
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
"${guest[@]}" env "PULP_GOLDEN_BINDING_PATH=$binding_path" bash -s <<'GUEST'
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
sudo rm -f "$PULP_GOLDEN_BINDING_PATH"
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
