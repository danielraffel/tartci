#!/usr/bin/env python3
"""The installed host-vitals sensor follows Pulp origin/main, and drift is shown.

On 2026-09-29 m1, m3 and m5 all ran the 2026-09-25 copy (no fseventsd
reading) and m5studio had none; `pool status` read `fseventsd: UNKNOWN`.
"""

from __future__ import annotations

import testing_support  # noqa: E402
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import host_vitals_sensor as hvs  # noqa: E402
import pulp_reapers as pr  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "checkout" / "tools" / "scripts"
        self.bin = self.tmp / "bin"
        self.plist = self.tmp / "LaunchAgents" / f"{hvs.LABEL}.plist"
        self.src.mkdir(parents=True)
        for name in hvs.SENSOR_FILES:
            (self.src / name).write_text(f"# {name} with fseventsd\n")
        # Pulp's installer copies the scripts to bin and writes the plist; this
        # stand-in does the same against the fixture paths.
        (self.src / hvs.INSTALLER).write_text(
            "#!/bin/bash\nset -e\nsrc=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
            f"mkdir -p {self.bin} {self.plist.parent}\n"
            + "".join(f"cp \"$src/{n}\" {self.bin}/{n}\n" for n in hvs.SENSOR_FILES)
            + f"echo plist > {self.plist}\n")

    def install_old(self) -> None:
        self.bin.mkdir(parents=True, exist_ok=True)
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_text("plist")
        for name in hvs.SENSOR_FILES:
            (self.bin / name).write_text(f"# {name} from 2026-09-25\n")

    def drift(self):
        return hvs.drift(self.src, self.bin, self.plist)

    def refresh(self, fix=True, **kw):
        return hvs.refresh(self.src, fix, bin_dir=self.bin, plist=self.plist, **kw)


class DriftTests(Fixture):
    def test_states(self) -> None:
        self.assertEqual(self.drift()["state"], "not_installed")
        self.install_old()
        value = self.drift()
        self.assertEqual((value["state"], value["files"]), ("drift", list(hvs.SENSOR_FILES)))
        for name in hvs.SENSOR_FILES:
            shutil.copy(self.src / name, self.bin / name)
        self.assertEqual(self.drift()["state"], "current")
        (self.src / "host_vitals.sh").unlink()
        self.assertEqual(self.drift()["state"], "source_missing")
        # A host without the reclaim checkout (a CI box, a non-fleet Mac) is
        # not told to install anything, and pool status stays quiet.
        absent = hvs.drift(self.tmp / "none", self.bin, self.plist)
        self.assertEqual(absent["state"], "not_applicable")
        self.assertIsNone(hvs.status_line(absent))

    @testing_support.requires_tomllib
    def test_pool_status_line_only_when_it_is_not_origin_main(self) -> None:
        import macos_fleet_lanes as lanes
        self.install_old()
        self.assertIn("host-vitals sensor: DRIFT", hvs.status_line(self.drift()))
        self.assertIn("NOT INSTALLED", hvs.status_line({"state": "not_installed"}))
        self.assertIsNone(hvs.status_line({"state": "current"}))
        with mock.patch.object(hvs, "status_line", return_value="host-vitals sensor: DRIFT (x)"):
            lines = lanes.host_vitals_summary(self.tmp / "absent.json")["lines"]
        self.assertEqual(lines[-1], "host-vitals sensor: DRIFT (x)")
        self.assertIn("fseventsd", lines[0])


class RefreshTests(Fixture):
    def test_a_drifted_sensor_is_reinstalled_from_origin_main(self) -> None:
        self.install_old()
        out = self.refresh()
        self.assertEqual(out["state"], "refreshed", out)
        self.assertEqual(self.drift()["state"], "current")

    def test_a_host_without_the_sensor_gets_it(self) -> None:
        out = self.refresh()
        self.assertEqual(out["state"], "refreshed", out)
        self.assertTrue(self.plist.exists())

    def test_dry_run_and_current_do_nothing(self) -> None:
        self.install_old()
        self.assertEqual(self.refresh(fix=False)["state"], "would_refresh")
        self.assertEqual(self.drift()["state"], "drift")
        self.refresh()
        calls = []
        self.assertEqual(self.refresh(runner=lambda *a, **k: calls.append(a))["state"], "current")
        self.assertEqual(calls, [])

    def test_a_failing_installer_is_reported(self) -> None:
        self.install_old()
        (self.src / hvs.INSTALLER).write_text("#!/bin/bash\necho denied >&2\nexit 3\n")
        out = self.refresh()
        self.assertEqual(out["state"], "refresh_failed")
        self.assertIn("installer exit 3", out["detail"])


class ReclaimWiringTests(Fixture):
    @testing_support.requires_tomllib
    def test_the_reclaim_pass_refreshes_even_without_a_worktree_root(self) -> None:
        # m5studio: worktrees_root did not exist yet, so the pass returned
        # before it ever materialized origin/main.
        profile = self.tmp / "profile.toml"
        repo = self.tmp / "pulp"
        repo.mkdir()
        profile.write_text(
            "[reclaim]\npulp_worktree_builds = true\n"
            f'repo = "{repo}"\nworktrees_root = "{self.tmp / "missing"}"\n')
        checkout = self.tmp / "checkout"
        seen = []
        with mock.patch.object(pr, "materialize", return_value=(checkout, "origin/main x", "x")), \
                mock.patch.object(pr, "tmp_worktrees", return_value={}), \
                mock.patch.object(hvs, "refresh",
                                  side_effect=lambda src, fix: seen.append((src, fix)) or
                                  {"state": "refreshed"}):
            report = pr.run(fix=True, profile=profile, state_dir=self.tmp / "state")
        self.assertIn("is not a directory", report["error"])
        self.assertEqual(seen, [(checkout / "tools" / "scripts", True)])
        self.assertEqual(report["host_vitals_sensor"]["state"], "refreshed")

    @testing_support.requires_tomllib
    def test_every_fleet_host_runs_the_pass_that_refreshes_it(self) -> None:
        profiles = sorted((ROOT / "profiles").glob("*-macos-fleet.toml"))
        self.assertEqual([p.name for p in profiles],
                         ["m1-macos-fleet.toml", "m3-macos-fleet.toml", "m5-macos-fleet.toml",
                          "m5studio-macos-fleet.toml"])
        for path in profiles:
            with self.subTest(profile=path.name):
                settings, why = pr.load_settings(path)
                self.assertIsNotNone(settings, why)


if __name__ == "__main__":
    unittest.main()
