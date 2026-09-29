"""Keep Luanti #14072 in the candidate pool while Gate 5--7 is blocked.

The current-v2 source, patch, atomicity, profile, and test-manifest chain is
valid.  The native downstream preflight is infrastructure-blocked before any
candidate execution because the fixed capsule, registered IrrlichtMt headers,
and an isolated selector are absent.  This receipt makes that distinction
explicit and never turns the downstream blocker into a quality terminal.
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
_RECEIPT = ArtifactReference(
    "candidate_reclassification/luanti/14072/current-v2",
    "candidate_reclassification",
    "1.0",
)


def write_luanti_14072_candidate_reclassification(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist and reload a nonterminal candidate-pool blocker receipt."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    blocker = _load(root, _BLOCKER)
    _validate_chain(chain, blocker)
    payload = {
        "candidate": _CANDIDATE,
        "state": "candidate_pool_reopened_pending_gate5_7",
        "outcome": "candidate_pool_reclassified",
        "terminal": False,
        "resumable": True,
        "candidate_pool_status": "eligible_for_fresh_candidate_specific_gate5_7",
        "reclassification": {
            "from": "infrastructure_blocked_gate5_7",
            "to": "candidate_pool_pending",
            "reason": "downstream_infrastructure_is_nonterminal_and_gate1_4_remain_valid",
            "roster_modified": False,
            "old_attempts_loaded": False,
            "old_terminal_conclusion_inherited": False,
        },
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed",
            "gate_3_strict_inverse_patch": "passed",
            "gate_4_test_api_compatibility": "passed",
            "gate_5_fixed_f2p_3x": "blocked_before_first_attempt",
            "gate_6_buggy_f2p_3x_failure": "blocked_before_first_attempt",
            "gate_7_fixed_buggy_p2p_3x": "blocked_before_first_attempt",
        },
        "blockers": blocker.payload.get("blockers", []),
        "fresh_gate57_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "candidate_specific_runner_created": False,
        },
        "next_action": (
            "register_current_v2_fixed_capsule_and_IrrlichtMt_headers, isolate "
            "TestMoveAction selectors, then run a fresh candidate-specific 12-cell epoch"
        ),
        "resource_policy": {
            "network": False,
            "paid": False,
            "formal_trials": False,
        },
    }
    inputs = _CHAIN + (_BLOCKER,)
    existing = _try_load(root, _RECEIPT)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14072 candidate-pool receipt conflicts with current state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_RECEIPT.artifact_id,
        artifact_kind=_RECEIPT.artifact_kind,
        semantic_version=_RECEIPT.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _RECEIPT)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#14072 candidate-pool receipt typed reload failed")
    return artifact


def _validate_chain(
    chain: tuple[StableArtifact, ...], blocker: StableArtifact
) -> None:
    if any(item.payload.get("candidate") != _CANDIDATE for item in chain):
        raise RuntimeError("#14072 candidate chain binding drifted")
    source, patch, atomicity, manifest, profile, preparation, plan, native = chain
    if (
        source.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or atomicity.payload.get("outcome") != "accepted"
        or manifest.payload.get("state") != "passed"
        or profile.payload.get("strict_gate_status", {}).get("source_authority")
        != "passed"
        or profile.payload.get("strict_gate_status", {}).get("single_defect_mapping")
        != "passed"
        or profile.payload.get("strict_gate_status", {}).get("strict_inverse_patch")
        != "passed"
        or profile.payload.get("strict_gate_status", {}).get("test_api_compatibility")
        != "passed"
        or preparation.payload.get("state") != "prepared"
        or plan.payload.get("state") != "planned"
        or native.payload.get("outcome") != "infrastructure_blocked"
        or native.payload.get("candidate_execution_performed") is not False
        or blocker.payload.get("state") != "blocked"
        or blocker.payload.get("terminal") is not False
        or blocker.payload.get("resumable") is not True
        or blocker.payload.get("candidate_execution_performed") is not False
    ):
        raise RuntimeError("#14072 current Gate1--7 state is invalid")


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


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14072 candidate-pool receipt authorization is inactive")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


__all__ = ["write_luanti_14072_candidate_reclassification"]
