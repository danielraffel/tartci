#!/usr/bin/env python3
"""Conservative, bounded cleanup of merged Pulp Git worktrees."""
from __future__ import annotations
import argparse, datetime as dt, fcntl, hashlib, json, os, re, shutil, signal, stat, subprocess, tempfile, time
from pathlib import Path

SCHEMA = 1
GIT="/usr/bin/git"; DU="/usr/bin/du"; PS="/bin/ps"; LSOF="/usr/sbin/lsof"
GHAPP="/Users/danielraffel/.local/bin/ghapp"
CMUX="/Applications/cmux.app/Contents/Resources/bin/cmux"
GIT_CONFIG=["-c","core.fsmonitor=false","-c","core.hooksPath=/dev/null","-c","core.pager=cat","-c","pager.status=false"]
GIT_ENV={"PATH":"/usr/bin:/bin:/usr/sbin:/sbin","GIT_CONFIG_GLOBAL":"/dev/null","GIT_CONFIG_SYSTEM":"/dev/null","GIT_CONFIG_NOSYSTEM":"1","GIT_TERMINAL_PROMPT":"0","GIT_OPTIONAL_LOCKS":"0","LANG":"C"}
ALLOWED={GIT,DU,PS,LSOF,GHAPP,CMUX}
PRIVATE_DIGESTS={}
class Stop(RuntimeError): pass

def run(command, *, env=None, timeout=30, check=True):
    executable=command[0]
    if executable=="git": command=[GIT,*GIT_CONFIG,*command[1:]]; executable=GIT
    if executable not in ALLOWED: raise Stop(f"external executable is not allowed: {executable}")
    identity=None
    if executable in (GHAPP,CMUX):
        target=Path(executable)
        info=target.lstat()
        if target.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o022 or not os.access(target,os.X_OK) or str(target.resolve(strict=True))!=executable: raise Stop(f"private executable identity is invalid: {executable}")
        expected=PRIVATE_DIGESTS.get(executable)
        if not expected or hashlib.sha256(target.read_bytes()).hexdigest()!=expected: raise Stop(f"private executable digest is not approved: {executable}")
        identity=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
    controlled={**GIT_ENV,"HOME":os.environ.get("HOME","/Users/danielraffel"),"USER":os.environ.get("USER","danielraffel")}
    result=subprocess.run(command,text=True,capture_output=True,env=controlled,timeout=timeout,check=False)
    if identity is not None:
        info=Path(executable).lstat()
        if identity!=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns): raise Stop(f"private executable changed during execution: {executable}")
    if check and result.returncode: raise Stop(f"command failed ({result.returncode}): {' '.join(command)}: {result.stderr.strip()}")
    return result

def parse_worktrees(raw: bytes):
    rows=[]; row={}
    for field in raw.split(b"\0"):
        if not field:
            if row: rows.append(row); row={}
            continue
        text=field.decode("utf-8")
        key,_,value=text.partition(" ")
        if key=="worktree" and row: rows.append(row); row={}
        row[key]=value if value else True
    if row: rows.append(row)
    return rows

def inventory(primary:Path,timeout:int=30):
    result=run(["git","-C",str(primary),"worktree","list","--porcelain","-z"],timeout=timeout)
    rows=parse_worktrees(result.stdout.encode())
    if any(not isinstance(row.get("worktree"),str) or not re.fullmatch(r"[0-9a-f]{40}",str(row.get("HEAD",""))) for row in rows): raise Stop("worktree inventory is ambiguous")
    if len({row["worktree"] for row in rows})!=len(rows): raise Stop("worktree inventory contains duplicate paths")
    return rows

def registered(rows,path:Path):
    matches=[row for row in rows if row.get("worktree")==str(path)]
    if len(matches)>1: raise Stop(f"duplicate worktree registration: {path}")
    return matches[0] if matches else None

def reject_executable_git_config(primary:Path,timeout:int):
    result=run(["git","-C",str(primary),"config","--local","--name-only","--get-regexp",r"^(core\.sshcommand|credential\..*helper|credential\.helper|filter\..*\.(process|clean|smudge)|core\.hookspath|url\..*\.insteadof|include(if)?\..*|diff\.external|diff\..*\.command|merge\..*\.driver|alias\..*)$"],timeout=timeout,check=False)
    if result.returncode not in (0,1): raise Stop("local Git config inspection failed")
    if result.stdout.strip(): raise Stop("repository local config contains executable-bearing settings")

