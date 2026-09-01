#!/usr/bin/env python3
from pathlib import Path
import hashlib,json,sys,tempfile,unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent)); import macos_launcher_identity as identity
DETAIL="""Identifier=com.danielraffel.tartci.launcher
CodeDirectory v=20500 size=1 flags=0x10000(runtime) hashes=1+0 location=embedded
Authority=Developer ID Application: Daniel Raffel (95CX6P84C4)
TeamIdentifier=95CX6P84C4
"""; REQUIREMENT='designated => identifier "com.danielraffel.tartci.launcher" and anchor apple generic\n'
class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.app=Path(self.temp.name)/"Launcher.app"; self.exe=self.app/"Contents/MacOS/tartci-launcher"; self.exe.parent.mkdir(parents=True); self.exe.write_bytes(b"mach-o"); self.exe.chmod(0o755); resources=self.app/"Contents/Resources"; (resources/"support").mkdir(parents=True); manifest=b'{"schema":2}\n'; (resources/"support/.tartci-support-manifest.json").write_bytes(manifest); (resources/"lanes.json").write_text('{"schema":1,"lanes":{"studio-pulp-gate":{"environment":{"TART_HOME":"/Volumes/Workshop/VMs"}}}}'); (resources/"bundle.json").write_text(json.dumps({"schema":1,"source_commit":"a"*40,"support_manifest_sha256":hashlib.sha256(manifest).hexdigest(),"profile_policy_sha256":"b"*64,"tart_home":"/Volumes/Workshop/VMs"}))
    def tearDown(self): self.temp.cleanup()
    def inspect(self,detail=DETAIL):
        return mock.patch.object(identity,"_run",side_effect=lambda cmd: REQUIREMENT if "-r-" in cmd else ("arm64\n" if "/usr/bin/lipo" in cmd else detail))
    def verify(self,**kw): return identity.verify(self.app,identifier="com.danielraffel.tartci.launcher",team_id="95CX6P84C4",**kw)
    def test_record(self):
        with self.inspect(): record=self.verify()
        self.assertEqual(record["architecture"],"arm64"); self.assertTrue(record["hardened_runtime"]); self.assertEqual(record["lane_ids"],["studio-pulp-gate"])
    def test_symlink_and_digest_rejected(self):
        link=self.app.with_name("link.app"); link.symlink_to(self.app)
        with self.assertRaises(identity.IdentityError): identity.verify(link,identifier="x",team_id="y")
        with self.inspect(),self.assertRaises(identity.IdentityError): self.verify(sha256="0"*64)
    def test_internal_symlink_rejected(self):
        (self.app/"Contents/Resources/link").symlink_to(self.exe)
        with self.inspect(),self.assertRaises(identity.IdentityError): self.verify()
    def test_sealed_metadata_bindings_are_checked(self):
        with self.inspect(): self.verify(profile_policy_sha256="b"*64,source_commit="a"*40)
        with self.inspect(),self.assertRaises(identity.IdentityError): self.verify(profile_policy_sha256="c"*64)
        with self.inspect(),self.assertRaises(identity.IdentityError): self.verify(source_commit="d"*40)
    def test_bad_signing_properties_rejected(self):
        variants=(DETAIL.replace("com.danielraffel.tartci.launcher","wrong.id"),DETAIL.replace("95CX6P84C4","WRONGTEAM1"),DETAIL.replace("Developer ID Application: Daniel Raffel (95CX6P84C4)","Apple Development: Daniel"),DETAIL.replace("Developer ID Application: Daniel Raffel (95CX6P84C4)","adhoc"),DETAIL.replace("Authority=Developer ID Application: Daniel Raffel (95CX6P84C4)\n",""),DETAIL.replace("0x10000(runtime)","0x0(none)"))
        for detail in variants:
            with self.subTest(detail=detail),self.inspect(detail),self.assertRaises(identity.IdentityError): self.verify()
        with mock.patch.object(identity,"_run",side_effect=lambda cmd: REQUIREMENT if "-r-" in cmd else ("x86_64\n" if "/usr/bin/lipo" in cmd else DETAIL)),self.assertRaises(identity.IdentityError): self.verify()
if __name__=="__main__": unittest.main()
