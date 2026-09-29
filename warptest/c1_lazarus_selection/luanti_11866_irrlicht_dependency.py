"""Exact IrrlichtMt compatibility audit and candidate-owned header closure.

Luanti #11866's current CMake server path requires Irrlicht headers even with
the client disabled.  This module first records a read-only compatibility
audit against the exact fixed capsule and retained IrrlichtMt source snapshot,
then copies only that header closure to a #11866-owned immutable dependency
path.  It never reads #12602 build/attempt receipts; the retained source and
all resulting tree digests are bound in the typed receipts.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 11866}
_STATIC = ArtifactReference(
    "candidate_gate34_static_audit/luanti/11866/current-v2/v1",
    "candidate_gate34_static_audit",
    "1.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/11866/fixed/v2", "fixed_source_capsule", "2.0"
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_11866_current_execution_blocker/luanti/11866/native/v1",
    "candidate_gate57_11866_current_execution_blocker",
    "1.0",
)
_COMPATIBILITY = ArtifactReference(
    "candidate_gate57_11866_irrlicht_compatibility/luanti/11866/native/v1",
    "candidate_gate57_11866_irrlicht_compatibility",
    "1.0",
)
_DEPENDENCY = ArtifactReference(
    "candidate_gate57_11866_irrlicht_dependency/luanti/11866/native/v1",
    "candidate_gate57_11866_irrlicht_dependency",
    "1.0",
)
_IRR_SOURCE = PurePosixPath("private/fixed-capsules/luanti/14464/fixed-source/irr")
_IRR_ROOT = PurePosixPath("private/dependencies/luanti-11866-irrlicht/1.9.0.15")
_IRR_INCLUDE = _IRR_ROOT / "include"
_FIXED_ROOT = PurePosixPath("private/fixed-capsules/luanti/11866/fixed-source")
_VERSION = "1.0"


def register_luanti_11866_irrlicht_dependency(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact]:
    """Audit and register the exact #11866-compatible IrrlichtMt headers."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    static, capsule, blocker = tuple(
        _load(root, reference) for reference in (_STATIC, _CAPSULE, _BLOCKER)
    )
    audit_payload = _compatibility_payload(root, static, capsule, blocker)
    compatibility = _write_or_reload(
        root,
        _COMPATIBILITY,
        (_STATIC, _CAPSULE, _BLOCKER),
        audit_payload,
        authorization,
    )
    include = root / _IRR_INCLUDE
    source_include = root / _IRR_SOURCE / "include"
    if include.exists():
        if (
            not include.is_dir()
            or _tree_digest(include) != audit_payload["include_tree_fingerprint"]
        ):
            raise RuntimeError("#11866 IrrlichtMt destination digest drifted")
    else:
        include.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copytree(source_include, include, symlinks=False)
    dependency_payload = {
        "candidate": _CANDIDATE,
        "dependency_id": "irrlichtmt-headers",
        "dependency_version": "1.9.0.15",
        "source_tree_path": _IRR_SOURCE.as_posix(),
        "destination_root": _IRR_ROOT.as_posix(),
        "include_path": _IRR_INCLUDE.as_posix(),
        "source_tree_fingerprint": audit_payload["source_tree_fingerprint"],
        "include_tree_fingerprint": _tree_digest(include),
        "copied_file_count": sum(1 for path in include.rglob("*") if path.is_file()),
        "compatibility_audit": _COMPATIBILITY.artifact_id,
        "network_used": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "state": "materialized",
        "complete": True,
    }
    dependency = _write_or_reload(
        root,
        _DEPENDENCY,
        (_COMPATIBILITY,),
        dependency_payload,
        authorization,
    )
    return compatibility, dependency


