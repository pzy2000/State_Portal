"""Typed downstream blocker for the current-v2 Luanti #9572 candidate.

The fresh current-v2 Gate 5--7 epoch for #9572 is qualified, but its capsule
contains only the candidate's Lua production/support/test files.  There is no
candidate-specific application pair, registered world-task mapping, or
runtime/capture closure from which Gate 8--13 can be claimed.  This module is
an infrastructure audit only: it performs no build, launch, input, replay, or
visual capture and never borrows another candidate's assets or attempts.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 9572}
_PROFILE = ArtifactReference(
    "candidate_gate57_9572_fresh_profile/luanti/9572/lua/v2",
    "candidate_gate57_9572_fresh_profile",
    "1.0",
)
_PREPARATION = ArtifactReference(
    "candidate_gate57_9572_fresh_preparation/luanti/9572/lua/v2",
    "candidate_gate57_9572_fresh_preparation",
    "1.0",
)
_CAPSULE = ArtifactReference(
    "candidate_gate57_9572_fresh_capsule/luanti/9572/lua/v2",
    "candidate_gate57_9572_fresh_capsule",
    "1.0",
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_9572_fresh_aggregate/luanti/9572/lua/v2",
    "candidate_gate57_9572_fresh_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_9572_fresh_transition/luanti/9572/lua/v2",
    "candidate_gate57_9572_fresh_transition",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate8_9572_downstream_blocker/luanti/9572/current-v2",
    "candidate_gate8_9572_downstream_blocker",
    "1.0",
)

_CAPSULE_TREE = PurePosixPath(
    "private/fixed-capsules/luanti/9572/fresh-source-v2"
)
_BUSTED_RUNTIME = PurePosixPath(
    "private/dependencies/luanti-lua-busted/1.0/bin/busted"
)
_APP_PAIR_ROOT = PurePosixPath("private/materialized/luanti/9572/current-v2")
_TASK_MAPPING_ROOT = PurePosixPath("artifacts/lazarus_task_mapping/luanti/9572")


def write_luanti_9572_downstream_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write and reload a resumable, non-terminal #9572 downstream blocker."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    profile = _load(root, _PROFILE)
    preparation = _load(root, _PREPARATION)
    capsule = _load(root, _CAPSULE)
    aggregate = _load(root, _AGGREGATE)
    transition = _load(root, _TRANSITION)
    _validate_gate57(profile, preparation, capsule, aggregate, transition)

    capsule_tree = root.joinpath(*_CAPSULE_TREE.parts)
    busted = root.joinpath(*_BUSTED_RUNTIME.parts)
    app_pair_root = root.joinpath(*_APP_PAIR_ROOT.parts)
    mapping_root = root.joinpath(*_TASK_MAPPING_ROOT.parts)
    payload: dict[str, object] = {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "new_application_launch_started": False,
        "old_attempts_inherited": False,
        "old_runtime_inherited": False,
        "other_candidate_assets_reused": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_status": {
            "gate1_4": "passed",
            "gate5_7": "passed_current_v2_fresh",
            "gate8_source_capsule": "passed_lua_capsule_only",
            "registered_task_mapping": "missing",
            "fixed_buggy_application_pair": "missing",
            "gate9_to_13": "pending",
        },
        "bound_inputs": {
            "profile": _PROFILE.artifact_id,
            "preparation": _PREPARATION.artifact_id,
            "capsule": _CAPSULE.artifact_id,
            "aggregate": _AGGREGATE.artifact_id,
            "transition": _TRANSITION.artifact_id,
        },
        "observed_current_v2": {
            "capsule_tree": _CAPSULE_TREE.as_posix(),
            "capsule_tree_present": capsule_tree.is_dir(),
            "registered_runtime": _BUSTED_RUNTIME.as_posix(),
            "registered_runtime_present": busted.is_file(),
            "candidate_application_pair_root": _APP_PAIR_ROOT.as_posix(),
            "candidate_application_pair_present": app_pair_root.is_dir(),
            "task_mapping_root": _TASK_MAPPING_ROOT.as_posix(),
            "task_mapping_present": mapping_root.is_dir(),
            "capsule_scope": [
                "builtin/common/misc_helpers.lua",
                "builtin/common/vector.lua",
                "builtin/common/tests/vector_spec.lua",
            ],
        },
        "missing_closure": [
            "current_v2_registered_lazarus_task_mapping",
            "same_lineage_fixed_application_or_server",
            "same_lineage_buggy_application_or_server",
            "registered_start_and_ordered_replay",
            "hidden_oracle_and_real_semantic_observations",
            "common_before_fixed_after_and_buggy_after_goldens",
            "before_visual_equivalence_and_after_2x2_cross_judgment",
            "same_lineage_construction_receipt",
        ],
        "reason": (
            "The fresh current-v2 Gate 5-7 closure is Lua/Busted-only; no "
            "#9572-specific application pair, world-task mapping, or capture "
            "runtime is registered.  Gate 8-13 cannot be inferred from the "
            "Lua attempts or from another candidate's materialization."
        ),
        "next_action": (
            "register a #9572 current-v2 task mapping and immutable fixed/buggy "
            "application/runtime closure, then request bounded Gate 8-10 "
            "execution; construct Gate 11-13 only from that same lineage"
        ),
        "resource_policy": {
            "network": False,
            "paid": False,
            "formal_trials": False,
        },
    }
    inputs = (
        _PROFILE,
        _PREPARATION,
        _CAPSULE,
        _AGGREGATE,
        _TRANSITION,
    )
    existing = _try_load(root, _BLOCKER)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#9572 downstream blocker conflicts with current state")
        return existing
    created_at = _now()
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=created_at,
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _BLOCKER)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#9572 downstream blocker typed reload failed")
    return artifact


def _validate_gate57(
    profile: StableArtifact,
    preparation: StableArtifact,
    capsule: StableArtifact,
    aggregate: StableArtifact,
    transition: StableArtifact,
) -> None:
    for artifact in (profile, preparation, capsule, aggregate, transition):
        if artifact.payload.get("candidate") != _CANDIDATE:
            raise RuntimeError("#9572 downstream input is not candidate-bound")
    if preparation.payload.get("state") != "prepared":
        raise RuntimeError("#9572 fresh preparation is not prepared")
    if aggregate.payload.get("qualified") is not True:
        raise RuntimeError("#9572 fresh Gate 5-7 aggregate is not qualified")
    if transition.payload.get("state") != "passed":
        raise RuntimeError("#9572 fresh Gate 5-7 transition is not passed")
    if profile.payload.get("candidate_execution_performed") is not False:
        raise RuntimeError("#9572 fresh profile would inherit execution")
    if capsule.payload.get("source_tree_path") != _CAPSULE_TREE.as_posix():
        raise RuntimeError("#9572 fresh capsule path is not candidate-specific")


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#9572 downstream blocker authorization is inactive")


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


__all__ = ["write_luanti_9572_downstream_blocker"]