def atomic_json(path:Path,value):
    path.parent.mkdir(parents=True,exist_ok=True); fd,name=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent)
    try:
        with os.fdopen(fd,"w") as handle: json.dump(value,handle,sort_keys=True,indent=2); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(name,path)
        descriptor=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass

def canonical_directory(path:Path,prefix:Path,device:int):
    if path.is_symlink() or not path.is_dir(): raise Stop(f"non-directory or symlink worktree: {path}")
    resolved=path.resolve(strict=True); prefix=prefix.resolve(strict=True)
    if resolved==prefix or prefix not in resolved.parents: raise Stop(f"worktree escapes cleanup prefix: {path}")
    if resolved.stat().st_dev!=device: raise Stop(f"worktree is on another device: {path}")
    return resolved

def fresh_main(primary:Path,repo:str,ref:str,timeout:int):
    reject_executable_git_config(primary,timeout)
    api=run([GHAPP,"api",f"repos/{repo}/git/ref/heads/{ref.removeprefix('origin/')}","--jq",".object.sha"],timeout=timeout).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}",api): raise Stop("fresh GitHub main identity is malformed")
    fetched="refs/tartci-worktree-cleanup/fetched-main"
    try:
        run(["git","-C",str(primary),"fetch","--no-tags","--force",f"https://github.com/{repo}.git",f"refs/heads/{ref.removeprefix('origin/')}:"+fetched],timeout=timeout)
        sha=run(["git","-C",str(primary),"rev-parse",fetched],timeout=timeout).stdout.strip()
    finally: run(["git","-C",str(primary),"update-ref","-d",fetched],check=False)
    if sha!=api: raise Stop("Git fetch and authenticated API main identities disagree")
    return sha

def observations(prefix:Path,timeout:int):
    run([PS,"-axo","pid=,command="],timeout=timeout)
    lsof_result=run([LSOF,"-Fn","+D",str(prefix)],timeout=timeout,check=False)
    if lsof_result.returncode not in (0,1) or lsof_result.stderr.strip(): raise Stop("lsof observation is incomplete")
    paths=set()
    for line in lsof_result.stdout.splitlines():
        if line.startswith("n/"): paths.add(line[1:])
    cmux=run([CMUX,"list-workspaces","--json"],timeout=timeout).stdout
    try: cmux_value=json.loads(cmux)
    except json.JSONDecodeError as error: raise Stop("cmux workspace observation is malformed") from error
    def collect(value,key=""):
        if isinstance(value,dict):
            for child_key,child in value.items(): collect(child,str(child_key).lower())
        elif isinstance(value,list):
            for child in value: collect(child,key)
        elif isinstance(value,str) and key in {"path","cwd","root","current_directory","working_directory","workingdirectory"} and value.startswith("/"): paths.add(value)
    collect(cmux_value)
    return paths

def observation_active(paths,target:Path):
    target=target.resolve(strict=False)
    for value in paths:
        candidate=Path(value).resolve(strict=False)
        if candidate==target or target in candidate.parents: return True
    return False