def load_luanti_11866_irrlicht_dependency(
    artifact_root: str | Path,
) -> StableArtifact:
    """Load the registered candidate-specific dependency and verify its tree."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    dependency = _load(root, _DEPENDENCY)
    if (
        dependency.payload.get("candidate") != _CANDIDATE
        or dependency.payload.get("complete") is not True
    ):
        raise RuntimeError("#11866 IrrlichtMt dependency receipt is invalid")
    include = root / PurePosixPath(dependency.payload["include_path"])
    if (
        not include.is_dir()
        or _tree_digest(include) != dependency.payload["include_tree_fingerprint"]
    ):
        raise RuntimeError(
            "#11866 IrrlichtMt dependency tree is unavailable or drifted"
        )
    if not (include / "irrlicht.h").is_file():
        raise RuntimeError("#11866 IrrlichtMt dependency lacks irrlicht.h")
    return dependency


def _compatibility_payload(
    root: Path,
    static: StableArtifact,
    capsule: StableArtifact,
    blocker: StableArtifact,
) -> dict[str, object]:
    if static.payload.get("outcome") != "gate3_4_static_passed_pending_gate57":
        raise RuntimeError("#11866 Gate 3/4 static audit is not current")
    if capsule.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#11866 fixed capsule candidate binding is invalid")
    if blocker.payload.get("reason") != "fixed_configure_missing_irrlicht_headers":
        raise RuntimeError(
            "#11866 configure blocker does not identify the Irrlicht gap"
        )
    fixed = root / _FIXED_ROOT
    irr = root / _IRR_SOURCE
    source_cmake = fixed / "CMakeLists.txt"
    source_module = fixed / "cmake/Modules/MinetestFindIrrlichtHeaders.cmake"
    server_cmake = fixed / "src/CMakeLists.txt"
    irr_cmake = irr / "CMakeLists.txt"
    irr_include = irr / "include"
    required = (
        source_cmake,
        source_module,
        server_cmake,
        irr_cmake,
        irr_include / "irrlicht.h",
    )
    if not all(path.is_file() or path.is_dir() for path in required):
        raise RuntimeError("#11866 compatibility audit inputs are incomplete")
    source_text = source_cmake.read_text(encoding="utf-8")
    module_text = source_module.read_text(encoding="utf-8")
    server_text = server_cmake.read_text(encoding="utf-8")
    irr_text = irr_cmake.read_text(encoding="utf-8")
    type_text = (fixed / "src/irrlichttypes.h").read_text(encoding="utf-8")
    required_markers = {
        "source_accepts_include_override": 'set_target_properties(IrrlichtMt::IrrlichtMt PROPERTIES\n\t\t\tINTERFACE_INCLUDE_DIRECTORIES "${IRRLICHT_INCLUDE_DIR}")',
        "source_server_header_failure": "Irrlicht or IrrlichtMt headers are required to build the server",
        "module_accepts_irrlichtmt_and_irrlicht": "foreach(libname IN ITEMS IrrlichtMt Irrlicht)",
        "server_uses_interface_includes": "target_include_directories(${PROJECT_NAME}server PRIVATE ${IRRLICHT_INCLUDES})",
        "server_links_no_irrlicht_library": "target_link_libraries(\n\t\t${PROJECT_NAME}server",
        "luanti_requires_irrlicht_1_9": "IRRLICHT_VERSION_MAJOR == 1 && IRRLICHT_VERSION_MINOR >= 9",
        "irrlicht_revision_15": "set(IRRLICHTMT_REVISION 15)",
        "irrlicht_project_version": "VERSION 1.9.0.${IRRLICHTMT_REVISION}",
    }
    texts = {
        "source_accepts_include_override": source_text,
        "source_server_header_failure": source_text,
        "module_accepts_irrlichtmt_and_irrlicht": module_text,
        "server_uses_interface_includes": server_text,
        "server_links_no_irrlicht_library": server_text,
        "luanti_requires_irrlicht_1_9": type_text,
        "irrlicht_revision_15": irr_text,
        "irrlicht_project_version": irr_text,
    }
    missing = [
        key for key, marker in required_markers.items() if marker not in texts[key]
    ]
    if missing:
        # Do not silently register a closure when any current-CMake/API proof
        # has drifted.
        raise RuntimeError(
            f"#11866 IrrlichtMt compatibility markers missing: {missing}"
        )
    source_tree_fingerprint = _tree_digest(irr)
    include_tree_fingerprint = _tree_digest(irr_include)
    return {
        "candidate": _CANDIDATE,
        "state": "compatible",
        "outcome": "irrlichtmt_headers_compatible",
        "terminal": False,
        "resumable": True,
        "dependency_id": "irrlichtmt-headers",
        "dependency_version": "1.9.0.15",
        "source_tree_path": _IRR_SOURCE.as_posix(),
        "source_tree_fingerprint": source_tree_fingerprint,
        "include_tree_fingerprint": include_tree_fingerprint,
        "include_file_count": sum(
            1 for path in irr_include.rglob("*") if path.is_file()
        ),
        "fixed_capsule_path": _FIXED_ROOT.as_posix(),
        "compatibility": {
            "candidates_current_cmake_module_fingerprint": _sha256(source_module),
            "candidates_current_cmake_fingerprint": _sha256(source_cmake),
            "candidates_server_cmake_fingerprint": _sha256(server_cmake),
            "irrlicht_cmake_fingerprint": _sha256(irr_cmake),
            "required_markers": required_markers,
            "include_override": "-DIRRLICHT_INCLUDE_DIR=<candidate-owned include>",
            "server_link_mode": "headers-only INTERFACE target; no Irrlicht library required when BUILD_CLIENT=FALSE",
            "version_proof": "IrrlichtMt CMake project VERSION 1.9.0.15; Luanti guard requires Irrlicht >=1.9",
        },
        "source_provenance": {
            "retained_source": _IRR_SOURCE.as_posix(),
            "borrowed_candidate_receipts": False,
            "borrowed_attempts": False,
            "network_used": False,
        },
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(
            "#11866 IrrlichtMt registration authorization is inactive"
        )


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _write_or_reload(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
        existing = None
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(
                f"{reference.artifact_id} conflicts with exact current state"
            )
        return existing
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
    return _load(root, reference)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(131072), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    if not root.is_dir():
        raise RuntimeError("IrrlichtMt tree is not a directory")
    digest = hashlib.sha256()
    for path in sorted(
        root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()
    ):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("IrrlichtMt dependency contains an unsafe symlink")
        if stat.S_ISDIR(info.st_mode):
            digest.update(f"D:{relative}\n".encode())
        elif stat.S_ISREG(info.st_mode):
            digest.update(f"F:{relative}:".encode())
            digest.update(_sha256(path).encode())
            digest.update(b"\n")
        else:
            raise RuntimeError("IrrlichtMt dependency contains an unsafe entry")
    return digest.hexdigest()


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "load_luanti_11866_irrlicht_dependency",
    "register_luanti_11866_irrlicht_dependency",
]
