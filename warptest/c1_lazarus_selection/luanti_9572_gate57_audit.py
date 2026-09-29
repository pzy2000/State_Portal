"""Typed audit for the #9572 selector-corrected runtime blocker.

The corrected epoch selected exactly one test, but the registered Lua 5.1
runtime lacks ``math.hypot`` used by the fixed vector implementation.  That
is an infrastructure/API-compatibility blocker, not a candidate semantic
terminal; this receipt preserves the distinction without rewriting either
fresh execution aggregate.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact, load_stable_artifact, write_stable_artifact


_AGGREGATE = ArtifactReference(
    "candidate_gate57_9572_retry_aggregate/luanti/9572/lua/v1",
    "candidate_gate57_9572_retry_aggregate",
    "1.0",
)
_AUDIT = ArtifactReference(
    "candidate_gate57_9572_retry_runtime_audit/luanti/9572/lua/v1",
    "candidate_gate57_9572_retry_runtime_audit",
    "1.0",
)


def write_luanti_9572_runtime_audit(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist a non-terminal reclassification of the retry runtime result."""

    if authorization.scope != "preannotation-execution":
        raise PermissionError("#9572 runtime audit requires preannotation authorization")
    if authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("#9572 runtime audit authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    aggregate = load_stable_artifact(
        root,
        artifact_id=_AGGREGATE.artifact_id,
        artifact_kind=_AGGREGATE.artifact_kind,
        semantic_version=_AGGREGATE.semantic_version,
    )
    if aggregate.payload.get("candidate") != {"repository_key": "luanti", "change_number": 9572}:
        raise RuntimeError("#9572 runtime audit candidate identity is invalid")
    if aggregate.payload.get("outcome") != "candidate_semantic_mismatch":
        raise RuntimeError("#9572 runtime audit expected the selector-corrected mismatch")
    if aggregate.payload.get("cells", [{}])[1].get("observed_outcomes") != ["failure", "failure", "failure"]:
        raise RuntimeError("#9572 runtime audit fixed-cell evidence is invalid")
    if (root / "artifacts" / Path(_AUDIT.artifact_id) / _AUDIT.semantic_version).exists():
        raise RuntimeError("#9572 runtime audit already exists")
    payload = {
        "candidate": {"repository_key": "luanti", "change_number": 9572},
        "profile_id": "luanti-9572-rotation-selector-corrected-v1",
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "reclassified_from": aggregate.reference.artifact_id,
        "reason": "registered_busted_runtime_missing_math_hypot",
        "evidence": {
            "phase": "f2p",
            "variant": "fixed",
            "selector": "vector rotate() rotates",
            "observed_process_outcome": "completed",
            "observed_returncode": 1,
            "observed_test_count": 1,
            "runtime_error": "builtin/common/vector.lua:62: attempt to call field 'hypot' (a nil value)",
        },
        "next_action": "provide_registered_math_hypot_compatibility_or_select_vector_candidate_without_hypot_dependency",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    write_stable_artifact(
        root,
        artifact_id=_AUDIT.artifact_id,
        artifact_kind=_AUDIT.artifact_kind,
        semantic_version=_AUDIT.semantic_version,
        created_at=_now(),
        inputs=(aggregate.reference,),
        payload=payload,
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        root,
        artifact_id=_AUDIT.artifact_id,
        artifact_kind=_AUDIT.artifact_kind,
        semantic_version=_AUDIT.semantic_version,
    )
    if artifact.payload != payload or artifact.inputs != (aggregate.reference,):
        raise RuntimeError("#9572 runtime audit typed reload failed")
    return artifact


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_9572_runtime_audit"]
