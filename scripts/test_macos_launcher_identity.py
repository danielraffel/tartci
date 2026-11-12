#!/usr/bin/env python3
import testing_support  # noqa: E402
testing_support.skip_module_without_tomllib()
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
        self.temp=tempfile.TemporaryDirectory(); self.app=Path(self.temp.name)/"Launcher.app"; self.exe=self.app/"Contents/MacOS/tartci-launcher"; self.exe.parent.mkdir(parents=True); self.exe.write_bytes(b"mach-o"); self.exe.chmod(0o755); resources=self.app/"Contents/Resources"; (resources/"support").mkdir(parents=True); manifest=b'{"schema":2}\n'; (resources/"support/.tartci-support-manifest.json").write_bytes(manifest); (resources/"lanes.json").write_text('{"schema":1,"lanes":{"studio-pulp-gate":{"environment":{"TART_HOME":"/Volumes/Example/VMs"}}}}'); (resources/"bundle.json").write_text(json.dumps({"schema":1,"source_commit":"a"*40,"support_manifest_sha256":hashlib.sha256(manifest).hexdigest(),"profile_policy_sha256":"b"*64,"tart_home":"/Volumes/Example/VMs"}))
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
    def set_store(self,store):
        meta=self.app/"Contents/Resources/bundle.json"; value=json.loads(meta.read_text()); value["tart_home"]=store; meta.write_text(json.dumps(value))
    def test_any_external_volume_store_is_accepted_and_pinned_by_caller(self):
        self.set_store("/Volumes/Store/VMs")
        with self.inspect(): self.verify()
        with self.inspect(): self.verify(tart_home="/Volumes/Store/VMs")
        # Control: the same bundle against another host's store is refused.
        with self.inspect(),self.assertRaises(identity.IdentityError): self.verify(tart_home="/Volumes/Example/VMs")
    def test_non_external_sealed_store_is_rejected(self):
        for store in ("/Users/x/VMs","/Volumes/Store","/Volumes/Store/../VMs",""):
            with self.subTest(store=store):
                self.set_store(store)
                with self.inspect(),self.assertRaises(identity.IdentityError): self.verify()
        self.set_store("/Volumes/Store/VMs")
        with self.inspect(): self.verify()
    def test_sealed_metadata_bindings_are_checked(self):
        with self.inspect(): self.verify(profile_policy_sha256="b"*64,source_commit="a"*40)
        with self.inspect(),self.assertRaises(identity.IdentityError): self.verify(profile_policy_sha256="c"*64)
        with self.inspect(),self.assertRaises(identity.IdentityError): self.verify(source_commit="d"*40)
    def test_bad_signing_properties_rejected(self):
        variants=(DETAIL.replace("com.danielraffel.tartci.launcher","wrong.id"),DETAIL.replace("95CX6P84C4","WRONGTEAM1"),DETAIL.replace("Developer ID Application: Daniel Raffel (95CX6P84C4)","Apple Development: Daniel"),DETAIL.replace("Developer ID Application: Daniel Raffel (95CX6P84C4)","adhoc"),DETAIL.replace("Authority=Developer ID Application: Daniel Raffel (95CX6P84C4)\n",""),DETAIL.replace("0x10000(runtime)","0x0(none)"))
        for detail in variants:
            with self.subTest(detail=detail),self.inspect(detail),self.assertRaises(identity.IdentityError): self.verify()
        with mock.patch.object(identity,"_run",side_effect=lambda cmd: REQUIREMENT if "-r-" in cmd else ("x86_64\n" if "/usr/bin/lipo" in cmd else DETAIL)),self.assertRaises(identity.IdentityError): self.verify()
