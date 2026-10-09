#!/usr/bin/env python3
"""pf holds an enable reference on a VM host (pf_reference.py, doctor pf_reference)."""
from __future__ import annotations

import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_doctor as fd  # noqa: E402
import pf_reference as pr  # noqa: E402

HEALTHY = "\tstate = not running\n\truns = 765\n\tlast exit code = 0\n"
NO_REFERENCE = "\tstate = spawn scheduled\n\truns = 2621\n\tlast exit code = 3\n"
OTHER_EXIT = "\tstate = spawn scheduled\n\truns = 40\n\tlast exit code = 1\n"
# The holder Daniel installed on m5 on 2026-10-09, as read from the host.
M5_HOLDER = {"Label": "com.danielraffel.pf-enable-ref",
             "ProgramArguments": ["/bin/sh", "-c",
                                  "/sbin/pfctl -E 2>&1 | /usr/bin/sed -n 's/^Token : //p' "
                                  "> /var/run/pf-enable-ref.token"],
             "RunAtLoad": True}


class PfReference(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pfd = self.tmp / "pfd"
        self.pfd.write_text(HEALTHY)
        self.sharing = self.tmp / "sharing"
        self.sharing.write_text("53305")
        launchctl = self.tmp / "launchctl"
        launchctl.write_text(
            "#!/bin/bash\n"
            f"case \"$2\" in *com.apple.pfd) [ -s {str(self.pfd)!r} ] || exit 113;"
            f" cat {str(self.pfd)!r}; exit 0 ;; esac\n"
            "case \"$2\" in *NetworkSharing)\n"
            f"  [ -s {str(self.sharing)!r} ] || exit 113\n"
            f"  printf '\\tstate = running\\n\\tpid = %s\\n' \"$(cat {str(self.sharing)!r})\"; exit 0 ;;\n"
            "esac\nexit 113\n")
        launchctl.chmod(0o755)
        self.daemons = self.tmp / "LaunchDaemons"
        self.daemons.mkdir()
        env = {"TARTCI_VM_DHCP_LAUNCHCTL": str(launchctl),
               "TARTCI_PF_LAUNCHDAEMONS_DIR": str(self.daemons)}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None
                                 else os.environ.__setitem__(k, v) for k, v in saved.items()])

    def finding(self, lanes: int = 3) -> fd.Finding:
        return fd.check_pf_reference(pr.status(lanes))

    def holder(self, name: str, value: dict) -> None:
        (self.daemons / f"{name}.plist").write_bytes(plistlib.dumps(value))

    def test_m5_after_a_reboot_is_a_missing_reference(self):
        self.pfd.write_text(NO_REFERENCE)
        found = self.finding()
        self.assertEqual((found.state, found.code), (fd.PROBLEM, "pf_reference_missing"))
        self.assertIn("pfd exits 3", found.detail)
        self.assertIn("no LaunchDaemon takes a pf reference at boot", found.detail)

    def test_a_healthy_pfd_is_ok_and_names_the_boot_holder(self):
        self.holder("com.danielraffel.pf-enable-ref", M5_HOLDER)
        found = self.finding()
        self.assertEqual((found.state, found.code), (fd.OK, "pf_reference_ok"))
        self.assertIn("taken at boot by com.danielraffel.pf-enable-ref", found.detail)

    def test_no_boot_holder_alone_is_not_a_problem(self):
        # m3, m5s and m1 have no holder and a healthy pfd: they hold pf some other way.
        found = self.finding()
        self.assertEqual((found.state, found.code), (fd.OK, "pf_reference_ok"))
        self.assertEqual(found.facts["pf_reference"]["boot_holders"], [])

    def test_without_vm_lanes_it_is_not_applicable(self):
        # Negative control: the same exit-3 pfd on a host with no VM lanes.
        self.pfd.write_text(NO_REFERENCE)
        found = self.finding(lanes=0)
        self.assertEqual((found.state, found.code), (fd.NOT_APPLICABLE, "pf_not_applicable"))

    def test_an_idle_internet_sharing_still_reports_a_missing_reference(self):
        # InternetSharing is launched on demand: between jobs it is not running
        # (m5 on 2026-10-09), and the next VM still needs pf's reference.
        self.pfd.write_text(NO_REFERENCE)
        self.sharing.write_text("")
        found = self.finding()
        self.assertEqual((found.state, found.code), (fd.PROBLEM, "pf_reference_missing"))
        self.assertIsNone(found.facts["pf_reference"]["sharing_pid"])

    def test_another_exit_code_is_named_separately(self):
        self.pfd.write_text(OTHER_EXIT)
        found = self.finding()
        self.assertEqual((found.state, found.code), (fd.PROBLEM, "pf_pfd_exiting"))

    def test_an_unreadable_pfd_is_unknown_not_ok(self):
        self.pfd.write_text("")
        found = self.finding()
        self.assertEqual((found.state, found.code), (fd.UNKNOWN, "pf_reference_unknown"))

    def test_only_a_run_at_load_pfctl_enable_counts_as_a_holder(self):
        self.holder("a-holder", M5_HOLDER)
        self.holder("b-not-at-load", {**M5_HOLDER, "Label": "b", "RunAtLoad": False})
        self.holder("c-disables", {"Label": "c", "RunAtLoad": True,
                                   "ProgramArguments": ["/sbin/pfctl", "-d"]})
        self.holder("d-direct", {"Label": "d", "RunAtLoad": True,
                                 "ProgramArguments": ["/sbin/pfctl", "-E"]})
        (self.daemons / "e-garbage.plist").write_text("not a plist")
        self.assertEqual(pr.boot_holders(), ["com.danielraffel.pf-enable-ref", "d"])

    def test_the_doctor_runs_it_and_never_raises(self):
        self.pfd.write_text(NO_REFERENCE)
        # With no lane registrations under this home the doctor counts no lanes.
        codes = [f.code for f in fd.collect(home=self.tmp, skip_census=True)
                 if f.check == "pf_reference"]
        self.assertEqual(codes, ["pf_not_applicable"])
        # Control: the same host with three VM lanes registered reads the fault.
        import lease_fit
        from unittest import mock
        self.tmp.joinpath("Library", "LaunchAgents").mkdir(parents=True)
        with mock.patch.object(lease_fit, "lane_records",
                               return_value=([{"lane": n} for n in "abc"], [])):
            codes = [f.code for f in fd.collect(home=self.tmp, skip_census=True)
                     if f.check == "pf_reference"]
        self.assertEqual(codes, ["pf_reference_missing"])
        broken = fd.collect(home=self.tmp, skip_census=True,
                            pf_value={"state": "garbage"})
        self.assertEqual([f.code for f in broken if f.check == "pf_reference"],
                         ["pf_reference_unknown"])


if __name__ == "__main__":
    unittest.main()
