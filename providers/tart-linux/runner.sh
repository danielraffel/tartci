#!/usr/bin/env bash
# tart-linux/runner.sh — ephemeral, per-job GitHub Actions runner on a Tart LINUX
# VM. The pool-serving sibling of run.sh: where run.sh does ONE on-demand
# build+ctest in-guest and exits, this mints a Just-In-Time (single-job) runner
# config, clones the golden, boots it with the host ccache mounted, runs the
# Actions agent ONCE against that JIT config, then discards the clone. The
# WORKFLOW (build.yml on GitHub) drives the build — the supervisor only supplies
# a clean VM per job. Native arm64 Ubuntu; Skia is baked in-checkout.
#
# Ported from Pulp's tools/ci/tart-runner-linux.sh (the proven supervisor) into
# the project-agnostic tartci provider shape: repo/golden/labels are env-driven.
# Defaults target danielraffel/pulp (the first consumer); override for any repo.
#
# The golden must carry the actions-runner agent at ~/actions-runner (the
# linux-arm64 install). This supervisor never registers a long-lived runner —
# JIT configs are single-job and ephemeral.
#
# CONCURRENCY: Linux guests are UNCAPPED (no 2-VM macOS kernel quota), so this can
# run several concurrent clones on one host; the --loop gate still only boots when
# there is queued work, to avoid spinning idle VMs.
#
# Pilot-safe by default: the label is `<repo>-build-linux` (NOT a required check),
# so jobs only land here when a workflow explicitly routes to it. Promote to the
# pooled label once a pilot is clean.
#
# Usage:
#   providers/tart-linux/runner.sh                 # one ephemeral job then exit (pilot default)
#   providers/tart-linux/runner.sh --loop          # keep serving jobs (LaunchAgent uses this)
#   providers/tart-linux/runner.sh --labels self-hosted,Linux,ARM64,pulp-build
set -euo pipefail

export TART_HOME="${TART_HOME:-/Volumes/Workshop/VMs}"
SSH_KEY_PRIV="${TARTCI_VM_SSH_KEY:-${PULP_VM_SSH_KEY:-$HOME/.ssh/id_ed25519}}"
VM_USER="${TARTCI_VM_USER:-${PULP_VM_USER:-admin}}"
CACHE_ROOT="${TARTCI_CI_CACHE:-${PULP_CI_CACHE:-$HOME/.cache/tartci}}"
GOLDEN="${TARTCI_LINUX_GOLDEN:-${PULP_LINUX_GOLDEN:-pulp-linux-build:latest}}"
REPO="${TARTCI_RUNNER_REPO:-${PULP_RUNNER_REPO:-danielraffel/pulp}}"
LABELS="${TARTCI_RUNNER_LABELS:-${PULP_RUNNER_LABELS:-self-hosted,Linux,ARM64,pulp-build-linux}}"
RUNNER_GROUP_ID="${TARTCI_RUNNER_GROUP_ID:-${PULP_RUNNER_GROUP_ID:-1}}"
# Workflow name the --loop gate counts as "queued work". Override per repo.
WORKFLOW_NAME="${TARTCI_RUNNER_WORKFLOW_NAME:-Build and Test}"
LOOP=0
POLL="${TARTCI_VM_POLL:-${PULP_VM_POLL:-20}}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes)

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
command -v tart >/dev/null 2>&1 || die "tart not installed"
command -v gh   >/dev/null 2>&1 || die "gh not installed / authed (need admin to mint JIT config)"

while [ $# -gt 0 ]; do case "$1" in
  --loop) LOOP=1; shift;;
  --once) LOOP=0; shift;;
  --golden) GOLDEN="$2"; shift 2;;
  --labels) LABELS="$2"; shift 2;;
  --repo) REPO="$2"; shift 2;;
  -h|--help) sed -n '2,30p' "$0"; exit 0;;
  *) die "unknown arg: $1";;
esac; done

# Coarse "is there work waiting?" gate for --loop: count queued runs of the named
# workflow. 0 on any gh failure (treat as "no work" so a flaky API doesn't spin VMs).
queued_work(){
  gh api "repos/$REPO/actions/runs?status=queued&per_page=30" \
    --jq "[.workflow_runs[] | select(.name == \"$WORKFLOW_NAME\")] | length" \
    2>/dev/null || echo 0
}

