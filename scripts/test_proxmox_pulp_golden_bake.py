#!/usr/bin/env python3
"""Behavioral safety tests for the additive Proxmox m153 golden baker."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "providers/proxmox-linux/bake-pulp-golden.sh"
PULP_SHA = "21fbc9da9214d4e6279fa2e8b4e70df9bed8662a"


class ProxmoxPulpGoldenBakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        self.state = self.root / "state"
        self.receipts = self.root / "receipts"
        self.receipts.mkdir()
        self.parent_config = "name: pulp-linux-golden-warm4\ntemplate: 1"
        self.parent_digest = hashlib.sha256(
            (self.parent_config + "\n").encode()
        ).hexdigest()
        self.guest_receipt = self.root / "guest-receipt.json"
        self.write_guest_receipt()
        self.write_stub(
            "qm",
            r'''#!/bin/bash
set -euo pipefail
printf 'qm %s\n' "$*" >>"$TARTCI_TEST_LOG"
case "$1" in
  config)
    if [ "$2" = 9005 ]; then
      if [ "${TARTCI_TEST_BAD_PARENT:-0}" = 1 ]; then
        printf 'name: mutable-parent\n'
      else
        printf '%s\n' "$TARTCI_TEST_PARENT_CONFIG"
      fi
    elif [ "${TARTCI_TEST_EXISTING:-0}" = 1 ] || [ -e "$TARTCI_TEST_STATE/exists" ]; then
      printf 'name: candidate\n'
      if [ -e "$TARTCI_TEST_STATE/templated" ]; then printf 'template: 1\n'; fi
    else
      exit 2
    fi
    ;;
  clone) touch "$TARTCI_TEST_STATE/exists" ;;
  start) touch "$TARTCI_TEST_STATE/running" ;;
  guest)
    nonce="$(printf '%s' "$*" | grep -Eo '[0-9a-f]{64}' | head -1)"
    [ -n "$nonce" ]
    printf '%s' "$nonce" >"$TARTCI_TEST_STATE/binding"
    ;;
  status) printf 'status: stopped\n' ;;
  template) touch "$TARTCI_TEST_STATE/templated" ;;
  *) exit 3 ;;
esac
''',
        )
        self.write_stub(
            "pvesh",
            r'''#!/bin/bash
set -euo pipefail
printf 'pvesh %s\n' "$*" >>"$TARTCI_TEST_LOG"
if [ "${TARTCI_TEST_VMID_QUERY_FAIL:-0}" = 1 ]; then
  printf 'permission or cluster failure\n' >&2
  exit 1
fi
if [ "${TARTCI_TEST_EXISTING:-0}" = 1 ]; then
  printf 'VM 9006 already exists\n' >&2
  exit 2
fi
printf '"%s"\n' "${TARTCI_TEST_NEXTID:-9006}"
''',
        )
        self.write_stub(
            "ssh",
            r'''#!/bin/bash
set -euo pipefail
printf 'ssh %s\n' "$*" >>"$TARTCI_TEST_LOG"
joined="$*"
if [[ "$joined" == *' cat /run/tartci-pulp-golden-9006.binding'* ]]; then
  if [ "${TARTCI_TEST_WRONG_PEER:-0}" = 1 ]; then
    printf 'wrong-peer'
  else
    cat "$TARTCI_TEST_STATE/binding"
  fi
  exit 0
fi
if [[ "$joined" == *'cat "$HOME/.config/tartci/pulp-render-generation.json"'* ]]; then
  cat "$TARTCI_TEST_GUEST_RECEIPT"
  exit 0
fi
if [[ "$joined" == *PULP_REPOSITORY=* ]]; then
  cat >/dev/null
  [ "${TARTCI_TEST_GUEST_FAIL:-0}" != 1 ]
  exit
fi
if [[ "$joined" == *'cat >'* ]]; then
  cat >/dev/null
fi
if [[ "$joined" == *'bash -s'* ]]; then
  cat >/dev/null
  [ "${TARTCI_TEST_CLEANUP_FAIL:-0}" != 1 ]
  exit
fi
exit 0
''',
        )
        self.write_stub("flock", "#!/bin/bash\nexit 0\n")
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.bin}:{self.env['PATH']}",
                "TARTCI_QM_BIN": str(self.bin / "qm"),
                "TARTCI_PVESH_BIN": str(self.bin / "pvesh"),
                "TARTCI_SSH_BIN": str(self.bin / "ssh"),
                "TARTCI_TEST_LOG": str(self.log),
                "TARTCI_TEST_STATE": str(self.state),
                "TARTCI_TEST_PARENT_CONFIG": self.parent_config,
                "TARTCI_TEST_GUEST_RECEIPT": str(self.guest_receipt),
                "PULP_GOLDEN_RECEIPT_DIR": str(self.receipts),
                "PULP_GOLDEN_LOCK_PATH": str(self.root / "bake.lock"),
            }
        )
        self.state.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def write_guest_receipt(self, **changes: object) -> None:
        receipt: dict[str, object] = {
            "schema": 1,
            "status": "pass",
            "pulp": {
                "repository": "https://github.com/Generous-Corp/pulp",
                "commit": PULP_SHA,
                "manifest_sha256": "0" * 64,
            },
            "parent": {
                "kind": "proxmox-template",
                "identity": "9005",
                "digest_sha256": self.parent_digest,
            },
            "skia_dawn": {
                "release": "chrome/m153",
                "skia_commit": "1" * 40,
                "built_dawn": "2" * 40,
                "platform": "linux-x64",
                "asset_sha256": "3" * 64,
                "generation_receipt_sha256": "4" * 64,
                "capability_result_sha256": "5" * 64,
                "capabilities": [
                    "SkLogHandler.GetInstance.execute",
                    "SkLogHandler.SetInstance.compile-link-only",
                    "Graphite.ContextOptions.fExecutor.execute",
                ],
                "limitations": [
                    "SkLogHandler.SetInstance is not executed because it installs process-global first-install-wins state",
                ],
                "probe_count": 1,
            },
            "v8": {
                "disposition": "baked-provider-only",
                "version": "v8-m153-test",
                "platform": "linux-x64",
                "asset_sha256": "7" * 64,
                "generation_receipt_sha256": "6" * 64,
                "runtime_policy": "provider-cached; Pulp defaults to QuickJS",
            },
        }
        receipt.update(changes)
        self.guest_receipt.write_text(json.dumps(receipt))

    def run_bake(self, *extra: str, env_changes: dict[str, str] | None = None):
        env = dict(self.env)
        env.update(env_changes or {})
        return subprocess.run(
            [
                "/bin/bash",
                str(SCRIPT),
                "--new-vmid",
                "9006",
                "--guest-host",
                "192.0.2.6",
                *extra,
            ],
            text=True,
            capture_output=True,
            env=env,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_success_templates_only_after_guest_proof_and_host_receipt(self) -> None:
        result = self.run_bake()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        clone = next(i for i, call in enumerate(calls) if "qm clone 9005 9006" in call)
        binding = next(i for i, call in enumerate(calls) if "qm guest exec 9006" in call)
        bake = next(i for i, call in enumerate(calls) if "PULP_REPOSITORY=" in call)
        receipt = next(
            i for i, call in enumerate(calls) if "pulp-render-generation.json" in call and "cat >" not in call
        )
        cleanup = max(i for i, call in enumerate(calls) if call.startswith("ssh ") and "bash -s" in call)
        template = next(i for i, call in enumerate(calls) if "qm template 9006" in call)
        self.assertLess(clone, bake)
        self.assertLess(clone, binding)
        self.assertLess(binding, bake)
        self.assertLess(bake, receipt)
        self.assertLess(receipt, cleanup)
        self.assertLess(cleanup, template)
        self.assertTrue(any("ci@192.0.2.6" in call for call in calls))
        self.assertFalse(any("runner@" in call for call in calls))
        self.assertNotIn("destroy", "\n".join(calls))
        self.assertNotIn("qm template 9005", "\n".join(calls))
        output = self.receipts / "pulp-linux-template-9006.json"
        self.assertTrue(output.is_file())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o444)

    def test_existing_vmid_fails_before_clone(self) -> None:
        result = self.run_bake(env_changes={"TARTCI_TEST_EXISTING": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not authoritatively prove", result.stderr)
        self.assertFalse(any("qm clone" in call for call in self.calls()))

    def test_vmid_permission_or_transient_failure_is_not_absence(self) -> None:
        result = self.run_bake(env_changes={"TARTCI_TEST_VMID_QUERY_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not authoritatively prove", result.stderr)
        self.assertFalse(any("qm clone" in call for call in self.calls()))

    def test_allocator_must_confirm_the_exact_requested_vmid(self) -> None:
        result = self.run_bake(env_changes={"TARTCI_TEST_NEXTID": "9007"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not confirm exact unused VMID 9006", result.stderr)
        self.assertFalse(any("qm clone" in call for call in self.calls()))

    def test_qga_binding_is_owned_for_canonical_nonroot_consumer(self) -> None:
        result = self.run_bake(
            env_changes={"PULP_GOLDEN_GUEST_USER": "runner"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("runner@" in call for call in self.calls()))
        qga = "\n".join(self.calls())
        self.assertIn("qm guest exec 9006", qga)
        self.assertIn("id -u -- ci", qga)
        self.assertIn("id -g -- ci", qga)
        self.assertIn('chown "$uid:$gid"', qga)
        self.assertIn("chmod 0400", qga)
        self.assertIn('mv -f -- "$tmp" "$target"', qga)

    def test_mutable_parent_fails_before_clone(self) -> None:
        result = self.run_bake(env_changes={"TARTCI_TEST_BAD_PARENT": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an immutable template", result.stderr)
        self.assertFalse(any("qm clone" in call for call in self.calls()))

    def test_guest_verification_failure_retains_candidate_without_templating(self) -> None:
        result = self.run_bake(env_changes={"TARTCI_TEST_GUEST_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        calls = "\n".join(self.calls())
        self.assertIn("qm clone 9005 9006", calls)
        self.assertNotIn("qm template 9006", calls)
        self.assertNotIn("destroy", calls)
        self.assertIn("retained as a non-template", result.stderr)
        self.assertFalse((self.receipts / "pulp-linux-template-9006.json").exists())

    def test_wrong_ssh_peer_fails_before_bake_or_templating(self) -> None:
        result = self.run_bake(env_changes={"TARTCI_TEST_WRONG_PEER": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match candidate VMID 9006", result.stderr)
        calls = "\n".join(self.calls())
        self.assertNotIn("PULP_REPOSITORY=", calls)
        self.assertNotIn("qm template 9006", calls)
        self.assertIn("retained as a non-template", result.stderr)

    def test_wrong_receipt_fails_before_templating(self) -> None:
        self.write_guest_receipt(skia_dawn={"release": "chrome/m149"})
        result = self.run_bake()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("golden receipt validation failed: skia_release", result.stderr)
        self.assertFalse(any("qm template 9006" in call for call in self.calls()))

    def test_incomplete_receipt_fails_before_templating(self) -> None:
        self.write_guest_receipt(pulp={"commit": PULP_SHA})
        result = self.run_bake()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pulp_repository", result.stderr)
        self.assertIn("pulp_manifest", result.stderr)
        self.assertFalse(any("qm template 9006" in call for call in self.calls()))

    def test_warm_build_matches_protected_cache_and_example_policy(self) -> None:
        source = SCRIPT.read_text()
        for expected in (
            "export CCACHE_COMPILERCHECK=content",
            "export CCACHE_NODEPEND=true",
            "export CCACHE_SLOPPINESS=time_macros",
            "-DPULP_BUILD_EXAMPLES=OFF",
        ):
            self.assertIn(expected, source)

    def test_identity_scrub_failure_leaves_only_candidate_receipt(self) -> None:
        result = self.run_bake(env_changes={"TARTCI_TEST_CLEANUP_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            (self.receipts / "pulp-linux-template-9006.candidate.json").is_file()
        )
        self.assertFalse((self.receipts / "pulp-linux-template-9006.json").exists())
        self.assertFalse(any("qm template 9006" in call for call in self.calls()))

    def test_prior_receipt_is_never_overwritten(self) -> None:
        output = self.receipts / "pulp-linux-template-9006.json"
        output.write_text("preserve me")
        result = self.run_bake()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_text(), "preserve me")
        self.assertFalse(any("qm clone" in call for call in self.calls()))

    def test_script_has_no_vm_delete_or_parent_mutation_path(self) -> None:
        source = SCRIPT.read_text()
        self.assertNotIn("qm destroy", source)
        self.assertNotIn('template "$PARENT_VMID"', source)
        self.assertNotIn('set "$PARENT_VMID"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
