"""Typed non-terminal current-chain blocker for Luanti change #17231.

The current-v2 source, patch, source-atomicity, and test-manifest heads are
complete, but no candidate-owned capsule/profile/toolchain/runtime is
registered.  This receipt stops before materialization, builds, and the fresh
Gate 5--7 twelve-cell epoch; it does not eliminate the candidate.
"""

from __future__ import annotations

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


_CANDIDATE = {"repository_key": "luanti", "change_number": 17231}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/17231/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/17231/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/17231/preannotation/v2",
    "source_atomicity",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/17231/test_manifest/v2",
    "candidate_stage",
    "2.0",
)
_BLOCKER = ArtifactReference(
    "candidate_current_chain_blocker/luanti/17231/current-v2",
    "candidate_current_chain_blocker",
    "1.0",
)


def write_luanti_17231_current_chain_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist and typed-reload the no-current-chain blocker."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#17231 current-chain blocker authorization is inactive")

    root = Path(artifact_root).absolute().resolve(strict=True)
    refs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    chain = tuple(_load(root, ref) for ref in refs)
    source, patch, atomicity, manifest = chain
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#17231 current-v2 chain candidate binding drifted")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#17231 source packet is incomplete")
    if patch.payload.get("complete") is not True or patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#17231 test patch is not eligible")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 16894
    ):
        raise RuntimeError("#17231 source atomicity is not the accepted single-issue receipt")
    details = manifest.payload.get("details")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != ["src/unittest/test_compression.cpp"]
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#17231 test manifest is not passed")

    # Probe only exact candidate namespaces.  A pre-existing closure means
    # this receipt is stale and must be re-audited instead of overwriting it.
    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/17231",
        "candidate_preparation": root / "artifacts/candidate_preparation/luanti/17231",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/17231",
        "gate57_profile": root / "artifacts/candidate_gate57_profile/luanti/17231",
        "fresh_toolchain": root / "artifacts/candidate_gate57_fresh_toolchain/luanti/17231",
        "native_dependency_preflight": root
        / "artifacts/candidate_stage/luanti/17231/native_dependency_preflight",
        "gate57_transition": root / "artifacts/candidate_gate57_transition/luanti/17231",
        "gate57_aggregate": root / "artifacts/candidate_gate57_aggregate/luanti/17231",
        "native_build": root / "artifacts/native_regression_build/luanti/17231",
        "native_attempt": root / "artifacts/native_regression_attempt/luanti/17231",
    }
    present = {name: path.exists() for name, path in required.items()}
    if any(present.values()):
        raise RuntimeError("#17231 current-chain blocker inputs changed; re-audit exact chain")

    payload = {
        "candidate": dict(_CANDIDATE),
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "current_v2_chain_stops_before_candidate_capsule_profile_runtime",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_v2_chain_revisions": {
            artifact.reference.artifact_id: artifact.created_at
            for artifact in chain
        },
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed_primary_issue_16894",
            "gate_3_strict_inverse_patch": "passed_current_v2_patch_eligible",
            "gate_4_test_manifest": "passed",
            "gate_4_native_api_compatibility": "not_run_missing_candidate_capsule",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "required_current_v2_chain": {
            name: {"registered": present[name], "probe": path.as_posix()}
            for name, path in required.items()
        },
        "missing_for_fresh_gate5_7": [
            "candidate_specific_profile_and_selector_binding",
            "fixed_source_capsule_at_merged_fix_revision",
            "production_only_buggy_inverse_receipt",
            "candidate_specific_native_toolchain_and_dependency_closure",
            "fixed_buggy_build_receipts",
            "fresh_12_cell_gate57_runner_aggregate_and_transition",
        ],
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
        "next_action": (
            "register candidate-owned capsule/profile/runtime/toolchain, then re-audit Gate 4 "
            "and launch one independent fresh Gate 5--7 epoch"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    existing = _try_load(root)
    inputs = refs
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#17231 current-chain blocker conflicts with exact state")
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
        raise RuntimeError("#17231 current-chain blocker typed reload failed")
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
        return _load(root, _BLOCKER)
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


__all__ = ["write_luanti_17231_current_chain_blocker"]
