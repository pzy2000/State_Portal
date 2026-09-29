"""No-inheritance blocker for a fresh Luanti #14555 Gate 5--7 epoch.

The current v2 source/patch/atomicity/materialization chain is sound, but old
test-transition receipts are deliberately not admitted as current evidence.
This receipt records the smallest next action without starting a runtime or a
build.
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
    "candidate_gate57_fresh_required_blocker/luanti/14555/preannotation/v1",
    "candidate_gate57_fresh_required_blocker",
    "1.0",
)


def write_luanti_14555_fresh_epoch_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Bind the current chain and require twelve new, isolated cells."""

    if authorization.scope != "candidate-screening":
        raise PermissionError("#14555 fresh-epoch blocker requires candidate-screening authorization")
    if authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("#14555 fresh-epoch blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    artifacts = tuple(_load(root, reference) for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _MATERIALIZATION))
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in artifacts):
        raise RuntimeError("#14555 current v2 chain candidate binding is invalid")
    if artifacts[0].payload.get("complete") is not True or artifacts[1].payload.get("outcome") != "patch_eligible" or artifacts[2].payload.get("outcome") != "accepted" or artifacts[3].payload.get("state") != "passed" or artifacts[4].payload.get("state") != "passed":
        raise RuntimeError("#14555 current v2 chain is not ready for a fresh-epoch blocker")
    _assert_absent(root)
    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "fresh_epoch_required",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "reason": "no_inheritance_requires_new_fresh_12_cell_attempts",
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "version_materialization": "passed",
            "gate57_admission": "pending_fresh_epoch",
        },
        "fresh_epoch": {
            "required_attempt_count": 12,
            "cells": [
                {"phase": "f2p", "variant": "fixed", "repetitions": 3, "expected": "pass"},
                {"phase": "f2p", "variant": "buggy", "repetitions": 3, "expected": "failure"},
                {"phase": "p2p", "variant": "fixed", "repetitions": 3, "expected": "pass"},
                {"phase": "p2p", "variant": "buggy", "repetitions": 3, "expected": "pass"},
            ],
            "inherit_prior_attempts": False,
            "requires_current_chain": True,
        },
        "prior_execution_admissibility": "not_admitted_without_fresh_epoch",
        "next_action": "execute_candidate_specific_fresh_gate57_epoch_when_authorized",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = tuple(artifact.reference for artifact in artifacts)
    write_stable_artifact(root, artifact_id=_BLOCKER.artifact_id, artifact_kind=_BLOCKER.artifact_kind, semantic_version=_BLOCKER.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    artifact = _load(root, _BLOCKER)
    if artifact.payload != payload or artifact.inputs != inputs:
        raise RuntimeError("#14555 fresh-epoch blocker typed reload failed")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _assert_absent(root: Path) -> None:
    path = root / "artifacts" / Path(_BLOCKER.artifact_id) / _BLOCKER.semantic_version
    if path.exists():
        raise RuntimeError("#14555 fresh-epoch blocker already exists")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_14555_fresh_epoch_blocker"]
