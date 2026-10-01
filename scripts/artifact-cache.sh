#!/usr/bin/env bash
# Populate the host artifact cache that macOS guests mount read-only.
#
#   scripts/artifact-cache.sh add --url <url> --sha256 <hex> [--dir DIR]
#   scripts/artifact-cache.sh git-sync --repo <owner/repo> [--branch main] [--dir DIR]
#   scripts/artifact-cache.sh prune --older-than-days <N> [--dir DIR]
#   scripts/artifact-cache.sh compact --repo <owner/repo> [--dir DIR]
#   scripts/artifact-cache.sh status [--dir DIR]
#
# add      downloads <url>, checks it against <hex> and stores it as
#          sha256/<hex>. The digest is the one the consuming job already pins,
#          so the cache can only ever hold bytes that job trusts. Re-adding an
#          existing blob re-verifies it and refreshes its age for prune.
# git-sync keeps git/<owner>/<repo>.git, a bare mirror of one branch, current.
#          Jobs use it as a Git alternate, so a stale mirror still saves every
#          byte it holds and the job fetches only what is newer.
# prune    removes blobs not added or re-added in the last <N> days. Mirrors
#          are bounded by their repository and are never pruned.
# compact  folds a mirror's packs into one. A guest reads packs through the
#          share for its whole job and a deleted pack may not be rediscovered
#          there, so this refuses while any Tart VM is running.
#
# A running guest may have the directory mounted, so nothing is ever rewritten
# in place: a blob lands in a private staging file on the same filesystem and
# is renamed into place, which is atomic, and `git-sync` only ever adds packs.
# Never delete a mirror or a blob a running VM may be reading. Runners pick the
# cache up at the next VM boot; no service restart is needed.
set -euo pipefail

usage(){
  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

die(){ printf 'artifact-cache: %s\n' "$*" >&2; exit 1; }

cmd="${1:-}"
case "$cmd" in
  add|git-sync|prune|compact|status) shift;;
  -h|--help) usage 0;;
  *) usage 2;;
esac

cache_root="${TARTCI_CI_CACHE:-${PULP_CI_CACHE:-$HOME/.cache/pulp-ci}}"
dir="${TARTCI_ARTIFACT_CACHE_DIR:-$cache_root/artifact-cache}"
url="" sha="" repo="" branch="main" days=""
# Where mirrors fetch from; tests point it at a local repository.
git_base="${TARTCI_ARTIFACT_CACHE_GIT_BASE:-https://github.com}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --url) url="${2:-}"; shift 2;;
    --sha256) sha="${2:-}"; shift 2;;
    --repo) repo="${2:-}"; shift 2;;
    --branch) branch="${2:-}"; shift 2;;
    --older-than-days) days="${2:-}"; shift 2;;
    --dir) dir="${2:-}"; shift 2;;
    -h|--help) usage 0;;
    *) die "unknown argument: $1";;
  esac
done

