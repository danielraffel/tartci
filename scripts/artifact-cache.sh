#!/usr/bin/env bash
# Populate the host artifact cache that macOS guests mount read-only.
#
#   scripts/artifact-cache.sh add --url <url> --sha256 <hex> [--dir DIR]
#   scripts/artifact-cache.sh git-sync --repo <owner/repo> [--branch main] [--dir DIR]
#   scripts/artifact-cache.sh prune --older-than-days <N> [--dir DIR]
#   scripts/artifact-cache.sh compact --repo <owner/repo> [--dir DIR]
#   scripts/artifact-cache.sh refresh [--compact-above <packs>] [--dir DIR]
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
# refresh  re-syncs every branch of every mirror that already exists under git/
#          and creates none, so a host that never ran git-sync is untouched; an
#          absent cache is a no-op. With --compact-above N it also folds a
#          mirror holding more than N packs, only when Tart reports no running
#          VM at that moment, and skips (never fails) otherwise. Then, for every
#          line of the pin list (--pins FILE, default
#          manifests/artifact-cache-pins.tsv) whose repository is mirrored
#          here, it reads the pinned url and sha256 from the mirror's own copy
#          of that repository's dependency manifest and adds the blob, so a
#          pin bump reaches the cache within one refresh instead of never.
#          It also adds the GitHub Actions Runner archive the macOS lane pins
#          (url and digest from `runner.sh --print-runner-archive`), so a VM
#          copies it instead of downloading it. This is what the scheduled
#          com.danielraffel.tartci.artifact-cache-refresh agent runs. One failed mirror or pin does not stop the
#          others; the exit is 1.
#
# A running guest may have the directory mounted, so nothing is ever rewritten
# in place: a blob lands in a private staging file on the same filesystem and
# is renamed into place, which is atomic, and `git-sync` only ever adds packs.
# Never delete a mirror or a blob a running VM may be reading. Runners pick the
# cache up at the next VM boot; no service restart is needed.
set -euo pipefail

usage(){
  sed -n '2,43p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

die(){ printf 'artifact-cache: %s\n' "$*" >&2; exit 1; }

cmd="${1:-}"
case "$cmd" in
  add|git-sync|prune|compact|refresh|status) shift;;
  -h|--help) usage 0;;
  *) usage 2;;
esac

cache_root="${TARTCI_CI_CACHE:-${PULP_CI_CACHE:-$HOME/.cache/pulp-ci}}"
dir="${TARTCI_ARTIFACT_CACHE_DIR:-$cache_root/artifact-cache}"
url="" sha="" repo="" branch="main" days="" compact_above=""
# Which pinned archives `refresh` keeps in the cache (see the file's header).
pins="${TARTCI_ARTIFACT_CACHE_PINS:-$(cd "$(dirname "$0")/.." && pwd)/manifests/artifact-cache-pins.tsv}"
# Where mirrors fetch from; tests point it at a local repository.
git_base="${TARTCI_ARTIFACT_CACHE_GIT_BASE:-https://github.com}"
# Source of the pinned Actions Runner archive (url and digest). Set empty, it is skipped.
runner_sh="${TARTCI_ARTIFACT_CACHE_RUNNER_SH-$(cd "$(dirname "$0")/.." && pwd)/providers/tart-macos/runner.sh}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --url) url="${2:-}"; shift 2;;
    --sha256) sha="${2:-}"; shift 2;;
    --repo) repo="${2:-}"; shift 2;;
    --branch) branch="${2:-}"; shift 2;;
    --older-than-days) days="${2:-}"; shift 2;;
    --compact-above) compact_above="${2:-}"; shift 2;;
    --pins) pins="${2:-}"; shift 2;;
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

pack_count(){ find "$1/objects/pack" -name '*.pack' | wc -l | tr -d ' '; }

# Number of running Tart VMs, or empty when Tart cannot say. Only an explicit 0
# permits a repack: unknown is treated as "a guest may be reading".
running_vms(){
  ${TARTCI_TART_BIN:-tart} list --format json 2>/dev/null \
    | python3 -c 'import json,sys; print(sum(1 for v in json.load(sys.stdin) if v.get("State") == "running" or v.get("Running")))' \
    2>/dev/null || true
}

