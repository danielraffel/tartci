#!/usr/bin/env bash
# Create the non-admin `ci` account that the persistent Shipyard GitHub
# runners on a fleet Mac should run as, close the login account's home to it,
# and print the steps that move each runner onto it.
#
#   fleet/ops/create-ci-runner-user.sh              # plan (default): runners, home audit, steps
#   sudo fleet/ops/create-ci-runner-user.sh --apply # create `ci`, close the home, print the moves
#
# Why: the persistent runners (~/actions-ci/<name>/, one LaunchAgent each)
# run as the login account. A pull-request job then shares a uid with
# everything that account can read: the fleet SSH keys, the GitHub App
# private key, signing identities and notarization credentials. A job runs
# code from the PR, so any of that is one `cat` away. Running the runners as
# a separate user with no SSH keys and no keychain secrets puts that material
# behind a uid boundary.
#
# A separate uid is not enough on its own. A stock macOS login home is 0750
# owner:staff, and a user made by sysadminctl has staff as its primary group,
# so it could read every group-readable file in that home. So:
#
#   * `ci` gets its own primary group `ci` (not staff, gid = uid, picked free
#     in 401-499), no password at all (Password "*": login is disabled, and
#     no secret ever exists to leak), hidden from the login window, home
#     /Users/ci owned ci:ci 0700. It is created with dscl/dseditgroup only.
#   * the invoking user's home is set to 0700 if group or others can enter
#     it, and ~/.ssh, ~/.config/pulp/secrets and ~/.config/shipyard/github-apps
#     are checked for 0700 directories holding 0600 files (reported, never
#     changed: fix those by hand).
#
# --apply does nothing else. It does NOT touch any runner: the per-runner
# re-registration (stop the old service, `config.sh remove`, `config.sh` as ci
# with a fresh token and the same labels, a LaunchDaemon with UserName=ci) is
# printed, shell-quoted, for an operator to run one runner at a time.
#
# Idempotent: an existing `ci` user is checked (not an admin, not in staff)
# and left alone; a home already at 0700 is left alone.
#
# Exit: 0 done/plan, 2 usage, 3 refused (not root, an unsafe existing `ci`),
#       4 account creation or its verification failed.
#
# Test mode (FLEET_OPS_TEST=1 only): FLEET_OPS_TEST_EUID replaces the
# effective uid, FLEET_OPS_TEST_HOME the invoking user's home,
# FLEET_OPS_TEST_CI_HOME the ci home, and id/dscl/dseditgroup/chown/launchctl
# are taken from PATH instead of their absolute paths.
set -euo pipefail

PROG="create-ci-runner-user"
CI_USER=ci
CI_HOME="/Users/$CI_USER"
CI_NAME="CI Runner"
SECRET_DIRS=".ssh .config/pulp/secrets .config/shipyard/github-apps"
SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

EUID_NOW="$(id -u)"
HOME_OVERRIDE=""
ID=/usr/bin/id
DSCL=/usr/bin/dscl
DSEDITGROUP=/usr/sbin/dseditgroup
CHOWN=/usr/sbin/chown
LAUNCHCTL=/bin/launchctl
if [ "${FLEET_OPS_TEST:-}" = 1 ]; then
  EUID_NOW="${FLEET_OPS_TEST_EUID:-$EUID_NOW}"
  HOME_OVERRIDE="${FLEET_OPS_TEST_HOME:-}"
  CI_HOME="${FLEET_OPS_TEST_CI_HOME:-$CI_HOME}"
  ID=id DSCL=dscl DSEDITGROUP=dseditgroup CHOWN=chown LAUNCHCTL=launchctl
fi

