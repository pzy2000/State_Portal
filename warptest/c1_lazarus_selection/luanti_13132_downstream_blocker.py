"""Typed, read-only downstream blocker for current-v2 Luanti #13132.

The candidate is already qualified through Gate 1--7.  This module records
the missing current-v2 application pair and IrrlichtMt closure without
starting a build, launching a process, or importing another candidate's
runtime/attempt evidence.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 13132}
_AGGREGATE = ArtifactReference(
    "candidate_gate57_13132_timeout_repair_aggregate/luanti/13132/lua/v1",
    "candidate_gate57_13132_timeout_repair_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_13132_timeout_repair_transition/luanti/13132/lua/v1",
    "candidate_gate57_13132_timeout_repair_transition",
    "1.0",
)
_BUILD = ArtifactReference(
    "candidate_build_receipt/luanti/13132/fixed-buggy/v1",
    "candidate_build_receipt",
    "1.0",
)
_PREFLIGHT = ArtifactReference(
    "downstream_preflight/luanti/13132/runtime-visual/v3",
    "downstream_preflight",
    "3.0",
)
_BLOCKER = ArtifactReference(
    "candidate_downstream_blocker/luanti/13132/current-v2",
    "candidate_downstream_blocker",
    "1.0",
)


def write_luanti_13132_downstream_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Record a resumable blocker and strictly reload it."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#13132 downstream blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    aggregate = _load(root, _AGGREGATE)
    transition = _load(root, _TRANSITION)
    build = _load(root, _BUILD)
    preflight = _load(root, _PREFLIGHT)
    if aggregate.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#13132 aggregate is not candidate-bound")
    if transition.payload.get("qualified") is not True:
        raise RuntimeError("#13132 Gate 1--7 transition is not qualified")
    if build.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#13132 build receipt is not candidate-bound")
    if build.payload.get("outcome") != "infrastructure_blocked":
        raise RuntimeError("#13132 build receipt no longer records the blocker")
    if build.payload.get("configure", {}).get("required_dependency") != "IrrlichtMt":
        raise RuntimeError("#13132 missing dependency is not IrrlichtMt")
    if preflight.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#13132 downstream preflight is not candidate-bound")
    if preflight.payload.get("application_pair_status") != "missing":
        raise RuntimeError("#13132 application pair is no longer missing")
    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_runtime_inheritance": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_invalidate_gate_1_7": True,
        "reason": "current_v2_application_pair_and_irrlichtmt_closure_missing",
        "current_gate_status": {
            "gate_1_7": "qualified",
            "gate8_source_capsule_and_mapping": "passed",
            "gate8_application_pair": "blocked_missing_fixed_buggy_pair",
            "gate9_to_13": "pending",
        },
        "observed_missing": {
            "fixed_application": True,
            "buggy_application": True,
            "registered_irrlichtmt_headers": False,
            "registered_irrlichtmt_library": False,
            "matching_candidate_specific_runtime": False,
        },
        "required_closure": {
            "dependency": "IrrlichtMt 1.9.0mt9-compatible headers/library",
            "versions": ["fixed", "buggy"],
            "immutable_candidate_specific_build_receipt": True,
            "world_task_and_registered_start": True,
        },
        "next_action": "register_candidate_specific_irrlichtmt_and_materialize_fixed_buggy_pair",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_AGGREGATE, _TRANSITION, _BUILD, _PREFLIGHT)
    existing = _try_load(root, _BLOCKER)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#13132 downstream blocker conflicts with current state")
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
        raise RuntimeError("#13132 downstream blocker typed reload failed")
    return artifact


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


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_13132_downstream_blocker"]
