#!/usr/bin/env bash
# Forge-only Google Chrome contract for disposable macOS guests.
#
# The host application is exposed through Tart's read-only directory sharing.
# Only the disposable guest receives a symlink; neither the host app nor golden
# image is modified.

CHROME_APP_DIR="${TARTCI_RUNNER_CHROME_APP_DIR:-}"
CHROME_MOUNT_ARG=""

configure_chrome_mount(){
  [ -n "$CHROME_APP_DIR" ] || return 0
  [ "$REPO" = "Generous-Corp/forge" ] \
    || die "TARTCI_RUNNER_CHROME_APP_DIR is restricted to Generous-Corp/forge"
  case "$CHROME_APP_DIR" in
    /*) ;;
    *) die "TARTCI_RUNNER_CHROME_APP_DIR must be an absolute path";;
  esac
  case "$CHROME_APP_DIR" in
    *:*|*$'\n'*|*$'\r'*) die "TARTCI_RUNNER_CHROME_APP_DIR contains an unsupported mount-path character";;
  esac
  [ "${CHROME_APP_DIR##*/}" = "Google Chrome.app" ] \
    || die "TARTCI_RUNNER_CHROME_APP_DIR must name Google Chrome.app"
  [ -x "$CHROME_APP_DIR/Contents/MacOS/Google Chrome" ] \
    || die "configured Google Chrome executable is unavailable: $CHROME_APP_DIR/Contents/MacOS/Google Chrome"
  CHROME_MOUNT_ARG="google-chrome:$CHROME_APP_DIR:ro"
}

install_and_preflight_chrome(){
  local ip="$1"
  [ -n "$CHROME_MOUNT_ARG" ] || return 0
  ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "set -e; mounted='/Volumes/My Shared Files/google-chrome'; target='/Applications/Google Chrome.app'; executable='Contents/MacOS/Google Chrome'; \
     test -x \"\$mounted/\$executable\"; \
     if [ -L \"\$target\" ]; then \
       [ \"\$(readlink \"\$target\")\" = \"\$mounted\" ]; \
     elif [ -e \"\$target\" ]; then \
       echo 'guest Google Chrome target already exists and is not the governed read-only mount' >&2; exit 1; \
     else \
       sudo mkdir -p /Applications; sudo ln -s \"\$mounted\" \"\$target\"; \
     fi; \
     test -x \"\$target/\$executable\""
}
