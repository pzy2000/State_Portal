"""Typed, read-only downstream blocker for Luanti #15408.

Gate 1--7 is already qualified for this candidate.  The native fixed source
capsule is present, but there is no candidate-bound Lazarus mapping/fixture,
materialized fixed/buggy application pair, or registered runtime dependency.
This receipt records that exact missing closure without copying another
candidate's runtime, starting a process, or changing Gate 1--7.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/15408/fixed/v2",
    "fixed_source_capsule",
    "2.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_transition/luanti/15408/native/v1",
    "candidate_gate57_transition",
    "1.0",
)
_AGGREGATE = ArtifactReference(
    "native_regression_aggregate/luanti/15408/fixed-buggy/fresh-20260814-15408-v1",
    "native_regression_aggregate",
    "3.0",
)
_GATE8 = ArtifactReference(
    "candidate_gate8_readiness_blocker/luanti/15408/current-v2",
    "candidate_gate8_readiness_blocker",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_downstream_blocker/luanti/15408/current-v2",
    "candidate_downstream_blocker",
    "1.0",
)

_INPUTS = (
    _SOURCE,
    _PATCH,
    _ATOMICITY,
    _MANIFEST,
    _CAPSULE,
    _TRANSITION,
    _AGGREGATE,
    _GATE8,
)


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


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


def _validate_upstream(root: Path) -> dict[str, StableArtifact]:
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    capsule = _load(root, _CAPSULE)
    transition = _load(root, _TRANSITION)
    aggregate = _load(root, _AGGREGATE)
    gate8 = _load(root, _GATE8)

    if source.payload.get("candidate") != _CANDIDATE or source.payload.get("complete") is not True:
        raise RuntimeError("#15408 source packet is not complete and candidate-bound")
    if (
        patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("#15408 patch is not eligible and candidate-bound")
    if (
        atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
    ):
        raise RuntimeError("#15408 atomicity is not accepted as one authoritative issue")
    if (
        manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("state") != "passed"
    ):
        raise RuntimeError("#15408 test manifest is not passed")
    if (
        capsule.payload.get("candidate") != _CANDIDATE
        or capsule.payload.get("source_tree_path")
        != "private/fixed-capsules/luanti/15408/fixed-source"
        or capsule.payload.get("profile_id")
        != "luanti-15408-collision-acceleration-v1-native-gate57"
    ):
        raise RuntimeError("#15408 fixed capsule is not candidate-bound")
    if (
        transition.payload.get("candidate") != _CANDIDATE
        or transition.payload.get("state") != "passed"
        or transition.payload.get("outcome") != "qualified"
        or transition.payload.get("attempt_count") != 12
    ):
        raise RuntimeError("#15408 Gate 1--7 transition is not qualified")
    if (
        aggregate.payload.get("candidate") != _CANDIDATE
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
    ):
        raise RuntimeError("#15408 native aggregate is not qualified 12-cell evidence")
    if (
        gate8.payload.get("candidate") != _CANDIDATE
        or gate8.payload.get("outcome") != "infrastructure_blocked"
        or gate8.payload.get("terminal") is not False
        or gate8.payload.get("resumable") is not True
    ):
        raise RuntimeError("#15408 Gate8 readiness blocker is not resumable")
    return {
        "source": source,
        "patch": patch,
        "atomicity": atomicity,
        "manifest": manifest,
        "capsule": capsule,
        "transition": transition,
        "aggregate": aggregate,
        "gate8": gate8,
    }


def _readiness_payload(root: Path, chain: dict[str, StableArtifact]) -> dict[str, Any]:
    capsule_tree = root / "private/fixed-capsules/luanti/15408/fixed-source"
    materialization_root = root / "private/materialized/luanti/15408"
    fixed_application = materialization_root / "application-v2/fixed"
    buggy_application = materialization_root / "application-v2/buggy"
    mapping_root = root / "artifacts/lazarus_task_mapping/luanti/15408"
    fixture_root = root / "private/downstream-fixtures/luanti/15408"
    plan_root = root / "artifacts/candidate_downstream_plan/luanti/15408"
    preflight_root = root / "artifacts/downstream_preflight/luanti/15408"
    start_root = root / "artifacts/candidate_runtime_start/luanti/15408"
    replay_root = root / "artifacts/candidate_replay/luanti/15408"
    oracle_root = root / "artifacts/candidate_oracle/luanti/15408"
    dependency_artifact_root = root / "artifacts/candidate_runtime_dependency/luanti/15408"
    shared_dependency_root = root / "private/dependencies/luanti-native/3.0"

    def check(path: Path) -> dict[str, object]:
        return {"path": str(path), "present": path.exists()}

    transition = chain["transition"].payload
    aggregate = chain["aggregate"].payload
    return {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "candidate_bound_lazarus_mapping_fixture_and_runtime_source_closure_missing",
        "current_gate_status": {
            "gate_1_7": "qualified",
            "gate8_source_capsule": "passed",
            "gate8_candidate_mapping_fixture": "blocked_missing",
            "gate8_fixed_buggy_pair": "blocked_missing_materialization",
            "gate8_candidate_runtime_dependency": "blocked_no_candidate_registration",
            "gate9_start_replay": "pending",
            "gate10_oracle": "pending",
            "gate11_to_13": "pending",
        },
        "observed_paths": {
            "fixed_capsule_artifact": check(chain["capsule"].revision_path),
            "fixed_capsule_tree": check(capsule_tree),
            "candidate_materialization_root": check(materialization_root),
            "fixed_application_pair": check(fixed_application),
            "buggy_application_pair": check(buggy_application),
            "candidate_lazarus_mapping": check(mapping_root),
            "candidate_downstream_fixture": check(fixture_root),
            "candidate_downstream_plan": check(plan_root),
            "candidate_downstream_preflight": check(preflight_root),
            "candidate_runtime_start": check(start_root),
            "candidate_ordered_replay": check(replay_root),
            "candidate_hidden_oracle": check(oracle_root),
            "candidate_runtime_dependency_receipt": check(dependency_artifact_root),
            "common_dependency_root": check(shared_dependency_root),
        },
        "dependency_policy": {
            "candidate_specific_registration": False,
            "common_dependency_root_may_be_inherited": False,
            "other_candidate_dependency_reused": False,
            "network_fetch_performed": False,
        },
        "existing_gate57": {
            "transition_revision": str(chain["transition"].revision_path),
            "transition_state": transition.get("state"),
            "transition_outcome": transition.get("outcome"),
            "transition_attempt_count": transition.get("attempt_count"),
            "aggregate_revision": str(chain["aggregate"].revision_path),
            "aggregate_qualified": aggregate.get("qualified"),
            "aggregate_attempt_count": aggregate.get("attempt_count"),
        },
        "required_closure": {
            "candidate_bound_lazarus_mapping": True,
            "candidate_bound_downstream_fixture": True,
            "materialized_fixed_application_pair": True,
            "materialized_buggy_inverse_pair": True,
            "candidate_specific_runtime_dependency_receipt": True,
            "registered_start_and_ordered_replay": True,
            "candidate_hidden_oracle": True,
        },
        "missing_required_evidence": [
            "candidate_bound_lazarus_mapping",
            "candidate_bound_downstream_fixture",
            "candidate_bound_fixed_application_pair",
            "candidate_bound_buggy_application_pair",
            "candidate_specific_runtime_dependency_receipt",
            "candidate_bound_registered_start",
            "candidate_bound_ordered_replay",
            "candidate_bound_hidden_oracle",
        ],
        "next_action": "register_candidate_bound_lazarus_mapping_fixture_and_runtime_dependency_then_materialize_fixed_buggy_pair",
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_invalidate_gate_1_7": True,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
        "complete": True,
    }


def audit_luanti_15408_downstream(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist or reload the exact nonterminal downstream blocker."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15408 downstream blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _validate_upstream(root)
    payload = _readiness_payload(root, chain)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != _INPUTS or dict(existing.payload) != payload:
            raise RuntimeError("#15408 downstream blocker conflicts with exact state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=_INPUTS,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _BLOCKER)
    if artifact.inputs != _INPUTS or dict(artifact.payload) != payload:
        raise RuntimeError("#15408 downstream blocker typed reload failed")
    return artifact


if __name__ == "__main__":
    authorization = AuthorizationRecord(
        authorization_id="authorization/luanti-15408-downstream-audit-v1",
        scope="infrastructure-audit",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    artifact = audit_luanti_15408_downstream(
        Path(".warptest-cache/c1_lazarus_selection_v1"), authorization=authorization
    )
    print(artifact.reference.artifact_id)
    print(artifact.revision_path)


__all__ = ["audit_luanti_15408_downstream"]
