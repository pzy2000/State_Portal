"""Candidate-specific fresh native Gate 5--7 epoch for Luanti #14464.

The generic native executor is reused only as the closed build/process engine;
this adapter adds the #14464 current-v2 chain (including atomicity,
preparation and downstream-plan receipts) to the typed Gate 5--7 boundary and
uses a new candidate-specific aggregate/transition namespace.  It never loads
or resumes a historical attempt and it does not perform any Gate 8--13 work.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .luanti_native_executor import (
    EXECUTION_CONFIRMATION,
    NativeRegressionExecutionResult,
    execute_luanti_native_regression,
)
from .luanti_native_regression import plan_luanti_native_regression_execution
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = EXECUTION_CONFIRMATION
_CANDIDATE = {"repository_key": "luanti", "change_number": 14464}
_ROOT_CANDIDATE = CandidateKey("luanti", 14464)
_CHAIN = (
    ArtifactReference(
        "candidate_source_packet/luanti/14464/source/v2",
        "candidate_source_packet",
        "2.0",
    ),
    ArtifactReference(
        "candidate_test_patch/luanti/14464/screening/v2",
        "candidate_test_patch",
        "2.0",
    ),
    ArtifactReference(
        "source_atomicity/luanti/14464/preannotation/v2",
        "source_atomicity",
        "2.0",
    ),
    ArtifactReference(
        "candidate_stage/luanti/14464/test_manifest/v2",
        "candidate_stage",
        "2.0",
    ),
    ArtifactReference(
        "candidate_profile/luanti/14464/preflight/v2",
        "candidate_profile",
        "2.0",
    ),
    ArtifactReference(
        "candidate_preparation/luanti/14464/preannotation/v2",
        "candidate_preparation",
        "2.0",
    ),
    ArtifactReference(
        "candidate_downstream_plan/luanti/14464/preannotation/v2",
        "candidate_downstream_plan",
        "2.0",
    ),
    ArtifactReference(
        "fixed_source_capsule/luanti/14464/fixed/v2",
        "fixed_source_capsule",
        "2.0",
    ),
)
_GENERIC_AGGREGATE = ArtifactReference(
    "native_regression_aggregate/luanti/14464/fixed-buggy/v3",
    "native_regression_aggregate",
    "3.0",
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_14464_fresh_aggregate/luanti/14464/native/v1",
    "candidate_gate57_14464_fresh_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_14464_fresh_transition/luanti/14464/native/v1",
    "candidate_gate57_14464_fresh_transition",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_14464_fresh_blocker/luanti/14464/native/v1",
    "candidate_gate57_14464_fresh_blocker",
    "1.0",
)


def execute_luanti_14464_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
) -> tuple[StableArtifact, StableArtifact]:
    """Run exactly twelve fresh native cells and persist the typed boundary."""

    _require_authorization(authorization)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_and_validate_chain(root)
    _assert_fresh_namespace(root)
    try:
        result = execute_luanti_native_regression(
            root,
            candidate=_ROOT_CANDIDATE,
            authorization=authorization,
            confirmation=confirmation,
            planner=plan_luanti_native_regression_execution,
        )
    except BaseException as error:
        blocker = _write_blocker(root, chain, error, authorization)
        return blocker, blocker
    generic = _load(root, _GENERIC_AGGREGATE)
    aggregate = _write_aggregate(root, chain, result, generic, authorization)
    transition = _write_transition(root, chain, aggregate, result, authorization)
    return aggregate, transition


def _load_and_validate_chain(root: Path) -> tuple[StableArtifact, ...]:
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    if any(item.payload.get("candidate") != _CANDIDATE for item in chain):
        raise RuntimeError("#14464 current-v2 chain candidate binding drifted")
    source, patch, atomicity, manifest, profile, preparation, plan, capsule = chain
    if (
        source.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or atomicity.payload.get("outcome") != "accepted"
        or manifest.payload.get("state") != "passed"
        or profile.payload.get("strict_gate_status", {}).get("source_authority")
        != "passed"
        or profile.payload.get("strict_gate_status", {}).get("test_api_compatibility")
        != "passed"
        or preparation.payload.get("state") != "prepared"
        or plan.payload.get("state") != "planned"
        or capsule.payload.get("source_tree_path")
        != "private/fixed-capsules/luanti/14464/fixed-source"
    ):
        raise RuntimeError("#14464 current-v2 prerequisites are not passed")
    tree = root / "private/fixed-capsules/luanti/14464/fixed-source"
    for relative in (
        "src/content_nodemeta.cpp",
        "src/nodemetadata.cpp",
        "src/object_properties.cpp",
        "src/script/common/c_converter.cpp",
        "src/serialization.cpp",
        "src/util/serialize.h",
        "src/util/string.cpp",
        "src/unittest/test_utilities.cpp",
        "src/unittest/test_settings.cpp",
    ):
        path = tree / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"#14464 capsule file is missing or unsafe: {relative}")
    return chain


def _assert_fresh_namespace(root: Path) -> None:
    for reference in (_GENERIC_AGGREGATE, _AGGREGATE, _TRANSITION, _BLOCKER):
        if (root / "artifacts" / Path(reference.artifact_id) / reference.semantic_version).exists():
            raise RuntimeError(f"#14464 fresh artifact already exists: {reference.artifact_id}")
    for kind in ("native_regression_build", "native_regression_attempt"):
        directory = root / "artifacts" / kind / "luanti" / "14464"
        if directory.exists() and any(directory.rglob("*.json")):
            raise RuntimeError(f"#14464 generic fresh namespace is not empty: {kind}")


def _write_aggregate(
    root: Path,
    chain: tuple[StableArtifact, ...],
    result: NativeRegressionExecutionResult,
    generic: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if result.executed_attempts != 12 or result.resumed_attempts != 0:
        raise RuntimeError("#14464 Gate 5--7 did not execute twelve fresh cells")
    payload = {
        "candidate": _CANDIDATE,
        "state": "qualified" if result.qualified else "blocked",
        "outcome": "qualified" if result.qualified else "candidate_semantic_mismatch",
        "qualified": result.qualified,
        "terminal": not result.qualified,
        "resumable": not result.qualified,
        "fresh_epoch": "candidate_gate57_14464_native_v1",
        "attempt_count": result.executed_attempts,
        "repetitions_per_cell": 3,
        "old_attempts_loaded": False,
        "generic_aggregate_reference": generic.reference.artifact_id,
        "truth_table": {
            "fixed": {"f2p": "pass", "p2p": "pass"},
            "buggy": {"f2p": "failure", "p2p": "pass"},
        },
        "cells": generic.payload.get("cells", []),
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(root, _AGGREGATE, tuple(item.reference for item in chain) + (generic.reference,), payload, authorization)


def _write_transition(
    root: Path,
    chain: tuple[StableArtifact, ...],
    aggregate: StableArtifact,
    result: NativeRegressionExecutionResult,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "state": "passed" if result.qualified else "blocked",
        "outcome": "qualified" if result.qualified else "candidate_semantic_mismatch",
        "aggregate_reference": aggregate.reference.artifact_id,
        "gate_1_7_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "fixed_f2p_3x": "passed" if result.qualified else "failed",
            "buggy_f2p_3x": "passed_clean_failure" if result.qualified else "failed",
            "fixed_p2p_3x": "passed" if result.qualified else "failed",
            "buggy_p2p_3x": "passed" if result.qualified else "failed",
        },
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "deferred_until_after_gate5_7": [
            "gate8_full_versions",
            "gate9_start_replay_oracle_observations",
            "gate10_goldens",
            "gate11_visual_cross_judgment",
            "gate12_same_lineage_receipt",
            "gate13_freeze",
        ],
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(root, _TRANSITION, tuple(item.reference for item in chain) + (aggregate.reference,), payload, authorization)


def _write_blocker(
    root: Path,
    chain: tuple[StableArtifact, ...],
    error: BaseException,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "native_build_or_execution_failed",
        "error_type": type(error).__name__,
        "error": str(error),
        "candidate_execution_performed": False,
        "candidate_materialization_performed": True,
        "attempt_count": 0,
        "expected_attempt_count": 12,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "inspect candidate-specific build receipts; do not reuse attempts",
    }
    return _write(root, _BLOCKER, tuple(item.reference for item in chain), payload, authorization)


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
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


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("#14464 Gate 5--7 authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "execute_luanti_14464_gate57"]
