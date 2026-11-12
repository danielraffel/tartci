#!/usr/bin/env python3
"""Fail-closed identity verifier for the sealed Tart CI launcher app."""
from __future__ import annotations
import argparse, hashlib, json, os, re, stat, subprocess, tomllib
from pathlib import Path

class IdentityError(ValueError): pass
def external_store(value: object)->bool:
    """An absolute /Volumes/<volume>/<dir>... path with no dot components."""
    if not isinstance(value,str) or not value.startswith("/Volumes/"): return False
    parts=value.split("/")[1:]
    return len(parts)>=3 and all(part not in ("",".","..") for part in parts)
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
def _signature(canonical:str,executable:Path,*,identifier:str,team_id:str)->tuple[str,str,str]:
    """Developer ID, hardened, arm64, and the identifier and team asked for."""
    detail=_run(["/usr/bin/codesign","-d","--verbose=4",canonical]); actual_identifier=_one(r"^Identifier=(.+)$",detail,"identifier"); actual_team=_one(r"^TeamIdentifier=(.+)$",detail,"team identifier"); authority=_one(r"^Authority=(.+)$",detail,"signing authority"); flags=int(_one(r"^CodeDirectory .*?flags=0x([0-9a-fA-F]+)",detail,"CodeDirectory flags"),16)
    if actual_identifier!=identifier: raise IdentityError("launcher identifier does not match")
    if actual_team!=team_id: raise IdentityError("launcher team identifier does not match")
    if not authority.startswith("Developer ID Application:"): raise IdentityError("launcher is not signed by Developer ID Application")
    if flags&0x10000==0: raise IdentityError("launcher signature does not enable hardened runtime")
    if _run(["/usr/bin/lipo","-archs",str(executable)]).strip()!="arm64": raise IdentityError("launcher must be arm64-only")
    requirement=f'anchor apple generic and certificate leaf[subject.OU] = "{team_id}" and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
    _run(["/usr/bin/codesign","--verify","--strict","--deep","--verbose=4",f"-R={requirement}",canonical])
    designated=_one(r"^designated => (.+)$",_run(["/usr/bin/codesign","-d","-r-",canonical]),"designated requirement")
    return actual_identifier,actual_team,designated
def verify(path_value: str|Path,*,identifier:str,team_id:str,sha256:str|None=None,profile_policy_sha256:str|None=None,source_commit:str|None=None,tart_home:str|None=None)->dict[str,object]:
    path=Path(path_value)
    if path.is_symlink() or not path.is_dir(): raise IdentityError("launcher must be a regular non-symlink app bundle")
    metadata=path.stat()
    if metadata.st_uid!=os.getuid(): raise IdentityError("launcher bundle must be owned by the fleet user")
    executable=path/"Contents/MacOS/tartci-launcher"
    if executable.is_symlink() or not executable.is_file() or executable.stat().st_mode&0o111==0: raise IdentityError("launcher bundle executable is invalid")
    canonical=str(path.resolve(strict=True)); digest=bundle_digest(path)
    if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}",sha256): raise IdentityError("expected sha256 must be 64 hexadecimal characters")
    if sha256 is not None and digest!=sha256.lower(): raise IdentityError("launcher bundle sha256 does not match pinned digest")
    actual_identifier,actual_team,designated=_signature(canonical,executable,identifier=identifier,team_id=team_id)
    resources=path/"Contents/Resources"
    try:
        metadata=json.loads((resources/"bundle.json").read_text())
        lanes=json.loads((resources/"lanes.json").read_text())
        manifest=(resources/"support/.tartci-support-manifest.json").read_bytes()
    except (OSError,json.JSONDecodeError) as error:
        raise IdentityError(f"launcher sealed metadata is unreadable: {error}") from error
    if not isinstance(metadata,dict) or set(metadata)!={"schema","source_commit","support_manifest_sha256","profile_policy_sha256","tart_home"} or metadata.get("schema")!=1:
        raise IdentityError("launcher sealed metadata is malformed")
    # The sealed store is any external-volume store (m3: Workshop, m5studio:
    # Atelier); the profile policy digest already binds it to one profile, and
    # a caller that knows the profile's tart_home pins it exactly.
    sealed_store=metadata.get("tart_home")
    if not external_store(sealed_store): raise IdentityError("launcher sealed metadata has no external-volume Tart store")
    if tart_home is not None and sealed_store!=tart_home: raise IdentityError("launcher sealed metadata has the wrong Tart store")
    if not re.fullmatch(r"[0-9a-f]{40}",str(metadata.get("source_commit",""))): raise IdentityError("launcher source commit is malformed")
    if not re.fullmatch(r"[0-9a-f]{64}",str(metadata.get("profile_policy_sha256",""))): raise IdentityError("launcher profile policy digest is malformed")
    if hashlib.sha256(manifest).hexdigest()!=metadata.get("support_manifest_sha256"): raise IdentityError("launcher support manifest digest does not match sealed metadata")
    if source_commit is not None and metadata["source_commit"]!=source_commit: raise IdentityError("launcher source commit does not match the installed support cohort")
    if profile_policy_sha256 is not None and metadata["profile_policy_sha256"]!=profile_policy_sha256: raise IdentityError("launcher profile policy digest does not match the installed profile")
    if not isinstance(lanes,dict) or lanes.get("schema")!=1 or not isinstance(lanes.get("lanes"),dict) or not lanes["lanes"]:
        raise IdentityError("launcher sealed lane configuration is malformed")
    if bundle_digest(path)!=digest: raise IdentityError("launcher bundle changed during verification")
    return {"schema":1,"path":canonical,"sha256":digest,"owner_uid":path.stat().st_uid,"mode":stat.S_IMODE(path.stat().st_mode),"identifier":actual_identifier,"team_id":actual_team,"designated_requirement":designated,"designated_requirement_sha256":hashlib.sha256(designated.encode()).hexdigest(),"hardened_runtime":True,"authority_class":"developer-id-application","architecture":"arm64","source_commit":metadata["source_commit"],"profile_policy_sha256":metadata["profile_policy_sha256"],"support_manifest_sha256":metadata["support_manifest_sha256"],"lane_ids":sorted(lanes["lanes"])}
