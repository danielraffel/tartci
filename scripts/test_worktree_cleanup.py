#!/usr/bin/env python3
from pathlib import Path
import json,subprocess,tempfile,time,types,unittest
from unittest import mock
import worktree_cleanup as cleanup

class Tests(unittest.TestCase):
    def authority_argv(self, root):
        return ["--provider","merged-main-v1","--repo","Generous-Corp/pulp","--primary","/Volumes/Workshop/Code/pulp","--prefix","/Volumes/Workshop/Code","--main-ref","origin/main","--receipt",str(root/"receipt.json"),"--lock",str(root/"lock"),"--required-bytes","1","--before-free-bytes","0"]

    def test_nonblocking_lock_and_cooldown_stop_before_inspection(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); lock=(root/"lock").open("a+"); import fcntl; fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(cleanup.main(self.authority_argv(root)),3); lock.close()
            (root/"receipt.json").write_text("{}")
            with mock.patch.object(cleanup,"fresh_main") as fresh:
                self.assertEqual(cleanup.main(self.authority_argv(root)),4); fresh.assert_not_called()

    def test_apply_cli_enters_apply_path(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); receipt=root/"receipt.json"
            argv=[*self.authority_argv(root),"--apply"]
            with mock.patch.object(cleanup,"fresh_main",return_value="a"*40),mock.patch.object(cleanup,"inspect",return_value=([],[],0)),mock.patch.object(cleanup,"apply_selected") as apply,mock.patch.object(cleanup.shutil,"disk_usage",return_value=types.SimpleNamespace(free=2)):
                self.assertEqual(cleanup.main(argv),0); apply.assert_called_once()

    def test_apply_cli_returns_nonzero_when_final_free_space_is_insufficient(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); argv=[*self.authority_argv(root),"--apply"]
            with mock.patch.object(cleanup,"fresh_main",return_value="a"*40),mock.patch.object(cleanup,"inspect",return_value=([],[],0)),mock.patch.object(cleanup,"apply_selected"),mock.patch.object(cleanup.shutil,"disk_usage",return_value=types.SimpleNamespace(free=0)):
                self.assertEqual(cleanup.main(argv),7)
            self.assertEqual(json.loads((root/"receipt.json").read_text())["retry_admission"],"denied")

    def make_repo(self, root, names):
        primary=root/"primary"; subprocess.run(["git","init","-q",str(primary)],check=True)
        subprocess.run(["git","-C",str(primary),"config","user.email","test@example.com"],check=True); subprocess.run(["git","-C",str(primary),"config","user.name","Test"],check=True)
        (primary/"tracked").write_text("ok"); subprocess.run(["git","-C",str(primary),"add","tracked"],check=True); subprocess.run(["git","-C",str(primary),"commit","-qm","base"],check=True)
        head=subprocess.run(["git","-C",str(primary),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip(); selected=[]
        for name in names:
            path=root/name; subprocess.run(["git","-C",str(primary),"branch",name,head],check=True); subprocess.run(["git","-C",str(primary),"worktree","add","-q",str(path),name],check=True)
            selected.append((path,head,f"refs/heads/{name}",1,{"path":str(path),"status":"candidate"}))
        return primary,head,selected

    def test_apply_stops_at_target_and_retains_branch_registry_proof(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); primary,head,selected=self.make_repo(root,["one","two"]); receipt=root/"receipt.json"
            args=types.SimpleNamespace(primary=primary,prefix=root,receipt=receipt,required_bytes=50,max_bytes=100,max_trees=8,timeout=30)
            record={"removals":[],"dispositions":[item[4] for item in selected]}
            with mock.patch.object(cleanup,"inspect",return_value=(selected,[],2)), mock.patch.object(cleanup,"observations",return_value=""), mock.patch.object(cleanup.shutil,"disk_usage",side_effect=[types.SimpleNamespace(free=0),types.SimpleNamespace(free=100)]):
                cleanup.apply_selected(args,selected,record,head)
            self.assertFalse(selected[0][0].exists()); self.assertTrue(selected[1][0].exists())
            final=json.loads(receipt.read_text()); self.assertEqual(len(final["removals"]),1); self.assertEqual(final["removals"][0]["branch_retained_at"],head)
            self.assertEqual(subprocess.run(["git","-C",str(primary),"show-ref","--verify","--hash","refs/heads/one"],text=True,capture_output=True,check=True).stdout.strip(),head)

    def test_apply_receipt_preserves_completed_removal_when_later_revalidation_stops(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); primary,head,selected=self.make_repo(root,["one","two"]); receipt=root/"receipt.json"
            args=types.SimpleNamespace(primary=primary,prefix=root,receipt=receipt,required_bytes=999,max_bytes=100,max_trees=8,timeout=30)
            record={"removals":[],"dispositions":[item[4] for item in selected]}
            with mock.patch.object(cleanup,"inspect",side_effect=[(selected,[],2),cleanup.Stop("changed")]), mock.patch.object(cleanup,"observations",return_value=""), mock.patch.object(cleanup.shutil,"disk_usage",return_value=types.SimpleNamespace(free=0)):
                with self.assertRaises(cleanup.Stop): cleanup.apply_selected(args,selected,record,head)
            progress=json.loads(receipt.read_text()); self.assertEqual([row["path"] for row in progress["removals"]],[str(selected[0][0])]); self.assertNotIn("current_removal",progress)

    def test_activity_after_quarantine_claim_rolls_back_without_removal(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); primary,head,selected=self.make_repo(root,["one"]); path=selected[0][0]; receipt=root/"receipt.json"
            args=types.SimpleNamespace(primary=primary,prefix=root,receipt=receipt,required_bytes=999,max_bytes=100,max_trees=8,timeout=30)
            record={"removals":[],"dispositions":[selected[0][4]]}
            with mock.patch.object(cleanup,"inspect",return_value=(selected,[],1)), mock.patch.object(cleanup,"observations",side_effect=lambda target,_: {str(target)}), mock.patch.object(cleanup.shutil,"disk_usage",return_value=types.SimpleNamespace(free=0)), self.assertRaises(cleanup.Stop):
                cleanup.apply_selected(args,selected,record,head)
            self.assertTrue(path.is_dir()); self.assertEqual(cleanup.registered(cleanup.inventory(primary),path)["HEAD"],head)
            progress=json.loads(receipt.read_text()); self.assertNotIn("current_removal",progress); self.assertEqual(progress["aborted_removals"][0]["reason"],"activity_after_claim")

    def test_restart_before_move_clears_aborted_intent_and_after_move_rolls_back(self):
        for move_first in (False,True):
            with self.subTest(move_first=move_first), tempfile.TemporaryDirectory() as td:
                root=Path(td).resolve(); primary,head,selected=self.make_repo(root,["one"]); path,_,branch,size,_=selected[0]; quarantine=root/"quarantine"; receipt=root/"receipt.json"
                if move_first: subprocess.run(["git","-C",str(primary),"worktree","move",str(path),str(quarantine)],check=True)
                receipt.write_text(json.dumps({"current_removal":{"path":str(path),"quarantine_path":str(quarantine),"head":head,"branch":branch,"size_bytes":size,"phase":"quarantined" if move_first else "intent_recorded"},"removals":[]}))
                args=types.SimpleNamespace(primary=primary,prefix=root,receipt=receipt,timeout=30)
                self.assertTrue(cleanup.reconcile_pending(args)); self.assertTrue(path.is_dir()); self.assertFalse(quarantine.exists())
                self.assertNotIn("current_removal",json.loads(receipt.read_text()))

    def test_restart_reconciles_durable_pending_removal_intent(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); primary,head,selected=self.make_repo(root,["one"]); path,_,branch,size,_=selected[0]; receipt=root/"receipt.json"
            quarantine=root/"quarantine"; receipt.write_text(json.dumps({"current_removal":{"path":str(path),"quarantine_path":str(quarantine),"head":head,"branch":branch,"size_bytes":size,"phase":"removal_in_progress"},"removals":[]}))
            subprocess.run(["git","-C",str(primary),"worktree","remove",str(path)],check=True)
            args=types.SimpleNamespace(primary=primary,prefix=root,receipt=receipt)
            self.assertTrue(cleanup.reconcile_pending(args)); record=json.loads(receipt.read_text())
            self.assertTrue(record["removals"][0]["reconciled_after_restart"]); self.assertNotIn("current_removal",record)

    def test_git_ignores_malicious_path_and_core_fsmonitor(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); primary,head,_=self.make_repo(root,[]); marker=root/"executed"; hook=root/"fsmonitor"
            hook.write_text(f"#!/bin/sh\ntouch {marker}\n"); hook.chmod(0o755); subprocess.run(["git","-C",str(primary),"config","core.fsmonitor",str(hook)],check=True)
            hostile=root/"bin"; hostile.mkdir(); fake=hostile/"git"; fake.write_text(f"#!/bin/sh\ntouch {marker}\nexit 99\n"); fake.chmod(0o755)
            with mock.patch.dict("os.environ",{"PATH":str(hostile)}),mock.patch.object(cleanup,"observations",return_value=""):
                cleanup.inspect(primary,root,head,1024**3,8,30)
            self.assertFalse(marker.exists())

    def test_executable_and_transport_bearing_local_git_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); primary,head,_=self.make_repo(root,[]); marker=root/"executed"; hostile=root/"hostile"
            hostile.write_text(f"#!/bin/sh\ntouch {marker}\n"); hostile.chmod(0o755)
            for key,value in (("core.sshCommand",str(hostile)),("filter.evil.process",str(hostile)),("http.sslVerify","false"),("http.https://github.com/.proxy","http://127.0.0.1:9"),("remote.origin.proxy","http://127.0.0.1:9")):
                subprocess.run(["git","-C",str(primary),"config",key,value],check=True)
                with self.assertRaises(cleanup.Stop): cleanup.inspect(primary,root,head,1024**3,8,30)
                subprocess.run(["git","-C",str(primary),"config","--unset-all",key],check=True)
            self.assertFalse(marker.exists())

    def test_per_worktree_transport_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); primary,_,selected=self.make_repo(root,["one"]); candidate=selected[0][0]
            subprocess.run(["git","-C",str(primary),"config","extensions.worktreeConfig","true"],check=True)
            subprocess.run(["git","-C",str(candidate),"config","--worktree","http.sslVerify","false"],check=True)
            local=subprocess.run(["git","-C",str(candidate),"config","--local","--name-only","--get-regexp",r"^http\..*"],capture_output=True)
            self.assertEqual(local.returncode,1)
            with self.assertRaises(cleanup.Stop): cleanup.reject_executable_git_config(candidate,30)

    def test_fresh_main_uses_only_canonical_https_and_validates_sha(self):
        commands=[]
        def response(command,**_):
            commands.append(command)
            if "rev-parse" in command: value="b"*40+"\n"
            else: value=""
            return subprocess.CompletedProcess(command,0,value,"")
        with mock.patch.object(cleanup,"run",side_effect=response),mock.patch.object(cleanup,"reject_executable_git_config"):
            self.assertEqual(cleanup.fresh_main(Path("/primary"),"Generous-Corp/pulp","origin/main",30),"b"*40)
        fetch=next(command for command in commands if "fetch" in command)
        self.assertIn("https://github.com/Generous-Corp/pulp.git",fetch); self.assertNotIn("origin",fetch)

    def test_git_subprocess_environment_ignores_global_and_system_config(self):
        with mock.patch.object(cleanup.subprocess,"run",return_value=subprocess.CompletedProcess([],0,"","")) as invoked:
            cleanup.run(["git","status"])
        env=invoked.call_args.kwargs["env"]
        self.assertEqual(env["GIT_CONFIG_GLOBAL"],"/dev/null"); self.assertEqual(env["GIT_CONFIG_SYSTEM"],"/dev/null")
        self.assertEqual(env["GIT_NO_REPLACE_OBJECTS"],"1")
        self.assertEqual(env["PATH"],"/usr/bin:/bin:/usr/sbin:/sbin")

    def test_replace_refs_cannot_falsify_ancestry_and_legacy_grafts_stop(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); repo=root/"repo"
            subprocess.run(["git","init","-q",str(repo)],check=True)
            subprocess.run(["git","-C",str(repo),"config","user.email","test@example.com"],check=True)
            subprocess.run(["git","-C",str(repo),"config","user.name","Test"],check=True)
            (repo/"tracked").write_text("main"); subprocess.run(["git","-C",str(repo),"add","tracked"],check=True); subprocess.run(["git","-C",str(repo),"commit","-qm","main"],check=True)
            main=subprocess.run(["git","-C",str(repo),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip()
            subprocess.run(["git","-C",str(repo),"checkout","--orphan","unrelated"],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            subprocess.run(["git","-C",str(repo),"rm","-rf","."],check=True,stdout=subprocess.DEVNULL); (repo/"other").write_text("orphan"); subprocess.run(["git","-C",str(repo),"add","other"],check=True); subprocess.run(["git","-C",str(repo),"commit","-qm","orphan"],check=True)
            orphan=subprocess.run(["git","-C",str(repo),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip()
            subprocess.run(["git","-C",str(repo),"replace","--graft",main,orphan],check=True)
            self.assertEqual(subprocess.run(["git","-C",str(repo),"merge-base","--is-ancestor",orphan,main]).returncode,0)
            self.assertNotEqual(cleanup.run(["git","-C",str(repo),"merge-base","--is-ancestor",orphan,main],check=False).returncode,0)
            grafts=repo/".git/info/grafts"; grafts.write_text(f"{main} {orphan}\n")
            with self.assertRaises(cleanup.Stop): cleanup.reject_executable_git_config(repo,30)

    def test_lsof_observation_tracks_live_cwd_after_worktree_quarantine_move(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); primary,_,selected=self.make_repo(root,["one"]); path=selected[0][0]; quarantine=root/"quarantine"
            holder=subprocess.Popen(["/bin/sh","-c",f"cd {str(path)!r}; exec sleep 30"])
            try:
                time.sleep(0.2)
                subprocess.run(["git","-C",str(primary),"worktree","move",str(path),str(quarantine)],check=True)
                self.assertTrue(cleanup.observation_active(cleanup.observations(quarantine,30),quarantine))
            finally:
                holder.terminate(); holder.wait(timeout=5)

    def test_inspect_selects_only_clean_merged_attached_branch(self):
        with tempfile.TemporaryDirectory() as td:
            prefix=Path(td).resolve(); primary=prefix/"primary"
            subprocess.run(["git","init","-q",str(primary)],check=True)
            subprocess.run(["git","-C",str(primary),"config","user.email","test@example.com"],check=True)
            subprocess.run(["git","-C",str(primary),"config","user.name","Test"],check=True)
            (primary/"tracked").write_text("ok"); subprocess.run(["git","-C",str(primary),"add","tracked"],check=True); subprocess.run(["git","-C",str(primary),"commit","-qm","base"],check=True)
            head=subprocess.run(["git","-C",str(primary),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip()
            tree=prefix/"candidate"; subprocess.run(["git","-C",str(primary),"branch","merged",head],check=True); subprocess.run(["git","-C",str(primary),"worktree","add","-q",str(tree),"merged"],check=True)
            with mock.patch.object(cleanup,"observations",return_value=""):
                selected,_,_=cleanup.inspect(primary,prefix,head,1024**3,8,30)
                self.assertEqual([item[0] for item in selected],[tree])
                (tree/"untracked").write_text("dirty")
                selected,dispositions,_=cleanup.inspect(primary,prefix,head,1024**3,8,30)
                self.assertEqual(selected,[]); self.assertIn("dirty",[row.get("reason") for row in dispositions])

    def test_inspect_rejects_ignored_entries(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); primary,head,selected=self.make_repo(root,["candidate"]); tree=selected[0][0]
            (tree/".gitignore").write_text("build/\n"); subprocess.run(["git","-C",str(tree),"add",".gitignore"],check=True); subprocess.run(["git","-C",str(tree),"commit","-qm","ignore"],check=True)
            head=subprocess.run(["git","-C",str(tree),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip(); (tree/"build").mkdir(); (tree/"build/output").write_text("x")
            with mock.patch.object(cleanup,"observations",return_value=""):
                chosen,dispositions,_=cleanup.inspect(primary,root,head,1024**3,8,30)
            self.assertEqual(chosen,[]); self.assertIn("ignored_or_dirty",[row.get("reason") for row in dispositions])

    def test_registry_matching_is_exact_not_path_prefix(self):
        rows=[{"worktree":"/tmp/tree-long","HEAD":"a"*40,"branch":"refs/heads/x"}]
        self.assertIsNone(cleanup.registered(rows,Path("/tmp/tree")))

    def test_activity_paths_use_exact_containment_not_prefix_or_command_text(self):
        target=Path("/tmp/tree")
        self.assertFalse(cleanup.observation_active({"/tmp/tree-long/file"},target))
        self.assertTrue(cleanup.observation_active({"/tmp/tree/file"},target))

    def test_inspect_excludes_detached_locked_and_active_worktrees(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); primary,head,selected=self.make_repo(root,["clean","locked"])
            detached=root/"detached"; subprocess.run(["git","-C",str(primary),"worktree","add","-q","--detach",str(detached),head],check=True)
            subprocess.run(["git","-C",str(primary),"worktree","lock",str(root/"locked")],check=True)
            with mock.patch.object(cleanup,"observations",return_value={str(root/"clean")}):
                chosen,dispositions,_=cleanup.inspect(primary,root,head,1024**3,8,30)
            self.assertEqual(chosen,[]); reasons={row.get("reason") for row in dispositions}
            self.assertTrue({"active_observation","locked","detached"}.issubset(reasons))

    def test_inspect_excludes_dirty_submodule_state(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); sub=root/"sub-source"; subprocess.run(["git","init","-q",str(sub)],check=True)
            subprocess.run(["git","-C",str(sub),"config","user.email","test@example.com"],check=True); subprocess.run(["git","-C",str(sub),"config","user.name","Test"],check=True)
            (sub/"value").write_text("one"); subprocess.run(["git","-C",str(sub),"add","value"],check=True); subprocess.run(["git","-C",str(sub),"commit","-qm","sub"],check=True)
            primary,_,_=self.make_repo(root,[ ]); subprocess.run(["git","-c","protocol.file.allow=always","-C",str(primary),"submodule","add","-q",str(sub),"module"],check=True); subprocess.run(["git","-C",str(primary),"commit","-qam","submodule"],check=True)
            head=subprocess.run(["git","-C",str(primary),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip(); tree=root/"candidate"; subprocess.run(["git","-C",str(primary),"branch","with-submodule",head],check=True); subprocess.run(["git","-C",str(primary),"worktree","add","-q",str(tree),"with-submodule"],check=True)
            subprocess.run(["git","-c","protocol.file.allow=always","-C",str(tree),"submodule","update","--init","-q"],check=True); (tree/"module/value").write_text("dirty")
            with mock.patch.object(cleanup,"observations",return_value=""):
                selected,dispositions,_=cleanup.inspect(primary,root,head,1024**3,8,30)
            self.assertEqual(selected,[]); self.assertTrue(any(row.get("reason") in {"dirty","dirty_submodule"} for row in dispositions if row["path"]==str(tree)))

    def test_inspect_stops_on_common_directory_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); primary,head,selected=self.make_repo(root,["candidate"]); tree=selected[0][0]; original=cleanup.run
            def mismatched(command,**kwargs):
                if command[0]=="git" and "--git-common-dir" in command and str(tree) in command:
                    return subprocess.CompletedProcess(command,0,"/different/common\n","")
                return original(command,**kwargs)
            with mock.patch.object(cleanup,"observations",return_value=""),mock.patch.object(cleanup,"run",side_effect=mismatched),self.assertRaises(cleanup.Stop):
                cleanup.inspect(primary,root,head,1024**3,8,30)

    def test_porcelain_z_parser(self):
        rows=cleanup.parse_worktrees(b"worktree /a\0HEAD "+b"a"*40+b"\0branch refs/heads/a\0\0worktree /b\0HEAD "+b"b"*40+b"\0detached\0")
        self.assertEqual(rows[0]["branch"],"refs/heads/a"); self.assertTrue(rows[1]["detached"])
    def test_canonical_directory_rejects_symlink_and_escape(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); prefix=root/"code"; prefix.mkdir(); target=prefix/"tree"; target.mkdir(); link=prefix/"link"; link.symlink_to(target)
            self.assertEqual(cleanup.canonical_directory(target,prefix,target.stat().st_dev),target.resolve())
            with self.assertRaises(cleanup.Stop): cleanup.canonical_directory(link,prefix,target.stat().st_dev)
            with self.assertRaises(cleanup.Stop): cleanup.canonical_directory(root,prefix,target.stat().st_dev)
            with self.assertRaises(cleanup.Stop): cleanup.canonical_directory(target,prefix,target.stat().st_dev+1)

    def test_authority_roots_reject_primary_or_prefix_symlinks(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); prefix=root/"Code"; prefix.mkdir(); primary=prefix/"pulp"; primary.mkdir()
            cleanup.validate_authority_roots(primary,prefix)
            primary_link=prefix/"pulp-link"; primary_link.symlink_to(primary,target_is_directory=True)
            with self.assertRaises(cleanup.Stop): cleanup.validate_authority_roots(primary_link,prefix)
            prefix_link=root/"Code-link"; prefix_link.symlink_to(prefix,target_is_directory=True)
            with self.assertRaises(cleanup.Stop): cleanup.validate_authority_roots(prefix_link,prefix_link/"pulp")
    def test_provider_is_fail_closed(self):
        with self.assertRaises(cleanup.Stop): cleanup.main(["--provider","other","--repo","Generous-Corp/pulp","--primary","/x","--prefix","/y","--main-ref","origin/main","--receipt","/r","--lock","/l","--required-bytes","1","--before-free-bytes","0"])
if __name__=="__main__": unittest.main()
