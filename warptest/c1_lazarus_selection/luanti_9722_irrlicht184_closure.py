"""Typed Irrlicht 1.8.4 source and server-build closure for Luanti #9722."""

from __future__ import annotations

import hashlib
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 9722}
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/9722/fixed-buggy/v2",
    "candidate_materialization",
    "2.0",
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_aggregate/luanti/9722/lua/v1",
    "candidate_gate57_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_transition/luanti/9722/lua/v1",
    "candidate_gate57_transition",
    "1.0",
)
_OLD_BLOCKER = ArtifactReference(
    "candidate_gate8_9722_runtime_blocker/luanti/9722/current-v2",
    "candidate_gate8_9722_runtime_blocker",
    "1.0",
)
_SOURCE = ArtifactReference(
    "candidate_gate8_9722_irrlicht184_source/luanti/9722/current-v2/v1",
    "candidate_gate8_9722_irrlicht184_source",
    "1.0",
)
_BUILD = ArtifactReference(
    "candidate_gate8_9722_native_build/luanti/9722/irrlicht184-v1",
    "candidate_gate8_9722_native_build",
    "1.0",
)
_PAIR = PurePosixPath("private/materialized/luanti/9722/application-v2")
_DEP = PurePosixPath("private/dependencies/luanti-9722-irrlicht/1.8.4")
_BUILD_ROOT = PurePosixPath("private/bounded-builds/luanti/9722/native-irrlicht184-v1")
_REMOTE = "https://downloads.sourceforge.net/irrlicht/irrlicht-1.8.4.zip"
_ARCHIVE_SHA256 = "f42b280bc608e545b820206fe2a999c55f290de5c7509a02bdbeeccc1bf9e433"


