"""Candidate-specific Gate 2--7 audit for Luanti #15408.

The current-v2 chain is already persisted.  This module binds only that
chain, registers an in-process native profile, materializes the merged
revision into the candidate's fixed capsule namespace, and runs a fresh
12-cell fixed/buggy native epoch.  It never reads or resumes another
candidate's attempts and exposes no paid or formal-trial action.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from .luanti_fixed_source_legacy_migration_reader import read_legacy_merged_revision
from .luanti_native_executor import (
    EXECUTION_CONFIRMATION,
    NativeRegressionExecutionResult,
    execute_luanti_native_regression,
)
from .luanti_native_fixed_source_capsule import (
    _materialize,
    _ordinary_directory,
    _read_archive,
    _remove_new_tree,
    _validated_members,
)
from .luanti_native_regression import (
    NativeRegressionExecutionPlan,
    NativeRegressionProfile,
    _fixed_capsule_or_block,
    _profile,
    _source_packet_or_block,
    _test_manifest_or_block,
    plan_luanti_native_regression_execution,
)
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = CandidateKey("luanti", 15408)
PROFILE: NativeRegressionProfile = _profile(
    15408,
    profile_id="luanti-15408-collision-acceleration-v1-native-gate57",
    production_paths=("src/collision.cpp",),
    support_paths=(),
    test_paths=("src/unittest/test_collision.cpp",),
    p2p_test_paths=("src/unittest/test_settings.cpp",),
    patch_test_markers=(
        "/* glitched into ground */",
        "/* falling on ground */",
        "/* jumping on ground */",
    ),
    fixed_test_markers=(
        'const char *getName() { return "TestCollision"; }',
        "/* glitched into ground */",
        'const char *getName() { return "TestSettings"; }',
    ),
    f2p_selector="TestCollision",
    p2p_selector="TestSettings",
    platform_policy="portable",
)

_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = PROFILE.source_packet_reference
_PATCH = PROFILE.patch_reference
_MANIFEST = PROFILE.test_manifest_reference
_CAPSULE = PROFILE.fixed_capsule_reference
_MIRROR = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_CAPSULE_REL = PurePosixPath(PROFILE.fixed_source_tree_path)
_NAMESPACE = "fresh-20260814-15408-v1"
_CAPSULE_CONFIRMATION = "MATERIALIZE_LUANTI_15408_NATIVE_FIXED_SOURCE_CAPSULE"


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _install_profile() -> None:
    # The profile lives only in this process.  The shared roster is unchanged.
    import warptest.c1_lazarus_selection.luanti_native_regression as native

    native._PROFILES[CANDIDATE.change_number] = PROFILE


def materialize_fixed_capsule(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> StableArtifact:
    if confirmation != _CAPSULE_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {_CAPSULE_CONFIRMATION!r}")
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "protocol-migration"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15408 fixed capsule authorization is inactive")
    _install_profile()
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    manifest = _load(root, _MANIFEST)
    # Revalidate the persisted current-v2 chain before any materialization.
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != CANDIDATE.to_value()
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != CANDIDATE.to_value()
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != PROFILE.production_paths
        or tuple(patch.payload.get("test_paths", ())) != PROFILE.test_paths
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != CANDIDATE.to_value()
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("executable_test_paths")
        != list(PROFILE.test_paths)
    ):
        raise RuntimeError("#15408 current-v2 chain is not mechanically closed")
    revision = read_legacy_merged_revision(
        root,
        raw_manifest_path=source.payload["raw_evidence_path"],
        candidate=CANDIDATE,
    )
    mirror = _ordinary_directory(root, _MIRROR, create=False)
    archive = _read_archive(mirror, revision)
    members = _validated_members(archive)
    destination = root / _CAPSULE_REL
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("#15408 fixed capsule destination already exists")
    parent = _ordinary_directory(
        root,
        PurePosixPath(*_CAPSULE_REL.parts[:-1]),
        create=True,
    )
    _materialize(parent, destination, archive, members)
    payload = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": PROFILE.profile_id,
        "source_tree_path": PROFILE.fixed_source_tree_path,
        "source_manifest_path": source.payload["raw_evidence_path"],
        "test_paths": list(PROFILE.test_paths),
        "p2p_test_paths": list(PROFILE.p2p_test_paths),
        "support_paths": list(PROFILE.support_paths),
    }
    inputs = (_SOURCE, _PATCH, _MANIFEST)
    try:
        write_stable_artifact(
            root,
            artifact_id=_CAPSULE.artifact_id,
            artifact_kind=_CAPSULE.artifact_kind,
            semantic_version=_CAPSULE.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
        artifact = _load(root, _CAPSULE)
    except BaseException:
        _remove_new_tree(destination)
        raise
    if artifact.inputs != inputs or artifact.payload != payload:
        _remove_new_tree(destination)
        raise RuntimeError("#15408 fixed capsule typed reload is invalid")
    return artifact


def build_execution_plan(root: Path) -> NativeRegressionExecutionPlan | StageDecision:
    _install_profile()
    plan = plan_luanti_native_regression_execution(root, candidate=CANDIDATE)
    if isinstance(plan, StageDecision):
        return plan
    return NativeRegressionExecutionPlan(
        profile=plan.profile,
        toolchain=plan.toolchain,
        fixed_capsule=plan.fixed_capsule,
        source_manifest_path=plan.source_manifest_path,
        raw_patch_path=plan.raw_patch_path,
        production_inverse=plan.production_inverse,
        configure_argv=plan.configure_argv,
        build_argv=plan.build_argv,
        runs=plan.runs,
        closed_environment=plan.closed_environment,
        execution_namespace=_NAMESPACE,
        dependency_references=plan.dependency_references,
    )


def execute_fresh(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> NativeRegressionExecutionResult:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15408 Gate 5--7 authorization is inactive")
    _install_profile()
    return execute_luanti_native_regression(
        artifact_root,
        candidate=CANDIDATE,
        authorization=authorization,
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda root, *, candidate: build_execution_plan(Path(root)),
    )


def persist_gate34_admission(
    artifact_root: str | Path,
    *,
    plan: NativeRegressionExecutionPlan,
    result: NativeRegressionExecutionResult,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Bind Gate2--4 and the fresh Gate5--7 aggregate in one typed receipt."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15408 Gate34 admission authorization is inactive")
    if plan.profile.candidate != CANDIDATE or result.candidate != CANDIDATE:
        raise ValueError("#15408 Gate34 candidate binding is invalid")
    if result.aggregate.payload.get("qualified") is not True or result.executed_attempts != 12:
        raise ValueError("#15408 fresh aggregate is not qualified 12-cell evidence")
    fixed_build = ArtifactReference(
        f"native_regression_build/luanti/15408/fixed/build/{_NAMESPACE}",
        "native_regression_build",
        "3.0",
    )
    buggy_build = ArtifactReference(
        f"native_regression_build/luanti/15408/buggy/build/{_NAMESPACE}",
        "native_regression_build",
        "3.0",
    )
    reference = ArtifactReference(
        "candidate_gate34_admission/luanti/15408/native/v1",
        "candidate_gate34_admission",
        "1.0",
    )
    payload: dict[str, Any] = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": plan.profile.profile_id,
        "gate2": {
            "state": "passed",
            "authoritative_issue_count": 1,
            "primary_issue_number": 15027,
            "linked_issue_numbers": [15027, 15196],
        },
        "gate3": {
            "state": "passed",
            "production_only_inverse": True,
            "inverse_paths": list(plan.production_inverse.paths),
            "test_paths_excluded": list(plan.profile.test_paths),
            "support_paths_excluded": list(plan.profile.support_paths),
        },
        "gate4": {
            "state": "passed",
            "fixed_test_paths": list(plan.profile.test_paths),
            "buggy_api_compile": "passed",
            "fixed_build": fixed_build.artifact_id,
            "buggy_build": buggy_build.artifact_id,
            "fixed_test_selector": plan.profile.f2p_selector,
            "p2p_test_selector": plan.profile.p2p_selector,
        },
        "fresh_gate57_qualified": True,
        "execution_namespace": _NAMESPACE,
        "executed_attempts": result.executed_attempts,
        "resumed_attempts": result.resumed_attempts,
        "paid_annotation": False,
        "formal_trial": False,
        "gate_8_13": "not_claimed",
    }
    inputs = (
        plan.profile.source_packet_reference,
        plan.profile.patch_reference,
        plan.profile.test_manifest_reference,
        plan.profile.fixed_capsule_reference,
        fixed_build,
        buggy_build,
        result.aggregate.reference,
    )
    root = Path(artifact_root).absolute().resolve(strict=True)
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
    artifact = _load(root, reference)
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("#15408 Gate34 admission typed reload is invalid")
    return artifact


def persist_gate57_transition(
    artifact_root: str | Path,
    *,
    plan: NativeRegressionExecutionPlan,
    result: NativeRegressionExecutionResult,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist the qualified Gate 1--7 transition without claiming Gate 8--13."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15408 Gate57 transition authorization is inactive")
    if (
        plan.profile.candidate != CANDIDATE
        or result.candidate != CANDIDATE
        or result.aggregate.payload.get("qualified") is not True
        or result.aggregate.payload.get("attempt_count") != 12
    ):
        raise ValueError("#15408 Gate57 transition evidence is invalid")
    reference = ArtifactReference(
        "candidate_gate57_transition/luanti/15408/native/v1",
        "candidate_gate57_transition",
        "1.0",
    )
    payload: dict[str, Any] = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": plan.profile.profile_id,
        "state": "passed",
        "outcome": "qualified",
        "terminal": False,
        "resumable": True,
        "execution_lineage": f"candidate-gate57/luanti/15408/native/{_NAMESPACE}",
        "candidate_execution_performed": True,
        "attempt_count": 12,
        "gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "version_materialization": "passed",
            "fixed_buggy_f2p_p2p_3x": "passed",
        },
        "deferred_until_after_gate5_7": [
            "runtime_world_replay",
            "semantic_observations",
            "golden_cross_judgment",
            "construction_receipt",
        ],
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = (
        plan.profile.source_packet_reference,
        plan.profile.patch_reference,
        plan.profile.test_manifest_reference,
        plan.profile.fixed_capsule_reference,
        result.aggregate.reference,
    )
    root = Path(artifact_root).absolute().resolve(strict=True)
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
    artifact = _load(root, reference)
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("#15408 Gate57 transition typed reload is invalid")
    return artifact


def _authorization(scope: str, authorization_id: str) -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=authorization_id,
        scope=scope,
        granted_at=_now(),
        local_user_id=os.getuid(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path(".warptest-cache/c1_lazarus_selection_v1"),
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    _install_profile()
    root = args.artifact_root
    capsule = materialize_fixed_capsule(
        root,
        authorization=_authorization(
            "protocol-migration", "authorization/luanti-15408-capsule-v1"
        ),
        confirmation=_CAPSULE_CONFIRMATION,
    )
    plan = build_execution_plan(root)
    result: dict[str, Any] = {
        "capsule": capsule.reference.artifact_id,
        "plan": (
            {"state": "blocked", "details": plan.details}
            if isinstance(plan, StageDecision)
            else {
                "state": "ready",
                "profile_id": plan.profile.profile_id,
                "production_inverse_paths": list(plan.production_inverse.paths),
                "f2p_selector": plan.profile.f2p_selector,
                "p2p_selector": plan.profile.p2p_selector,
                "execution_namespace": plan.execution_namespace,
            }
        ),
    }
    if args.execute:
        execution = execute_fresh(
            root,
            authorization=_authorization(
                "preannotation-execution", "authorization/luanti-15408-gate57-v1"
            ),
        )
        result["execution"] = {
            "aggregate": execution.aggregate.reference.artifact_id,
            "executed_build_steps": execution.executed_build_steps,
            "executed_attempts": execution.executed_attempts,
            "resumed_attempts": execution.resumed_attempts,
            "qualified": execution.qualified,
            "cells": execution.aggregate.payload.get("cells"),
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CANDIDATE",
    "PROFILE",
    "build_execution_plan",
    "execute_fresh",
    "materialize_fixed_capsule",
    "persist_gate34_admission",
    "persist_gate57_transition",
]
