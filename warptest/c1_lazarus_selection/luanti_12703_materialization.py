"""Typed, candidate-owned Gate-8 materialization for Luanti #12703.

This module starts no build.  It validates the already completed fixed/buggy
client builds, the exact production-only inverse, and the #12703-owned
IrrlichtMt 1.9.0mt7 checkout before registering an immutable aggregate.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

from .luanti_lua_regression import profile_for_candidate
from .luanti_lua_visual_closure import evaluate_luanti_lua_version_materialization
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("luanti", 12703)
_VALUE = _CANDIDATE.to_value()
_FIXED_COMMIT = "adc89f7977ae4b1e86cb413fb52406865150b064"
_BUGGY_COMMIT = "cae7ec1eb4c5fb56a9f4666a16d5937ac1e4c527"
_IRR_COMMIT = "ff645cc8764ab3fc79f0e67677a328ba36c35087"
_IRR_TREE = "private/dependencies/luanti-12703-irrlicht/1.9.0mt7/source"
_IRR_BUILD = "private/dependencies/luanti-12703-irrlicht/1.9.0mt7/build"
_PAIR_ROOT = "private/luanti-12703-formal-materialization"
_FIXED_AUTHORITY = "private/materialized/luanti/12703/source-fixed-v1"
_BUGGY_AUTHORITY = "private/materialized/luanti/12703/source-buggy-v1"
_DEPENDENCY = ArtifactReference(
    "candidate_gate8_12703_irrlicht_dependency/luanti/12703/1.9.0mt7/v1",
    "candidate_gate8_12703_irrlicht_dependency",
    "1.0",
)
_SOURCE_PAIR = ArtifactReference(
    "candidate_gate8_12703_source_pair/luanti/12703/production-only/v1",
    "candidate_gate8_12703_source_pair",
    "1.0",
)
_BUILDS = {
    variant: ArtifactReference(
        f"candidate_gate8_12703_client_build/luanti/12703/{variant}/v1",
        "candidate_gate8_12703_client_build",
        "1.0",
    )
    for variant in ("fixed", "buggy")
}
MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/12703/fixed-buggy/v1",
    "candidate_materialization",
    "1.0",
)


def register_luanti_12703_materialization(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Validate and register the existing candidate-specific application pair."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    boundary = evaluate_luanti_lua_version_materialization(root, candidate=_CANDIDATE)
    transition = _load(root, boundary.unit_transition)
    dependency = _register_dependency(root, transition, authorization)
    source_pair = _register_source_pair(root, transition, authorization)
    builds = tuple(
        _register_build(root, variant, dependency, source_pair, authorization)
        for variant in ("fixed", "buggy")
    )
    payload = {
        "candidate": dict(_VALUE),
        "state": "built",
        "gate8_status": "passed_complete_fixed_buggy_materialization",
        "fixed_tree_path": f"{_PAIR_ROOT}/fixed",
        "buggy_tree_path": f"{_PAIR_ROOT}/buggy",
        "fixed_app_path": f"{_PAIR_ROOT}/fixed/build-app/out/minetest.app",
        "buggy_app_path": f"{_PAIR_ROOT}/buggy/build-app/out/minetest.app",
        "candidate_owned_irrlicht": dependency.reference.artifact_id,
        "source_pair": source_pair.reference.artifact_id,
        "build_receipts": [item.reference.artifact_id for item in builds],
        "production_only_difference": "builtin/mainmenu/pkgmgr.lua",
        "fixed_test_support_bytes_preserved": True,
        "other_candidate_dependency_reused": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }
    return _write_once(
        root,
        MATERIALIZATION,
        (transition.reference, dependency.reference, source_pair.reference, *(item.reference for item in builds)),
        payload,
        authorization,
    )


def load_luanti_12703_materialization(artifact_root: str | Path) -> StableArtifact:
    root = Path(artifact_root).absolute().resolve(strict=True)
    artifact = _load(root, MATERIALIZATION)
    if not (
        artifact.payload.get("candidate") == _VALUE
        and artifact.payload.get("state") == "built"
        and artifact.payload.get("gate8_status")
        == "passed_complete_fixed_buggy_materialization"
        and artifact.payload.get("old_attempts_loaded") is False
        and artifact.payload.get("paid_actions_authorized") is False
        and artifact.payload.get("formal_trials_authorized") is False
        and artifact.payload.get("complete") is True
    ):
        raise RuntimeError("#12703 materialization aggregate is invalid")
    for variant in ("fixed", "buggy"):
        _validate_application(root, variant)
    return artifact


def _register_dependency(
    root: Path,
    transition: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    source = _ordinary_directory(root / _IRR_TREE, root)
    build = _ordinary_directory(root / _IRR_BUILD, root)
    head = _git(source, "rev-parse", "HEAD")
    tag = _git(source, "rev-parse", "refs/tags/1.9.0mt7^{}")
    tree = _git(source, "rev-parse", "HEAD^{tree}")
    remote = _git(source, "remote", "get-url", "origin")
    status_text = _git(source, "status", "--porcelain")
    if (
        head != _IRR_COMMIT
        or tag != _IRR_COMMIT
        or remote != "https://github.com/minetest/irrlicht.git"
        or status_text
    ):
        raise RuntimeError("#12703 IrrlichtMt source authority is invalid")
    tracked = _git_bytes(source, "ls-files", "-z").split(b"\0")
    tracked_paths = [item.decode("utf-8") for item in tracked if item]
    tracked_digest = hashlib.sha256()
    for relative in sorted(tracked_paths):
        path = _ordinary_file(source / PurePosixPath(relative), source)
        tracked_digest.update(relative.encode("utf-8") + b"\0")
        tracked_digest.update(hashlib.sha256(path.read_bytes()).digest())
    products = {
        "cmake/IrrlichtMtConfig.cmake": _file_record(build / "cmake/IrrlichtMtConfig.cmake", root),
        "lib/OSX/libIrrlichtMt.a": _file_record(build / "lib/OSX/libIrrlichtMt.a", root),
    }
    payload = {
        "candidate": dict(_VALUE),
        "state": "built",
        "dependency_id": "luanti-12703-irrlichtmt-1.9.0mt7",
        "authority_remote": remote,
        "authority_tag": "1.9.0mt7",
        "authority_commit": head,
        "authority_tree": tree,
        "source_path": _IRR_TREE,
        "build_path": _IRR_BUILD,
        "tracked_file_count": len(tracked_paths),
        "tracked_inventory_fingerprint": tracked_digest.hexdigest(),
        "build_products": products,
        "candidate_owned": True,
        "other_candidate_dependency_reused": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }
    return _write_once(
        root,
        _DEPENDENCY,
        (transition.reference,),
        payload,
        authorization,
    )


def _register_source_pair(
    root: Path,
    transition: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    profile = profile_for_candidate(_CANDIDATE)
    fixed = _ordinary_directory(root / _PAIR_ROOT / "fixed", root)
    buggy = _ordinary_directory(root / _PAIR_ROOT / "buggy", root)
    fixed_authority = _ordinary_directory(root / _FIXED_AUTHORITY, root)
    buggy_authority = _ordinary_directory(root / _BUGGY_AUTHORITY, root)
    if _git(fixed_authority, "rev-parse", "HEAD") != _FIXED_COMMIT:
        raise RuntimeError("#12703 fixed authority commit is invalid")
    if _git(buggy_authority, "rev-parse", "HEAD") != _BUGGY_COMMIT:
        raise RuntimeError("#12703 buggy authority commit is invalid")
    fixed_inventory = _source_inventory(fixed)
    buggy_inventory = _source_inventory(buggy)
    if set(fixed_inventory) != set(buggy_inventory):
        raise RuntimeError("#12703 source pair inventory differs")
    differences = sorted(
        relative
        for relative in fixed_inventory
        if fixed_inventory[relative] != buggy_inventory[relative]
    )
    if differences != list(profile.production_paths):
        raise RuntimeError("#12703 source pair is not production-only")
    for relative in (*profile.support_paths, *profile.test_paths):
        fixed_bytes = (fixed / PurePosixPath(relative)).read_bytes()
        if fixed_bytes != (buggy / PurePosixPath(relative)).read_bytes():
            raise RuntimeError("#12703 fixed test/support bytes changed")
        capsule = root / PurePosixPath(profile.fixed_source_tree_path) / PurePosixPath(relative)
        if fixed_bytes != _ordinary_file(capsule, root).read_bytes():
            raise RuntimeError("#12703 test/support bytes drifted from fixed capsule")
    production = profile.production_paths[0]
    fixed_product = fixed / PurePosixPath(production)
    buggy_product = buggy / PurePosixPath(production)
    if fixed_product.read_bytes() != (fixed_authority / PurePosixPath(production)).read_bytes():
        raise RuntimeError("#12703 fixed production source is not authoritative")
    if buggy_product.read_bytes() != (buggy_authority / PurePosixPath(production)).read_bytes():
        raise RuntimeError("#12703 buggy production source is not authoritative")
    payload = {
        "candidate": dict(_VALUE),
        "state": "materialized",
        "fixed_commit": _FIXED_COMMIT,
        "buggy_commit": _BUGGY_COMMIT,
        "fixed_tree_path": f"{_PAIR_ROOT}/fixed",
        "buggy_tree_path": f"{_PAIR_ROOT}/buggy",
        "common_inventory_count": len(fixed_inventory),
        "only_different_paths": differences,
        "fixed_inventory_fingerprint": _inventory_digest(fixed_inventory),
        "buggy_inventory_fingerprint": _inventory_digest(buggy_inventory),
        "production_fingerprints": {
            "fixed": hashlib.sha256(fixed_product.read_bytes()).hexdigest(),
            "buggy": hashlib.sha256(buggy_product.read_bytes()).hexdigest(),
        },
        "fixed_test_support_bytes_preserved": True,
        "inverse_scope": "production_only",
        "old_attempts_loaded": False,
        "complete": True,
    }
    return _write_once(
        root,
        _SOURCE_PAIR,
        (transition.reference, profile.fixed_capsule_reference),
        payload,
        authorization,
    )


def _register_build(
    root: Path,
    variant: str,
    dependency: StableArtifact,
    source_pair: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    outputs = _validate_application(root, variant)
    tree = root / _PAIR_ROOT / variant
    cache = _ordinary_file(tree / "build-app/CMakeCache.txt", root).read_text(
        encoding="utf-8", errors="replace"
    )
    required = {
        "BUILD_CLIENT:BOOL=TRUE",
        "BUILD_SERVER:BOOL=FALSE",
        "BUILD_UNITTESTS:BOOL=FALSE",
        "ENABLE_GETTEXT:BOOL=FALSE",
        "ENABLE_SOUND:BOOL=FALSE",
        "RUN_IN_PLACE:BOOL=FALSE",
    }
    if not required.issubset(set(cache.splitlines())):
        raise RuntimeError(f"#12703 {variant} CMake configuration is invalid")
    payload = {
        "candidate": dict(_VALUE),
        "variant": variant,
        "state": "built",
        "tree_path": f"{_PAIR_ROOT}/{variant}",
        "build_path": f"{_PAIR_ROOT}/{variant}/build-app",
        "application_path": f"{_PAIR_ROOT}/{variant}/build-app/out/minetest.app",
        "binary_path": f"{_PAIR_ROOT}/{variant}/bin/minetest",
        "outputs": outputs,
        "build_parallelism": 1,
        "client_enabled": True,
        "server_enabled": False,
        "unit_tests_enabled": False,
        "run_in_place": False,
        "candidate_owned_irrlicht": dependency.reference.artifact_id,
        "application_resource_matches_source": True,
        "unresolved_rpath_dependencies": [],
        "old_build_receipts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }
    return _write_once(
        root,
        _BUILDS[variant],
        (dependency.reference, source_pair.reference),
        payload,
        authorization,
    )


def _validate_application(root: Path, variant: str) -> dict[str, object]:
    tree = _ordinary_directory(root / _PAIR_ROOT / variant, root)
    binary = _ordinary_file(tree / "bin/minetest", tree)
    contents = _ordinary_directory(tree / "build-app/out/minetest.app/Contents", tree)
    app_binary = _ordinary_file(contents / "MacOS/minetest", contents)
    info = _ordinary_file(contents / "Info.plist", contents)
    builtin = _ordinary_directory(contents / "Resources/builtin", contents)
    source_product = _ordinary_file(tree / "builtin/mainmenu/pkgmgr.lua", tree)
    app_product = _ordinary_file(builtin / "mainmenu/pkgmgr.lua", builtin)
    if not os.access(binary, os.X_OK) or not os.access(app_binary, os.X_OK):
        raise RuntimeError(f"#12703 {variant} application is not executable")
    if source_product.read_bytes() != app_product.read_bytes():
        raise RuntimeError(f"#12703 {variant} app resource does not match source")
    linked = subprocess.run(
        ("otool", "-L", str(app_binary)),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.decode("utf-8", errors="replace")
    if "@rpath" in linked:
        raise RuntimeError(f"#12703 {variant} app contains unresolved @rpath")
    return {
        "binary": _file_record(binary, root),
        "app_binary": _file_record(app_binary, root),
        "info_plist": _file_record(info, root),
        "pkgmgr_resource": _file_record(app_product, root),
        "framework_count": len(tuple((contents / "Frameworks").glob("*.dylib"))),
    }


def _source_inventory(tree: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for directory, names, files in os.walk(tree, topdown=True, followlinks=False):
        names[:] = [name for name in names if name not in {".git", "bin", "build-app"}]
        base = Path(directory)
        for name in sorted(files):
            path = base / name
            relative = path.relative_to(tree).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                result[relative] = "symlink:" + os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                result[relative] = "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                raise RuntimeError("#12703 source tree contains an unsafe entry")
    return result


def _inventory_digest(inventory: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative, value in sorted(inventory.items()):
        digest.update(relative.encode("utf-8") + b"\0" + value.encode("utf-8") + b"\0")
    return digest.hexdigest()


def _file_record(path: Path, root: Path) -> dict[str, object]:
    path = _ordinary_file(path, root)
    raw = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "fingerprint": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "executable": os.access(path, os.X_OK),
    }


def _git(cwd: Path, *argv: str) -> str:
    return _git_bytes(cwd, *argv).decode("utf-8").strip()


def _git_bytes(cwd: Path, *argv: str) -> bytes:
    return subprocess.run(
        ("git", *argv),
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "LC_ALL": "C"},
    ).stdout


def _ordinary_directory(path: Path, root: Path) -> Path:
    info = path.lstat()
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve(strict=True))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("#12703 required directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    info = path.lstat()
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve(strict=True))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("#12703 required file is unsafe")
    return resolved


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _write_once(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if existing.inputs != inputs or existing.payload != payload:
            raise RuntimeError(f"#12703 artifact drifted: {reference.artifact_id}")
        return existing
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    return _load(root, reference)


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if not (
        type(authorization) is AuthorizationRecord
        and authorization.scope == "preannotation-execution"
        and not authorization.revoked
        and authorization.local_user_id == os.getuid()
    ):
        raise PermissionError("preannotation execution authorization is required")


__all__ = [
    "MATERIALIZATION",
    "load_luanti_12703_materialization",
    "register_luanti_12703_materialization",
]