usage() {
  cat <<EOF
usage: $PROG.sh [--plan | --apply]

  --plan    (default) list the persistent runners under ~/actions-ci/*/, audit the
            home and secret directories, and print the steps
  --apply   root: create the hidden, non-admin user '$CI_USER' with its own group (not
            staff), no password and no SSH keys or keychain secrets; set your home
            to 0700 if group/other can enter it; print the per-runner commands

Why: pull-request jobs run code from the PR. On a runner that runs as the login
account, that code shares a uid with the fleet SSH keys, the GitHub App private
key and the signing/notarization material, and can read all of it. Moving the
runners to a separate non-admin '$CI_USER' account puts that material behind a
uid boundary. '$CI_USER' is kept out of staff and the home is closed to 0700 because
a stock home is 0750 owner:staff. Only the account and the home mode are changed;
re-registering each runner is printed for an operator to run, one at a time.

Run --apply with: sudo $SCRIPT_PATH --apply
EOF
}

MODE=plan
case "${1:-}" in
  ""|--plan) ;;
  --apply) MODE=apply ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { usage >&2; exit 2; }

say() { printf '%s: %s\n' "$PROG" "$*"; }
refuse() { printf '%s: refused: %s\n' "$PROG" "$*" >&2; exit 3; }

# One shell word: $1 single-quoted, any ' inside escaped.
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# Octal permission bits of a path (GNU stat first: BSD `stat -c` fails cleanly).
mode_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }

# The account that owns the runners today: whoever ran sudo, else ourselves.
owner_user() {
  if [ "$EUID_NOW" = 0 ] && [ -n "${SUDO_USER:-}" ]; then printf '%s\n' "$SUDO_USER"
  else "$ID" -un; fi
}

owner_home() {
  local u="$1" h=""
  if [ -n "$HOME_OVERRIDE" ]; then printf '%s\n' "$HOME_OVERRIDE"; return; fi
  h="$("$DSCL" . -read "/Users/$u" NFSHomeDirectory 2>/dev/null | sed -n 's/^NFSHomeDirectory: //p' || true)"
  printf '%s\n' "${h:-$HOME}"
}

# A field from a runner's .runner JSON (it starts with a UTF-8 BOM; sed does not care).
runner_field() {
  sed -n "s/.*\"$2\": *\"\\([^\"]*\\)\".*/\\1/p" "$1/.runner" | head -1
}

# Runner names, repos and labels go into printed commands and a plist; accept
# only the characters GitHub itself allows there.
safe_word() { printf '%s' "$1" | grep -Eq '^[A-Za-z0-9._/,-]+$'; }

# The plist of a runner's service: its .service file names it; otherwise
# look for the conventional plist name.
runner_service_plist() {
  local dir="$1" home="$2" name="$3" f
  if [ -s "$dir/.service" ]; then head -1 "$dir/.service"; return; fi
  for f in "$home"/Library/LaunchAgents/actions.runner.*."$name".plist; do
    [ -e "$f" ] && { printf '%s\n' "$f"; return; }
  done
  echo ""
}

# Custom labels of a registered runner, comma-separated, via the GitHub App.
runner_labels() {
  local slug="$1" name="$2"
  command -v ghapp >/dev/null 2>&1 || { echo "unknown (ghapp not on PATH)"; return; }
  [ "$EUID_NOW" != 0 ] || { echo "unknown (run --plan without sudo to read labels)"; return; }
  ghapp api "repos/$slug/actions/runners?per_page=100" \
    --jq ".runners[] | select(.name == \"$name\") | [.labels[] | select(.type == \"custom\") | .name] | join(\",\")" \
    2>/dev/null || echo "unknown (ghapp api failed)"
}

ci_user_exists() { "$ID" "$CI_USER" >/dev/null 2>&1; }
ci_in_group() { "$DSEDITGROUP" -o checkmember -m "$CI_USER" "$1" >/dev/null 2>&1; }
ci_in_staff() { ci_in_group staff || [ "$("$ID" -gn "$CI_USER" 2>/dev/null)" = staff ]; }
ci_group_exists() { "$DSCL" . -read "/Groups/$CI_USER" PrimaryGroupID >/dev/null 2>&1; }

ci_user_state() {
  if ! ci_user_exists; then echo absent
  elif ci_in_group admin; then echo "present, ADMIN (unsafe)"
  elif ci_in_staff; then echo "present, IN STAFF (unsafe: can read group-readable files in login homes)"
  else echo "present, standard, own group"
  fi
}

