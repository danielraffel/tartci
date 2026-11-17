#!/usr/bin/env python3
"""tcc_risk_lint: LaunchAgents that can raise a privacy prompt are reported,
and stable ones are not.

Every flag has a control on the same instrument: the same plist with the
interpreter, signature or path changed to a stable one is not flagged. No
test touches /Volumes; the fakes record every readlink and codesign call so
the tests also prove the lint never resolves a /Volumes path.
"""

from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tartci_launchd_watchdog as wd  # noqa: E402
import tcc_risk_lint as lint  # noqa: E402

NOW = 1_800_000_000.0
CELLAR = "/opt/homebrew/Cellar/python@3.12/3.12.13_2/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
LAUNCHER = "/Users/example/.local/libexec/TartCILauncher.app/Contents/MacOS/tartci-launcher"
FRAMEWORK = "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"


class Fakes:
    def __init__(self, links=None, signatures=None, files=None):
        self.links = dict(links or {})
        self.signatures = dict(signatures or {})
        self.files = dict(files or {})
        self.readlinks: list[str] = []
        self.codesigns: list[str] = []
        self.reads: list[str] = []

    def readlink(self, path):
        self.readlinks.append(path)
        if path in self.links:
            return self.links[path]
        raise OSError(22, "not a link")

    def codesign(self, path):
        self.codesigns.append(path)
        return self.signatures.get(path, {"signed": True, "adhoc": False, "team": "TEAM123456"})

    def read_text(self, path):
        self.reads.append(path)
        if path in self.files:
            return self.files[path]
        raise OSError(2, "absent")

    def read_head(self, path):
        self.reads.append(path)
        if path in self.files:
            return self.files[path].encode()
        raise OSError(2, "absent")

    def kwargs(self):
        return {"readlink": self.readlink, "read_text": self.read_text, "read_head": self.read_head}


class LintTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.agents = self.home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)

    def agent(self, label, argv, **extra):
        value = {"Label": label, "ProgramArguments": argv, "StartInterval": 300, **extra}
        (self.agents / f"{label}.plist").write_bytes(plistlib.dumps(value))

    def scan(self, fakes, loaded=None, budget=lint.MAX_CODESIGN_PER_PASS, entries=None):
        cache = lint.CodesignCache(entries, codesign=fakes.codesign, mtime=lambda p: 1.0,
                                   budget=budget)
        return lint.scan(self.agents, cache, loaded, **fakes.kwargs()), cache

    def kinds(self, flagged, label):
        return sorted(f["kind"] for f in flagged.get(label, []))


