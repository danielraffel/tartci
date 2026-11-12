#!/usr/bin/env bash
# Optional read-only host artifact cache for disposable macOS guests.
#
# Every gate job on an ephemeral VM otherwise re-downloads the same bytes: the
# repository history its checkout and provenance steps fetch, and pinned
# archives such as a browser build or a prebuilt library. A host directory
# shared read-only lets the guest take those bytes from local disk instead.
#
# Layout (populated by scripts/artifact-cache.sh, never by a guest):
#   git/<owner>/<repo>.git   bare mirror of the default branch; a job may use
#                            it as a Git alternate object store
#   sha256/<hex>             a file whose SHA-256 is <hex>; a job looks up the
#                            digest it already pins and re-verifies the bytes
#
# It is an accelerator, never an authority. A consumer must fall back to its
# own download when an entry is absent, and must keep checking the digest it
# pins. Nothing here is required: an absent or empty directory means no mount
# and no declaration, so a host that never ran the sync boots exactly as before.

# Succeeds when $1 is a usable cache: an absolute directory path Tart can share
# (no ':' or newline) that already holds at least one git mirror or one blob.
artifact_cache_ready(){
  local dir="${1:-}" entry
  case "$dir" in
    /*) ;;
    *) return 1;;
  esac
  case "$dir" in
    *:*|*$'\n'*|*$'\r'*) return 1;;
  esac
  [ -d "$dir" ] || return 1
  for entry in "$dir"/sha256/* "$dir"/git/*/*.git; do
    [ -e "$entry" ] && return 0
  done
  return 1
}
