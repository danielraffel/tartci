#!/usr/bin/env bash
# tartci goldens — list + sync CI golden images across the host pool.
#
# Windows QEMU goldens are large (~26 GB) portable qcow2 files that each host
# keeps under its own $TARTCI_GOLDENS. When one host re-bakes a golden the others
# drift until it's copied over and their runners re-pointed. This automates that:
# connect the machines (fastest over a Thunderbolt cable), then one command.
#
# Scope: Windows (qcow2) goldens. macOS/Linux Tart goldens sync via tart
# export/clone and are out of scope for this MVP (see docs/golden-sync.md).
#
# Usage:
#   tartci goldens list [--os windows]
#   tartci goldens sync --to HOST   [opts]   # PUSH this host's canonical golden → HOST
#   tartci goldens sync --from HOST [opts]   # PULL HOST's canonical golden → this host (new-machine setup)
#     opts: [--os windows] [--prune] [--dry-run] [--no-reload] [--via IP]
#
# HOST is an ssh alias. The fastest link is auto-detected: a Thunderbolt
# link-local peer (169.254.x on bridge0) that identifies as HOST is preferred,
# else it falls back to whatever the ssh alias resolves to (LAN/Tailscale).
# To seed a brand-new host, run `--from` on it once tartci is deployed; if no
# peer has the golden yet, bake one from scratch (docs/runbook.md §4).
set -euo pipefail

note(){ printf '\033[36m• %s\033[0m\n' "$*"; }
ok(){   printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn(){ printf '\033[33m• %s\033[0m\n' "$*" >&2; }
die(){  printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

GOLDENS="${TARTCI_GOLDENS:-$HOME/.tartci/goldens}"
OS="windows"
WIN_RUNNER_LABEL="com.danielraffel.pulp.qemu-runner-windows"

golden_glob(){ # $1=os -> printable glob (no sha files)
  case "$1" in
    windows) echo "pulp-windows-build-*.qcow2" ;;
    *) die "unsupported --os '$1' (MVP: windows only)" ;;
  esac
}

# goldens for OS, newest first, excluding .sha256 sidecars. Golden filenames are
# controlled (pulp-<os>-build-*.qcow2, no spaces/newlines), so `ls -t` for mtime
# ordering is safe here.
list_goldens(){
  local g; g="$(golden_glob "$OS")"
  # `|| true`: an empty match makes grep exit 1, which under pipefail+set -e would
  # abort callers instead of returning "no goldens" gracefully.
  # shellcheck disable=SC2010,SC2086
  ls -t "$GOLDENS"/$g 2>/dev/null | grep -v '\.sha256$' || true
}
canonical_local(){ list_goldens | head -1; }

ensure_sha(){ # $1=golden path -> ensures a .sha256 sidecar exists locally
  local f="$1"
  [ -f "$f.sha256" ] && return 0
  note "generating missing sha256 for $(basename "$f")"
  ( cd "$(dirname "$f")" && shasum -a 256 "$(basename "$f")" > "$(basename "$f").sha256" )
}

# Resolve the ssh user + identity the alias uses, so we can reuse them when
# probing a raw Thunderbolt IP (which isn't in ~/.ssh/config).
ssh_user_for(){ ssh -G "$1" 2>/dev/null | awk '/^user /{print $2; exit}'; }
ssh_key_for(){  ssh -G "$1" 2>/dev/null | awk '/^identityfile /{print $2; exit}'; }