class SameBundleTests(unittest.TestCase):
    """An identical live launcher is left in place; anything else is replaced."""
    setUp=Tests.setUp; tearDown=Tests.tearDown
    def copy(self):
        import shutil
        other=Path(self.temp.name)/"Source.app"; shutil.copytree(self.app,other); return other
    def test_an_identical_copy_is_the_same(self):
        self.assertTrue(identity.same_bundle(self.app,self.copy()))
        self.assertTrue(identity.same_bundle(self.app,self.app))
    def test_any_content_mode_or_file_difference_is_not_the_same(self):
        for change in ("content","mode","extra","missing"):
            with self.subTest(change=change):
                other=self.copy(); exe=other/"Contents/MacOS/tartci-launcher"
                if change=="content": exe.write_bytes(b"mach-o2")
                elif change=="mode": exe.chmod(0o700)
                elif change=="extra": (other/"Contents/Resources/extra").write_text("x")
                else: (other/"Contents/Resources/lanes.json").unlink()
                self.assertFalse(identity.same_bundle(self.app,other))
                import shutil; shutil.rmtree(other)
    def test_a_missing_symlinked_or_unreadable_live_bundle_is_not_the_same(self):
        source=self.copy()
        self.assertFalse(identity.same_bundle(Path(self.temp.name)/"absent.app",source))
        link=Path(self.temp.name)/"link.app"; link.symlink_to(self.app)
        self.assertFalse(identity.same_bundle(link,source))
        (self.app/"Contents/Resources/inner").symlink_to(self.exe)
        self.assertFalse(identity.same_bundle(self.app,source))
    def test_the_cli_exit_code_carries_the_answer(self):
        import subprocess
        script=str(Path(identity.__file__))
        same=subprocess.run([sys.executable,script,"same",str(self.app),str(self.copy())],capture_output=True,timeout=60)
        self.assertEqual(0,same.returncode,same.stderr)
        differ=subprocess.run([sys.executable,script,"same",str(self.app),str(Path(self.temp.name)/"absent.app")],capture_output=True,timeout=60)
        self.assertEqual(1,differ.returncode,differ.stderr)
    def test_the_installer_leaves_an_identical_live_launcher_in_place(self):
        body=(Path(identity.__file__).parent/"install_macos_fleet.sh").read_text()
        decide=body.index('macos_launcher_identity.py" same')
        copy=body.index('/usr/bin/ditto --noqtn "$LAUNCH_HELPER_SOURCE/"')
        self.assertLess(decide,copy)
        self.assertIn("launcher_unchanged=1\n  else\n    launcher_candidate=",body)
        swap=body.index('mv "$launcher_candidate" "$launcher_target"')
        guard=body.index('if [ -n "$launcher_target" ] && [ "$launcher_unchanged" = 1 ]; then')
        replace=body.index('elif [ -n "$launcher_target" ]; then', guard)
        self.assertLess(guard,replace); self.assertLess(replace,swap)
        # The unchanged branch neither moves the live bundle aside nor replaces it.
        unchanged=body[guard:replace]
        self.assertNotIn("mv ",unchanged); self.assertNotIn("ditto",unchanged)
        self.assertIn("verify_launcher \"$launcher_target\"",unchanged)


PROFILE={"host":{"home":"/Users/example","tart_home":"/Volumes/Example/VMs"},
         "launch_helper":{"identifier":"com.danielraffel.tartci.launcher","team_id":"95CX6P84C4"}}
