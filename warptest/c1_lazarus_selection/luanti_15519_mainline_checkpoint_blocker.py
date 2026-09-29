"""Typed mainline checkpoint blocker for Luanti #15519.

The current-v4 server receipts close the candidate's registered Start, ordered
replay, hidden oracle, and real fixed/buggy semantic observations.  They do not
close the common-before frame, the two after frames, the visual cross-judgment,
or the same-lineage construction receipt.  This module performs only a stable
artifact audit and writes a resumable mainline checkpoint; it never launches a
process, reruns the v4 pair, edits the persistent materialization, or touches
the protected PIDs registered by the visual-capture plan.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_V1 = "1.0"
_V2 = "2.0"
_V4 = "4.0"
_V5 = "5.0"
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15519/fixed-buggy/v1",
    "candidate_materialization",
    _V1,
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/15519/fixed/v2", "fixed_source_capsule", _V2
)
_CLIENT_AGGREGATE = ArtifactReference(
    "candidate_gate57_15519_client_aggregate/luanti/15519/v1",
    "candidate_gate57_15519_client_aggregate",
    _V1,
)
_TASK_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/15519/issue-15518/v2",
    "lazarus_task_mapping",
    _V2,
)
_OBSERVATION = ArtifactReference(
    "candidate_headless_server_observation/luanti/15519/fixed-buggy/v4",
    "candidate_headless_server_observation",
    _V4,
)
_REPLAY = ArtifactReference(
    "candidate_headless_server_replay/luanti/15519/v4",
    "candidate_headless_server_replay",
    _V4,
)
_ORACLE = ArtifactReference(
    "candidate_headless_server_oracle/luanti/15519/v4",
    "candidate_headless_server_oracle",
    _V4,
)
_CONSTRUCTION = ArtifactReference(
    "candidate_headless_server_construction/luanti/15519/v4",
    "candidate_headless_server_construction",
    _V4,
)
_SEMANTIC = ArtifactReference(
    "candidate_headless_semantic_golden/luanti/15519/v4",
    "candidate_headless_semantic_golden",
    _V4,
)
_VISUAL = ArtifactReference(
    "candidate_headless_visual_golden_blocker/luanti/15519/v4",
    "candidate_headless_visual_golden_blocker",
    _V4,
)
_LINEAGE = ArtifactReference(
    "candidate_headless_construction_lineage/luanti/15519/v4",
    "candidate_headless_construction_lineage",
    _V4,
)
_PLAN = ArtifactReference(
    "candidate_headless_visual_capture_plan/luanti/15519/v5",
    "candidate_headless_visual_capture_plan",
    _V5,
)
_CHECKPOINT = ArtifactReference(
    "candidate_mainline_final_checkpoint_blocker/luanti/15519/v5",
    "candidate_mainline_final_checkpoint_blocker",
    _V1,
)
_PROTECTED_PIDS = (76485, 89366, 95665, 99762)
_MISSING = (
    "common_before_semantic_observation",
    "common_before_visual_frame",
    "fixed_after_visual_frame",
    "buggy_after_visual_frame",
    "pairwise_before_visual_equivalence",
    "two_by_two_after_visual_cross_matrix",
    "same_lineage_construction_receipt",
)


def write_luanti_15519_mainline_checkpoint_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Audit current Gate 9--12 outputs and persist a nonterminal checkpoint."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    artifacts = {
        "materialization": _load(root, _MATERIALIZATION),
        "capsule": _load(root, _CAPSULE),
        "client_aggregate": _load(root, _CLIENT_AGGREGATE),
        "task_mapping": _load(root, _TASK_MAPPING),
        "observation": _load(root, _OBSERVATION),
        "replay": _load(root, _REPLAY),
        "oracle": _load(root, _ORACLE),
        "construction": _load(root, _CONSTRUCTION),
        "semantic": _load(root, _SEMANTIC),
        "visual": _load(root, _VISUAL),
        "lineage": _load(root, _LINEAGE),
        "plan": _load(root, _PLAN),
    }
    _validate_inputs(artifacts)
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "mainline_final_checkpoint_blocked",
        "terminal": False,
        "resumable": True,
        "complete": True,
        "roster_modified": False,
        "admission_gate_1_7": "qualified_preserved",
        "gate_status": {
            "version_materialization": {
                "state": "passed_persistent_fixed_buggy_pair",
                "evidence": _MATERIALIZATION.artifact_id,
            },
            "registered_start_ordered_replay_hidden_oracle_semantic": {
                "state": "passed_current_v4_real_server",
                "evidence": {
                    "observation": _OBSERVATION.artifact_id,
                    "replay": _REPLAY.artifact_id,
                    "oracle": _ORACLE.artifact_id,
                },
            },
            "common_before_fixed_after_buggy_after_goldens": {
                "state": "blocked_common_before_and_visual_frames_missing",
                "evidence": _SEMANTIC.artifact_id,
            },
            "before_equivalence_after_2x2_visual_cross_judgment": {
                "state": "pending_no_real_frames",
                "evidence": _VISUAL.artifact_id,
            },
            "same_lineage_construction_receipt": {
                "state": "pending_visual_and_common_before",
                "evidence": _LINEAGE.artifact_id,
            },
            "mainline_final_checkpoint": "blocked_until_visual_and_lineage_gates_close",
        },
        "semantic_evidence": {
            "source": "current_v4_real_headless_observations",
            "fixed_after_observed": True,
            "buggy_after_observed": True,
            "common_before_observed": False,
            "synthetic_frames_written": False,
            "semantic_golden_state": artifacts["semantic"].payload.get("state"),
        },
        "visual_evidence": {
            "visual_capture_supported": False,
            "null_video_driver_only": True,
            "common_before": False,
            "fixed_after": False,
            "buggy_after": False,
            "before_equivalence": "pending",
            "after_2x2_cross_matrix": "pending",
            "visual_blocker": _VISUAL.artifact_id,
        },
        "construction_evidence": {
            "runtime_replay_oracle_state": artifacts["lineage"].payload.get(
                "runtime_replay_oracle_state"
            ),
            "construction_lineage_state": artifacts["lineage"].payload.get(
                "construction_lineage_state"
            ),
            "construction_reference": _CONSTRUCTION.artifact_id,
            "lineage_reference": _LINEAGE.artifact_id,
        },
        "missing_required_evidence": list(_MISSING),
        "safety": {
            "candidate_execution_performed_this_audit": False,
            "old_pair_rerun": False,
            "old_attempts_loaded": False,
            "persistent_materialization_modified": False,
            "protected_pids": list(_PROTECTED_PIDS),
            "protected_pids_touched": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        "next_action": (
            "register a fresh non-null client capture adapter in an isolated disposable "
            "copy, capture common-before/fixed-after/buggy-after real frames, issue the "
            "before-equivalence and 2x2 cross-judgment, then write a same-lineage "
            "construction receipt and re-evaluate the mainline final checkpoint"
        ),
    }
    inputs = tuple(
        artifacts[name].reference
        for name in (
            "materialization",
            "capsule",
            "client_aggregate",
            "task_mapping",
            "observation",
            "replay",
            "oracle",
            "construction",
            "semantic",
            "visual",
            "lineage",
            "plan",
        )
    )
    existing = _try_load(root, _CHECKPOINT)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("15519 mainline checkpoint conflicts with current state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_CHECKPOINT.artifact_id,
        artifact_kind=_CHECKPOINT.artifact_kind,
        semantic_version=_CHECKPOINT.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _CHECKPOINT)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("15519 mainline checkpoint typed reload failed")
    return artifact


def _validate_inputs(artifacts: dict[str, StableArtifact]) -> None:
    for name, artifact in artifacts.items():
        if artifact.payload.get("candidate") != _CANDIDATE:
            raise RuntimeError(f"15519 {name} is not candidate-bound")
    materialization = artifacts["materialization"]
    aggregate = artifacts["client_aggregate"]
    if materialization.payload.get("state") != "built" or materialization.payload.get("complete") is not True:
        raise RuntimeError("15519 persistent fixed/buggy materialization is not built")
    if aggregate.payload.get("qualified") is not True or aggregate.payload.get("state") != "qualified":
        raise RuntimeError("15519 Gate 1--7 client aggregate is not qualified")
    observation = artifacts["observation"].payload
    replay = artifacts["replay"].payload
    oracle = artifacts["oracle"].payload
    if not (
        observation.get("state") == "passed"
        and observation.get("fixed_buggy_pair") is True
        and observation.get("registered_start") is True
        and observation.get("ordered_replay") is True
        and observation.get("semantic_observations") is True
        and replay.get("state") == "passed"
        and replay.get("registered_start") is True
        and replay.get("ordered_replay") is True
        and replay.get("hidden_oracle") is True
        and replay.get("real_semantic_observations") is True
        and oracle.get("state") == "passed"
        and oracle.get("fixed_goal_observed") is True
        and oracle.get("buggy_defect_observed") is True
        and oracle.get("invariant_observed") is True
    ):
        raise RuntimeError("15519 Gate 9/10 server semantic outputs are incomplete")
    construction = artifacts["construction"].payload
    semantic = artifacts["semantic"].payload
    visual = artifacts["visual"].payload
    lineage = artifacts["lineage"].payload
    plan = artifacts["plan"].payload
    if construction.get("runtime_state") != "passed" or construction.get("oracle_state") != "passed":
        raise RuntimeError("15519 construction boundary lost runtime/oracle pass")
    if semantic.get("state") != "downstream_pending" or semantic.get("common_before", {}).get("state") != "missing":
        raise RuntimeError("15519 semantic golden unexpectedly changed")
    if not (
        semantic.get("fixed_after", {}).get("state") == "observed"
        and semantic.get("buggy_after", {}).get("state") == "observed"
        and semantic.get("fixed_after", {}).get("synthetic") is False
        and semantic.get("buggy_after", {}).get("synthetic") is False
    ):
        raise RuntimeError("15519 semantic after observations are incomplete")
    if not (
        visual.get("state") == "downstream_pending"
        and visual.get("visual_capture_supported") is False
        and visual.get("null_video_driver_only") is True
        and visual.get("synthetic_frames_written") is False
    ):
        raise RuntimeError("15519 visual blocker no longer describes missing frames")
    if not (
        lineage.get("state") == "downstream_pending"
        and lineage.get("construction_lineage_state") == "pending_visual_and_common_before"
        and lineage.get("runtime_replay_oracle_state") == "passed"
        and type(lineage.get("same_lineage_inputs")) is list
    ):
        raise RuntimeError("15519 construction lineage is not the expected pending state")
    if not (
        plan.get("state") == "downstream_pending"
        and plan.get("terminal") is False
        and plan.get("old_attempts_loaded") is False
        and plan.get("do_not_touch_pids") == list(_PROTECTED_PIDS)
    ):
        raise RuntimeError("15519 visual capture plan safety/status drifted")


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("15519 mainline checkpoint authorization is inactive")


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
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_15519_mainline_checkpoint_blocker"]
