"""Typed runtime-dependency blocker for Luanti change 9722.

The current-v2 fixed/buggy source pair and Gate 8 hand-off are valid, but no
candidate-specific Irrlicht closure is registered.  This receipt is a
read-only preflight: it does not run CMake, build either tree, launch Luanti,
or reuse another candidate's dependency/runtime artifacts.
"""

from __future__ import annotations

import os
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 9722}
_PLAN = ArtifactReference(
    "candidate_downstream_plan/luanti/9722/runtime-visual/v2",
    "candidate_downstream_plan",
    "2.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/9722/fixed-buggy/v2",
    "candidate_materialization",
    "2.0",
)
_BLOCKER = ArtifactReference(
    "candidate_runtime_dependency_blocker/luanti/9722/current-v2",
    "candidate_runtime_dependency_blocker",
    "1.0",
)
_NATIVE_ROOT = PurePosixPath("private/dependencies/luanti-native/3.0")


def write_luanti_9722_runtime_dependency_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist and strictly reload the missing-Irrlicht preflight."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#9722 runtime blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    plan = _load(root, _PLAN)
    materialization = _load(root, _MATERIALIZATION)
    _validate_inputs(root, plan, materialization)
    native = root / _NATIVE_ROOT
    header_candidates = tuple(native.rglob("irrlicht.h")) if native.is_dir() else ()
    library_candidates = (
        tuple(
            path
            for pattern in (
                "libIrrlicht.a",
                "libIrrlicht.dylib",
                "Irrlicht.lib",
                "libIrrlicht.so",
            )
            for path in native.rglob(pattern)
        )
        if native.is_dir()
        else ()
    )
    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "reason": "registered_irrlicht_closure_missing_for_current_v2",
        "current_gate_status": {
            "gate8_source_pair": "passed_source_pair_no_build",
            "registered_runtime_dependency": "blocked",
            "gate9_to_13": "pending",
        },
        "cmake_requirement": {
            "project_file": "private/materialized/luanti/9722/application-v2/fixed/CMakeLists.txt",
            "find_package_call": "find_package(Irrlicht)",
            "default_build_client": True,
            "candidate_specific_server_only_experiment": False,
        },
        "observed_dependency": {
            "dependency_id": "luanti-9722-native-runtime-v1",
            "root": _NATIVE_ROOT.as_posix(),
            "header_candidates": [
                path.relative_to(root).as_posix() for path in header_candidates
            ],
            "library_candidates": [
                path.relative_to(root).as_posix() for path in library_candidates
            ],
            "irrlicht_header_present": bool(header_candidates),
            "irrlicht_library_present": bool(library_candidates),
            "registered_candidate_specific": False,
            "other_candidate_dependencies_reused": False,
        },
        "required_closure": {
            "irrlicht_header": "irrlicht.h",
            "irrlicht_library": "libIrrlicht.a or platform-equivalent",
            "matching_runtime_manifest": "candidate-specific immutable receipt",
            "fixed_buggy_pair": True,
        },
        "next_action": "register_candidate_specific_Irrlicht_closure_then_run_bounded_runtime_preflight",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_PLAN, _MATERIALIZATION)
    existing = _try_load(root, _BLOCKER)
    if existing is not None:
        if existing.inputs != inputs or existing.payload != payload:
            raise RuntimeError("#9722 runtime blocker conflicts with current-v2 state")
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
        raise RuntimeError("#9722 runtime blocker typed reload failed")
    return artifact


def _validate_inputs(
    root: Path, plan: StableArtifact, materialization: StableArtifact
) -> None:
    if plan.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#9722 runtime plan is not candidate-bound")
    if plan.payload.get("outcome") != "gate8_ready_source_pair":
        raise RuntimeError("#9722 Gate 8 plan is not ready")
    if materialization.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#9722 materialization is not candidate-bound")
    if materialization.payload.get("complete") is not True:
        raise RuntimeError("#9722 materialization is incomplete")
    for variant in ("fixed", "buggy"):
        relative = materialization.payload.get(f"{variant}_tree_path")
        if not isinstance(relative, str):
            raise RuntimeError("#9722 materialized tree path is invalid")
        tree = root / PurePosixPath(relative)
        info = tree.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("#9722 materialized tree is unsafe")


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
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_9722_runtime_dependency_blocker"]