class V2Tests(unittest.TestCase):
    """Launcher v2: the stable app and its inputs digest."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); root=Path(self.temp.name)
        self.support=root/"support"; native=self.support/"native/macos/tartci-launcher"; native.mkdir(parents=True)
        (native/"main.swift").write_text("print(1)\n"); (native/"Info.plist").write_text("<plist/>\n")
        self.app=root/"Launcher.app"; exe=self.app/"Contents/MacOS/tartci-launcher"; exe.parent.mkdir(parents=True); exe.write_bytes(b"mach-o"); exe.chmod(0o755)
        res=self.app/"Contents/Resources"; res.mkdir(parents=True)
        (self.app/"Contents/Info.plist").write_text("<plist/>\n")
        (res/"policy.json").write_bytes(identity.v2_policy_bytes(PROFILE))
        (res/"inputs.json").write_text(json.dumps({"schema":1,"sha256":identity.source_inputs_digest(self.support,PROFILE)}))
        sig=self.app/"Contents/_CodeSignature"; sig.mkdir(); (sig/"CodeResources").write_text("x")
    def tearDown(self): self.temp.cleanup()
    def verify(self,**kw):
        with Tests.inspect(self): return identity.verify_v2(self.app,identifier="com.danielraffel.tartci.launcher",team_id="95CX6P84C4",profile=PROFILE,**kw)
    def test_record(self):
        record=self.verify(support_root=self.support)
        self.assertEqual("v2",record["layout"]); self.assertEqual(2,record["schema"])
        self.assertEqual(identity.source_inputs_digest(self.support,PROFILE),record["inputs_sha256"])
        self.assertNotIn("source_commit",record)
    def test_the_policy_is_the_profiles_host_facts_only(self):
        self.assertEqual({"schema":2,"identifier":"com.danielraffel.tartci.launcher","team_id":"95CX6P84C4",
                          "tart_home":"/Volumes/Example/VMs","generations_root":"/Users/example/.local/share/tartci-generations",
                          "home":"/Users/example","pointer":"/Users/example/.config/tartci/launcher-generation.json",
                          "floor":"/Users/example/.config/tartci/launcher-floor.json"},identity.v2_policy(PROFILE))
        # A lane, a source commit or any other profile change leaves it alone.
        changed=json.loads(json.dumps(PROFILE)); changed["lane"]=[{"id":"x"}]; changed["host"]["python"]="/opt/py"
        self.assertEqual(identity.v2_policy_bytes(PROFILE),identity.v2_policy_bytes(changed))
        for bad in ({"tart_home":"/Users/example/VMs"},{"team_id":"abc"},{"home":"relative"}):
            with self.subTest(bad=bad):
                profile=json.loads(json.dumps(PROFILE))
                for key,value in bad.items():
                    (profile["launch_helper"] if key=="team_id" else profile["host"])[key]=value
                with self.assertRaises(identity.IdentityError): identity.v2_policy(profile)
    def test_the_inputs_digest_moves_with_each_input_and_nothing_else(self):
        base=identity.inputs_digest(b"a",b"b",b"c")
        self.assertEqual(base,identity.inputs_digest(b"a",b"b",b"c"))
        for parts in ((b"a2",b"b",b"c"),(b"a",b"b2",b"c"),(b"a",b"b",b"c2"),(b"ab",b"",b"c")):
            with self.subTest(parts=parts): self.assertNotEqual(base,identity.inputs_digest(*parts))
    def test_an_extra_or_missing_file_is_refused(self):
        (self.app/"Contents/Resources/lanes.json").write_text("{}")
        with self.assertRaisesRegex(identity.IdentityError,"extra=\\['Contents/Resources/lanes.json'\\]"): self.verify()
        (self.app/"Contents/Resources/lanes.json").unlink(); (self.app/"Contents/Resources/inputs.json").unlink()
        with self.assertRaisesRegex(identity.IdentityError,"missing"): self.verify()
    def test_a_policy_for_another_profile_is_refused(self):
        other=json.loads(json.dumps(PROFILE)); other["host"]["tart_home"]="/Volumes/Store/VMs"
        (self.app/"Contents/Resources/policy.json").write_bytes(identity.v2_policy_bytes(other))
        with self.assertRaisesRegex(identity.IdentityError,"policy does not match"): self.verify()
    def test_inputs_from_another_source_are_refused(self):
        (self.support/"native/macos/tartci-launcher/main.swift").write_text("print(2)\n")
        with self.assertRaisesRegex(identity.IdentityError,"not built from this support tree"): self.verify(support_root=self.support)
        self.verify()   # without a support tree the record is only read
    def test_v1_signature_rules_still_apply(self):
        with self.assertRaisesRegex(identity.IdentityError,"identifier does not match"):
            with Tests.inspect(self): identity.verify_v2(self.app,identifier="com.example.other",team_id="95CX6P84C4",profile=PROFILE)
    def test_the_verify_v2_command_reads_the_profile_and_refuses(self):
        import subprocess
        profile=self.app.parent/"profile.toml"
        profile.write_text('[host]\nhome = "/Users/example"\ntart_home = "/Volumes/Example/VMs"\n'
                           '[launch_helper]\nidentifier = "com.danielraffel.tartci.launcher"\nteam_id = "95CX6P84C4"\n')
        (self.app/"Contents/Resources/lanes.json").write_text("{}")
        result=subprocess.run([sys.executable,str(Path(identity.__file__)),"verify-v2",str(self.app),
                               "--identifier","com.danielraffel.tartci.launcher","--team-id","95CX6P84C4",
                               "--profile",str(profile),"--support-root",str(self.support)],
                              capture_output=True,text=True,check=False)
        self.assertEqual(1,result.returncode)
        self.assertIn("macos launcher identity rejected: launcher v2 bundle files differ",result.stderr)

if __name__=="__main__": unittest.main()