class FlagTests(LintTestCase):
    def test_a_cellar_interpreter_reached_through_bin_is_flagged(self) -> None:
        # The host-attestation agent as m3 ran it: /opt/homebrew/bin/python3.
        fakes = Fakes(links={"/opt/homebrew/bin/python3":
                             "../Cellar/python@3.12/3.12.13_2/Frameworks/Python.framework/"
                             "Versions/3.12/bin/python3.12"})
        self.agent("com.example.cellar", ["/opt/homebrew/bin/python3", "/Users/example/a.py"])
        flagged, _ = self.scan(fakes)
        self.assertEqual(self.kinds(flagged, "com.example.cellar"), ["cellar"])
        self.assertIn(CELLAR, flagged["com.example.cellar"][0]["reason"])

    def test_a_cellar_interpreter_behind_an_opt_directory_link_is_flagged(self) -> None:
        # An intermediate component is the link, not the binary itself.
        fakes = Fakes(links={"/opt/homebrew/opt/python@3.12": "../Cellar/python@3.12/3.12.13_2"})
        self.agent("com.example.opt", ["/opt/homebrew/opt/python@3.12/bin/python3.12"])
        flagged, _ = self.scan(fakes)
        self.assertEqual(self.kinds(flagged, "com.example.opt"), ["cellar"])

    def test_a_venv_python_is_judged_by_its_base_interpreter(self) -> None:
        fakes = Fakes(files={"/Users/example/venv/pyvenv.cfg":
                             "home = /opt/homebrew/Cellar/python@3.12/3.12.13_2/bin\n"})
        self.agent("com.example.venv", ["/Users/example/venv/bin/python3"])
        flagged, _ = self.scan(fakes)
        self.assertEqual(self.kinds(flagged, "com.example.venv"), ["cellar"])
        self.assertIn("venv", flagged["com.example.venv"][0]["reason"])

    def test_an_adhoc_binary_is_flagged(self) -> None:
        tool = "/Users/example/.local/bin/tool"
        fakes = Fakes(signatures={tool: {"signed": True, "adhoc": True, "team": None}})
        self.agent("com.example.adhoc", [tool, "serve"])
        flagged, _ = self.scan(fakes)
        self.assertEqual(self.kinds(flagged, "com.example.adhoc"), ["adhoc"])
        self.assertEqual(fakes.codesigns, [tool])

    def test_a_script_is_judged_by_its_shebang_interpreter(self) -> None:
        script = "/Users/example/.local/bin/tick"
        fakes = Fakes(files={script: "#!/opt/homebrew/Cellar/python@3.12/3.12.13_2/bin/python3\n"})
        self.agent("com.example.script", [script])
        flagged, _ = self.scan(fakes)
        self.assertEqual(self.kinds(flagged, "com.example.script"), ["cellar"])

    def test_a_volumes_script_or_cwd_is_flagged_without_touching_volumes(self) -> None:
        # com.pulp.fleet.prewall on m3: a stable interpreter, script and cwd on the store.
        fakes = Fakes(links={"/usr/local/bin/python3": FRAMEWORK})
        self.agent("com.pulp.fleet.prewall",
                   ["/usr/local/bin/python3", "/Volumes/Example/Code/tools/prewall_warning.py"],
                   WorkingDirectory="/Volumes/Example/Code/tools")
        self.agent("com.example.program-on-volumes", ["/Volumes/Example/bin/run"])
        flagged, _ = self.scan(fakes)
        self.assertEqual(self.kinds(flagged, "com.pulp.fleet.prewall"), ["volumes", "volumes"])
        self.assertEqual(self.kinds(flagged, "com.example.program-on-volumes"), ["volumes"])
        touched = fakes.readlinks + fakes.codesigns + fakes.reads
        self.assertFalse([p for p in touched if p.startswith("/Volumes")], touched)

    def test_a_link_that_lands_on_volumes_stops_there(self) -> None:
        fakes = Fakes(links={"/Users/example/bin/run": "/Volumes/Example/bin/run"})
        self.agent("com.example.link", ["/Users/example/bin/run"])
        flagged, _ = self.scan(fakes)
        self.assertEqual(self.kinds(flagged, "com.example.link"), ["volumes"])
        self.assertFalse([p for p in fakes.readlinks if p.startswith("/Volumes")])
        self.assertEqual(fakes.codesigns, [])


class PassTests(LintTestCase):
    def test_a_launcher_run_job_passes_even_with_a_volumes_cwd(self) -> None:
        fakes = Fakes()
        self.agent("com.example.launcher", [LAUNCHER, "lane"],
                   WorkingDirectory="/Volumes/Example/VMs")
        self.agent("com.example.shipyard",
                   ["/Users/example/.local/libexec/ShipyardLauncher.app/Contents/MacOS/shipyard"])
        flagged, _ = self.scan(fakes)
        self.assertEqual(flagged, {})
        self.assertEqual(fakes.codesigns, [])

    def test_a_usr_bin_python3_job_passes_without_a_signature_read(self) -> None:
        fakes = Fakes()
        self.agent("com.example.apple", ["/usr/bin/python3", "/Users/example/job.py"],
                   WorkingDirectory="/Users/example")
        self.agent("com.example.bash", ["/bin/bash", "/Users/example/.local/bin/tartci"])
        flagged, _ = self.scan(fakes)
        self.assertEqual(flagged, {})
        self.assertEqual(fakes.codesigns, [])

    def test_a_developer_id_framework_python_passes(self) -> None:
        # Control for the Cellar flag: the pinned interpreter the installer now uses.
        fakes = Fakes(signatures={FRAMEWORK: {"signed": True, "adhoc": False, "team": "BMM5U3QVKW"}})
        self.agent("com.example.framework", [FRAMEWORK, "/Users/example/a.py"])
        flagged, _ = self.scan(fakes)
        self.assertEqual(flagged, {})

    def test_an_unloaded_agent_is_not_scanned(self) -> None:
        fakes = Fakes(signatures={"/Users/example/t": {"signed": True, "adhoc": True}})
        self.agent("com.example.off", ["/Users/example/t"])
        flagged, _ = self.scan(fakes, loaded={"com.example.other"})
        self.assertEqual(flagged, {})
        self.assertEqual(fakes.codesigns, [])


