"""Candidate-specific Gate 8 receipt/plan for Luanti change 9722.

The current-v2 source pair has already been materialized as ordinary fixed and
buggy application trees.  This adapter only reloads that receipt and records
the downstream hand-off.  It does not build, launch, replay, touch any older
UE process, or inherit the earlier semantic-only closure.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .luanti_9722_candidate_materialization import load_luanti_9722_full_pair
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 9722}
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/9722/fixed-buggy/v2",
    "candidate_materialization",
    "2.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/9722/lz-luanti-9722-reversible-number-serialize/v3",
    "lazarus_task_mapping",
    "3.0",
)
_PLAN = ArtifactReference(
    "candidate_downstream_plan/luanti/9722/runtime-visual/v2",
    "candidate_downstream_plan",
    "2.0",
)
_CHAIN = (
    ArtifactReference(
        "candidate_source_packet/luanti/9722/source/v2",
        "candidate_source_packet",
        "2.0",
    ),
    ArtifactReference(
        "candidate_test_patch/luanti/9722/screening/v2",
        "candidate_test_patch",
        "2.0",
    ),
    ArtifactReference(
        "source_atomicity/luanti/9722/preannotation/v2",
        "source_atomicity",
        "2.0",
    ),
    ArtifactReference(
        "candidate_stage/luanti/9722/test_manifest/v2",
        "candidate_stage",
        "2.0",
    ),
    ArtifactReference(
        "candidate_gate57_aggregate/luanti/9722/lua/v1",
        "candidate_gate57_aggregate",
        "1.0",
    ),
    ArtifactReference(
        "candidate_gate57_transition/luanti/9722/lua/v1",
        "candidate_gate57_transition",
        "1.0",
    ),
    ArtifactReference(
        "fixed_source_capsule/luanti/9722/fixed/v2",
        "fixed_source_capsule",
        "2.0",
    ),
)


def write_luanti_9722_gate8_plan(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist a typed Gate 8 hand-off after validating the exact pair."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-planning"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#9722 Gate 8 plan authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    materialization = load_luanti_9722_full_pair(root)
    mapping = _load(root, _MAPPING)
    _validate(root, chain, materialization, mapping)
    payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-9722-reversible-number-serialize-v1",
        "state": "downstream_pending",
        "outcome": "gate8_ready_source_pair",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "no_old_artifact_inheritance": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_status": {
            "gate8_complete_fixed_buggy_application_versions": {
                "state": "passed_source_pair_no_build",
                "evidence": _MATERIALIZATION.artifact_id,
            },
            "gate9_registered_start_ordered_replay": "pending_no_registered_launch_receipt",
            "gate10_hidden_oracle_semantic_observations": "pending_no_runtime_observations",
            "gate11_common_before_fixed_after_buggy_after_goldens": "pending_no_frames",
            "gate12_before_equivalence_after_2x2_visual_cross_matrix": "pending_no_frames",
            "gate13_same_lineage_construction_receipt": "pending_gates_9_to_12",
        },
        "application_pair": {
            "fixed_tree_path": materialization.payload["fixed_tree_path"],
            "buggy_tree_path": materialization.payload["buggy_tree_path"],
            "materialization_mode": materialization.payload["materialization_mode"],
            "fixed_tree_fingerprint": materialization.payload["fixed_tree_fingerprint"],
            "buggy_tree_fingerprint": materialization.payload["buggy_tree_fingerprint"],
            "source_only": True,
            "candidate_specific_pair": True,
        },
        "registered_downstream_design": {
            "mapping_reference": _MAPPING.artifact_id,
            "registered_start": "world marker plus disposable mod-storage result",
            "ordered_replay": "restore_registered_start -> serialize roundtrip -> inspect result",
            "hidden_oracle": "both registered numbers survive exact serialize/deserialize",
            "semantic_observations": "goal, defect signature, and world responsiveness invariant",
            "golden_contract": "common-before/fixed-after/buggy-after plus visual cross-matrix",
        },
        "runtime_preflight": {
            "fixed_buggy_executable_pair_present": False,
            "native_toolchain_root": "private/dependencies/luanti-native/3.0",
            "registered_irrlichtmt_closure_present": False,
            "host_or_unregistered_binary_substitution_allowed": False,
        },
        "missing_required_evidence": [
            "registered_start_launch_receipts",
            "ordered_reference_replay_with_actions_and_waits",
            "raw_fixed_and_buggy_start_observations",
            "semantic_goal_defect_and_invariant_observations",
            "common_before_fixed_after_and_buggy_after_goldens",
            "pairwise_before_visual_equivalence",
            "two_by_two_after_visual_cross_matrix",
            "same_lineage_construction_receipt",
        ],
        "next_action": "register_headless_or_native_runtime_closure_then_run_bounded_start_replay",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (*_CHAIN, _MATERIALIZATION, _MAPPING)
    existing = _try_load(root, _PLAN)
    if existing is not None:
        if existing.inputs != inputs or existing.payload != payload:
            raise RuntimeError("#9722 Gate 8 plan conflicts with current-v2 state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_PLAN.artifact_id,
        artifact_kind=_PLAN.artifact_kind,
        semantic_version=_PLAN.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _PLAN)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#9722 Gate 8 plan typed reload failed")
    return artifact


def _validate(
    root: Path,
    chain: tuple[StableArtifact, ...],
    materialization: StableArtifact,
    mapping: StableArtifact,
) -> None:
    if any(item.payload.get("candidate") != _CANDIDATE for item in chain):
        raise RuntimeError("#9722 current-v2 admission chain is not candidate-bound")
    source, patch, atomicity, manifest, aggregate, transition, capsule = chain
    if source.payload.get("complete") is not True:
        raise RuntimeError("#9722 source packet is incomplete")
    if patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#9722 patch eligibility is not current")
    if atomicity.payload.get("outcome") != "accepted":
        raise RuntimeError("#9722 source atomicity is not accepted")
    if manifest.payload.get("state") != "passed":
        raise RuntimeError("#9722 test manifest is not passed")
    if (
        aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
    ):
        raise RuntimeError("#9722 fresh Gate 5--7 aggregate is not qualified")
    if (
        transition.payload.get("state") != "passed"
        or transition.payload.get("terminal") is not False
    ):
        raise RuntimeError("#9722 Gate 5--7 transition is not active")
    if (
        capsule.payload.get("source_tree_path")
        != "private/fixed-capsules/luanti/9722/fixed-source-v2"
    ):
        raise RuntimeError("#9722 fixed capsule drifted")
    if (
        materialization.reference != _MATERIALIZATION
        or materialization.payload.get("complete") is not True
    ):
        raise RuntimeError("#9722 materialization is not complete")
    if materialization.payload.get("no_old_artifact_inheritance") is not True:
        raise RuntimeError("#9722 materialization inherited an old artifact")
    if (
        mapping.payload.get("candidate") != _CANDIDATE
        or mapping.payload.get("complete") is not True
    ):
        raise RuntimeError("#9722 downstream mapping is not complete")
    for variant in ("fixed", "buggy"):
        relative = materialization.payload.get(f"{variant}_tree_path")
        if not isinstance(relative, str):
            raise RuntimeError("#9722 materialized tree path is invalid")
        tree = root / PurePosixPath(relative)
        info = tree.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("#9722 materialized tree is not an ordinary directory")
        for required in (
            "builtin/common/serialize.lua",
            "builtin/common/tests/serialize_spec.lua",
        ):
            file_info = (tree / required).lstat()
            if stat.S_ISLNK(file_info.st_mode) or not stat.S_ISREG(file_info.st_mode):
                raise RuntimeError(
                    "#9722 materialized tree is missing preserved test/production files"
                )


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
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_9722_gate8_plan"]