def inspect(primary:Path,prefix:Path,main_sha:str,limit_bytes:int,max_trees:int,timeout:int):
    reject_executable_git_config(primary,timeout)
    rows=inventory(primary,timeout)
    common=Path(run(["git","-C",str(primary),"rev-parse","--path-format=absolute","--git-common-dir"]).stdout.strip()).resolve()
    device=prefix.resolve(strict=True).stat().st_dev; observed=observations(prefix,timeout); dispositions=[]; candidates=[]
    for row in rows:
        path=Path(str(row.get("worktree",""))); disposition={"path":str(path)}
        try: resolved=canonical_directory(path,prefix,device)
        except Stop as error: disposition.update(status="excluded",reason=str(error)); dispositions.append(disposition); continue
        reason=None
        if resolved==primary.resolve(): reason="primary"
        elif row.get("detached"): reason="detached"
        elif row.get("locked"): reason="locked"
        elif not isinstance(row.get("branch"),str): reason="ambiguous_branch"
        if reason: disposition.update(status="excluded",reason=reason); dispositions.append(disposition); continue
        if observation_active(observed,resolved): disposition.update(status="excluded",reason="active_observation"); dispositions.append(disposition); continue
        candidate_common=Path(run(["git","-C",str(resolved),"rev-parse","--path-format=absolute","--git-common-dir"]).stdout.strip()).resolve()
        if candidate_common!=common: raise Stop(f"worktree common-dir mismatch: {resolved}")
        head=run(["git","-C",str(resolved),"rev-parse","HEAD"]).stdout.strip(); branch=str(row["branch"])
        if run(["git","-C",str(primary),"merge-base","--is-ancestor",head,main_sha],check=False).returncode: disposition.update(status="excluded",reason="not_merged"); dispositions.append(disposition); continue
        if run(["git","-C",str(resolved),"status","--porcelain=v1","--untracked-files=all","--ignore-submodules=none"]).stdout: disposition.update(status="excluded",reason="dirty"); dispositions.append(disposition); continue
        if run(["git","-C",str(resolved),"status","--porcelain=v1","--ignored","--untracked-files=all","--ignore-submodules=none"]).stdout: disposition.update(status="excluded",reason="ignored_or_dirty"); dispositions.append(disposition); continue
        branch_head=run(["git","-C",str(primary),"show-ref","--verify","--hash",branch],check=False)
        if branch_head.returncode or branch_head.stdout.strip()!=head: disposition.update(status="excluded",reason="branch_head_mismatch"); dispositions.append(disposition); continue
        size=int(run([DU,"-sk",str(resolved)]).stdout.split()[0])*1024
        disposition.update(status="candidate",head=head,branch=branch,size_bytes=size); dispositions.append(disposition); candidates.append((resolved,head,branch,size,disposition))
    candidates.sort(key=lambda item:item[3],reverse=True)
    selected=[]; total=0
    for item in candidates:
        if len(selected)>=max_trees or total+item[3]>limit_bytes: item[4].update(status="excluded",reason="bounds"); continue
        selected.append(item); total+=item[3]
    return selected,dispositions,total

def quarantine_root(prefix:Path):
    root=prefix/".tartci-worktree-cleanup-quarantine"
    if root.exists() or root.is_symlink():
        info=root.lstat()
        if root.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077: raise Stop("cleanup quarantine identity is invalid")
    else: root.mkdir(mode=0o700)
    info=root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077 or root.resolve(strict=True).parent!=prefix.resolve(strict=True) or info.st_dev!=prefix.resolve(strict=True).stat().st_dev: raise Stop("cleanup quarantine escapes prefix or device")
    return root.resolve(strict=True)

def exact_state(primary:Path,path:Path,head:str,branch:str,timeout:int):
    row=registered(inventory(primary,timeout),path)
    if row is None or row.get("HEAD")!=head or row.get("branch")!=branch or not path.is_dir() or path.is_symlink(): return False
    primary_common=Path(run(["git","-C",str(primary),"rev-parse","--path-format=absolute","--git-common-dir"]).stdout.strip()).resolve()
    candidate_common=Path(run(["git","-C",str(path),"rev-parse","--path-format=absolute","--git-common-dir"]).stdout.strip()).resolve()
    return candidate_common==primary_common

