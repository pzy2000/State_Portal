"""Candidate-specific, source-preserving C++17 override for Luanti #11866."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
_BLOCKER = ArtifactReference(
    "candidate_gate57_11866_irrlicht_build_blocker/luanti/11866/native/v2",
    "candidate_gate57_11866_irrlicht_build_blocker",
    "1.0",
)
_OVERRIDE = ArtifactReference(
    "candidate_gate57_11866_cxx17_override/luanti/11866/native/v1",
    "candidate_gate57_11866_cxx17_override",
    "1.0",
)
_LAUNCHER = "private/fresh-gate57-tools/luanti/11866/cpp17-v1/cxx17-launcher"
_CONTENT = (
    b'#!/bin/sh\nset -eu\ncompiler="$1"\nshift\nexec "$compiler" "$@" -std=c++17\n'
)
_CHAIN = (_STATIC, _COMPATIBILITY, _DEPENDENCY, _BLOCKER)


def register_luanti_11866_cxx17_override(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Register a source-preserving C++17 compiler-launcher override."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#11866 C++17 override authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    _validate_chain(chain)
    launcher = root / _LAUNCHER
    if launcher.exists() or launcher.is_symlink():
        if launcher.is_symlink() or launcher.read_bytes() != _CONTENT:
            raise RuntimeError("#11866 C++17 launcher drifted")
    else:
        launcher.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        launcher.write_bytes(_CONTENT)
        launcher.chmod(0o700)
    payload = {
        "candidate": _CANDIDATE,
        "state": "registered",
        "outcome": "source_preserving_cxx17_override",
        "terminal": False,
        "resumable": True,
        "fresh_namespace": "irrlicht-v3",
        "wrapper_path": _LAUNCHER,
        "content_fingerprint": hashlib.sha256(_CONTENT).hexdigest(),
        "language_standard": "c++17",
        "configure_overrides": [
            "-DCMAKE_CXX_STANDARD=17",
            "-DCMAKE_CXX_STANDARD_REQUIRED=TRUE",
            "-DCMAKE_CXX_EXTENSIONS=OFF",
            f"-DCMAKE_CXX_COMPILER_LAUNCHER={_LAUNCHER}",
        ],
        "source_cmake_reassigns_cxx11": True,
        "launcher_appends_final_cxx17_flag": True,
        "production_source_bytes_modified": False,
        "test_source_bytes_modified": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "input_chain": {
            "static_gate34": _STATIC.artifact_id,
            "irrlicht_compatibility": _COMPATIBILITY.artifact_id,
            "irrlicht_dependency": _DEPENDENCY.artifact_id,
            "prior_build_blocker": _BLOCKER.artifact_id,
        },
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != _CHAIN or dict(existing.payload) != payload:
            raise RuntimeError("#11866 C++17 override conflicts with exact state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_OVERRIDE.artifact_id,
        artifact_kind=_OVERRIDE.artifact_kind,
        semantic_version=_OVERRIDE.semantic_version,
        created_at=_now(),
        inputs=_CHAIN,
        payload=payload,
        authorization=authorization,
    )
    return _load(root, _OVERRIDE)


def load_luanti_11866_cxx17_override(
    artifact_root: str | Path,
) -> StableArtifact:
    root = Path(artifact_root).absolute().resolve(strict=True)
    artifact = _load(root, _OVERRIDE)
    launcher = root / _LAUNCHER
    if (
        not launcher.is_file()
        or launcher.is_symlink()
        or launcher.read_bytes() != _CONTENT
    ):
        raise RuntimeError("#11866 C++17 override launcher is unavailable or drifted")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path) -> StableArtifact | None:
    try:
        return _load(root, _OVERRIDE)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#11866 C++17 override chain candidate binding drifted")
    static, compatibility, dependency, blocker = chain
    if static.payload.get("outcome") != "gate3_4_static_passed_pending_gate57":
        raise RuntimeError("#11866 Gate 3/4 static audit is not current")
    if compatibility.payload.get("outcome") != "irrlichtmt_headers_compatible":
        raise RuntimeError("#11866 Irrlicht compatibility audit is not current")
    if dependency.payload.get("complete") is not True:
        raise RuntimeError("#11866 Irrlicht dependency is incomplete")
    if (
        blocker.payload.get("reason")
        != "irrlicht_headers_require_cxx17_but_candidate_cmake_is_cxx11"
    ):
        raise RuntimeError("#11866 C++17 override is not bound to the observed blocker")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "load_luanti_11866_cxx17_override",
    "register_luanti_11866_cxx17_override",
]
