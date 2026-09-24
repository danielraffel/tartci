#!/usr/bin/env bash
# Optional read-only pip wheelhouse for disposable macOS guests.
#
# A job that installs Python wheels otherwise reaches PyPI through the guest's
# egress path, so a relay allowlist gap or an index outage fails the job even
# though nothing about the change under test is wrong. A host directory of
# pre-downloaded wheels, shared read-only, lets the guest install with no
# network at all. The job keeps the authority over WHAT it installs (it should
# install with --require-hashes); the wheelhouse only decides where the bytes
# come from.
#
# Populate it with scripts/pip-wheelhouse.sh. Nothing here is required: an
# absent or empty directory means no mount and no declaration.

# Succeeds when $1 is a usable wheelhouse: an absolute directory path Tart can
# share (no ':' or newline) that already holds at least one wheel.
pip_wheelhouse_ready(){
  local dir="${1:-}" wheel
  case "$dir" in
    /*) ;;
    *) return 1;;
  esac
  case "$dir" in
    *:*|*$'\n'*|*$'\r'*) return 1;;
  esac
  [ -d "$dir" ] || return 1
  for wheel in "$dir"/*.whl; do
    [ -f "$wheel" ] && return 0
  done
  return 1
}