# Fetch one branch into an existing mirror. Only adds packs (see mirror_config).
fetch_branch(){
  mirror_config "$1"
  git -C "$1" fetch --quiet --no-tags origin "+refs/heads/$2:refs/heads/$2"
}

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

# Store <url>'s bytes as sha256/<sha> when they hash to <sha>. Caller holds
# the lock. Returns non-zero with a message on stderr; never exits, so refresh
# can carry on with the next pin.
add_blob(){
  local url="$1" sha="$2" dest actual
  [[ "$sha" =~ ^[0-9a-f]{64}$ ]] \
    || { printf 'artifact-cache: --sha256 must be 64 lowercase hex digits\n' >&2; return 1; }
  case "$url" in
    https://*) ;;
    *) printf 'artifact-cache: --url must be https: %s\n' "$url" >&2; return 1;;
  esac
  mkdir -p "$dir/sha256"
  dest="$dir/sha256/$sha"
  if [ -f "$dest" ]; then
    [ "$(sha256_of "$dest")" = "$sha" ] || {
      printf 'artifact-cache: %s does not hash to its name; remove it by hand and re-add\n' "$dest" >&2
      return 1
    }
    touch "$dest"
    printf 'artifact-cache: sha256/%s already present (verified, age refreshed)\n' "$sha"
    return 0
  fi
  staging="$(mktemp "$dir/sha256/.staging.XXXXXX")"
  if ! curl --fail --location --silent --show-error --retry 5 --retry-all-errors \
      --retry-delay 5 --connect-timeout 30 --output "$staging" "$url"; then
    rm -f "$staging"; staging=""
    printf 'artifact-cache: download failed: %s\n' "$url" >&2
    return 1
  fi
  actual="$(sha256_of "$staging")"
  if [ "$actual" != "$sha" ]; then
    rm -f "$staging"; staging=""
    printf 'artifact-cache: SHA-256 mismatch for %s: expected %s, got %s\n' "$url" "$sha" "$actual" >&2
    return 1
  fi
  chmod 0644 "$staging"
  mv "$staging" "$dest"
  staging=""
  printf 'artifact-cache: added sha256/%s (%s bytes) from %s\n' \
    "$sha" "$(wc -c <"$dest" | tr -d ' ')" "$url"
}

# Print "<url> <sha256>" for one pin: the release asset <asset> of the
# dependency named <dependency> in <path> on <branch> of the local mirror. The
# manifest is read from the mirror, never from the network or a guest, so the
# digest is exactly the one the repository's own jobs pin and re-verify.
pin_asset(){
  local mirror="$1" pin_branch="$2" path="$3" dependency="$4" asset="$5"
  git -C "$mirror" show "refs/heads/$pin_branch:$path" 2>/dev/null \
    | python3 -c '
import json, re, sys
dependency, asset = sys.argv[1], sys.argv[2]
try:
    manifest = json.load(sys.stdin)
except ValueError:
    sys.exit("manifest is not JSON")
for entry in manifest.get("dependencies", []):
    if isinstance(entry, dict) and str(entry.get("name", "")).lower() == dependency.lower():
        found = ((entry.get("determinism") or {}).get("release_assets") or {}).get(asset)
        if not isinstance(found, dict):
            sys.exit(f"{dependency} has no release asset {asset}")
        url, sha = str(found.get("url", "")), str(found.get("sha256", ""))
        if not url.startswith("https://") or re.fullmatch(r"[0-9a-f]{64}", sha) is None or " " in url:
            sys.exit(f"{dependency} {asset}: unusable url or sha256")
        print(url, sha)
        sys.exit(0)
sys.exit(f"no dependency named {dependency}")
' "$dependency" "$asset"
}

