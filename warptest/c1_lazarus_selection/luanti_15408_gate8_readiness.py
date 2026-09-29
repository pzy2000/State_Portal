"""Read-only Gate 8 readiness audit for Luanti #15408.

The candidate already has a qualified, fresh native Gate 5--7 aggregate.  This
module deliberately does not start a runtime or rebuild anything: it checks
the candidate-bound materialization, mapping, fixture, plan, start, replay,
and oracle seams and persists one typed, resumable blocker when those seams are
not present.
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


_ROOT_CANDIDATE = {"repository_key": "luanti", "change_number": 15408}

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
_BLOCKER = ArtifactReference(
    "candidate_gate8_readiness_blocker/luanti/15408/current-v2",
    "candidate_gate8_readiness_blocker",
    "1.0",
)

_INPUTS = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CAPSULE, _TRANSITION, _AGGREGATE)


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


def _validate_chain(root: Path) -> dict[str, StableArtifact]:
    """Load and mechanically validate only the candidate's current-v2 inputs."""

    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    capsule = _load(root, _CAPSULE)
    transition = _load(root, _TRANSITION)
    aggregate = _load(root, _AGGREGATE)

    if source.payload.get("candidate") != _ROOT_CANDIDATE or source.payload.get("complete") is not True:
        raise RuntimeError("#15408 source packet is not complete and candidate-bound")
    if (
        patch.payload.get("candidate") != _ROOT_CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("#15408 test patch is not eligible and candidate-bound")
    if (
        atomicity.payload.get("candidate") != _ROOT_CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 15027
    ):
        raise RuntimeError("#15408 source atomicity is not accepted as one authoritative issue")
    if (
        manifest.payload.get("candidate") != _ROOT_CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("state") != "passed"
        or tuple(manifest.payload.get("details", {}).get("executable_test_paths", ()))
        != ("src/unittest/test_collision.cpp",)
    ):
        raise RuntimeError("#15408 test manifest is not a passed executable manifest")
    if (
        capsule.payload.get("candidate") != _ROOT_CANDIDATE
        or capsule.payload.get("profile_id")
        != "luanti-15408-collision-acceleration-v1-native-gate57"
        or capsule.payload.get("source_tree_path")
        != "private/fixed-capsules/luanti/15408/fixed-source"
    ):
        raise RuntimeError("#15408 fixed source capsule is not candidate-bound")
    if (
        transition.payload.get("candidate") != _ROOT_CANDIDATE
        or transition.payload.get("state") != "passed"
        or transition.payload.get("outcome") != "qualified"
        or transition.payload.get("terminal") is not False
        or transition.payload.get("attempt_count") != 12
    ):
        raise RuntimeError("#15408 Gate 5--7 transition is not qualified 12-cell evidence")
    if (
        aggregate.payload.get("candidate") != _ROOT_CANDIDATE
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
        or aggregate.payload.get("repetitions_per_cell") != 3
    ):
        raise RuntimeError("#15408 native Gate 5--7 aggregate is not qualified 12-cell evidence")
    return {
        "source": source,
        "patch": patch,
        "atomicity": atomicity,
        "manifest": manifest,
        "capsule": capsule,
        "transition": transition,
        "aggregate": aggregate,
    }


def _readiness_payload(root: Path, chain: dict[str, StableArtifact]) -> dict[str, Any]:
    fixed_capsule_tree = root / "private/fixed-capsules/luanti/15408/fixed-source"
    candidate_pair_root = root / "private/materialized/luanti/15408"
    buggy_pair_path = candidate_pair_root / "application-v2/buggy"
    fixture_root = root / "private/downstream-fixtures/luanti/15408"
    mapping_artifact_root = root / "artifacts/lazarus_task_mapping/luanti/15408"
    plan_artifact_root = root / "artifacts/candidate_downstream_plan/luanti/15408"
    preflight_artifact_root = root / "artifacts/downstream_preflight/luanti/15408"
    start_artifact_root = root / "artifacts/candidate_runtime_start/luanti/15408"
    replay_artifact_root = root / "artifacts/candidate_replay/luanti/15408"
    oracle_artifact_root = root / "artifacts/candidate_oracle/luanti/15408"

    checks = {
        "fixed_capsule_artifact": {
            "path": str(chain["capsule"].revision_path),
            "present": chain["capsule"].revision_path.exists(),
        },
        "fixed_capsule_tree": {
            "path": str(fixed_capsule_tree),
            "present": fixed_capsule_tree.exists(),
        },
        "candidate_pair_root": {
            "path": str(candidate_pair_root),
            "present": candidate_pair_root.exists(),
        },
        "candidate_bound_buggy_pair": {
            "path": str(buggy_pair_path),
            "present": buggy_pair_path.exists(),
        },
        "candidate_bound_downstream_fixture": {
            "path": str(fixture_root),
            "present": fixture_root.exists(),
        },
        "candidate_bound_lazarus_task_mapping": {
            "path": str(mapping_artifact_root),
            "present": mapping_artifact_root.exists(),
        },
        "candidate_bound_downstream_plan": {
            "path": str(plan_artifact_root),
            "present": plan_artifact_root.exists(),
        },
        "candidate_bound_downstream_preflight": {
            "path": str(preflight_artifact_root),
            "present": preflight_artifact_root.exists(),
        },
        "candidate_bound_registered_start": {
            "path": str(start_artifact_root),
            "present": start_artifact_root.exists(),
        },
        "candidate_bound_ordered_replay": {
            "path": str(replay_artifact_root),
            "present": replay_artifact_root.exists(),
        },
        "candidate_bound_hidden_oracle": {
            "path": str(oracle_artifact_root),
            "present": oracle_artifact_root.exists(),
        },
    }

    transition_payload = chain["transition"].payload
    aggregate_payload = chain["aggregate"].payload
    return {
        "candidate": _ROOT_CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "gate8_candidate_mapping_fixture_and_fresh_runtime_closure_missing",
        "current_gate_status": {
            "gate_1_7": "qualified",
            "gate8_source_capsule": "passed",
            "gate8_fixed_buggy_pair": "blocked_missing_candidate_materialization",
            "gate8_mapping_fixture": "blocked_missing",
            "gate9_start_replay": "blocked_missing",
            "gate10_oracle": "blocked_missing",
            "gate11_to_13": "pending",
        },
        "readiness_checks": checks,
        "existing_gate57": {
            "transition": {
                "artifact_revision": str(chain["transition"].revision_path),
                "state": transition_payload.get("state"),
                "outcome": transition_payload.get("outcome"),
                "attempt_count": transition_payload.get("attempt_count"),
                "execution_lineage": transition_payload.get("execution_lineage"),
                "candidate_execution_performed": transition_payload.get(
                    "candidate_execution_performed"
                ),
            },
            "aggregate": {
                "artifact_revision": str(chain["aggregate"].revision_path),
                "qualified": aggregate_payload.get("qualified"),
                "attempt_count": aggregate_payload.get("attempt_count"),
                "repetitions_per_cell": aggregate_payload.get("repetitions_per_cell"),
                "profile_id": aggregate_payload.get("profile_id"),
            },
        },
        "missing_required_evidence": [
            "candidate_bound_fixed_buggy_application_pair",
            "candidate_bound_lazarus_task_mapping",
            "candidate_bound_downstream_fixture",
            "candidate_bound_downstream_plan",
            "candidate_bound_registered_start",
            "candidate_bound_ordered_replay",
            "candidate_bound_hidden_oracle",
        ],
        "next_action": "register_candidate_bound_mapping_fixture_and_materialize_fixed_buggy_pair_then_write_gate8_plan",
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


def audit_luanti_15408_gate8_readiness(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist or reload the candidate-specific Gate 8 readiness blocker."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15408 Gate 8 readiness authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _validate_chain(root)
    payload = _readiness_payload(root, chain)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != _INPUTS or dict(existing.payload) != payload:
            raise RuntimeError("#15408 Gate 8 readiness blocker conflicts with exact current state")
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
        raise RuntimeError("#15408 Gate 8 readiness blocker typed reload failed")
    return artifact


if __name__ == "__main__":
    root = Path(".warptest-cache/c1_lazarus_selection_v1")
    authorization = AuthorizationRecord(
        authorization_id="authorization/luanti-15408-gate8-readiness-v1",
        scope="infrastructure-audit",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    artifact = audit_luanti_15408_gate8_readiness(root, authorization=authorization)
    print(artifact.reference.artifact_id)
    print(artifact.revision_path)


__all__ = ["audit_luanti_15408_gate8_readiness"]
