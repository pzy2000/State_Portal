"""Typed, non-terminal Gate 2 blocker for Luanti change #8742.

The current-v2 source/patch/manifest heads are intact, but the authoritative
issue receipt has no authoritative issue (only linked issue #15728).  This
receipt deliberately stops before capsule/profile/toolchain creation and any
native build or twelve-cell execution.  It does not eliminate the candidate;
it is resumable after a corrected authority mapping is captured.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 8742}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/8742/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/8742/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/8742/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/8742/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/8742/test_manifest/v2", "candidate_stage", "2.0"
)
_BLOCKER = ArtifactReference(
    "candidate_gate2_authority_blocker/luanti/8742/current-v2",
    "candidate_gate2_authority_blocker",
    "1.0",
)


def write_luanti_8742_gate2_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Bind the exact heads and persist the non-terminal Gate 2 stop."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#8742 Gate 2 blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    refs = (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST)
    chain = tuple(_load(root, ref) for ref in refs)
    enumeration, source, patch, checkpoint, atomicity, manifest = chain
    if enumeration.payload.get("repository_key") != "luanti":
        raise RuntimeError("#8742 enumeration is not Luanti")
    for artifact in chain[1:]:
        if artifact.payload.get("candidate") != _CANDIDATE:
            raise RuntimeError("#8742 current-v2 chain candidate binding drifted")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#8742 source head is incomplete")
    if patch.payload.get("complete") is not True or patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#8742 patch head is not eligible")
    if checkpoint.inputs != (_ENUMERATION, _PATCH):
        raise RuntimeError("#8742 screening checkpoint binding drifted")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "rejected"
        or atomicity.payload.get("authoritative_issue_count") != 0
        or atomicity.payload.get("linked_issue_numbers") != [15728]
    ):
        raise RuntimeError("#8742 expected missing-authority receipt not present")
    if manifest.payload.get("complete") is not True or manifest.payload.get("state") != "passed":
        raise RuntimeError("#8742 test manifest is not passed")

    payload = {
        "candidate": dict(_CANDIDATE),
        "state": "blocked",
        "outcome": "gate2_authoritative_issue_missing",
        "terminal": False,
        "resumable": True,
        "candidate_eligibility": "not_admitted_pending_authority_mapping",
        "does_not_freeze_or_eliminate_candidate": True,
        "reason": "current_v2_source_atomicity_rejected_authoritative_issue_missing",
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "blocked_authoritative_issue_missing",
            "gate_3_strict_inverse_patch": "not_admitted_before_gate2_repair",
            "gate_4_fixed_test_api_compatibility": "not_admitted_before_gate2_repair",
            "gate_5_fixed_f2p_3x": "not_run_gate2_blocker",
            "gate_6_buggy_f2p_3x_failure": "not_run_gate2_blocker",
            "gate_7_fixed_buggy_p2p": "not_run_gate2_blocker",
        },
        "chain_refs": {ref.artifact_id: artifact.created_at for ref, artifact in zip(refs, chain)},
        "source_atomicity": {
            "receipt_outcome": atomicity.payload["outcome"],
            "reason": atomicity.payload["reason"],
            "authoritative_issue_numbers": list(atomicity.payload["authoritative_issue_numbers"]),
            "linked_issue_numbers": list(atomicity.payload["linked_issue_numbers"]),
            "single_defect_mapping_admissible": False,
        },
        "candidate_materialization_performed": False,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "downstream_8_13_authorized": False,
        "fresh_gate57_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
            "candidate_specific_runner_created": False,
        },
        "next_action": (
            "repair_or_recapture the authoritative issue mapping in current-v2, "
            "then re-audit Gates 2--4 before registering capsule/profile/toolchain"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = tuple(ref for ref in refs)
    try:
        existing = _load(root, _BLOCKER)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#8742 Gate 2 blocker conflicts with current-v2 state")
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
        raise RuntimeError("#8742 Gate 2 blocker typed reload failed")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_8742_gate2_blocker"]