class BudgetTests(LintTestCase):
    def test_codesign_reads_are_capped_and_cached_by_path_and_mtime(self) -> None:
        tools = [f"/Users/example/bin/t{i}" for i in range(3)]
        fakes = Fakes(signatures={t: {"signed": True, "adhoc": True} for t in tools})
        for i, tool in enumerate(tools):
            self.agent(f"com.example.t{i}", [tool])
        flagged, cache = self.scan(fakes, budget=1)
        self.assertEqual(len(fakes.codesigns), 1)
        verdicts = sorted(f["verdict"] for fs in flagged.values() for f in fs)
        self.assertEqual(verdicts, ["flag", "pending", "pending"])
        # Next pass: the cached one costs nothing, one more is read.
        flagged, cache = self.scan(fakes, budget=1, entries=cache.entries)
        self.assertEqual(len(fakes.codesigns), 2)
        verdicts = sorted(f["verdict"] for fs in flagged.values() for f in fs)
        self.assertEqual(verdicts, ["flag", "flag", "pending"])


class WatchdogPassTests(LintTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.state = self.home / "state"
        self.issues: list[tuple[str, str]] = []
        self.closed: list[str] = []

    def run_pass(self, now, fakes, loaded):
        def issue(title, body):
            self.issues.append((title, body))
            return 0, "41"

        def close(number):
            self.closed.append(number)
            return 0, ""
        return lint.watchdog_pass(now, home=str(self.home), state_dir=self.state,
                                  loaded=loaded, codesign=fakes.codesign, host="m3",
                                  issue=issue, close=close, **fakes.kwargs())

    def events(self):
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_warns_records_an_incident_and_closes_it_when_fixed(self) -> None:
        fakes = Fakes(signatures={"/Users/example/t": {"signed": True, "adhoc": True}})
        self.agent("com.example.adhoc", ["/Users/example/t"])
        lines = self.run_pass(NOW, fakes, {"com.example.adhoc"})
        self.assertTrue(any("WARN tcc-risk com.example.adhoc: " in line for line in lines), lines)
        self.assertEqual(len(self.issues), 1)
        self.assertIn('"class":"tcc_risk"', self.issues[0][1])
        self.assertEqual([e["event"] for e in self.events()], ["tcc_risk"])
        # Inside the interval: nothing re-read, nothing repeated.
        self.assertEqual(self.run_pass(NOW + 60, fakes, {"com.example.adhoc"}), [])
        # Same finding a pass later: one issue, one event, still warned.
        lines = self.run_pass(NOW + lint.INTERVAL_S, fakes, {"com.example.adhoc"})
        self.assertTrue(lines)
        self.assertEqual(len(self.issues), 1)
        self.assertEqual(len(self.events()), 1)
        # Fixed: the incident closes.
        self.agent("com.example.adhoc", ["/usr/bin/python3", "/Users/example/a.py"])
        self.assertEqual(self.run_pass(NOW + 2 * lint.INTERVAL_S, fakes, {"com.example.adhoc"}), [])
        self.assertEqual(self.closed, ["41"])

    def test_the_watchdog_runs_it_and_never_raises(self) -> None:
        source = Path(wd.__file__).read_text()
        self.assertIn("tcc_risk_line = tcc_risk_pass()", source[source.index("def main("):])
        line = wd.tcc_risk_pass(NOW, home=str(self.home), state_dir=self.state,
                                loaded=None, codesign=lambda p: 1 / 0, host="m3")
        # An empty LaunchAgents dir: nothing to say, and no exception escapes.
        self.assertIsNone(line)


if __name__ == "__main__":
    unittest.main()