def write_luanti_9722_irrlicht184_closure(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact]:
    """Persist the already-completed candidate-specific source/build closure."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#9722 Irrlicht 1.8.4 closure authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    materialization, aggregate, transition, old_blocker = (
        _load(root, ref)
        for ref in (_MATERIALIZATION, _AGGREGATE, _TRANSITION, _OLD_BLOCKER)
    )
    if (
        materialization.payload.get("candidate") != _CANDIDATE
        or materialization.payload.get("state") != "materialized"
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
        or transition.payload.get("state") != "passed"
        or old_blocker.payload.get("terminal") is not False
    ):
        raise RuntimeError("#9722 typed input chain drifted")

    fixed = _ordinary_dir(root / _PAIR / "fixed", root)
    buggy = _ordinary_dir(root / _PAIR / "buggy", root)
    for tree in (fixed, buggy):
        if (tree / "misc/irrlichtmt_tag.txt").exists():
            raise RuntimeError("#9722 unexpectedly declares an IrrlichtMt tag")
        evidence = _ordinary_file(tree / "util/buildbot/buildwin64.sh", root).read_text(
            encoding="utf-8"
        )
        if "irrlicht_version=1.8.4" not in evidence:
            raise RuntimeError("#9722 Irrlicht version evidence drifted")

    archive = _ordinary_file(root / _DEP / "irrlicht-1.8.4.zip", root)
    source_tree = _ordinary_dir(root / _DEP / "source", root)
    if _sha256(archive) != _ARCHIVE_SHA256:
        raise RuntimeError("#9722 authoritative Irrlicht archive fingerprint drifted")
    inventory, tree_fingerprint = _archive_inventory(archive, source_tree)
    source_payload = {
        "candidate": _CANDIDATE,
        "state": "registered",
        "complete": True,
        "dependency_family": "Irrlicht",
        "not_irrlichtmt": True,
        "release_tag": "1.8.4",
        "remote": _REMOTE,
        "archive_path": f"{_DEP.as_posix()}/irrlicht-1.8.4.zip",
        "archive_fingerprint": _ARCHIVE_SHA256,
        "commit": None,
        "commit_availability": "not_applicable_authoritative_sourceforge_release_archive",
        "source_tree_path": f"{_DEP.as_posix()}/source",
        "release_tree_fingerprint": tree_fingerprint,
        "inventory": inventory,
        "version_evidence": [
            f"{_PAIR.as_posix()}/fixed/util/buildbot/buildwin64.sh",
            f"{_PAIR.as_posix()}/buggy/util/buildbot/buildwin64.sh",
        ],
        "candidate_specific_namespace": True,
        "source_from_other_candidate": False,
        "network_action": "read_only_authoritative_release_download",
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    source = _write_once(
        root,
        _SOURCE,
        (materialization.reference, aggregate.reference, transition.reference, old_blocker.reference),
        source_payload,
        authorization,
    )

    binaries = {
        variant: _ordinary_executable(root / _PAIR / variant / "bin/minetestserver", root)
        for variant in ("fixed", "buggy")
    }
    library = _ordinary_file(root / _DEP / "lib/MacOSX/libIrrlicht.a", root)
    build_ninja = {
        variant: _ordinary_file(root / _BUILD_ROOT / variant / "build.ninja", root).read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        for variant in ("fixed", "buggy")
    }
    include_marker = f"{(root / _DEP / 'source/include').as_posix()}"
    library_marker = f"{(root / _DEP / 'lib/MacOSX/libIrrlicht.a').as_posix()}"
    if any(include_marker not in value for value in build_ninja.values()):
        raise RuntimeError("#9722 build did not use candidate-specific Irrlicht headers")
    if any(library_marker in value for value in build_ninja.values()):
        raise RuntimeError("#9722 server unexpectedly links Irrlicht runtime symbols")
    for variant in ("fixed", "buggy"):
        cache = _ordinary_file(root / _BUILD_ROOT / variant / "CMakeCache.txt", root).read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        required = (
            "BUILD_CLIENT:BOOL=FALSE",
            "BUILD_SERVER:BOOL=TRUE",
            "ENABLE_FREETYPE:BOOL=FALSE",
            "ENABLE_CURSES:BOOL=FALSE",
            f"IRRLICHT_INCLUDE_DIR:PATH={include_marker}",
            f"IRRLICHT_LIBRARY:FILEPATH={library_marker}",
        )
        if any(marker not in cache for marker in required):
            raise RuntimeError(f"#9722 {variant} closed configure cache drifted")
    build_payload = {
        "candidate": _CANDIDATE,
        "state": "built",
        "outcome": "fixed_buggy_native_server_build_passed",
        "complete": True,
        "dependency": source.reference.artifact_id,
        "dependency_release": "Irrlicht 1.8.4",
        "fixed_binary": binaries["fixed"].relative_to(root).as_posix(),
        "buggy_binary": binaries["buggy"].relative_to(root).as_posix(),
        "fixed_binary_fingerprint": _sha256(binaries["fixed"]),
        "buggy_binary_fingerprint": _sha256(binaries["buggy"]),
        "build_root": _BUILD_ROOT.as_posix(),
        "single_threaded": True,
        "build_parallelism": 1,
        "source_pair_modified": False,
        "closed_configure": {
            "build_client": False,
            "build_server": True,
            "freetype": False,
            "curses": False,
            "irrlicht_headers": f"{_DEP.as_posix()}/source/include",
            "host_freetype_or_ncurses_linkage": False,
        },
        "server_irrlicht_contract": {
            "headers_from_authoritative_1_8_4_release": True,
            "runtime_symbols_required": False,
            "link_placeholder_archive": library.relative_to(root).as_posix(),
            "link_placeholder_fingerprint": _sha256(library),
            "placeholder_contains_only_empty_object": True,
            "build_ninja_contains_irrlicht_runtime_library": False,
        },
        "prior_1_9_0_15_blocker_superseded": True,
        "prior_blocker_was_cross_candidate_dependency": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "fresh candidate-bound registered Start/replay/oracle using these two server binaries",
    }
    build = _write_once(
        root,
        _BUILD,
        (materialization.reference, aggregate.reference, transition.reference, source.reference),
        build_payload,
        authorization,
    )
    return source, build


def _archive_inventory(archive: Path, source_tree: Path) -> tuple[dict[str, int], str]:
    digest = hashlib.sha256()
    files = directories = total_bytes = 0
    with zipfile.ZipFile(archive) as package:
        for info in sorted(package.infolist(), key=lambda item: item.filename):
            if not info.filename.startswith("irrlicht-1.8.4/") or ".." in PurePosixPath(info.filename).parts:
                raise RuntimeError("#9722 Irrlicht archive inventory is unsafe")
            relative = PurePosixPath(info.filename).relative_to("irrlicht-1.8.4")
            if info.is_dir():
                directories += 1
                continue
            files += 1
            content = package.read(info)
            total_bytes += len(content)
            extracted = _ordinary_file(source_tree / Path(*relative.parts), source_tree.parent)
            if extracted.read_bytes() != content:
                raise RuntimeError("#9722 extracted Irrlicht release differs from archive")
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(content).digest())
    return {"files": files, "directories": directories, "bytes": total_bytes}, digest.hexdigest()


def _write_once(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    existing = _try_load(root, reference)
    if existing is None:
        write_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
    elif existing.inputs != inputs or dict(existing.payload) != payload:
        raise RuntimeError(f"#9722 artifact conflicts: {reference.artifact_id}")
    result = _load(root, reference)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError(f"#9722 typed reload failed: {reference.artifact_id}")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ordinary_dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("#9722 closure directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("#9722 closure file is unsafe")
    return resolved


def _ordinary_executable(path: Path, root: Path) -> Path:
    result = _ordinary_file(path, root)
    if not os.access(result, os.X_OK):
        raise RuntimeError("#9722 server binary is not executable")
    return result


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_9722_irrlicht184_closure"]
