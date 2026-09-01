#!/usr/bin/env python3
from pathlib import Path
import subprocess,tempfile,unittest
from unittest import mock
import worktree_cleanup as cleanup

class Tests(unittest.TestCase):
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

    def test_porcelain_z_parser(self):
        rows=cleanup.parse_worktrees(b"worktree /a\0HEAD "+b"a"*40+b"\0branch refs/heads/a\0\0worktree /b\0HEAD "+b"b"*40+b"\0detached\0")
        self.assertEqual(rows[0]["branch"],"refs/heads/a"); self.assertTrue(rows[1]["detached"])
    def test_canonical_directory_rejects_symlink_and_escape(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); prefix=root/"code"; prefix.mkdir(); target=prefix/"tree"; target.mkdir(); link=prefix/"link"; link.symlink_to(target)
            self.assertEqual(cleanup.canonical_directory(target,prefix,target.stat().st_dev),target.resolve())
            with self.assertRaises(cleanup.Stop): cleanup.canonical_directory(link,prefix,target.stat().st_dev)
            with self.assertRaises(cleanup.Stop): cleanup.canonical_directory(root,prefix,target.stat().st_dev)
    def test_provider_is_fail_closed(self):
        with self.assertRaises(cleanup.Stop): cleanup.main(["--provider","other","--repo","Generous-Corp/pulp","--primary","/x","--prefix","/y","--main-ref","origin/main","--github-cli","ghapp","--receipt","/r","--lock","/l","--required-bytes","1","--before-free-bytes","0"])
if __name__=="__main__": unittest.main()