# First id in 401-499 free as both a uid and a gid.
free_id() {
  local used n
  used=" $("$DSCL" . -list /Users UniqueID | awk '{print $2}' | tr '\n' ' ') $("$DSCL" . -list /Groups PrimaryGroupID | awk '{print $2}' | tr '\n' ' ') "
  for n in $(seq 401 499); do
    case "$used" in *" $n "*) ;; *) echo "$n"; return 0 ;; esac
  done
  return 1
}

create_ci_user() {
  local id
  ci_group_exists && refuse "group '$CI_USER' exists without a '$CI_USER' user; investigate it (dscl . -read /Groups/$CI_USER) before re-running"
  id="$(free_id)" || { say "no free uid/gid in 401-499" >&2; exit 4; }
  if ! {
    "$DSEDITGROUP" -o create -r "$CI_NAME" -i "$id" "$CI_USER" &&
    "$DSCL" . -create "/Users/$CI_USER" &&
    "$DSCL" . -create "/Users/$CI_USER" UniqueID "$id" &&
    "$DSCL" . -create "/Users/$CI_USER" PrimaryGroupID "$id" &&
    "$DSCL" . -create "/Users/$CI_USER" RealName "$CI_NAME" &&
    "$DSCL" . -create "/Users/$CI_USER" UserShell /bin/zsh &&
    "$DSCL" . -create "/Users/$CI_USER" NFSHomeDirectory "$CI_HOME" &&
    "$DSCL" . -create "/Users/$CI_USER" Password '*' &&
    "$DSCL" . -create "/Users/$CI_USER" IsHidden 1 &&
    mkdir -p "$CI_HOME" &&
    "$CHOWN" "$CI_USER:$CI_USER" "$CI_HOME" &&
    chmod 700 "$CI_HOME"
  }; then
    say "creating '$CI_USER' failed part way; undo with: sudo dscl . -delete /Users/$CI_USER; sudo dseditgroup -o delete $CI_USER" >&2
    exit 4
  fi
  ci_user_exists || { say "user '$CI_USER' missing after creation" >&2; exit 4; }
  ci_in_group admin && { say "user '$CI_USER' came out an admin; investigate before using it" >&2; exit 4; }
  ci_in_staff && { say "user '$CI_USER' came out in staff; investigate before using it" >&2; exit 4; }
  say "created user '$CI_USER' (uid/gid $id, group $CI_USER not staff, no password, hidden, home $CI_HOME 0700)"
}

# The invoking user's home must not be enterable by group/other; secret dirs
# must be 0700 with 0600 files. $1=apply fixes the home mode only.
audit_home() {
  local fix="${1:-}" mode rel dir f fmode
  if [ "$OWNER" = root ] || [ ! -d "$OWNER_HOME" ]; then
    say "home audit: skipped (no non-root invoking user; run through sudo from the login account)"
    return
  fi
  mode="$(mode_of "$OWNER_HOME")"
  case "$mode" in
    *00) say "home $OWNER_HOME: $mode (closed to group/other)" ;;
    *)
      if [ "$fix" = apply ]; then
        chmod 700 "$OWNER_HOME"
        say "home $OWNER_HOME: was $mode, group/other-accessible; set to $(mode_of "$OWNER_HOME")"
      else
        say "home $OWNER_HOME: $mode, group/other-accessible; --apply sets it to 700"
      fi ;;
  esac
  for rel in $SECRET_DIRS; do
    dir="$OWNER_HOME/$rel"
    if [ ! -d "$dir" ]; then say "  ~/$rel: absent"; continue; fi
    mode="$(mode_of "$dir")"
    [ "$mode" = 700 ] && say "  ~/$rel: 700" || say "  WARNING ~/$rel: $mode, want 700 (fix by hand: chmod 700 $(q "$dir"))"
    find "$dir" -type f 2>/dev/null | while IFS= read -r f; do
      fmode="$(mode_of "$f")"
      [ "$fmode" = 600 ] || say "  WARNING ${f#"$OWNER_HOME"/}: $fmode, want 600"
    done
  done
}

