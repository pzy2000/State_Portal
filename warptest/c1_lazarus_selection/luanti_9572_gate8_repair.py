"""Candidate-owned full-application Gate-8 readiness for Luanti #9572."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import zipfile

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 9572}
_FIXED_COMMIT = "7148834440d10bc211628aa2652e31166bdd58a0"
_ARCHIVE_FINGERPRINT = "f42b280bc608e545b820206fe2a999c55f290de5c7509a02bdbeeccc1bf9e433"
_PAIR = PurePosixPath("private/materialized/luanti/9572/current-v2")
_DEP = PurePosixPath("private/dependencies/luanti-9572-irrlicht/1.8.4")
_BUILD = PurePosixPath("private/bounded-builds/luanti/9572/gate8-v1")
_MIRROR = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")

_SOURCE = ArtifactReference("candidate_source_packet/luanti/9572/source/v2", "candidate_source_packet", "2.0")
_PATCH = ArtifactReference("candidate_test_patch/luanti/9572/screening/v2", "candidate_test_patch", "2.0")
_ATOMICITY = ArtifactReference("source_atomicity/luanti/9572/preannotation/v2", "source_atomicity", "2.0")
_CAPSULE = ArtifactReference("candidate_gate57_9572_strict_capsule/luanti/9572/lua/v4", "candidate_gate57_9572_strict_capsule", "1.0")
_AGGREGATE = ArtifactReference("candidate_gate57_9572_strict_aggregate/luanti/9572/lua/v4", "candidate_gate57_9572_strict_aggregate", "1.0")
_TRANSITION = ArtifactReference("candidate_gate57_9572_strict_transition/luanti/9572/lua/v4", "candidate_gate57_9572_strict_transition", "1.0")
_DEPENDENCY = ArtifactReference("candidate_gate8_9572_irrlicht184_source/luanti/9572/current-v2/v1", "candidate_gate8_9572_irrlicht184_source", "1.0")
_SOURCE_PAIR = ArtifactReference("candidate_gate8_9572_source_pair/luanti/9572/production-only/v1", "candidate_gate8_9572_source_pair", "1.0")
_CONFIGURES = {
    variant: ArtifactReference(
        f"candidate_gate8_9572_configure/luanti/9572/{variant}/v1",
        "candidate_gate8_9572_configure",
        "1.0",
    )
    for variant in ("fixed", "buggy")
}
_READINESS = ArtifactReference("candidate_gate8_readiness/luanti/9572/current-v2/v2", "candidate_gate8_readiness", "2.0")
_CHAIN = (_SOURCE, _PATCH, _ATOMICITY, _CAPSULE, _AGGREGATE, _TRANSITION)


def register_luanti_9572_gate8_repair(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, ...]:
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    dependency = _register_dependency(root, chain, authorization)
    pair = _register_pair(root, chain, dependency, authorization)
    configures = tuple(
        _register_configure(root, variant, chain, dependency, pair, authorization)
        for variant in ("fixed", "buggy")
    )
    readiness = _register_readiness(
        root, chain, dependency, pair, configures, authorization
    )
    return dependency, pair, *configures, readiness


def _load_chain(root: Path) -> tuple[StableArtifact, ...]:
    values = tuple(_load(root, ref) for ref in _CHAIN)
    source, patch, atomicity, capsule, aggregate, transition = values
    if source.payload.get("candidate") != _CANDIDATE or source.payload.get("complete") is not True:
        raise RuntimeError("#9572 source packet drifted")
    if (
        patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != ["builtin/common/vector.lua"]
        or patch.payload.get("test_paths") != ["builtin/common/tests/vector_spec.lua"]
    ):
        raise RuntimeError("#9572 patch receipt drifted")
    if (
        atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 9798
    ):
        raise RuntimeError("#9572 atomicity receipt drifted")
    if capsule.payload.get("merge_revision") != _FIXED_COMMIT:
        raise RuntimeError("#9572 strict capsule drifted")
    if (
        aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
        or aggregate.payload.get("stable_repetitions") is not True
    ):
        raise RuntimeError("#9572 strict aggregate drifted")
    if transition.payload.get("state") != "passed" or transition.payload.get("terminal") is not False:
        raise RuntimeError("#9572 strict transition drifted")
    return values


def _register_dependency(
    root: Path, chain: tuple[StableArtifact, ...], authorization: AuthorizationRecord
) -> StableArtifact:
    fixed = _dir(root / _PAIR / "fixed", root)
    evidence = {
        "win64": fixed / "util/buildbot/buildwin64.sh",
        "win32": fixed / "util/buildbot/buildwin32.sh",
    }
    if any("irrlicht_version=1.8.4" not in _file(path, root).read_text() for path in evidence.values()):
        raise RuntimeError("#9572 Irrlicht pin drifted")
    archive = _file(root / _DEP / "irrlicht-1.8.4.zip", root)
    if _fingerprint(archive) != _ARCHIVE_FINGERPRINT:
        raise RuntimeError("#9572 Irrlicht archive drifted")
    source = _dir(root / _DEP / "source", root)
    count, total, release = _archive_inventory(archive, source)
    placeholder = _file(root / _DEP / "link-placeholder/libIrrlicht.a", root)
    payload = {
        "candidate": _CANDIDATE,
        "state": "registered",
        "complete": True,
        "dependency_family": "Irrlicht",
        "release": "1.8.4",
        "authority_remote": "https://downloads.sourceforge.net/irrlicht/irrlicht-1.8.4.zip",
        "archive_path": f"{_DEP.as_posix()}/irrlicht-1.8.4.zip",
        "archive_fingerprint": _ARCHIVE_FINGERPRINT,
        "source_path": f"{_DEP.as_posix()}/source",
        "release_tree_fingerprint": release,
        "file_count": count,
        "total_bytes": total,
        "version_evidence": {key: path.relative_to(root).as_posix() for key, path in evidence.items()},
        "server_link_placeholder": placeholder.relative_to(root).as_posix(),
        "server_link_placeholder_fingerprint": _fingerprint(placeholder),
        "candidate_owned": True,
        "source_from_other_candidate": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(root, _DEPENDENCY, tuple(item.reference for item in chain), payload, authorization)


def _register_pair(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    mirror = _dir(root / _MIRROR, root)
    fixed = _dir(root / _PAIR / "fixed", root)
    buggy = _dir(root / _PAIR / "buggy", root)
    entries = _git(mirror, "ls-tree", "-r", _FIXED_COMMIT).splitlines()
    differences: list[str] = []
    left_value, right_value = hashlib.sha256(), hashlib.sha256()
    for entry in entries:
        meta, relative = entry.split("\t", 1)
        mode = meta.split(" ", 1)[0]
        left = _tree_bytes(fixed / PurePosixPath(relative), mode, root)
        right = _tree_bytes(buggy / PurePosixPath(relative), mode, root)
        for value, content in ((left_value, left), (right_value, right)):
            value.update(relative.encode() + b"\0" + mode.encode() + b"\0")
            value.update(hashlib.sha256(content).digest())
        if left != right:
            differences.append(relative)
    if differences != ["builtin/common/vector.lua"]:
        raise RuntimeError("#9572 pair is not production-only")
    test = "builtin/common/tests/vector_spec.lua"
    if _file(fixed / test, root).read_bytes() != _file(buggy / test, root).read_bytes():
        raise RuntimeError("#9572 fixed test bytes changed")
    payload = {
        "candidate": _CANDIDATE,
        "state": "materialized",
        "complete": True,
        "fixed_tree_path": f"{_PAIR.as_posix()}/fixed",
        "buggy_tree_path": f"{_PAIR.as_posix()}/buggy",
        "tracked_file_count": len(entries),
        "only_different_paths": differences,
        "fixed_inventory_fingerprint": left_value.hexdigest(),
        "buggy_inventory_fingerprint": right_value.hexdigest(),
        "fixed_test_support_bytes_preserved": True,
        "inverse_scope": "production_only",
        "candidate_owned_irrlicht": dependency.reference.artifact_id,
        "other_candidate_assets_reused": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in chain) + (dependency.reference,)
    return _write(root, _SOURCE_PAIR, inputs, payload, authorization)


def _register_configure(
    root: Path,
    variant: str,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    pair: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    build = _dir(root / _BUILD / variant, root)
    cache_path, ninja_path = _file(build / "CMakeCache.txt", root), _file(build / "build.ninja", root)
    cache, ninja = cache_path.read_text(errors="replace"), ninja_path.read_text(errors="replace")
    include = (root / _DEP / "source/include").as_posix()
    library = (root / _DEP / "link-placeholder/libIrrlicht.a").as_posix()
    required = (
        "BUILD_CLIENT:BOOL=FALSE",
        "BUILD_SERVER:BOOL=TRUE",
        f"IRRLICHT_INCLUDE_DIR:PATH={include}",
        f"IRRLICHT_LIBRARY:FILEPATH={library}",
        "CMAKE_EXE_LINKER_FLAGS:STRING=-Wl,-pagezero_size,0x100000000",
    )
    if any(marker not in cache for marker in required) or include not in ninja:
        raise RuntimeError(f"#9572 {variant} configure drifted")
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "state": "configured",
        "outcome": "closed_minimal_server_configure_passed",
        "complete": True,
        "source_tree_path": f"{_PAIR.as_posix()}/{variant}",
        "build_tree_path": f"{_BUILD.as_posix()}/{variant}",
        "cache_fingerprint": _fingerprint(cache_path),
        "build_graph_fingerprint": _fingerprint(ninja_path),
        "target": "minetestserver",
        "planned_steps": sum(1 for line in _dry_run(root, build).splitlines() if line.startswith("[")),
        "build_started": False,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in chain) + (dependency.reference, pair.reference)
    return _write(root, _CONFIGURES[variant], inputs, payload, authorization)


def _register_readiness(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    pair: StableArtifact,
    configures: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "state": "configured_waiting_native_lane",
        "outcome": "gate8_full_pair_dependency_configure_complete_build_pending",
        "complete": True,
        "terminal": False,
        "resumable": True,
        "current_gate_status": {
            "gate_1_7": "qualified_strict_v4",
            "gate8_lua_capsule": "passed",
            "gate8_full_fixed_buggy_application_pair": "passed",
            "gate8_candidate_owned_dependency": "passed",
            "gate8_fixed_buggy_configure": "passed",
            "gate8_fixed_buggy_build": "pending_native_lane",
            "gate9_to_13": "pending",
        },
        "candidate_owned_dependency": dependency.reference.artifact_id,
        "source_pair": pair.reference.artifact_id,
        "configure_receipts": [item.reference.artifact_id for item in configures],
        "prior_support_blockers_superseded_by_strict_v4": True,
        "other_candidate_assets_reused": False,
        "new_build_started": False,
        "does_not_invalidate_gate_1_7": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "after native-lane release, build fixed then buggy sequentially and register current-v2 application receipts",
    }
    inputs = tuple(item.reference for item in chain) + (
        dependency.reference,
        pair.reference,
        *(item.reference for item in configures),
    )
    return _write(root, _READINESS, inputs, payload, authorization)


def _archive_inventory(archive: Path, source: Path) -> tuple[int, int, str]:
    value = hashlib.sha256(); count = total = 0
    with zipfile.ZipFile(archive) as package:
        for info in sorted(package.infolist(), key=lambda item: item.filename):
            if info.is_dir(): continue
            relative = PurePosixPath(info.filename).relative_to("irrlicht-1.8.4")
            content = package.read(info)
            if _file(source / Path(*relative.parts), source.parent).read_bytes() != content:
                raise RuntimeError("#9572 Irrlicht extraction drifted")
            count += 1; total += len(content)
            value.update(relative.as_posix().encode() + b"\0" + hashlib.sha256(content).digest())
    return count, total, value.hexdigest()


def _dry_run(root: Path, build: Path) -> str:
    process = subprocess.run(
        (str(root / "private/dependencies/luanti-native/3.0/bin/ninja"), "-C", str(build), "-n", "minetestserver"),
        capture_output=True, check=False, timeout=30
    )
    if process.returncode != 0: raise RuntimeError("#9572 target dry-run failed")
    return process.stdout.decode(errors="replace")


def _require_auth(value: AuthorizationRecord) -> None:
    if type(value) is not AuthorizationRecord or value.scope != "preannotation-execution" or value.revoked or value.local_user_id != os.getuid():
        raise PermissionError("#9572 authorization inactive")


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version)


def _write(root: Path, ref: ArtifactReference, inputs: tuple[ArtifactReference, ...], payload: dict[str, object], auth: AuthorizationRecord) -> StableArtifact:
    try: existing = _load(root, ref)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}: raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload: raise RuntimeError(f"#9572 artifact conflict {ref.artifact_id}")
        return existing
    write_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=auth)
    return _load(root, ref)


def _dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True); resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir(): raise RuntimeError("unsafe directory")
    return resolved


def _file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True); resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file(): raise RuntimeError("unsafe file")
    return resolved


def _tree_bytes(path: Path, mode: str, root: Path) -> bytes:
    info = path.lstat()
    if mode == "120000":
        if not stat.S_ISLNK(info.st_mode): raise RuntimeError("symlink drift")
        return os.readlink(path).encode()
    return _file(path, root).read_bytes()


def _fingerprint(path: Path) -> str:
    value = hashlib.sha256(); value.update(path.read_bytes()); return value.hexdigest()


def _git(cwd: Path, *args: str) -> str:
    process = subprocess.run(("git", "-C", str(cwd), *args), capture_output=True, check=False)
    if process.returncode != 0: raise RuntimeError(process.stderr.decode(errors="replace")[:500])
    return process.stdout.decode().strip()


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["register_luanti_9572_gate8_repair"]
