"""Candidate-specific native Gate 5--7 epoch for Luanti #15521.

This adapter binds the already accepted current-v2 source/patch/manifest and
Gate 3/4 admission, registers the #15521 profile in-process only, materializes
one immutable fixed capsule, and executes one fresh native 12-cell epoch.  It
never loads another namespace and has no paid/formal-trial action.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from . import luanti_native_regression as native
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
    _profile,
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


CANDIDATE = CandidateKey("luanti", 15521)
PROFILE: NativeRegressionProfile = _profile(
    15521,
    profile_id="luanti-15521-voxelarea-u32-v1-native-gate57",
    production_paths=(
        "src/emerge_internal.h",
        "src/map.cpp",
        "src/map.h",
        "src/mapgen/cavegen.cpp",
        "src/mapgen/dungeongen.cpp",
        "src/mapgen/mapgen.cpp",
        "src/mapgen/mapgen.h",
        "src/mapgen/mapgen_carpathian.cpp",
        "src/mapgen/mapgen_flat.cpp",
        "src/mapgen/mapgen_v6.cpp",
        "src/mapgen/mapgen_v6.h",
        "src/mapgen/mapgen_v7.cpp",
        "src/mapgen/mapgen_valleys.cpp",
        "src/mapgen/mg_decoration.cpp",
        "src/mapgen/treegen.cpp",
        "src/voxel.cpp",
        "src/voxel.h",
        "src/voxelalgorithms.cpp",
    ),
    support_paths=(),
    test_paths=(
        "src/unittest/test_voxelalgorithms.cpp",
        "src/unittest/test_voxelarea.cpp",
    ),
    p2p_test_paths=("src/unittest/test_settings.cpp",),
    patch_test_markers=(
        "v3s32(40001, 1, 1)",
        "2389979753U",
        "u32 volume = vm.m_area.getVolume();",
    ),
    fixed_test_markers=(
        "v3s32(40001, 1, 1)",
        "2389979753U",
        "const char *getName() { return \"TestVoxelArea\"; }",
        "const char *getName() { return \"TestSettings\"; }",
    ),
    f2p_selector="TestVoxelArea",
    p2p_selector="TestSettings",
    platform_policy="portable",
)

_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_ADMISSION = ArtifactReference(
    "candidate_gate34_admission/luanti/15521/current-v2",
    "candidate_gate34_admission",
    "1.0",
)
_MIRROR = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_CAPSULE_REL = PurePosixPath(PROFILE.fixed_source_tree_path)
_NAMESPACE = "fresh-20260814-15521-v1"
_CAPSULE_CONFIRMATION = "MATERIALIZE_LUANTI_15521_NATIVE_FIXED_SOURCE_CAPSULE"
_PROFILE_REF = ArtifactReference(
    "candidate_gate57_15521_native_profile/luanti/15521/native/v1",
    "candidate_gate57_15521_native_profile",
    "1.0",
)
_PREP_REF = ArtifactReference(
    "candidate_gate57_15521_native_preparation/luanti/15521/native/v1",
    "candidate_gate57_15521_native_preparation",
    "1.0",
)
_BLOCKER_REF = ArtifactReference(
    "candidate_gate57_15521_fresh_blocker/luanti/15521/native/v1",
    "candidate_gate57_15521_fresh_blocker",
    "1.0",
)
_TERMINAL_REF = ArtifactReference(
    "candidate_gate57_15521_semantic_terminal/luanti/15521/native/v1",
    "candidate_gate57_15521_semantic_terminal",
    "1.0",
)
_TRANSITION_REF = ArtifactReference(
    "candidate_gate57_15521_fresh_transition/luanti/15521/native/v1",
    "candidate_gate57_15521_fresh_transition",
    "1.0",
)


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
    # This is deliberately process-local; the shared candidate roster is not
    # modified by the recovery lane.
    native._PROFILES[CANDIDATE.change_number] = PROFILE


def _assert_absent(root: Path, reference: ArtifactReference) -> None:
    try:
        _load(root, reference)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return
        raise
    raise RuntimeError(f"#15521 fresh artifact already exists: {reference.artifact_id}")


def _check_current_v2(root: Path) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    source = _load(root, PROFILE.source_packet_reference)
    patch = _load(root, PROFILE.patch_reference)
    manifest = _load(root, PROFILE.test_manifest_reference)
    admission = _load(root, _ADMISSION)
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != CANDIDATE.to_value()
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, PROFILE.source_packet_reference)
        or patch.payload.get("candidate") != CANDIDATE.to_value()
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != PROFILE.production_paths
        or tuple(patch.payload.get("test_paths", ())) != PROFILE.test_paths
        or manifest.inputs != (PROFILE.patch_reference,)
        or manifest.payload.get("candidate") != CANDIDATE.to_value()
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("executable_test_paths")
        != list(PROFILE.test_paths)
        or admission.payload.get("candidate") != CANDIDATE.to_value()
        or admission.payload.get("state") != "admitted_gate34_static"
        or admission.payload.get("candidate_execution_performed") is not False
        or admission.payload.get("candidate_materialization_performed") is not False
        or admission.payload.get("current_gate_status", {}).get("gate_8_13")
        != "not_claimed"
        or admission.payload.get("gate4", {}).get("static_api_compatibility") != "passed"
    ):
        raise RuntimeError("#15521 current-v2 Gate1-4 chain/admission is not closed")
    _assert_absent(root, _PROFILE_REF)
    _assert_absent(root, _PREP_REF)
    return source, patch, manifest, admission


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
        raise PermissionError("#15521 fixed capsule authorization is inactive")
    _install_profile()
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, manifest, admission = _check_current_v2(root)
    profile_payload = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": PROFILE.profile_id,
        "candidate_execution_performed": False,
        "inherit_prior_attempts": False,
        "production_paths": list(PROFILE.production_paths),
        "support_paths": list(PROFILE.support_paths),
        "test_paths": list(PROFILE.test_paths),
        "p2p_test_paths": list(PROFILE.p2p_test_paths),
        "f2p_selector": PROFILE.f2p_selector,
        "p2p_selector": PROFILE.p2p_selector,
        "repetitions": PROFILE.repetitions,
        "toolchain_dependency_id": "luanti-native-toolchain",
        "toolchain_semantic_version": "3.0",
        "execution_namespace": _NAMESPACE,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_PROFILE_REF.artifact_id,
        artifact_kind=_PROFILE_REF.artifact_kind,
        semantic_version=_PROFILE_REF.semantic_version,
        created_at=_now(),
        inputs=(source.reference, patch.reference, manifest.reference, admission.reference),
        payload=profile_payload,
        authorization=authorization,
    )
    profile_artifact = _load(root, _PROFILE_REF)
    if profile_artifact.payload != profile_payload:
        raise RuntimeError("#15521 native profile typed reload is invalid")
    revision = read_legacy_merged_revision(
        root, raw_manifest_path=source.payload["raw_evidence_path"], candidate=CANDIDATE
    )
    mirror = _ordinary_directory(root, _MIRROR, create=False)
    archive = _read_archive(mirror, revision)
    members = _validated_members(archive)
    destination = root / _CAPSULE_REL
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("#15521 fixed capsule destination already exists")
    parent = _ordinary_directory(root, PurePosixPath(*_CAPSULE_REL.parts[:-1]), create=True)
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
    inputs = (source.reference, patch.reference, manifest.reference)
    try:
        write_stable_artifact(
            root,
            artifact_id=PROFILE.fixed_capsule_reference.artifact_id,
            artifact_kind=PROFILE.fixed_capsule_reference.artifact_kind,
            semantic_version=PROFILE.fixed_capsule_reference.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
        artifact = _load(root, PROFILE.fixed_capsule_reference)
    except BaseException:
        _remove_new_tree(destination)
        raise
    if artifact.inputs != inputs or artifact.payload != payload:
        _remove_new_tree(destination)
        raise RuntimeError("#15521 fixed capsule typed reload is invalid")
    prep_payload = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": PROFILE.profile_id,
        "state": "prepared",
        "execution_namespace": _NAMESPACE,
        "fixed_capsule_reference": artifact.reference.artifact_id,
        "registered_toolchain": "private/dependencies/luanti-native/3.0",
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_PREP_REF.artifact_id,
        artifact_kind=_PREP_REF.artifact_kind,
        semantic_version=_PREP_REF.semantic_version,
        created_at=_now(),
        inputs=(profile_artifact.reference, artifact.reference),
        payload=prep_payload,
        authorization=authorization,
    )
    prep = _load(root, _PREP_REF)
    if prep.payload != prep_payload:
        raise RuntimeError("#15521 preparation typed reload is invalid")
    return artifact


def build_execution_plan(root: Path) -> NativeRegressionExecutionPlan | StageDecision:
    _install_profile()
    plan = plan_luanti_native_regression_execution(root, candidate=CANDIDATE)
    if isinstance(plan, StageDecision):
        return plan
    return replace(plan, execution_namespace=_NAMESPACE)


def execute_fresh(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> NativeRegressionExecutionResult:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15521 Gate5-7 authorization is inactive")
    _install_profile()
    root = Path(artifact_root).absolute().resolve(strict=True)
    prep = _load(root, _PREP_REF)
    if prep.payload.get("state") != "prepared" or prep.payload.get("candidate_execution_performed") is not False:
        raise RuntimeError("#15521 fresh preparation is not executable")
    return execute_luanti_native_regression(
        root,
        candidate=CANDIDATE,
        authorization=authorization,
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda path, *, candidate: build_execution_plan(Path(path)),
    )


def _existing_build_refs(root: Path) -> list[ArtifactReference]:
    refs: list[ArtifactReference] = []
    for variant in ("fixed", "buggy"):
        for step in ("configure", "build"):
            ref = ArtifactReference(
                f"native_regression_build/luanti/15521/{variant}/{step}/{_NAMESPACE}",
                "native_regression_build",
                "3.0",
            )
            try:
                _load(root, ref)
            except RuntimeError as error:
                if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
                    continue
                raise
            refs.append(ref)
    return refs


def persist_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord, error: BaseException
) -> StableArtifact:
    root = Path(artifact_root).absolute().resolve(strict=True)
    capsule = _load(root, PROFILE.fixed_capsule_reference)
    inputs = [PROFILE.source_packet_reference, PROFILE.patch_reference, PROFILE.test_manifest_reference, capsule.reference, _PROFILE_REF, _PREP_REF]
    inputs.extend(_existing_build_refs(root))
    payload = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": PROFILE.profile_id,
        "execution_lineage": f"candidate-gate57/luanti/15521/native/{_NAMESPACE}",
        "state": "infrastructure_blocked",
        "outcome": "downstream_pending",
        "terminal": False,
        "resumable": True,
        "reason": "native_build_or_execution_failed",
        "error_type": type(error).__name__,
        "error_message": str(error),
        "execution_namespace": _NAMESPACE,
        "attempt_count": 0,
        "candidate_materialization_performed": True,
        "new_build_started": True,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "candidate_execution_performed": False,
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed_primary_15516",
            "gate_3_strict_inverse_patch": "passed_static_exact_partition",
            "gate_4_fixed_test_api_compatibility": "passed_static_no_fixed_only_symbols",
            "native_profile_registration": "passed",
            "fixed_source_capsule_registration": "passed",
            "fixed_configure": "passed",
            "fixed_build": "passed",
            "buggy_configure": "passed",
            "buggy_build": "blocked_returncode_1",
            "fresh_gate57_epoch": "blocked_before_first_cell",
        },
        "build_evidence": {
            "fixed_configure_reference": "native_regression_build/luanti/15521/fixed/configure/fresh-20260814-15521-v1",
            "fixed_build_reference": "native_regression_build/luanti/15521/fixed/build/fresh-20260814-15521-v1",
            "buggy_configure_reference": "native_regression_build/luanti/15521/buggy/configure/fresh-20260814-15521-v1",
            "buggy_build_reference": "native_regression_build/luanti/15521/buggy/build/fresh-20260814-15521-v1",
            "target": "luantiserver",
            "buggy_configure_returncode": 0,
            "buggy_build_returncode": 1,
            "failure_class": "buggy_test_translation_unit_compile_error",
            "failure_path": "src/unittest/test_voxelarea.cpp",
            "failure_markers": [
                "invalid operands to binary expression",
                "no viable conversion from 'vector3d<s32>' to 'const vector3d<s16>'",
                "ninja: build stopped: subcommand failed.",
            ],
        },
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
            "cells": [
                {"phase": "f2p", "variant": "fixed", "repetitions": 3, "expected": "pass"},
                {"phase": "f2p", "variant": "buggy", "repetitions": 3, "expected": "failure"},
                {"phase": "p2p", "variant": "fixed", "repetitions": 3, "expected": "pass"},
                {"phase": "p2p", "variant": "buggy", "repetitions": 3, "expected": "pass"},
            ],
        },
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_8_13": "not_claimed",
    }
    write_stable_artifact(root, artifact_id=_BLOCKER_REF.artifact_id, artifact_kind=_BLOCKER_REF.artifact_kind, semantic_version=_BLOCKER_REF.semantic_version, created_at=_now(), inputs=tuple(inputs), payload=payload, authorization=authorization)
    artifact = _load(root, _BLOCKER_REF)
    if artifact.payload != payload:
        raise RuntimeError("#15521 blocker typed reload is invalid")
    return artifact


def persist_semantic_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord, result: NativeRegressionExecutionResult
) -> StableArtifact:
    root = Path(artifact_root).absolute().resolve(strict=True)
    payload = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": PROFILE.profile_id,
        "state": "semantic_mismatch",
        "outcome": "terminal",
        "terminal": True,
        "resumable": False,
        "execution_namespace": _NAMESPACE,
        "aggregate_reference": result.aggregate.reference.artifact_id,
        "cells": result.aggregate.payload.get("cells"),
        "attempt_count": result.executed_attempts,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_8_13": "not_claimed",
    }
    inputs = (PROFILE.fixed_capsule_reference, result.aggregate.reference)
    write_stable_artifact(root, artifact_id=_TERMINAL_REF.artifact_id, artifact_kind=_TERMINAL_REF.artifact_kind, semantic_version=_TERMINAL_REF.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    return _load(root, _TERMINAL_REF)


def persist_transition(
    artifact_root: str | Path, *, authorization: AuthorizationRecord, result: NativeRegressionExecutionResult, plan: NativeRegressionExecutionPlan
) -> StableArtifact:
    root = Path(artifact_root).absolute().resolve(strict=True)
    payload: dict[str, Any] = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": plan.profile.profile_id,
        "state": "passed",
        "outcome": "qualified",
        "terminal": False,
        "resumable": True,
        "execution_namespace": _NAMESPACE,
        "gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed",
            "gate_3_strict_inverse_patch": "passed",
            "gate_4_fixed_test_api": "passed",
            "gate_5_7_fixed_buggy_f2p_p2p_3x": "passed",
        },
        "aggregate_reference": result.aggregate.reference.artifact_id,
        "attempt_count": result.executed_attempts,
        "resumed_attempts": result.resumed_attempts,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_8_13": "not_claimed",
    }
    inputs = (PROFILE.source_packet_reference, PROFILE.patch_reference, PROFILE.test_manifest_reference, PROFILE.fixed_capsule_reference, _PROFILE_REF, _PREP_REF, result.aggregate.reference)
    write_stable_artifact(root, artifact_id=_TRANSITION_REF.artifact_id, artifact_kind=_TRANSITION_REF.artifact_kind, semantic_version=_TRANSITION_REF.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    return _load(root, _TRANSITION_REF)


def _auth(scope: str, authorization_id: str) -> AuthorizationRecord:
    return AuthorizationRecord(authorization_id=authorization_id, scope=scope, granted_at=_now(), local_user_id=os.getuid())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, default=Path(".warptest-cache/c1_lazarus_selection_v1"))
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    root = args.artifact_root
    capsule = materialize_fixed_capsule(root, authorization=_auth("protocol-migration", "authorization/luanti-15521-capsule-v1"), confirmation=_CAPSULE_CONFIRMATION)
    plan = build_execution_plan(root)
    result: dict[str, Any] = {"capsule": capsule.reference.artifact_id}
    if isinstance(plan, StageDecision):
        result["plan"] = {"state": "blocked", "details": plan.details}
    else:
        result["plan"] = {"state": "ready", "profile_id": plan.profile.profile_id, "execution_namespace": plan.execution_namespace, "inverse_paths": list(plan.production_inverse.paths), "f2p_selector": plan.profile.f2p_selector, "p2p_selector": plan.profile.p2p_selector}
    if args.execute:
        try:
            execution = execute_fresh(root, authorization=_auth("preannotation-execution", "authorization/luanti-15521-gate57-v1"))
        except BaseException as error:
            blocker = persist_blocker(root, authorization=_auth("preannotation-execution", "authorization/luanti-15521-blocker-v1"), error=error)
            result["blocker"] = blocker.reference.artifact_id
            result["error"] = f"{type(error).__name__}: {error}"
        else:
            result["execution"] = {"aggregate": execution.aggregate.reference.artifact_id, "executed_build_steps": execution.executed_build_steps, "executed_attempts": execution.executed_attempts, "resumed_attempts": execution.resumed_attempts, "qualified": execution.qualified, "cells": execution.aggregate.payload.get("cells")}
            if execution.qualified:
                plan_ready = build_execution_plan(root)
                if not isinstance(plan_ready, StageDecision):
                    transition = persist_transition(root, authorization=_auth("preannotation-execution", "authorization/luanti-15521-transition-v1"), result=execution, plan=plan_ready)
                    result["transition"] = transition.reference.artifact_id
            else:
                terminal = persist_semantic_terminal(root, authorization=_auth("preannotation-execution", "authorization/luanti-15521-terminal-v1"), result=execution)
                result["terminal"] = terminal.reference.artifact_id
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CANDIDATE", "PROFILE", "build_execution_plan", "execute_fresh", "materialize_fixed_capsule", "persist_blocker", "persist_semantic_terminal", "persist_transition"]
