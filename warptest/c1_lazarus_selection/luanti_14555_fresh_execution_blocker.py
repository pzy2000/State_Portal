"""Typed infrastructure blocker when #14555 has no local executable pair.

This is deliberately a no-inheritance, no-build preflight.  It binds the
current v2 chain and leaves the candidate resumable for a later fresh epoch.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact, load_stable_artifact, write_stable_artifact


_CANDIDATE = {"repository_key": "luanti", "change_number": 14555}
_SOURCE = ArtifactReference("candidate_source_packet/luanti/14555/source/v2", "candidate_source_packet", "2.0")
_PATCH = ArtifactReference("candidate_test_patch/luanti/14555/screening/v2", "candidate_test_patch", "2.0")
_ATOMICITY = ArtifactReference("source_atomicity/luanti/14555/preannotation/v2", "source_atomicity", "2.0")
_MANIFEST = ArtifactReference("candidate_stage/luanti/14555/test_manifest/v2", "candidate_stage", "2.0")
_MATERIALIZATION = ArtifactReference("candidate_stage/luanti/14555/version_materialization/v2", "candidate_stage", "2.0")
_BLOCKER = ArtifactReference(
    "candidate_gate57_fresh_execution_blocker/luanti/14555/preannotation/v1",
    "candidate_gate57_fresh_execution_blocker",
    "1.0",
)


def write_luanti_14555_fresh_execution_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Record missing local binaries without creating attempts or builds."""

    if authorization.scope != "preannotation-execution":
        raise PermissionError("#14555 execution blocker requires preannotation authorization")
    if authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("#14555 execution blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest, materialization = tuple(
        _load(root, reference)
        for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _MATERIALIZATION)
    )
    chain = (source, patch, atomicity, manifest, materialization)
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#14555 fresh execution blocker candidate binding is invalid")
    if (
        source.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or atomicity.payload.get("outcome") != "accepted"
        or manifest.payload.get("state") != "passed"
        or materialization.payload.get("state") != "passed"
    ):
        raise RuntimeError("#14555 current v2 chain is not ready for execution blocker")
    _assert_absent(root)
    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "reason": "current_v2_materialization_has_no_local_executable_pair_for_fresh_epoch",
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "version_materialization_receipt": "passed",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_curated_attempts_referenced": False,
            "fixed_expected": {"f2p": "pass", "p2p": "pass"},
            "buggy_expected": {"f2p": "failure", "p2p": "pass"},
        },
        "materialization_receipt": {
            "reference": _MATERIALIZATION.artifact_id,
            "declared_versions": ["fixed", "buggy"],
            "local_executable_pair": "unavailable",
            "new_build_started": False,
        },
        "next_action": "provide_registered_fixed_buggy_executable_pair_then_run_fresh_gate57_epoch",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = tuple(artifact.reference for artifact in chain)
    write_stable_artifact(root, artifact_id=_BLOCKER.artifact_id, artifact_kind=_BLOCKER.artifact_kind, semantic_version=_BLOCKER.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    artifact = _load(root, _BLOCKER)
    if artifact.payload != payload or artifact.inputs != inputs:
        raise RuntimeError("#14555 fresh execution blocker typed reload failed")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _assert_absent(root: Path) -> None:
    path = root / "artifacts" / Path(_BLOCKER.artifact_id) / _BLOCKER.semantic_version
    if path.exists():
        raise RuntimeError("#14555 fresh execution blocker already exists")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_14555_fresh_execution_blocker"]