OWNER="$(owner_user)"
OWNER_HOME="$(owner_home "$OWNER")"
OWNER_UID="$("$ID" -u "$OWNER" 2>/dev/null || echo "<uid of $OWNER>")"

# Runner dirs: ~/actions-ci/<name>/ holding a .runner registration.
RUNNERS=()
for d in "$OWNER_HOME"/actions-ci/*/; do
  [ -f "$d.runner" ] && RUNNERS+=("${d%/}")
done

describe_runners() {
  local dir name url slug plist labels loaded
  say "persistent runners under $OWNER_HOME/actions-ci/ (owner $OWNER):"
  if [ "${#RUNNERS[@]}" -eq 0 ]; then
    echo "  (none found)"
    return
  fi
  for dir in ${RUNNERS[@]+"${RUNNERS[@]}"}; do
    name="$(runner_field "$dir" agentName)"
    url="$(runner_field "$dir" gitHubUrl)"
    slug="${url#https://github.com/}"
    if ! safe_word "$name" || ! safe_word "$slug"; then
      echo "  - $dir: name/repo has unexpected characters; skipped"
      continue
    fi
    plist="$(runner_service_plist "$dir" "$OWNER_HOME" "$name")"
    labels="$(runner_labels "$slug" "$name")"
    loaded=unknown
    if [ -n "$plist" ]; then
      if "$LAUNCHCTL" print "gui/$OWNER_UID/$(basename "$plist" .plist)" >/dev/null 2>&1; then
        loaded=loaded; else loaded="not loaded"; fi
    fi
    echo "  - $name"
    echo "      dir:         $dir"
    echo "      repo:        $slug"
    echo "      labels:      ${labels:-none (custom)}"
    echo "      LaunchAgent: ${plist:-none found} ($loaded)"
  done
}

# Exact commands to move one runner from the login account to `ci`. Every
# value is a single-quoted word; nothing is spliced into an `sh -c` script.
print_moves() {
  local dir name url slug plist label labels labels_arg new daemon agent n=0
  if [ "${#RUNNERS[@]}" -eq 0 ]; then
    say "no runners to move"
    return
  fi
  cat <<EOF

$PROG: per-runner re-registration (NOT performed; run one runner at a time,
$PROG: as $OWNER on this host, and check each comes back online before the next).
$PROG: Toolchains the old runner found under $OWNER_HOME (rustup, cargo, uv, ...)
$PROG: are not visible to $CI_USER; install them for $CI_USER or system-wide first.
EOF
  for dir in ${RUNNERS[@]+"${RUNNERS[@]}"}; do
    name="$(runner_field "$dir" agentName)"
    url="$(runner_field "$dir" gitHubUrl)"
    slug="${url#https://github.com/}"
    if ! safe_word "$name" || ! safe_word "$slug"; then
      say "skipping $dir: name/repo has unexpected characters"
      continue
    fi
    n=$((n + 1))
    plist="$(runner_service_plist "$dir" "$OWNER_HOME" "$name")"
    label="$(basename "${plist:-actions.runner.${slug%%/*}-${slug#*/}.$name.plist}" .plist)"
    agent="${plist:-$OWNER_HOME/Library/LaunchAgents/$label.plist}"
    labels="$(runner_labels "$slug" "$name")"
    case "$labels" in
      "") labels_arg="" ;;
      unknown*) labels_arg=" --labels <same custom labels as today: $labels>" ;;
      *) if safe_word "$labels"; then labels_arg=" --labels $(q "$labels")"
         else labels_arg=" --labels <labels have unexpected characters; copy them from GitHub>"; fi ;;
    esac
    new="$CI_HOME/actions-ci/$name"
    daemon="/Library/LaunchDaemons/$label.plist"
    cat <<EOF

