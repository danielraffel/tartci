#!/usr/bin/env bash
# Idempotently remove the retired Orchard controller/worker LaunchAgents.
set -euo pipefail

usage() {
  cat <<'EOF'
usage: disable_orchard.sh [--apply]

Prints the no-Orchard cleanup plan by default. --apply boots out both retired
LaunchAgents, removes their installed user plists, and verifies neither label
remains loaded. It is safe to repeat.
EOF
}

APPLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

DOMAIN="gui/$(id -u)"
LABELS=(
  "com.danielraffel.tartci.orchard-controller"
  "com.danielraffel.tartci.orchard-worker"
)

echo "no-Orchard cleanup plan:"
for label in "${LABELS[@]}"; do
  echo "  label=$DOMAIN/$label"
  echo "  plist=$HOME/Library/LaunchAgents/$label.plist"
done
if [ "$APPLY" != "1" ]; then
  echo "  mode=dry-run (pass --apply to remove)"
  exit 0
fi

for label in "${LABELS[@]}"; do
  launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1 || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done

failed=0
for label in "${LABELS[@]}"; do
  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    echo "retired Orchard LaunchAgent is still loaded: $DOMAIN/$label" >&2
    failed=1
  fi
  if [ -e "$HOME/Library/LaunchAgents/$label.plist" ]; then
    echo "retired Orchard plist still exists: $HOME/Library/LaunchAgents/$label.plist" >&2
    failed=1
  fi
done
[ "$failed" = "0" ] || exit 1
echo "Orchard controller and worker LaunchAgents are absent"
