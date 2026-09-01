#!/usr/bin/env python3
"""Fail-closed identity verifier for the sealed Tart CI launcher app."""
from __future__ import annotations
import argparse, hashlib, json, os, re, stat, subprocess, tomllib
from pathlib import Path

class IdentityError(ValueError): pass
def profile_policy_digest(path_value: str|Path)->str:
    try: value=tomllib.loads(Path(path_value).read_text())
    except (OSError,tomllib.TOMLDecodeError) as error: raise IdentityError(f"cannot read launcher profile policy: {error}") from error
    helper=value.get("launch_helper")
    if not isinstance(helper,dict): raise IdentityError("launcher profile policy has no launch_helper")
    helper.pop("sha256",None)
    payload=json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()
def _run(command: list[str]) -> str:
    result=subprocess.run(command,text=True,capture_output=True,check=False,timeout=15)
    if result.returncode: raise IdentityError(f"signature inspection failed: {result.stderr.strip()}")
    return result.stdout+result.stderr
def _one(pattern,text,label):
    match=re.search(pattern,text,re.MULTILINE)
    if not match: raise IdentityError(f"missing {label}")
    return match.group(1).strip()
def bundle_digest(root: Path) -> str:
    digest=hashlib.sha256()
    for path in sorted(root.rglob("*"),key=lambda p:p.relative_to(root).as_posix()):
        relative=path.relative_to(root).as_posix(); info=path.lstat()
        if path.is_symlink(): raise IdentityError(f"launcher bundle contains a symlink: {relative}")
        if not(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)): raise IdentityError(f"launcher bundle contains a special file: {relative}")
        digest.update(relative.encode()+b"\0"+str(stat.S_IMODE(info.st_mode)).encode()+b"\0")
        if stat.S_ISREG(info.st_mode): digest.update(path.read_bytes())
    return digest.hexdigest()
def verify(path_value: str|Path,*,identifier:str,team_id:str,sha256:str|None=None,profile_policy_sha256:str|None=None,source_commit:str|None=None)->dict[str,object]:
    path=Path(path_value)
    if path.is_symlink() or not path.is_dir(): raise IdentityError("launcher must be a regular non-symlink app bundle")
    metadata=path.stat()
    if metadata.st_uid!=os.getuid(): raise IdentityError("launcher bundle must be owned by the fleet user")
    executable=path/"Contents/MacOS/tartci-launcher"
    if executable.is_symlink() or not executable.is_file() or executable.stat().st_mode&0o111==0: raise IdentityError("launcher bundle executable is invalid")
    canonical=str(path.resolve(strict=True)); digest=bundle_digest(path)
    if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}",sha256): raise IdentityError("expected sha256 must be 64 hexadecimal characters")
    if sha256 is not None and digest!=sha256.lower(): raise IdentityError("launcher bundle sha256 does not match pinned digest")
    detail=_run(["/usr/bin/codesign","-d","--verbose=4",canonical]); actual_identifier=_one(r"^Identifier=(.+)$",detail,"identifier"); actual_team=_one(r"^TeamIdentifier=(.+)$",detail,"team identifier"); authority=_one(r"^Authority=(.+)$",detail,"signing authority"); flags=int(_one(r"^CodeDirectory .*?flags=0x([0-9a-fA-F]+)",detail,"CodeDirectory flags"),16)
    if actual_identifier!=identifier: raise IdentityError("launcher identifier does not match")
    if actual_team!=team_id: raise IdentityError("launcher team identifier does not match")
    if not authority.startswith("Developer ID Application:"): raise IdentityError("launcher is not signed by Developer ID Application")
    if flags&0x10000==0: raise IdentityError("launcher signature does not enable hardened runtime")
    if _run(["/usr/bin/lipo","-archs",str(executable)]).strip()!="arm64": raise IdentityError("launcher must be arm64-only")
    requirement=f'anchor apple generic and certificate leaf[subject.OU] = "{team_id}" and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
    _run(["/usr/bin/codesign","--verify","--strict","--deep","--verbose=4",f"-R={requirement}",canonical])
    designated=_one(r"^designated => (.+)$",_run(["/usr/bin/codesign","-d","-r-",canonical]),"designated requirement")
    resources=path/"Contents/Resources"
    try:
        metadata=json.loads((resources/"bundle.json").read_text())
        lanes=json.loads((resources/"lanes.json").read_text())
        manifest=(resources/"support/.tartci-support-manifest.json").read_bytes()
    except (OSError,json.JSONDecodeError) as error:
        raise IdentityError(f"launcher sealed metadata is unreadable: {error}") from error
    if not isinstance(metadata,dict) or set(metadata)!={"schema","source_commit","support_manifest_sha256","profile_policy_sha256","tart_home"} or metadata.get("schema")!=1:
        raise IdentityError("launcher sealed metadata is malformed")
    if metadata.get("tart_home")!="/Volumes/Workshop/VMs": raise IdentityError("launcher sealed metadata has the wrong Tart store")
    if not re.fullmatch(r"[0-9a-f]{40}",str(metadata.get("source_commit",""))): raise IdentityError("launcher source commit is malformed")
    if not re.fullmatch(r"[0-9a-f]{64}",str(metadata.get("profile_policy_sha256",""))): raise IdentityError("launcher profile policy digest is malformed")
    if hashlib.sha256(manifest).hexdigest()!=metadata.get("support_manifest_sha256"): raise IdentityError("launcher support manifest digest does not match sealed metadata")
    if source_commit is not None and metadata["source_commit"]!=source_commit: raise IdentityError("launcher source commit does not match the installed support cohort")
    if profile_policy_sha256 is not None and metadata["profile_policy_sha256"]!=profile_policy_sha256: raise IdentityError("launcher profile policy digest does not match the installed profile")
    if not isinstance(lanes,dict) or lanes.get("schema")!=1 or not isinstance(lanes.get("lanes"),dict) or not lanes["lanes"]:
        raise IdentityError("launcher sealed lane configuration is malformed")
    if bundle_digest(path)!=digest: raise IdentityError("launcher bundle changed during verification")
    return {"schema":1,"path":canonical,"sha256":digest,"owner_uid":path.stat().st_uid,"mode":stat.S_IMODE(path.stat().st_mode),"identifier":actual_identifier,"team_id":actual_team,"designated_requirement":designated,"designated_requirement_sha256":hashlib.sha256(designated.encode()).hexdigest(),"hardened_runtime":True,"authority_class":"developer-id-application","architecture":"arm64","source_commit":metadata["source_commit"],"profile_policy_sha256":metadata["profile_policy_sha256"],"support_manifest_sha256":metadata["support_manifest_sha256"],"lane_ids":sorted(lanes["lanes"])}
def main()->int:
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command",required=True); command=sub.add_parser("verify"); command.add_argument("path"); command.add_argument("--identifier",required=True); command.add_argument("--team-id",required=True); command.add_argument("--sha256"); command.add_argument("--profile-policy-sha256"); command.add_argument("--source-commit"); args=parser.parse_args()
    try: record=verify(args.path,identifier=args.identifier,team_id=args.team_id,sha256=args.sha256,profile_policy_sha256=args.profile_policy_sha256,source_commit=args.source_commit)
    except (IdentityError,OSError,subprocess.TimeoutExpired) as error: parser.exit(1,f"macos launcher identity rejected: {error}\n")
    print(json.dumps(record,sort_keys=True,separators=(",",":"))); return 0
if __name__=="__main__": raise SystemExit(main())