# ── runner $n: $name ($slug) ───────────────────────────────────────────────
# 1. stop the old LaunchAgent (no new jobs start; let a running job finish first)
launchctl bootout $(q "gui/$OWNER_UID/$label")
# 2. remove the old registration
REMOVE_TOKEN="\$(ghapp api -X POST $(q "repos/$slug/actions/runners/remove-token") --jq .token)"
(cd $(q "$dir") && ./config.sh remove --token "\$REMOVE_TOKEN")
mv $(q "$agent") $(q "$agent.disabled")
# 3. copy the runner binaries (no credentials, env or work) to $CI_USER
sudo mkdir -p $(q "$new/_diag")
sudo rsync -a --exclude '.credentials*' --exclude '.runner*' --exclude .service \\
  --exclude .env --exclude .path --exclude _work --exclude _diag $(q "$dir/") $(q "$new/")
sudo chown -R $CI_USER:$CI_USER $(q "$CI_HOME/actions-ci")
# 4. register under $CI_USER with a fresh token and the SAME labels
REG_TOKEN="\$(ghapp api -X POST $(q "repos/$slug/actions/runners/registration-token") --jq .token)"
sudo -u $CI_USER -H /bin/sh -c 'cd "\$1" && shift && exec ./config.sh "\$@"' sh $(q "$new") \\
  --unattended --url $(q "https://github.com/$slug") --token "\$REG_TOKEN" --name $(q "$name") \\
  --work _work$labels_arg --disableupdate --replace
# 5. run it as a LaunchDaemon with UserName=$CI_USER ($CI_USER never logs in, so it has no gui domain)
sudo tee $(q "$daemon") >/dev/null <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>UserName</key><string>$CI_USER</string>
  <key>GroupName</key><string>$CI_USER</string>
  <key>ProgramArguments</key><array><string>$new/runsvc.sh</string></array>
  <key>WorkingDirectory</key><string>$new</string>
  <key>EnvironmentVariables</key><dict><key>ACTIONS_RUNNER_SVC</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>SessionCreate</key><true/>
  <key>StandardOutPath</key><string>$new/_diag/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$new/_diag/launchd.err.log</string>
</dict>
</plist>
PLIST
sudo chown root:wheel $(q "$daemon") && sudo chmod 644 $(q "$daemon")
sudo launchctl bootstrap system $(q "$daemon")
# 6. verify: online with the same labels, and the listener runs as $CI_USER
ghapp api $(q "repos/$slug/actions/runners?per_page=100") --jq $(q ".runners[] | select(.name == \"$name\") | {status, labels: [.labels[].name]}")
ps -o user=,pid=,command= -U $CI_USER | grep Runner.Listener
EOF
  done
}

case "$MODE" in
  plan)
    describe_runners
    say "user '$CI_USER': $(ci_user_state)"
    audit_home
    cat <<EOF
$PROG: --apply would:
  1. create the hidden user '$CI_USER' with its own primary group '$CI_USER' (not staff),
     no password (login disabled), no SSH keys, no keychain items, home $CI_HOME 0700
  2. set $OWNER_HOME to 700 if group/other can enter it (secret dirs: report only)
  3. print, not run, the per-runner re-registration commands below
$PROG: run: sudo $SCRIPT_PATH --apply
EOF
    print_moves
    ;;

  apply)
    [ "$EUID_NOW" = 0 ] || refuse "--apply needs root; run: sudo $SCRIPT_PATH --apply"
    if ci_user_exists; then
      ci_in_group admin && refuse "user '$CI_USER' exists and is an admin; runners must not run as an admin. Remove it from admin (sudo dseditgroup -o edit -d $CI_USER -t user admin) and re-run"
      ci_in_staff && refuse "user '$CI_USER' exists and is in staff, so it can read group-readable files in login homes. Give it its own primary group (or delete it: sudo dscl . -delete /Users/$CI_USER) and re-run"
      say "user '$CI_USER' already exists, not admin, not in staff; leaving it"
    else
      create_ci_user
    fi
    audit_home apply
    print_moves
    ;;
esac
