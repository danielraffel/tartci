#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../scripts/shipyard-daemon-health.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/.local/bin" \
  "$WORK/Library/Application Support/shipyard/daemon" \
  "$WORK/Library/Logs"

cat > "$WORK/.local/bin/shipyard" <<'EOF'
#!/usr/bin/env bash
case "$1 $2" in
  "daemon status") echo "daemon running tunnel=inactive repos=—" ;;
  "daemon refresh") exit 0 ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$WORK/.local/bin/shipyard"

# Force the silent-wedge heal path with no existing refresh stamps. Before the
# regression fix, command substitution produced "0\n0" and bash logged
# "integer expression expected".
touch -t 202001010000 "$WORK/Library/Application Support/shipyard/daemon/daemon.log"
output="$(HOME="$WORK" bash "$SCRIPT" 2>&1)"

if [ -n "$output" ]; then
  echo "FAIL: daemon health script wrote unexpected stderr/stdout: $output" >&2
  exit 1
fi

stamp="$WORK/Library/Application Support/shipyard/.health-refresh-stamps"
[ "$(wc -l < "$stamp" | tr -d ' ')" = "1" ] || {
  echo "FAIL: expected exactly one recorded refresh" >&2
  exit 1
}

grep -q 'refresh 0+1/4' "$WORK/Library/Logs/shipyard-daemon-health.log" || {
  echo "FAIL: expected an integer zero refresh count" >&2
  exit 1
}

echo "PASS: empty refresh history emits one integer and heals once"