case "$dir" in
  /*) ;;
  *) die "--dir must be an absolute path: $dir";;
esac
case "$dir" in
  *:*|*$'\n'*|*$'\r'*) die "--dir contains a character Tart cannot share: $dir";;
esac

# A guest reads the mirror's packs for its whole job, so nothing may delete or
# rewrite one behind its back: no gc, no background maintenance (newer git runs
# a detached `maintenance run --auto` after every fetch, which can repack
# without consulting gc.auto), and every fetch kept as a pack rather than loose
# objects that a later repack would fold away. Applied on every sync so an
# older mirror picks up the same settings.
mirror_config(){
  git -C "$1" config gc.auto 0
  git -C "$1" config maintenance.auto false
  git -C "$1" config transfer.unpackLimit 1
}

sha256_of(){ shasum -a 256 "$1" | awk '{print $1}'; }

# One writer at a time per cache; a second sync waits rather than racing.
staging=""
lock_dir=""
cleanup(){
  [ -z "$staging" ] || rm -rf "$staging"
  [ -z "$lock_dir" ] || rmdir "$lock_dir" 2>/dev/null || true
}
lock_cache(){
  local waited=0
  mkdir -p "$dir"
  until mkdir "$dir/.lock" 2>/dev/null; do
    waited=$((waited + 1))
    [ "$waited" -le 600 ] || die "another sync has held $dir/.lock for 10 minutes"
    sleep 1
  done
  lock_dir="$dir/.lock"
  trap cleanup EXIT
}

case "$cmd" in
  add)
    [ -n "$url" ] || die "--url is required"
    [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "--sha256 must be 64 lowercase hex digits"
    case "$url" in
      https://*) ;;
      *) die "--url must be https: $url";;
    esac
    lock_cache
    mkdir -p "$dir/sha256"
    dest="$dir/sha256/$sha"
    if [ -f "$dest" ]; then
      [ "$(sha256_of "$dest")" = "$sha" ] \
        || die "$dest does not hash to its name; remove it by hand and re-add"
      touch "$dest"
      printf 'artifact-cache: sha256/%s already present (verified, age refreshed)\n' "$sha"
      exit 0
    fi
    staging="$(mktemp "$dir/sha256/.staging.XXXXXX")"
    curl --fail --location --silent --show-error --retry 5 --retry-all-errors \
      --retry-delay 5 --connect-timeout 30 --output "$staging" "$url" \
      || die "download failed: $url"
    actual="$(sha256_of "$staging")"
    [ "$actual" = "$sha" ] || die "SHA-256 mismatch for $url: expected $sha, got $actual"
    chmod 0644 "$staging"
    mv "$staging" "$dest"
    staging=""
    printf 'artifact-cache: added sha256/%s (%s bytes) from %s\n' \
      "$sha" "$(wc -c <"$dest" | tr -d ' ')" "$url"
    ;;

  git-sync)
    [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "--repo must look like owner/name"
    [[ "$branch" =~ ^[A-Za-z0-9_./-]+$ ]] || die "--branch has unsupported characters: $branch"
    lock_cache
    mirror="$dir/git/$repo.git"
    if [ ! -d "$mirror" ]; then
      mkdir -p "${mirror%/*}"
      staging="$(mktemp -d "${mirror%/*}/.staging.XXXXXX")"
      git init --quiet --bare "$staging"
      git -C "$staging" remote add origin "$git_base/$repo.git"
      mirror_config "$staging"
      git -C "$staging" fetch --quiet --no-tags origin \
        "+refs/heads/$branch:refs/heads/$branch" \
        || die "initial fetch of $repo failed"
      mv "$staging" "$mirror"
      staging=""
    else
      mirror_config "$mirror"
      git -C "$mirror" fetch --quiet --no-tags origin \
        "+refs/heads/$branch:refs/heads/$branch" \
        || die "fetch of $repo failed"
    fi
    packs="$(find "$mirror/objects/pack" -name '*.pack' | wc -l | tr -d ' ')"
    printf 'artifact-cache: %s at %s (%s, %s packs)\n' "$repo" \
      "$(git -C "$mirror" rev-parse --short "refs/heads/$branch")" \
      "$(du -sh "$mirror" | awk '{print $1}')" "$packs"
    [ "$packs" -le 32 ] \
      || printf 'artifact-cache: run `compact --repo %s` while no VM is running\n' "$repo"
    ;;

  compact)
    [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "--repo must look like owner/name"
    mirror="$dir/git/$repo.git"
    [ -d "$mirror" ] || die "no mirror at $mirror"
    running="$(${TARTCI_TART_BIN:-tart} list --format json 2>/dev/null \
      | python3 -c 'import json,sys; print(sum(1 for v in json.load(sys.stdin) if v.get("State") == "running" or v.get("Running")))' \
      2>/dev/null)" || running=""
    [ "$running" = 0 ] \
      || die "refusing to compact while Tart reports running VMs (${running:-unknown}); a guest may be reading these packs"
    lock_cache
    git -C "$mirror" repack -a -d -q
    printf 'artifact-cache: compacted %s to %s pack(s)\n' "$repo" \
      "$(find "$mirror/objects/pack" -name '*.pack' | wc -l | tr -d ' ')"
    ;;

  prune)
    [[ "$days" =~ ^[1-9][0-9]*$ ]] || die "--older-than-days must be a positive integer"
    [ -d "$dir/sha256" ] || { printf 'artifact-cache: nothing to prune\n'; exit 0; }
    lock_cache
    removed=0
    while IFS= read -r blob; do
      rm -f "$blob"
      removed=$((removed + 1))
    done < <(find "$dir/sha256" -maxdepth 1 -type f ! -name '.*' -mtime "+$days")
    printf 'artifact-cache: pruned %d blob(s) older than %d day(s)\n' "$removed" "$days"
    ;;

  status)
    [ -d "$dir" ] || { printf 'artifact-cache: %s does not exist\n' "$dir"; exit 0; }
    printf 'artifact-cache: %s (%s)\n' "$dir" "$(du -sh "$dir" | awk '{print $1}')"
    for blob in "$dir"/sha256/*; do
      [ -f "$blob" ] || continue
      printf '  sha256/%s  %s bytes\n' "${blob##*/}" "$(wc -c <"$blob" | tr -d ' ')"
    done
    for mirror in "$dir"/git/*/*.git; do
      [ -d "$mirror" ] || continue
      printf '  %s  %s\n' "${mirror#"$dir"/}" \
        "$(git -C "$mirror" for-each-ref --format='%(refname:short)@%(objectname:short)' refs/heads | tr '\n' ' ')"
    done
    ;;
esac
