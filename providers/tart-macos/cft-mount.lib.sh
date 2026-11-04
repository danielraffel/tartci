#!/usr/bin/env bash
# Vellum-only Chrome for Testing contract for disposable macOS guests.
#
# A host directory holding exactly one `Google Chrome for Testing.app` is
# verified against a pinned tree digest on the host, then exposed through
# Tart's read-only directory sharing. The guest gets a symlink and a small
# marker file naming the version and digest; neither the host copy nor the
# golden image is modified. Any failure discards the unregistered VM, so a job
# never starts on a missing or mismatched Chrome.

CFT_DIR="${TARTCI_RUNNER_CFT_DIR:-}"
CFT_VERSION="${TARTCI_RUNNER_CFT_VERSION:-}"
CFT_SHA256="${TARTCI_RUNNER_CFT_SHA256:-}"
CFT_MOUNT_ARG=""
CFT_GUEST_APP="/Applications/Google Chrome for Testing.app"
CFT_GUEST_MARKER="/Users/Shared/tartci/chrome-for-testing.env"

verify_cft_mount(){
  [ -n "$CFT_DIR" ] || return 0
  python3 -I "$TARTCI_ROOT/scripts/cft_tree_digest.py" verify "$CFT_DIR" "$CFT_SHA256"
}

configure_cft_mount(){
  [ -n "$CFT_DIR" ] || return 0
  [ "$REPO" = "Generous-Corp/vellum" ] \
    || die "TARTCI_RUNNER_CFT_DIR is restricted to Generous-Corp/vellum"
  case "$CFT_DIR" in
    /*) ;;
    *) die "TARTCI_RUNNER_CFT_DIR must be an absolute path";;
  esac
  case "$CFT_DIR" in
    *:*|*$'\n'*|*$'\r'*) die "TARTCI_RUNNER_CFT_DIR contains an unsupported mount-path character";;
  esac
  case "$CFT_VERSION" in
    [0-9]*.[0-9]*.[0-9]*.[0-9]*) ;;
    *) die "TARTCI_RUNNER_CFT_VERSION must be a four-part Chrome version";;
  esac
  case "$CFT_VERSION" in *[!0-9.]*) die "TARTCI_RUNNER_CFT_VERSION must be a four-part Chrome version";; esac
  case "$CFT_SHA256" in
    *[!0-9a-f]*|'') die "TARTCI_RUNNER_CFT_SHA256 must be 64 lowercase hex characters";;
  esac
  [ "${#CFT_SHA256}" -eq 64 ] || die "TARTCI_RUNNER_CFT_SHA256 must be 64 lowercase hex characters"
  verify_cft_mount \
    || die "Chrome for Testing $CFT_VERSION at $CFT_DIR failed digest verification; not mounting"
  CFT_MOUNT_ARG="chrome-for-testing:$CFT_DIR:ro"
}

install_and_preflight_cft(){
  local ip="$1"
  [ -n "$CFT_MOUNT_ARG" ] || return 0
  verify_cft_mount || return 1
  ssh -n ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "set -e; mounted='/Volumes/My Shared Files/chrome-for-testing/Google Chrome for Testing.app'; target='$CFT_GUEST_APP'; marker='$CFT_GUEST_MARKER'; \
     test -x \"\$mounted/Contents/MacOS/Google Chrome for Testing\"; \
     if [ -L \"\$target\" ]; then \
       [ \"\$(readlink \"\$target\")\" = \"\$mounted\" ]; \
     elif [ -e \"\$target\" ]; then \
       echo 'guest Chrome for Testing target already exists and is not the governed read-only mount' >&2; exit 1; \
     else \
       sudo mkdir -p /Applications; sudo ln -s \"\$mounted\" \"\$target\"; \
     fi; \
     test -x \"\$target/Contents/MacOS/Google Chrome for Testing\"; \
     sudo mkdir -p /Users/Shared/tartci; \
     printf 'TARTCI_CHROME_FOR_TESTING_PATH=%s\nTARTCI_CHROME_FOR_TESTING_VERSION=%s\nTARTCI_CHROME_FOR_TESTING_SHA256=%s\n' \
       \"\$target/Contents/MacOS/Google Chrome for Testing\" '$CFT_VERSION' '$CFT_SHA256' | sudo tee \"\$marker\" >/dev/null"
}