# Pick the fastest link to HOST. Echoes an ssh destination (raw IP over TB, or
# the alias). Sets global LINK to "thunderbolt"|"alias" for reporting.
LINK="alias"
resolve_link(){ # $1=host  $2=via_override
  local host="$1" via="$2" want user key ip h
  if [ -n "$via" ]; then LINK="thunderbolt(--via)"; echo "$via"; return; fi
  # what hostname does the alias report? (short, lowercased for compare)
  want="$(ssh -o BatchMode=yes -o ConnectTimeout=6 "$host" 'scutil --get LocalHostName 2>/dev/null || hostname -s' 2>/dev/null | tr '[:upper:]' '[:lower:]')"
  [ -n "$want" ] || { echo "$host"; return; }   # alias unreachable? let ssh error later
  user="$(ssh_user_for "$host")"; key="$(ssh_key_for "$host")"
  # candidate TB link-local peers: bridge0 neighbors in 169.254/16 (excluding our own)
  local mine; mine="$(ipconfig getifaddr bridge0 2>/dev/null || true)"
  for ip in $(arp -an 2>/dev/null | grep -oE '169\.254\.[0-9]+\.[0-9]+' | sort -u); do
    [ "$ip" = "$mine" ] && continue
    h="$(ssh ${key:+-i "$key"} -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
         -o ConnectTimeout=4 "${user:+$user@}$ip" 'scutil --get LocalHostName 2>/dev/null || hostname -s' 2>/dev/null | tr '[:upper:]' '[:lower:]')" || continue
    if [ "$h" = "$want" ]; then LINK="thunderbolt"; echo "$ip"; return; fi
  done
  echo "$host"
}

# Build the ssh command array for a resolved destination of HOST.
# Uses the alias's key/user when the destination is a raw IP.
_rsh(){ # $1=host_alias $2=dest -> prints an -e ssh string
  local host="$1" dest="$2" key
  if [ "$dest" = "$host" ]; then echo "ssh -o BatchMode=yes -o ConnectTimeout=10"; return; fi
  key="$(ssh_key_for "$host")"
  echo "ssh ${key:+-i $key} -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10"
}
_dest_prefix(){ # $1=host_alias $2=dest -> user@ip for raw IP, or alias
  local host="$1" dest="$2" user
  [ "$dest" = "$host" ] && { echo "$host"; return; }
  user="$(ssh_user_for "$host")"; echo "${user:+$user@}$dest"
}

cmd_list(){
  while [ $# -gt 0 ]; do case "$1" in --os) OS="$2"; shift 2;; *) die "unknown arg: $1";; esac; done
  local c; c="$(canonical_local)"
  note "local \$TARTCI_GOLDENS = $GOLDENS"
  if [ -z "$c" ]; then warn "no $OS golden found locally"; return 0; fi
  ok "canonical $OS golden: $(basename "$c") ($(du -h "$c" | cut -f1))"
  if [ -f "$c.sha256" ]; then ok "sha256 sidecar present"; else warn "sha256 sidecar MISSING (sync will generate it)"; fi
  # list any older goldens (prune candidates)
  list_goldens | tail -n +2 | while read -r old; do
    warn "superseded (prune candidate): $(basename "$old") ($(du -h "$old" | cut -f1))"
  done
}

# Read a host's own $TARTCI_GOLDENS (from its runner plist; don't assume the path).
remote_goldens_dir(){ # $1=host
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$1" '
    /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:TARTCI_GOLDENS" \
      ~/Library/LaunchAgents/'"$WIN_RUNNER_LABEL"'.plist 2>/dev/null \
      || echo "${TARTCI_GOLDENS:-$HOME/.tartci/goldens}"' 2>/dev/null
}

# The repoint + idle-only reload + guarded prune, applied on whichever host runs
# it — piped to `ssh HOST bash -s` for --to (remote) or `bash -s` for --from
# (local). Single source of truth so push and pull can't drift. Positional args:
#   <goldens_dir> <name> <label> <reload:0|1> <prune:0|1> <os>
_apply_snippet(){ cat <<'SNIP'
set -euo pipefail
gd="$1"; name="$2"; label="$3"; reload="$4"; prune="$5"; os="$6"
case "$os" in windows) glob="pulp-windows-build-*.qcow2" ;; *) glob="pulp-$os-build-*.qcow2" ;; esac
plist="$HOME/Library/LaunchAgents/$label.plist"
note(){ printf '  • %s\n' "$*"; }
if [ -f "$plist" ] && /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:TARTCI_WIN_GOLDEN" "$plist" >/dev/null 2>&1; then
  cp "$plist" "$plist.bak.$(date +%Y%m%d-%H%M%S)"
  /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:TARTCI_WIN_GOLDEN $gd/$name" "$plist"
  note "repointed pin → $name"
