#!/usr/bin/env bash
# tartci version: an installed generation answers from its support manifest,
# a checkout from git, and a copy that is neither refuses.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/tartci-version-test.XXXXXX")" && pwd)"
trap 'rm -rf "$tmp"' EXIT
commit=0123456789abcdef0123456789abcdef01234567

# An installed generation: the tree without .git, plus its manifest.
gen="$tmp/generation"
mkdir -p "$gen"
(cd "$ROOT" && tar --exclude .git -cf - .) | tar -xf - -C "$gen"
rm -f "$gen/.tartci-support-manifest.json"
printf '{\n  "schema": 2,\n  "source_commit": "%s",\n  "members": []\n}\n' "$commit" \
  >"$gen/.tartci-support-manifest.json"
out="$(bash "$gen/tartci" version)"
test "$out" = "tartci ${commit:0:12} (generation $gen)"
json="$(bash "$gen/tartci" --version --json)"
case "$json" in *"\"commit\": \"$commit\""*'"source": "generation"'*'"dirty": false'*) ;;
  *) echo "unexpected json: $json" >&2; exit 1 ;; esac

# A copy that is neither: no manifest, no git.
rm "$gen/.tartci-support-manifest.json"
if bash "$gen/tartci" version >"$tmp/out" 2>&1; then
  echo "version answered without a manifest or git: $(cat "$tmp/out")" >&2
  exit 1
fi
grep -q 'cannot tell which commit' "$tmp/out"

# A checkout: git's HEAD, and uncommitted changes are named.
git -C "$gen" init -q
git -C "$gen" -c user.name=t -c user.email=t@t add -A
git -C "$gen" -c user.name=t -c user.email=t@t commit -qm fixture
head="$(git -C "$gen" rev-parse HEAD)"
test "$(bash "$gen/tartci" -V)" = "tartci ${head:0:12} (checkout $gen)"
echo change >>"$gen/README.md"
bash "$gen/tartci" version | grep -q 'with uncommitted changes'

# Bad usage is a usage error, not an answer.
set +e
bash "$gen/tartci" version --bogus >/dev/null 2>&1
rc=$?
set -e
test "$rc" -eq 2
echo 'tartci version: ok'
