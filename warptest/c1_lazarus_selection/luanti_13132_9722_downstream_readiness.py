"""Read-only downstream readiness comparison for Luanti #13132 and #9722.

Neither candidate currently has a candidate-matching immutable IrrlichtMt
source closure suitable for a source-preserving native build.  #13132 is the
preferred future lane because its own source expects IrrlichtMt and can raise
the compiler dialect without changing TextureLayer call sites.  #9722's
registered Irrlicht source is explicitly derived from #14464 and has already
shown source/API incompatibility.  No attempt receipt is loaded here.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_REFS = {
    "13132_aggregate": ArtifactReference(
        "candidate_gate57_13132_timeout_repair_aggregate/luanti/13132/lua/v1",
        "candidate_gate57_13132_timeout_repair_aggregate",
        "1.0",
    ),
    "13132_transition": ArtifactReference(
        "candidate_gate57_13132_timeout_repair_transition/luanti/13132/lua/v1",
        "candidate_gate57_13132_timeout_repair_transition",
        "1.0",
    ),
    "13132_build": ArtifactReference(
        "candidate_build_receipt/luanti/13132/fixed-buggy/v1",
        "candidate_build_receipt",
        "1.0",
    ),
    "13132_blocker": ArtifactReference(
        "candidate_downstream_blocker/luanti/13132/current-v2",
        "candidate_downstream_blocker",
        "1.0",
    ),
    "9722_aggregate": ArtifactReference(
        "candidate_gate57_aggregate/luanti/9722/lua/v1",
        "candidate_gate57_aggregate",
        "1.0",
    ),
    "9722_transition": ArtifactReference(
        "candidate_gate57_transition/luanti/9722/lua/v1",
        "candidate_gate57_transition",
        "1.0",
    ),
    "9722_materialization": ArtifactReference(
        "candidate_materialization/luanti/9722/fixed-buggy/v2",
        "candidate_materialization",
        "2.0",
    ),
    "9722_dependency": ArtifactReference(
        "candidate_gate8_9722_irrlicht_dependency/luanti/9722/current-v2",
        "candidate_gate8_9722_irrlicht_dependency",
        "1.0",
    ),
    "9722_dependency_build": ArtifactReference(
        "candidate_gate8_9722_irrlicht_build/luanti/9722/current-v2",
        "candidate_gate8_9722_irrlicht_build",
        "1.0",
    ),
    "9722_blocker": ArtifactReference(
        "candidate_gate8_9722_runtime_blocker/luanti/9722/current-v2",
        "candidate_gate8_9722_runtime_blocker",
        "1.0",
    ),
}
_BLOCKER = ArtifactReference(
    "candidate_downstream_readiness_comparison/luanti/13132-9722/current-v2/v1",
    "candidate_downstream_readiness_comparison",
    "1.0",
)
_SOURCE_13132 = PurePosixPath("private/candidate-source/luanti/13132/fixed")
_SOURCE_9722 = PurePosixPath("private/materialized/luanti/9722/application-v2/fixed")
_IRR_9722 = PurePosixPath("private/dependencies/luanti-9722-irrlicht/1.0/source")


def write_luanti_13132_9722_downstream_readiness(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Write a plan/blocker without starting another native build."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#13132/#9722 readiness authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    loaded = {name: _load(root, ref) for name, ref in _REFS.items()}
    _validate_artifacts(loaded)

    source_13132 = _ordinary_dir(root / _SOURCE_13132, root)
    source_9722 = _ordinary_dir(root / _SOURCE_9722, root)
    irr_9722 = _ordinary_dir(root / _IRR_9722, root)
    cmake_13132 = _ordinary_file(source_13132 / "CMakeLists.txt", root).read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    cmake_9722 = _ordinary_file(source_9722 / "CMakeLists.txt", root).read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    tile_13132 = _ordinary_file(source_13132 / "src/client/tile.h", root).read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    tile_9722 = _ordinary_file(source_9722 / "src/client/tile.h", root).read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    irr_cmake = _ordinary_file(irr_9722 / "CMakeLists.txt", root).read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    if (
        "set(CMAKE_CXX_STANDARD 14)" not in cmake_13132
        or "set(CMAKE_CXX_STANDARD 11)" not in cmake_9722
        or "TextureLayer" not in tile_13132
        or "TextureLayer" not in tile_9722
        or "IRRLICHTMT_REVISION 15" not in irr_cmake
    ):
        raise RuntimeError("#13132/#9722 source compatibility evidence drifted")
    candidate_13132_irr = root / "private/dependencies/luanti-13132-irrlicht/1.0/source"
    if candidate_13132_irr.exists():
        raise RuntimeError("#13132 Irrlicht source appeared; re-audit required")

    payload: dict[str, Any] = {
        "state": "blocked",
        "outcome": "no_candidate_matching_irrlicht_source_ready",
        "terminal": False,
        "resumable": True,
        "recommended_candidate": 13132,
        "candidate_comparison": {
            "13132": {
                "gate_1_7": "qualified",
                "candidate_application_source": _SOURCE_13132.as_posix(),
                "application_source_present": True,
                "declared_cpp_standard": 14,
                "source_preserving_cpp17_override_possible": True,
                "texture_layer_calls_preserved": True,
                "required_irrlicht": "IrrlichtMt 1.9.0mt9-compatible",
                "candidate_matching_irrlicht_source_registered": False,
                "ready_for_second_native_build": False,
                "ranking": "preferred_after_exact_irrlichtmt_closure_registration",
            },
            "9722": {
                "gate_1_7": "qualified",
                "candidate_application_source": _SOURCE_9722.as_posix(),
                "application_source_present": True,
                "declared_cpp_standard": 11,
                "registered_irrlicht_source": _IRR_9722.as_posix(),
                "registered_irrlicht_version": "1.9.0.15",
                "registered_source_artifact": "fixed_source_capsule/luanti/14464/fixed/v2",
                "candidate_matching_irrlicht_source_registered": False,
                "cpp17_may_address_enable_if_t_only": True,
                "unresolved_source_api_error": "call_to_equals_is_ambiguous",
                "texture_layer_source_preservation_unproven": True,
                "ready_for_second_native_build": False,
                "ranking": "do_not_retry_with_cross_candidate_irrlicht_source",
            },
        },
        "fresh_execution_contract": {
            "new_namespace_required": True,
            "old_attempts_loaded": False,
            "old_attempts_may_be_inputs": False,
            "source_changes_allowed": False,
            "compiler_dialect_override_only": "C++17",
            "second_native_build_started": False,
        },
        "candidate_execution_performed": False,
        "new_build_started": False,
        "network_actions_authorized": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "register_provenance_bound_irrlichtmt_1.9.0mt9_for_13132_then_reaudit_cpp17_without_source_edits",
        "complete": True,
    }
    inputs = tuple(ref for ref in _REFS.values())
    existing = _try_load(root, _BLOCKER)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#13132/#9722 comparison conflicts with current state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _BLOCKER)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#13132/#9722 comparison typed reload failed")
    return artifact


def _validate_artifacts(loaded: dict[str, StableArtifact]) -> None:
    if (
        loaded["13132_transition"].payload.get("qualified") is not True
        or loaded["13132_build"].payload.get("outcome") != "infrastructure_blocked"
        or loaded["13132_build"].payload.get("configure", {}).get("source_has_vendored_irrlicht") is not False
        or loaded["13132_blocker"].payload.get("terminal") is not False
        or loaded["9722_aggregate"].payload.get("qualified") is not True
        or loaded["9722_transition"].payload.get("state") != "passed"
        or loaded["9722_materialization"].payload.get("no_old_artifact_inheritance") is not True
        or loaded["9722_dependency"].payload.get("source_artifact")
        != "fixed_source_capsule/luanti/14464/fixed/v2"
        or loaded["9722_dependency_build"].payload.get("state") != "built"
        or not any(
            "enable_if_t" in value
            for value in loaded["9722_blocker"].payload.get("build_error_signatures", [])
        )
        or not any(
            "call to equals is ambiguous" in value
            for value in loaded["9722_blocker"].payload.get("build_error_signatures", [])
        )
    ):
        raise RuntimeError("#13132/#9722 typed readiness chain drifted")


def _ordinary_dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("readiness directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("readiness file is unsafe")
    return resolved


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


__all__ = ["write_luanti_13132_9722_downstream_readiness"]
