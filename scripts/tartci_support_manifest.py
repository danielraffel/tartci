#!/usr/bin/env python3
"""Build and verify an immutable TartCI runtime/support cohort manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


SCHEMA = 2
COMMIT = re.compile(r"^[0-9a-f]{40}$")
GITHUB_REPOSITORY = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$"
)
ROOT_FILES = {"tartci"}
ROOT_DIRS = {"launchd", "native", "profiles", "providers", "scripts"}
MANIFEST_NAME = ".tartci-support-manifest.json"
LAUNCH_NAME = ".tartci-launch"
TRUSTED_REPOSITORY = "https://github.com/danielraffel/tartci.git"


def fail(message: str) -> None:
    raise ValueError(message)


def selected(relative: str) -> bool:
    path = PurePosixPath(relative)
    if relative in ROOT_FILES:
        return True
    if not path.parts or path.parts[0] not in ROOT_DIRS:
        return False
    if path.parts[0] == "scripts" and path.name.startswith("test_"):
        return False
    return (
        "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
        and path.name not in {".DS_Store", MANIFEST_NAME}
    )


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def member(root: Path, relative: str) -> dict[str, object]:
    target = root / relative
    try:
        info = target.lstat()
    except OSError as exc:
        fail(f"support member is unavailable: {relative}: {exc}")
    if not stat.S_ISREG(info.st_mode) or target.is_symlink():
        fail(f"support member must be a regular non-symlink file: {relative}")
    return {
        "path": relative,
        "mode": stat.S_IMODE(info.st_mode),
        "sha256": digest(target),
    }


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        fail(result.stderr.strip() or "git command failed while building support manifest")
    return result.stdout


def repository_key(root: Path) -> str:
    try:
        raw = _git(root, "remote", "get-url", "origin").strip()
    except ValueError:
        fail("support source origin must identify one exact GitHub repository")
    match = GITHUB_REPOSITORY.fullmatch(raw)
    if match is None:
        fail("support source origin must identify one exact GitHub repository")
    owner, repository = match.groups()
    canonical = f"https://github.com/{owner.lower()}/{repository.lower()}.git"
    if canonical != TRUSTED_REPOSITORY:
        fail(f"support source repository is not trusted: {canonical}")
    return canonical


def build(root: Path) -> dict[str, object]:
    root = root.resolve()
    commit = _git(root, "rev-parse", "HEAD").strip()
    if not COMMIT.fullmatch(commit):
        fail("support source commit must be an exact lowercase 40-hex commit")
    tree_modes: dict[str, int] = {}
    tree = _git(
        root, "ls-tree", "-r", "-z", "HEAD", "--",
        "tartci", "launchd", "native", "profiles", "providers", "scripts",
    )
    for record in tree.split("\0"):
        if not record:
            continue
        metadata, name = record.split("\t", 1)
        mode, kind, _object_id = metadata.split(" ", 2)
        if not selected(name):
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            fail(f"support source member has unsupported Git type or mode: {name}")
        if any(ord(character) < 32 for character in name):
            fail("support source member names may not contain control characters")
        tree_modes[name] = 0o755 if mode == "100755" else 0o644
    names = sorted(tree_modes)
    if not names:
        fail("support manifest selection is empty")
    dirty = subprocess.run(
        ["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", *names],
        check=False,
    )
    if dirty.returncode != 0:
        fail("selected support files must exactly match the source commit")
    for name, expected_mode in tree_modes.items():
        if member(root, name)["mode"] != expected_mode:
            fail(f"support member mode differs from the source commit: {name}")
    return {
        "schema": SCHEMA,
        "repository": repository_key(root),
        "source_commit": commit,
        "members": [member(root, name) for name in names],
    }


def write(root: Path, output: Path) -> None:
    manifest = build(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.is_symlink() or not output.parent.is_dir():
        fail(f"support manifest parent must be a regular directory: {output.parent}")
    fd, staged_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(0o644)
        os.replace(staged, output)
    finally:
        staged.unlink(missing_ok=True)
    verify(root, output)


def load(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        fail(f"support manifest must be a regular non-symlink file: {path}")
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"could not read support manifest {path}: {exc}")
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        fail(f"support manifest schema must be {SCHEMA}")
    if set(manifest) != {"schema", "repository", "source_commit", "members"}:
        fail("support manifest has unknown or missing top-level fields")
    if not re.fullmatch(
        r"https://github\.com/[a-z0-9_.-]+/[a-z0-9_.-]+\.git",
        str(manifest.get("repository", "")),
    ):
        fail("support manifest repository must be a canonical GitHub repository key")
    if not COMMIT.fullmatch(str(manifest.get("source_commit", ""))):
        fail("support manifest source_commit must be lowercase 40-hex")
    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        fail("support manifest members must be a non-empty array")
    return manifest


def filesystem_names(root: Path) -> set[str]:
    names: set[str] = set()
    for root_file in ROOT_FILES:
        if (root / root_file).exists() or (root / root_file).is_symlink():
            names.add(root_file)
    for root_dir in ROOT_DIRS:
        directory = root / root_dir
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            fail(f"support directory must be a regular directory: {directory}")
        for current, dirs, files in os.walk(directory, followlinks=False):
            current_path = Path(current)
            for name in dirs:
                target = current_path / name
                if target.is_symlink():
                    relative = target.relative_to(root).as_posix()
                    if selected(relative):
                        fail(f"support tree contains a symlinked directory: {relative}")
            for name in files:
                relative = (current_path / name).relative_to(root).as_posix()
                if selected(relative):
                    names.add(relative)
    return names


def verify(
    root: Path, manifest_path: Path, *, immutable: bool = False
) -> dict[str, object]:
    root = root.resolve()
    manifest = load(manifest_path)
    if immutable:
        manifest_info = manifest_path.lstat()
        if stat.S_IMODE(manifest_info.st_mode) != 0o444:
            fail("installed support manifest must have immutable mode 0444")
    recorded: dict[str, dict[str, object]] = {}
    for entry in manifest["members"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "mode", "sha256"}:
            fail("support manifest member has unknown or missing fields")
        relative = entry.get("path")
        if (
            not isinstance(relative, str)
            or not selected(relative)
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or relative in recorded
        ):
            fail(f"support manifest member path is invalid or duplicated: {relative!r}")
        if type(entry.get("mode")) is not int or not 0 <= entry["mode"] <= 0o777:
            fail(f"support manifest member mode is invalid: {relative}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
            fail(f"support manifest member digest is invalid: {relative}")
        recorded[relative] = entry
    actual_names = filesystem_names(root)
    if actual_names != set(recorded):
        missing = sorted(set(recorded) - actual_names)
        extra = sorted(actual_names - set(recorded))
        fail(f"installed support cohort differs from manifest: missing={missing} extra={extra}")
    for relative, expected in recorded.items():
        actual = member(root, relative)
        expected_actual = dict(expected)
        if immutable:
            expected_actual["mode"] = int(expected_actual["mode"]) & ~0o222
        if actual != expected_actual:
            fail(f"installed support member failed verification: {relative}")
    if immutable:
        directories = {root}
        for relative in recorded:
            parent = (root / relative).parent
            while parent != root:
                directories.add(parent)
                parent = parent.parent
        for directory in directories:
            info = directory.lstat()
            if (
                directory.is_symlink()
                or not stat.S_ISDIR(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o555
            ):
                fail(
                    "installed support directory must have immutable mode 0555: "
                    f"{directory}"
                )
    return manifest


def canonical_wrapper_bytes(support_root: Path) -> bytes:
    root = support_root.resolve()
    target = root / "tartci"
    verifier = root / "scripts/tartci_support_manifest.py"
    manifest = root / MANIFEST_NAME
    return (
        "#!/bin/bash\n"
        f"/usr/bin/python3 {shlex.quote(str(verifier))} verify "
        f"{shlex.quote(str(manifest))} --root {shlex.quote(str(root))} "
        "--immutable >/dev/null || exit $?\n"
        f"exec /bin/bash {shlex.quote(str(target))} \"$@\"\n"
    ).encode()


def canonical_entrypoint_path(path: Path) -> str:
    return str(path.parent.resolve() / path.name)


def entrypoint_record(
    path: Path, support_root: Path, *, expected_mode: int
) -> dict[str, object]:
    try:
        info = path.lstat()
    except OSError as exc:
        fail(f"TartCI entrypoint is unavailable: {path}: {exc}")
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        fail(f"TartCI entrypoint must be a regular non-symlink file: {path}")
    if info.st_uid != os.getuid():
        fail(f"TartCI entrypoint must be owned by the current user: {path}")
    if stat.S_IMODE(info.st_mode) != expected_mode:
        fail(f"TartCI entrypoint must have mode {expected_mode:04o}: {path}")
    expected = canonical_wrapper_bytes(support_root)
    if path.read_bytes() != expected:
        fail(f"TartCI entrypoint does not select the receipted support root: {path}")
    return {
        "path": canonical_entrypoint_path(path),
        "mode": expected_mode,
        "sha256": hashlib.sha256(expected).hexdigest(),
        "support_root": str(support_root.resolve()),
    }


def wrapper_record(path: Path, support_root: Path) -> dict[str, object]:
    return entrypoint_record(path, support_root, expected_mode=0o755)


def launch_record(path: Path, support_root: Path) -> dict[str, object]:
    return entrypoint_record(path, support_root, expected_mode=0o555)


def staged_wrapper_record(
    staged_path: Path, entrypoint: Path, support_root: Path
) -> dict[str, object]:
    record = wrapper_record(staged_path, support_root)
    record["path"] = canonical_entrypoint_path(entrypoint)
    return record


def write_wrapper(path: Path, support_root: Path) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != os.getuid():
        fail(f"TartCI entrypoint parent must be an owned regular directory: {parent}")
    fd, staged_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_wrapper_bytes(support_root))
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(0o755)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    wrapper_record(path, support_root)


def stage_install(
    source_root: Path, manifest_path: Path, generations_root: Path
) -> dict[str, object]:
    """Materialize one immutable support generation without activating it."""
    source_root = source_root.resolve()
    manifest_path = manifest_path.resolve()
    manifest = verify(source_root, manifest_path)
    if manifest != build(source_root):
        fail("support manifest is not the exact clean source commit manifest")
    manifest_digest = digest(manifest_path)
    generation_name = f"{manifest['source_commit']}-{manifest_digest[:16]}"
    generations_root.mkdir(parents=True, exist_ok=True)
    if (
        generations_root.is_symlink()
        or not generations_root.is_dir()
        or generations_root.stat().st_uid != os.getuid()
    ):
        fail(
            "support generations root must be an owned regular directory: "
            f"{generations_root}"
        )
    destination = generations_root.resolve() / generation_name
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            fail(f"support generation path is not a regular directory: {destination}")
        installed_manifest = destination / MANIFEST_NAME
        if digest(installed_manifest) != manifest_digest:
            fail(f"existing support generation has a foreign manifest: {destination}")
        verify(destination, installed_manifest, immutable=True)
        launch = destination / LAUNCH_NAME
        launch_record(launch, destination)
        return {
            "created": False,
            "root": str(destination),
            "manifest": str(installed_manifest),
            "launch_entrypoint": str(launch),
        }

    staged = Path(tempfile.mkdtemp(prefix=f".{generation_name}.", dir=generations_root))
    try:
        for entry in manifest["members"]:
            relative = str(entry["path"])
            source = source_root / relative
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target, follow_symlinks=False)
            target.chmod(int(entry["mode"]) & ~0o222)
            with target.open("rb") as handle:
                os.fsync(handle.fileno())
        installed_manifest = staged / MANIFEST_NAME
        installed_manifest.write_bytes(manifest_path.read_bytes())
        installed_manifest.chmod(0o444)
        with installed_manifest.open("rb") as handle:
            os.fsync(handle.fileno())
        launch = staged / LAUNCH_NAME
        launch.write_bytes(canonical_wrapper_bytes(destination))
        launch.chmod(0o555)
        with launch.open("rb") as handle:
            os.fsync(handle.fileno())
        for directory in sorted(
            (path for path in staged.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.chmod(0o555)
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        staged.chmod(0o555)
        descriptor = os.open(staged, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        verify(staged, installed_manifest, immutable=True)
        launch_record(launch, destination)
        os.rename(staged, destination)
        descriptor = os.open(generations_root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if staged.exists():
            for directory in [staged, *staged.rglob("*")]:
                if directory.is_dir() and not directory.is_symlink():
                    directory.chmod(0o755)
            shutil.rmtree(staged)
    return {
        "created": True,
        "root": str(destination),
        "manifest": str(destination / MANIFEST_NAME),
        "launch_entrypoint": str(destination / LAUNCH_NAME),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci support-manifest")
    sub = parser.add_subparsers(dest="command", required=True)
    write_cmd = sub.add_parser("write")
    write_cmd.add_argument("--root", type=Path, required=True)
    write_cmd.add_argument("--output", type=Path, required=True)
    verify_cmd = sub.add_parser("verify")
    verify_cmd.add_argument("manifest", type=Path)
    verify_cmd.add_argument("--root", type=Path, required=True)
    verify_cmd.add_argument("--immutable", action="store_true")
    stage_cmd = sub.add_parser("stage-install")
    stage_cmd.add_argument("manifest", type=Path)
    stage_cmd.add_argument("--source-root", type=Path, required=True)
    stage_cmd.add_argument("--generations-root", type=Path, required=True)
    wrapper_write = sub.add_parser("wrapper-write")
    wrapper_write.add_argument("path", type=Path)
    wrapper_write.add_argument("--support-root", type=Path, required=True)
    wrapper_verify = sub.add_parser("wrapper-verify")
    wrapper_verify.add_argument("path", type=Path)
    wrapper_verify.add_argument("--support-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "write":
            write(args.root, args.output)
            print(args.output)
        elif args.command == "stage-install":
            print(json.dumps(stage_install(
                args.source_root, args.manifest, args.generations_root
            ), sort_keys=True))
        elif args.command == "wrapper-write":
            write_wrapper(args.path, args.support_root)
            print(args.path)
        elif args.command == "wrapper-verify":
            print(json.dumps(wrapper_record(args.path, args.support_root), sort_keys=True))
        else:
            manifest = verify(
                args.root, args.manifest, immutable=args.immutable
            )
            print(f"verified TartCI support cohort at {manifest['source_commit']}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"support-manifest: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