run_one(){ # $1=iteration index (unique VM name without Date.now/rand)
  local i="$1" vm="linux-ephr-$$-$1" jit
  note "[$i] minting JIT runner config (labels=$LABELS, ephemeral)"
  local label_args=(); local l; IFS=',' read -ra _ls <<< "$LABELS"
  for l in "${_ls[@]}"; do label_args+=(-f "labels[]=$l"); done
  jit="$(gh api -X POST "repos/$REPO/actions/runners/generate-jitconfig" \
        -f "name=$vm" -F "runner_group_id=$RUNNER_GROUP_ID" "${label_args[@]}" \
        --jq '.encoded_jit_config')" || die "JIT config mint failed (need repo admin)"
  [ -n "$jit" ] || die "empty JIT config"

  note "[$i] clone $GOLDEN → $vm (CoW) + boot with host ccache mounted"
  tart clone "$GOLDEN" "$vm"
  mkdir -p "$CACHE_ROOT/ccache-linux"
  local boot_log; boot_log="$(mktemp -t "tart-run-$vm")"
  local rpid; tart run --no-graphics --dir="ccache:$CACHE_ROOT/ccache-linux" "$vm" >"$boot_log" 2>&1 & rpid=$!

  local ip=""
  for _ in $(seq 1 60); do ip="$(tart ip "$vm" 2>/dev/null || true)"; [ -n "$ip" ] && break; sleep 2; done
  if [ -z "$ip" ]; then
    note "[$i] no IP after 120s — last lines of \`tart run\` ($boot_log):"; tail -3 "$boot_log" >&2 2>/dev/null || true
    tart stop "$vm" >/dev/null 2>&1||true; kill "$rpid" 2>/dev/null||true; tart delete "$vm" >/dev/null 2>&1||true
    rm -f "$boot_log"; die "[$i] no IP (see \`tart run\` output above)"
  fi
  rm -f "$boot_log"
  local sshok=0
  for _ in $(seq 1 90); do ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" true 2>/dev/null && { sshok=1; break; }; sleep 2; done
  if [ "$sshok" != 1 ]; then
    note "[$i] no SSH on $vm after 180s — discarding (won't run a job on an unreachable VM)"
    tart stop "$vm" >/dev/null 2>&1 || true; kill "$rpid" 2>/dev/null || true; tart delete "$vm" >/dev/null 2>&1 || true
    return 1
  fi
  note "[$i] vm $vm up at $ip — mounting ccache + launching JIT runner (one job)"

  # Best-effort host ccache via virtio-fs (named "ccache" subdir is the rw one).
  # Then write the JIT config and run the agent once — a JIT runner processes
  # exactly one job and deregisters. CCACHE_* come from the baked
  # ~/actions-runner/.env so job steps inherit warm-cache settings.
  ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "sudo mkdir -p /mnt/host 2>/dev/null; sudo mount -t virtiofs com.apple.virtio-fs.automount /mnt/host 2>/dev/null || true; \
     if [ -d /mnt/host/ccache ] && [ -w /mnt/host/ccache ]; then mkdir -p ~/.ccache && ln -sfn /mnt/host/ccache ~/.ccache; fi; \
     printf '%s' '$jit' > ~/jit.cfg && cd ~/actions-runner && ./run.sh --jitconfig \"\$(cat ~/jit.cfg)\"" \
    || note "[$i] runner exited non-zero (job failure or no job) — VM will be discarded regardless"

  note "[$i] discarding ephemeral VM $vm"
  tart stop "$vm" >/dev/null 2>&1 || true; kill "$rpid" 2>/dev/null || true; sleep 2
  tart delete "$vm" >/dev/null 2>&1 || true
}

i=0
if [ "$LOOP" = 1 ]; then
  note "ephemeral Linux runner LOOP (Ctrl-C to stop); golden=$GOLDEN labels=$LABELS"
  while true; do
    q="$(queued_work)"
    if [ "${q:-0}" -gt 0 ]; then
      i=$((i+1)); note "[$i] queued=$q → booting ephemeral Linux VM"; run_one "$i" || true
    else
      note "waiting ${POLL}s (queued=$q — no '$WORKFLOW_NAME' work)"; sleep "$POLL"
    fi
  done
else
  note "ephemeral Linux runner ONCE; golden=$GOLDEN labels=$LABELS"
  run_one 1
fi