else
  note "no explicit pin — provider default resolves to the canonical name"
fi
# Reload only when idle (never mid-job — that kills the running build). Repointing
# the pin changed plist env, and `launchctl kickstart -k` does NOT re-read plist
# EnvironmentVariables — only a full bootout+bootstrap does. Prefer tartci's own
# launchd reloader, which handles the bootout/bootstrap race.
if [ "$reload" = 1 ]; then
  log="$HOME/Library/Logs/tartci/${label##*.}.log"
  if tail -1 "$log" 2>/dev/null | grep -q 'waiting'; then
    if [ -x "$HOME/.local/share/tartci/tartci" ] \
       && "$HOME/.local/share/tartci/tartci" launchd reload "$label" >/dev/null 2>&1; then
      note "reloaded runner via 'tartci launchd reload' (was idle)"
    else
      u="$(id -u)"; launchctl bootout "gui/$u/$label" 2>/dev/null; sleep 3
      if launchctl bootstrap "gui/$u" "$plist" 2>/dev/null; then
        note "reloaded runner (bootout+bootstrap, was idle)"
      else
        note "reload failed — new pin applies on the runner's next natural cycle"
      fi
    fi
  else
    note "runner busy — new golden applies on its next cycle (not reloading mid-job)"
  fi
fi
# guarded prune: remove older goldens, never one a live VM is backed by
if [ "$prune" = 1 ]; then
  cd "$gd"
  # shellcheck disable=SC2010,SC2086
  ls -t $glob 2>/dev/null | grep -v '\.sha256$' | tail -n +2 | while read -r old; do
    if ps aux | grep -i qemu-system | grep -v grep | grep -qF "$old"; then
      note "KEEP $old (a running VM is backed by it)"
    else
      rm -f "$old" "$old.sha256" && note "pruned $old"
    fi
  done
fi
SNIP
}

# Emits bash (piped to `ssh HOST bash -s -- <os> <goldens_dir>`) that echoes the
# host's newest golden filename for OS, ensuring its .sha256 sidecar exists, or
# "__NONE__". Glob is built inside the remote bash so the login shell can't
# glob-expand a literal '*'. Piped (not a heredoc-in-$()) to keep bash's command-
# substitution parser away from the ')' chars in the body.
_remote_canonical_query(){ cat <<'SNIP'
set -euo pipefail
os="$1"; gd="$2"
case "$os" in windows) g="pulp-windows-build-*.qcow2" ;; *) g="pulp-$os-build-*.qcow2" ;; esac
# shellcheck disable=SC2010,SC2086
newest="$(cd "$gd" 2>/dev/null && ls -t $g 2>/dev/null | grep -v '\.sha256$' | head -1 || true)"
[ -n "$newest" ] || { echo "__NONE__"; exit 0; }
[ -f "$gd/$newest.sha256" ] || ( cd "$gd" && shasum -a 256 "$newest" > "$newest.sha256" )
echo "$newest"
SNIP
}

