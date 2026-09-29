"""Typed, resumable Gate 5--7 blocker for Luanti #11011.

The current-v2 source, inverse patch, source-atomicity receipt and test
manifest are already registered for this candidate.  There is no registered
candidate-specific native profile, fixed-source capsule, toolchain closure or
fresh Gate 5--7 runner, so this module records the exact preflight stop
without building, executing, inheriting an old attempt, or probing the whole
artifact tree.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 11011}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/11011/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/11011/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/11011/preannotation/v2",
    "source_atomicity",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/11011/test_manifest/v2",
    "candidate_stage",
    "2.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_11011_fresh_execution_blocker/luanti/11011/native/v1",
    "candidate_gate57_11011_fresh_execution_blocker",
    "1.0",
)


def write_luanti_11011_current_chain_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist and strictly reload the no-current-native-chain receipt."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#11011 current-chain blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference) for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    _validate_chain(chain)

    # Probe only exact current-v2 namespaces.  A new registration must force a
    # fresh audit instead of silently leaving this receipt in place.
    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/11011",
        "candidate_preparation": root / "artifacts/candidate_preparation/luanti/11011",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/11011",
        "native_dependency_preflight": root
        / "artifacts/candidate_stage/luanti/11011/native_dependency_preflight",
        "gate57_profile": root / "artifacts/candidate_gate57_profile/luanti/11011",
        "gate57_aggregate": root / "artifacts/candidate_gate57_aggregate/luanti/11011",
        "gate57_transition": root
        / "artifacts/candidate_gate57_transition/luanti/11011",
    }
    present = {name: path.exists() for name, path in required.items()}
    if any(present.values()):
        raise RuntimeError(
            "#11011 current-v2 native prerequisites changed; re-audit exact chain"
        )

    payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-11011-schematic-node-resolver-v2",
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "reason": "current_v2_native_execution_chain_not_registered",
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
                {
                    "phase": "f2p",
                    "variant": "fixed",
                    "repetitions": 3,
                    "expected": "pass",
                },
                {
                    "phase": "f2p",
                    "variant": "buggy",
                    "repetitions": 3,
                    "expected": "failure",
                },
                {
                    "phase": "p2p",
                    "variant": "fixed",
                    "repetitions": 3,
                    "expected": "pass",
                },
                {
                    "phase": "p2p",
                    "variant": "buggy",
                    "repetitions": 3,
                    "expected": "pass",
                },
            ],
        },
        "required_current_v2_chain": {
            name: {"registered": present[name], "probe": path.as_posix()}
            for name, path in required.items()
        },
        "missing_for_fresh_gate5_7": [
            "candidate_specific_native_profile_and_selector_binding",
            "fixed_source_capsule_and_production_only_buggy_inverse",
            "registered_native_toolchain_and_dependency_closure",
            "candidate_specific_fixed_buggy_build_receipts",
            "fresh_12_cell_gate57_runner_and_aggregate",
        ],
        "next_action": (
            "register_current_v2_fixed_capsule_native_toolchain_and_profile_then_run_independent_fresh_12_cell_epoch"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(
                "#11011 current-chain blocker conflicts with exact state"
            )
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
        raise RuntimeError("#11011 current-chain blocker typed reload failed")
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
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#11011 current-v2 chain candidate binding is invalid")
    source, patch, atomicity, manifest = chain
    if source.payload.get("complete") is not True:
        raise RuntimeError("#11011 source packet is not complete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("#11011 test patch is not eligible")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 10995
    ):
        raise RuntimeError("#11011 source atomicity is not accepted")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("test_count") != 2
    ):
        raise RuntimeError("#11011 test manifest is not passed")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_11011_current_chain_blocker"]