def apply_selected(args, selected, record, main_sha):
    record["status"]="in_progress"; record["after_free_bytes"]=shutil.disk_usage(args.prefix).free
    atomic_json(args.receipt,record)
    for path,head,branch,size,disposition in selected:
        if record["after_free_bytes"]>=args.required_bytes: break
        again,_,_=inspect(args.primary,args.prefix,main_sha,args.max_bytes,args.max_trees,args.timeout)
        if not any(item[0]==path and item[1]==head and item[2]==branch for item in again): raise Stop(f"candidate changed before removal: {path}")
        quarantine=quarantine_root(args.prefix)/(path.name+"-"+head[:12])
        if quarantine.exists() or registered(inventory(args.primary,args.timeout),quarantine): raise Stop(f"quarantine target already exists: {quarantine}")
        record["current_removal"]={"path":str(path),"quarantine_path":str(quarantine),"head":head,"branch":branch,"size_bytes":size,"phase":"intent_recorded"}
        atomic_json(args.receipt,record)
        run(["git","-C",str(args.primary),"worktree","move",str(path),str(quarantine)],timeout=args.timeout)
        record["current_removal"]["phase"]="quarantined"; atomic_json(args.receipt,record)
        if observation_active(observations(quarantine,args.timeout),quarantine):
            run(["git","-C",str(args.primary),"worktree","move",str(quarantine),str(path)],timeout=args.timeout)
            record.setdefault("aborted_removals",[]).append({**record["current_removal"],"reason":"activity_after_claim"}); record.pop("current_removal",None); atomic_json(args.receipt,record)
            raise Stop(f"activity appeared after quarantine claim: {path}")
        if not exact_state(args.primary,quarantine,head,branch,args.timeout): raise Stop(f"quarantined candidate identity changed: {path}")
        if run(["git","-C",str(quarantine),"status","--porcelain=v1","--ignored","--untracked-files=all","--ignore-submodules=none"]).stdout: raise Stop(f"quarantined candidate became dirty or contains ignored entries: {path}")
        proof=run(["git","-C",str(args.primary),"show-ref","--verify","--hash",branch]).stdout.strip()
        if proof!=head: raise Stop(f"branch changed after quarantine claim: {branch}")
        record["current_removal"]["phase"]="removal_in_progress"; atomic_json(args.receipt,record)
        run(["git","-C",str(args.primary),"worktree","remove",str(quarantine)],timeout=args.timeout)
        if quarantine.exists() or registered(inventory(args.primary,args.timeout),quarantine): raise Stop(f"removed worktree remains present: {path}")
        disposition["status"]="removed"; record["removals"].append({"path":str(path),"head":head,"branch":branch,"branch_retained_at":proof,"size_bytes":size})
        record.pop("current_removal",None); record["after_free_bytes"]=shutil.disk_usage(args.prefix).free
        atomic_json(args.receipt,record)

def reconcile_pending(args):
    if not args.receipt.is_file() or args.receipt.is_symlink(): return False
    try: record=json.loads(args.receipt.read_text())
    except (OSError,json.JSONDecodeError) as error: raise Stop("cleanup receipt is malformed") from error
    pending=record.get("current_removal")
    if not isinstance(pending,dict): return False
    path=Path(str(pending.get("path",""))); quarantine=Path(str(pending.get("quarantine_path",""))); head=pending.get("head"); branch=pending.get("branch")
    rows=inventory(args.primary,getattr(args,"timeout",30)); original_row=registered(rows,path); quarantine_row=registered(rows,quarantine) if str(quarantine) not in ("", ".") else None
    original_ok=original_row is not None and original_row.get("HEAD")==head and original_row.get("branch")==branch and path.is_dir() and not path.is_symlink()
    quarantine_ok=quarantine_row is not None and quarantine_row.get("HEAD")==head and quarantine_row.get("branch")==branch and quarantine.is_dir() and not quarantine.is_symlink()
    if original_ok and quarantine_row is None and not quarantine.exists():
        record.setdefault("aborted_removals",[]).append({**pending,"reconciled_after_restart":True}); record.pop("current_removal",None); record["status"]="stopped_after_reconciliation"; record["retry_admission"]="denied"; atomic_json(args.receipt,record); return True
    if quarantine_ok and original_row is None and not path.exists():
        run(["git","-C",str(args.primary),"worktree","move",str(quarantine),str(path)],timeout=getattr(args,"timeout",30))
        record.setdefault("aborted_removals",[]).append({**pending,"reconciled_after_restart":True,"rolled_back":True}); record.pop("current_removal",None); record["status"]="stopped_after_reconciliation"; record["retry_admission"]="denied"; atomic_json(args.receipt,record); return True
    if path.exists() or quarantine.exists() or original_row is not None or quarantine_row is not None: raise Stop("pending removal has mixed path or registry state")
    proof=run(["git","-C",str(args.primary),"show-ref","--verify","--hash",str(branch)]).stdout.strip()
    if proof!=head: raise Stop("pending removal branch proof failed")
    completed={key:pending[key] for key in ("path","head","branch","size_bytes")}; completed["branch_retained_at"]=proof; completed["reconciled_after_restart"]=True
    record.setdefault("removals",[]).append(completed); record.pop("current_removal",None); record["status"]="stopped_after_reconciliation"; record["retry_admission"]="denied"
    record["after_free_bytes"]=shutil.disk_usage(args.prefix).free; record["finished_at"]=dt.datetime.now(dt.timezone.utc).isoformat(); atomic_json(args.receipt,record); return True