case "$cmd" in
  add)
    [ -n "$url" ] || die "--url is required"
    lock_cache
    add_blob "$url" "$sha" || exit 1
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
      fetch_branch "$mirror" "$branch" || die "fetch of $repo failed"
    fi
    packs="$(pack_count "$mirror")"
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
    running="$(running_vms)"
    [ "$running" = 0 ] \
      || die "refusing to compact while Tart reports running VMs (${running:-unknown}); a guest may be reading these packs"
    lock_cache
    git -C "$mirror" repack -a -d -q
    printf 'artifact-cache: compacted %s to %s pack(s)\n' "$repo" "$(pack_count "$mirror")"
    ;;

  refresh)
    [ -z "$compact_above" ] || [[ "$compact_above" =~ ^[1-9][0-9]*$ ]] \
      || die "--compact-above must be a positive integer"
    # Never create the cache or a mirror: absence means this host did not opt in.
    mirrors=()
    for mirror in "$dir"/git/*/*.git; do
      [ -d "$mirror" ] || continue
      mirrors+=("$mirror")
    done
    if [ "${#mirrors[@]}" -eq 0 ]; then
      printf 'artifact-cache: no mirrors under %s/git; nothing to refresh\n' "$dir"
      exit 0
    fi
    lock_cache
    failed=0
    for mirror in ${mirrors[@]+"${mirrors[@]}"}; do
      name="${mirror#"$dir"/git/}"
      name="${name%.git}"
      if ! [[ "$name" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        printf 'artifact-cache: skipping %s (not an owner/name mirror)\n' "$mirror" >&2
        failed=1
        continue
      fi
      branches="$(git -C "$mirror" for-each-ref --format='%(refname:lstrip=2)' refs/heads 2>/dev/null)" || branches=""
      if [ -z "$branches" ]; then
        printf 'artifact-cache: %s has no branch to refresh\n' "$name" >&2
        failed=1
        continue
      fi
      while IFS= read -r ref; do
        if fetch_branch "$mirror" "$ref"; then
          printf 'artifact-cache: refreshed %s %s at %s\n' "$name" "$ref" \
            "$(git -C "$mirror" rev-parse --short "refs/heads/$ref")"
        else
          printf 'artifact-cache: fetch of %s %s failed\n' "$name" "$ref" >&2
          failed=1
        fi
      done <<<"$branches"
      packs="$(pack_count "$mirror")"
      if [ -n "$compact_above" ] && [ "$packs" -gt "$compact_above" ]; then
        # Asked as late as possible: the window between this answer and the
        # repack is the only one in which a booting guest could see a pack go.
        running="$(running_vms)"
        if [ "$running" = 0 ]; then
          git -C "$mirror" repack -a -d -q
          printf 'artifact-cache: compacted %s from %s to %s pack(s)\n' "$name" "$packs" "$(pack_count "$mirror")"
        else
          printf 'artifact-cache: %s holds %s packs; compaction deferred (running VMs: %s)\n' \
            "$name" "$packs" "${running:-unknown}"
        fi
      fi
    done
    # Pinned archives: one tab-separated line per blob, for mirrored repos only.
    if [ -n "$pins" ] && [ -f "$pins" ]; then
      while IFS=$'\t' read -r pin_repo pin_branch pin_path pin_dep pin_asset || [ -n "$pin_repo" ]; do
        case "$pin_repo" in ''|'#'*) continue;; esac
        mirror="$dir/git/$pin_repo.git"
        # A pin for a repository this host does not mirror is not this host's.
        [ -d "$mirror" ] || continue
        if ! [[ "$pin_branch" =~ ^[A-Za-z0-9_./-]+$ ]] || [ -z "$pin_path" ] \
           || [ -z "$pin_dep" ] || [ -z "$pin_asset" ]; then
          printf 'artifact-cache: malformed pin line for %s in %s\n' "$pin_repo" "$pins" >&2
          failed=1
          continue
        fi
        if ! pinned="$(pin_asset "$mirror" "$pin_branch" "$pin_path" "$pin_dep" "$pin_asset")"; then
          printf 'artifact-cache: pin %s %s %s: could not read it from %s:%s\n' \
            "$pin_repo" "$pin_dep" "$pin_asset" "$pin_branch" "$pin_path" >&2
          failed=1
          continue
        fi
        if ! add_blob "${pinned% *}" "${pinned##* }"; then
          failed=1
        fi
      done <"$pins"
    fi
    # The Actions Runner archive the lane installs into every VM.
    if [ -f "$runner_sh" ]; then
      if archive="$(bash "$runner_sh" --print-runner-archive 2>/dev/null)" \
         && [[ "$archive" =~ ^https://[^\ ]+\ [0-9a-f]{64}$ ]]; then
        add_blob "${archive% *}" "${archive##* }" || failed=1
      else
        printf 'artifact-cache: could not read the pinned runner archive from %s\n' "$runner_sh" >&2
        failed=1
      fi
    fi
    exit "$failed"
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