# Launcher v2: a stable app (executable, Info.plist, policy.json, inputs.json)
# whose bytes change only when its inputs do. A lane runs the signed launch set
# of the generation the pointer names (launch_set.py).
V2_FILES=frozenset({"Contents/Info.plist","Contents/MacOS/tartci-launcher","Contents/Resources/policy.json",
                    "Contents/Resources/inputs.json","Contents/_CodeSignature/CodeResources"})
def v2_policy(profile: dict)->dict[str,object]:
    """policy.json for a profile: the launcher's only host facts."""
    host,helper=profile.get("host") or {},profile.get("launch_helper") or {}
    tart_home=host.get("tart_home")
    if not external_store(tart_home): raise IdentityError(f"launch helper requires an external-volume Tart store, got {tart_home!r}")
    if not isinstance(helper.get("team_id"),str) or not re.fullmatch(r"[A-Z0-9]{10}",helper["team_id"]): raise IdentityError("launch_helper.team_id must be a ten-character Apple Team ID")
    if not isinstance(helper.get("identifier"),str) or not helper["identifier"]: raise IdentityError("launch_helper.identifier is missing")
    home=str(host.get("home","")).rstrip("/")
    if not home.startswith("/"): raise IdentityError("host.home must be absolute")
    return {"schema":2,"identifier":helper["identifier"],"team_id":helper["team_id"],"tart_home":tart_home,
            "generations_root":f"{home}/.local/share/tartci-generations","home":home,
            "pointer":f"{home}/.config/tartci/launcher-generation.json","floor":f"{home}/.config/tartci/launcher-floor.json"}
def v2_policy_bytes(profile: dict)->bytes:
    return (json.dumps(v2_policy(profile),sort_keys=True,separators=(",",":"))+"\n").encode()
def inputs_digest(main_swift: bytes, info_plist: bytes, policy: bytes)->str:
    """What a v2 app is made of: its source, Info.plist and policy, length-delimited."""
    digest=hashlib.sha256()
    for part in (main_swift,info_plist,policy):
        digest.update(len(part).to_bytes(8,"big")); digest.update(part)
    return digest.hexdigest()
def source_inputs_digest(support_root: str|Path, profile: dict)->str:
    """The inputs digest a v2 app built now from this support tree would carry."""
    native=Path(support_root)/"native/macos/tartci-launcher"
    return inputs_digest((native/"main.swift").read_bytes(),(native/"Info.plist").read_bytes(),v2_policy_bytes(profile))