def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--provider",required=True); p.add_argument("--repo",required=True); p.add_argument("--primary",type=Path,required=True); p.add_argument("--prefix",type=Path,required=True); p.add_argument("--main-ref",required=True); p.add_argument("--github-cli",required=True); p.add_argument("--ghapp-sha256",required=True); p.add_argument("--cmux-sha256",required=True); p.add_argument("--receipt",type=Path,required=True); p.add_argument("--lock",type=Path,required=True); p.add_argument("--required-bytes",type=int,required=True); p.add_argument("--before-free-bytes",type=int,required=True); p.add_argument("--max-trees",type=int,default=8); p.add_argument("--max-bytes",type=int,default=512*1024**3); p.add_argument("--timeout",type=int,default=300); p.add_argument("--cooldown",type=int,default=3600); p.add_argument("--apply",action="store_true"); args=p.parse_args(argv)
    if args.provider!="merged-main-v1" or args.repo!="Generous-Corp/pulp" or args.primary!=Path("/Volumes/Workshop/Code/pulp") or args.prefix!=Path("/Volumes/Workshop/Code") or args.main_ref!="origin/main" or args.github_cli!="ghapp" or not re.fullmatch(r"[0-9a-f]{64}",args.ghapp_sha256) or not re.fullmatch(r"[0-9a-f]{64}",args.cmux_sha256) or not 1<=args.max_trees<=8 or not 0<args.max_bytes<=512*1024**3 or not 1<=args.timeout<=300 or args.cooldown<0: raise Stop("unsupported cleanup authority")
    PRIVATE_DIGESTS.update({GHAPP:args.ghapp_sha256,CMUX:args.cmux_sha256})
    started=time.monotonic(); args.lock.parent.mkdir(parents=True,exist_ok=True)
    with args.lock.open("a+") as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return 3
        try:
            if reconcile_pending(args): return 6
        except Exception as error:
            return 2
        if args.receipt.exists() and time.time()-args.receipt.stat().st_mtime<args.cooldown: return 4
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(Stop("cleanup timeout exceeded")))
        signal.alarm(args.timeout)
        record={"schema":SCHEMA,"provider":args.provider,"repo":args.repo,"started_at":dt.datetime.now(dt.timezone.utc).isoformat(),"before_free_bytes":args.before_free_bytes,"required_bytes":args.required_bytes,"bounds":{"max_trees":args.max_trees,"max_bytes":args.max_bytes,"timeout_seconds":args.timeout,"cooldown_seconds":args.cooldown},"apply":args.apply,"removals":[]}
        try:
            main_sha=fresh_main(args.primary,args.repo,args.main_ref,args.timeout); record["main_sha"]=main_sha
            selected,dispositions,total=inspect(args.primary,args.prefix,main_sha,args.max_bytes,args.max_trees,args.timeout); record["dispositions"]=dispositions; record["selected_bytes"]=total
            if args.apply:
                apply_selected(args,selected,record,main_sha)
            usage=shutil.disk_usage(args.prefix); record["after_free_bytes"]=usage.free; record["retry_admission"]="eligible" if usage.free>=args.required_bytes else "denied"; record["status"]="complete"
        except Exception as error: record["status"]="stopped"; record["error"]=str(error); record["retry_admission"]="denied"; record["finished_at"]=dt.datetime.now(dt.timezone.utc).isoformat(); atomic_json(args.receipt,record); return 2
        record["finished_at"]=dt.datetime.now(dt.timezone.utc).isoformat()
        if not args.apply:
            record["retry_admission"]="not_requested_apply_false"
            atomic_json(args.receipt,record); signal.alarm(0); return 5
        atomic_json(args.receipt,record); signal.alarm(0)
        return 0 if record["after_free_bytes"]>=args.required_bytes else 7
if __name__=="__main__": raise SystemExit(main())
