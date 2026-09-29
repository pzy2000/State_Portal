"""Typed no-current-chain blocker for Luanti #15408.

Only the current-v2 source packet is registered for this candidate.  This
read-only receipt deliberately does not infer a candidate profile, capsule,
toolchain, build, or Gate 5--7 epoch from another change number.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Mapping

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15408}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/15408/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/15408/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/15408/preannotation/v2",
    "source_atomicity",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/15408/test_manifest/v2",
    "candidate_stage",
    "2.0",
)
_BLOCKER = ArtifactReference(
    "candidate_current_chain_blocker/luanti/15408/current-v2",
    "candidate_current_chain_blocker",
    "1.0",
)


def write_luanti_15408_current_chain_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist a resumable blocker without building or running anything."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15408 current-chain blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = load_stable_artifact(
        root,
        artifact_id=_SOURCE.artifact_id,
        artifact_kind=_SOURCE.artifact_kind,
        semantic_version=_SOURCE.semantic_version,
    )
    if source.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#15408 source packet is not candidate-bound")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#15408 source packet is not complete")
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    if patch.payload.get("candidate") != _CANDIDATE or patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#15408 test patch is not eligible")
    if atomicity.payload.get("candidate") != _CANDIDATE or atomicity.payload.get("outcome") != "accepted":
        raise RuntimeError("#15408 source atomicity is not accepted")
    if manifest.payload.get("candidate") != _CANDIDATE or manifest.payload.get("state") != "passed":
        raise RuntimeError("#15408 test manifest is not passed")

    # These are the only current-v2 chain namespaces needed before a fresh
    # Gate 5--7 epoch.  Probe exact paths; do not scan the artifact tree.
    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/15408",
        "candidate_test_patch": root / "artifacts/candidate_test_patch/luanti/15408",
        "source_atomicity": root / "artifacts/source_atomicity/luanti/15408",
        "candidate_stage": root / "artifacts/candidate_stage/luanti/15408",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/15408",
        "gate57_profile": root / "artifacts/candidate_gate57_profile/luanti/15408",
        "gate57_aggregate": root / "artifacts/candidate_gate57_aggregate/luanti/15408",
        "gate57_transition": root / "artifacts/candidate_gate57_transition/luanti/15408",
    }
    present = {name: path.exists() for name, path in required.items()}
    if any(present[name] for name in ("candidate_profile", "fixed_source_capsule", "gate57_profile", "gate57_aggregate", "gate57_transition")):
        raise RuntimeError("#15408 current-chain blocker inputs changed; re-audit exact chain")
    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "current_v2_chain_not_registered_beyond_source_patch_atomicity_manifest",
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_invalidate_other_candidates": True,
        "source_status": "complete_source_patch_atomicity_manifest_without_capsule_or_profile",
        "required_current_v2_chain": {
            name: {"registered": present[name]} for name in required
        },
        "missing_for_fresh_gate5_7": [
            "candidate_profile",
            "fixed_source_capsule_and_buggy_inverse",
            "candidate_specific_toolchain_and_build_plan",
            "fresh_12_cell_gate57_runner",
        ],
        "next_action": "register_complete_current_v2_chain_then_run_independent_fresh_gate57_epoch",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    existing = _try_load(root)
    if existing is not None:
        inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#15408 current-chain blocker conflicts with exact state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=(_SOURCE, _PATCH, _ATOMICITY, _MANIFEST),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load_blocker(root)
    if artifact.inputs != (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST) or dict(artifact.payload) != payload:
        raise RuntimeError("#15408 current-chain blocker typed reload failed")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _load_blocker(root: Path) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
    )


def _try_load(root: Path) -> StableArtifact | None:
    try:
        return _load_blocker(root)
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


__all__ = ["write_luanti_15408_current_chain_blocker"]