def verify_v2(path_value: str|Path,*,identifier:str,team_id:str,profile:dict,sha256:str|None=None,support_root:str|Path|None=None)->dict[str,object]:
    """A v2 app: the v1 signature rules, exactly V2_FILES, the profile's policy,
    and (given the support tree) an inputs digest it would build the same."""
    path=Path(path_value)
    if path.is_symlink() or not path.is_dir(): raise IdentityError("launcher must be a regular non-symlink app bundle")
    if path.stat().st_uid!=os.getuid(): raise IdentityError("launcher bundle must be owned by the fleet user")
    executable=path/"Contents/MacOS/tartci-launcher"
    if executable.is_symlink() or not executable.is_file() or executable.stat().st_mode&0o111==0: raise IdentityError("launcher bundle executable is invalid")
    canonical=str(path.resolve(strict=True)); digest=bundle_digest(path)
    if sha256 is not None and digest!=sha256.lower(): raise IdentityError("launcher bundle sha256 does not match pinned digest")
    files={p.relative_to(path).as_posix() for p in path.rglob("*") if not p.is_dir()}
    if files!=V2_FILES: raise IdentityError(f"launcher v2 bundle files differ: extra={sorted(files-V2_FILES)} missing={sorted(V2_FILES-files)}")
    actual_identifier,actual_team,designated=_signature(canonical,executable,identifier=identifier,team_id=team_id)
    resources=path/"Contents/Resources"
    policy=(resources/"policy.json").read_bytes()
    if policy!=v2_policy_bytes(profile): raise IdentityError("launcher policy does not match the profile")
    try: inputs=json.loads((resources/"inputs.json").read_text())
    except (OSError,json.JSONDecodeError) as error: raise IdentityError(f"launcher inputs record is unreadable: {error}") from error
    if not isinstance(inputs,dict) or inputs.get("schema")!=1 or not re.fullmatch(r"[0-9a-f]{64}",str(inputs.get("sha256",""))): raise IdentityError("launcher inputs record is malformed")
    if support_root is not None:
        if inputs["sha256"]!=source_inputs_digest(support_root,profile): raise IdentityError("launcher was not built from this support tree's launcher inputs")
    if bundle_digest(path)!=digest: raise IdentityError("launcher bundle changed during verification")
    return {"schema":2,"path":canonical,"sha256":digest,"owner_uid":path.stat().st_uid,"mode":stat.S_IMODE(path.stat().st_mode),
            "identifier":actual_identifier,"team_id":actual_team,"designated_requirement":designated,
            "designated_requirement_sha256":hashlib.sha256(designated.encode()).hexdigest(),"hardened_runtime":True,
            "authority_class":"developer-id-application","architecture":"arm64","layout":"v2",
            "inputs_sha256":inputs["sha256"],"policy_sha256":hashlib.sha256(policy).hexdigest()}
def same_bundle(live: str|Path, source: str|Path)->bool:
    """Whether the live launcher is byte-for-byte the (already verified) source.

    Reinstalling an identical bundle replaces the app directory, which can
    reset its per-app privacy consent (Removable Volumes); an identical live
    bundle is left in place instead. Compares paths, modes and contents,
    signature included; any doubt reads as different.
    """
    live,source=Path(live),Path(source)
    try:
        if live.is_symlink() or not live.is_dir() or live.stat().st_uid!=os.getuid(): return False
        if source.is_symlink() or not source.is_dir(): return False
        return bundle_digest(live)==bundle_digest(source)
    except (IdentityError,OSError):
        return False
def main()->int:
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command",required=True); same=sub.add_parser("same"); same.add_argument("live"); same.add_argument("source"); command=sub.add_parser("verify"); command.add_argument("path"); command.add_argument("--identifier",required=True); command.add_argument("--team-id",required=True); command.add_argument("--sha256"); command.add_argument("--profile-policy-sha256"); command.add_argument("--source-commit"); command.add_argument("--tart-home")
    v2=sub.add_parser("verify-v2"); v2.add_argument("path"); v2.add_argument("--identifier",required=True); v2.add_argument("--team-id",required=True); v2.add_argument("--profile",required=True); v2.add_argument("--sha256"); v2.add_argument("--support-root")
    args=parser.parse_args()
    if args.command=="same": return 0 if same_bundle(args.live,args.source) else 1
    if args.command=="verify-v2":
        try: record=verify_v2(args.path,identifier=args.identifier,team_id=args.team_id,profile=tomllib.loads(Path(args.profile).read_text()),sha256=args.sha256,support_root=args.support_root)
        except (IdentityError,OSError,ValueError,subprocess.TimeoutExpired) as error: parser.exit(1,f"macos launcher identity rejected: {error}\n")
        print(json.dumps(record,sort_keys=True,separators=(",",":"))); return 0
    try: record=verify(args.path,identifier=args.identifier,team_id=args.team_id,sha256=args.sha256,profile_policy_sha256=args.profile_policy_sha256,source_commit=args.source_commit,tart_home=args.tart_home)
    except (IdentityError,OSError,subprocess.TimeoutExpired) as error: parser.exit(1,f"macos launcher identity rejected: {error}\n")
    print(json.dumps(record,sort_keys=True,separators=(",",":"))); return 0
if __name__=="__main__": raise SystemExit(main())