cmd_sync(){
  local TO="" FROM="" PRUNE=0 DRY=0 RELOAD=1 VIA=""
  while [ $# -gt 0 ]; do case "$1" in
    --to) TO="$2"; shift 2;;
    --from) FROM="$2"; shift 2;;
    --os) OS="$2"; shift 2;;
    --prune) PRUNE=1; shift;;
    --dry-run) DRY=1; shift;;
    --no-reload) RELOAD=0; shift;;
    --via) VIA="$2"; shift 2;;
    *) die "unknown arg: $1";;
  esac; done
  [ -n "$TO" ] || [ -n "$FROM" ] || die "usage: tartci goldens sync (--to HOST | --from HOST) [--os windows] [--prune] [--dry-run] [--no-reload] [--via IP]"
  [ -n "$TO" ] && [ -n "$FROM" ] && die "--to and --from are mutually exclusive"

  local HOST flags="-a --partial --progress"
  [ "$DRY" = 1 ] && flags="$flags -n"

  if [ -n "$TO" ]; then
    ##### PUSH: this host's canonical golden → HOST #####
    HOST="$TO"
    local golden name; golden="$(canonical_local)"
    [ -n "$golden" ] || die "no $OS golden found locally in $GOLDENS"
    name="$(basename "$golden")"; ensure_sha "$golden"
    ok "local canonical: $name ($(du -h "$golden" | cut -f1))"
    local rgoldens; rgoldens="$(remote_goldens_dir "$HOST")"
    [ -n "$rgoldens" ] || die "could not resolve remote TARTCI_GOLDENS on $HOST"
    note "remote \$TARTCI_GOLDENS on $HOST = $rgoldens"
    local dest rsh pfx
    dest="$(resolve_link "$HOST" "$VIA")"; rsh="$(_rsh "$HOST" "$dest")"; pfx="$(_dest_prefix "$HOST" "$dest")"
    note "link: $LINK  →  $pfx"
    note "rsync $name (+sha256) → $HOST:$rgoldens"
    # shellcheck disable=SC2086
    rsync $flags -e "$rsh" "$golden" "$golden.sha256" "$pfx:$rgoldens/" || die "rsync failed"
    if [ "$DRY" = 1 ]; then note "dry-run — stopping before verify/reload/prune"; return 0; fi
    note "verifying sha256 on $HOST"
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "cd '$rgoldens' && shasum -a 256 -c '$name.sha256'" \
      || die "sha256 verify FAILED on $HOST — not repointing"
    ok "verified on $HOST"
    _apply_snippet | ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" bash -s -- \
      "$rgoldens" "$name" "$WIN_RUNNER_LABEL" "$RELOAD" "$PRUNE" "$OS" \
      || die "remote repoint/reload on $HOST failed"
    ok "$HOST synced to $name"
  else
    ##### PULL: HOST's canonical golden → this host (new-machine setup) #####
    HOST="$FROM"
    local rgoldens; rgoldens="$(remote_goldens_dir "$HOST")"
    [ -n "$rgoldens" ] || die "could not resolve remote TARTCI_GOLDENS on $HOST"
    # remote canonical NAME + ensure its .sha256 exists (glob built inside remote
    # bash so the login shell can't glob-expand a literal '*').
    local rname
    rname="$(_remote_canonical_query | ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" bash -s -- "$OS" "$rgoldens")"
    [ -n "$rname" ] && [ "$rname" != "__NONE__" ] || die "$HOST has no $OS golden in $rgoldens (bake one first — docs/runbook.md §4)"
    ok "$HOST canonical: $rname"
    note "local \$TARTCI_GOLDENS = $GOLDENS"; mkdir -p "$GOLDENS"
    local dest rsh pfx
    dest="$(resolve_link "$HOST" "$VIA")"; rsh="$(_rsh "$HOST" "$dest")"; pfx="$(_dest_prefix "$HOST" "$dest")"
    note "link: $LINK  ←  $pfx"
    note "rsync $rname (+sha256) ← $HOST:$rgoldens"
    # shellcheck disable=SC2086
    rsync $flags -e "$rsh" "$pfx:$rgoldens/$rname" "$pfx:$rgoldens/$rname.sha256" "$GOLDENS/" || die "rsync failed"
    if [ "$DRY" = 1 ]; then note "dry-run — stopping before verify/reload/prune"; return 0; fi
    note "verifying sha256 locally"
    ( cd "$GOLDENS" && shasum -a 256 -c "$rname.sha256" ) || die "sha256 verify FAILED locally — not repointing"
    ok "verified locally"
    _apply_snippet | bash -s -- "$GOLDENS" "$rname" "$WIN_RUNNER_LABEL" "$RELOAD" "$PRUNE" "$OS" \
      || die "local repoint/reload failed"
    ok "this host synced to $rname"
  fi
}

sub="${1:-}"; shift || true
case "$sub" in
  list) cmd_list "$@";;
  sync) cmd_sync "$@";;
  ""|-h|--help|help)
    sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//';;
  *) die "unknown goldens subcommand: $sub (try: list, sync)";;
esac
