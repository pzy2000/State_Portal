"""No-inheritance Gate 5--7 blocker for Luanti change 14072.

The current v2 source/patch/atomicity/test-manifest chain is admitted through
Gate 1--4, but this workspace has no fixed-source capsule or registered
IrrlichtMt closure from which a native server pair could be built.  This
candidate-specific receipt records that fact without starting a build or
creating attempts.  It deliberately binds every current typed prerequisite so
that a later resume must re-run the fresh epoch from this exact chain.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 14072}
_CHAIN = (
    ArtifactReference(
        "candidate_source_packet/luanti/14072/source/v2",
        "candidate_source_packet",
        "2.0",
    ),
    ArtifactReference(
        "candidate_test_patch/luanti/14072/screening/v2",
        "candidate_test_patch",
        "2.0",
    ),
    ArtifactReference(
        "source_atomicity/luanti/14072/preannotation/v2",
        "source_atomicity",
        "2.0",
    ),
    ArtifactReference(
        "candidate_stage/luanti/14072/test_manifest/v2",
        "candidate_stage",
        "2.0",
    ),
    ArtifactReference(
        "candidate_profile/luanti/14072/preflight/v2",
        "candidate_profile",
        "2.0",
    ),
    ArtifactReference(
        "candidate_preparation/luanti/14072/preannotation/v2",
        "candidate_preparation",
        "2.0",
    ),
    ArtifactReference(
        "candidate_downstream_plan/luanti/14072/preannotation/v2",
        "candidate_downstream_plan",
        "2.0",
    ),
    ArtifactReference(
        "candidate_stage/luanti/14072/native_dependency_preflight/v1",
        "candidate_stage",
        "1.0",
    ),
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_fresh_execution_blocker/luanti/14072/native/v1",
    "candidate_gate57_fresh_execution_blocker",
    "1.0",
)


def write_luanti_14072_fresh_gate57_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write and strictly reload the current-v2, no-inheritance blocker."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14072 fresh Gate 5--7 blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    _validate_chain(chain)
    _assert_absent(root)

    payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-14072-oversized-stack-v2",
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "paid_actions_authorized": False,
        "reason": "current_v2_native_execution_prerequisites_missing",
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
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
        "blockers": [
            {
                "kind": "candidate_fixed_capsule_missing",
                "required_path": "private/fixed-capsules/luanti/14072/fixed-source",
            },
            {
                "kind": "registered_irrlichtmt_headers_missing",
                "required_by": "server-only configure",
                "required_version": "1.9.0mt13-compatible",
                "required_root": "private/dependencies/luanti-native/3.0",
            },
            {
                "kind": "exact_selector_not_isolated",
                "cli_selector": "TestMoveAction",
                "f2p_method": "testMoveFillStack",
                "p2p_method": "testMovePartial",
                "reason": "--test-module executes both methods together",
            },
        ],
        "observed_assets": {
            "fixed_capsule_present": False,
            "fixed_buggy_materialized_trees": False,
            "candidate_specific_build_receipts": False,
            "candidate_specific_fixed_buggy_binaries": False,
            "native_toolchain_registered": False,
            "new_build_started": False,
        },
        "next_action": (
            "register_current_v2_fixed_capsule_and_IrrlichtMt_closure_then_run_isolated_fresh_12_cell_epoch"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = tuple(reference for reference in _CHAIN)
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
        raise RuntimeError("#14072 fresh Gate 5--7 blocker typed reload failed")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#14072 current-v2 chain candidate binding is invalid")
    source, patch, atomicity, manifest, profile, preparation, plan, native = chain
    if source.payload.get("complete") is not True:
        raise RuntimeError("#14072 source packet is not complete")
    if patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#14072 test patch is not eligible")
    if atomicity.payload.get("outcome") != "accepted":
        raise RuntimeError("#14072 source atomicity is not accepted")
    if manifest.payload.get("state") != "passed":
        raise RuntimeError("#14072 test manifest is not passed")
    if profile.payload.get("strict_gate_status", {}).get("test_api_compatibility") != "passed":
        raise RuntimeError("#14072 profile does not prove API compatibility")
    if preparation.payload.get("state") != "prepared" or plan.payload.get("state") != "planned":
        raise RuntimeError("#14072 current preparation/plan state is invalid")
    if native.payload.get("outcome") != "infrastructure_blocked":
        raise RuntimeError("#14072 native dependency preflight is not blocked")
    if native.payload.get("candidate_execution_performed") is not False:
        raise RuntimeError("#14072 preflight unexpectedly executed candidate")


def _assert_absent(root: Path) -> None:
    directory = root / "artifacts" / Path(_BLOCKER.artifact_id) / _BLOCKER.semantic_version
    if directory.exists():
        raise RuntimeError("#14072 fresh Gate 5--7 blocker already exists")


def _now() -> str:
    return (
        datetime.now(UTC) + timedelta(microseconds=1)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_14072_fresh_gate57_blocker"]
